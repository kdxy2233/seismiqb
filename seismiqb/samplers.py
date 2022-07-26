""" Generator of (label-dependant) randomized locations, mainly for model training.

Locations describe the cube and the exact place to load from in the following format:
(field_id, label_id, orientation, i_start, x_start, h_start, i_stop, x_stop, h_stop).

Locations are passed to `make_locations` method of `SeismicCropBatch`, which
transforms them into 3D slices to index the data and other useful info like origin points, shapes and orientation.

Each of the classes provides:
    - `call` method (aliased to either `sample` or `next_batch`), that generates given amount of locations
    - `to_names` method to convert the first two columns of sampled locations into string names of field and label
    - convenient visualization to explore underlying `locations` structure
"""
import numpy as np
from numba import njit

from batchflow import Sampler, ConstantSampler
from .labels import Horizon, Fault
from .field import Field, SyntheticField
from .geometry import SeismicGeometry
from .utils import filtering_function, AugmentedDict
from .labels.fault import insert_fault_into_mask
from .plotters import plot



class BaseSampler(Sampler):
    """ Common logic of making locations. Refer to the documentation of inherited classes for more details. """
    dim = 9 # dimensionality of sampled points: field_id and label_id, orientation, locations

    def _make_locations(self, field, points, matrix, crop_shape, ranges, threshold, filtering_matrix):
        # Parse parameters
        ranges = ranges if ranges is not None else [None, None, None]
        ranges = [item if item is not None else [0, c]
                  for item, c in zip(ranges, field.shape)]
        ranges = np.array(ranges)

        crop_shape = np.array(crop_shape)
        crop_shape_t = crop_shape[[1, 0, 2]]
        n_threshold = np.int32(crop_shape[0] * crop_shape[1] * threshold)

        # Keep only points, that can be a starting point for a crop of given shape
        i_mask = ((ranges[:2, 0] <= points[:, :2]).all(axis=1) &
                  ((points[:, :2] +   crop_shape[:2]) <= ranges[:2, 1]).all(axis=1))
        x_mask = ((ranges[:2, 0] <= points[:, :2]).all(axis=1) &
                  ((points[:, :2] + crop_shape_t[:2]) <= ranges[:2, 1]).all(axis=1))
        mask = i_mask | x_mask

        points = points[mask]
        i_mask = i_mask[mask]
        x_mask = x_mask[mask]

        # Apply filtration
        if filtering_matrix is not None:
            points = filtering_function(points, filtering_matrix)

        # Keep only points, that produce crops with horizon larger than threshold; append flag
        # TODO: Implement threshold check via filtering points with matrix obtained by
        # convolution of horizon binary matrix and a kernel with size of crop shape
        if threshold != 0.0:
            points = spatial_check_points(points, matrix, crop_shape[:2], i_mask, x_mask, n_threshold)
        else:
            _points = np.empty((i_mask.sum() + x_mask.sum(), 4), dtype=np.int32)
            _points[:i_mask.sum(), 0:3] = points[i_mask, :]
            _points[:i_mask.sum(), 3] = 0

            _points[i_mask.sum():, 0:3] = points[x_mask, :]
            _points[i_mask.sum():, 3] = 1

            points = _points

        # Transform points to (orientation, i_start, x_start, h_start, i_stop, x_stop, h_stop)
        buffer = np.empty((len(points), 7), dtype=np.int32)
        buffer[:, 0] = points[:, 3]
        buffer[:, 1:4] = points[:, 0:3]
        buffer[:, 4:7] = points[:, 0:3]
        buffer[buffer[:, 0] == 0, 4:7] += crop_shape
        buffer[buffer[:, 0] == 1, 4:7] += crop_shape_t

        self.n = len(buffer)
        self.crop_shape = crop_shape
        self.crop_shape_t = crop_shape_t
        self.crop_height = crop_shape[2]
        self.ranges = ranges
        self.threshold = threshold
        self.n_threshold = n_threshold
        return buffer


    @property
    def orientation_matrix(self):
        """ Possible locations, mapped on field top-view map.
            - np.nan where no locations can be sampled.
            - 1 where only iline-oriented crops can be sampled.
            - 2 where only xline-oriented crops can be sampled.
            - 3 where both types of crop orientations can be sampled.
        """
        matrix = np.zeros_like(self.matrix, dtype=np.float32)
        orientations = self.locations[:, 0].astype(np.bool_)

        i_locations = self.locations[~orientations]
        matrix[i_locations[:, 1], i_locations[:, 2]] += 1

        x_locations = self.locations[orientations]
        matrix[x_locations[:, 1], x_locations[:, 2]] += 2

        matrix[matrix == 0] = np.nan
        return matrix


