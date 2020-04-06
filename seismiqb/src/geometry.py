""" SeismicGeometry-class containing geometrical info about seismic-cube."""
import os
import sys
import logging
from textwrap import dedent
from random import random
from itertools import product
from tqdm.auto import tqdm

import numpy as np
import pandas as pd
import h5py
import segyio
import h5pickle

from .utils import lru_cache, find_min_max
from .plot_utils import plot_images_overlap





class SpatialDescriptor:
    """ Allows to set names for parts of information about index.
    ilines_len = SpatialDescriptor('INLINE_3D', 'lens', 'ilines_len')
    allows to get instance.lens[idx], where `idx` is position of `INLINE_3D` inside instance.index.

    Roughly equivalent to::
    @property
    def ilines_len(self):
        idx = self.index.index('INLINE_3D')
        return self.lens[idx]
    """
    def __set_name__(self, owner, name):
        self.name = name

    def __init__(self, header=None, attribute=None, name=None, ):
        self.header = header
        self.attribute = attribute

        if name is not None:
            self.name = name

    def __get__(self, obj, obj_class=None):
        # If attribute is already stored in object, just return it
        if self.name in obj.__dict__:
            return obj.__dict__[self.name]

        # Find index of header, use it to access attr
        try:
            idx = obj.index.index(self.header)
            return getattr(obj, self.attribute)[idx]
        except ValueError:
            raise ValueError(f'Current index does not contain {self.header}.')


def add_descriptors(cls):
    """ Add multiple descriptors to the decorated class.
    Name of each descriptor is `alias + postfix`.

    Roughly equivalent to::
    ilines = SpatialDescriptor('INLINE_3D', 'vals', 'ilines')
    xlines = SpatialDescriptor('CROSSLINE_3D', 'vals', 'ilines')

    ilines_len = SpatialDescriptor('INLINE_3D', 'lens', 'ilines_len')
    xlines_len = SpatialDescriptor('CROSSLINE_3D', 'lens', 'ilines_len')
    etc
    """
    attrs = ['vals', 'offsets', 'lens', 'uniques'] # which attrs hold information
    postfixes = ['', '_offset', '_len', '_unique'] # postfix of current attr

    headers = ['INLINE_3D', 'CROSSLINE_3D'] # headers to use
    aliases = ['ilines', 'xlines'] # alias for header

    for attr, postfix in zip(attrs, postfixes):
        for alias, header in zip(aliases, headers):
            name = alias + postfix
            descriptor = SpatialDescriptor(header=header, attribute=attr, name=name)
            setattr(cls, name, descriptor)
    return cls



