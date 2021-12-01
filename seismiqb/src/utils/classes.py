""" Helper classes. """
import os
from ast import literal_eval
from time import perf_counter
from collections import OrderedDict
from functools import wraps

import numpy as np
try:
    import cupy as cp
    CUPY_AVAILABLE = True
except ImportError:
    cp = np
    CUPY_AVAILABLE = False
import bottleneck as bn

import h5py

from .functions import to_list



class AugmentedNumpy:
    """ NumPy with better routines for nan-handling. """
    def __getattr__(self, key):
        return getattr(bn, key, getattr(np, key))
augmented_np = AugmentedNumpy()



class Accumulator:
    """ Class to accumulate statistics over streamed matrices.
    An example of usage:
        one can either store matrices and take a mean along desired axis at the end of their generation,
        or sequentially update the `mean` matrix with the new data by using this class.
    Note the latter approach is inherintly slower, but requires O(N) times less memory,
    where N is the number of accumulated matrices.

    This class is intended to be used in the following manner:
        - initialize the instance with desired aggregation
        - iteratively call `update` method with new matrices
        - to get the aggregated result, use `get` method

    NaNs are ignored in all computations.
    This class works with both CPU (`numpy`) and GPU (`cupy`) arrays and automatically detects current device.

    Parameters
    ----------
    agg : str
        Which type of aggregation to use. Currently, following modes are implemented:
            - 'mean' works by storing matrix of sums and non-nan counts.
            To get the mean result, the sum is divided by the counts
            - 'std' works by keeping track of sum of the matrices, sum of squared matrices,
            and non-nan counts. To get the result, we subtract squared mean from mean of squared values
            - 'min', 'max' works by iteratively updating the matrix of minima/maxima values
            - 'argmin', 'argmax' iteratively updates index of the minima/maxima values in the passed matrices
            - 'stack' just stores the matrices and concatenates them along (new) last axis
            - 'mode' stores supplied matrices and computes mode along the last axis during the `get` call
    amortize : bool
        If False, then supplied matrices are stacked into ndarray, and then aggregation is applied.
        If True, then accumulation logic is applied.
        Allows for trade-off between memory usage and speed: `amortize=False` is faster,
        but takes more memory resources.
    total : int or None
        If integer, then total number of matrices to be aggregated.
        Used to reduce the memory footprint if `amortize` is set to False.
    axis : int
        Axis to stack matrices on and to apply aggregation funcitons.
    """
    #pylint: disable=attribute-defined-outside-init
    def __init__(self, agg='mean', amortize=False, total=None, axis=0):
        self.agg = agg
        self.amortize = amortize
        self.total = total
        self.axis = axis

        self.initialized = False


    def init(self, matrix):
        """ Initialize all the containers on first `update`. """
        # No amortization: collect all the matrices and apply reduce afterwards
        self.module = cp.get_array_module(matrix) if CUPY_AVAILABLE else augmented_np
        self.n = 1

        if self.amortize is False or self.agg in ['stack', 'mode']:
            if self.total:
                self.values = self.module.empty((self.total, *matrix.shape))
                self.values[0, ...] = matrix
            else:
                self.values = [matrix]

            self.initialized = True
            return

        # Amortization: init all the containers
        if self.agg in ['mean', 'nanmean']:
            # Sum of values and counts of non-nan
            self.value = matrix
            self.counts = (~self.module.isnan(matrix)).astype(self.module.int32)

        elif self.agg in ['min', 'nanmin', 'max', 'nanmax']:
            self.value = matrix

        elif self.agg in ['std', 'nanstd']:
            # Same as means, but need to keep track of mean of squares and squared mean
            self.means = matrix
            self.squared_means = matrix ** 2
            self.counts = (~self.module.isnan(matrix)).astype(self.module.int32)

        elif self.agg in ['argmin', 'argmax', 'nanargmin', 'nanargmax']:
            # Keep the current maximum/minimum and update indices matrix, if needed
            self.value = matrix
            self.indices = self.module.zeros_like(matrix)

        self.initialized = True
        return


    def update(self, matrix):
        """ Update containers with new matrix. """
        if not self.initialized:
            self.init(matrix.copy())
            return

        # No amortization: just store everything
        if self.amortize is False or self.agg in ['stack', 'mode']:
            if self.total:
                self.values[self.n, ...] = matrix
            else:
                self.values.append(matrix)

            self.n += 1
            return

        # Amortization: update underlying containers
        slc = ~self.module.isnan(matrix)

        if self.agg in ['min', 'nanmin']:
            self.value[slc] = self.module.fmin(self.value[slc], matrix[slc])

        elif self.agg in ['max', 'nanmax']:
            self.value[slc] = self.module.fmax(self.value[slc], matrix[slc])

        elif self.agg in ['mean', 'nanmean']:
            mask = np.logical_and(slc, self.module.isnan(self.value))
            self.value[mask] = 0.0
            self.value[slc] += matrix[slc]
            self.counts[slc] += 1

        elif self.agg in ['std', 'nanstd']:
            mask = np.logical_and(slc, self.module.isnan(self.means))
            self.means[mask] = 0.0
            self.squared_means[mask] = 0.0
            self.means[slc] += matrix[slc]
            self.squared_means[slc] += matrix[slc] ** 2
            self.counts[slc] += 1

        elif self.agg in ['argmin', 'nanargmin']:
            mask = self.module.logical_and(slc, self.module.isnan(self.value))
            self.value[mask] = matrix[mask]
            self.indices[mask] = self.n

            slc_ = matrix < self.value
            self.value[slc_] = matrix[slc_]
            self.indices[slc_] = self.n

        elif self.agg in ['argmax', 'nanargmax']:
            mask = self.module.logical_and(slc, self.module.isnan(self.value))
            self.value[mask] = matrix[mask]
            self.indices[mask] = self.n

            slc_ = matrix > self.value
            self.value[slc_] = matrix[slc_]
            self.indices[slc_] = self.n

        self.n += 1
        return

    def get(self, final=False):
        """ Use stored matrices to get the aggregated result. """
        # No amortization: apply function along the axis to the stacked array
        if self.amortize is False or self.agg in ['stack', 'mode']:
            if self.total:
                stacked = self.values
            else:
                stacked = self.module.stack(self.values, axis=self.axis)

            if final:
                self.values = None

            if self.agg in ['stack']:
                value = stacked

            elif self.agg in ['mode']:
                uniques = self.module.unique(stacked)

                accumulator = Accumulator('argmax')
                for item in uniques[~self.module.isnan(uniques)]:
                    counts = (stacked == item).sum(axis=self.axis)
                    accumulator.update(counts)
                indices = accumulator.get(final=True)
                value = uniques[indices]
                value[self.module.isnan(self.module.max(stacked, axis=self.axis))] = self.module.nan

            else:
                value = getattr(self.module, self.agg)(stacked, axis=self.axis)

            return value

        # Amortization: compute desired aggregation
        if self.agg in ['min', 'nanmin', 'max', 'nanmax']:
            value = self.value

        elif self.agg in ['mean', 'nanmean']:
            slc = self.counts > 0
            value = self.value if final else self.value.copy()
            value[slc] /= self.counts[slc]

        elif self.agg in ['std', 'nanstd']:
            slc = self.counts > 0
            means = self.means if final else self.means.copy()
            means[slc] /= self.counts[slc]

            squared_means = self.squared_means if final else self.squared_means.copy()
            squared_means[slc] /= self.counts[slc]
            value = self.module.sqrt(squared_means - means ** 2)

        elif self.agg in ['argmin', 'argmax', 'nanargmin', 'nanargmax']:
            value = self.indices

        return value



