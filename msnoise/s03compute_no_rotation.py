""" This code is responsible for the computation of the cross-correlation
functions.

This script will group *jobs* marked "T"odo in the database by day and process
them using the following scheme. As soon as one day is selected, the
corresponding jobs are marked "I"n Progress in the database. This allows
running several instances of this script in parallel.

Configuration Parameters
~~~~~~~~~~~~~~~~~~~~~~~~

* |cc_sampling_rate|
* |analysis_duration|
* |overlap|
* |maxlag|
* |corr_duration|
* |windsorizing|
* |resampling_method|
* |remove_response|
* |response_format|
* |response_path|
* |response_prefilt|
* |preprocess_lowpass|
* |preprocess_highpass|
* |keep_all|
* |keep_days|
* |stack_method|
* |pws_timegate|
* |pws_power|
* |whitening|  | *new in 1.5*

Waveform Pre-processing
~~~~~~~~~~~~~~~~~~~~~~~
Pairs are first split and a station list is created. The database is then
queried to get file paths. For each station, all files potentially containing
data for the day are opened. The traces are then merged and splitted, to obtain
the most continuous chunks possible. The different chunks are then demeaned,
tapered and merged again to a 1-day long trace. If a chunk is not aligned
on the sampling grid (that is, start at a integer times the sample spacing in s)
, the chunk is phase-shifted in the frequency domain. This requires tapering and
fft/ifft. If the gap between two chunks is small, compared to a currently
hard-coded value (10 samples), the gap is filled with interpolated values.
Larger gaps will not be filled with interpolated values.

.. warning::
    As from MSNoise 1.5, traces are no longer padded by or merged with 0s.

Each 1-day long trace is then low-passed (at ``preprocess_lowpass`` Hz),
high-passed (at ``preprocess_highpass`` Hz), then if needed,
decimated/downsampled. Decimation/Downsampling are configurable
(``resampling_method``) and users are advised testing Decimate. One advantage of
Downsampling over Decimation is that it is able to downsample the data by any
factor, not only integer factors. Downsampling can be achieved with the new
ObsPy Lanczos resampler, giving results similar to those by scikits.samplerate.

.. note:: Python 3 users will most probably struggle installing
    scikits.samplerate, and therefore will have to use either Decimate or
    Lanczos instead of Resample. This is not a problem because the Lanczos
    resampling gives results similar to those by scikits.samplerate.


If configured, each 1-day long trace is corrected for its instrument response.
Currently, only dataless seed and inventory XML are supported.

As from MSNoise 1.5, the preprocessing routine is separated from the compute_cc
and can be used by plugins with their own parameters. The routine returns a
Stream object containing all the traces for all the stations/components.

Processing
~~~~~~~~~~

Once all traces are preprocessed, station pairs are processed sequentially.
If a component different from *ZZ* is to be computed, the traces are first
rotated. This supposes the user has provided the station coordinates in the
*station* table. The rotation is computed for Radial and Transverse components.

Then, for each ``corr_duration`` window in the signal, and for each filter
configured in the database, the traces are clipped to ``windsorizing`` times
the RMS (or 1-bit converted) and then whitened in the frequency domain
(see :ref:`whiten`) between the frequency bounds. The whitening procedure can be
skipped by setting the ``whitening`` configuration to `None`. The two other
``whitening`` modes are "[A]ll except for auto-correlation" or "Only if
[C]omponents are different". This allows skipping the whitening when, for
example, computing ZZ components for very close by stations (much closer than
the wavelength sampled), leading to spatial autocorrelation issues.

When both traces are ready, the cross-correlation function is computed
(see :ref:`mycorr`). The function returned contains data for time lags
corresponding to ``maxlag`` in the acausal (negative lags) and causal
(positive lags) parts.

Stacking and Saving Results
~~~~~~~~~~~~~~~~~~~~~~~~~~~

If configured (setting ``keep_all`` to 'Y'), each ``corr_duration`` CCF is
saved to the hard disk. By default, the ``keep_days`` setting is set to True
and so "N = 1 day / corr_duration" CCF are stacked and saved to the hard disk
in the STACKS/001_DAYS folder.

.. note:: Currently, the keep-all data (every CCF) are not used by next steps.

If ``stack_method`` is 'linear', then a simple mean CCF of all windows is saved
as the daily CCF. On the other hand, if ``stack_method`` is 'pws', then
all the Phase Weighted Stack (PWS) is computed and saved as the daily CCF. The
PWS is done in two steps: first the mean coherence between the instataneous
phases of all windows is calculated, and eventually serves a weighting factor
on the mean. The smoothness of this weighting array is defined using the
``pws_timegate`` parameter in the configuration. The weighting array is the
power of the mean coherence array. If ``pws_power`` is equal to 0, a linear
stack is done (then it's faster to do set ``stack_method`` = 'linear'). Usual
value is 2.

.. warning:: PWS is largely untested, not cross-validated. It looks good, but
    that doesn't mean a lot, does it? Use with Caution! And if you
    cross-validate it, please let us know!!

    Schimmel, M. and Paulssen H., "Noise reduction and detection
    of weak, coherent signals through phase-weighted stacks". Geophysical
    Journal International 130, 2 (1997): 497-505.

Once done, each job is marked "D"one in the database.

To run this script:

.. code-block:: sh

    $ msnoise compute_cc


This step also supports parallel processing/threading:

.. code-block:: sh

    $ msnoise -t 4 compute_cc

will start 4 instances of the code (after 1 second delay to avoid database
conflicts). This works both with SQLite and MySQL but be aware problems
could occur with SQLite.


.. versionadded:: 1.4
    The Instrument Response removal & The Phase Weighted Stack &
    Parallel Processing

.. versionadded:: 1.5
    The Obspy Lanczos resampling method, gives similar results as the
    scikits.samplerate package, thus removing the requirement for it.
    This method is defined by default.

.. versionadded:: 1.5
    The preprocessing routine is separated from the compute_cc and can be called
    by external plugins.

"""
#TODO docstring
import sys
import time

