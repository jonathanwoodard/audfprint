"""
audfprint.py

Implementation of acoustic-landmark-based robust fingerprinting.
Port of the Matlab implementation.

2014-05-25 Dan Ellis dpwe@ee.columbia.edu
"""

import numpy as np
import matplotlib.pyplot as plt
import librosa
import scipy.signal

# Parameters
# DENSITY controls the density of landmarks found (approx DENSITY per sec)
DENSITY = 20.0
# OVERSAMP > 1 tries to generate extra landmarks by decaying faster
OVERSAMP = 1
# 512 pt FFT @ 11025 Hz, 50% hop
t_win = 0.0464
t_hop = 0.0232
# spectrogram enhancement
hpf_pole = 0.98
# masking envelope decay constant
a_dec = (1.0 - 0.01*(DENSITY*np.sqrt(t_hop/0.032)/35))**(1.0/OVERSAMP)
# how wide to spreak peaks
f_sd = 30.0
# Maximum number of local maxima to keep per frame
maxpksperframe = 5

# Values controlling peaks2landmarks
targetdf = 31   # +/- 50 bins in freq (LIMITED TO -32..31 IN LANDMARK2HASH)
targetdt = 63   # max lookahead in time (LIMITED TO <64 IN LANDMARK2HASH)
maxpairsperpeak=3  # Limit the number of pairs we'll make from each peak

def locmax(x, indices=False):
    """ Return a boolean vector of which points in x are local maxima.  
        End points are peaks if larger than single neighbors.
        if indices=True, return the indices of the True values instead 
        of the boolean vector. 
    """
    nbr = np.greater_equal(np.r_[x, x[-1]-1], np.r_[x[0], x])
    # x[-1]-1 means last value can be a peak
    maxmask = (nbr[:-1] & ~nbr[1:])
    if indices:
        return np.nonzero(maxmask)[0]
    else:
        return maxmask

def spreadpeaksinvector(vector, width=4.0):
    """ Create a blurred version of vector, where each of the local maxes 
        is spread by a gaussian with SD <width>.
    """
    npts = len(vector)
    peaks = locmax(vector, indices=True)
    return spreadpeaks(zip(peaks, vector[peaks]), npoints=npts, width=width)

def spreadpeaks(peaks, npoints=None, width=4.0, base=None):
    """ Generate a vector consisting of the max of a set of Gaussian bumps
    :params:
        peaks : list
          list of (index, value) pairs giving the center point and height of each gaussian
        npoints : int
          the length of the output vector (needed if base not provided)
        width : float
          the half-width of the Gaussians to lay down at each point
        base : np.array
          optional initial lower bound to place Gaussians above
    :returns:
        vector : np.array(npoints)
          the maximum across all the scaled Gaussians
    """
    if base is None:
        Y = np.zeros(npoints)
    else:
        Y = np.copy(base)
    binvals = np.arange(len(Y))
    for pos, val in peaks:
        Y = np.maximum(Y, val*np.exp(-0.5*(((binvals - pos)/float(width))**2)))
    return Y

