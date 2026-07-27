"""Synthetic barrel point-cloud generator for training and evaluation.

Generates point clouds (P, N) + ground-truth rho grids from a parametric
barrel profile, with injected noise/artifacts matching real-scan statistics.
All dimensions in **metres** (matching barrel_reconstruct.py's internal units).

Usage:
    python barrel_synth.py --test          # quick self-check: 10 barrels, print stats
    python barrel_synth.py --dump FILE.npz # generate one barrel, save arrays
"""
import argparse
import sys

import numpy as np

# ── Real-scan calibration ──────────────────────────────────────────────────────
# From profiles_combined.csv (12 barrels, 53 axial slices):
#   Profile position is mm from left crozebevel → right crozebevel.
#   Below are the mean radii at 20 mm increments (0–760 mm), used to
#   build a cubic-spline profile for the synthetic generator.
_PROFILE_POS_MM = np.array([
      0,  20,  40,  60,  80, 100, 120, 140, 160, 180,
    200, 220, 240, 260, 280, 300, 320, 340, 360, 380,
    400, 420, 440, 460, 480, 500, 520, 540, 560, 580,
    600, 620, 640, 660, 680, 700, 720, 740, 760], dtype=float)

_PROFILE_RADIUS_MM = np.array([
    269.97, 273.34, 276.94, 279.86, 283.39, 286.71, 289.96, 293.13,
    296.09, 298.89, 301.63, 304.28, 306.97, 309.49, 311.83, 313.88,
    315.60, 316.88, 317.74, 318.15, 318.18, 317.86, 317.09, 315.86,
    314.24, 312.23, 309.95, 307.44, 304.72, 302.08, 299.29, 296.51,
    293.56, 290.47, 287.28, 283.95, 280.56, 277.31, 273.92], dtype=float)

# Cross-barrel standard deviations at each slice (mm)
_PROFILE_STD_MM = np.array([
    3.81, 3.32, 2.82, 2.47, 2.33, 2.21, 2.09, 1.91, 1.81, 1.78,
    1.80, 1.81, 1.93, 2.01, 2.08, 2.13, 2.19, 2.24, 2.29, 2.32,
    2.33, 2.37, 2.35, 2.31, 2.24, 2.16, 2.08, 2.04, 1.97, 1.99,
    2.01, 2.13, 2.31, 2.52, 2.61, 2.80, 2.91, 3.11, 3.46], dtype=float)

# Aggregate stats (from measurements_summary.csv, 12 barrels)
_AXIAL_SPAN_MEAN_MM = 828.2
_AXIAL_SPAN_STD_MM  = 7.4
_VOLUME_MEAN_L      = 225.7
_VOLUME_STD_L       = 2.3

# Crozehead radius (where the profile is widest in el, i.e. where wall meets head)
_CROZEHEAD_RADIUS_MM = 266.0   # approximate — the creased peak
# Head radius (flat disc)
_HEAD_RADIUS_MM      = 250.0   # approximate inner head disc radius

# Noise floor from real scans (fidelity_rms from barrel_reconstruct.py logs)
_NOISE_RMS_MM = 1.5


# ── Profile interpolation ─────────────────────────────────────────────────────

def _interp_profile(z_m, span_m, r_crozehead_m, r_bilge_m, rng):
    """Interpolate the calibration profile to arbitrary barrel dimensions.
    `z_m` are axial positions (metres) relative to barrel midpoint.
    Returns radius (m) at each z."""
    half = span_m / 2.0
    # Map z to the normalized 0–1 range (crozebevel to crozebevel)
    t = (z_m + half) / span_m
    t = np.clip(t, 0, 1)
    # Map calibration profile to 0–1
    t_cal = _PROFILE_POS_MM / _PROFILE_POS_MM[-1]
    r_cal = _PROFILE_RADIUS_MM / 1000.0   # mm → m

    # Scale calibration profile to match requested bilge & crozehead radii
    cal_bilge = r_cal.max()
    cal_crozehead = r_cal[0]   # crozebevel is close to crozehead
    # Linear map: cal_crozehead→r_crozehead, cal_bilge→r_bilge
    a = (r_bilge_m - r_crozehead_m) / (cal_bilge - cal_crozehead)
    b = r_crozehead_m - a * cal_crozehead
    r_scaled = a * r_cal + b

    # Per-barrel random perturbation (within observed cross-barrel std)
    perturb = rng.normal(0, _PROFILE_STD_MM / 1000.0 * 0.5)  # half std
    r_scaled = r_scaled + perturb

    # Interpolate
    return np.interp(t, t_cal, r_scaled)


