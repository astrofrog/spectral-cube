from __future__ import print_function, absolute_import, division

import six
import warnings
import tempfile
import shutil
from astropy.io import fits
from astropy.wcs import WCS
from astropy import units as u
from astropy.wcs.wcsapi.sliced_low_level_wcs import sanitize_slices
from astropy import log
from astropy.io import registry as io_registry
import numpy as np
from radio_beam import Beam, Beams

import dask.array

from .. import SpectralCube, StokesSpectralCube, BooleanArrayMask, LazyMask, VaryingResolutionSpectralCube
from ..spectral_cube import BaseSpectralCube
from .. import cube_utils
from .. utils import BeamWarning, cached, StokesWarning
from .. import wcs_utils

# Read and write from a CASA image. This has a few
# complications. First, by default CASA does not return the
# "python order" and so we either have to transpose the cube on
# read or have dueling conventions. Second, CASA often has
# degenerate stokes axes present in unpredictable places (3rd or
# 4th without a clear expectation). We need to replicate these
# when writing but don't want them in memory. By default, try to
# yield the same array in memory that we would get from astropy.


def is_casa_image(origin, filepath, fileobj, *args, **kwargs):

    # See note before StringWrapper definition
    from .core import StringWrapper
    if len(args) > 0 and isinstance(args[0], StringWrapper):
        filepath = args[0].value

    return filepath is not None and filepath.lower().endswith('.image')


def wcs_casa2astropy(ia, coordsys):
    """
    Convert a casac.coordsys object into an astropy.wcs.WCS object
    """

    # Rather than try and parse the CASA coordsys ourselves, we delegate
    # to CASA by getting it to write out a FITS file and reading back in
    # using WCS

    tmpimagefile = tempfile.mktemp() + '.image'
    tmpfitsfile = tempfile.mktemp() + '.fits'
    ia.fromarray(outfile=tmpimagefile,
                 pixels=np.ones([1] * coordsys.naxes()),
                 csys=coordsys.torecord(), log=False)
    ia.done()

    ia.open(tmpimagefile)
    ia.tofits(tmpfitsfile, stokeslast=False)
    ia.done()

    return WCS(tmpfitsfile)


class ArraylikeCasaData:

    def __init__(self, filename, ia_kwargs={}):

        try:
            import casatools
            self.iatool = casatools.image
            tb = casatools.table()
        except ImportError:
            try:
                from taskinit import iatool, tbtool
                self.iatool = iatool
                tb = tbtool()
            except ImportError:
                raise ImportError("Could not import CASA (casac) and therefore cannot read CASA .image files")


        self.ia_kwargs = ia_kwargs

        self.filename = filename

        self._cache = {}

        log.debug("Creating ArrayLikeCasa object")

        # try to trick CASA into destroying the ia object
        def getshape():
            ia = self.iatool()
            # use the ia tool to get the file contents
            try:
                ia.open(self.filename, cache=False)
            except AssertionError as ex:
                if 'must be of cReqPath type' in str(ex):
                    raise IOError("File {0} not found.  Error was: {1}"
                                  .format(self.filename, str(ex)))
                else:
                    raise ex

            self.shape = tuple(ia.shape()[::-1])
            self.dtype = np.dtype(ia.pixeltype())

            ia.done()
            ia.close()

        getshape()

        self.ndim = len(self.shape)

        tb.open(self.filename)
        dminfo = tb.getdminfo()
        tb.done()

        # unclear if this is always the correct callspec!!!
        # (transpose requires this be backwards)
        self.chunksize = dminfo['*1']['SPEC']['DEFAULTTILESHAPE'][::-1]


        log.debug("Finished with initialization of ArrayLikeCasa object")



    def __getitem__(self, value):


        log.debug(f"Retrieving slice {value} from {self}")

        value = sanitize_slices(value[::-1], self.ndim)

        # several cases:
        # integer: just use an integer
        # slice starting w/number: start number
        # slice starting w/None: use -1
        blc = [(-1 if slc.start is None else slc.start)
               if hasattr(slc, 'start') else slc
               for slc in value]
        # slice ending w/number >= 1: end number -1 (CASA is end-inclusive)
        # slice ending w/zero: use zero, not -1.
        # slice ending w/negative: use it, but ?????
        # slice ending w/None: use -1
        trc = [((slc.stop-1 if slc.stop >= 1 else slc.stop)
                if slc.stop is not None else -1)
               if hasattr(slc, 'stop') else slc for slc in value]
        inc = [(slc.step or 1) if hasattr(slc, 'step') else 1 for slc in value]


        ia = self.iatool()
        # use the ia tool to get the file contents
        try:
            ia.open(self.filename, cache=False)
        except AssertionError as ex:
            if 'must be of cReqPath type' in str(ex):
                raise IOError("File {0} not found.  Error was: {1}"
                              .format(self.filename, str(ex)))
            else:
                raise ex

        log.debug(f'blc={blc}, trc={trc}, inc={inc}, kwargs={self.ia_kwargs}')
        data = ia.getchunk(blc=blc, trc=trc, inc=inc, **self.ia_kwargs)
        ia.done()
        ia.close()

        log.debug(f"Done retrieving slice {value} from {self}")

        # keep all sliced axes but drop all integer axes
        new_view = [slice(None) if isinstance(slc, slice) else 0
                    for slc in value]

        transposed_data = data[tuple(new_view)].transpose()

        log.debug(f"Done transposing data with view {new_view}")

        return transposed_data


