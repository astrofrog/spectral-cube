from __future__ import print_function, absolute_import, division

import os
import warnings
from astropy.io import fits
from astropy.extern import six
from astropy.wcs import WCS
import numpy as np

from .. import SpectralCube, StokesSpectralCube, BooleanArrayMask, LazyMask
from .. import cube_utils

try:
    import taskinit
except ImportError:
    TASKINIT_INSTALLED = False
    try:
        import casacore
    except ImportError:
        CASACORE_INSTALLED = False
    else:
        CASACORE_INSTALLED = True
        del casacore
else:
    TASKINIT_INSTALLED = True
    del taskinit

# Read and write from a CASA image. This has a few
# complications. First, by default CASA does not return the
# "python order" and so we either have to transpose the cube on
# read or have dueling conventions. Second, CASA often has
# degenerate stokes axes present in unpredictable places (3rd or
# 4th without a clear expectation). We need to replicate these
# when writing but don't want them in memory. By default, try to
# yield the same array in memory that we would get from astropy.


def is_casa_image(input, **kwargs):
    if isinstance(input, six.string_types):
        if input.endswith('.image'):
            return True
    return False


def flattened(iterable):
    print(iterable)
    flat = []
    for item in iterable:
        if np.isscalar(item):
            flat.append(item)
        elif len(item) == 0:
            flat.append('')
        else:
            flat.extend(item)
    print(flat)
    return flat


def wcs_casa2astropy(casa_wcs):
    """
    Convert a casac.coordsys object into an astropy.wcs.WCS object
    """

    if TASKINIT_INSTALLED:

        wcs = WCS(naxis=int(casa_wcs.naxes()))

        crpix = casa_wcs.referencepixel()
        if crpix['ar_type'] != 'absolute':
            raise ValueError("Unexpected ar_type: %s" % crpix['ar_type'])
        elif crpix['pw_type'] != 'pixel':
            raise ValueError("Unexpected pw_type: %s" % crpix['pw_type'])
        else:
            wcs.wcs.crpix = crpix['numeric']

        cdelt = casa_wcs.increment()
        if cdelt['ar_type'] != 'absolute':
            raise ValueError("Unexpected ar_type: %s" % cdelt['ar_type'])
        elif cdelt['pw_type'] != 'world':
            raise ValueError("Unexpected pw_type: %s" % cdelt['pw_type'])
        else:
            wcs.wcs.cdelt = cdelt['numeric']

        crval = casa_wcs.referencevalue()
        if crval['ar_type'] != 'absolute':
            raise ValueError("Unexpected ar_type: %s" % crval['ar_type'])
        elif crval['pw_type'] != 'world':
            raise ValueError("Unexpected pw_type: %s" % crval['pw_type'])
        else:
            wcs.wcs.crval = crval['numeric']

        wcs.wcs.cunit = casa_wcs.units()

        names = casa_wcs.names()
        types = casa_wcs.axiscoordinatetypes()
        direction_coords = [names[i] for i in range(len(names)) if types[i].lower() == 'direction']
        projection = casa_wcs.projection()['type']

    elif CASACORE_INSTALLED:

        # TODO: order returned here is not intrinsic WCS oder, need to check get_image_axis()

        wcs = WCS(naxis=len(flattened(casa_wcs.get_axes())))
        wcs.wcs.crpix = flattened(casa_wcs.get_referencepixel())
        wcs.wcs.crval = flattened(casa_wcs.get_referencevalue())
        wcs.wcs.cdelt = flattened(casa_wcs.get_increment())
        wcs.wcs.cunit = flattened(casa_wcs.get_unit())

        names = flattened(casa_wcs.get_axes())
        direction_coords = casa_wcs.get_coordinate('direction').get_axes()
        projection = casa_wcs.get_coordinate('direction').get_projection()

    else:

        raise Exception("Loading CASA WCS requires either CASA or the "
                        "python-casacore package to be installed")

    # mapping betweeen CASA and FITS
    COORD_TYPE = {}
    COORD_TYPE['Right Ascension'] = "RA--"
    COORD_TYPE['Declination'] = "DEC-"
    COORD_TYPE['Longitude'] = "GLON"
    COORD_TYPE['Latitude'] = "GLAT"
    COORD_TYPE['Frequency'] = "FREQ"
    COORD_TYPE['Stokes'] = "STOKES"

    # There is no easy way at the moment to extract the orginal projection
    # codes from a coordsys object, so we need to figure out how to do this in
    # the most general way. The code below is still experimental.
    ctype = []
    for i, name in enumerate(names):
        if name in COORD_TYPE:
            ctype.append(COORD_TYPE[name])
            if name in direction_coords:
                ctype[-1] += ("%4s" % projection).replace(' ', '-')
        else:
            raise KeyError("Don't know how to convert: %s" % name)

    wcs.wcs.ctype = ctype

    return wcs


def load_casa_image(filename, skipdata=False,
                    skipvalid=False, skipcs=False, **kwargs):
    """
    Load a cube (into memory?) from a CASA image. By default it will transpose
    the cube into a 'python' order and drop degenerate axes. These options can
    be suppressed. The object holds the coordsys object from the image in
    memory.
    """

    if TASKINIT_INSTALLED:

        from taskinit import ia
        ia.open(filename)
        if not skipdata:
            data = ia.getchunk()
        if not skipvalid:
            valid = ia.getchunk(getmask=True)
        wcs = wcs_casa2astropy(ia.coordsys())
        unit = ia.brightnessunit()
        ia.close()

    elif CASACORE_INSTALLED:

        from casacore.images import image
        ia = image(filename)
        if not skipdata:
            data = ia.getdata()
        if not skipvalid:
            valid = ~ia.getmask()
        wcs = wcs_casa2astropy(ia.coordinates())
        unit = ia.unit()

        print(data.shape)

    else:

        raise Exception("Loading CASA images requires either CASA or the "
                        "python-casacore package to be installed")

    # don't need this yet
    # stokes = get_casa_axis(temp_cs, wanttype="Stokes", skipdeg=False,)

    #    if stokes == None:
    #        order = np.arange(self.data.ndim)
    #    else:
    #        order = []
    #        for ax in np.arange(self.data.ndim+1):
    #            if ax == stokes:
    #                continue
    #            order.append(ax)

    #    self.casa_cs = ia.coordsys(order)

    # This should work, but coordsys.reorder() has a bug
    # on the error checking. JIRA filed. Until then the
    # axes will be reversed from the original.

    # if transpose == True:
    #    new_order = np.arange(self.data.ndim)
    #    new_order = new_order[-1*np.arange(self.data.ndim)-1]
    #    print new_order
    #    self.casa_cs.reorder(new_order)

    meta = {'filename': filename,
            'BUNIT': unit}

    if wcs.naxis == 3:
        mask = BooleanArrayMask(np.logical_not(valid), wcs)
        cube = SpectralCube(data, wcs, mask, meta=meta)

    elif wcs.naxis == 4:
        data, wcs = cube_utils._split_stokes(data.T, wcs)
        mask = {}
        for component in data:
            data[component], wcs_slice = cube_utils._orient(data[component],
                                                            wcs)
            mask[component] = LazyMask(np.isfinite, data=data[component],
                                       wcs=wcs_slice)

        cube = StokesSpectralCube(data, wcs_slice, mask, meta=meta)

    return cube