class Accumulator3D:
    """ Base class to aggregate predicted sub-volumes into a larger 3D cube.
    Can accumulate data in memory (Numpy arrays) or on disk (HDF5 datasets).

    Type of aggregation is defined in subclasses, that must implement `__init__`, `_update` and `_aggregate` methods.
    The main result in subclasses should be stored in `data` attribute, which is accessed by the base class.

    Supposed to be used in combination with `:class:.~RegularGrid` and
    `:meth:.~SeismicCropBatch.update_accumulator` in a following manner:
        - `RegularGrid` defines how to split desired cube range into small crops
        - `Accumulator3D` creates necessary placeholders for a desired type of aggregation
        - `update_accumulator` action of pipeline passes individual crops (and their locations) to
        update those placeholders (see `:meth:~.update`)
        - `:meth:~.aggregate` is used to get the resulting volume
        - `:meth:~.clear` can be optionally used to remove array references and HDF5 file from disk

    This class is an alternative to `:meth:.~SeismicDataset.assemble_crops`, but allows to
    greatly reduce memory footprint of crop aggregation by up to `overlap_factor` times.
    Also, as this class updates rely on `location`s of crops, it can take crops in any order.

    Note that not all pixels of placeholders will be updated with data due to removal of dead traces,
    so we have to be careful with initialization!

    Parameters
    ----------
    shape : sequence
        Shape of the placeholder.
    origin : sequence
        The upper left point of the volume: used to shift crop's locations.
    dtype : np.dtype
        Dtype of storage. Must be either integer or float.
    transform : callable, optional
        Additional function to call before storing the crop data.
    path : str or file-like object, optional
        If provided, then we use HDF5 datasets instead of regular Numpy arrays, storing the data directly on disk.
        After the initialization, we keep the file handle in `w-` mode during the update phase.
        After aggregation, we re-open the file to automatically repack it in `r` mode.
    kwargs : dict
        Other parameters are passed to HDF5 dataset creation.
    """
    def __init__(self, shape=None, origin=None, dtype=np.float32, transform=None, path=None, **kwargs):
        # Dimensionality and location
        self.shape = shape
        self.origin = origin
        self.location = [slice(start, start + shape) for start, shape in zip(self.origin, self.shape)]

        # Properties of storages
        self.dtype = dtype
        self.transform = transform if transform is not None else lambda array: array

        # Container definition
        if path is not None:
            if isinstance(path, str) and os.path.exists(path):
                os.remove(path)
            self.path = path

            self.file = h5py.File(path, mode='w-')
        self.type = 'hdf5' if path is not None else 'numpy'

        self.aggregated = False
        self.kwargs = kwargs

    # Placeholder management
    def create_placeholder(self, name=None, dtype=None, fill_value=None):
        """ Create named storage as a dataset of HDF5 or plain array. """
        if self.type == 'hdf5':
            placeholder = self.file.create_dataset(name, shape=self.shape, dtype=dtype, fillvalue=fill_value)
        elif self.type == 'numpy':
            placeholder = np.full(shape=self.shape, fill_value=fill_value, dtype=dtype)

        setattr(self, name, placeholder)

    def remove_placeholder(self, name=None):
        """ Remove created placeholder. """
        if self.type in ['hdf5', 'blosc']:
            del self.file[name]
        setattr(self, name, None)


    def update(self, crop, location):
        """ Update underlying storages in supplied `location` with data from `crop`. """
        if self.aggregated:
            raise RuntimeError('Aggregated data has been already computed!')

        # Check all shapes for compatibility
        for s, slc in zip(crop.shape, location):
            if slc.step and slc.step != 1:
                raise ValueError(f"Invalid step in location {location}")

            if s < slc.stop - slc.start:
                raise ValueError(f"Inconsistent crop_shape {crop.shape} and location {location}")

        # Compute correct shapes
        loc, loc_crop = [], []
        for xmin, slc, xmax in zip(self.origin, location, self.shape):
            loc.append(slice(max(0, slc.start - xmin), min(xmax, slc.stop - xmin)))
            loc_crop.append(slice(max(0, xmin - slc.start), min(xmax + xmin - slc.start , slc.stop - slc.start)))

        # Actual update
        crop = self.transform(crop[tuple(loc_crop)])
        location = tuple(loc)
        self._update(crop, location)

    def _update(self, crop, location):
        """ Update placeholders with data from `crop` at `locations`. """
        _ = crop, location
        raise NotImplementedError

    def aggregate(self):
        """ Finalize underlying storages to create required aggregation. """
        if self.aggregated:
            raise RuntimeError('All data in the container has already been cleared!')
        self._aggregate()

        # Re-open the HDF5 file to force flush changes and release disk space from deleted datasets
        # Also add alias to `data` dataset, so the resulting cube can be opened by `SeismicGeometry`
        if self.type == 'hdf5':
            self.file['cube_i'] = self.file['data']
            self.file.close()
            self.file = h5py.File(self.path, 'r')

            self.data = self.file['data']

        self.aggregated = True
        return self.data

    def _aggregate(self):
        """ Aggregate placeholders into resulting array. Changes `data` placeholder inplace. """
        raise NotImplementedError

    def __del__(self):
        if self.type in ['hdf5', 'blosc']:
            self.file.close()

    def clear(self):
        """ Remove placeholders from memory and disk. """
        if self.type in ['hdf5', 'blosc']:
            os.remove(self.path)

    @property
    def result(self):
        """ Reference to the aggregated result. """
        if not self.aggregated:
            self.aggregate()
        return self.data

    @classmethod
    def from_aggregation(cls, aggregation='max', shape=None, origin=None, dtype=np.float32, fill_value=None,
                         transform=None, path=None, **kwargs):
        """ Initialize chosen type of accumulator aggregation. """
        class_to_aggregation = {
            MaxAccumulator3D: ['max', 'maximum'],
            MeanAccumulator3D: ['mean', 'avg', 'average'],
            GMeanAccumulator3D: ['gmean', 'geometric'],
            ModeAccumulator3D: ['mode']
        }
        aggregation_to_class = {alias: class_ for class_, lst in class_to_aggregation.items()
                                for alias in lst}

        return aggregation_to_class[aggregation](shape=shape, origin=origin, dtype=dtype, fill_value=fill_value,
                                                 transform=transform, path=path, **kwargs)

    @classmethod
    def from_grid(cls, grid, aggregation='max', dtype=np.float32, fill_value=None, transform=None, path=None, **kwargs):
        """ Infer necessary parameters for accumulator creation from a passed grid. """
        return cls.from_aggregation(aggregation=aggregation, dtype=dtype, fill_value=fill_value,
                                    shape=grid.shape, origin=grid.origin, orientation=grid.orientation,
                                    transform=transform, path=path, **kwargs)