def load_casa_image(filename, skipdata=False,
                    skipvalid=False, skipcs=False, target_cls=None, **kwargs):
    """
    Load a cube (into memory?) from a CASA image. By default it will transpose
    the cube into a 'python' order and drop degenerate axes. These options can
    be suppressed. The object holds the coordsys object from the image in
    memory.
    """

    from .core import StringWrapper
    if isinstance(filename, StringWrapper):
        filename = filename.value

    try:
        import casatools
        iatool = casatools.image
    except ImportError:
        try:
            from taskinit import iatool
        except ImportError:
            raise ImportError("Could not import CASA (casac) and therefore cannot read CASA .image files")

    ia = iatool()

    # use the ia tool to get the file contents
    try:
        ia.open(filename, cache=False)
    except AssertionError as ex:
        if 'must be of cReqPath type' in str(ex):
            raise IOError("File {0} not found.  Error was: {1}"
                          .format(filename, str(ex)))
        else:
            raise ex

    # read in the data
    if not skipdata:
        arrdata = ArraylikeCasaData(filename)
        # CASA data are apparently transposed.
        data = dask.array.from_array(arrdata,
                                     chunks=arrdata.chunksize,
                                     name=filename
                                    )

    # CASA stores validity of data as a mask
    if not skipvalid:
        boolarr = ArraylikeCasaData(filename, ia_kwargs={'getmask': True})
        valid = dask.array.from_array(boolarr, chunks=boolarr.chunksize,
                                      name=filename+".mask"
                                     )

    # transpose is dealt with within the cube object

    # read in coordinate system object
    casa_cs = ia.coordsys()

    unit = ia.brightnessunit()

    beam_ = ia.restoringbeam()

    ia.done()
    ia.close()

    wcs = wcs_casa2astropy(ia, casa_cs)

    del casa_cs
    del ia


    if 'major' in beam_:
        beam = Beam(major=u.Quantity(beam_['major']['value'], unit=beam_['major']['unit']),
                    minor=u.Quantity(beam_['minor']['value'], unit=beam_['minor']['unit']),
                    pa=u.Quantity(beam_['positionangle']['value'], unit=beam_['positionangle']['unit']),
                   )
    elif 'beams' in beam_:
        bdict = beam_['beams']
        if beam_['nStokes'] > 1:
            raise NotImplementedError()
        nbeams = len(bdict)
        assert nbeams == beam_['nChannels']
        stokesidx = '*0'

        majors = [u.Quantity(bdict['*{0}'.format(ii)][stokesidx]['major']['value'],
                             bdict['*{0}'.format(ii)][stokesidx]['major']['unit']) for ii in range(nbeams)]
        minors = [u.Quantity(bdict['*{0}'.format(ii)][stokesidx]['minor']['value'],
                             bdict['*{0}'.format(ii)][stokesidx]['minor']['unit']) for ii in range(nbeams)]
        pas = [u.Quantity(bdict['*{0}'.format(ii)][stokesidx]['positionangle']['value'],
                          bdict['*{0}'.format(ii)][stokesidx]['positionangle']['unit']) for ii in range(nbeams)]

        beams = Beams(major=u.Quantity(majors),
                      minor=u.Quantity(minors),
                      pa=u.Quantity(pas))
    else:
        warnings.warn("No beam information found in CASA image.",
                      BeamWarning)


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
        data, wcs_slice = cube_utils._orient(data, wcs)
        valid, _ = cube_utils._orient(valid, wcs)

        mask = BooleanArrayMask(valid, wcs_slice)
        if 'beam' in locals():
            cube = SpectralCube(data, wcs_slice, mask, meta=meta, beam=beam)
        elif 'beams' in locals():
            cube = VaryingResolutionSpectralCube(data, wcs_slice, mask, meta=meta, beams=beams)
        else:
            cube = SpectralCube(data, wcs_slice, mask, meta=meta)
        # with #592, this is no longer true
        # we've already loaded the cube into memory because of CASA
        # limitations, so there's no reason to disallow operations
        # cube.allow_huge_operations = True
        assert cube.mask.shape == cube.shape

    elif wcs.naxis == 4:
        valid, _ = cube_utils._split_stokes(valid, wcs)
        data, wcs = cube_utils._split_stokes(data, wcs)
        mask = {}
        for component in data:
            data_, wcs_slice = cube_utils._orient(data[component], wcs)
            valid_, _ = cube_utils._orient(valid[component], wcs)
            mask[component] = BooleanArrayMask(valid_, wcs_slice)

            if 'beam' in locals():
                data[component] = SpectralCube(data_, wcs_slice, mask[component],
                                               meta=meta, beam=beam)
            elif 'beams' in locals():
                data[component] = VaryingResolutionSpectralCube(data_,
                                                                wcs_slice,
                                                                mask[component],
                                                                meta=meta,
                                                                beams=beams)
            else:
                data[component] = SpectralCube(data_, wcs_slice, mask[component],
                                               meta=meta)

            data[component].allow_huge_operations = True


        cube = StokesSpectralCube(stokes_data=data)
        assert cube.I.mask.shape == cube.shape
        assert wcs_utils.check_equality(cube.I.mask._wcs, cube.wcs)
    else:
        raise ValueError("CASA image has {0} dimensions, and therefore "
                         "is not readable by spectral-cube.".format(wcs.naxis))

    from .core import normalize_cube_stokes
    return normalize_cube_stokes(cube, target_cls=target_cls)