# ── Barrel geometry ────────────────────────────────────────────────────────────

def _barrel_profile(el, params):
    """Compute rho(el) for a parametric barrel.

    The barrel is represented as a spherical height field ρ(el) about its
    geometric centre (midpoint of the axis).  The profile has three zones:

    1. Top head (el < corner_top):  flat disc at axial distance h_top
       → ρ = h_top / cos(el)
    2. Wall (corner_top < el < corner_bot):  the stave wall, interpolated
       from the calibration profile
    3. Bottom head (el > corner_bot): flat disc at axial distance h_bot
       → ρ = h_bot / cos(π - el)

    The crozehead corners (el values where wall meets head) are derived from
    the barrel geometry.

    Parameters
    ----------
    el : ndarray, shape (N,)
        Polar angles (0 at +axis pole, π at −axis pole).
    params : dict with keys:
        span_m, r_crozehead_m, r_bilge_m, corner_top, corner_bot,
        h_top, h_bot, rng

    Returns
    -------
    rho : ndarray, shape (N,)
        Radial distance from the barrel centre.
    zone : ndarray of int, shape (N,)
        0=top head, 1=wall, 2=bottom head
    """
    corner_top = params["corner_top"]
    corner_bot = params["corner_bot"]
    h_top = params["h_top"]
    h_bot = params["h_bot"]
    span_m = params["span_m"]
    r_crozehead_m = params["r_crozehead_m"]
    r_bilge_m = params["r_bilge_m"]
    rng = params["rng"]

    rho = np.empty_like(el)
    zone = np.ones(len(el), dtype=int)   # default: wall

    # Head zones
    top_mask = el < corner_top
    bot_mask = el > corner_bot
    wall_mask = ~top_mask & ~bot_mask
    zone[top_mask] = 0
    zone[bot_mask] = 2

    # Top head: flat disc ρ = h / cos(el)
    cos_top = np.cos(el[top_mask])
    cos_top = np.where(np.abs(cos_top) > 1e-6, cos_top, 1e-6)
    rho[top_mask] = h_top / cos_top

    # Bottom head: flat disc ρ = h / |cos(el)|
    cos_bot = np.abs(np.cos(el[bot_mask]))
    cos_bot = np.where(cos_bot > 1e-6, cos_bot, 1e-6)
    rho[bot_mask] = h_bot / cos_bot

    # Wall: convert el to axial coordinate z, then interpolate profile
    # For a point at polar angle el and radial distance ρ from the centre,
    # the axial coordinate is z = ρ cos(el) and radial r = ρ sin(el).
    # On the wall, we know r(z) from the profile, and ρ = r / sin(el).
    # To avoid the circular dependency, parameterize by z directly.
    # Map el to z: for the wall region, z ranges from +half_span (near top)
    # to -half_span (near bottom).
    half = span_m / 2.0
    # At the corners, z_top = h_top, z_bot = -h_bot (signed)
    # Map wall el linearly to z between z_top and z_bot
    el_wall = el[wall_mask]
    t = (el_wall - corner_top) / (corner_bot - corner_top)  # 0 at top corner, 1 at bot
    z_wall = h_top * (1 - t) + (-h_bot) * t   # linear in el → z

    r_wall = _interp_profile(z_wall, span_m, r_crozehead_m, r_bilge_m, rng)
    sin_el = np.sin(el_wall)
    sin_el = np.where(sin_el > 1e-6, sin_el, 1e-6)
    rho[wall_mask] = np.sqrt(z_wall**2 + r_wall**2)

    return rho, zone