class MaxAccumulator3D(Accumulator3D):
    """ Accumulator that takes maximum value of overlapping crops. """
    def __init__(self, shape=None, origin=None, dtype=np.float32, fill_value=None, transform=None, path=None, **kwargs):
        super().__init__(shape=shape, origin=origin, dtype=dtype, transform=transform, path=path, **kwargs)

        min_value = np.finfo(dtype).min if 'float' in dtype.__name__ else np.iinfo(dtype).min
        self.fill_value = fill_value if fill_value is not None else min_value
        self.create_placeholder(name='data', dtype=self.dtype, fill_value=self.fill_value)

    def _update(self, crop, location):
        self.data[location] = np.maximum(crop, self.data[location])

    def _aggregate(self):
        pass


class MeanAccumulator3D(Accumulator3D):
    """ Accumulator that takes mean value of overlapping crops. """
    def __init__(self, shape=None, origin=None, dtype=np.float32, transform=None, path=None, **kwargs):
        super().__init__(shape=shape, origin=origin, dtype=dtype, transform=transform, path=path, **kwargs)

        self.create_placeholder(name='data', dtype=self.dtype, fill_value=0)
        self.create_placeholder(name='counts', dtype=np.int8, fill_value=0)

    def _update(self, crop, location):
        self.data[location] += crop
        self.counts[location] += 1

    def _aggregate(self):
        #pylint: disable=access-member-before-definition
        if self.type == 'hdf5':
            # Amortized updates for HDF5
            for i in range(self.data.shape[0]):
                counts = self.counts[i]
                counts[counts == 0] = 1
                if np.issubdtype(self.dtype, np.floating):
                    self.data[i] /= counts
                else:
                    self.data[i] //= counts

        elif self.type == 'numpy':
            self.counts[self.counts == 0] = 1
            if np.issubdtype(self.dtype, np.floating):
                self.data /= self.counts
            else:
                self.data //= self.counts

        # Cleanup
        self.remove_placeholder('counts')


