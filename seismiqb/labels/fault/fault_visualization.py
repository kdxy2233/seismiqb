""" Fault visualization mixin. """

import numpy as np

from ..mixins import VisualizationMixin
from ...plotters import show_3d
from ...utils import make_slices

class FaultVisualizationMixin(VisualizationMixin):
    """ Mixin to visualize fault. """
    def __repr__(self):
        return f"""<Fault `{self.name}` for `{self.field.displayed_name}` at {hex(id(self))}>"""

    def load_slide(self, loc, axis=0, width=3):
        """ Create a mask at desired location along supplied axis. """
        axis = self.field.geometry.parse_axis(axis)
        locations = self.field.geometry.make_slide_locations(loc, axis=axis)
        shape = np.array([(slc.stop - slc.start) for slc in locations])
        width = width or max(5, shape[-1] // 100)

        mask = np.zeros(shape, dtype=np.float32)
        mask = self.add_to_mask(mask, locations=locations, width=width)
        return np.squeeze(mask)

    def show_slide(self, loc, **kwargs):
        """ Show slides from seismic with fault. """
        cmap = kwargs.get('cmap', ['Greys_r', 'red'])
        width = kwargs.get('width', 5)

        kwargs = {**kwargs, 'cmap': cmap, 'width': width}
        super().show_slide(loc, **kwargs)

    def show(self, axis=0, centering=True, zoom=None, **kwargs):
        """ Show center of fault for different axes. """
        if centering and zoom is None:
            zoom = [
                slice(max(0, self.bbox[i][0]-20), min(self.bbox[i][1]+20, self.field.shape[i]))
                for i in range(3) if i != axis
            ]

        return self.show_slide(loc=int(np.mean(self.bbox[axis])), zoom=zoom, axis=axis, **kwargs)

    def show_3d(self, sticks_step=None, stick_nodes_step=None, z_ratio=1., colors='green',
                zoom=None, margin=20, sticks=False, **kwargs):
        """ Interactive 3D plot. Roughly, does the following:
            - select `n` points to represent the horizon surface
            - triangulate those points
            - remove some of the triangles on conditions
            - use Plotly to draw the tri-surface

        Parameters
        ----------
        sticks_step : int
            Number of slides between sticks.
        stick_nodes_step : int
            Distance between stick nodes
        z_ratio : int
            Aspect ratio between height axis and spatial ones.
        zoom : tuple of slices or None.
            Crop from cube to show. If None, the whole cube volume will be shown.
        show_axes : bool
            Whether to show axes and their labels, by default True
        width, height : int
            Size of the image, by default 1200, 1200
        margin : int
            Added margin from below and above along height axis, by default 20
        savepath : str
            Path to save interactive html to.
        sticks : bool
            If True, show fault sticks. If False, show interpolated surface.
        kwargs : dict
            Other arguments of plot creation.
        """
        title = f'Fault `{self.name}` on `{self.field.displayed_name}`'
        aspect_ratio = (self.i_length / self.x_length, 1, z_ratio)
        axis_labels = (self.field.index_headers[0], self.field.index_headers[1], 'DEPTH')

        zoom = make_slices(zoom, self.field.shape)

        margin = [margin] * 3 if isinstance(margin, int) else margin
        x, y, z, simplices = self.make_triangulation(zoom, sticks_step, stick_nodes_step, sticks)
        if isinstance(colors, str):
            colors = [colors for _ in simplices]

        show_3d(x, y, z, simplices, title=title, zoom=zoom, aspect_ratio=aspect_ratio,
                axis_labels=axis_labels, margin=margin, colors=colors, **kwargs)

    # TODO: cache?
    def make_triangulation(self, slices=None, sticks_step=None, stick_nodes_step=None, sticks=False, **kwargs):
        """ Return triangulation of the fault. It will created if needed. """
        if sticks_step is not None or stick_nodes_step is not None:
            fake_fault = self._class_({'points': self.points}, field=self.field)
            fake_fault.points_to_sticks(slices, sticks_step or 10, stick_nodes_step or 10)
            return fake_fault.make_triangulation(slices, sticks=sticks, **kwargs)

        if sticks:
            sticks = self.sticks
            faults = [
                self.__class__({'sticks': [stick]}, direction=self.direction,
                               field=self.field, name=self.short_name + '_' + str(i))
                for i, stick in enumerate(sticks)
            ]
            x, y, z, simplices = [], [], [], []
            n_points = 0
            for fault in faults:
                triangulation = fault.make_triangulation(slices)
                if len(triangulation[3]) > 0:
                    simplices.append(triangulation[3] + n_points)
                x.append(triangulation[0])
                y.append(triangulation[1])
                z.append(triangulation[2])
                n_points += len(triangulation[0])
            return [np.concatenate(data) if len(data) > 0 else data for data in [x, y, z, simplices]]

        if len(self.simplices) > 0:
            return self.nodes[:, 0], self.nodes[:, 1], self.nodes[:, 2], self.simplices
        if len(self.sticks) == 1 and len(self.sticks[0]) > 1:
            fake_fault = get_fake_one_stick_fault(self)
            return fake_fault.make_triangulation(slices, sticks_step, stick_nodes_step,  **kwargs)

        return [], [], [], []

def get_fake_one_stick_fault(fault):
    """ Create fault with shifted stick to visualize one stick faults. """
    stick = fault.sticks[0]
    stick_2 = stick.copy() # TODO
    loc = stick[0, fault.direction]
    stick_2[:, fault.direction] = loc - 1 if loc >= 1 else loc + 1

    fake_fault = fault.__class__({'sticks': np.array([stick, stick_2])}, direction=fault.direction,
                       field=fault.field)

    return fake_fault
