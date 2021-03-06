import os.path as op
import numpy as np
from nose.tools import assert_true, assert_equal, assert_raises
from numpy.testing import assert_allclose
import warnings

from mne import (read_dipole, read_forward_solution,
                 convert_forward_solution, read_evokeds, read_cov,
                 SourceEstimate, write_evokeds, fit_dipole,
                 transform_surface_to, make_sphere_model, pick_types,
                 pick_info, EvokedArray)
from mne.simulation import generate_evoked
from mne.datasets import testing
from mne.utils import (run_tests_if_main, _TempDir, slow_test, requires_mne,
                       run_subprocess, requires_sklearn)
from mne.proj import make_eeg_average_ref_proj

from mne.io import Raw

from mne.surface import _bem_find_surface, _compute_nearest, read_bem_solution
from mne.transforms import (read_trans, apply_trans, _get_mri_head_t)

warnings.simplefilter('always')
data_path = testing.data_path(download=False)
fname_dip = op.join(data_path, 'MEG', 'sample', 'sample_audvis_trunc_set1.dip')
fname_evo = op.join(data_path, 'MEG', 'sample', 'sample_audvis_trunc-ave.fif')
fname_cov = op.join(data_path, 'MEG', 'sample', 'sample_audvis_trunc-cov.fif')
fname_bem = op.join(data_path, 'subjects', 'sample', 'bem',
                    'sample-1280-1280-1280-bem-sol.fif')
fname_fwd = op.join(data_path, 'MEG', 'sample',
                    'sample_audvis_trunc-meg-eeg-oct-6-fwd.fif')


def _compare_dipoles(orig, new):
    """Compare dipole results for equivalence"""
    assert_allclose(orig.times, new.times, atol=1e-3, err_msg='times')
    assert_allclose(orig.pos, new.pos, err_msg='pos')
    assert_allclose(orig.amplitude, new.amplitude, err_msg='amplitude')
    assert_allclose(orig.gof, new.gof, err_msg='gof')
    assert_allclose(orig.ori, new.ori, rtol=1e-4, atol=1e-4, err_msg='ori')
    assert_equal(orig.name, new.name)


def _check_dipole(dip, n_dipoles):
    assert_equal(len(dip), n_dipoles)
    assert_equal(dip.pos.shape, (n_dipoles, 3))
    assert_equal(dip.ori.shape, (n_dipoles, 3))
    assert_equal(dip.gof.shape, (n_dipoles,))
    assert_equal(dip.amplitude.shape, (n_dipoles,))


@testing.requires_testing_data
def test_io_dipoles():
    """Test IO for .dip files
    """
    tempdir = _TempDir()
    dipole = read_dipole(fname_dip)
    print(dipole)  # test repr
    out_fname = op.join(tempdir, 'temp.dip')
    dipole.save(out_fname)
    dipole_new = read_dipole(out_fname)
    _compare_dipoles(dipole, dipole_new)