class GMeanAccumulator3D(Accumulator3D):
    """ Accumulator that takes geometric mean value of overlapping crops. """
    def __init__(self, shape=None, origin=None, dtype=np.float32, transform=None, path=None, **kwargs):
        super().__init__(shape=shape, origin=origin, dtype=dtype, transform=transform, path=path, **kwargs)

        self.create_placeholder(name='data', dtype=self.dtype, fill_value=1)
        self.create_placeholder(name='counts', dtype=np.int8, fill_value=0)

    def _update(self, crop, location):
        self.data[location] *= crop
        self.counts[location] += 1

    def _aggregate(self):
        #pylint: disable=access-member-before-definition
        if self.type == 'hdf5':
            # Amortized updates for HDF5
            for i in range(self.data.shape[0]):
                counts = self.counts[i]
                counts[counts == 0] = 1

                counts = counts.astype(np.float32)
                counts **= -1
                self.data[i] **= counts

        elif self.type == 'numpy':
            self.counts[self.counts == 0] = 1

            self.counts = self.counts.astype(np.float32)
            self.counts **= -1
            self.data **= self.counts

        # Cleanup
        self.remove_placeholder('counts')


class ModeAccumulator3D(Accumulator3D):
    """ Accumulator that takes mode value in overlapping crops. """
    def __init__(self, shape=None, origin=None, dtype=np.float32,
                 n_classes=2, transform=None, path=None, **kwargs):
        # Create placeholder with counters for each class
        self.fill_value = 0
        self.n_classes = n_classes

        shape = (*shape, n_classes)
        origin = (*origin, 0)

        super().__init__(shape=shape, origin=origin, dtype=dtype, transform=transform, path=path, **kwargs)

        self.create_placeholder(name='data', dtype=self.dtype, fill_value=self.fill_value)

    def _update(self, crop, location):
        # Update class counters in location
        crop = np.eye(self.n_classes)[crop]
        self.data[location] += crop

    def _aggregate(self):
        # Choose the most frequently seen class value
        if self.type == 'hdf5':
            for i in range(self.data.shape[0]):
                self.data[i] = np.argmax(self.data[i], axis=-1)

        elif self.type == 'numpy':
            self.data = np.argmax(self.data, axis=-1)


