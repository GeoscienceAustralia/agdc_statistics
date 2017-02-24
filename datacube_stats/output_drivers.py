"""
Provide some classes for writing data out to files on disk.

The `NetcdfOutputDriver` will write multiple variables into a single file.

The `RioOutputDriver` writes a single __band__ of data per file.
"""
import abc
import logging
import operator
from collections import OrderedDict
from functools import reduce as reduce_
from six import with_metaclass
from pathlib import Path
import subprocess

import numpy
import rasterio
import xarray

from datacube.model import Variable, GeoPolygon
from datacube.model.utils import make_dataset, xr_apply, datasets_to_doc
from datacube.storage import netcdf_writer
from datacube.storage.storage import create_netcdf_storage_unit
from datacube.utils import unsqueeze_data_array, geometry

_LOG = logging.getLogger(__name__)
_NETCDF_VARIABLE__PARAMETER_NAMES = {'zlib',
                                     'complevel',
                                     'shuffle',
                                     'fletcher32',
                                     'contiguous',
                                     'attrs'}

OUTPUT_DRIVERS = {}


class RegisterDriver(abc.ABCMeta):
    def __new__(mcs, name, bases, class_dict):
        cls = type.__new__(mcs, name, bases, class_dict)
        name = cls.__name__.replace('OutputDriver', '')
        if name:
            OUTPUT_DRIVERS[name] = cls
        return cls


class StatsOutputError(Exception):
    pass


class OutputFileAlreadyExists(Exception):
    def __init__(self, output_file=None):
        self._output_file = output_file

    def __str__(self):
        return 'Output file already exists: {}'.format(self._output_file)

    def __repr__(self):
        return "OutputFileAlreadyExists({})".format(self._output_file)


def _walk_dict(file_handles, func):
    """

    :param file_handles:
    :param func: returns iterable
    :return:
    """
    for _, output_fh in file_handles.items():
        if isinstance(output_fh, dict):
            _walk_dict(output_fh, func)
        else:
            try:
                yield func(output_fh)
            except TypeError as te:
                _LOG.debug('Error running %s: %s', func, te)


