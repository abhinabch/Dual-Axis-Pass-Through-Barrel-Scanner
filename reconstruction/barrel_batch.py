"""Standalone (no-Blender) barrel pipeline for a whole directory of STL files.

For every ``*.stl`` in the input directory it:
  1. loads the mesh (trimesh),
  2. runs the bung-removal / cleanup port (``barrel_clean``),
  3. runs the ORIGINAL ``barrel_pipeline.run_crozehead_analysis`` unchanged, behind a
     thin Blender compatibility shim (``blender_stub``),
  4. collects the per-barrel measurement + radial-profile results.

Outputs (in the output directory, default ``<input>/barrel_output``):
  * ``measurements_summary.csv`` — one row per STL, one column per measurement
    (the "summarized measurement" table).
  * ``profiles_combined.csv``     — every barrel's crozebevel-to-crozebevel radial profile,
    stacked, with a ``source_file`` column.
  * ``per_file/<name>_measurements.csv`` / ``_profile.csv`` — the full detailed
    per-barrel CSVs (identical format to the Blender pipeline), plus ``<name>.log``.

Usage:
    python barrel_batch.py <input_dir> [--out DIR] [--max-passes N]
                           [--no-clean] [--pattern GLOB] [--verbose]
"""

import argparse
import contextlib
import csv
import gc
import glob
import os
import sys
import time

import numpy as np
import trimesh

import blender_stub
blender_stub.install()          # must precede `import barrel_pipeline`

# barrel_pipeline.py is Blender code (it `import bpy`), reused here for its analysis
# math behind the blender_stub shim.  After the repo was split into sibling
# `blender/` and `standalone/` folders it lives one level up in `blender/`; add
# that to sys.path so the import resolves either way (also works if kept alongside).
_BLENDER_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "blender")
if os.path.isdir(_BLENDER_DIR) and _BLENDER_DIR not in sys.path:
    sys.path.insert(0, _BLENDER_DIR)

import barrel_pipeline as bp     # noqa: E402  (imported after shim install + path)
import barrel_clean              # noqa: E402
from blender_stub import Vector, Matrix  # noqa: E402


# ── shim mesh object that `run_crozehead_analysis` can read ──────────────────────
# Lazy wrappers: no per-vertex / per-triangle Python objects are stored, so a
# million-face mesh does not blow up memory.  `v.co` hands back a raw numpy row
# (Matrix.__matmul__ accepts array-likes); `t.vertices` hands back a row too.
class _Vert:
    __slots__ = ("co",)

    def __init__(self, co):
        self.co = co                      # numpy row view; identity matrix @ row is fine


class _Tri:
    __slots__ = ("vertices",)

    def __init__(self, row):
        self.vertices = row               # supports row[:] → the 3 vertex indices


class _VertProxy:
    def __init__(self, V):
        self._V = V

    def __len__(self):
        return len(self._V)

    def __iter__(self):
        return (_Vert(row) for row in self._V)

    def foreach_get(self, attr, out):     # attr is always 'co'
        out[:] = self._V.reshape(-1)


class _TriSeq:
    def __init__(self, F):
        self._F = F

    def __len__(self):
        return len(self._F)

    def __iter__(self):
        return (_Tri(row) for row in self._F)


class _MeshData:
    def __init__(self, V, F):
        self._V = np.ascontiguousarray(V, dtype=np.float64)
        self._F = np.ascontiguousarray(F, dtype=np.int64)
        self.vertices = _VertProxy(self._V)
        self.loop_triangles = _TriSeq(self._F)

    def calc_loop_triangles(self):
        pass

    def update(self):
        pass


class _Obj:
    def __init__(self, name, V, F):
        self.name = name
        self.type = "MESH"
        self.data = _MeshData(V, F)
        self.matrix_world = Matrix()      # identity — verts are already world-space


# ── STL loading ───────────────────────────────────────────────────────────────
def load_stl(path):
    """Return welded (V, F) arrays from an STL (topology merged for adjacency)."""
    m = trimesh.load(path, force="mesh")
    if isinstance(m, trimesh.Scene):
        m = m.dump(concatenate=True)
    V = np.asarray(m.vertices, dtype=np.float64)
    F = np.asarray(m.faces, dtype=np.int64)
    return V, F


