"""Reconstruct a clean, watertight barrel mesh from the fused point cloud
stored inside an .obscan (SQLite) scan container.

The .obscan `mesh_vn_0` blob is the scanner's on-device fusion of all depth
frames: ~5.3M vertices (position + unit normal), plus a `mesh_t_0` triangle
list.  That fused cloud is already bundle-adjusted and registered, so we do
NOT re-fuse raw depth (its per-frame poses are not stored and the depth codec
is proprietary).  Instead we impose the barrel prior on the fused cloud.

Key geometric fact: the barrel was scanned from the INSIDE, and a barrel
interior is convex — star-shaped about its centre.  So every ray from the
barrel centre hits the inner surface (stave wall OR a head) exactly once.
That lets us represent the whole inner surface as a single-valued spherical
height field  rho(az, el)  about the centre.  This one representation:

  * removes overlapping / duplicate points   -> one rho per (az, el) cell
  * rejects floaters / stray interior points  -> robust per-cell median
  * unifies the stave wall and both heads     -> no separate end-capping
  * is watertight by construction             -> closed UV-sphere topology
  * localises the bung to a few outlier cells -> detect (rotational
        symmetry + connected component) and fill from the symmetric wall

Two meshes are produced on the SAME grid so they are directly comparable:
  <out>_barrel_clean.ply    - measured rho(az, el): true shape, bung fixed
  <out>_barrel_axisym.ply   - az-median per el-row: ideal surface of revolution

Usage:
    python barrel_reconstruct.py            # uses defaults below
    python barrel_reconstruct.py C:/raw barrel/resources.obscan
"""
import os
import sqlite3
import struct
import sys
import numpy as np
from scipy.optimize import minimize
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import connected_components

# ── Config ─────────────────────────────────────────────────────────────────────
SRC_DEFAULT = "C:/raw barrel/resources.obscan"
OUT_DIR     = "C:/raw barrel"
# each of the four output variants goes in its own subfolder of OUT_DIR
# (created if missing).  Keyed by (tag, extension).
OUT_SUBDIRS = {("clean",  "ply"): "clean_ply",
               ("axisym", "ply"): "axisym_ply",
               ("clean",  "stl"): "clean_stl",
               ("axisym", "stl"): "axisym_stl"}
N_AZ        = 720          # azimuthal grid resolution (around the axis; 0.5 deg)
N_EL_BASE   = 260          # baseline polar rows (pole to pole along the axis)
CORNER_ROWS = 30           # extra polar rows packed around each crozehead corner
SMOOTH_PASSES = 1          # light crease-preserving smoothing passes on the clean grid
CORNER_HALF_DEG = 7.0      # half-width (deg of el) of the corner densification band
MIN_CELL_PTS = 4           # min points for a wall/head cell to be trusted (else NaN)
GROSS_OUTLIER_MM = 12.0    # cell rejected if it departs its el-row median by more
                           # (kills stray corner/pole cells; keeps real <=7 mm ovality)
AXIS_NORMAL_SPLIT = 0.5    # |n.axis| above this => head point, below => wall point
BUNG_SEED_MM = 6.0         # rho excess above el-row median to seed the bung (mm)
BUNG_SHOULDER_MM = 2.0     # rho excess to include as bung shoulder inside region
MIN_BUNG_CELLS = 8         # min connected cells to treat as a real bung (not noise)
BUNG_BAND_MARGIN_DEG = 5.0 # keep bung seeding this far inside each crozehead corner
BUNG_MAX_AZ_DEG = 90.0     # reject "bungs" wider than this in azimuth (ring feature)
POLAR_DECIMATE = True      # halve azimuth resolution toward the poles so the flat
                           # heads don't get a 720-spoke pole pinch (radial-line artifact)
HEAD_POLE_MARGIN_DEG = 3.0 # keep flat-head extrapolation this far inside the crozehead
                           # corner when estimating each head plane (flatten_head_poles)
WRITE_STL   = True         # also emit a binary STL beside each PLY
STL_UNITS_MM = False       # True: scale STL metres->mm (endcap_metrics.py expects mm); False: keep metres


# ── Load ───────────────────────────────────────────────────────────────────────