import matplotlib.mlab as mlab

from .api import *
from .move2obspy import myCorr2
from .move2obspy import whiten2

from .preprocessing import preprocess

from scipy.stats import scoreatpercentile


def main():
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s [%(levelname)s] %(message)s',
                        datefmt='%Y-%m-%d %H:%M:%S')

    logging.info('*** Starting: Compute CC ***')

    # Connection to the DB
    db = connect()

    if len(get_filters(db, all=False)) == 0:
        logging.info("NO FILTERS DEFINED, exiting")
        sys.exit()

    # Get Configuration
    params = get_params(db)
    filters = get_filters(db, all=False)
    logging.info("Will compute %s" % " ".join(params.components_to_compute))

    if params.remove_response:
        logging.debug('Pre-loading all instrument response')
        responses = preload_instrument_responses(db)
    else:
        responses = None
    logging.info("Checking if there are jobs to do")
    while is_next_job(db, jobtype='CC'):
        logging.info("Getting the next job")
        jobs = get_next_job(db, jobtype='CC')

        if len(jobs) == 0:
            # edge case, should only occur when is_next returns true, but
            # get_next receives no jobs (heavily parallelised code)
            continue

        stations = []
        pairs = []
        refs = []

        for job in jobs:
            refs.append(job.ref)
            pairs.append(job.pair)
            netsta1, netsta2 = job.pair.split(':')
            stations.append(netsta1)
            stations.append(netsta2)
            goal_day = job.day

        stations = np.unique(stations)

        logging.info("New CC Job: %s (%i pairs with %i stations)" %
                     (goal_day, len(pairs), len(stations)))
        jt = time.time()

        comps = []
        for comp in params.components_to_compute:
            if comp[0] in ["R", "T"] or comp[1] in ["R", "T"]:
                comps.append("E")
                comps.append("N")
            else:
                comps.append(comp[0])
                comps.append(comp[1])
        comps = np.unique(comps)
        stream = preprocess(db, stations, comps, goal_day, params, responses)
        if not len(stream):
            logging.info("Not enough data for this day !")
            logging.info("Marking job Done and continuing with next !")
            for job in jobs:
                update_job(db, job.day, job.pair, 'CC', 'D', ref=job.ref)
            continue
        # print '##### STREAMS ARE ALL PREPARED AT goal Hz #####'
        dt = 1. / params.goal_sampling_rate
        logging.info("Starting slides")
        start_processing = time.time()
        allcorr = {}
        for tmp in stream.slide(params.corr_duration,
                                params.corr_duration * (1 - params.overlap)):
            logging.info("Processing %s - %s" % (tmp[0].stats.starttime,
                                                 tmp[0].stats.endtime))
            tmp = tmp.copy().sort()

            channels_to_remove = []
            for gap in tmp.get_gaps(min_gap=0):
                if gap[-2] > 0:
                    channels_to_remove.append(
                        ".".join([gap[0], gap[1], gap[2], gap[3]]))

            for chan in np.unique(channels_to_remove):
                logging.debug("%s contains gap(s), removing it" % chan)
                net, sta, loc, chan = chan.split(".")
                for tr in tmp.select(network=net,
                                     station=sta,
                                     location=loc,
                                     channel=chan):
                    tmp.remove(tr)
            if len(tmp) == 0:
                logging.debug("No traces without gaps")
                continue

            base = np.amax([tr.stats.npts for tr in tmp])
            if base <= (params.maxlag*params.goal_sampling_rate*2+1):
                logging.debug("All traces shorter are too short to export"
                              " +-maxlag")
                continue

            for tr in tmp:
                if tr.stats.npts != base:
                    tmp.remove(tr)
                    logging.debug("One trace is too short, removing it")

            if len(tmp) == 0:
                logging.debug("No traces left in slice")
                continue

            nfft = next_fast_len(tmp[0].stats.npts)
            tmp.detrend("demean")

            for tr in tmp:
                if params.windsorizing == -1:
                    np.sign(tr.data, tr.data)  # inplace
                elif params.windsorizing != 0:
                    imin, imax = scoreatpercentile(tr.data, [1, 99])
                    not_outliers = np.where((tr.data >= imin) &
                                            (tr.data <= imax))[0]
                    rms = tr.data[not_outliers].std() * params.windsorizing
                    np.clip(tr.data, -rms, rms, tr.data)  # inplace
            # TODO should not hardcode 4 percent!
            tmp.taper(0.04)

            # TODO should not hardcode 100 taper points in spectrum
            napod = 100

            data = np.asarray([tr.data for tr in tmp])
            names = [tr.id.split(".") for tr in tmp]

            # index net.sta comps for energy later
            channel_index = {}
            psds = []
            for i, name in enumerate(names):
                n1, s1, l1, c1 = name
                netsta = "%s.%s" % (n1, s1)
                if netsta not in channel_index:
                    channel_index[netsta] = {}
                channel_index[netsta][c1[-1]] = i

                pxx, freqs = mlab.psd(tmp[i].data,
                                      Fs=tmp[i].stats.sampling_rate,
                                      NFFT=nfft,
                                      detrend='mean')
                psds.append(np.sqrt(pxx))
            psds = np.asarray(psds)

            for chan in channel_index:
                comps = channel_index[chan].keys()
                if "E" in comps and "N" in comps:
                    i_e = channel_index[chan]["E"]
                    i_n = channel_index[chan]["N"]
                    # iZ = channel_index[chan]["Z"]
                    mm = psds[[i_e,i_n]].mean(axis=0)
                    psds[i_e] = mm
                    psds[i_n] = mm
                    # psds[iZ] = mm

            # define pairwise CCs
            tmptime = tmp[0].stats.starttime.datetime
            thisdate = tmptime.strftime("%Y-%m-%d")
            thistime = tmptime.strftime("%Y-%m-%d %H:%M:%S")
            pair_index = []

            # Different iterator func if autocorr:
            if params.autocorr:
                iterfunc = itertools.combinations_with_replacement
            else:
                iterfunc = itertools.combinations
            for sta1, sta2 in iterfunc(names, 2):
                n1, s1, l1, c1 = sta1
                n2, s2, l2, c2 = sta2
                comp = "%s%s" % (c1[-1], c2[-1])
                if comp in params.components_to_compute:
                    pair_index.append(
                        ["%s.%s_%s.%s_%s" % (n1, s1, n2, s2, comp),
                         names.index(sta1), names.index(sta2)])

            for filterdb in filters:
                filterid = filterdb.ref
                low = float(filterdb.low)
                high = float(filterdb.high)

                freq_vec = scipy.fftpack.fftfreq(nfft, d=dt)[:nfft // 2]
                freq_sel = np.where((freq_vec >= low) & (freq_vec <= high))[0]
                low = freq_sel[0] - napod
                if low <= 0:
                    low = 1
                p1 = freq_sel[0]
                p2 = freq_sel[-1]
                high = freq_sel[-1] + napod
                if high > nfft / 2:
                    high = int(nfft // 2)

                ffts = scipy.fftpack.fftn(data, shape=[nfft, ], axes=[1, ])
                # TODO: AC will require a more clever handling, no whiten...
                whiten2(ffts, nfft, low, high, p1, p2, psds,
                        params.whitening)  # inplace
                # energy = np.sqrt(np.sum(np.abs(ffts)**2, axis=1)/nfft)
                energy = np.real(np.sqrt( np.mean(scipy.fftpack.ifft(ffts, n=nfft, axis=1) ** 2, axis=1)))

                # logging.info("Pre-whitened %i traces"%(i+1))
                corr = myCorr2(ffts,
                               np.ceil(params.maxlag / dt),
                               energy,
                               pair_index,
                               plot=False,
                               nfft=nfft)

                for key in corr:
                    ccfid = key + "_%02i" % filterid + "_" + thisdate
                    if ccfid not in allcorr:
                        allcorr[ccfid] = {}
                    allcorr[ccfid][thistime] = corr[key]
                del corr

        # Needed to clean the FFT memory caching of SciPy
        clean_scipy_cache()

        if params.keep_all:
            for ccfid in allcorr.keys():
                export_allcorr2(db, ccfid, allcorr[ccfid])

        if params.keep_days:
            for ccfid in allcorr.keys():
                station1, station2, components, filterid, date = \
                    ccfid.split('_')

                corrs = np.asarray(list(allcorr[ccfid].values()))
                if not len(corrs):
                    logging.debug("No data to stack.")
                    continue
                corr = stack(corrs, params.stack_method, params.pws_timegate,
                             params.pws_power, params.goal_sampling_rate)
                if not len(corr):
                    logging.debug("No data to save.")
                    continue
                thisdate = goal_day
                thistime = "0_0"
                add_corr(
                    db, station1.replace('.', '_'),
                    station2.replace('.', '_'), int(filterid),
                    thisdate, thistime, params.min30 /
                                        params.goal_sampling_rate,
                    components, corr,
                    params.goal_sampling_rate, day=True,
                    ncorr=corrs.shape[0],
                    params=params)

        # THIS SHOULD BE IN THE API
        massive_update_job(db, jobs, "D")

        for job in jobs:
            update_job(db, job.day, job.pair, 'STACK', 'T')

        logging.info("Job Finished. It took %.2f seconds (preprocess: %.2f s & "
                     "process %.2f s)" % ((time.time() - jt),
                                          start_processing - jt,
                                          time.time() - start_processing))
        del stream
    logging.info('*** Finished: Compute CC ***')