def generate_barrel_params(rng, overrides=None):
    """Sample random barrel geometry parameters from the calibration ranges.

    Returns a dict of parameters suitable for `_barrel_profile`.
    """
    ov = overrides or {}

    span_m = ov.get("span_m",
                     rng.normal(_AXIAL_SPAN_MEAN_MM, _AXIAL_SPAN_STD_MM) / 1000.0)
    r_crozehead_m = ov.get("r_crozehead_m",
                           rng.normal(_CROZEHEAD_RADIUS_MM, 4.0) / 1000.0)
    r_bilge_m = ov.get("r_bilge_m",
                       rng.normal(_PROFILE_RADIUS_MM.max(), 2.5) / 1000.0)

    half = span_m / 2.0
    # Head plane offsets from centre (slightly inside the crozehead)
    h_top = ov.get("h_top", half - rng.uniform(0.002, 0.005))
    h_bot = ov.get("h_bot", half - rng.uniform(0.002, 0.005))

    # Crozehead corner angles: el where the wall meets the head
    # At the corner, tan(el) = r_crozehead / h  → el = atan2(r_crozehead, h)
    corner_top = np.arctan2(r_crozehead_m, h_top)
    corner_bot = np.pi - np.arctan2(r_crozehead_m, h_bot)

    # Ovality: slight elliptical distortion in azimuth
    ovality_amp = ov.get("ovality_amp", rng.uniform(0.0, 0.003))  # up to 3 mm
    ovality_phase = ov.get("ovality_phase", rng.uniform(0, 2 * np.pi))

    # Stave ripple
    n_staves = ov.get("n_staves", rng.integers(28, 36))
    stave_amp = ov.get("stave_amp", rng.uniform(0.0, 0.001))  # up to 1 mm

    return {
        "span_m": span_m,
        "r_crozehead_m": r_crozehead_m,
        "r_bilge_m": r_bilge_m,
        "h_top": h_top,
        "h_bot": h_bot,
        "corner_top": corner_top,
        "corner_bot": corner_bot,
        "ovality_amp": ovality_amp,
        "ovality_phase": ovality_phase,
        "n_staves": n_staves,
        "stave_amp": stave_amp,
        "rng": rng,
    }


def generate_clean_cloud(params, n_points=500_000):
    """Generate a clean point cloud on the analytic barrel surface.

    Returns (P, N, rho_gt, el, az, zone) all shape (n_points,).
    P and N are in metres, in the barrel's local frame (axis = [1,0,0],
    centre at origin).
    """
    rng = params["rng"]
    # Sample uniformly on the sphere in (el, az)
    # For a barrel, area element ~ sin(el) del daz, but the head caps are small.
    # Use stratified sampling for better coverage of pole regions.
    el = np.arccos(1 - 2 * rng.random(n_points))  # uniform on sphere
    az = rng.uniform(-np.pi, np.pi, n_points)

    # Get ground-truth rho
    rho_gt, zone = _barrel_profile(el, params)

    # Apply ovality (az-dependent radial modulation, wall only)
    ovality = 1.0 + params["ovality_amp"] * np.cos(2 * (az - params["ovality_phase"]))
    wall = zone == 1
    rho_gt[wall] *= ovality[wall]

    # Apply stave ripple (wall only)
    ripple = 1.0 + params["stave_amp"] * np.cos(params["n_staves"] * az)
    rho_gt[wall] *= ripple[wall]

    # Convert to Cartesian (axis = x)
    # el from +x axis, az in y-z plane
    x = rho_gt * np.cos(el)
    y = rho_gt * np.sin(el) * np.cos(az)
    z = rho_gt * np.sin(el) * np.sin(az)
    P = np.column_stack([x, y, z])

    # Surface normals (approximate via finite-difference on the analytic surface)
    # For the spherical height field, the outward normal at (el, az) is
    # approximately the radial direction (from centre) since the barrel is
    # nearly convex.  Refine with ∂ρ/∂el and ∂ρ/∂az for accuracy.
    deps = 1e-4
    el_p = np.clip(el + deps, 0, np.pi)
    el_m = np.clip(el - deps, 0, np.pi)
    rho_ep, _ = _barrel_profile(el_p, params)
    rho_em, _ = _barrel_profile(el_m, params)
    rho_ep[zone == 1] *= (1.0 + params["ovality_amp"] *
                          np.cos(2 * (az[zone == 1] - params["ovality_phase"])))
    rho_em[zone == 1] *= (1.0 + params["ovality_amp"] *
                          np.cos(2 * (az[zone == 1] - params["ovality_phase"])))
    drho_del = (rho_ep - rho_em) / (2 * deps)

    az_p = az + deps
    az_m = az - deps
    rho_ap, _ = _barrel_profile(el, params)
    # ovality modulation for az perturbation
    ov_p = 1.0 + params["ovality_amp"] * np.cos(2 * (az_p - params["ovality_phase"]))
    ov_m = 1.0 + params["ovality_amp"] * np.cos(2 * (az_m - params["ovality_phase"]))
    rp_p = 1.0 + params["stave_amp"] * np.cos(params["n_staves"] * az_p)
    rp_m = 1.0 + params["stave_amp"] * np.cos(params["n_staves"] * az_m)
    rho_azp = rho_ap.copy()
    rho_azm = rho_ap.copy()
    rho_azp[wall] *= ov_p[wall] * rp_p[wall]
    rho_azm[wall] *= ov_m[wall] * rp_m[wall]
    drho_daz = (rho_azp - rho_azm) / (2 * deps)

    # Tangent vectors in (el, az) directions
    # ∂P/∂el = drho/del * r_hat + rho * ∂r_hat/∂el
    # ∂P/∂az = drho/daz * r_hat + rho * sin(el) * az_hat
    # For simplicity, compute numerically
    def to_cart(r, e, a):
        return np.column_stack([r * np.cos(e),
                                r * np.sin(e) * np.cos(a),
                                r * np.sin(e) * np.sin(a)])
    P_ep = to_cart(rho_ep, el_p, az)
    P_em = to_cart(rho_em, el_m, az)
    P_ap = to_cart(rho_azp, el, az_p)
    P_am = to_cart(rho_azm, el, az_m)
    t_el = (P_ep - P_em) / (2 * deps)
    t_az = (P_ap - P_am) / (2 * deps)
    N = np.cross(t_el, t_az)
    norms = np.linalg.norm(N, axis=1, keepdims=True)
    norms[norms == 0] = 1
    N = N / norms

    # Ensure normals point inward (toward centre = origin) since the barrel
    # is scanned from inside
    dot = np.einsum("ij,ij->i", N, -P)  # dot with inward direction
    flip = dot < 0
    N[flip] *= -1

    return P, N, rho_gt, el, az, zone


