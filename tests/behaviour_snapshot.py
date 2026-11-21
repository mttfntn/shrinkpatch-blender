"""Record what Shrink Patch produces for a broad set of options (runs inside Blender).

    python tests/run.py --script tests/behaviour_snapshot.py --addon-dir DIR -- OUT.json

For every combination of the main options and both kinds of patch, it builds a
fresh scene, applies the patch, snaps it, toggles the stack order, moves it along
the surface and lets the live sync run, then records modifiers, transforms and
fingerprints of the evaluated geometry. Comparing the JSON of two versions
(tests/compare_snapshots.py) shows whether a change altered behaviour.
"""

import importlib
import itertools
import json
import math
import os
import sys

import bpy
from mathutils import Vector

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fixtures as fx  # noqa: E402

sys.path.insert(0, os.environ["SHRINKPATCH_ADDON_DIR"])
sp = importlib.import_module("shrinkpatch")

OUT = sys.argv[sys.argv.index("--") + 1]
OPTIONS = {
    "wrap_method": ['PROJECT', 'NEAREST_SURFACEPOINT', 'TARGET_PROJECT'],
    "conform_whole": [False, True],
    "subdiv_after_wrap": [True, False],
    "auto_cut": [True, False],
    "transfer_normals": [True, False],
    "parent_to_surface": [True, False],
}


def r(x, n=5):
    return round(float(x), n)


def mesh_fingerprint(ob):
    """Vertex/face counts and moments of the evaluated mesh (world space)."""
    if ob is None:
        return None
    verts = fx.evaluated_world_verts(ob)
    dg = bpy.context.evaluated_depsgraph_get()
    n_faces = len(ob.evaluated_get(dg).data.polygons)
    s = [sum(v[i] for v in verts) for i in range(3)]
    s2 = sum(v.length_squared for v in verts)
    return {"verts": len(verts), "faces": n_faces, "sum": [r(x, 3) for x in s], "sum2": r(s2, 3)}


def _fibonacci_directions(n):
    golden = math.pi * (3.0 - math.sqrt(5.0))
    for i in range(n):
        z = 1.0 - 2.0 * (i + 0.5) / n
        rr = math.sqrt(1.0 - z * z)
        yield Vector((rr * math.cos(golden * i), rr * math.sin(golden * i), z))


def surface_shape(surface, patch):
    """Shape of the (cut) surface, independent of how the Boolean triangulates it:
    total area, 256 rays from the centre and an 11x11 grid of rays over the patch."""
    dg = bpy.context.evaluated_depsgraph_get()
    ev = surface.evaluated_get(dg)
    mw, imw = surface.matrix_world, surface.matrix_world.inverted()
    area = sum(p.area for p in ev.data.polygons) * mw.to_scale().x ** 2
    centre = mw.translation

    def hit(start, direction):
        ok, loc, _, _ = ev.ray_cast(imw @ start, (imw.to_3x3() @ direction).normalized())
        return r(((mw @ loc) - start).length, 4) if ok else None

    radial = [hit(centre + d * 10.0, -d) for d in _fibonacci_directions(256)]
    pmw = patch.matrix_world
    down = (pmw.to_3x3() @ Vector((0, 0, -1))).normalized()
    grid = [hit(pmw @ Vector((i / 5 * 0.95, j / 5 * 0.95, 2.0)), down) for i in range(-5, 6) for j in range(-5, 6)]
    return {"area": r(area, 3), "radial": radial, "grid": grid}


def modifier_state(m):
    d = {"name": m.name, "type": m.type, "show": m.show_viewport}
    for k in ("vertex_group", "wrap_method", "wrap_mode", "offset", "loop_mapping", "mix_mode", "levels",
              "render_levels", "show_only_control_edges", "operation", "operand_type", "solver"):
        if hasattr(m, k):
            v = getattr(m, k)
            d[k] = r(v) if isinstance(v, float) else v
    for k in ("target", "object", "collection"):
        v = getattr(m, k, None)
        if v is not None:
            d[k] = v.name
    if m.type == 'DATA_TRANSFER':
        d["data_types_loops"] = sorted(m.data_types_loops)
    return d


def state(surface, patch):
    cut = sp.autocut_of(patch)
    return {
        "patch_modifiers": [modifier_state(m) for m in patch.modifiers],
        "surface_modifiers": [modifier_state(m) for m in surface.modifiers],
        "proxy_modifiers": [modifier_state(m) for m in sp.wrap_object(surface).modifiers],
        "matrix": [r(x) for row in patch.matrix_world for x in row],
        "parent": patch.parent.name if patch.parent else None,
        "groups": [g.name for g in patch.vertex_groups],
        "cutters": sorted(c.name for c in sp.patch_cutters(patch)),
        "patch_mesh": mesh_fingerprint(patch),
        "surface_shape": surface_shape(surface, patch),
        "cutter_mesh": mesh_fingerprint(cut),
    }


def run_case(kind, opts):
    fx.reset_scene()
    st = bpy.context.scene.shrink_patch
    for k, v in opts.items():
        setattr(st, k, v)
    surface = fx.make_surface()
    rings = fx.INSET_RINGS if kind == "inset" else fx.RAISED_RINGS
    patch = fx.make_patch("Patch", rings, location=(0.4, 0.1, 3.3))
    case = {}
    case["created"] = sp.setup_modifiers(patch, surface, "base", st)
    fx.select(patch)
    with bpy.context.temp_override(active_object=patch, object=patch, selected_objects=[patch]):
        bpy.ops.object.shrink_patch_snap()
        bpy.context.view_layer.update()
        case["after_snap"] = state(surface, patch)
        bpy.ops.object.shrink_patch_order(patch_name=patch.name, subdiv_after_wrap=not opts["subdiv_after_wrap"])
        bpy.ops.object.shrink_patch_order(patch_name=patch.name, subdiv_after_wrap=opts["subdiv_after_wrap"])
    bpy.context.view_layer.update()
    case["after_order_toggle"] = state(surface, patch)
    sp.process_watch()
    fx.rotate_about(patch, surface.matrix_world.translation, 0.3)
    bpy.context.view_layer.update()
    done = sp.process_watch()
    case["watch_after_move"] = {k: (sorted(v) if isinstance(v, list) else v) for k, v in done.items()}
    case["after_move"] = state(surface, patch)
    return case


def main():
    sp.register()
    results = {}
    keys = list(OPTIONS)
    for kind in ("raised", "inset"):
        for values in itertools.product(*(OPTIONS[k] for k in keys)):
            opts = dict(zip(keys, values))
            name = kind + " " + " ".join(f"{k}={v}" for k, v in opts.items())
            try:
                results[name] = run_case(kind, opts)
            except Exception as e:  # an error is behaviour too: record it, keep going
                results[name] = {"error": f"{type(e).__name__}: {e}"}
    sp.unregister()
    with open(OUT, "w") as f:
        json.dump(results, f, indent=1, sort_keys=True)
    print(f"SNAPSHOT cases={len(results)} -> {OUT}")


main()
