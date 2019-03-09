# -*- coding: utf-8 -*-
"""

Authors
-------
John Weaver <john.weaver.astro@gmail.com>


About
-----
Class function to handle potentially blended sources (i.e. blobs)

Known Issues
------------
None


"""


import os
import sys
import numpy as np
import matplotlib.pyplot as plt
from astropy.io import ascii, fits
from astropy.table import Table, Column
from tractor import NCircularGaussianPSF, PixelizedPSF, Image, Tractor, LinearPhotoCal, NullWCS, ConstantSky, GalaxyShape, Fluxes, Pointsource, ExpGalaxy, DevGalaxy, FixedCompositeGalaxy, SoftenedFracDev, PixPos
from time import time
import photutils
import sep

from .subimage import Subimage
from .utils import SimpleGalaxy
import config as conf


class Blob(Subimage):
    """TODO: docstring"""

    def __init__(self, brick, blob_id):
        """TODO: docstring"""

        blobmask = np.array(brick.blobmap == blob_id, bool)
        mask_frac = blobmask.sum() / blobmask.size
        if (mask_frac > conf.SPARSE_THRESH) & (blobmask.size > conf.SPARSE_SIZE):
            print('Blob is rejected as mask is sparse - likely an artefact issue.')
            blob = None

        self.brick = brick

        # Grab blob
        self.blob_id = blob_id
        blobmask = np.array(brick.blobmap == self.blob_id, bool)
        blob_sources = np.unique(brick.segmap[blobmask])

        # Dimensions
        idx, idy = blobmask.nonzero()
        xlo, xhi = np.min(idx), np.max(idx) + 1
        ylo, yhi = np.min(idy), np.max(idy) + 1
        w = xhi - xlo
        h = yhi - ylo

        # Make cutout
        blob_comps = brick._get_subimage(xlo, ylo, w, h, buffer=conf.BLOB_BUFFER)
        # FIXME: too many return values
        self.images, self.weights, self.masks, self.psfmodels, self.bands, self.wcs, self.subvector, self.slicepix, self.slice = blob_comps

        self.masks[self.slicepix] = np.logical_not(blobmask[self.slice], dtype=bool)
        self.segmap = brick.segmap[self.slice]

        # Clean
        blob_sourcemask = np.in1d(brick.catalog['sid'], blob_sources)
        self.catalog = brick.catalog[blob_sourcemask]
        self.catalog['x'] -= self.subvector[1]
        self.catalog['y'] -= self.subvector[0]
        self.n_sources = len(self.catalog)

        self.mids = np.ones(self.n_sources, dtype=int)
        self.model_catalog = np.zeros(self.n_sources, dtype=object)
        self.solution_catalog = np.zeros(self.n_sources, dtype=object)
        self.solved_chisq = np.zeros(self.n_sources)
        self.tr_catalogs = np.zeros((self.n_sources, 3, 2), dtype=object)
        self.chisq = np.zeros((self.n_sources, 3, 2))
        self.position_variance = np.zeros((self.n_sources, 2))
        self.parameter_variance = np.zeros((self.n_sources, 3))
        self.forced_variance = np.zeros((self.n_sources, self.n_bands))

        self.residual_catalog = np.zeros((self.n_bands), dtype=object)
        self.residual_segmap = np.zeros_like(self.segmap)
        self.n_residual_sources = np.zeros(self.n_bands, dtype=int)

        # TODO NEED TO LOOK AT OLD SCRIPT FOR AN IDEA ABOUT WHAT COMPOSITE SPITS OUT!!!

    def stage_images(self):
        """TODO: docstring"""

        timages = np.zeros(self.n_bands, dtype=object)

        # TODO: try to simplify this. particularly the zip...
        for i, (image, weight, mask, psf, band) in enumerate(zip(self.images, self.weights, self.masks, self.psfmodels, self.bands)):
            tweight = weight.copy()
            tweight[mask] = 0

            if psf is -99:
                psfmodel = NCircularGaussianPSF([2,], [1,])
            else:
                psfmodel = PixelizedPSF(psf)

            timages[i] = Image(data=image,
                            invvar=tweight,
                            psf=psfmodel,
                            wcs=NullWCS(),
                            photocal=LinearPhotoCal(1, band),
                            sky=ConstantSky(0.))

        self.timages = timages

    def stage_models(self):
        # Currently this makes NEW models for each trial. Do we want to freeze solution models and re-use them?

        # Trackers

        for i, (mid, src) in enumerate(zip(self.mids, self.catalog)):

            freeze_position = (self.mids >= 2).all()
            if freeze_position:
                position = self.tr_catalogs[i,0,0].getPosition()
            else:
                position = PixPos(src['x'], src['y'])
            flux = Fluxes(**dict(zip(self.bands, src['flux'] * np.ones(self.n_bands))))

            shape = GalaxyShape(1, src['b'] / src['a'], src['theta'])

            if mid == 1:
                self.model_catalog[i] = PointSource(position, flux)
            elif mid == 2:
                self.model_catalog[i] = SimpleGalaxy(position, flux)
            elif mid == 3:
                self.model_catalog[i] = ExpGalaxy(position, flux, shape)
            elif mid == 4:
                self.model_catalog[i] = DevGalaxy(position, flux, shape)
            elif mid == 5:
                self.model_catalog[i] = FixedCompositeGalaxy(
                                                position, flux,
                                                SoftenedFracDev(0.5),
                                                shape, shape)
            if freeze_position:
                self.model_catalog[i].freezeParams('pos')

    def tractor_phot(self):

        # TODO: The meaning of the following line is not clear
        idx_models = ((1, 2), (3, 4), (5,))

        self._solved = self.solution_catalog != 0

        self._level = -1

        while not self._solved.all():
            self._level += 1
            for sublevel in np.arange(len(idx_models[self._level])):
                self._sublevel = sublevel

                self.stage = f'Morph Model ({self._level}, {self._sublevel})'

                # prepare models
                self.mids[~self._solved] = idx_models[self._level][sublevel]
                self.stage_models()

                # store
                self.tr = Tractor(self.timages, self.model_catalog)

                # optimize
                self.status = self.optimize_tractor()

                if self.status == False:
                    return False

                # clean up
                self.tr_catalogs[:, self._level, self._sublevel] = self.tr.getCatalog()

                if (self._level == 0) & (self._sublevel == 0):
                    self.position_variance = np.array([self.variance[i][:2] for i in np.arange(self.n_sources)]) # THIS MAY JUST WORK!
                    # print(f'POSITION VAR: {self.position_variance}')

                for i, src in enumerate(self.catalog):
                    if self._solved[i]:
                        continue
                    totalchisq = np.sum((self.tr.getChiImage(0)[self.segmap == src['sid']])**2)
                    self.chisq[i, self._level, self._sublevel] = totalchisq

                # Move unsolved to next sublevel
                if sublevel == 0:
                    self.mids[~self._solved] += 1

            # decide
            self.decide_winners()
            self._solved = self.solution_catalog != 0

        # print('Starting final optimization')
        # Final optimization
        self.model_catalog = self.solution_catalog
        self.tr = Tractor(self.timages, self.model_catalog)

        self.stage = 'Final Optimization'
        self.status = self.optimize_tractor()

        self.solution_tractor = self.tr
        self.solution_catalog = self.tr.getCatalog()
        self.parameter_variance = [self.variance[i][self.n_bands:] for i in np.arange(self.n_sources)]
        # print(f'PARAMETER VAR: {self.parameter_variance}')

        for i, src in enumerate(self.catalog):
            totalchisq = np.sum((self.tr.getChiImage(0)[self.segmap == src['sid']])**2)
            self.solved_chisq[i] = totalchisq

        return True

    def decide_winners(self):
        # take the model_catalog and chisq and figure out what's what
        # Only look at unsolved models!

        # holders - or else it's pure insanity.
        chisq = self.chisq[~self._solved]
        solution_catalog = self.solution_catalog[~self._solved]
        solved_chisq = self.solved_chisq[~self._solved]
        tr_catalogs = self.tr_catalogs[~self._solved]
        mids = self.mids[~self._solved]

        if self._level == 0:
            # Which have chi2(PS) < chi2(SG)?
            chmask = (chisq[:, 0, 0] < chisq[:, 0, 1])
            if chmask.any():
                solution_catalog[chmask] = tr_catalogs[chmask, 0, 0].copy()
                solved_chisq[chmask] = chisq[chmask, 0, 0]
                mids[chmask] = 1

            # So chi2(SG) is min, try more models
            mids[~chmask] = 3

        if self._level == 1:
            # For which are they nearly equally good?
            movemask = (abs(chisq[:, 1, 0] - chisq[:, 1, 1]) < conf.EXP_DEV_THRESH)

            # Has Exp beaten SG?
            expmask = (chisq[:, 1, 0] < chisq[:, 0, 1])

            # Has Dev beaten SG?
            devmask = (chisq[:, 1, 1] < chisq[:, 0, 1])

            # Which better than SG but nearly equally good?
            nextmask = expmask & devmask & movemask

            # For which was SG better
            premask = ~expmask & ~devmask

            # If Exp beats Dev by a lot
            nexpmask = expmask & ~movemask & (chisq[:, 1, 0] < chisq[:, 1, 1])

             # If Dev beats Exp by a lot
            ndevmask = devmask & ~movemask & (chisq[:, 1, 1] < chisq[:, 1, 0])

            if nextmask.any():
                mids[nextmask] = 5

            if premask.any():
                solution_catalog[premask] = tr_catalogs[premask, 0, 1].copy()
                solved_chisq[premask] = chisq[premask, 0, 1]
                mids[premask] = 2

            if nexpmask.any():

                solution_catalog[nexpmask] = tr_catalogs[nexpmask, 1, 0].copy()
                solved_chisq[nexpmask] = chisq[nexpmask, 1, 0]
                mids[nexpmask] = 3

            if ndevmask.any():

                solution_catalog[ndevmask] = tr_catalogs[ndevmask, 1, 1].copy()
                solved_chisq[ndevmask] = chisq[ndevmask, 1, 1]
                mids[ndevmask] = 4

        if self._level == 2:
            # For which did Comp beat EXP and DEV?
            compmask = (chisq[:, 2, 0] < chisq[:, 1, 0]) &\
                       (chisq[:, 2, 0] < chisq[:, 1, 1])

            if compmask.any():
                solution_catalog[compmask] = tr_catalogs[compmask, 2, 0].copy()
                solved_chisq[compmask] = chisq[compmask, 2, 0]
                mids[compmask] = 5

            # where better as EXP or DEV
            if (~compmask).any():
                ch_exp = (chisq[:, 1, 0] < chisq[:, 1, 1]) & ~compmask

                if ch_exp.any():
                    solution_catalog[ch_exp] = tr_catalogs[ch_exp, 1, 0].copy()
                    solved_chisq[ch_exp] = chisq[ch_exp, 1, 0]
                    mids[ch_exp] = 3

                ch_dev = (chisq[:, 1, 1] < chisq[:, 1, 0]) & ~compmask

                if ch_dev.any():
                    solution_catalog[ch_dev] = tr_catalogs[ch_dev, 1, 1].copy()
                    solved_chisq[ch_dev] = chisq[ch_dev, 1, 1]
                    mids[ch_dev] = 4

        # hand back
        self.chisq[~self._solved] = chisq
        self.solution_catalog[~self._solved] = solution_catalog
        self.solved_chisq[~self._solved] = solved_chisq
        self.mids[~self._solved] = mids

    def optimize_tractor(self, tr=None):

        if tr is None:
            tr = self.tr

        tr.freezeParams('images')
        #tr.thawAllParams()

        start = time()
        for i in range(conf.TRACTOR_MAXSTEPS):
            try:
                dlnp, X, alpha, var = tr.optimize(variance=True)
            except:
                print('FAILED')
                return False

            if dlnp < conf.TRACTOR_CONTHRESH:
                break

        # print()
        # print(f'RAW VAR HAS {len(var)} VALUES')
        # print(var)
        if (self.solution_catalog != 0).all():
            # print('CHANGING TO SOLUTION CATALOG FOR PARAMETERS')
            var_catalog = self.solution_catalog
        else:
            var_catalog = self.model_catalog

        self.variance = []
        counter = 0
        for i, src in enumerate(np.arange(self.n_sources)):
            n_params = var_catalog[i].numberOfParams()
            myvar = var[counter: n_params + counter]
            # print(f'{i}) {var_catalog[i].name} has {n_params} params and {len(myvar)} variances: {myvar}')
            counter += n_params
            self.variance.append(myvar)

        expvar = np.sum([var_catalog[i].numberOfParams() for i in np.arange(len(var_catalog))])
        # print(f'I have {len(var)} variance parameters for {self.n_sources} sources. I expected {expvar}.')
        for i, mod in enumerate(var_catalog):
            totalchisq = np.sum((self.tr.getChiImage(0)[self.segmap == self.catalog[i]['sid']])**2)

        return True

    def aperture_phot(self, band, image_type=None, sub_background=False):
        # Allow user to enter image (i.e. image, residual, model...)

        if image_type not in ('image', 'model', 'residual'):
            raise TypeError("image_type must be 'image', 'model' or 'residual'")
            return

        idx = self._band2idx(band)

        if image_type == 'image':
            image = self.images[idx]

        elif image_type == 'model':
            image = self.solution_tractor.getModelImage(idx)

        elif image_type == 'residual':
            image = self.images[idx] - self.solution_tractor.getModelImage(idx)

        background = self.backgrounds[idx]

        if (self.weights == 1).all():
            # No weight given - kinda
            var = None
            thresh = conf.RES_THRESH * background.globalrms
            if not sub_background:
                thresh += background.globalback

        else:
            thresh = conf.RES_THRESH
            tweight = self.weights[idx].copy()
            tweight[self.masks[idx]] = 0  # Well this isn't going to go well.
            var = 1. / tweight # TODO: WRITE TO UTILS

        if sub_background:
            image -= background.back()

        cat = self.solution_catalog
        xxyy = np.vstack([src.getPosition() for src in cat]).T
        apxy = xxyy - 1.

        apertures_arcsec = np.array(conf.APER_PHOT)
        apertures = apertures_arcsec / self.pixel_scale

        apflux = np.zeros((len(cat), len(apertures)), np.float32)
        apflux_err = np.zeros((len(cat), len(apertures)), np.float32)

        H,W = image.shape
        Iap = np.flatnonzero((apxy[0,:] >= 0)   * (apxy[1,:] >= 0) *
                            (apxy[0,:] <= W-1) * (apxy[1,:] <= H-1))

        for i, rad in enumerate(apertures):
            aper = photutils.CircularAperture(apxy[:,Iap], rad)
            p = photutils.aperture_photometry(image, aper, error=np.sqrt(var))
            apflux[:, i] = p.field('aperture_sum')
            apflux_err[:, i] = p.field('aperture_sum_err')

        band = band.replace(' ', '_')
        if f'aperphot_{band}_{image_type}' not in self.brick.catalog.colnames:
            self.brick.catalog.add_column(Column(np.zeros(len(self.brick.catalog), dtype=(float, len(apertures))), name=f'aperphot_{band}_{image_type}'))
            self.brick.catalog.add_column(Column(np.zeros(len(self.brick.catalog), dtype=(float, len(apertures))), name=f'aperphot_{band}_{image_type}_err'))

        for idx, src in enumerate(self.solution_catalog):
            sid = self.catalog['sid'][idx]
            row = np.argwhere(self.brick.catalog['sid'] == sid)[0][0]
            self.brick.catalog[row][f'aperphot_{band}_{image_type}'] = tuple(apflux[idx])
            self.brick.catalog[row][f'aperphot_{band}_{image_type}_err'] = tuple(apflux_err[idx])


    def sextract_phot(self, band, sub_background=False):
        # SHOULD WE STACK THE RESIDUALS? (No?)
        # SHOULD WE DO THIS ON THE DETECTION IMAGE TOO? (I suppose we can already...!)
        idx = self._band2idx(band)
        residual = self.images[idx] - self.solution_tractor.getModelImage(idx)
        tweight = self.weights[idx].copy()
        tweight[self.masks[idx]] = 0 # Well this isn't going to go well.
        var = 1. / tweight # TODO: WRITE TO UTILS
        background = self.backgrounds[idx]

        if (self.weights == 1).all():
            # No weight given - kinda
            var = None
            thresh = conf.RES_THRESH * background.globalrms
            if not sub_background:
                thresh += background.globalback

        else:
            thresh = conf.RES_THRESH

        if sub_background:
            residual -= background.back()

        kwargs = dict(var=var, minarea=conf.RES_MINAREA, segmentation_map=True, deblend_nthresh=conf.RES_DEBLEND_NTHRESH, deblend_cont=conf.RES_DEBLEND_CONT)
        catalog, segmap = sep.extract(residual, thresh, **kwargs)

        if len(catalog) != 0:
            catalog = Table(catalog)
            self.residual_catalog[idx] = catalog
            n_residual_sources = len(catalog)
            self.residual_segmap = segmap
            self.n_residual_sources[idx] = n_residual_sources
            print(f'SExtractor Found {n_residual_sources} in {band} residual!')

            if f'{band}_n_residual_sources' not in self.brick.catalog.colnames:
                self.brick.catalog.add_column(Column(np.zeros(len(self.brick.catalog), dtype=bool), name=f'{band}_n_residual_sources'))

            for idx, src in enumerate(self.solution_catalog):
                sid = self.catalog['sid'][idx]
                row = np.argwhere(self.brick.catalog['sid'] == sid)[0][0]
                self.brick.catalog[row][f'{band}_n_residual_sources'] = True

            return catalog, segmap
        else:
            print('No objects found by SExtractor.')


        pass

    def forced_phot(self):

        # print('Starting forced photometry')
        # Update the incoming models
        for i, model in enumerate(self.model_catalog):
            model.brightness = Fluxes(**dict(zip(self.bands, model.brightness[0] * np.ones(self.n_bands))))
            model.freezeAllBut('brightness')

        # Stash in Tractor
        self.tr = Tractor(self.timages, self.model_catalog)
        self.stage = 'Forced Photometry'

        # Optimize
        status = self.optimize_tractor()

        # Chisq
        self.forced_variance = self.variance
        # print(f'FLUX VAR: {self.forced_variance}')
        self.solution_chisq = np.zeros((self.n_sources, self.n_bands))
        for i, src in enumerate(self.catalog):
            for j, band in enumerate(self.bands):
                totalchisq = np.sum((self.tr.getChiImage(j)[self.segmap == src['sid']])**2)# / (np.sum(self.segmap == src['sid']) - self.model_catalog[i].numberOfParams())
                ### PENALTIES ARE TBD
                # residual = self.images[j] - self.tr.getModelImage(j)
                # if np.median(residual[self.masks[j]]) < 0:
                #     totalchisq = 1E30
                self.solution_chisq[i, j] = totalchisq

        self.solution_tractor = Tractor(self.timages, self.tr.getCatalog())
        self.solution_catalog = self.solution_tractor.getCatalog()

        for idx, src in enumerate(self.solution_catalog):
            sid = self.catalog['sid'][idx]
            row = np.argwhere(self.brick.catalog['sid'] == sid)[0][0]
            # print(f'STASHING {sid} IN ROW {row}')
            self.add_to_catalog(row, idx, src)

        return status

    def add_to_catalog(self, row, idx, src):
        for i, band in enumerate(self.bands):
            band = band.replace(' ', '_')
            self.brick.catalog[row][band] = src.brightness[i]
            self.brick.catalog[row][band+'_err'] = np.sqrt(self.forced_variance[idx][i])
            self.brick.catalog[row][band+'_chisq'] = self.solution_chisq[idx, i]
        self.brick.catalog[row]['x_model'] = src.pos[0] + self.subvector[0]
        self.brick.catalog[row]['y_model'] = src.pos[1] + self.subvector[1]
        self.brick.catalog[row]['x_model_err'] = np.sqrt(self.position_variance[idx, 0])
        self.brick.catalog[row]['y_model_err'] = np.sqrt(self.position_variance[idx, 1])
        skyc = self._wcs.pixel_to_world(src.pos[0] + self.subvector[0], src.pos[1] + self.subvector[1])
        self.brick.catalog[row]['RA'] = skyc.ra.value
        self.brick.catalog[row]['Dec'] = skyc.dec.value
        try:
            self.brick.catalog[row]['solmodel'] = src.name
        except:
            self.brick.catalog[row]['solmodel'] = 'maybe_PS'
        if src.name in ('ExpGalaxy', 'DevGalaxy', 'CompositeGalaxy'):
            self.brick.catalog[row]['reff'] = src.shape.re
            self.brick.catalog[row]['ab'] = src.shape.ab
            self.brick.catalog[row]['phi'] = src.shape.phi
            self.brick.catalog[row]['reff_err'] = np.sqrt(self.parameter_variance[idx][0])
            self.brick.catalog[row]['ab_err'] = np.sqrt(self.parameter_variance[idx][1])
            self.brick.catalog[row]['phi_err'] = np.sqrt(self.parameter_variance[idx][2])


        # add model chisq, band chisqs, ra, dec