@add_descriptors
class SeismicGeometry:
    """ !!. """
    #TODO: add separate class for cube-like labels
    #pylint: disable=attribute-defined-outside-init, too-many-instance-attributes, too-many-public-methods
    SEGY_ALIASES = ['sgy', 'segy', 'seg']
    HDF5_ALIASES = ['hdf5', 'h5py']

    # Attributes to store during SEG-Y -> HDF5 conversion
    PRESERVED = [
        'depth', 'delay', 'sample_rate',
        'fields', 'offsets', 'uniques', 'lens', # vals can't be saved due to different lenghts of arrays
        'value_min', 'value_max', 'q01', 'q99', 'bins', 'trace_container',
        'ilines', 'xlines', 'ilines_offset', 'xlines_offset',
        'ilines_len', 'xlines_len', 'ilines_unique', 'xlines_unique',
        'zero_traces', 'min_matrix', 'max_matrix', 'mean_matrix', 'std_matrix', 'hist_matrix'
    ]

    # Headers to load from SEG-Y cube
    HEADERS_PRE_FULL = ['FieldRecord', 'TraceNumber', 'TRACE_SEQUENCE_FILE', 'CDP', 'CDP_TRACE', 'offset', ]
    HEADERS_POST_FULL = ['INLINE_3D', 'CROSSLINE_3D', 'CDP_X', 'CDP_Y']
    HEADERS_POST = ['INLINE_3D', 'CROSSLINE_3D']

    # Headers to use as id of a trace
    INDEX_PRE = ['FieldRecord', 'TraceNumber']
    INDEX_POST = ['INLINE_3D', 'CROSSLINE_3D']
    INDEX_CDP = ['CDP_Y', 'CDP_X']

    def __init__(self, path, process=True, headers=None, index=None, **kwargs):
        self.path = path
        self.name = os.path.basename(self.path)
        self.short_name = self.name.split('.')[0]
        self.long_name = ':'.join(self.path.split('/')[-2:])
        self.format = os.path.splitext(self.path)[1][1:]

        self.depth = None

        self._quality_map = None
        self._quality_grid = None

        if process:
            self.process(headers, index, **kwargs)

    # Methods that wrap around SEGY-Y/HDF5
    def process(self, headers=None, index=None, **kwargs):
        """ Selector to choose whether to process file in `segy` or `hdf5` fashion.
        All the parameters work for `segy` mode.

        Parameters
        ----------
        headers : sequence of str
            Headers to load from `segy` file.
        index : sequence of str
            Subset of headers to use as current index.
        kwargs : dict
            Passed directly to processing functions.
        """
        if self.format in self.SEGY_ALIASES:
            self.structured = False
            self.dataframe = None
            self.segyfile = None

            self.headers = headers or self.HEADERS_POST
            self.index = index or self.INDEX_POST
            self.process_segy(**kwargs)

        elif self.format in self.HDF5_ALIASES:
            self.structured = True
            self.file_hdf5 = None
            self.process_hdf5(**kwargs)

    def load_crop(self, location, axis=None, mode=None, threshold=10):
        """ Selector to choose whether to load crops in `segy` or `hdf5` fashion. """
        if self.structured:
            _ = mode, threshold
            return self.load_hdf5(location, axis=axis)

        _ = axis
        return self.load_segy(location, mode=mode, threshold=threshold)

    def load_slide(self, loc=None, start=None, end=None, step=1, axis=0, stable=False):
        """ Selector to choose whether to load slide in `segy` or `hdf5` fashion.

        If the current index is 1D, then slide is defined by `start`, `end`, `step`.
        If the current index is 2D, then slide is defined by `loc` and `axis`.

        Parameters
        ----------
        loc : int
            Number of slide to load.
        axis : int
            Number of axis to load slide along.
        start, end, step : ints
            Parameters of slice loading for 1D index.
        stable : bool
            Whether or not to use the same sorting order as in the segyfile.
        """
        if self.structured:
            _ = stable, start, end, step
            return self.load_slide_hdf5(loc, axis=axis)

        return self.load_slide_segy(loc=loc, start=start, end=end, step=step, axis=axis, stable=stable)

    def show_slide(self, loc=None, start=None, end=None, step=1, axis=0, stable=False, order_axes=None, **kwargs):
        """ Load slide in `segy` or `hdf5` fashion and display it. """
        slide = self.load_slide(loc=loc, start=start, end=end, step=step, axis=axis, stable=stable)

        title = f'{self.index[axis]} {loc} out of {self.uniques[axis]}'
        meta_title = ''
        plot_images_overlap([slide], title=title, order_axes=order_axes, meta_title=meta_title, **kwargs)

    # SEG-Y methods: infer dataframe, attributes, load data from file
    def process_segy(self, collect_stats=False, **kwargs):
        """ Create dataframe based on `segy` file headers. """
        # Note that all the `segyio` structure inference is disabled
        self.segyfile = segyio.open(self.path, mode='r', strict=False, ignore_geometry=True)
        self.segyfile.mmap()

        self.depth = len(self.segyfile.trace[0])
        self.delay = self.segyfile.header[0].get(segyio.TraceField.DelayRecordingTime)
        self.sample_rate = segyio.dt(self.segyfile) / 1000

        # Load all the headers
        dataframe = {}
        for column in self.headers:
            dataframe[column] = self.segyfile.attributes(getattr(segyio.TraceField, column))[slice(None)]

        dataframe = pd.DataFrame(dataframe)
        dataframe.reset_index(inplace=True)
        dataframe.rename(columns={'index': 'trace_index'}, inplace=True)
        self.dataframe = dataframe.set_index(self.index)

        self.add_attributes_segy()
        if collect_stats:
            self.collect_stats_segy(**kwargs)

    def set_index(self, index, sortby=None):
        """ Change current index to a subset of loaded headers. """
        self.dataframe.reset_index(inplace=True)
        if sortby:
            self.dataframe.sort_values(index, inplace=True, kind='mergesort') # the only stable sorting algorithm
        self.dataframe.set_index(index, inplace=True)
        self.index = index
        self.add_attributes_segy()

    def add_attributes_segy(self):
        """ Infer info about curent index from `dataframe` attribute. """
        self.index_len = len(self.index)
        self._zero_trace = np.zeros(self.depth)
        # Attributes
        self.vals = [np.sort(np.unique(self.dataframe.index.get_level_values(i).values))
                     for i in range(self.index_len)]
        self.vals_inversed = [{v: j for j, v in enumerate(self.vals[i])}
                              for i in range(self.index_len)]
        self.unsorted_vals = [np.unique(self.dataframe.index.get_level_values(i).values)
                              for i in range(self.index_len)]

        self.fields = [getattr(segyio.TraceField, idx) for idx in self.index]
        self.offsets = [np.min(item) for item in self.vals]
        self.uniques = [len(item) for item in self.vals]
        self.lens = [(np.max(item) - np.min(item) + 1) for item in self.vals]

        self.cube_shape = np.asarray([*self.uniques, self.depth])

    def collect_stats_segy(self, spatial=True, bins=25, num_keep=15000, **kwargs):
        """ Pass through file data to collect stats:
            - min/max values.
            - q01/q99 quantiles of amplitudes in the cube.
            - certain amount of traces are stored to `trace_container` attribute.

        If `spatial` is True, makes an additional pass through the cube to obtain following:
            - min/max/mean/std for every trace - `min_matrix`, `max_matrix` and so on.
            - histogram of values for each trace: - `hist_matrix`.
            - bins for histogram creation: - `bins`.

        Parameters
        ----------
        spatial : bool
            Whether to collect additional stats.
        bins : int or str
            Number of bins or name of automatic algorithm of defining number of bins.
        num_keep : int
            Number of traces to store.
        """
        #pylint: disable=not-an-iterable
        _ = kwargs

        num_traces = len(self.segyfile.header)

        # Get min/max values, store some of the traces
        trace_container = []
        value_min, value_max = np.inf, -np.inf

        for i in tqdm(range(num_traces), desc='Finding min/max', ncols=1000):
            trace = self.segyfile.trace[i]

            val_min, val_max = find_min_max(trace)
            if val_min < value_min:
                value_min = val_min
            if val_max > value_max:
                value_max = val_max

            if random() < (num_keep / num_traces) and val_min != val_max:
                trace_container.extend(trace.tolist())
                #TODO: add dtype for storing

        # Collect more spatial stats: min, max, mean, std, histograms matrices
        if spatial:
            # Make bins
            bins = np.histogram_bin_edges(None, bins, range=(value_min, value_max)).astype(np.float)
            self.bins = bins

            # Create containers
            min_matrix, max_matrix = np.full(self.uniques, np.nan), np.full(self.uniques, np.nan)
            hist_matrix = np.full((*self.uniques, len(bins)-1), np.nan)

            # Iterate over traces
            description = f'Collecting stats for {self.name}'
            for i in tqdm(range(num_traces), desc=description, ncols=1000):
                trace = self.segyfile.trace[i]
                header = self.segyfile.header[i]

                #
                keys = [header.get(field) for field in self.fields]
                store_key = [self.vals_inversed[j][item] for j, item in enumerate(keys)]
                # store_key = [np.where(self.vals[i] == item)[0][0] for i, item in enumerate(keys)] # a bit slower
                store_key = tuple(store_key)

                #
                val_min, val_max = find_min_max(trace)
                min_matrix[store_key] = val_min
                max_matrix[store_key] = val_max

                if val_min != val_max:
                    histogram = np.histogram(trace, bins=bins)[0]
                    hist_matrix[store_key] = histogram

            # Restore stats from histogram
            midpoints = (bins[1:] + bins[:-1]) / 2
            probs = hist_matrix / np.sum(hist_matrix, axis=-1, keepdims=True)

            mean_matrix = np.sum(probs * midpoints, axis=-1)
            std_matrix = np.sqrt(np.sum((np.broadcast_to(midpoints, (*mean_matrix.shape, len(midpoints))) - \
                                            mean_matrix.reshape(*mean_matrix.shape, 1))**2 * probs,
                                        axis=-1))

            # Store everything into instance
            self.min_matrix, self.max_matrix = min_matrix, max_matrix
            self.mean_matrix, self.std_matrix = mean_matrix, std_matrix
            self.hist_matrix = hist_matrix
            self.zero_traces = (min_matrix == max_matrix).astype(np.int)
            self.zero_traces[np.isnan(min_matrix)] = 1

        self.value_min, self.value_max = value_min, value_max
        self.trace_container = np.array(trace_container)
        self.q01, self.q99 = np.quantile(trace_container, [0.01, 0.99])

    # Methods to load actual data from SEG-Y
    def load_trace_segy(self, index):
        """ Load individual trace from segyfile.
        If passed `np.nan`, returns trace of zeros.
        """
        if not np.isnan(index):
            return self.segyfile.trace.raw[int(index)]
        return self._zero_trace

    def load_traces_segy(self, trace_indices):
        """ Stack multiple traces together. """
        return np.stack([self.load_trace_segy(idx) for idx in trace_indices])

    @lru_cache(128, attributes='index')
    def load_slide_segy(self, loc=None, axis=0, start=None, end=None, step=1, stable=False):
        """ Create indices and load actual traces for one slide.

        If the current index is 1D, then slide is defined by `start`, `end`, `step`.
        If the current index is 2D, then slide is defined by `loc` and `axis`.

        Parameters
        ----------
        loc : int
            Number of slide to load.
        axis : int
            Number of axis to load slide along.
        start, end, step : ints
            Parameters of slice loading for 1D index.
        stable : bool
            Whether or not to use the same sorting order as in the segyfile.
        """
        indices = self.make_slide_indices(loc=loc, start=start, end=end, step=step, axis=axis, stable=stable)
        slide = self.load_traces_segy(indices)
        return slide


    def make_slide_indices(self, loc=None, axis=0, start=None, end=None, step=1, stable=False, return_iterator=False):
        """ Choose appropriate version of index creation for various lengths of current index.

        Parameters
        ----------
        start, end, step : ints
            Parameters of slice loading for 1d index.
        stable : bool
            Whether or not to use the same sorting order as in the segyfile.
        return_iterator : bool
            Whether to return the same iterator that is used to index current `dataframe`.
            Can be useful for subsequent loads from the same place in various instances.
        """
        if len(self.index) == 1:
            _ = loc, axis
            result = self.make_slide_indices_1d(start=start, end=end, step=step, stable=stable,
                                                return_iterator=return_iterator)
        elif len(self.index) == 2:
            _ = start, end, step
            result = self.make_slide_indices_2d(loc=loc, axis=axis, stable=stable,
                                                return_iterator=return_iterator)
        elif len(self.index) == 3:
            raise NotImplementedError('Yet to be done!')
        else:
            raise ValueError('Index lenght must be less than 4. ')
        return result

    def make_slide_indices_1d(self, start=None, end=None, step=1, stable=False, return_iterator=False):
        """ 1D version of index creation. """
        start = start or self.offsets[0]
        end = end or self.vals[0][-1]

        if stable:
            iterator = self.dataframe.index[(self.dataframe.index >= start) & (self.dataframe.index <= end)]
            iterator = iterator.values[::step]
        else:
            iterator = np.arange(start, end+1, step)

        indices = self.dataframe['trace_index'].get(iterator, np.nan).values

        if return_iterator:
            return indices, iterator
        return indices

    def make_slide_indices_2d(self, loc, axis=0, stable=False, return_iterator=False):
        """ 2D version of index creation. """
        other_axis = 1 - axis
        location = self.vals[axis][loc]

        if stable:
            others = self.dataframe[self.dataframe.index.get_level_values(axis) == location]
            others = others.index.get_level_values(other_axis).values
        else:
            others = self.vals[other_axis]

        iterator = list(zip([location] * len(others), others) if axis == 0 else zip(others, [location] * len(others)))
        indices = self.dataframe['trace_index'].get(iterator, np.nan).values

        #TODO: keep only uniques, when needed, with `nan` filtering
        if stable:
            indices = np.unique(indices)

        if return_iterator:
            return indices, iterator
        return indices


    def load_crop_segy(self, locations):
        """ Load 3D crop from the cube.

        Parameters
        ----------
        locations : sequence of arrays
            List of desired locations to load: along the first index, the second, and depth.

        Example
        -------
        If the current index is `INLINE_3D` and `CROSSLINE_3D`, then to load
        5:110 ilines, 100:1105 crosslines, 0:700 depths, locations must be::
            [
                np.arange(5, 110),
                np.arange(100, 1105),
                np.arange(0, 700)
            ]
        """
        shape = np.array([len(item) for item in locations])
        indices = self.make_crop_indices(locations)
        crop = self.load_traces_segy(indices)[..., locations[-1]].reshape(shape)
        return crop

    def make_crop_indices(self, locations):
        """ Create indices for 3D crop loading. """
        iterator = list(product(*[[self.vals[idx][i] for i in locations[idx]] for idx in range(2)]))
        indices = self.dataframe['trace_index'].get(list(iterator), np.nan).values
        return np.unique(indices)

    def load_segy(self, locations, threshold=10, mode=None):
        """ Smart choice between using :meth:`.load_crop_segy` and
        stacking multiple slides created by :meth:`.load_slide_segy`.
        """
        shape = np.array([len(item) for item in locations])
        mode = mode or ('slide' if min(shape) < threshold else 'crop')

        if mode == 'slide':
            axis = np.argmin(shape)
            #TODO: add depth-slicing; move this logic to separate function
            if axis in [0, 1]:
                return np.stack([self.load_slide_segy(loc, axis=axis)[..., locations[-1]]
                                 for loc in locations[axis]],
                                axis=axis)
        return self.load_crop_segy(locations)

    # HDF5 methods: convert from SEG-Y, process cube and attributes, load data
    def make_hdf5(self, path_hdf5=None, postfix='', dtype=np.float32):
        """ Converts `.segy` cube to `.hdf5` format.

        Parameters
        ----------
        path_hdf5 : str
            Path to store converted cube. By default, new cube is stored right next to original.
        postfix : str
            Postfix to add to the name of resulting cube.
        dtype : str
            data-type to use for storing the cube. Has to be supported by numpy.
        """
        if self.index != self.INDEX_POST:
            raise TypeError(f'Current index must be {self.INDEX_POST}')
        if self.format not in self.SEGY_ALIASES:
            raise TypeError(f'Format should be in {self.SEGY_ALIASES}')

        path_hdf5 = path_hdf5 or (os.path.splitext(self.path)[0] + postfix + '.hdf5')

        # Remove file, if exists: h5py can't do that
        if os.path.exists(path_hdf5):
            os.remove(path_hdf5)

        # Create file and datasets inside
        # ctx
        with h5py.File(path_hdf5, "a") as file_hdf5:
            cube_hdf5 = file_hdf5.create_dataset('cube', self.cube_shape, dtype=dtype)
            cube_hdf5_x = file_hdf5.create_dataset('cube_x', self.cube_shape[[1, 2, 0]], dtype=dtype)
            cube_hdf5_h = file_hdf5.create_dataset('cube_h', self.cube_shape[[2, 0, 1]], dtype=dtype)

            # Default projection: (ilines, xlines, depth)
            # Depth-projection: (depth, ilines, xlines)
            pbar = tqdm(total=self.ilines_unique + self.xlines_unique, ncols=1000)

            pbar.set_description(f'Converting {self.long_name}; ilines projection')
            for i in range(self.ilines_unique):
                #
                slide = self.load_slide_segy(i).astype(dtype)
                cube_hdf5[i, :, :] = slide.reshape(1, self.xlines_unique, self.depth)
                cube_hdf5_h[:, i, :] = slide.T
                pbar.update()

            # xline-oriented projection: (xlines, depth, ilines)
            pbar.set_description(f'Converting {self.long_name} to hdf5; xlines projection')
            for x in range(self.xlines_unique):
                slide = self.load_slide_segy(x, axis=1).T.astype(dtype)
                cube_hdf5_x[x, :, :,] = slide
                pbar.update()
            pbar.close()

            # Save all the necessary attributes to the `info` group
            for attr in self.PRESERVED:
                if hasattr(self, attr):
                    file_hdf5['/info/' + attr] = getattr(self, attr)

        self.file_hdf5 = h5py.File(path_hdf5, "r")
        self.add_attributes_hdf5()
        self.structured = True


    def process_hdf5(self, **kwargs):
        """ Put info from `.hdf5` groups to attributes.
        No passing through data whatsoever.
        """
        _ = kwargs
        self.file_hdf5 = h5pickle.File(self.path, "r")
        self.add_attributes_hdf5()

    def add_attributes_hdf5(self):
        """ Store values from `hdf5` file to attributes. """
        self.index = self.INDEX_POST

        for item in self.PRESERVED:
            try:
                value = self.file_hdf5['/info/' + item][()]
                setattr(self, item, value)
            except KeyError:
                pass
        # BC
        self.ilines_offset = min(self.ilines)
        self.xlines_offset = min(self.xlines)
        self.ilines_len = len(self.ilines)
        self.xlines_len = len(self.xlines)
        self.cube_shape = np.asarray([self.ilines_len, self.xlines_len, self.depth])

    # Methods to load actual data from HDF5
    def load_hdf5(self, locations, axis=None):
        """ Load 3D crop from the cube.
        Automatically chooses the fastest axis to use: as `hdf5` files store multiple copies of data with
        various orientations, some axis are faster than others depending on exact crop location and size.

        Parameters
        locations : sequence of arrays
            List of desired locations to load: along the first index, the second, and depth.
        axis : str or int
            Identificator of the axis to use to load data.
            Can be `i`, `x`, `h`, 0, 1, 2.
        """
        if axis is None:
            shape = np.array([len(item) for item in locations])
            axis = np.argmin(shape)
        else:
            mapping = {0: 0, 1: 1, 2: 2,
                       'i': 0, 'x': 1, 'h': 2,
                       'iline': 0, 'xline': 1, 'height': 2, 'depth': 2}
            axis = mapping[axis]

        if axis == 1 and 'cube_x' in self.file_hdf5:
            crop = self._load_hdf5_x(*locations)
        elif axis == 2 and 'cube_h' in self.file_hdf5:
            crop = self._load_hdf5_h(*locations)
        else: # backward compatibility
            crop = self._load_hdf5_i(*locations)
        return crop

    def _load_hdf5_i(self, ilines, xlines, heights):
        cube_hdf5 = self.file_hdf5['cube']
        dtype = cube_hdf5.dtype
        return np.stack([self._load_slide_hdf5(cube_hdf5, iline)[xlines, :][:, heights]
                         for iline in ilines]).astype(dtype)

    def _load_hdf5_x(self, ilines, xlines, heights):
        cube_hdf5 = self.file_hdf5['cube_x']
        dtype = cube_hdf5.dtype
        return np.stack([self._load_slide_hdf5(cube_hdf5, xline)[heights, :][:, ilines].transpose([1, 0])
                         for xline in xlines], axis=1).astype(dtype)

    def _load_hdf5_h(self, ilines, xlines, heights):
        cube_hdf5 = self.file_hdf5['cube_h']
        dtype = cube_hdf5.dtype
        return np.stack([self._load_slide_hdf5(cube_hdf5, height)[ilines, :][:, xlines]
                         for height in heights], axis=2).astype(dtype)

    @lru_cache(128)
    def _load_slide_hdf5(self, cube, loc):
        """ Load one slide of data from a certain cube projection.
        Caches the result in a thread-safe manner.
        """
        return cube[loc, :, :]

    def load_slide_hdf5(self, loc, axis='iline'):
        """ Load desired slide along desired axis. """
        location = self.make_slide_locations(loc=loc, axis=axis)
        return np.squeeze(self.load_hdf5(location))


    # Common methods/properties for SEG-Y/HDF5
    def scaler(self, array, mode='minmax'):
        """ Normalize array of amplitudes cut from the cube.

        Parameters
        ----------
        array : ndarray
            Crop of amplitudes.
        mode : str
            If `minmax`, then data is scaled to [0, 1] via minmax scaling.
            If `q`, then data is scaled to [-1, 1] with quantiles.
        """
        if mode == 'minmax':
            scale = (self.value_max - self.value_min)
            return (array - self.value_min) / scale
        if mode in ['q', 'normalize']:
            return np.clip(array, self.q01, self.q99) / max(abs(self.q01), abs(self.q99))
        raise ValueError('Wrong mode', mode)


    def make_slide_locations(self, loc, axis=0, return_axis=False):
        """ Create locations (sequence of locations for each axis) for desired slide along desired axis. """
        locations = [np.arange(item) for item in self.uniques]
        locations += [np.arange(self.depth)]

        if isinstance(axis, str):
            if axis in self.index:
                axis = self.index.index(axis)
            elif axis in ['i', 'il', 'iline']:
                axis = 0
            elif axis in ['x', 'xl', 'xline']:
                axis = 1
            elif axis in ['h', 'height', 'depth']:
                axis = 2
        locations[axis] = [loc]

        if return_axis:
            return locations, axis
        return locations


    @property
    def nbytes(self):
        """ Size of instance in bytes. """
        attrs = [
            'dataframe',
            'trace_container', 'min_matrix', 'max_matrix',
            'mean_matrix', 'std_matrix', 'zero_traces', 'hist_matrix'
        ]
        return sum(sys.getsizeof(getattr(self, attr)) for attr in attrs if hasattr(self, attr))

    @property
    def nmbytes(self):
        """ Size of instance in megabytes. """
        return self.nbytes / (1024**2)

    @property
    def ngbytes(self):
        """ Size of instance in gigabytes. """
        return self.nbytes / (1024**3)


    @property
    def quality_map(self):
        """ Spatial matrix to show harder places in the cube. """
        if self._quality_map is None:
            self.make_quality_map([0.1], ['support_js', 'support_hellinger'])
        return self._quality_map

    @property
    def quality_grid(self):
        """ Spatial grid based on `quality_map`. """
        if self._quality_grid is None:
            self.make_quality_grid((20, 150))
        return self._quality_grid

    def make_quality_map(self, quantiles, metric_names, **kwargs):
        """ Create `quality_map` matrix that shows harder places in the cube.

        Parameters
        ----------
        quantiles : sequence of floats
            Quantiles for computing hardness thresholds. Must be in (0, 1) ranges.
        metric_names : sequence or str
            Metrics to compute to assess hardness of cube.
        """
        from .metrics import GeometryMetrics #pylint: disable=import-outside-toplevel
        quality_map = GeometryMetrics(self).evaluate('quality_map', quantiles=quantiles, agg=None,
                                                     metric_names=metric_names, **kwargs)
        self._quality_map = quality_map
        return quality_map

    def make_quality_grid(self, frequencies, iline=True, xline=True, margin=0, **kwargs):
        """ Create `quality_grid` based on `quality_map`.

        Parameters
        ----------
        frequencies : sequence of numbers
            Grid frequencies for individual levels of hardness in `quality_map`.
        margin : int
            Margin of boundaries to not include in the grid.
        iline, xline : bool
            Whether to make lines in grid to account for `ilines`/`xlines`.
        """
        from .metrics import GeometryMetrics #pylint: disable=import-outside-toplevel
        quality_grid = GeometryMetrics(self).make_grid(self.quality_map, frequencies,
                                                       iline=iline, xline=xline, margin=margin, **kwargs)
        self._quality_grid = quality_grid
        return quality_grid


    @lru_cache(100)
    def get_quantile_matrix(self, q):
        """ Restore the quantile matrix for desired `q` from `hist_matrix`.

        Parameters
        ----------
        q : number
            Quantile to compute. Must be in (0, 1) range.
        """
        #pylint: disable=line-too-long
        threshold = self.depth * q

        cumsums = np.cumsum(self.hist_matrix, axis=-1)

        positions = np.argmax(cumsums >= threshold, axis=-1)
        idx_1, idx_2 = np.nonzero(positions)
        indices = positions[idx_1, idx_2]

        broadcasted_bins = np.broadcast_to(self.bins, (*positions.shape, len(self.bins)))

        q_matrix = np.zeros_like(positions, dtype=np.float)
        q_matrix[idx_1, idx_2] += broadcasted_bins[idx_1, idx_2, indices]
        q_matrix[idx_1, idx_2] += (broadcasted_bins[idx_1, idx_2, indices+1] - broadcasted_bins[idx_1, idx_2, indices]) * \
                                   (threshold - cumsums[idx_1, idx_2, indices-1]) / self.hist_matrix[idx_1, idx_2, indices]
        q_matrix[q_matrix == 0.0] = np.nan
        setattr(self, f'q{int(q*100)}_matrix', q_matrix)
        return q_matrix

    # Visualization methods
    def __repr__(self):
        return 'Inferred geometry for {}: ({}x{}x{})'.format(os.path.basename(self.path), *self.cube_shape)

    def __str__(self):
        msg = f"""
        Geometry for cube {self.path}
        Time delay and sample rate: {self.delay}, {self.sample_rate}
        Depth of one trace is: {self.depth}
        Current index: {self.index}
        Shape: {self.cube_shape}
        """

        if hasattr(self, 'value_min'):
            msg += f"""
            Min/max values: {self.value_min, self.value_max}
            q01/q99 values: {self.q01, self.q99}
            """
        return dedent(msg)

    def log(self, printer=None):
        """ Log some info into desired stream. """
        if not callable(printer):
            path_log = '/'.join(self.path.split('/')[:-1]) + '/CUBE_INFO.log'
            handler = logging.FileHandler(path_log, mode='w')
            handler.setFormatter(logging.Formatter('%(message)s'))

            logger = logging.getLogger('geometry_logger')
            logger.setLevel(logging.INFO)
            logger.addHandler(handler)
            printer = logger.info
        printer(str(self))