class GeometrySampler(BaseSampler):
    """ Generator of crop locations, based on a field. Not intended to be used directly, see `SeismicSampler`.
    Makes locations that:
        - start from the non-dead trace on a field, excluding those marked by `filtering_matrix`
        - contain more than `threshold` non-dead traces inside
        - don't go beyond cube limits

    Locations are produced as np.ndarray of (size, 9) shape with following columns:
        (field_id, field_id, orientation, i_start, x_start, h_start, i_stop, x_stop, h_stop).
    Depth location is randomized in desired `ranges`.

    Under the hood, we prepare `locations` attribute:
        - filter non-dead trace coordinates so that only points that can generate
        either inline or crossline oriented crop (or both) remain
        - apply `filtering_matrix` to remove more points
        - keep only those points and directions which create crops with more than `threshold` non-dead traces
        - store all possible locations for each of the remaining points
    For sampling, we randomly choose `size` rows from `locations` and generate height in desired range.

    Parameters
    ----------
    field : Field
        Field to base sampler on.
    crop_shape : tuple
        Shape of crop locations to generate.
    threshold : float
        Minimum proportion of labeled points in each sampled location.
    ranges : sequence, optional
        Sequence of three tuples of two ints or `None`s.
        If tuple of two ints, then defines ranges of sampling along corresponding axis.
        If None, then field limits are used (no constraints).
    filtering_matrix : np.ndarray, optional
        Map of points to remove from potentially generated locations.
    field_id, label_id : int
        Used as the first two columns of sampled values.
    """
    def __init__(self, field, crop_shape, threshold=0.05, ranges=None, filtering_matrix=None,
                 field_id=0, label_id=0, **kwargs):
        matrix = (1 - field.zero_traces).astype(np.float32)
        idx = np.nonzero(matrix != 0)
        points = np.hstack([idx[0].reshape(-1, 1),
                            idx[1].reshape(-1, 1),
                            np.zeros((len(idx[0]), 1), dtype=np.int32)]).astype(np.int32)

        self.locations = self._make_locations(field=field, points=points, matrix=matrix,
                                              crop_shape=crop_shape, ranges=ranges, threshold=threshold,
                                              filtering_matrix=filtering_matrix)
        self.kwargs = kwargs

        self.field_id = field_id
        self.label_id = label_id

        self.field = field
        self.matrix = matrix
        self.name = field.short_name
        self.displayed_name = field.displayed_name
        super().__init__()

    def sample(self, size):
        """ Get exactly `size` locations. """
        idx = np.random.randint(self.n, size=size)
        sampled = self.locations[idx]

        heights = np.random.randint(low=self.ranges[2, 0],
                                    high=self.ranges[2, 1] - self.crop_height,
                                    size=size, dtype=np.int32)

        buffer = np.empty((size, 9), dtype=np.int32)
        buffer[:, 0] = self.field_id
        buffer[:, 1] = self.label_id

        buffer[:, [2, 3, 4, 6, 7]] = sampled[:, [0, 1, 2, 4, 5]]
        buffer[:, 5] = heights
        buffer[:, 8] = heights + self.crop_height
        return buffer

    def __repr__(self):
        return f'<GeometrySampler for {self.displayed_name}: '\
               f'crop_shape={tuple(self.crop_shape)}, threshold={self.threshold}>'