class AccumulatorBlosc(Accumulator3D):
    """ Accumulate predictions into `BLOSC` file.
    Each of the saved slides supposed to be finalized, e.g. coming from another accumulator.
    During the aggregation, we repack the file to remove duplicates.

    Parameters
    ----------
    path : str
        Path to save `BLOSC` file to.
    orientation : int
        If 0, then predictions are stored as `cube_i` dataset inside the file.
        If 1, then predictions are stored as `cube_x` dataset inside the file and transposed before storing.
    aggregation : str
        Type of aggregation for duplicate slides.
        If `max`, then we take element-wise maximum.
        If `mean`, then take mean value.
        If None, then we take random slide.
    """
    def __init__(self, path, orientation=0, aggregation='max',
                 shape=None, origin=None, dtype=np.float32, transform=None, **kwargs):
        super().__init__(shape=shape, origin=origin, dtype=dtype, transform=transform, path=None)
        if orientation == 2:
            raise ValueError("Can't use BLOSC accumulator for a joined grid with mixed orientations!")

        self.type = 'blosc'
        self.path = path
        self.orientation = orientation
        self.aggregation = aggregation

        # Manage the `BLOSC` file
        from .geometry import BloscFile #pylint: disable=import-outside-toplevel
        self.file = BloscFile(path, mode='w')
        if orientation == 0:
            name = 'cube_i'
        elif orientation == 1:
            name = 'cube_x'
            shape = np.array(shape)[[1, 0, 2]]
        self.file.create_dataset(name, shape=shape, dtype=dtype)


    def _update(self, crop, location):
        crop = crop.astype(self.dtype)
        iterator = range(location[self.orientation].start, location[self.orientation].stop)

        # `i` is `loc_idx` shifted by `origin`
        for i, loc_idx in enumerate(iterator):
            slc = [slice(None), slice(None), slice(None)]
            slc[self.orientation] = i
            slide = crop[tuple(slc)]
            self.data[loc_idx, :, :] = slide.T if self.orientation == 1 else slide

    def _aggregate(self):
        self.file = self.file.repack(aggregation=self.aggregation)


    @classmethod
    def from_grid(cls, grid, aggregation='max', dtype=np.float32, transform=None, path=None, **kwargs):
        """ Infer necessary parameters for accumulator creation from a passed grid. """
        return cls(path=path, aggregation=aggregation, dtype=dtype, transform=transform,
                   shape=grid.shape, origin=grid.origin, orientation=grid.orientation, **kwargs)