def casa_image_array_reader(imagename):
    """
    Read a CASA image (a folder containing a ``table.f0_TSM0`` file) into a
    numpy array.
    """
    from casatools import table
    tb = table()

    # load the metadata from the image table
    tb.open(imagename)
    dminfo = tb.getdminfo()
    tb.close()

    from pprint import pprint
    pprint(dminfo)

    # chunkshape definse how the chunks (array subsets) are written to disk
    chunkshape = tuple(dminfo['*1']['SPEC']['DEFAULTTILESHAPE'])
    chunksize = np.product(chunkshape)
    # the total size defines the final output array size
    totalshape = dminfo['*1']['SPEC']['HYPERCUBES']['*1']['CubeShape']
    # the ratio between these tells you how many chunks must be combined
    # to create a final stack
    stacks = totalshape / chunkshape
    nchunks = np.product(totalshape) // np.product(chunkshape)

    img_fn = f'{imagename}/table.f0_TSM0'
    # each of the chunks is stored in order on disk in fortran-order
    chunks = [np.memmap(img_fn, dtype='float32', offset=ii*chunksize,
                        shape=chunkshape, order='F')
              for ii in range(nchunks)]

    from astropy.io import fits
    fits.writeto('chunk.fits', chunks[0])

    # with all of the chunks stored in the above list, we then need to concatenate
    # the resulting pieces into a final array
    # this process was arrived at empirically, but in short:
    # (1) stack the cubes along the last dimension first
    # (2) then stack along each dimension until you get to the first
    rslt = chunks
    rstacks = list(stacks)
    jj = 0
    while len(rstacks) > 0:
        rstacks.pop()
        kk = len(stacks) - jj - 1
        remaining_dims = rstacks
        if len(remaining_dims) == 0:
            assert kk == 0
            rslt = np.concatenate(rslt, 0)
        else:
            cut = np.product(remaining_dims)
            assert cut % 1 == 0
            cut = int(cut)
            rslt = [np.concatenate(rslt[ii::cut], kk) for ii in range(cut)]
        jj += 1

    # this alternative approach puts the chunks in their appropriate spots
    # but I haven't figured out a way to turn them into the correct full-sized
    # array.  You could do it by creating a full-sized array with a
    # rightly-sized memmap, or something like that, but... that's not what
    # we're trying to accomplish here.  I want an in-memory object that points
    # to the right things with the right shape, not a copy in memory or on disk
    #stacks = list(map(int, stacks))
    #chunk_inds = np.arange(np.product(stacks)).reshape(stacks, order='F')

    #def recursive_relist(x):
    #    if isinstance(x, list) or isinstance(x, np.ndarray) and len(x) > 0:
    #        return [recursive_relist(y) for y in x]
    #    else:
    #        return chunks[x]

    return rslt