def load_cloud(src):
    """Return (P Nx3, N Nx3) positions + unit normals from mesh_vn_0."""
    # immutable=1: read-only, and do NOT create -wal/-shm sidecar files
    uri = "file:%s?immutable=1" % src.replace("\\", "/")
    con = sqlite3.connect(uri, uri=True)
    cur = con.cursor()
    cur.execute("SELECT data FROM files WHERE name='mesh_vn_0.bat'")
    blob = cur.fetchone()[0][32:]                       # strip 32-byte wrapper
    con.close()
    V = int.from_bytes(blob[16:20], "big")             # vertex count
    vn = np.frombuffer(blob[20:20 + V * 28], dtype=">f4").reshape(V, 7).astype(np.float64)
    return vn[:, 0:3], vn[:, 4:7]


# ── Axis fit (robust surface-of-revolution axis) ───────────────────────────────

def _cyl(P, c, a):
    d = P - c
    z = d @ a
    r = np.linalg.norm(d - np.outer(z, a), axis=1)
    return z, r


def _frame(a):
    a = a / np.linalg.norm(a)
    t = np.array([1.0, 0, 0]) if abs(a[0]) < 0.9 else np.array([0, 1.0, 0])
    u = np.cross(a, t); u /= np.linalg.norm(u)
    w = np.cross(a, u)
    return u, w


def fit_axis(P):
    """PCA seed (pick the rotationally-uniform eigenvector) then refine axis
    direction + centre by minimising within-slice radial MAD (robust)."""
    c0 = P.mean(0)
    _, evec = np.linalg.eigh(np.cov((P - c0).T))
    best, best_score = evec[:, 0], -1.0
    for i in range(3):
        a = evec[:, i]
        z, r = _cyl(P, c0, a)
        span = z.max() - z.min()
        m = np.abs(z - z.mean()) < span * 0.07
        if m.sum() < 50:
            continue
        score = r[m].mean() / (r[m].std() + 1e-9)   # round slice => high score
        if score > best_score:
            best_score, best = score, a
    a0 = best
    u0, w0 = _frame(a0)

    rng = np.random.default_rng(0)
    Ps = P[rng.choice(len(P), min(300_000, len(P)), replace=False)]
    NB = 60

    def obj(x):
        a = a0 + x[0] * u0 + x[1] * w0; a /= np.linalg.norm(a)
        c = c0 + x[2] * u0 + x[3] * w0
        z, r = _cyl(Ps, c, a)
        edges = np.linspace(z.min(), z.max(), NB + 1)
        idx = np.clip(np.digitize(z, edges) - 1, 0, NB - 1)
        tot = 0.0
        for i in range(NB):
            rr = r[idx == i]
            if rr.size < 50:
                continue
            tot += np.median(np.abs(rr - np.median(rr)))
        return tot

    res = minimize(obj, [0, 0, 0, 0], method="Powell",
                   options={"xtol": 1e-5, "ftol": 1e-5})
    x = res.x
    a = a0 + x[0] * u0 + x[1] * w0; a /= np.linalg.norm(a)
    c = c0 + x[2] * u0 + x[3] * w0
    return c, a


# ── Spherical map about the barrel centre ──────────────────────────────────────

def spherical_coords(P, centre, a, u, w):
    """Return az[-pi,pi], el[0,pi], rho(dist) of each point about `centre`,
    with el measured from +axis `a`."""
    d = P - centre
    rho = np.linalg.norm(d, axis=1)
    t = np.clip((d @ a) / rho, -1.0, 1.0)
    el = np.arccos(t)
    az = np.arctan2(d @ w, d @ u)
    return az, el, rho


def el_bin_index(el, el_edges):
    n_el = len(el_edges) - 1
    return np.clip(np.digitize(el, el_edges) - 1, 0, n_el - 1)


def build_rho_grid(az, el, rho, n_az, el_edges, min_count=1):
    """Robust median rho per (az, el) cell, with el binned by (possibly
    non-uniform) `el_edges`.  Cells with < min_count points are NaN.
    Returns (grid, count) arrays of shape (n_el, n_az)."""
    n_el = len(el_edges) - 1
    ai = np.clip(((az + np.pi) / (2 * np.pi) * n_az).astype(int), 0, n_az - 1)
    ei = el_bin_index(el, el_edges)
    flat = ei * n_az + ai
    order = np.argsort(flat, kind="stable")
    flat_s, rho_s = flat[order], rho[order]
    bounds = np.searchsorted(flat_s, np.arange(n_el * n_az + 1))
    grid = np.full(n_el * n_az, np.nan)
    cnt = np.zeros(n_el * n_az, dtype=np.int64)
    for cell in range(n_el * n_az):
        s, e = bounds[cell], bounds[cell + 1]
        if e - s >= min_count:
            grid[cell] = np.median(rho_s[s:e])
            cnt[cell] = e - s
    return grid.reshape(n_el, n_az), cnt.reshape(n_el, n_az)


