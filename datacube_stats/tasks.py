import logging
from typing import Iterator
from functools import partial

import fiona
from datacube import Datacube
from datacube.api import GridWorkflow, Tile
from datacube.api.query import query_group_by, query_geopolygon
from datacube.model import GridSpec
from datacube.utils.geometry import CRS, GeoBox, Geometry

from datacube_stats.models import StatsTask
from datacube_stats.utils.dates import filter_time_by_source
from datacube_stats.utils.tide_utility import features_from_file, get_filter_product
from .models import DataSource
from .utils import report_unmatched_datasets
from .utils.query import multi_product_list_cells
from .utils.timer import MultiTimer

DEFAULT_GROUP_BY = 'time'

_LOG = logging.getLogger(__name__)


def select_task_generator(input_region, storage, filter_product):
    if input_region is None or input_region == {}:
        _LOG.info('No input_region specified. Generating full available spatial region, gridded files.')
        return GriddedTaskGenerator(storage)

    elif 'geometry' in input_region:  # Larger spatial region
        # A large, multi-tile input region, specified as geojson. Output will be individual tiles.
        geometry = Geometry(input_region['geometry'], CRS('EPSG:4326'))  # GeoJSON is always 4326
        return GriddedTaskGenerator(storage, geopolygon=geometry, tile_indexes=input_region.get('tiles'))

    elif 'tile' in input_region:  # For one tile
        return GriddedTaskGenerator(storage, tile_indexes=[input_region['tile']])

    elif 'tiles' in input_region:  # List of tiles
        return GriddedTaskGenerator(storage, tile_indexes=input_region['tiles'])

    elif 'from_file' in input_region:
        _LOG.info('Input spatial region specified by file: %s', input_region['from_file'])

        if 'feature_id' in input_region or input_region.get('gridded') is False:
            _LOG.info('Generating tasks based on feature polygons.')
            features = features_from_file(input_region['from_file'], input_region.get('feature_id'))

            return NonGriddedTaskGenerator(input_region=input_region,
                                           filter_product=filter_product,
                                           features=features, storage=storage)

        else:
            _LOG.info('Generating tasks based on grid.')
            geometry = boundary_polygon_from_file(input_region['from_file'])
            return GriddedTaskGenerator(storage, geopolygon=geometry)
    else:
        _LOG.info('Generating statistics for an ungridded `input region`. Output as a single file.')
        return NonGriddedTaskGenerator(input_region=input_region, storage=storage,
                                       filter_product=filter_product)


def boundary_polygon_from_file(filename: str) -> Geometry:
    # TODO: This should be refactored and moved into datacube.utils.geometry
    import shapely.ops
    from shapely.geometry import shape, mapping
    with fiona.open(filename) as input_region:
        joined = shapely.ops.unary_union(list(shape(geom['geometry']).buffer(0) for geom in input_region))
        final = joined.convex_hull
        crs = CRS(input_region.crs_wkt)
        boundary_polygon = Geometry(mapping(final), crs)
    return boundary_polygon