def casa_image_dask_reader(imagename):
    """
    Read a CASA image (a folder containing a ``table.f0_TSM0`` file) into a
    numpy array.
    """
    from casatools import table
    tb = table()

    # load the metadata from the image table
    tb.open(imagename)
    dminfo = tb.getdminfo()
    tb.close()

    from pprint import pprint
    pprint(dminfo)

    # chunkshape definse how the chunks (array subsets) are written to disk
    chunkshape = tuple(dminfo['*1']['SPEC']['DEFAULTTILESHAPE'])
    chunksize = np.product(chunkshape)
    # the total size defines the final output array size
    totalshape = dminfo['*1']['SPEC']['HYPERCUBES']['*1']['CubeShape']
    # the ratio between these tells you how many chunks must be combined
    # to create a final stack
    stacks = totalshape // chunkshape
    nchunks = np.product(totalshape) // np.product(chunkshape)

    img_fn = f'{imagename}/table.f0_TSM0'
    # each of the chunks is stored in order on disk in fortran-order
    chunks = [np.memmap(img_fn, dtype='float32', offset=ii*chunksize,
                        shape=chunkshape, order='F')
              for ii in range(nchunks)]
    # chunks = [np.ones(chunkshape) * ii for ii in range(nchunks)]

    # for idim in list(range(len(stacks))):
    # for idim in [3, 0, 1]:
    for idim in list(range(len(stacks)))[::-1]:

        if stacks[idim] == 1:
            continue

        chunks_new = []
        for i in range(nchunks // stacks[idim]):
            print(i, i * stacks[idim], (i+1) * stacks[idim], len(chunks))
            sub = chunks[i * stacks[idim]:(i+1) * stacks[idim]]
            chunks_new.append(dask.array.concatenate(sub, axis=idim))
            # fits.writeto(f'chunk_dim{idim}_id{i}.fits', np.asarray(chunks[-1]))
        chunks = chunks_new
        nchunks //= stacks[idim]

        print([np.unique(np.asarray(c)) for c in chunks])

    print(nchunks, len(chunks))

    print(np.unique(np.asarray(chunks[0])))

    return chunks[0]
    # return dask.array.stack(chunks)

    # from astropy.io import fits
    # fits.writeto('chunk.fits', chunks[0])

    # # with all of the chunks stored in the above list, we then need to concatenate
    # # the resulting pieces into a final array
    # # this process was arrived at empirically, but in short:
    # # (1) stack the cubes along the last dimension first
    # # (2) then stack along each dimension until you get to the first
    # rslt = chunks
    # rstacks = list(stacks)
    # jj = 0
    # while len(rstacks) > 0:
    #     rstacks.pop()
    #     kk = len(stacks) - jj - 1
    #     remaining_dims = rstacks
    #     if len(remaining_dims) == 0:
    #         assert kk == 0
    #         rslt = np.concatenate(rslt, 0)
    #     else:
    #         cut = np.product(remaining_dims)
    #         assert cut % 1 == 0
    #         cut = int(cut)
    #         rslt = [np.concatenate(rslt[ii::cut], kk) for ii in range(cut)]
    #     jj += 1

    # this alternative approach puts the chunks in their appropriate spots
    # but I haven't figured out a way to turn them into the correct full-sized
    # array.  You could do it by creating a full-sized array with a
    # rightly-sized memmap, or something like that, but... that's not what
    # we're trying to accomplish here.  I want an in-memory object that points
    # to the right things with the right shape, not a copy in memory or on disk
    #stacks = list(map(int, stacks))
    #chunk_inds = np.arange(np.product(stacks)).reshape(stacks, order='F')

    #def recursive_relist(x):
    #    if isinstance(x, list) or isinstance(x, np.ndarray) and len(x) > 0:
    #        return [recursive_relist(y) for y in x]
    #    else:
    #        return chunks[x]

    return rslt



io_registry.register_reader('casa', BaseSpectralCube, load_casa_image)
io_registry.register_reader('casa_image', BaseSpectralCube, load_casa_image)
io_registry.register_identifier('casa', BaseSpectralCube, is_casa_image)

io_registry.register_reader('casa', StokesSpectralCube, load_casa_image)
io_registry.register_reader('casa_image', StokesSpectralCube, load_casa_image)
io_registry.register_identifier('casa', StokesSpectralCube, is_casa_image)