class LoopedList(list):
    """ List that loops from given position (default is 0).

        Examples
        --------
        >>> l = LoopedList(['a', 'b', 'c'])
        >>> [l[i] for i in range(9)]
        ['a', 'b', 'c', 'a', 'b', 'c', 'a', 'b', 'c']

        >>> l = LoopedList(['a', 'b', 'c', 'd'], loop_from=2)
        >>> [l[i] for i in range(9)]
        ['a', 'b', 'c', 'd', 'c', 'd', 'c', 'd', 'c']

        >>> l = LoopedList(['a', 'b', 'c', 'd', 'e'], loop_from=-1)
        >>> [l[i] for i in range(9)]
        ['a', 'b', 'c', 'd', 'e', 'e', 'e', 'e', 'e']
    """
    def __init__(self, *args, loop_from=0, **kwargs):
        self.loop_from = loop_from
        super().__init__(*args, **kwargs)

    def __getitem__(self, idx):
        if idx >= len(self):
            pos = self.loop_from + len(self) * (self.loop_from < 0)
            if pos < 0:
                raise IndexError(f"List of length {len(self)} is looped from {self.loop_from} index")
            idx = pos + (idx - pos) % (len(self) - pos)
        return super().__getitem__(idx)


class AugmentedList(list):
    """ List with additional features:
        - can be indexed with other iterables.
        - delegates calls to contained objects.
        For example, `a_list.method()` is equivalent to `[item.method() for item in a_list]`.
        Can be used to retrieve attributes, properties and call methods.
        Returns the list of results, which is itself an instance of `AugmentedList`.
        - auto-completes names to that of contained objects.
    """
    # Advanced indexing
    def __getitem__(self, key):
        if isinstance(key, (int, np.integer)):
            return super().__getitem__(key)
        if isinstance(key, slice):
            return AugmentedList(super().__getitem__(key))

        return AugmentedList([super().__getitem__(idx) for idx in key])

    # Delegating to contained objects
    def __getattr__(self, key):
        if len(self) == 0:
            return lambda *args, **kwargs: self

        attribute = getattr(self[0], key)

        if not callable(attribute):
            # Attribute or property
            return AugmentedList([getattr(item, key) for item in self])

        @wraps(attribute)
        def method_wrapper(*args, **kwargs):
            return AugmentedList([getattr(item, key)(*args, **kwargs) for item in self])
        return method_wrapper

    def __dir__(self):
        """ Correct autocompletion for delegated methods. """
        if len(self) != 0:
            return dir(self[0])
        return dir(list)

    # Correct type of operations
    def __add__(self, other):
        return type(self)(list.__add__(self, other))

    def __radd__(self, other):
        return self.__add__(other)

    def __mul__(self, other):
        return type(self)(list.__mul__(self, other))

    def __rmul__(self, other):
        return self.__mul__(other)


