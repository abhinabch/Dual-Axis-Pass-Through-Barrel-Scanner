"""Curvature and crease feature calculation for points and grids.

Provides local curvature / crease detection without hardcoding row indices
or fixed degree bands. Replaces CORNER_HALF_DEG thresholding with a continuous,
data-driven geometry feature.

Features:
  1. compute_point_curvature: PCA surface variation over k-NN neighborhoods (per-point)
  2. compute_grid_curvature: 2D surface curvature feature over rho(el, az) grid (per-cell)

Usage:
    python barrel_features.py --test
"""
import argparse
import numpy as np
from sklearn.neighbors import NearestNeighbors


def compute_point_curvature(P, k=20, max_points=200_000, seed=42):
    """Compute per-point PCA surface variation: σ = λ1 / (λ1 + λ2 + λ3).

    Measures local deviation from planarity.
    Near 0 on smooth stave wall and flat heads, peaks at crozehead creases.

    Parameters
    ----------
    P : ndarray (N, 3) — point cloud positions
    k : int — number of nearest neighbors (default 20)
    max_points : int — if N > max_points, estimate over a random subsample
                       or chunking to keep computation fast.

    Returns
    -------
    curvature : ndarray (N,) — per-point curvature score in [0, 0.5]
    """
    N_pts = len(P)
    if N_pts == 0:
        return np.zeros(0)

    # Subsample if cloud is huge for k-NN fitting speed
    if N_pts > max_points:
        rng = np.random.default_rng(seed)
        sub_idx = rng.choice(N_pts, max_points, replace=False)
        P_ref = P[sub_idx]
    else:
        P_ref = P

    nn = NearestNeighbors(n_neighbors=k, algorithm="kd_tree", n_jobs=1)
    nn.fit(P_ref)

    # Query neighbors for all points
    # Process in chunks if N_pts is large
    chunk_size = 50_000
    curvature = np.zeros(N_pts, dtype=np.float64)

    for start in range(0, N_pts, chunk_size):
        end = min(start + chunk_size, N_pts)
        idx = nn.kneighbors(P[start:end], return_distance=False)  # (chunk, k)
        neighbors = P_ref[idx]  # (chunk, k, 3)

        # Covariance per point neighborhood
        # Center neighbors: (chunk, k, 3)
        centered = neighbors - neighbors.mean(axis=1, keepdims=True)
        # Covariance matrix: (chunk, 3, 3)
        cov = np.matmul(centered.transpose(0, 2, 1), centered) / k

        # Eigenvalues per point: (chunk, 3)
        # eigh returns sorted ascending: evalues[:, 0] = λ1 (smallest)
        evals = np.linalg.eigvalsh(cov)
        evals = np.maximum(evals, 0.0)
        sum_evals = evals.sum(axis=1, keepdims=True) + 1e-12
        sigma = evals[:, 0] / sum_evals[:, 0]  # λ1 / sum(λ)
        curvature[start:end] = sigma

    return curvature