# ── Noise injection ───────────────────────────────────────────────────────────

def inject_noise(P, N, rho_gt, el, az, zone, params, rng,
                 noise_rms_m=_NOISE_RMS_MM / 1000.0,
                 add_bung=True, add_floaters=True,
                 head_dropout_rate=0.0):
    """Add realistic noise/artifacts to a clean point cloud.

    Returns (P_noisy, N_noisy, labels) where labels is:
      0 = clean point
      1 = bung point
      2 = floater point
      3 = dropped head point (set to NaN or removed)
    """
    n = len(P)
    labels = np.zeros(n, dtype=int)

    # 1. Gaussian radial noise
    radial_noise = rng.normal(0, noise_rms_m, n)
    # Direction: along the radial (centre→point) direction
    r_hat = P / (np.linalg.norm(P, axis=1, keepdims=True) + 1e-12)
    P_noisy = P + radial_noise[:, None] * r_hat

    # Perturb normals slightly
    n_noise = rng.normal(0, 0.05, N.shape)
    N_noisy = N + n_noise
    norms = np.linalg.norm(N_noisy, axis=1, keepdims=True)
    norms[norms == 0] = 1
    N_noisy = N_noisy / norms

    # 2. Bung injection (raised patch on the wall)
    bung_info = {}
    if add_bung and rng.random() < 0.4:   # 40% of barrels have a bung
        bung_el = rng.uniform(params["corner_top"] + 0.15,
                              params["corner_bot"] - 0.15)
        bung_az = rng.uniform(-np.pi, np.pi)
        bung_r_el = rng.uniform(0.03, 0.08)   # angular radius in el
        bung_r_az = rng.uniform(0.05, 0.15)   # angular radius in az
        bung_height = rng.uniform(0.004, 0.012)  # 4–12 mm excess

        # Elliptical patch
        d_el = (el - bung_el) / bung_r_el
        d_az_raw = az - bung_az
        d_az_raw = (d_az_raw + np.pi) % (2 * np.pi) - np.pi  # wrap
        d_az = d_az_raw / bung_r_az
        dist2 = d_el**2 + d_az**2
        bung_mask = (dist2 < 1.0) & (zone == 1)

        # Raised cosine profile
        bung_excess = bung_height * 0.5 * (1 + np.cos(np.pi * np.sqrt(dist2[bung_mask])))
        P_noisy[bung_mask] += bung_excess[:, None] * r_hat[bung_mask]
        labels[bung_mask] = 1

        bung_info = {
            "el_deg": np.rad2deg(bung_el),
            "az_deg": np.rad2deg(bung_az),
            "n_points": int(bung_mask.sum()),
            "height_mm": bung_height * 1000,
        }

    # 3. Floater injection (off-surface clusters)
    floater_info = {}
    if add_floaters and rng.random() < 0.5:   # 50% chance
        n_clusters = rng.integers(1, 4)
        total_floaters = 0
        for _ in range(n_clusters):
            # Random location near the surface
            c_el = rng.uniform(0.2, np.pi - 0.2)
            c_az = rng.uniform(-np.pi, np.pi)
            c_rho = rng.uniform(0.1, 0.25)   # 10–25 cm from centre (inside barrel)
            cx = c_rho * np.cos(c_el)
            cy = c_rho * np.sin(c_el) * np.cos(c_az)
            cz = c_rho * np.sin(c_el) * np.sin(c_az)
            centre = np.array([cx, cy, cz])

            n_f = rng.integers(20, 100)
            f_pts = centre + rng.normal(0, 0.005, (n_f, 3))  # 5 mm spread
            f_nrm = rng.normal(0, 1, (n_f, 3))
            f_nrm /= np.linalg.norm(f_nrm, axis=1, keepdims=True)

            P_noisy = np.vstack([P_noisy, f_pts])
            N_noisy = np.vstack([N_noisy, f_nrm])
            labels = np.concatenate([labels, np.full(n_f, 2, dtype=int)])
            total_floaters += n_f

        floater_info = {"n_floaters": total_floaters, "n_clusters": n_clusters}
        # Recalculate el, az for the new points
        rho_new = np.linalg.norm(P_noisy, axis=1)
        el = np.arccos(np.clip(P_noisy[:, 0] / (rho_new + 1e-12), -1, 1))
        az = np.arctan2(P_noisy[:, 2], P_noisy[:, 1])

    # 4. Head/pole dropout (simulate sparse pole coverage)
    if head_dropout_rate > 0:
        head_mask = (zone == 0) | (zone == 2)  # only original points, not floaters
        if head_mask.any():
            n_head = head_mask.sum()
            drop = rng.random(n_head) < head_dropout_rate
            drop_idx = np.where(head_mask)[0][drop]
            labels[drop_idx] = 3

    info = {"bung": bung_info, "floaters": floater_info,
            "head_dropout_rate": head_dropout_rate}
    return P_noisy, N_noisy, labels, info