def make_el_sampling(az, el, rho, n_az):
    """Choose polar (el) sample centres: a uniform base plus dense rows packed
    around each crozehead corner (the two ρ(el) peaks), with a row landing exactly
    on each corner so the mesh has an edge-loop on the crease.
    Returns (el_centres, el_edges, (corner_top, corner_bot))."""
    eps = 0.5 * np.pi / N_EL_BASE
    base_edges = np.linspace(0, np.pi, N_EL_BASE + 1)
    g0, _ = build_rho_grid(az, el, rho, n_az, base_edges)
    m0 = np.nanmedian(g0, axis=1)
    ctr = (base_edges[:-1] + base_edges[1:]) / 2
    half = N_EL_BASE // 2
    top = np.nanargmax(np.where(ctr < np.pi / 2, m0, -np.inf))     # crozehead near +axis
    bot = np.nanargmax(np.where(ctr > np.pi / 2, m0, -np.inf))     # crozehead near -axis
    c_top, c_bot = ctr[top], ctr[bot]
    band = np.deg2rad(CORNER_HALF_DEG)
    dense = np.concatenate([np.linspace(c_top - band, c_top + band, CORNER_ROWS),
                            np.linspace(c_bot - band, c_bot + band, CORNER_ROWS),
                            [c_top, c_bot]])
    centres = np.unique(np.clip(np.concatenate([ctr, dense]), eps, np.pi - eps))
    edges = np.concatenate([[0.0], (centres[:-1] + centres[1:]) / 2, [np.pi]])
    return centres, edges, (c_top, c_bot)


def combine_wall_head(az, el, rho, wall_flag, n_az, el_edges):
    """Build wall-only and head-only rho grids and merge them, preferring the
    wall where it has data.  Because wall points stop at the crozehead and head
    points start at the crozehead, the two never blend across the corner — so the
    ρ(el) peak (the crozehead crease) is preserved sharp."""
    gw, cw = build_rho_grid(az[wall_flag], el[wall_flag], rho[wall_flag],
                            n_az, el_edges, MIN_CELL_PTS)
    gh, ch = build_rho_grid(az[~wall_flag], el[~wall_flag], rho[~wall_flag],
                            n_az, el_edges, MIN_CELL_PTS)
    comb = np.where(~np.isnan(gw), gw, gh)
    return comb, gw, gh


def flatten_head_poles(grid, el_centres, corners, margin_deg=HEAD_POLE_MARGIN_DEG):
    """Fill empty near-pole rows with the flat-head secant law (removes the
    central 'spindle' artefact at each head).

    The barrel heads are flat discs perpendicular to the axis, but the spherical
    rho(az, el) map is singular at the two poles (el→0 and el→π) — exactly where
    the flat heads sit and where measured points thin out to nothing.  A fully
    empty pole row would otherwise be filled by fill_grid's global-median
    fallback (≈ the barrel's overall radius), planting the head-centre vertex
    tens of mm out along the axis: a spurious spike that inflates the length.

    For a flat head at axial distance h from the map centre the surface obeys
    rho(el) = h / cos(el_from_pole).  We estimate h from the nearest *valid* head
    rows (median of rho·cos(el)) and use it to fill every empty head row, so the
    reconstructed head caps land flat on the head plane.  Rows that already carry
    data are left untouched.  Returns a new grid.
    """
    g = grid.copy()
    n_el = g.shape[0]
    el = np.asarray(el_centres, dtype=float)
    row_med = np.array([np.nanmedian(g[i]) if np.isfinite(g[i]).any() else np.nan
                        for i in range(n_el)])
    band = np.deg2rad(margin_deg)
    n_fixed = 0
    for is_top in (True, False):
        if is_top:                                   # angle from pole is el
            in_head = el < corners[0]
            reliable = in_head & (el > band) & np.isfinite(row_med)
        else:                                        # angle from pole is π−el
            in_head = el > corners[1]
            reliable = in_head & (el < np.pi - band) & np.isfinite(row_med)
        if reliable.sum() < 3:
            continue
        # signed axial distance to the flat head (cos(el) < 0 on the far pole)
        hp = np.median(row_med[reliable] * np.cos(el[reliable]))
        for i in np.where(in_head & ~np.isfinite(row_med))[0]:
            ci = np.cos(el[i])
            if abs(ci) > 1e-6:
                g[i, :] = hp / ci
                n_fixed += 1
    return g, n_fixed