class OutputDriver(with_metaclass(RegisterDriver)):
    """
    Handles the creation of output data files for a StatsTask.

    Depending on the implementation, may create one or more files per instance.

    To use, instantiate the class, using it as a context manager, eg.

        with MyOutputDriver(task, storage, output_path):
            output_driver.write_data(prod_name, measure_name, tile_index, values)

    :param StatsTask task: A StatsTask that will be producing data
        A task will contain 1 or more output products, with each output product containing 1 or more measurements
    :param Union(Path, str) output_path: Base directory name to output file/s into
    :param storage: Dictionary describing the _storage format. eg.
        {
          'driver': 'NetCDF CF'
          'crs': 'EPSG:3577'
          'tile_size': {
                  'x': 100000.0
                  'y': 100000.0}
          'resolution': {
                  'x': 25
                  'y': -25}
          'chunking': {
              'x': 200
              'y': 200
              'time': 1}
          'dimension_order': ['time', 'y', 'x']}
    :param app_info:
    """
    valid_extensions = []

    def __init__(self, task, storage, output_path, app_info=None):
        self._storage = storage

        self._output_path = output_path

        self._app_info = app_info

        self._output_file_handles = {}

        #: datacube_stats.models.StatsTask
        self._task = task

        self._geobox = task.geobox
        self._output_products = task.output_products

    def close_files(self, completed_successfully, rename_tmps=True):
        # Turn file_handles into paths
        paths = list(_walk_dict(self._output_file_handles, self._handle_to_path))

        # Close Files, need to iterate through generator so as not to be lazy
        closed = list(_walk_dict(self._output_file_handles, lambda fh: fh.close()))

        # Remove '.tmp' suffix
        if completed_successfully and rename_tmps:
            paths = [path.rename(str(path)[:-4]) for path in paths]
        return paths

    def _handle_to_path(self, file_handle):
        return Path(file_handle.name)

    @abc.abstractmethod
    def open_output_files(self):
        raise NotImplementedError

    @abc.abstractmethod
    def write_data(self, prod_name, measurement_name, tile_index, values):
        if len(self._output_file_handles) <= 0:
            raise StatsOutputError('No files opened for writing.')

    @abc.abstractmethod
    def write_global_attributes(self, attributes):
        raise NotImplementedError

    def __enter__(self):
        self.open_output_files()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        completed_successfully = exc_type is None
        self.close_files(completed_successfully)

    def _prepare_output_file(self, stat, **kwargs):
        """
        Format the output filename for the current task,
        make sure it is valid and doesn't already exist
        Make sure parent directories exist
        Switch it around for a temporary filename.
        
        :return: Path to write output to
        """
        output_path = self._generate_output_filename(kwargs, stat)

        if output_path.suffix not in self.valid_extensions:
            raise StatsOutputError('Invalid Filename: %s for this Output Driver: %s' % (output_path, self))

        if output_path.exists():
            raise OutputFileAlreadyExists(output_path)

        try:
            output_path.parent.mkdir(parents=True)
        except OSError:
            pass

        tmp_path = output_path.with_suffix(output_path.suffix + '.tmp')
        if tmp_path.exists():
            tmp_path.unlink()

        return tmp_path

    def _generate_output_filename(self, kwargs, stat):
        # Fill parameters from config file filename specification
        x, y = self._task.tile_index
        epoch_start, epoch_end = self._task.time_period
        output_path = Path(self._output_path,
                           stat.file_path_template.format(
                               x=x, y=y,
                               epoch_start=epoch_start,
                               epoch_end=epoch_end,
                               **kwargs))
        return output_path

    def _find_source_datasets(self, stat, uri=None):
        """
        Find all the source datasets for a task

        Put them in order so that they can be assigned to a stacked output aligned against it's time dimension
        :return: (datasets, sources)
        """
        task = self._task
        geobox = self._task.geobox
        app_info = self._app_info

        def _make_dataset(labels, sources_):
            return make_dataset(product=stat.product,
                                sources=sources_,
                                extent=geobox.extent,
                                center_time=labels['time'],
                                uri=uri,
                                app_info=app_info,
                                valid_data=GeoPolygon.from_sources_extents(sources_, geobox))

        def merge_sources(prod):
            all_sources = xarray.align(prod['data'].sources, *[mask_tile.sources for mask_tile in prod['masks']])
            return reduce_(operator.add, (sources_.sum() for sources_ in all_sources))

        start_time, _ = task.time_period
        sources = reduce_(operator.add, (merge_sources(prod) for prod in task.sources))
        sources = unsqueeze_data_array(sources, dim='time', pos=0, coord=start_time,
                                       attrs=task.time_attributes)

        datasets = xr_apply(sources, _make_dataset, dtype='O')  # Store in DataArray to associate Time -> Dataset
        datasets = datasets_to_doc(datasets)
        return datasets, sources


class NetCDFCFOutputDriver(OutputDriver):
    """
    Write data to Datacube compatible NetCDF files

    The variables in the file will be 3 dimensional, with a single time dimension + y,x.
    """

    valid_extensions = ['.nc']

    def open_output_files(self):
        for prod_name, stat in self._output_products.items():
            output_filename = self._prepare_output_file(stat)
            self._output_file_handles[prod_name] = self._create_storage_unit(stat, output_filename)

    def _handle_to_path(self, file_handle):
        return Path(file_handle.filepath())

    def _create_storage_unit(self, stat, output_filename):
        all_measurement_defns = list(stat.product.measurements.values())

        datasets, sources = self._find_source_datasets(stat, uri=output_filename.as_uri())

        variable_params = self._create_netcdf_var_params(stat)
        nco = self._nco_from_sources(sources,
                                     self._geobox,
                                     all_measurement_defns,
                                     variable_params,
                                     output_filename)

        netcdf_writer.create_variable(nco, 'dataset', datasets, zlib=True)
        nco['dataset'][:] = netcdf_writer.netcdfy_data(datasets.values)
        return nco

    def _create_netcdf_var_params(self, stat):
        chunking = self._storage['chunking']
        chunking = [chunking[dim] for dim in self._storage['dimension_order']]

        variable_params = {}
        for measurement in stat.data_measurements:
            name = measurement['name']
            variable_params[name] = stat.output_params.copy()
            variable_params[name]['chunksizes'] = chunking
            variable_params[name].update(
                {k: v for k, v in measurement.items() if k in _NETCDF_VARIABLE__PARAMETER_NAMES})
        return variable_params

    @staticmethod
    def _nco_from_sources(sources, geobox, measurements, variable_params, filename):
        coordinates = OrderedDict((name, geometry.Coordinate(coord.values, coord.units))
                                  for name, coord in sources.coords.items())
        coordinates.update(geobox.coordinates)

        variables = OrderedDict((variable['name'], Variable(dtype=numpy.dtype(variable['dtype']),
                                                            nodata=variable['nodata'],
                                                            dims=sources.dims + geobox.dimensions,
                                                            units=variable['units']))
                                for variable in measurements)

        return create_netcdf_storage_unit(filename, geobox.crs, coordinates, variables, variable_params)

    def write_data(self, prod_name, measurement_name, tile_index, values):
        self._output_file_handles[prod_name][measurement_name][(0,) + tile_index[1:]] = netcdf_writer.netcdfy_data(
            values)
        self._output_file_handles[prod_name].sync()
        _LOG.debug("Updated %s %s", measurement_name, tile_index[1:])

    def write_global_attributes(self, attributes):
        for output_file in self._output_file_handles.values():
            for k, v in attributes.items():
                output_file.attrs[k] = v