class HorizonSampler(BaseSampler):
    """ Generator of crop locations, based on a single horizon. Not intended to be used directly, see `SeismicSampler`.
    Makes locations that:
        - start from the labeled point on horizon, excluding those marked by `filtering_matrix`
        - contain more than `threshold` labeled pixels inside
        - don't go beyond cube limits

    Locations are produced as np.ndarray of (size, 9) shape with following columns:
        (field_id, label_id, orientation, i_start, x_start, h_start, i_stop, x_stop, h_stop).
    Depth location is randomized in (0.1*shape, 0.9*shape) range.

    Under the hood, we prepare `locations` attribute:
        - filter horizon points so that only points that can generate
        either inline or crossline oriented crop (or both) remain
        - apply `filtering_matrix` to remove more points
        - keep only those points and directions which create crops with more than `threshold` labels
        - store all possible locations for each of the remaining points
    For sampling, we randomly choose `size` rows from `locations`. If some of the sampled locations does not fit the
    `threshold` constraint, resample until we get exactly `size` locations.

    Parameters
    ----------
    horizon : Horizon
        Horizon to base sampler on.
    crop_shape : tuple
        Shape of crop locations to generate.
    threshold : float
        Minimum proportion of labeled points in each sampled location.
    ranges : sequence, optional
        Sequence of three tuples of two ints or `None`s.
        If tuple of two ints, then defines ranges of sampling along this axis.
        If None, then field limits are used (no constraints).
        Note that we actually use only the first two elements, corresponding to spatial ranges.
    filtering_matrix : np.ndarray, optional
        Map of points to remove from potentially generated locations.
    field_id, label_id : int
        Used as the first two columns of sampled values.
    shift_height : bool
        Whether apply random shift to height locations of sampled horizon points or not.
    """
    def __init__(self, horizon, crop_shape, threshold=0.05, ranges=None, filtering_matrix=None,
                 shift_height=True, spatial_shift=False,
                 field_id=0, label_id=0, **kwargs):
        field = horizon.field
        matrix = horizon.full_matrix

        self.locations = self._make_locations(field=field, points=horizon.points.copy(), matrix=matrix,
                                              crop_shape=crop_shape, ranges=ranges, threshold=threshold,
                                              filtering_matrix=filtering_matrix)
        self.kwargs = kwargs

        self.field_id = field_id
        self.label_id = label_id

        self.horizon = horizon
        self.field = field
        self.matrix = matrix
        self.name = field.short_name
        self.displayed_name = horizon.short_name

        if shift_height:
            shift_height = shift_height if isinstance(shift_height, tuple) else (0.9, 0.1)
        self.shift_height = shift_height

        self.spatial_shift = spatial_shift
        super().__init__()

    def sample(self, size):
        """ Get exactly `size` locations. """
        if size == 0:
            return np.zeros((0, 9), np.int32)
        if self.threshold == 0.0:
            sampled = self._sample(size)
        else:
            accumulated = 0
            sampled_list = []

            while accumulated < size:
                sampled = self._sample(size*2)
                condition = spatial_check_sampled(sampled, self.matrix, self.n_threshold)

                sampled_list.append(sampled[condition])
                accumulated += condition.sum()
            sampled = np.concatenate(sampled_list)[:size]

        buffer = np.empty((size, 9), dtype=np.int32)
        buffer[:, 0] = self.field_id
        buffer[:, 1] = self.label_id
        buffer[:, 2:] = sampled
        return buffer

    def _sample(self, size):
        idx = np.random.randint(self.n, size=size)
        sampled = self.locations[idx] # (orientation, i_start, x_start, h_start, i_stop, x_stop, h_stop)

        if self.shift_height:
            shift = np.random.randint(low=-int(self.crop_height*self.shift_height[0]),
                                      high=-int(self.crop_height*self.shift_height[1]),
                                      size=(size, 1), dtype=np.int32)
            sampled[:, [3, 6]] += shift

        if self.spatial_shift:
            shapes_i = sampled[:, 4] - sampled[:, 1]
            shift_i = np.random.randint(low=-(shapes_i*self.spatial_shift[0][0]).astype(np.int32),
                                        high=-(shapes_i*self.spatial_shift[0][1]).astype(np.int32),
                                        size=(size, 1), dtype=np.int32)
            sampled[:, [1, 4]] += shift_i

            shapes_x = sampled[:, 5] - sampled[:, 2]
            shift_x = np.random.randint(low=-(shapes_x*self.spatial_shift[1][0]).astype(np.int32),
                                        high=-(shapes_x*self.spatial_shift[1][1]).astype(np.int32),
                                        size=(size, 1), dtype=np.int32)
            sampled[:, [2, 5]] += shift_x

            np.clip(sampled[:, 1], 0, self.field.shape[0] - self.crop_shape[0], out=sampled[:, 1])
            np.clip(sampled[:, 4], 0 + self.crop_shape[0], self.field.shape[0], out=sampled[:, 4])

            np.clip(sampled[:, 2], 0, self.field.shape[1] - self.crop_shape[1], out=sampled[:, 2])
            np.clip(sampled[:, 5], 0 + self.crop_shape[1], self.field.shape[1], out=sampled[:, 5])

        np.clip(sampled[:, 3], 0, self.field.depth - self.crop_height, out=sampled[:, 3])
        np.clip(sampled[:, 6], 0 + self.crop_height, self.field.depth, out=sampled[:, 6])
        return sampled


    def __repr__(self):
        return f'<HorizonSampler for {self.displayed_name}: '\
               f'crop_shape={tuple(self.crop_shape)}, threshold={self.threshold}, '\
               f'shift_height={self.shift_height}, spatial_shift={self.spatial_shift}>'

    @property
    def orientation_matrix(self):
        orientation_matrix = super().orientation_matrix
        if self.horizon.is_carcass:
            orientation_matrix = self.horizon.matrix_enlarge(orientation_matrix, 9)
        return orientation_matrix