# ── Bung detection + fill (rotational symmetry + connected component) ───────────

def _row_median_mad(grid):
    """Per el-row (constant polar angle) circular median rho + MAD, robust to
    the bung and gaps.  Row median is the rotationally-symmetric wall value."""
    n_el = grid.shape[0]
    med = np.full(n_el, np.nan)
    mad = np.full(n_el, np.nan)
    for i in range(n_el):
        v = grid[i][~np.isnan(grid[i])]
        if v.size >= 8:
            med[i] = np.median(v)
            mad[i] = np.median(np.abs(v - med[i])) + 1e-9
    return med, mad


def _az_span_cells(cols, n_az):
    """Minimal azimuthal arc (in cells) covering the given az column indices,
    accounting for wrap-around.  Full ring -> ~n_az."""
    u = np.unique(cols)
    if u.size <= 1:
        return int(u.size)
    gaps = np.diff(np.concatenate([u, [u[0] + n_az]]))
    return int(n_az - gaps.max())


def detect_bung(grid, seed_mm, min_cells, el_centres, corners, max_az_cells):
    """Find the bung: a contiguous patch rising > seed_mm above its el-row
    median.  A real bung lives on the staves (between the two crozeheads) and is
    azimuthally LOCALISED (one hole on one stave), so we (a) seed only inside
    the stave band, excluding the head caps, and (b) reject any component that
    wraps most of the way round (a full ring is the crozehead/crozebevel/head edge, not
    a bung).  Returns boolean mask (n_el, n_az) and an info dict."""
    n_el, n_az = grid.shape
    med, _ = _row_median_mad(grid)
    excess = grid - med[:, None]
    margin = np.deg2rad(BUNG_BAND_MARGIN_DEG)
    band = (el_centres > corners[0] + margin) & (el_centres < corners[1] - margin)
    seed = np.isfinite(excess) & (excess > seed_mm / 1000.0) & band[:, None]
    info = {"n_seed": int(seed.sum())}
    if seed.sum() == 0:
        return np.zeros_like(seed), info

    # 8-connectivity with azimuth wrap-around
    idx = np.argwhere(seed)
    id_of = {(int(i), int(j)): k for k, (i, j) in enumerate(idx)}
    rows, cols = [], []
    for k, (i, j) in enumerate(idx):
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                if di == 0 and dj == 0:
                    continue
                ni, nj = i + di, (j + dj) % n_az
                if 0 <= ni < n_el and (ni, nj) in id_of:
                    rows.append(k); cols.append(id_of[(ni, nj)])
    n = len(idx)
    A = csr_matrix((np.ones(len(rows)), (rows, cols)), shape=(n, n))
    _, labels = connected_components(A, directed=False)
    sizes = np.bincount(labels)
    info["sizes"] = np.sort(sizes)[::-1][:5].tolist()
    mask = np.zeros((n_el, n_az), dtype=bool)

    # largest component that is big enough AND azimuthally compact (a bung,
    # not a ring feature); skip ring-like components
    for lab in np.argsort(sizes)[::-1]:
        if sizes[lab] < min_cells:
            break
        sel = labels == lab
        cells = idx[sel]
        span = _az_span_cells(cells[:, 1], n_az)
        if span > max_az_cells:
            info.setdefault("rejected_ring", int(sizes[lab]))
            continue
        for (i, j) in cells:
            mask[i, j] = True
        info["bung_cells"] = int(sel.sum())
        info["az_span_deg"] = float(span / n_az * 360.0)
        info["el_deg"] = float(np.rad2deg(el_centres[cells[:, 0]].mean()))
        info["az_deg"] = float(cells[:, 1].mean() / n_az * 360.0 - 180.0)
        break
    return mask, info


def smooth_grid(grid, el_centres, corners, passes):
    """Light smoothing to remove sub-mm cell-to-cell facet noise WITHOUT
    rounding the crozehead crease: azimuthal smoothing is within-row (never crosses
    the crease), and polar smoothing is done separately within the three el
    segments split at the two corners, so the corners stay sharp."""
    if passes <= 0:
        return grid
    i_ct = int(np.argmin(np.abs(el_centres - corners[0])))
    i_cb = int(np.argmin(np.abs(el_centres - corners[1])))
    segs = [(0, i_ct + 1), (i_ct, i_cb + 1), (i_cb, len(el_centres))]
    g = grid.copy()
    for _ in range(passes):
        g = (np.roll(g, 1, axis=1) + 2 * g + np.roll(g, -1, axis=1)) / 4.0
        out = g.copy()
        for lo, hi in segs:
            s = g[lo:hi]
            if len(s) >= 3:
                out[lo + 1:hi - 1] = (s[:-2] + 2 * s[1:-1] + s[2:]) / 4.0
        g = out
    return g