class GeotiffOutputDriver(OutputDriver):
    """
    Save data to file/s using rasterio. Eg. GeoTiff

    Con write all statistics to the same output file, or each statistic to a different file.
    """
    valid_extensions = ['.tif', '.tiff']
    default_profile = {
        'compress': 'lzw',
        'driver': 'GTiff',
        'interleave': 'band',
        'tiled': True
    }
    _dtype_map = {
        'int8': 'uint8'
    }

    def __init__(self, *args, **kwargs):
        super(GeotiffOutputDriver, self).__init__(*args, **kwargs)

        self._measurement_bands = {}

    def _get_dtype(self, out_prod_name, measurement_name=None):
        if measurement_name:
            return self._output_products[out_prod_name].product.measurements[measurement_name]['dtype']
        else:
            dtypes = set(m['dtype'] for m in self._output_products[out_prod_name].product.measurements.values())
            if len(dtypes) == 1:
                return dtypes.pop()
            else:
                raise StatsOutputError('Not all measurements for %s have the same dtype.'
                                       'For GeoTiff output they must ' % out_prod_name)

    def _get_nodata(self, prod_name, measurement_name=None):
        dtype = self._get_dtype(prod_name, measurement_name)
        if measurement_name:
            nodata = self._output_products[prod_name].product.measurements[measurement_name]['nodata']
        else:
            nodatas = set(m['nodata'] for m in self._output_products[prod_name].product.measurements.values())
            if len(nodatas) == 1:
                nodata = nodatas.pop()
            else:
                raise StatsOutputError('Not all nodata values for output product "%s" are the same. '
                                       'Must all match for geotiff output' % prod_name)
        if dtype == 'uint8' and nodata < 0:
            # Convert to uint8 for Geotiff
            return 255
        else:
            return nodata

    def open_output_files(self):
        for prod_name, stat in self._output_products.items():

            # TODO: Save Dataset Metadata
            datasets, sources = self._find_source_datasets(stat, uri=output_filename.as_uri())

            num_measurements = len(stat.product.measurements)
            if num_measurements == 0:
                raise ValueError('No measurements to record for {}.'.format(prod_name))
            elif num_measurements > 1 and 'var_name' in stat.file_path_template:
                # Output each statistic product into a separate single band geotiff file
                for measurement_name, measure_def in stat.product.measurements.items():
                    self._open_single_band_geotiff(prod_name, stat, measurement_name)
            else:
                # Output all statistics into a single geotiff file, with as many bands
                # as there are output statistic products
                output_filename = self._prepare_output_file(stat, var_name=measurement_name)
                dest_fh = self._open_geotiff(prod_name, None, output_filename, num_measurements)

                for band, (measurement_name, measure_def) in enumerate(stat.product.measurements.items(), start=1):
                    self._set_band_metadata(dest_fh, measurement_name, band=band)
                self._output_file_handles[prod_name] = dest_fh

    def _open_single_band_geotiff(self, prod_name, stat, measurement_name=None):
        output_filename = self._prepare_output_file(stat, var_name=measurement_name)
        dest_fh = self._open_geotiff(prod_name, measurement_name, output_filename)
        self._set_band_metadata(dest_fh, measurement_name)
        self._output_file_handles.setdefault(prod_name, {})[measurement_name] = dest_fh

    def _set_band_metadata(self, dest_fh, measurement_name, band=1):
        start_date, end_date = self._task.time_period
        dest_fh.update_tags(band,
                            source_product=self._task.source_product_names(),
                            start_date='{:%Y-%m-%d}'.format(start_date),
                            end_date='{:%Y-%m-%d}'.format(end_date),
                            name=measurement_name)

    def _open_geotiff(self, prod_name, measurement_name, output_filename, num_bands=1):
        profile = self.default_profile.copy()
        dtype = self._get_dtype(prod_name, measurement_name)
        nodata = self._get_nodata(prod_name, measurement_name)
        x_block_size = self._storage['chunking']['x'] if 'x' in self._storage['chunking'] else self._storage['chunking']['longitude']
        y_block_size = self._storage['chunking']['y'] if 'y' in self._storage['chunking'] else self._storage['chunking']['latitude']
        profile.update({
            'blockxsize': x_block_size,
            'blockysize': y_block_size,

            'dtype': dtype,
            'nodata': nodata,
            'width': self._geobox.width,
            'height': self._geobox.height,
            'affine': self._geobox.affine,
            'crs': self._geobox.crs.crs_str,
            'count': num_bands
        })
        _LOG.debug("Opening %s for writing.", output_filename)
        dest_fh = rasterio.open(str(output_filename), mode='w', **profile)
        dest_fh.update_tags(created=self._app_info)
        return dest_fh

    def write_data(self, prod_name, measurement_name, tile_index, values):
        super(GeotiffOutputDriver, self).write_data(prod_name, measurement_name, tile_index, values)

        prod = self._output_file_handles[prod_name]
        if isinstance(prod, dict):
            output_fh = prod[measurement_name]
            band_num = 1
        else:
            output_fh = prod
            stat = self._output_products[prod_name]
            band_num = list(stat.product.measurements).index(measurement_name) + 1

        t, y, x = tile_index
        window = ((y.start, y.stop), (x.start, x.stop))
        _LOG.debug("Updating %s.%s %s", prod_name, measurement_name, window)

        dtype = self._get_dtype(prod_name, measurement_name)

        output_fh.write(values.astype(dtype), indexes=band_num, window=window)

    def write_global_attributes(self, attributes):
        for dest in self._output_file_handles.values():
            dest.update_tags(**attributes)