class FaultSampler(BaseSampler):
    """ Generator of crop locations, based on a single fault. Not intended to be used directly, see `SeismicSampler`.
    Makes locations that:
        - start from the labeled point on fault
        - don't go beyond cube limits

    Locations are produced as np.ndarray of (size, 9) shape with following columns:
        (field_id, label_id, orientation, i_start, x_start, h_start, i_stop, x_stop, h_stop).
    Location is randomized in (-0.4*shape, 0.4*shape) range.

    For sampling, we randomly choose `size` rows from `locations`. If some of the sampled locations does not fit the
    `threshold` constraint or it is impossible to make crop of defined shape, resample until we get exactly
    `size` locations.

    Parameters
    ----------
    fault : Fault
        Fault to base sampler on.
    crop_shape : tuple
        Shape of crop locations to generate.
    threshold : float
        Minimum proportion of labeled points in each sampled location.
    ranges : sequence, optional
        Sequence of three tuples of two ints or `None`s.
        If tuple of two ints, then defines ranges of sampling along this axis.
        If None, then field limits are used (no constraints).
        Note that we actually use only the first two elements, corresponding to spatial ranges.
    field_id, label_id : int
        Used as the first two columns of sampled values.
    extend : bool
        Create locations in non-labeled slides between labeled slides.
    transpose : bool
        Create transposed crop locations or not.
    """
    def __init__(self, fault, crop_shape, threshold=0, ranges=None, extend=True, transpose=False,
                 field_id=0, label_id=0, **kwargs):
        field = fault.field

        self.points = fault.points
        self.nodes = fault.nodes if hasattr(fault, 'nodes') else None
        self.direction = fault.direction
        self.transpose = transpose

        self.locations = self._make_locations(field, crop_shape, ranges, threshold, extend)

        self.kwargs = kwargs

        self.field_id = field_id
        self.label_id = label_id

        self.field = field
        self.name = field.short_name
        self.displayed_name = fault.short_name
        super().__init__(self)

    @property
    def interpolated_nodes(self):
        """ Create locations in non-labeled slides between labeled slides. """
        slides = np.unique(self.nodes[:, self.direction])
        if len(slides) == 1:
            return self.nodes
        locations = []
        for i, slide in enumerate(slides):
            left = slides[max(i-1, 0)]
            right = slides[min(i+1, len(slides)-1)]
            chunk = self.nodes[self.nodes[:, self.direction] == slide]
            for j in range(left, right):
                chunk[:, self.direction] = j
                locations += [chunk.copy()]
        return np.concatenate(locations, axis=0)

    def _make_locations(self, field, crop_shape, ranges, threshold, extend):
        # Parse parameters
        ranges = ranges if ranges is not None else [None, None, None]
        ranges = [item if item is not None else [0, c]
                  for item, c in zip(ranges, field.shape)]
        ranges = np.array(ranges)

        crop_shape = np.array(crop_shape)
        crop_shape_t = crop_shape[[1, 0, 2]]
        n_threshold = np.int32(np.prod(crop_shape) * threshold)

        if self.nodes is not None:
            nodes = self.interpolated_nodes if extend else self.nodes
        else:
            nodes = self.points

        # Keep only points, that can be a starting point for a crop of given shape
        i_mask = ((ranges[:2, 0] < nodes[:, :2]).all(axis=1) &
                  ((nodes[:, :2] + crop_shape[:2]) < ranges[:2, 1]).all(axis=1))
        x_mask = ((ranges[:2, 0] < nodes[:, :2]).all(axis=1) &
                  ((nodes[:, :2] + crop_shape_t[:2]) < ranges[:2, 1]).all(axis=1))
        nodes = nodes[i_mask | x_mask]

        # Transform points to (orientation, i_start, x_start, h_start, i_stop, x_stop, h_stop)
        directions = [0, 1] if self.transpose else [self.direction]

        buffer = np.empty((len(nodes) * len(directions), 7), dtype=np.int32)

        for i, direction, in enumerate(directions):
            start, end = i * len(nodes), (i+1) * len(nodes)
            shape = crop_shape if direction == 0 else crop_shape_t
            buffer[start:end, 1:4] = nodes - shape // 2
            buffer[start:end, 4:7] = buffer[start:end, 1:4] + shape
            buffer[start:end, 0] = direction

        self.n = len(buffer)
        self.crop_shape = crop_shape
        self.crop_shape_t = crop_shape_t
        self.crop_height = crop_shape[2]
        self.ranges = ranges
        self.threshold = threshold
        self.n_threshold = n_threshold
        return buffer

    def sample(self, size):
        """ Get exactly `size` locations. """
        if size == 0:
            return np.zeros((0, 9), np.int32)
        accumulated = 0
        sampled_list = []

        while accumulated < size:
            sampled = self._sample(size*4)
            condition = volumetric_check_sampled(sampled, self.points, self.crop_shape,
                                                 self.crop_shape_t, self.n_threshold)

            sampled_list.append(sampled[condition])
            accumulated += condition.sum()
        sampled = np.concatenate(sampled_list)[:size]

        buffer = np.empty((size, 9), dtype=np.int32)
        buffer[:, 0] = self.field_id
        buffer[:, 1] = self.label_id
        buffer[:, 2:] = sampled
        return buffer

    def _sample(self, size):
        idx = np.random.randint(self.n, size=size)
        sampled = self.locations[idx]
        i_mask = sampled[:, 0] == 0
        x_mask = sampled[:, 0] == 1

        for mask, shape in zip([i_mask, x_mask], [self.crop_shape, self.crop_shape_t]):
            high = np.floor(shape * 0.4)
            low = -high
            low[shape == 1] = 0
            high[shape == 1] = 1

            shift = np.random.randint(low=low, high=high, size=(mask.sum(), 3), dtype=np.int32)
            sampled[mask, 1:4] += shift
            sampled[mask, 4:] += shift

            sampled[mask, 1:4] = np.clip(sampled[mask, 1:4], 0, self.field.cube_shape - shape)
            sampled[mask, 4:7] = np.clip(sampled[mask, 4:7], 0 + shape, self.field.cube_shape)
        return sampled

    def __repr__(self):
        return f'<FaultSampler for {self.displayed_name}: '\
               f'crop_shape={tuple(self.crop_shape)}, threshold={self.threshold}>'