def fill_grid(grid, bad_mask):
    """Replace bad/empty cells with the rotationally-symmetric wall value:
    fill each row from its valid cells (circular linear interp), falling back
    to the row median.  Preserves genuine, gentle azimuthal asymmetry."""
    g = grid.copy()
    g[bad_mask] = np.nan
    med, _ = _row_median_mad(g)
    n_el, n_az = g.shape
    for i in range(n_el):
        row = g[i]
        valid = np.where(~np.isnan(row))[0]
        if valid.size == 0:
            g[i] = med[i] if np.isfinite(med[i]) else np.nanmedian(med)
            continue
        if valid.size < n_az:
            # circular interpolation over azimuth
            ext_x = np.concatenate([valid, valid[:1] + n_az])
            ext_y = np.concatenate([row[valid], row[valid[:1]]])
            allx = np.arange(n_az)
            g[i] = np.interp(allx, ext_x, ext_y, period=n_az)
    return g


# ── Mesh assembly (closed sphere, polar azimuth decimation) ─────────────────────

def _az_counts(el_centres, n_az):
    """Per-row azimuth vertex count: n_az near the equator, halved toward each
    pole so cells stay ~square and the flat heads don't collapse 720 meridians
    into one point.  Only power-of-two divisors of n_az, changing by at most one
    factor of two between adjacent rows (keeps ring-to-ring stitching 1:1 or 2:1)."""
    if not POLAR_DECIMATE:
        return np.full(len(el_centres), n_az, dtype=np.int64)
    maxlev, t = 0, n_az
    while t % 2 == 0 and t // 2 >= 8:              # keep >= 8 verts per ring
        t //= 2; maxlev += 1
    s = np.sin(np.asarray(el_centres))
    lev = np.clip(np.floor(-np.log2(np.clip(s, 1e-6, 1.0))).astype(int), 0, maxlev)
    eq = int(np.argmax(s))                         # equator row (full resolution)
    for i in range(eq + 1, len(lev)):              # spread reductions outward
        lev[i] = min(lev[i], lev[i - 1] + 1)
    for i in range(eq - 1, -1, -1):
        lev[i] = min(lev[i], lev[i + 1] + 1)
    return (n_az // (2 ** lev)).astype(np.int64)


def grid_to_mesh(grid, centre, a, u, w, n_az, el_centres):
    """Turn rho(el, az) into a watertight triangle mesh.  Azimuth resolution is
    decimated toward the poles (see _az_counts) to avoid the radial pole pinch on
    the heads; adjacent rings connect 1:1 (quad strip) or 2:1 (transition fan)."""
    el = np.asarray(el_centres)
    n_el = len(el)
    counts = _az_counts(el, n_az)

    ring_off = np.zeros(n_el + 1, dtype=np.int64)
    chunks = []
    for i in range(n_el):
        m = int(counts[i]); step = n_az // m
        cols = (np.arange(m)[:, None] * step + np.arange(step)[None, :] - step // 2) % n_az
        rho = grid[i][cols].mean(1)                # average the fine cells per coarse vertex
        azj = -np.pi + 2 * np.pi * np.arange(m) / m
        dirs = (np.cos(el[i]) * a[None, :]
                + np.sin(el[i]) * (np.cos(azj)[:, None] * u[None, :]
                                   + np.sin(azj)[:, None] * w[None, :]))
        chunks.append(centre + rho[:, None] * dirs)
        ring_off[i + 1] = ring_off[i] + m
    verts = np.vstack(chunks)
    top = centre + np.nanmedian(grid[0]) * a
    bot = centre - np.nanmedian(grid[-1]) * a
    top_id = len(verts); bot_id = top_id + 1
    verts = np.vstack([verts, top, bot])

    faces = []
    for i in range(n_el - 1):
        mi, mj = int(counts[i]), int(counts[i + 1])
        A, B = ring_off[i], ring_off[i + 1]
        if mi == mj:
            for j in range(mi):
                a0, a1 = A + j, A + (j + 1) % mi
                b0, b1 = B + j, B + (j + 1) % mi
                faces.append((a0, b0, b1)); faces.append((a0, b1, a1))
        else:                                      # 2:1 transition
            if mi > mj:
                fo, mf, co, mc = A, mi, B, mj
            else:
                fo, mf, co, mc = B, mj, A, mi
            for k in range(mc):
                c0, c1 = co + k, co + (k + 1) % mc
                f0 = fo + (2 * k) % mf; f1 = fo + (2 * k + 1) % mf; f2 = fo + (2 * k + 2) % mf
                faces.append((c0, f0, f1)); faces.append((c0, f1, f2)); faces.append((c0, f2, c1))
    m0 = int(counts[0]); A0 = ring_off[0]
    for j in range(m0):                            # cap near +axis pole
        faces.append((top_id, A0 + (j + 1) % m0, A0 + j))
    mn = int(counts[-1]); An = ring_off[n_el - 1]
    for j in range(mn):                            # cap near -axis pole
        faces.append((bot_id, An + j, An + (j + 1) % mn))
    faces = np.asarray(faces, dtype=np.int64)

    # orient every face outward (the surface is star-shaped from `centre`), so
    # winding is globally consistent regardless of how strips/transitions/caps
    # were built -- otherwise signed_volume is wrong.
    fc = verts[faces].mean(1)
    fn = np.cross(verts[faces[:, 1]] - verts[faces[:, 0]],
                  verts[faces[:, 2]] - verts[faces[:, 0]])
    flip = np.einsum("ij,ij->i", fn, fc - centre) < 0
    faces[flip] = faces[flip][:, ::-1]
    return verts, faces


# ── Mesh metrics ───────────────────────────────────────────────────────────────

def vertex_normals(verts, faces):
    fn = np.cross(verts[faces[:, 1]] - verts[faces[:, 0]],
                  verts[faces[:, 2]] - verts[faces[:, 0]])
    vn = np.zeros_like(verts)
    for k in range(3):
        np.add.at(vn, faces[:, k], fn)
    ln = np.linalg.norm(vn, axis=1, keepdims=True); ln[ln == 0] = 1
    return vn / ln


def signed_volume(verts, faces):
    v0, v1, v2 = verts[faces[:, 0]], verts[faces[:, 1]], verts[faces[:, 2]]
    return float(np.einsum("ij,ij->i", v0, np.cross(v1, v2)).sum() / 6.0)


def surface_area(verts, faces):
    v0, v1, v2 = verts[faces[:, 0]], verts[faces[:, 1]], verts[faces[:, 2]]
    return float(0.5 * np.linalg.norm(np.cross(v1 - v0, v2 - v0), axis=1).sum())


def is_watertight(faces):
    """Closed & manifold iff every undirected edge is shared by exactly 2 faces."""
    e = np.vstack([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]])
    e = np.sort(e, axis=1)
    _, counts = np.unique(e, axis=0, return_counts=True)
    return bool(np.all(counts == 2)), int((counts != 2).sum())


# ── PLY writer ─────────────────────────────────────────────────────────────────

def write_ply(path, verts, faces, normals=None):
    V, F = len(verts), len(faces)
    props = "property float x\nproperty float y\nproperty float z\n"
    if normals is not None:
        props += "property float nx\nproperty float ny\nproperty float nz\n"
        vb = np.empty((V, 6), "<f4"); vb[:, :3] = verts; vb[:, 3:] = normals
    else:
        vb = verts.astype("<f4")
    fb = np.empty(F, dtype=[("n", "u1"), ("v", "<u4", 3)])
    fb["n"] = 3; fb["v"] = faces.astype("<u4")
    hdr = ("ply\nformat binary_little_endian 1.0\n"
           "comment barrel reconstructed from resources.obscan fused cloud\n"
           "element vertex %d\n%s"
           "element face %d\nproperty list uchar uint vertex_indices\n"
           "end_header\n") % (V, props, F)
    with open(path, "wb") as f:
        f.write(hdr.encode("ascii"))
        f.write(vb.tobytes())
        f.write(fb.tobytes())


# ── STL writer ─────────────────────────────────────────────────────────────────

_STL_DT = np.dtype([("normal", "<f4", 3), ("v0", "<f4", 3),
                    ("v1", "<f4", 3), ("v2", "<f4", 3), ("attr", "<u2")])


def write_stl(path, verts, faces, scale=1.0):
    """Write a binary STL.  `scale` multiplies coordinates (1000 => metres->mm)."""
    v = verts * scale
    v0, v1, v2 = v[faces[:, 0]], v[faces[:, 1]], v[faces[:, 2]]
    n = np.cross(v1 - v0, v2 - v0)
    ln = np.linalg.norm(n, axis=1, keepdims=True); ln[ln == 0] = 1.0
    rec = np.zeros(len(faces), dtype=_STL_DT)
    rec["normal"] = n / ln; rec["v0"] = v0; rec["v1"] = v1; rec["v2"] = v2
    with open(path, "wb") as f:
        f.write(b"barrel_reconstruct".ljust(80)[:80])
        f.write(struct.pack("<I", len(faces)))
        rec.tofile(f)


# ── Per-file reconstruction ────────────────────────────────────────────────────

def out_path(tag, ext, stem):
    """Full path for an output variant, creating its subfolder if needed."""
    d = os.path.join(OUT_DIR, OUT_SUBDIRS[(tag, ext)])
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "%s_barrel_%s.%s" % (stem, tag, ext))