class GriddedTaskGenerator:
    def __init__(self, storage, geopolygon=None, tile_indexes=None):
        self.grid_spec = _make_grid_spec(storage)
        self.geopolygon = geopolygon
        self.tile_indexes = tile_indexes
        self._total_unmatched = 0

    def __call__(self, index, sources_spec, date_ranges) -> Iterator[StatsTask]:
        """
        Generate the required tasks through time and across a spatial grid.

        Input region can be limited by specifying either/or both of `geopolygon` and `cell_index`, which
        will both result in only datasets covering the poly or cell to be included.

        :param index: Datacube Index
        :return:
        """
        workflow = GridWorkflow(index, grid_spec=self.grid_spec)

        for time_period in date_ranges:
            _LOG.info('Making output product tasks for time period: %s', time_period)
            timer = MultiTimer().start('creating_tasks')
            created_tasks = 0

            if self.tile_indexes is not None:
                for tile_index in self.tile_indexes:
                    _LOG.debug('task for tile %s', tile_index)
                    for task in self.collect_tasks(workflow, time_period, sources_spec, tile_index):
                        created_tasks += 1
                        yield task
            else:
                for task in self.collect_tasks(workflow, time_period, sources_spec):
                    created_tasks += 1
                    yield task

            # is timing it still appropriate here?
            timer.pause('creating_tasks')
            if created_tasks:
                _LOG.info('Created %s tasks for time period: %s. In: %s',
                          created_tasks, time_period, timer)

    def collect_tasks(self, workflow, time_period, sources_spec, tile_index=None):
        """ Collect tasks for a time period. """
        # Tasks are grouped by tile_index, and may contain sources from multiple places
        # Each source may be masked by multiple masks

        # pylint: disable=too-many-locals
        tasks = {}

        for source_index, source_spec in enumerate(sources_spec):
            ep_range = filter_time_by_source(source_spec.get('time'), time_period)
            if ep_range is None:
                _LOG.info("Datasets not included for %s and time range for %s", source_spec['product'], time_period)
                continue
            group_by_name = source_spec.get('group_by', DEFAULT_GROUP_BY)

            products = [source_spec['product']] + [mask['product'] for mask in source_spec.get('masks', [])]

            product_query = {products[0]: {'source_filter': source_spec.get('source_filter', None)}}

            (data, *masks), unmatched_ = multi_product_list_cells(products, workflow,
                                                                  product_query=product_query,
                                                                  cell_index=tile_index,
                                                                  time=ep_range,
                                                                  group_by=group_by_name,
                                                                  geopolygon=self.geopolygon)

            self._total_unmatched += report_unmatched_datasets(unmatched_[0], _LOG.warning)

            for tile, sources in data.items():
                task = tasks.setdefault(tile, StatsTask(time_period=ep_range, spatial_id={'x': tile[0], 'y': tile[1]}))
                task.sources.append(DataSource(data=sources,
                                               masks=[mask.get(tile) for mask in masks],
                                               spec=source_spec,
                                               source_index=source_index))

        return list(tasks.values())

    def __del__(self):
        if self._total_unmatched > 0:
            _LOG.warning('There were source datasets for which masks were not found, total: %d',
                         self._total_unmatched)


def _make_grid_spec(storage) -> GridSpec:
    """Make a grid spec based on a storage spec."""
    assert 'tile_size' in storage

    crs = CRS(storage['crs'])
    return GridSpec(crs=crs,
                    tile_size=[storage['tile_size'][dim] for dim in crs.dimensions],
                    resolution=[storage['resolution'][dim] for dim in crs.dimensions])