class SyntheticSampler(Sampler):
    """ A sampler for synthetic fields (and their labels).
    As every synthetically generated crop is completely valid from a sampling point of view,
    we just return placeholder random locations of the desired `crop_shape`.
    """
    def __init__(self, field, crop_shape, field_id=None, label_id=None, **kwargs):
        self.field = field
        self.crop_shape = crop_shape
        self.field_id = field_id
        self.label_id = label_id
        self.kwargs = kwargs
        self._n = 10000
        self.n = self._n ** 3

        self.name = self.displayed_name = field.name
        super().__init__()

    def sample(self, size):
        """ Get exactly `size` locations. """
        buffer = np.empty((size, 9), dtype=np.int32)
        buffer[:, 0] = self.field_id
        buffer[:, 1] = self.label_id
        buffer[:, 2] = 0

        start_point = np.random.randint(low=(0, 0, 0), high=(self._n, self._n, self._n),
                                        size=(size, 3), dtype=np.int32)
        end_point = start_point + self.crop_shape
        buffer[:, [3, 4, 5]] = start_point
        buffer[:, [6, 7, 8]] = end_point
        return buffer


@njit
def spatial_check_points(points, matrix, crop_shape, i_mask, x_mask, threshold):
    """ Compute points, which would generate crops with more than `threshold` labeled pixels.
    For each point, we test two possible shapes (i-oriented and x-oriented) and check `matrix` to compute the
    number of present points. Therefore, each of the initial points can result in up to two points in the output.

    Used as one of the filters for points creation at sampler initialization.

    Parameters
    ----------
    points : np.ndarray
        Points in (i_start, x_start, h_start) format.
    matrix : np.ndarray
        Depth map in cube coordinates.
    crop_shape : tuple of two ints
        Spatial shape of crops to generate: (i_shape, x_shape).
    i_mask : np.ndarray
        For each point, whether to test i-oriented shape.
    x_mask : np.ndarray
        For each point, whether to test x-oriented shape.
    threshold : int
        Minimum amount of points in a generated crop.
    """
    shape_i, shape_x = crop_shape

    # Return inline, crossline, corrected_depth (mean across crop), and 0/1 as i/x flag
    buffer = np.empty((2 * len(points), 4), dtype=np.int32)
    counter = 0

    for (point_i, point_x, _), i_mask_, x_mask_ in zip(points, i_mask, x_mask):
        if i_mask_:
            sliced = matrix[point_i:point_i+shape_i, point_x:point_x+shape_x].ravel()
            present_mask = (sliced > 0)

            if present_mask.sum() >= threshold:
                h_mean = np.rint(sliced[present_mask].mean())
                buffer[counter, :] = point_i, point_x, np.int32(h_mean), np.int32(0)
                counter += 1

        if x_mask_:
            sliced = matrix[point_i:point_i+shape_x, point_x:point_x+shape_i].ravel()
            present_mask = (sliced > 0)

            if present_mask.sum() >= threshold:
                h_mean = np.rint(sliced[present_mask].mean())
                buffer[counter, :] = point_i, point_x, np.int32(h_mean), np.int32(1)
                counter += 1
    return buffer[:counter]