def find_peaks(d, sr):
    """Find the local peaks in the spectrogram as basis for fingerprints.
       Returns a list of (time_frame, freq_bin) pairs.
    """
    # Base spectrogram
    n_fft = int(np.round(sr*t_win))
    n_hop = int(np.round(sr*t_hop))
    # Take spectrogram
    mywin = np.hanning(n_fft+2)[1:-1]
    S = np.abs(librosa.stft(d, n_fft=n_fft, hop_length=n_hop, window=mywin))
    S = np.log(np.maximum(S, np.max(S)/1e6))
    S = S - np.mean(S)
    # High-pass filter onset emphasis
    S = np.array([scipy.signal.lfilter([1, -1], 
                                       [1, -(hpf_pole)**(1/OVERSAMP)], Srow) 
                  for Srow in S])
    # initial threshold envelope based on peaks in first 10 frames
    (srows, scols) = np.shape(S)
    sthresh = spreadpeaksinvector(np.max(S[:,:np.minimum(10, scols)],axis=1), 
                                  f_sd)
    # Store sthresh at each column, for debug
    thr = np.zeros((srows, scols))
    peaks = np.zeros((srows, scols))
    for col in range(scols):
        Scol = S[:, col]
        sdiff = np.maximum(0, Scol - sthresh)
        sdmaxposs = np.nonzero(locmax(sdiff))[0]
        npeaksfound = 0
        # Work down list of peaks in order of their prominence above threshold 
        # (compatible with Matlab)
        #pkvals = Scol[sdmaxposs] - sthresh[sdmaxposs]   # for MATCOMPAT v0.90
        # Work down list of peaks in order of their absolute value above 
        # threshold (sensible)
        pkvals = Scol[sdmaxposs]
        for val, peakpos in sorted(zip(pkvals, sdmaxposs), reverse=True):
            if npeaksfound < maxpksperframe:
                if Scol[peakpos] > sthresh[peakpos]:
                    #print "frame:", col, " bin:", peakpos, " val:", Scol[peakpos], " thr:", sthresh[peakpos]
                    npeaksfound += 1
                    sthresh = spreadpeaks([(peakpos, Scol[peakpos])], 
                                          base=sthresh, width=f_sd)
                    peaks[peakpos, col] = 1
        thr[:, col] = sthresh
        sthresh *= a_dec

    # Backwards filter to prune peaks
    sthresh = spreadpeaksinvector(S[:,-1], f_sd)
    for col in range(scols, 0, -1):
        pkposs = np.nonzero(peaks[:, col-1])[0]
        peakvals = S[pkposs, col-1]
        for val, peakpos in sorted(zip(peakvals, pkposs)):
            if val >= sthresh[peakpos]:
                # Setup the threshold
                sthresh = spreadpeaks([(peakpos, val)], base=sthresh, 
                                      width=f_sd)
            else:
                # delete the peak
                peaks[peakpos, col-1] = 0
        sthresh = a_dec*sthresh

    # build a list of peaks
    pklist = [[] for _ in xrange(scols)]
    for col in xrange(scols):
        pklist[col] = np.nonzero(peaks[:,col])[0]
    return pklist

def peaks2landmarks(pklist):
    """ Take a list of local peaks in spectrogram
        and form them into pairs as landmarks.
        peaks[i] is a list of the fft bins identified as landmark peaks 
        for time frame i (which will be empty for many frames).
        Return a list of (col, peak, peak2, col2-col) landmark descriptors.
    """
    # Form pairs of peaks into landmarks
    scols = len(pklist)
    # Build list of landmarks <starttime F1 endtime F2>
    landmarks = []
    for col in xrange(scols):
        for peak in pklist[col]:
            pairsthispeak = 0
            for col2 in xrange(col+1, min(scols, col+targetdt)):
                for peak2 in pklist[col2]:
                    if ( (pairsthispeak < maxpairsperpeak)
                         and abs(peak-peak2) < targetdf ):
                        # We have a pair!
                        landmarks.append( (col, peak, peak2, col2-col) )
                        pairsthispeak += 1

    return landmarks

def landmarks2hashes(landmarks):
    """ Convert a list of (time, bin1, bin2, dtime) landmarks 
        into a list of (time, hash) pairs where the hash combines 
        the three remaining values.
    """
    F1bits = 8
    DFbits = 6
    DTbits = 6

    b1mask  = (1 << F1bits) - 1
    b1shift = DFbits + DTbits
    dfmask  = (1 << DFbits) - 1
    dfshift = DTbits
    dtmask  = (1 << DTbits) - 1

    # build up and return the list of hashed values
    return [ ( time, 
               ( ((bin1 & b1mask) << b1shift) 
                 | (((bin2 - bin1) & dfmask) << dfshift)
                 | (dtime & dtmask)) ) 
             for time, bin1, bin2, dtime in landmarks ]


test = True
if test:
    fn = '/Users/dpwe/Downloads/carol11k.wav'
    d, sr = librosa.load(fn, sr=11025)
    hashes = landmarks2hashes(peaks2landmarks(find_peaks(d[:30*sr], sr)))

    print len(hashes)
    for hash in hashes[:40]:
        print hash

    import hash_table
    ht = hash_table.HashTable()
    ht.store(fn, hashes)
    ht.save('httest.pickle', {'version': 20140525})