def detect_scale(V, target=1.0):
    """Nearest power-of-ten factor that brings the mesh's longest extent near
    `target` metres.  The pipeline expects metre-scale coordinates (its 20 mm
    profile step and m/mm labels only line up then).  Returns 1.0 for meshes
    already in the right range, so metre inputs are left untouched."""
    import math
    ext = float((V.max(axis=0) - V.min(axis=0)).max())
    if ext <= 0:
        return 1.0
    return 10.0 ** round(math.log10(target / ext))


# ── per-file measurement / profile CSV parsing ───────────────────────────────
def read_measurements(path):
    """Parse a per-file _measurements.csv into an ordered [(param, value, unit)]."""
    rows = []
    with open(path, newline="") as f:
        for r in csv.reader(f):
            if not r or r[0] in ("", "parameter"):
                continue
            param = r[0]
            value = r[2] if len(r) > 2 else ""
            unit = r[3] if len(r) > 3 else ""
            rows.append((param, value, unit))
    return rows


def read_profile(path):
    with open(path, newline="") as f:
        rows = list(csv.reader(f))
    if not rows:
        return [], []
    return rows[0], rows[1:]


def collect_from_disk(per_file_dir):
    """Rebuild records from previously written per-file CSVs (for --rebuild-only).
    source_file is the per-file base name (spaces were replaced with '_')."""
    records = []
    for mpath in sorted(glob.glob(os.path.join(per_file_dir, "*_measurements.csv"))):
        name = os.path.basename(mpath)[:-len("_measurements.csv")]
        ppath = os.path.join(per_file_dir, name + "_profile.csv")
        ph, pr = read_profile(ppath) if os.path.exists(ppath) else ([], [])
        records.append(dict(name=name, ok=True, meas=read_measurements(mpath),
                            prof_header=ph, prof_rows=pr))
    return records


def _unit_slug(u):
    return (u.replace("³", "3").replace("%", "pct").replace(" ", "").strip()) or "na"