def reconstruct_one(src):
    """Reconstruct one .obscan; write <stem>_barrel_clean/axisym.ply to OUT_DIR.
    Returns a summary dict."""
    stem = os.path.splitext(os.path.basename(src))[0]
    print("\n" + "=" * 70)
    print("%s" % src)
    P, N = load_cloud(src)
    print("  %d vertices" % len(P))

    centre, a = fit_axis(P)
    u, w = _frame(a)
    z, r = _cyl(P, centre, a)
    L = z.max() - z.min()
    naxis = np.abs(N @ a)
    n_wall = int((naxis < AXIS_NORMAL_SPLIT).sum())
    print("  axis dir [%.4f %.4f %.4f]  length %.4f m" % (a[0], a[1], a[2], L))
    print("  wall points %d (%.1f%%), head points %d (%.1f%%)"
          % (n_wall, 100 * n_wall / len(P), len(P) - n_wall, 100 * (len(P) - n_wall) / len(P)))

    # centre the spherical map at the mid-length point on the axis
    mid = centre + (z.mean()) * a
    az, el, rho = spherical_coords(P, mid, a, u, w)
    wall_flag = naxis < AXIS_NORMAL_SPLIT

    # polar sampling with rows packed on the two crozehead corners (crisp crease)
    el_ctr, el_edges, corners = make_el_sampling(az, el, rho, N_AZ)
    n_el = len(el_ctr)
    print("  polar rows: %d (base %d + corner packs); crozeheads at el=%.1f/%.1f deg"
          % (n_el, N_EL_BASE, np.rad2deg(corners[0]), np.rad2deg(corners[1])))

    # separate wall / head height fields so they never blend across the corner
    grid, gw, gh = combine_wall_head(az, el, rho, wall_flag, N_AZ, el_edges)
    # flat-head extrapolation of empty pole rows (kills the central head spindle)
    grid, n_pole_fixed = flatten_head_poles(grid, el_ctr, corners)
    filled_empty = int(np.isnan(grid).sum())
    print("  grid %dx%d cells, %d empty before fill (%.2f%%); "
          "flat-head pole rows fixed: %d"
          % (n_el, N_AZ, filled_empty, 100 * filled_empty / grid.size, n_pole_fixed))

    bung_mask, info = detect_bung(grid, BUNG_SEED_MM, MIN_BUNG_CELLS, el_ctr,
                                  corners, int(BUNG_MAX_AZ_DEG / 360.0 * N_AZ))
    if info.get("bung_cells"):
        print("  bung: %d cells at el=%.1f deg, az=%.1f deg (span %.0f deg; clusters %s)"
              % (info["bung_cells"], info["el_deg"], info["az_deg"],
                 info["az_span_deg"], info["sizes"]))
    else:
        why = "ring feature rejected" if info.get("rejected_ring") else "none over threshold"
        print("  bung: %s (seed clusters %s)" % (why, info.get("sizes")))

    # reject gross per-row outliers (stray corner/pole cells) but keep genuine
    # gentle azimuthal asymmetry (ovality); then fill from the symmetric wall
    med_r, _ = _row_median_mad(grid)
    gross = np.isfinite(grid) & (np.abs(grid - med_r[:, None]) > GROSS_OUTLIER_MM / 1000.0)
    print("  gross outlier cells rejected: %d" % int(gross.sum()))
    bad = bung_mask | np.isnan(grid) | gross
    clean = fill_grid(grid, bad)
    clean = smooth_grid(clean, el_ctr, corners, SMOOTH_PASSES)

    # idealized: az-median per el-row => surface of revolution on the same grid
    axisym = np.repeat(np.nanmedian(clean, axis=1)[:, None], N_AZ, axis=1)

    # asymmetry: how far the real barrel departs from a perfect solid of revolution
    asym = clean - axisym
    print("  asymmetry (clean vs axisym): RMS %.2f mm, max %.2f mm"
          % (1000 * np.sqrt((asym ** 2).mean()), 1000 * np.abs(asym).max()))

    # fidelity: residual of every point to the reconstructed clean surface
    ai = np.clip(((az + np.pi) / (2 * np.pi) * N_AZ).astype(int), 0, N_AZ - 1)
    ei = el_bin_index(el, el_edges)
    resid = rho - clean[ei, ai]
    keep = ~bad[ei, ai]                              # exclude filled bung cells
    rr = resid[keep]
    # crozehead-region fidelity: points within CORNER_HALF_DEG of either corner
    near = keep & ((np.abs(el - corners[0]) < np.deg2rad(CORNER_HALF_DEG)) |
                   (np.abs(el - corners[1]) < np.deg2rad(CORNER_HALF_DEG)))
    cr = resid[near]
    print("  fidelity to raw points: RMS %.2f mm, 95%%=%.2f mm, max %.2f mm"
          % (1000 * np.sqrt((rr ** 2).mean()),
             1000 * np.percentile(np.abs(rr), 95), 1000 * np.abs(rr).max()))
    print("  crozehead-band fidelity:    RMS %.2f mm, max %.2f mm"
          % (1000 * np.sqrt((cr ** 2).mean()), 1000 * np.abs(cr).max()))

    result = {"src": src, "stem": stem, "verts_in": len(P), "length": L,
              "fidelity_rms": 1000 * np.sqrt((rr ** 2).mean()),
              "asym_rms": 1000 * np.sqrt((asym ** 2).mean()),
              "bung": info.get("bung_cells", 0)}
    for tag, g in (("clean", clean), ("axisym", axisym)):
        verts, faces = grid_to_mesh(g, mid, a, u, w, N_AZ, el_ctr)
        wt, bad_edges = is_watertight(faces)
        vol = abs(signed_volume(verts, faces))
        area = surface_area(verts, faces)
        nrm = vertex_normals(verts, faces)
        path = out_path(tag, "ply", stem)
        write_ply(path, verts, faces, nrm)
        out_names = os.path.relpath(path, OUT_DIR)
        if WRITE_STL:
            stl_path = out_path(tag, "stl", stem)
            write_stl(stl_path, verts, faces, 1000.0 if STL_UNITS_MM else 1.0)
            out_names += " + %s (%s)" % (os.path.relpath(stl_path, OUT_DIR),
                                         "mm" if STL_UNITS_MM else "m")
        result[tag] = {"path": path, "v": len(verts), "f": len(faces),
                       "watertight": wt, "vol_L": vol * 1000.0, "area": area}
        print("  [%s] %s" % (tag, out_names))
        print("     %d v / %d f  watertight=%s  volume %.3f L  area %.4f m^2"
              % (len(verts), len(faces), wt, vol * 1000.0, area))
    return result


