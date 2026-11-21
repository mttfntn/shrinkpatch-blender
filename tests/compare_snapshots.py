"""Compare two behaviour snapshots (plain Python, no Blender needed).

    python tests/compare_snapshots.py OLD.json NEW.json [--tol 1e-3]

Everything that is not a number (modifier stacks, names, groups, errors) must
match exactly. Numbers get the tolerance measured between two runs of the SAME
version (Blender's threaded evaluation is not bit-for-bit repeatable):
  * patch, cutter, transforms: relative 1e-4 (measured noise: 5e-5);
  * the cut surface is recorded by its shape (area and ray hits), not its
    vertices: the Manifold Boolean re-triangulates it a little differently on each
    run. Area: relative 1e-4; ray hits: 1e-3 (a ray that grazes the cut edge can
    land on either side of it, which the comparison reports for inspection).
Prints every difference with its path and exits non-zero if there is any.
"""

import argparse
import json
import sys

RAY_TOL = 1e-3


def numbers_differ(a, b, path, tol):
    if "/surface_shape/radial" in path or "/surface_shape/grid" in path:
        return abs(a - b) > RAY_TOL
    return abs(a - b) > tol * max(1.0, abs(a), abs(b))


def diff(a, b, path, tol, out):
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                out.append(f"{path}/{k}: only in {'new' if k not in a else 'old'}")
            else:
                diff(a[k], b[k], f"{path}/{k}", tol, out)
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append(f"{path}: length {len(a)} -> {len(b)}")
        for i, (x, y) in enumerate(zip(a, b)):
            diff(x, y, f"{path}[{i}]", tol, out)
    elif isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        if numbers_differ(a, b, path, tol):
            out.append(f"{path}: {a} -> {b}")
    elif a != b:
        out.append(f"{path}: {a!r} -> {b!r}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("old")
    ap.add_argument("new")
    ap.add_argument("--tol", type=float, default=1e-4)
    args = ap.parse_args()
    old, new = (json.load(open(p)) for p in (args.old, args.new))
    out = []
    diff(old, new, "", args.tol, out)
    for line in out:
        print(line)
    print(f"COMPARE cases={len(old)} differences={len(out)}")
    sys.exit(1 if out else 0)


if __name__ == "__main__":
    main()