def write_combined(records, out_dir):
    """Write measurements_summary.csv (wide, one row/barrel) and
    profiles_combined.csv (stacked).  Measurement columns are keyed by
    (parameter, unit) so multi-unit rows like volume are not collapsed."""
    import collections

    key_order, seen = [], set()
    for r in records:
        for param, _val, unit in r["meas"]:
            k = (param, unit)
            if k not in seen:
                seen.add(k)
                key_order.append(k)
    pcount = collections.Counter(p for p, _u in key_order)
    headers = [p if pcount[p] == 1 else f"{p}_{_unit_slug(u)}" for p, u in key_order]

    summary_path = os.path.join(out_dir, "measurements_summary.csv")
    with open(summary_path, "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["source_file"] + headers)
        wr.writerow(["unit"] + [u for _p, u in key_order])
        for r in records:
            vals = {(p, u): v for p, v, u in r["meas"]}
            wr.writerow([r["name"]] + [vals.get(k, "") for k in key_order])

    prof_header = next((r["prof_header"] for r in records if r["prof_header"]), [])
    profiles_path = os.path.join(out_dir, "profiles_combined.csv")
    with open(profiles_path, "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["source_file"] + prof_header)
        for r in records:
            for row in r["prof_rows"]:
                wr.writerow([r["name"]] + row)

    return summary_path, profiles_path


# ── driver ────────────────────────────────────────────────────────────────────
def process_file(path, per_file_dir, max_passes, do_clean, verbose,
                 overlap="auto", overlap_max_faces=200_000, scale="auto",
                 despindle=True):
    name = os.path.splitext(os.path.basename(path))[0]
    safe = name.replace(" ", "_")
    base = os.path.join(per_file_dir, safe)
    log_path = base + ".log"

    t0 = time.monotonic()
    logf = open(log_path, "w", encoding="utf-8")

    def emit(msg):                        # concise console line + full log
        logf.write(msg + "\n")

    class _Tee:
        def write(self, s):
            logf.write(s)
            if verbose:
                sys.__stdout__.write(s)

        def flush(self):
            logf.flush()

    try:
        V, F = load_stl(path)
        emit(f"loaded {path}: {len(V):,} verts, {len(F):,} faces")

        factor = detect_scale(V) if scale == "auto" else float(scale)
        if factor != 1.0:
            V = V * factor
            print(f"       scaled x{factor:g} (raw extent -> "
                  f"{float((V.max(0) - V.min(0)).max()):.4f} m)")
            emit(f"applied scale factor {factor:g}")

        with contextlib.redirect_stdout(_Tee()):
            if do_clean:
                V, F = barrel_clean.clean_mesh(V, F, bp, max_passes=max_passes,
                                               verbose=True, overlap=overlap,
                                               overlap_max_faces=overlap_max_faces,
                                               despindle=despindle)
            bp.bpy.data.filepath = base + ".blend"   # → writes base + _measurements/_profile.csv
            obj = _Obj(name, V, F)
            n_tris = len(F)
            bp.run_crozehead_analysis(obj)
            del obj, V, F

        meas = read_measurements(base + "_measurements.csv")
        prof_header, prof_rows = ([], [])
        if os.path.exists(base + "_profile.csv"):
            prof_header, prof_rows = read_profile(base + "_profile.csv")

        dt = time.monotonic() - t0
        print(f"  OK   {name}  ({n_tris:,} tris, {len(prof_rows)} profile rows, {dt:.1f}s)")
        return dict(name=name, meas=meas, prof_header=prof_header,
                    prof_rows=prof_rows, ok=True)
    except Exception as exc:
        import traceback
        traceback.print_exc(file=logf)
        print(f"  FAIL {name}: {exc}  (see {log_path})")
        return dict(name=name, ok=False, error=str(exc))
    finally:
        logf.close()
        gc.collect()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Batch barrel STL analysis (no Blender).")
    ap.add_argument("input_dir", help="directory containing .stl files")
    ap.add_argument("--out", default=None, help="output directory "
                    "(default: <input_dir>/barrel_output)")
    ap.add_argument("--pattern", default="*.stl", help="glob for STL files")
    ap.add_argument("--max-passes", type=int, default=3,
                    help="max bung detect/remove/fill passes (default 3)")
    ap.add_argument("--no-clean", action="store_true",
                    help="skip bung removal / cleanup, analyse the raw mesh")
    ap.add_argument("--no-despindle", action="store_true",
                    help="do not remove the artificial central spike at each head")
    ap.add_argument("--overlap", choices=("auto", "on", "off"), default="auto",
                    help="internal/overlap face removal (ray casting). 'auto' "
                         "skips it on meshes above --overlap-max-faces (default)")
    ap.add_argument("--overlap-max-faces", type=int, default=200_000,
                    help="in --overlap auto, skip overlap removal above this face count")
    ap.add_argument("--scale", default="auto",
                    help="'auto' (per-file nearest power-of-10 to metres, default), "
                         "or a fixed float factor (use 1 to disable rescaling)")
    ap.add_argument("--verbose", action="store_true",
                    help="echo full per-file pipeline output to the console")
    ap.add_argument("--rebuild-only", action="store_true",
                    help="skip analysis; just re-merge existing per-file CSVs into "
                         "the summary/combined outputs")
    args = ap.parse_args(argv)

    in_dir = args.input_dir
    out_dir = args.out or os.path.join(in_dir, "barrel_output")
    per_file_dir = os.path.join(out_dir, "per_file")
    os.makedirs(per_file_dir, exist_ok=True)

    stls = sorted(glob.glob(os.path.join(in_dir, args.pattern)))
    if not stls:
        print(f"No files matching {args.pattern!r} in {in_dir}")
        return 1
    print(f"Found {len(stls)} STL file(s) in {in_dir}")
    print(f"Output -> {out_dir}\n")

    if args.rebuild_only:
        ok = collect_from_disk(per_file_dir)
        n_total = len(ok)
        print(f"Rebuild-only: merging {n_total} existing per-file result(s)")
    else:
        results = []
        for path in stls:
            print(f"- {os.path.basename(path)}")
            results.append(process_file(path, per_file_dir, args.max_passes,
                                        not args.no_clean, args.verbose,
                                        overlap=args.overlap,
                                        overlap_max_faces=args.overlap_max_faces,
                                        scale=args.scale,
                                        despindle=not args.no_despindle))
        ok = [r for r in results if r.get("ok")]
        n_total = len(results)

    summary_path, profiles_path = write_combined(ok, out_dir)

    print(f"\nDone: {len(ok)}/{n_total} analysed.")
    print(f"  measurements summary -> {summary_path}")
    print(f"  combined profiles    -> {profiles_path}")
    print(f"  per-file detail      -> {per_file_dir}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