# ── Ground-truth grid generation ──────────────────────────────────────────────

def generate_gt_grid(params, el_centres):
    """Generate the ground-truth rho grid for a synthetic barrel.

    This is the *ideal* rho(el, az) that the denoiser should recover —
    the analytic surface value at each grid cell centre, including ovality
    and stave ripple.

    Parameters
    ----------
    params : dict from generate_barrel_params
    el_centres : ndarray, shape (n_el,)

    Returns
    -------
    gt_grid : ndarray, shape (n_el, n_az)
    """
    from barrel_reconstruct import N_AZ
    n_el = len(el_centres)
    az_centres = -np.pi + (np.arange(N_AZ) + 0.5) / N_AZ * (2 * np.pi)
    el_2d, az_2d = np.meshgrid(el_centres, az_centres, indexing="ij")
    el_flat = el_2d.ravel()
    az_flat = az_2d.ravel()

    rho_flat, zone_flat = _barrel_profile(el_flat, params)

    # Apply ovality and stave ripple (wall only)
    wall = zone_flat == 1
    ovality = 1.0 + params["ovality_amp"] * np.cos(
        2 * (az_flat - params["ovality_phase"]))
    ripple = 1.0 + params["stave_amp"] * np.cos(
        params["n_staves"] * az_flat)
    rho_flat[wall] *= ovality[wall] * ripple[wall]

    return rho_flat.reshape(n_el, N_AZ)


# ── High-level API ─────────────────────────────────────────────────────────────

def generate_barrel(seed=None, n_points=500_000, noise_rms_mm=_NOISE_RMS_MM,
                    add_bung=True, add_floaters=True, head_dropout_rate=0.3,
                    overrides=None):
    """Generate one synthetic barrel: clean cloud, noisy cloud, labels, gt grid.

    Parameters
    ----------
    seed : int or None
    n_points : int, number of surface sample points
    noise_rms_mm : float, Gaussian noise RMS in mm
    add_bung : bool
    add_floaters : bool
    head_dropout_rate : float, 0–1 fraction of head points to drop
    overrides : dict, override specific barrel params

    Returns
    -------
    result : dict with keys:
        P_clean, N_clean    — clean surface points+normals (n_points, 3)
        rho_gt              — ground-truth rho per clean point (n_points,)
        el_clean, az_clean  — spherical coords of clean points (n_points,)
        zone                — zone labels per clean point (n_points,)
        P_noisy, N_noisy    — noisy points+normals (n_noisy, 3); may be > n_points
        labels              — per-point labels (0=clean, 1=bung, 2=floater, 3=dropped)
        params              — barrel geometry parameters dict
        noise_info          — noise injection info dict
    """
    rng = np.random.default_rng(seed)
    params = generate_barrel_params(rng, overrides)
    P_clean, N_clean, rho_gt, el, az, zone = generate_clean_cloud(params, n_points)
    P_noisy, N_noisy, labels, noise_info = inject_noise(
        P_clean, N_clean, rho_gt, el, az, zone, params, rng,
        noise_rms_m=noise_rms_mm / 1000.0,
        add_bung=add_bung, add_floaters=add_floaters,
        head_dropout_rate=head_dropout_rate)
    return {
        "P_clean": P_clean, "N_clean": N_clean,
        "rho_gt": rho_gt, "el_clean": el, "az_clean": az, "zone": zone,
        "P_noisy": P_noisy, "N_noisy": N_noisy, "labels": labels,
        "params": params, "noise_info": noise_info,
    }


