"""Evaluation harness for barrel reconstruction quality.

Wraps the metric computations from barrel_reconstruct.reconstruct_one (fidelity,
crozehead-band fidelity, asymmetry, watertightness, volume) and adds:
  - gt_rms:       RMS vs. known ground-truth grid (synthetic only)
  - head_pole_rms: fidelity restricted to pole/head rows (el < 15° or el > 165°)

Usage:
    # Evaluate the rules pipeline on a synthetic barrel:
    python barrel_eval.py --synthetic --seed 42

    # Compare rules vs. learned on a real scan:
    python barrel_eval.py --compare BLXF-22-60575-1.obscan
"""
import argparse
import sys
import time

import numpy as np


# ── Metric functions (extracted from barrel_reconstruct.py logic) ──────────────

def fidelity_rms(rho_pts, el_pts, az_pts, clean_grid, el_edges, n_az, bad_mask):
    """RMS residual of every input point vs. the clean grid surface.
    Points mapping to bad/filled cells are excluded (same as reconstruct_one).

    Parameters
    ----------
    rho_pts  : ndarray (N,) — radial distances of original input points
    el_pts   : ndarray (N,) — polar angles of input points
    az_pts   : ndarray (N,) — azimuth angles of input points
    clean_grid : ndarray (n_el, n_az) — the denoised/cleaned rho grid
    el_edges : ndarray (n_el+1,) — polar bin edges
    n_az     : int — azimuthal resolution
    bad_mask : ndarray (n_el, n_az) bool — cells that were filled/replaced

    Returns
    -------
    rms : float — RMS in metres
    p95 : float — 95th percentile absolute residual in metres
    max_abs : float — max absolute residual in metres
    n_pts   : int — number of points used
    """
    from barrel_reconstruct import el_bin_index
    ai = np.clip(((az_pts + np.pi) / (2 * np.pi) * n_az).astype(int), 0, n_az - 1)
    ei = el_bin_index(el_pts, el_edges)
    resid = rho_pts - clean_grid[ei, ai]
    keep = ~bad_mask[ei, ai]
    rr = resid[keep]
    if rr.size == 0:
        return 0.0, 0.0, 0.0, 0
    rms = float(np.sqrt((rr**2).mean()))
    p95 = float(np.percentile(np.abs(rr), 95))
    mx = float(np.abs(rr).max())
    return rms, p95, mx, int(rr.size)


def crozehead_fidelity_rms(rho_pts, el_pts, az_pts, clean_grid, el_edges,
                           n_az, bad_mask, corners, half_deg):
    """Fidelity restricted to points within `half_deg` of either crozehead corner."""
    from barrel_reconstruct import el_bin_index
    ai = np.clip(((az_pts + np.pi) / (2 * np.pi) * n_az).astype(int), 0, n_az - 1)
    ei = el_bin_index(el_pts, el_edges)
    resid = rho_pts - clean_grid[ei, ai]
    keep = ~bad_mask[ei, ai]
    band = np.deg2rad(half_deg)
    near = keep & ((np.abs(el_pts - corners[0]) < band) |
                   (np.abs(el_pts - corners[1]) < band))
    cr = resid[near]
    if cr.size == 0:
        return 0.0, 0.0, 0
    rms = float(np.sqrt((cr**2).mean()))
    mx = float(np.abs(cr).max())
    return rms, mx, int(cr.size)


def head_pole_rms(rho_pts, el_pts, az_pts, clean_grid, el_edges,
                  n_az, bad_mask, pole_margin_deg=15.0):
    """Fidelity restricted to pole/head rows (el < margin or el > π−margin).
    This tracks the specific failure mode from Finding 2."""
    from barrel_reconstruct import el_bin_index
    ai = np.clip(((az_pts + np.pi) / (2 * np.pi) * n_az).astype(int), 0, n_az - 1)
    ei = el_bin_index(el_pts, el_edges)
    resid = rho_pts - clean_grid[ei, ai]
    keep = ~bad_mask[ei, ai]
    margin = np.deg2rad(pole_margin_deg)
    pole = keep & ((el_pts < margin) | (el_pts > np.pi - margin))
    pr = resid[pole]
    if pr.size == 0:
        return 0.0, 0.0, 0
    rms = float(np.sqrt((pr**2).mean()))
    mx = float(np.abs(pr).max())
    return rms, mx, int(pr.size)


def asymmetry_rms(clean_grid):
    """RMS departure of the clean grid from a surface of revolution
    (the per-el-row azimuthal median)."""
    axisym = np.repeat(np.nanmedian(clean_grid, axis=1)[:, None],
                       clean_grid.shape[1], axis=1)
    asym = clean_grid - axisym
    rms = float(np.sqrt((asym**2).mean()))
    mx = float(np.abs(asym).max())
    return rms, mx