@njit
def spatial_check_sampled(locations, matrix, threshold):
    """ Remove points, which correspond to crops with less than `threshold` labeled pixels.
    Used as a final filter for already sampled locations: they can generate crops with
    smaller than `threshold` mask only due to the depth randomization.

    Parameters
    ----------
    locations : np.ndarray
        Locations in (orientation, i_start, x_start, h_start, i_stop, x_stop, h_stop) format.
    matrix : np.ndarray
        Depth map in cube coordinates.
    threshold : int
        Minimum amount of labeled pixels in a crop.

    Returns
    -------
    condition : np.ndarray
        Boolean mask for locations.
    """
    condition = np.ones(len(locations), dtype=np.bool_)

    for i, (_, i_start, x_start, h_start, i_stop,  x_stop,  h_stop) in enumerate(locations):
        sliced = matrix[i_start:i_stop, x_start:x_stop].ravel()
        present_mask = (h_start < sliced) & (sliced < h_stop)

        if present_mask.sum() < threshold:
            condition[i] = False
    return condition

@njit
def volumetric_check_sampled(locations, points, crop_shape, crop_shape_t, threshold):
    """ Remove points, which correspond to crops with less than `threshold` labeled pixels.
    Used as a final filter for already sampled locations: they can generate crops with
    smaller than `threshold`.

    Parameters
    ----------
    locations : np.ndarray
        Locations in (orientation, i_start, x_start, h_start, i_stop, x_stop, h_stop) format.
    points : points
        Fault points.
    crop_shape : np.ndarray
        Crop shape
    crop_shape_t : np.ndarray
        Tranposed crop shape
    threshold : int
        Minimum amount of labeled pixels in a crop.

    Returns
    -------
    condition : np.ndarray
        Boolean mask for locations.
    """
    condition = np.ones(len(locations), dtype=np.bool_)

    if threshold > 0:
        for i, (orientation, i_start, x_start, h_start, i_stop,  x_stop, h_stop) in enumerate(locations):
            shape = crop_shape if orientation == 0 else crop_shape_t
            mask_bbox = np.array([[i_start, i_stop], [x_start, x_stop], [h_start, h_stop]], dtype=np.int32)
            mask = np.zeros((shape[0], shape[1], shape[2]), dtype=np.int32)

            insert_fault_into_mask(mask, points, mask_bbox, 1, 0)
            if mask.sum() < threshold:
                condition[i] = False

    return condition


