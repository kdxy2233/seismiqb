""" Helpers for coordinates processing. """
import numpy as np
from numba import njit

from scipy.ndimage.morphology import binary_erosion

# Coordinates operations
def dilate_coords(coords, dilate=3, axis=0, max_value=None):
    """ Dilate coordinates with (dilate, 1) structure. """
    dilated_coords = np.tile(coords, (dilate, 1))

    # Create dilated coordinates
    for i in range(dilate):
        start_idx, end_idx = i*len(coords), (i + 1)*len(coords)
        dilated_coords[start_idx:end_idx, axis] += i - dilate//2

    # Clip to the valid values
    if max_value is not None:
        dilated_coords = dilated_coords[(dilated_coords[:, axis] >= 0) & (dilated_coords[:, axis] <= max_value)]
    else:
        dilated_coords = dilated_coords[dilated_coords[:, axis] >= 0]

    # Get sorted unique values
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

    argmax_coord = coords[idx, :]
    previous_depth = argmax_coord[-1]
    previous_value = values[idx]

    for idx in order[1:]:
        current_depth = coords[idx, -1]
        current_value = values[idx]

        if previous_depth == current_depth:
            if previous_value < current_value:
                argmax_coord = coords[idx, :]

                previous_value = current_value

        else:
            output[position, :] = argmax_coord

            position += 1

            argmax_coord = coords[idx, :]
            previous_depth = current_depth
            previous_value = current_value

    # last depth update
    output[position, :] = argmax_coord
    position += 1

    return output[:position, :]

# Distance evaluation
def bboxes_intersected(bbox_1, bbox_2, axes=(0, 1, 2)):
    """ Check bboxes intersection on axes. """
    for axis in axes:
        overlap_size = min(bbox_1[axis, 1], bbox_2[axis, 1]) - max(bbox_1[axis, 0], bbox_2[axis, 0])

        if overlap_size < 0:
            return False
    return True

@njit
def bboxes_adjacent(bbox_1, bbox_2):
    """ Bboxes intersection or adjacent ranges if bboxes are adjoint. """
    borders = []

    for i in range(3):
        left_overlap_border = min(bbox_1[i, 1], bbox_2[i, 1])
        right_overlap_border = max(bbox_1[i, 0], bbox_2[i, 0])

        if left_overlap_border - right_overlap_border < -1:
            return None

        borders.append((left_overlap_border, right_overlap_border))

    return borders

@njit
def max_depthwise_distance(coords_1, coords_2, depths_ranges, step, axis, max_threshold=None):
    """ Find maximal depth-wise central distance between coordinates.
    
    ..!!..
    max_threshold : int or float
        Early stopping: threshold for max distance value.
    """
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

def find_contour(coords, projection_axis):
    """ Find closed contour of 2d projection.

    Note, returned contour coordinates are equal to 0 for the projection axis.
    """
    anchor_axis = 1 - projection_axis

    # Make 2d projection on projection_axis
    bbox = np.column_stack([np.min(coords, axis=0), np.max(coords, axis=0)])
    bbox = np.delete(bbox, projection_axis, 0)

    # Create object mask
    origin = bbox[:, 0]
    image_shape = bbox[:, 1] - bbox[:, 0] + 1

    mask = np.zeros(image_shape, bool)
    mask[coords[:, anchor_axis] - origin[0], coords[:, 2] - origin[1]] = 1

    # Get object contour
    contour = mask ^ binary_erosion(mask)

    # Extract coordinates from contour mask
    coords_2d = np.nonzero(contour)

    contour_coords = np.zeros((len(coords_2d[0]), 3), dtype=np.int16)

    contour_coords[:, anchor_axis] = coords_2d[0] + origin[0]
    contour_coords[:, 2] = coords_2d[1] + origin[1]
    return contour_coords

@njit
def restore_coords_from_projection(coords, buffer, axis):
    """ Find `axis` coordinates from coordinates and their projection.

    ..!!..
    buffer : np.ndarray
        Buffer with projection coordinates.
    """
    known_axes = [i for i in range(3) if i != axis]

    for i, buffer_line in enumerate(buffer):
        buffer[i, axis] = min(coords[(coords[:, known_axes[0]] == buffer_line[known_axes[0]]) & \
                                     (coords[:, known_axes[1]] == buffer_line[known_axes[1]]),
                                     axis])
    return buffer