# ── Batch driver ────────────────────────────────────────────────────────────────

def main():
    import glob
    args = [a for a in sys.argv[1:]]
    if args:
        files = args
    else:                                            # batch: every .obscan in OUT_DIR
        files = sorted(glob.glob(os.path.join(OUT_DIR, "*.obscan")))
    if not files:
        sys.exit("no .obscan found in %s" % OUT_DIR)
    print("Batch reconstructing %d scan(s) from %s" % (len(files), OUT_DIR))
    for sub in sorted(set(OUT_SUBDIRS.values())):    # ensure output folders exist
        os.makedirs(os.path.join(OUT_DIR, sub), exist_ok=True)
    print("Output folders: %s" % ", ".join(sorted(set(OUT_SUBDIRS.values()))))

    results = []
    for src in files:
        try:
            results.append(reconstruct_one(src))
        except Exception as exc:                     # keep the batch going
            print("  !! FAILED %s: %s" % (src, exc))

    print("\n" + "=" * 70)
    print("BATCH SUMMARY  (%d scan(s))" % len(results))
    print("  %-22s %8s %7s %6s %8s %8s %6s" %
          ("scan", "verts_in", "len_m", "bung", "fid_rms", "clean_L", "wtight"))
    for r in results:
        print("  %-22s %8d %7.3f %6d %7.2fm %8.2f %6s" %
              (r["stem"], r["verts_in"], r["length"], r["bung"],
               r["fidelity_rms"], r["clean"]["vol_L"], r["clean"]["watertight"]))


if __name__ == "__main__":
    main()
