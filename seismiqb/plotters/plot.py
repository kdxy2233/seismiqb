""" Plotter with redefined defaults. """
import batchflow



class plot(batchflow.plot):
    """ Wrapper over original `plot` with custom defaults. """
    IMAGE_DEFAULTS = {
        **batchflow.plot.IMAGE_DEFAULTS,
        'labeltop': True,
        'labelright': True,
        'xlabel_size': 22,
        'ylabel_size': 22,
        'transpose': (1, 0, 2)
    }

    def _ipython_display_(self):
        return None