def gt_grid_rms(clean_grid, gt_grid):
    """RMS error of the clean grid vs. a known ground-truth grid.
    Only valid for synthetic data with known analytic surface."""
    valid = np.isfinite(clean_grid) & np.isfinite(gt_grid)
    if valid.sum() == 0:
        return 0.0, 0.0
    diff = clean_grid[valid] - gt_grid[valid]
    rms = float(np.sqrt((diff**2).mean()))
    mx = float(np.abs(diff).max())
    return rms, mx


def mesh_metrics(verts, faces):
    """Watertightness, volume, and surface area of a triangle mesh."""
    from barrel_reconstruct import is_watertight, signed_volume, surface_area
    wt, bad_edges = is_watertight(faces)
    vol = abs(signed_volume(verts, faces))
    area = surface_area(verts, faces)
    return {"watertight": wt, "bad_edges": bad_edges,
            "volume_L": vol * 1000.0, "area_m2": area}


# ── Report generation ─────────────────────────────────────────────────────────

def evaluate_grid(rho_pts, el_pts, az_pts, clean_grid, el_edges, n_az,
                  bad_mask, corners, gt_grid=None, label="rules"):
    """Run all grid-level metrics and return a summary dict.

    Parameters
    ----------
    rho_pts, el_pts, az_pts : input point cloud spherical coords
    clean_grid : the denoised grid
    el_edges   : polar bin edges
    n_az       : azimuthal resolution
    bad_mask   : cells that were filled
    corners    : (corner_top, corner_bot) in radians
    gt_grid    : optional ground-truth grid (synthetic only)
    label      : string tag for this evaluation run

    Returns
    -------
    metrics : dict
    """
    from barrel_reconstruct import CORNER_HALF_DEG

    fid_rms, fid_p95, fid_max, fid_n = fidelity_rms(
        rho_pts, el_pts, az_pts, clean_grid, el_edges, n_az, bad_mask)
    crz_rms, crz_max, crz_n = crozehead_fidelity_rms(
        rho_pts, el_pts, az_pts, clean_grid, el_edges, n_az, bad_mask,
        corners, CORNER_HALF_DEG)
    hp_rms, hp_max, hp_n = head_pole_rms(
        rho_pts, el_pts, az_pts, clean_grid, el_edges, n_az, bad_mask)
    asym_rms_val, asym_max = asymmetry_rms(clean_grid)

    metrics = {
        "label": label,
        "fidelity_rms_mm": fid_rms * 1000,
        "fidelity_p95_mm": fid_p95 * 1000,
        "fidelity_max_mm": fid_max * 1000,
        "fidelity_n_pts": fid_n,
        "crozehead_rms_mm": crz_rms * 1000,
        "crozehead_max_mm": crz_max * 1000,
        "crozehead_n_pts": crz_n,
        "head_pole_rms_mm": hp_rms * 1000,
        "head_pole_max_mm": hp_max * 1000,
        "head_pole_n_pts": hp_n,
        "asym_rms_mm": asym_rms_val * 1000,
        "asym_max_mm": asym_max * 1000,
    }

    if gt_grid is not None:
        gt_rms_val, gt_max = gt_grid_rms(clean_grid, gt_grid)
        metrics["gt_rms_mm"] = gt_rms_val * 1000
        metrics["gt_max_mm"] = gt_max * 1000

    return metrics


def print_metrics(metrics, file=sys.stdout):
    """Pretty-print a metrics dict."""
    label = metrics.get("label", "?")
    print("  [%s] Fidelity:   RMS=%.3f mm  95%%=%.3f mm  max=%.3f mm  (%d pts)"
          % (label, metrics["fidelity_rms_mm"], metrics["fidelity_p95_mm"],
             metrics["fidelity_max_mm"], metrics["fidelity_n_pts"]), file=file)
    print("  [%s] Crozehead:  RMS=%.3f mm  max=%.3f mm  (%d pts)"
          % (label, metrics["crozehead_rms_mm"], metrics["crozehead_max_mm"],
             metrics["crozehead_n_pts"]), file=file)
    print("  [%s] Head/pole:  RMS=%.3f mm  max=%.3f mm  (%d pts)"
          % (label, metrics["head_pole_rms_mm"], metrics["head_pole_max_mm"],
             metrics["head_pole_n_pts"]), file=file)
    print("  [%s] Asymmetry:  RMS=%.3f mm  max=%.3f mm"
          % (label, metrics["asym_rms_mm"], metrics["asym_max_mm"]), file=file)
    if "gt_rms_mm" in metrics:
        print("  [%s] vs GT grid: RMS=%.3f mm  max=%.3f mm"
              % (label, metrics["gt_rms_mm"], metrics["gt_max_mm"]), file=file)