@slow_test
@testing.requires_testing_data
@requires_mne
def test_dipole_fitting():
    """Test dipole fitting"""
    amp = 10e-9
    tempdir = _TempDir()
    rng = np.random.RandomState(0)
    fname_dtemp = op.join(tempdir, 'test.dip')
    fname_sim = op.join(tempdir, 'test-ave.fif')
    fwd = convert_forward_solution(read_forward_solution(fname_fwd),
                                   surf_ori=False, force_fixed=True)
    evoked = read_evokeds(fname_evo)[0]
    cov = read_cov(fname_cov)
    n_per_hemi = 5
    vertices = [np.sort(rng.permutation(s['vertno'])[:n_per_hemi])
                for s in fwd['src']]
    nv = sum(len(v) for v in vertices)
    stc = SourceEstimate(amp * np.eye(nv), vertices, 0, 0.001)
    with warnings.catch_warnings(record=True):  # semi-def cov
        evoked = generate_evoked(fwd, stc, evoked, cov, snr=20,
                                 random_state=rng)
    # For speed, let's use a subset of channels (strange but works)
    picks = np.sort(np.concatenate([
        pick_types(evoked.info, meg=True, eeg=False)[::2],
        pick_types(evoked.info, meg=False, eeg=True)[::2]]))
    evoked.pick_channels([evoked.ch_names[p] for p in picks])
    evoked.add_proj(make_eeg_average_ref_proj(evoked.info))
    write_evokeds(fname_sim, evoked)

    # Run MNE-C version
    run_subprocess([
        'mne_dipole_fit', '--meas', fname_sim, '--meg', '--eeg',
        '--noise', fname_cov, '--dip', fname_dtemp,
        '--mri', fname_fwd, '--reg', '0', '--tmin', '0',
    ])
    dip_c = read_dipole(fname_dtemp)

    # Run mne-python version
    sphere = make_sphere_model(head_radius=0.1)
    dip, residuals = fit_dipole(evoked, fname_cov, sphere, fname_fwd)

    # Sanity check: do our residuals have less power than orig data?
    data_rms = np.sqrt(np.sum(evoked.data ** 2, axis=0))
    resi_rms = np.sqrt(np.sum(residuals ** 2, axis=0))
    assert_true((data_rms > resi_rms).all())

    # Compare to original points
    transform_surface_to(fwd['src'][0], 'head', fwd['mri_head_t'])
    transform_surface_to(fwd['src'][1], 'head', fwd['mri_head_t'])
    src_rr = np.concatenate([s['rr'][v] for s, v in zip(fwd['src'], vertices)],
                            axis=0)
    src_nn = np.concatenate([s['nn'][v] for s, v in zip(fwd['src'], vertices)],
                            axis=0)

    # MNE-C skips the last "time" point :(
    dip.crop(dip_c.times[0], dip_c.times[-1])
    src_rr, src_nn = src_rr[:-1], src_nn[:-1]

    # check that we did at least as well
    corrs, dists, gc_dists, amp_errs, gofs = [], [], [], [], []
    for d in (dip_c, dip):
        new = d.pos
        diffs = new - src_rr
        corrs += [np.corrcoef(src_rr.ravel(), new.ravel())[0, 1]]
        dists += [np.sqrt(np.mean(np.sum(diffs * diffs, axis=1)))]
        gc_dists += [180 / np.pi * np.mean(np.arccos(np.sum(src_nn * d.ori,
                                                     axis=1)))]
        amp_errs += [np.sqrt(np.mean((amp - d.amplitude) ** 2))]
        gofs += [np.mean(d.gof)]
    assert_true(dists[0] >= dists[1], 'dists: %s' % dists)
    assert_true(corrs[0] <= corrs[1], 'corrs: %s' % corrs)
    assert_true(gc_dists[0] >= gc_dists[1], 'gc-dists (ori): %s' % gc_dists)
    assert_true(amp_errs[0] >= amp_errs[1], 'amplitude errors: %s' % amp_errs)
    # assert_true(gofs[0] <= gofs[1], 'gof: %s' % gofs)


@testing.requires_testing_data
def test_len_index_dipoles():
    """Test len and indexing of Dipole objects
    """
    dipole = read_dipole(fname_dip)
    d0 = dipole[0]
    d1 = dipole[:1]
    _check_dipole(d0, 1)
    _check_dipole(d1, 1)
    _compare_dipoles(d0, d1)
    mask = dipole.gof > 15
    idx = np.where(mask)[0]
    d_mask = dipole[mask]
    _check_dipole(d_mask, 4)
    _compare_dipoles(d_mask, dipole[idx])


@requires_sklearn
@testing.requires_testing_data
def test_min_distance_fit_dipole():
    """Test dipole min_dist to inner_skull"""
    data_path = testing.data_path()
    raw_fname = data_path + '/MEG/sample/sample_audvis_trunc_raw.fif'

    subjects_dir = op.join(data_path, 'subjects')
    fname_cov = op.join(data_path, 'MEG', 'sample', 'sample_audvis-cov.fif')
    fname_trans = op.join(data_path, 'MEG', 'sample',
                          'sample_audvis_trunc-trans.fif')
    fname_bem = op.join(subjects_dir, 'sample', 'bem',
                        'sample-1280-1280-1280-bem-sol.fif')

    subject = 'sample'

    raw = Raw(raw_fname, preload=True)

    # select eeg data
    picks = pick_types(raw.info, meg=False, eeg=True, exclude='bads')
    info = pick_info(raw.info, picks)

    # Let's use cov = Identity
    cov = read_cov(fname_cov)
    cov['data'] = np.eye(cov['data'].shape[0])

    # Simulated scal map
    simulated_scalp_map = np.zeros(picks.shape[0])
    simulated_scalp_map[27:34] = 1

    simulated_scalp_map = simulated_scalp_map[:, None]

    evoked = EvokedArray(simulated_scalp_map, info, tmin=0)

    min_dist = 5.  # distance in mm

    dip, residual = fit_dipole(evoked, cov, fname_bem, fname_trans,
                               min_dist=min_dist)

    dist = _compute_depth(dip, fname_bem, fname_trans, subject, subjects_dir)

    assert_true(min_dist < (dist[0] * 1000.) < (min_dist + 1.))

    assert_raises(ValueError, fit_dipole, evoked, cov, fname_bem, fname_trans,
                  -1.)


def _compute_depth(dip, fname_bem, fname_trans, subject, subjects_dir):
    """Compute dipole depth"""
    trans = read_trans(fname_trans)
    trans = _get_mri_head_t(trans)[0]
    bem = read_bem_solution(fname_bem)
    surf = _bem_find_surface(bem, 'inner_skull')
    points = surf['rr']
    points = apply_trans(trans['trans'], points)
    depth = _compute_nearest(points, dip.pos, return_dists=True)[1][0]
    return np.ravel(depth)


run_tests_if_main(False)