class ENVIBILOutputDriver(GeotiffOutputDriver):
    """
    Writes out a tif file (with an incorrect extension), then converts it to another GDAL format.
    """
    valid_extensions = ['.bil']

    def close_files(self, completed_successfully):
        paths = super(ENVIBILOutputDriver, self).close_files(completed_successfully, rename_tmps=False)

        if completed_successfully:
            for filename in paths:
                self._tif_to_envi(filename)

    @staticmethod
    def _tif_to_envi(source_file):
        dest_file = source_file.with_name(source_file.stem)
        tmp_tif = source_file.with_suffix('.tif')
        source_file.replace(tmp_tif)
        gdal_translate_command = ['gdal_translate', '--debug', 'ON', '-of', 'ENVI', '-co', 'INTERLEAVE=BIL',
                                  str(tmp_tif), str(dest_file)]

        _LOG.debug('Executing: ' + ' '.join(gdal_translate_command))

        try:
            subprocess.check_output(gdal_translate_command, stderr=subprocess.STDOUT)

            tmp_tif.unlink()
        except subprocess.CalledProcessError as cpe:
            _LOG.error('Error running gdal_translate: %s', cpe.output)


class TestOutputDriver(OutputDriver):
    def write_global_attributes(self, attributes):
        pass

    def write_data(self, prod_name, measurement_name, tile_index, values):
        pass

    def open_output_files(self):
        pass


def _format_filename(path_template, **kwargs):
    x, y = kwargs['tile_index']
    epoch_start, epoch_end = kwargs['time_period']
    return Path(str(path_template).format(x=x, y=y, epoch_start=epoch_start, epoch_end=epoch_end,
                                          **kwargs))


def _polygon_from_sources_extents(sources, geobox):
    sources_union = geometry.unary_union(source.extent.to_crs(geobox.crs) for source in sources)
    valid_data = geobox.extent.intersection(sources_union)
    return valid_data


class XarrayOutputDriver(OutputDriver):
    def write_data(self, prod_name, measurement_name, tile_index, values):
        pass

    def write_global_attributes(self, attributes):
        pass

    def open_output_files(self):
        pass