def compare_metrics(m_rules, m_learned, file=sys.stdout):
    """Print a side-by-side comparison and flag regressions."""
    keys = ["fidelity_rms_mm", "crozehead_rms_mm", "head_pole_rms_mm",
            "asym_rms_mm"]
    if "gt_rms_mm" in m_rules:
        keys.append("gt_rms_mm")

    print("\n  %-20s  %10s  %10s  %8s  %s" %
          ("Metric", "Rules", "Learned", "Delta%", "Status"), file=file)
    print("  " + "-" * 62, file=file)
    regressions = 0
    for k in keys:
        r = m_rules.get(k, 0)
        l = m_learned.get(k, 0)
        if r > 0:
            delta_pct = (l - r) / r * 100
        else:
            delta_pct = 0
        status = "✓" if delta_pct <= 0 else "✗ REGRESSION"
        if delta_pct > 0:
            regressions += 1
        label = k.replace("_mm", " (mm)")
        print("  %-20s  %10.3f  %10.3f  %+7.1f%%  %s"
              % (label, r, l, delta_pct, status), file=file)
    return regressions


# ── CLI ────────────────────────────────────────────────────────────────────────

def _eval_synthetic(seed=42, n_points=300_000):
    """Evaluate the rules pipeline on a synthetic barrel."""
    from barrel_synth import generate_barrel, generate_gt_grid
    from barrel_reconstruct import (
        spherical_coords, make_el_sampling, combine_wall_head,
        flatten_head_poles, detect_bung, fill_grid, smooth_grid,
        N_AZ, BUNG_SEED_MM, MIN_BUNG_CELLS, BUNG_MAX_AZ_DEG,
        GROSS_OUTLIER_MM, SMOOTH_PASSES, AXIS_NORMAL_SPLIT,
        _frame, _row_median_mad,
    )

    print("Generating synthetic barrel (seed=%d, n=%d)..." % (seed, n_points))
    b = generate_barrel(seed=seed, n_points=n_points,
                        add_bung=False, add_floaters=False,
                        head_dropout_rate=0.3)
    P, N = b["P_noisy"], b["N_noisy"]

    # Run the barrel_reconstruct pipeline up to clean grid
    # Axis is [1,0,0], centre at origin for synthetic barrels
    a = np.array([1.0, 0.0, 0.0])
    centre = np.zeros(3)
    u, w = _frame(a)
    az, el, rho = spherical_coords(P, centre, a, u, w)
    naxis = np.abs(N @ a)
    wall_flag = naxis < AXIS_NORMAL_SPLIT

    el_ctr, el_edges, corners = make_el_sampling(az, el, rho, N_AZ)
    grid, gw, gh = combine_wall_head(az, el, rho, wall_flag, N_AZ, el_edges)
    grid, n_pole = flatten_head_poles(grid, el_ctr, corners)

    bung_mask, bung_info = detect_bung(
        grid, BUNG_SEED_MM, MIN_BUNG_CELLS, el_ctr, corners,
        int(BUNG_MAX_AZ_DEG / 360.0 * N_AZ))

    med_r, _ = _row_median_mad(grid)
    gross = np.isfinite(grid) & (np.abs(grid - med_r[:, None]) > GROSS_OUTLIER_MM / 1000.0)
    bad = bung_mask | np.isnan(grid) | gross
    clean = fill_grid(grid, bad)
    clean = smooth_grid(clean, el_ctr, corners, SMOOTH_PASSES)

    # Ground-truth grid
    gt_grid = generate_gt_grid(b["params"], el_ctr)

    # Evaluate
    metrics = evaluate_grid(rho, el, az, clean, el_edges, N_AZ, bad,
                            corners, gt_grid=gt_grid, label="rules")
    print_metrics(metrics)
    return metrics


def main():
    parser = argparse.ArgumentParser(description="Barrel evaluation harness")
    parser.add_argument("--synthetic", action="store_true",
                        help="Evaluate rules pipeline on a synthetic barrel")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--n-points", type=int, default=300_000)
    parser.add_argument("--compare", type=str, default=None,
                        help="Compare rules vs learned on a .obscan file")
    args = parser.parse_args()

    if args.synthetic:
        _eval_synthetic(seed=args.seed, n_points=args.n_points)
    elif args.compare:
        print("Comparison mode not yet implemented (needs Phase 2+3 models)")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