class NonGriddedTaskGenerator:
    """
    Make stats tasks for a single defined spatial region, not part of a grid.

    Usage:

    ngtg = NonGriddedTaskGenerator(input_region, filter_product, bounds, feature, storage)

    tasks = ngtg(index, sources_spec, date_ranges)

    :param input_region:
    :param storage:
    """

    def __init__(self, input_region, filter_product, storage, features=None):
        self.input_region = input_region
        self.filter_product = filter_product
        self.features = features
        self.storage = storage

    def set_task(self, task, filtered_times):
        """
        Set up task after applying filtered date/times
        :param task:
        :param filtered_times: Filtered date/times depending on products
        :return: new task sources
        """
        remove_index_list = list()

        for i, sr in enumerate(task.sources):
            v = sr.data
            if self.filter_product.get('method') == "by_hydrological_months":
                all_dates = [s.strftime("%Y-%m-%d") for s in
                             v.sources.time.values.astype('M8[s]').astype('O').tolist()]
            elif self.filter_product.get('method') == "by_tide_height":
                all_dates = [s.strftime("%Y-%m-%dT%H:%M:%S") for s in
                             v.sources.time.values.astype('M8[s]').astype('O').tolist()]
            if set(all_dates) & set(filtered_times):
                v.sources = v.sources.isel(time=[i for i, item in enumerate(all_dates) if item in
                                                 filtered_times])
                _LOG.info("source included %s", v.sources.time)
            else:
                remove_index_list.append(i)
        if len(remove_index_list) > 0:
            # NOTE pretty sure this should not work
            for i in remove_index_list:
                del task.sources[i]
        return task

    def filter_task(self, task, feature, date_ranges):
        """
        Filters dates and added to task geometry feature, crs txt and extra file name details
        :param task: For filter product, it reset the sources, setup geometry features and filename
        :param date_ranges: This is passed onto filter product function as epoch range
        :return: new task in case of filtering
        """
        all_source_times = list()
        if self.filter_product is not None and self.filter_product != {}:
            for sr in task.sources:
                v = sr.data
                all_source_times = (all_source_times +
                                    [dd for dd in v.sources.time.data.astype('M8[s]').astype('O').tolist()])
            all_source_times = sorted(all_source_times)

            extra_fn_args, filtered_times = get_filter_product(self.filter_product,
                                                               feature,
                                                               all_source_times, date_ranges)
            _LOG.info("Filtered times %s", filtered_times)
            task = self.set_task(task, filtered_times)

            # preserving old questionable behavior
            task.spatial_id = {'x': extra_fn_args[0], 'y': extra_fn_args[1]}

        return task

    def __call__(self, index, sources_spec, date_ranges) -> Iterator[StatsTask]:
        """

        :param index: database index
        :return: an iterator of StatTask objects to execute
        """
        features = self.features
        if features is None:
            # input region not from a shapefile
            features = [None]

        for feature in features:

            if feature is None or feature.id is None:
                feature_id = '(none)'
            else:
                feature_id = str(feature.id)

            for time_period in date_ranges:
                task = StatsTask(time_period=time_period, spatial_id={'feature_id': feature_id}, feature=feature)
                _LOG.info('Making output product tasks for time period: %s, feature: %s', time_period, feature_id)

                for source_index, source_spec in enumerate(sources_spec):
                    ep_range = filter_time_by_source(source_spec.get('time'), time_period)
                    if ep_range is None:
                        _LOG.info("Datasets not included for %s and time range for %s", source_spec['product'],
                                  time_period)
                        continue

                    # Build Tile
                    make_tile = partial(ArbitraryTileMaker(self.input_region, feature, self.storage),
                                        index=index, time=ep_range,
                                        group_by=source_spec.get('group_by', DEFAULT_GROUP_BY))

                    data = make_tile(product=source_spec['product'])
                    masks = [make_tile(product=mask['product'])
                             for mask in source_spec.get('masks', [])]

                    if len(data.sources.time) == 0:
                        _LOG.info("No matched for product %s", source_spec['product'])
                        continue

                    task.sources.append(DataSource(data=data,
                                                   masks=masks,
                                                   spec=source_spec,
                                                   source_index=source_index))

                _LOG.info("make tile finished")
                if task.sources:
                    # Function which takes a Tile, containing sources, and returns a new 'filtered' Tile
                    task = self.filter_task(task, feature, date_ranges)
                    _LOG.info('Created task for time period: %s', time_period)
                    yield task


class ArbitraryTileMaker:
    """
    Create a :class:`Tile` which can be used by :class:`GridWorkflow` to later load the required data.

    :param input_region: dictionary of spatial limits for searching for datasets. eg:
            geopolygon
            lat, lon boundaries

    """

    def __init__(self, input_region, feature, storage):
        self.input_region = input_region
        self.feature = feature
        self.storage = storage

    def __call__(self, index, product, time, group_by) -> Tile:
        # Do for a specific poly whose boundary is known
        output_crs = CRS(self.storage['crs'])
        filtered_items = ['geopolygon', 'lon', 'lat', 'longitude', 'latitude', 'x', 'y']
        filtered_dict = {k: v for k, v in self.input_region.items() if k in filtered_items}
        if self.feature is not None:
            filtered_dict['geopolygon'] = self.feature.geopolygon
            geopoly = filtered_dict['geopolygon']
        else:
            geopoly = query_geopolygon(**self.input_region)

        dc = Datacube(index=index)
        datasets = dc.find_datasets(product=product, time=time, group_by=group_by, **filtered_dict)
        group_by = query_group_by(group_by=group_by)
        sources = dc.group_datasets(datasets, group_by)
        output_resolution = [self.storage['resolution'][dim] for dim in output_crs.dimensions]
        geopoly = geopoly.to_crs(output_crs)
        geobox = GeoBox.from_geopolygon(geopoly, resolution=output_resolution)

        return Tile(sources, geobox)