class AugmentedDict(OrderedDict):
    """ Ordered dictionary with additional features:
        - can be indexed with ordinals.
        - delegates calls to contained objects.
        For example, `a_dict.method()` is equivalent to `{key : value.method() for key, value in a_dict.items()}`.
        Can be used to retrieve attributes, properties and call methods.
        Returns the dictionary with results, which is itself an instance of `AugmentedDict`.
        - auto-completes names to that of contained objects.
        - can be flattened.
    """
    # Ordinal indexation
    def __getitem__(self, key):
        if isinstance(key, (int, np.integer)):
            key = list(self.keys())[key]
        return super().__getitem__(key)

    def __setitem__(self, key, value):
        if isinstance(key, (int, np.integer)):
            key = list(self.keys())[key]

        if isinstance(value, list):
            value = AugmentedList(value)
        super().__setitem__(key, value)

    # Delegating to contained objects
    def __getattr__(self, key):
        if len(self) == 0:
            return lambda *args, **kwargs: self

        attribute = getattr(self[0], key)

        if not callable(attribute):
            # Attribute or property
            return AugmentedDict({key_ : getattr(value, key) for key_, value in self.items()})

        @wraps(attribute)
        def method_wrapper(*args, **kwargs):
            return AugmentedDict({key_ : getattr(value, key)(*args, **kwargs) for key_, value in self.items()})
        return method_wrapper

    def __dir__(self):
        """ Correct autocompletion for delegated methods. """
        if len(self) != 0:
            return dir(self[0])
        return dir(dict)

    # Convenient iterables
    def flatten(self, keys=None):
        """ Get dict values for requested keys in a single list. """
        keys = to_list(keys) if keys is not None else list(self.keys())
        lists = [self[key] if isinstance(self[key], list) else [self[key]] for key in keys]
        flattened = sum(lists, [])
        return AugmentedList(flattened)

    @property
    def flat(self):
        """ List of all dictionary values. """
        return self.flatten()



class MetaDict(dict):
    """ Dictionary that can dump itself on disk in a human-readable and human-editable way.
    Usually describes cube meta info such as name, coordinates (if known) and other useful data.
    """
    def __repr__(self):
        lines = '\n'.join(f'    "{key}" : {repr(value)},'
                          for key, value in self.items())
        return f'{{\n{lines}\n}}'

    @classmethod
    def load(cls, path):
        """ Load self from `path` by evaluating the containing dictionary. """
        with open(path, 'r', encoding='utf-8') as file:
            content = '\n'.join(file.readlines())
        return cls(literal_eval(content.replace('\n', '').replace('    ', '')))

    def dump(self, path):
        """ Save self to `path` with each key on a separate line. """
        with open(path, 'w', encoding='utf-8') as file:
            print(repr(self), file=file)


    @classmethod
    def placeholder(cls):
        """ Default MetaDict"""
        return cls({
            'name': 'UNKNOWN',
            'ru_name': 'Неизвестно',
            'latitude': None,
            'longitude': None,
            'info': 'дополнительная информация о кубе'
        })


class timer:
    """ Context manager for timing the code. """
    def __init__(self, string=''):
        self.string = string
        self.start_time = None

    def __enter__(self):
        self.start_time = perf_counter()
        return self

    def __exit__(self, exc_type, exc_value, exc_tb):
        print(f'{self.string} evaluated in {(perf_counter() - self.start_time):4.4f} seconds')



class SafeIO:
    """ Opens the file handler with desired `open` function, closes it at destruction.
    Can log open and close actions to the `log_file`.
    getattr, getitem and `in` operator are directed to the `handler`.
    """
    def __init__(self, path, opener=open, log_file=None, **kwargs):
        self.path = path
        self.log_file = log_file
        self.handler = opener(path, **kwargs)

        if self.log_file:
            self._info(self.log_file, f'Opened {self.path}')

    def _info(self, log_file, msg):
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write('\n' + msg)

    def __getattr__(self, key):
        return getattr(self.handler, key)

    def __getitem__(self, key):
        return self.handler[key]

    def __contains__(self, key):
        return key in self.handler

    def __del__(self):
        self.handler.close()

        if self.log_file:
            self._info(self.log_file, f'Closed {self.path}')