class SeismicSampler(Sampler):
    """ Mixture of samplers for multiple cubes with multiple labels.
    Used to sample crop locations in the format of
    (field_id, label_id, orientation, i_start, x_start, h_start, i_stop, x_stop, h_stop).

    Parameters
    ----------
    labels : dict
        Dictionary where keys are cube names and values are lists of labels.
    proportions : sequence, optional
        Proportion of each cube in the resulting mixture.
    baseclass : type
        Class for initializing individual label samplers.
    crop_shape : tuple
        Shape of crop locations to generate.
    threshold : float
        Minimum proportion of labeled points in each sampled location.
    ranges : sequence, optional
        Sequence of three tuples of two ints or `None`s.
        If tuple of two ints, then defines ranges of sampling along this axis.
        If None, then field limits are used (no constraints).
        Note that we actually use only the first two elements, corresponding to spatial ranges.
    filtering_matrix : np.ndarray, optional
        Map of points to remove from potentially generated locations.
    shift_height : bool
        Whether to apply random shift to height locations of sampled horizon points or not.
    kwargs : dict
        Other parameters of initializing label samplers.
    """
    LABELCLASS_TO_SAMPLERCLASS = {
        Field: GeometrySampler,
        SyntheticField: SyntheticSampler,
        SeismicGeometry: GeometrySampler,
        Horizon: HorizonSampler,
        Fault: FaultSampler,
    }

    @classmethod
    def labelclass_to_samplerclass(cls, labelclass):
        """ Mapping between label classes and used samplers.
        Uses `issubclass` check in addition to getitem.
        """
        samplerclass = cls.LABELCLASS_TO_SAMPLERCLASS.get(labelclass)
        if samplerclass is not None:
            return samplerclass

        for class_, samplerclass in cls.LABELCLASS_TO_SAMPLERCLASS.items():
            if issubclass(labelclass, class_):
                return samplerclass
        raise KeyError(f'Unable to determine the sampler class for `{labelclass}`')


    def __init__(self, labels, crop_shape, proportions=None,
                 threshold=0.05, ranges=None, filtering_matrix=None, shift_height=True, **kwargs):
        # One sampler of each `label` for each `field`
        names, sampler_classes = {}, {}
        samplers = AugmentedDict({field_name: [] for field_name in labels.keys()})

        labels_weights = []

        for field_id, (field_name, list_labels) in enumerate(labels.items()):
            list_labels = list_labels if isinstance(list_labels, (tuple, list)) else [list_labels]

            # Unpack parameters
            crop_shape_ = crop_shape[field_name] if isinstance(crop_shape, dict) else crop_shape
            threshold_ = threshold[field_name] if isinstance(threshold, dict) else threshold
            filtering_matrix_ = filtering_matrix[field_name] if isinstance(filtering_matrix, dict) else filtering_matrix
            ranges_ = ranges[field_name] if isinstance(ranges, dict) else ranges

            # Mixture for each field
            label_classes = [type(label) for label in list_labels]
            if len(set(label_classes)) != 1:
                raise ValueError(f'Labels contain different classes, {set(label_classes)}!')
            sampler_class = self.labelclass_to_samplerclass(label_classes[0])
            sampler_classes[field_id] = sampler_class

            for label_id, label in enumerate(list_labels):

                label_sampler = sampler_class(label, crop_shape=crop_shape_, threshold=threshold_,
                                              ranges=ranges_, filtering_matrix=filtering_matrix_,
                                              field_id=field_id, label_id=label_id, shift_height=shift_height,
                                              **kwargs)

                if label_sampler.n != 0:
                    samplers[field_name].append(label_sampler)
                    names[(field_id, label_id)] = (field_name, label.short_name)

            weights = np.array([len(label) for label in list_labels])
            weights = weights / weights.sum()
            labels_weights.append(weights)

        # Resulting sampler
        n_present_fields = sum(len(sampler_list) != 0 for sampler_list in samplers.values())
        if n_present_fields == 0:
            raise ValueError('Empty sampler!')

        proportions = proportions or [1 / n_present_fields for _ in labels]
        final_weights = AugmentedDict({idx: [] for idx in labels.keys()})

        sampler = 0 & ConstantSampler(np.int32(0), dim=9)

        for (field_name, sampler_list), p, l in zip(samplers.items(), proportions, labels_weights):
            if len(sampler_list) != 0:
                for label_sampler, label_weight in zip(sampler_list, l):
                    w = p * label_weight
                    final_weights[field_name].append(w)
                    sampler = sampler | (w & label_sampler)


        self.sampler = sampler
        self.samplers = samplers
        self.names = names
        self.sampler_classes = sampler_classes
        self.final_weights = final_weights

        self.crop_shape = crop_shape
        self.threshold = threshold
        self.proportions = proportions


    def sample(self, size):
        """ Generate exactly `size` locations. """
        return self.sampler.sample(size)

    def __call__(self, size):
        return self.sampler.sample(size)

    def to_names(self, id_array):
        """ Convert the first two columns of sampled locations into field and label string names. """
        return np.array([self.names[tuple(ids)] for ids in id_array])

    def __len__(self):
        return sum(len(sampler.locations) for sampler_list in self.samplers.values() for sampler in sampler_list)

    def __str__(self):
        msg = 'SeismicSampler:'
        for list_samplers, p in zip(self.samplers.values(), self.proportions):
            msg += f'\n    {list_samplers[0].field.short_name} @ {p}'
            for sampler in list_samplers:
                msg += f'\n        {sampler}'
        return msg

    def show_locations(self, savepath=None, **kwargs):
        """ Visualize on field map by using underlying `locations` structure. """
        data = []
        title = []
        xlabel = []
        ylabel = []

        for samplers_list in self.samplers.values():
            field = samplers_list[0].field

            if isinstance(field, SyntheticField):
                continue

            data += [[sampler.orientation_matrix, field.zero_traces] for sampler in samplers_list]
            title += [f'{field.displayed_name}: {sampler.displayed_name}' for sampler in samplers_list]
            xlabel += [field.index_headers[0]] * len(samplers_list)
            ylabel += [field.index_headers[1]] * len(samplers_list)

        data.append(None) # reserve extra subplot for future legend

        kwargs = {
            'cmap': [['Sampler', 'black']] * len(data),
            'alpha': [[1.0, 0.4]] * len(data),
            'title': title,
            'vmin': [[1, 0]] * len(data),
            'vmax': [[3, 1]] * len(data),
            'xlabel': xlabel,
            'ylabel': ylabel,
            **kwargs
        }

        plotter = plot(data, **kwargs)

        legend_params = {
            'mode': 'image',
            'color': ('purple','blue','red', 'white', 'gray'),
            'label': ('ILINES and CROSSLINES', 'only ILINES', 'only CROSSLINES', 'restricted', 'dead traces'),
            'size': 20,
            'loc': 10,
            'facecolor': 'silver',
        }

        plotter[-1].add_legend(**legend_params)
        if savepath is not None:
            plotter.save(savepath=savepath)
        return plotter

    def show_sampled(self, n=10000, binary=False, savepath=None, **kwargs):
        """ Visualize on field map by sampling `n` crop locations. """
        sampled = self.sample(n)

        data = []
        title = []
        for field_id in np.unique(sampled[:, 0]):
            field = self.samplers[field_id][0].field

            if isinstance(field, SyntheticField):
                continue

            matrix = np.zeros_like(field.zero_traces, dtype=np.int32)

            sampled_ = sampled[sampled[:, 0] == field_id]
            for (_, _, _, point_i_start, point_x_start, _, point_i_stop,  point_x_stop,  _) in sampled_:
                matrix[point_i_start : point_i_stop, point_x_start : point_x_stop] += 1
            if binary:
                matrix[matrix > 0] = 1

            field_data = [matrix, field.zero_traces]
            data.append(field_data)

            field_title = f'{field.displayed_name}: {len(sampled_)} points'
            title.append(field_title)

        data.append(None) # reserve extra subplot for future legend

        kwargs = {
            'matrix_name': 'Sampled slices',
            'cmap': [['Reds', 'black']] * len(data),
            'alpha': [[1.0, 0.4]] * len(data),
            'title': title,
            'interpolation': 'bilinear',
            'xlabel': field.index_headers[0],
            'ylabel': field.index_headers[1],
            **kwargs
        }

        plotter = plot(data, **kwargs)

        legend_params = {
            'mode': 'image',
            'color': ('beige', 'salmon', 'grey'),
            'label': ('alive traces', 'sampled locations', 'dead traces'),
            'size': 25,
            'loc': 10,
            'facecolor': 'silver',
        }

        plotter[-1].add_legend(**legend_params)
        if savepath is not None:
            plotter.save(savepath=savepath)
        return plotter