def compute_grid_curvature(grid, el_centres=None):
    """Compute 2D grid curvature feature over a rho(el, az) grid.

    Combines second derivative along polar angle (el) and azimuthal angle (az)
    with edge boundary handling (azimuth wraps, polar padded).

    Parameters
    ----------
    grid : ndarray (n_el, n_az) — surface height field
    el_centres : ndarray (n_el,) optional — polar row angles in radians
                 (used for exact non-uniform grid spacing scaling if provided)

    Returns
    -------
    curv_grid : ndarray (n_el, n_az) — continuous curvature channel normalized to [0, 1]
    """
    n_el, n_az = grid.shape
    g = np.nan_to_num(grid, nan=np.nanmedian(grid))

    # Azimuthal wrap padding
    g_az_padded = np.pad(g, ((0, 0), (1, 1)), mode="wrap")
    # Polar edge padding (reflect)
    g_full = np.pad(g_az_padded, ((1, 1), (0, 0)), mode="edge")

    # Second derivatives
    # Polar 2nd derivative: d2g/del2
    if el_centres is not None and len(el_centres) == n_el:
        # Non-uniform spacing correction along polar dimension
        d_el = np.gradient(el_centres)
        d_el_2d = d_el[:, None]
        d2_el = (g_full[2:, 1:-1] - 2 * g_full[1:-1, 1:-1] + g_full[:-2, 1:-1]) / (d_el_2d ** 2 + 1e-8)
    else:
        d2_el = g_full[2:, 1:-1] - 2 * g_full[1:-1, 1:-1] + g_full[:-2, 1:-1]

    # Azimuthal 2nd derivative: d2g/daz2
    d2_az = g_full[1:-1, 2:] - 2 * g_full[1:-1, 1:-1] + g_full[1:-1, :-2]

    # Combined magnitude of 2nd derivatives (Laplacian / curvature metric)
    curv = np.sqrt(d2_el**2 + d2_az**2)

    # Robust scale normalization to [0, 1]
    p99 = np.percentile(curv, 99)
    if p99 > 1e-8:
        curv_norm = np.clip(curv / p99, 0.0, 1.0)
    else:
        curv_norm = np.zeros_like(curv)

    return curv_norm


def _self_test():
    """Test curvature features on a synthetic barrel."""
    print("=" * 60)
    print("barrel_features self-test")
    print("=" * 60)

    from barrel_synth import generate_barrel, generate_gt_grid
    from barrel_reconstruct import (
        spherical_coords, make_el_sampling, build_rho_grid, N_AZ, _frame
    )

    print("1. Generating synthetic barrel...")
    b = generate_barrel(seed=42, n_points=100_000, add_bung=True, add_floaters=False)
    P, N = b["P_noisy"], b["N_noisy"]
    params = b["params"]
    c_top, c_bot = params["corner_top"], params["corner_bot"]

    print("2. Computing point-level PCA curvature...")
    pt_curv = compute_point_curvature(P, k=20)
    print("   Point curvature min=%.4f, max=%.4f, mean=%.4f" %
          (pt_curv.min(), pt_curv.max(), pt_curv.mean()))

    print("3. Building grid and computing grid curvature...")
    a = np.array([1.0, 0.0, 0.0])
    centre = np.zeros(3)
    u, w = _frame(a)
    az, el, rho = spherical_coords(P, centre, a, u, w)
    el_ctr, el_edges, corners = make_el_sampling(az, el, rho, N_AZ)
    grid, cnt = build_rho_grid(az, el, rho, N_AZ, el_edges)

    grid_curv = compute_grid_curvature(grid, el_ctr)
    print("   Grid curvature shape: %s" % str(grid_curv.shape))

    # Check that curvature peaks near crozehead corners
    row_curv = grid_curv.mean(axis=1)
    top_row = np.argmin(np.abs(el_ctr - c_top))
    bot_row = np.argmin(np.abs(el_ctr - c_bot))

    print("   Top crozehead corner row=%d (el=%.1f°), curvature=%.4f (row mean)" %
          (top_row, np.rad2deg(el_ctr[top_row]), row_curv[top_row]))
    print("   Bot crozehead corner row=%d (el=%.1f°), curvature=%.4f (row mean)" %
          (bot_row, np.rad2deg(el_ctr[bot_row]), row_curv[bot_row]))

    # Mid-wall row (between top and bot corner)
    mid_row = (top_row + bot_row) // 2
    print("   Mid stave wall row=%d (el=%.1f°), curvature=%.4f (row mean)" %
          (mid_row, np.rad2deg(el_ctr[mid_row]), row_curv[mid_row]))

    assert row_curv[top_row] > row_curv[mid_row], "Top corner curvature must exceed mid-wall"
    assert row_curv[bot_row] > row_curv[mid_row], "Bottom corner curvature must exceed mid-wall"
    print("Self-test passed! Curvature peaks sharply at crozehead creases.")


def main():
    parser = argparse.ArgumentParser(description="Barrel curvature features")
    parser.add_argument("--test", action="store_true", help="Run self-test")
    args = parser.parse_args()

    if args.test:
        _self_test()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