# ── Self-test ──────────────────────────────────────────────────────────────────

def _self_test(n_barrels=10):
    """Generate synthetic barrels and print diagnostic statistics."""
    print("=" * 60)
    print("barrel_synth self-test: generating %d barrels" % n_barrels)
    print("=" * 60)

    stats = {"n_points": [], "n_noisy": [], "rho_mean": [], "rho_std": [],
             "span": [], "has_bung": [], "n_floaters": [],
             "head_dropout": []}

    for i in range(n_barrels):
        b = generate_barrel(seed=i, n_points=200_000)
        P = b["P_clean"]
        rho = b["rho_gt"]
        labels = b["labels"]
        params = b["params"]

        span = params["span_m"] * 1000
        rho_mm = rho * 1000
        n_bung = int((labels == 1).sum())
        n_float = int((labels == 2).sum())
        n_drop = int((labels == 3).sum())

        stats["n_points"].append(len(P))
        stats["n_noisy"].append(len(b["P_noisy"]))
        stats["rho_mean"].append(rho_mm.mean())
        stats["rho_std"].append(rho_mm.std())
        stats["span"].append(span)
        stats["has_bung"].append(n_bung > 0)
        stats["n_floaters"].append(n_float)
        stats["head_dropout"].append(n_drop)

        print("  barrel %2d: span=%.1f mm, rho=%.1f±%.1f mm, "
              "bung=%d pts, floaters=%d, head_drop=%d, total_noisy=%d"
              % (i, span, rho_mm.mean(), rho_mm.std(),
                 n_bung, n_float, n_drop, len(b["P_noisy"])))

    print("\n--- Summary ---")
    print("  Axial span: %.1f ± %.1f mm (target: %.1f ± %.1f)"
          % (np.mean(stats["span"]), np.std(stats["span"]),
             _AXIAL_SPAN_MEAN_MM, _AXIAL_SPAN_STD_MM))
    print("  Mean rho: %.1f ± %.1f mm" % (np.mean(stats["rho_mean"]),
                                           np.std(stats["rho_mean"])))
    print("  Bungs present: %d / %d barrels" % (sum(stats["has_bung"]), n_barrels))
    print("  Floaters: %.0f ± %.0f per barrel" % (np.mean(stats["n_floaters"]),
                                                    np.std(stats["n_floaters"])))
    return stats


def main():
    parser = argparse.ArgumentParser(description="Synthetic barrel generator")
    parser.add_argument("--test", action="store_true",
                        help="Run self-test: generate 10 barrels, print stats")
    parser.add_argument("--dump", type=str, default=None,
                        help="Generate one barrel and save to .npz file")
    parser.add_argument("--seed", type=int, default=42,
                        help="Random seed")
    parser.add_argument("--n-points", type=int, default=500_000,
                        help="Points per barrel")
    args = parser.parse_args()

    if args.test:
        _self_test()
    elif args.dump:
        b = generate_barrel(seed=args.seed, n_points=args.n_points)
        # Can't save rng in npz — remove it from params
        save_params = {k: v for k, v in b["params"].items() if k != "rng"}
        np.savez_compressed(args.dump,
                            P_clean=b["P_clean"], N_clean=b["N_clean"],
                            rho_gt=b["rho_gt"], el_clean=b["el_clean"],
                            az_clean=b["az_clean"], zone=b["zone"],
                            P_noisy=b["P_noisy"], N_noisy=b["N_noisy"],
                            labels=b["labels"],
                            **{f"param_{k}": v for k, v in save_params.items()})
        print("Saved to %s" % args.dump)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
