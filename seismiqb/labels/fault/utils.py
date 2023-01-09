""" Helpers for coordinates processing. """
import numpy as np
from numba import njit

from scipy.ndimage.morphology import binary_erosion
from ...utils import groupby_min, groupby_max, groupby_all

# Coordinates operations
def dilate_coords(coords, dilate=3, axis=0, max_value=None):
    """ Dilate coordinates with (dilate, 1) structure. """
    dilated_coords = np.tile(coords, (dilate, 1))

    for counter, i in enumerate(range(-(dilate//2), dilate//2 + 1)):
        start_idx, end_idx = counter*len(coords), (counter + 1)*len(coords)
        dilated_coords[start_idx:end_idx, axis] += i

    if max_value is not None:
        dilated_coords = dilated_coords[(dilated_coords[:, axis] >= 0) & (dilated_coords[:, axis] <= max_value)]
    else:
        dilated_coords = dilated_coords[dilated_coords[:, axis] >= 0]

    dilated_coords = np.unique(dilated_coords, axis=0) # TODO: think about np.unique replacement
    return dilated_coords

@njit
def thin_coords(coords, values):
    """ Thin coords depend on values (choose coordinates corresponding to max values along the last axis).
    Rough approximation of `find_peaks` for coordinates.
    """
    order = np.argsort(coords[:, -1])[::-1]

    output = np.zeros_like(coords)
    position = 0

    idx = order[0]

    point_to_save = coords[idx, :]
    previous_depth = point_to_save[-1]
    previous_value = values[idx]

    for i in range(1, len(coords)):
        idx = order[i]
        current_depth = coords[idx, -1]
        current_value = values[idx]

        if previous_depth == current_depth:
            if previous_value < current_value:
                point_to_save = coords[idx, :]

                previous_value = current_value

        else:
            output[position, :] = point_to_save

            position += 1

            point_to_save = coords[idx, :]
            previous_depth = current_depth
            previous_value = current_value

    # last depth update
    output[position, :] = point_to_save
    position += 1

    return output[:position, :]

# Distance evaluation
def bboxes_intersected(bbox_1, bbox_2, axes=(0, 1, 2)):
    """ Check bboxes intersections on axes. """
    for axis in axes:
        borders_delta = min(bbox_1[axis, 1], bbox_2[axis, 1]) - max(bbox_1[axis, 0], bbox_2[axis, 0])

        if borders_delta < 0:
            return False
    return True

@njit
def bboxes_adjoin(bbox_1, bbox_2, axis=2):
    """ Check that bboxes are adjoint on axis and return intersection/adjoint indices. """
    axis = 2 if axis == -1 else axis

    for i in range(3):
        min_ = min(bbox_1[i, 1], bbox_2[i, 1])
        max_ = max(bbox_1[i, 0], bbox_2[i, 0])

        if min_ - max_ < -1: # distant bboxes
            return None, None

        if i == axis:
            intersection_borders = (min_, max_)

    return min(intersection_borders), max(intersection_borders) # intersection / adjoint indices for the axis

@njit
def max_depthwise_distance(coords_1, coords_2, depths_ranges, step, axis, max_threshold=None):
    """ Find maximal depth-wise central distance between coordinates."""
    max_distance = 0

    for depth in range(depths_ranges[0], depths_ranges[1]+1, step):
        coords_1_depth_slice = coords_1[coords_1[:, -1] == depth, axis]
        coords_2_depth_slice = coords_2[coords_2[:, -1] == depth, axis]

        distance = np.abs(coords_1_depth_slice[len(coords_1_depth_slice)//2] - \
                          coords_2_depth_slice[len(coords_2_depth_slice)//2])

        if (max_threshold is not None) and (distance >= max_threshold):
            return distance

        if distance > max_distance:
            max_distance = distance

    return max_distance


# Object-oriented operations
def find_border(coords, find_lower_border, projection_axis):
    """ Find non-closed border part of the 3d object (upper or lower border).

    Under the hood, we find border of a 2d projection on `projection_axis` and restore 3d coordinates.
    ..!!..

    Parameters
    ----------
    find_lower_border : bool
        Find lower or upper border for object.
    """
    anchor_axis = 1 if projection_axis == 0 else 0

    # Make 2d projection on projection_axis
    bbox = np.column_stack([np.min(coords, axis=0), np.max(coords, axis=0)])
    bbox = np.delete(bbox, projection_axis, 0)

    origin = bbox[:, 0]
    image_shape = bbox[:, 1] - bbox[:, 0] + 1

    mask = np.zeros(image_shape, bool)
    mask[coords[:, anchor_axis] - origin[0], coords[:, 2] - origin[1]] = 1

    contour = mask ^ binary_erosion(mask)

    coords_2d = np.nonzero(contour)

    contour_coords = np.zeros((len(coords_2d[0]), 3), dtype=int)

    contour_coords[:, anchor_axis] = coords_2d[0] + origin[0]
    contour_coords[:, 2] = coords_2d[1] + origin[1]

    # Delete extra border from contour
    contour_coords = groupby_max(contour_coords) if find_lower_border else groupby_min(contour_coords)

    # Restore 3d coordinates
    contour_coords = restore_coords_from_projection(coords=coords, buffer=contour_coords, axis=projection_axis)
    return contour_coords

@njit
def restore_coords_from_projection(coords, buffer, axis):
    """ Find `axis` coordinates from coordinates and their projection.

    ..!!..
    """
    known_axes = [i for i in range(3) if i != axis]

    for i, buffer_line in enumerate(buffer):
        buffer[i, axis] = min(coords[(coords[:, known_axes[0]] == buffer_line[known_axes[0]]) & \
                                     (coords[:, known_axes[1]] == buffer_line[known_axes[1]]),
                                     axis])
    return buffer
