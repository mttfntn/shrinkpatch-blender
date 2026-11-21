"""Scene fixtures for the Shrink Patch tests (runs inside Blender).

Everything is built procedurally, so the tests need no .blend file:
  * a smooth UV-sphere "surface" with a Subdivision modifier;
  * a "raised" patch (a boss sitting on the surface) and an "inset" patch (a
    bowl that dips below its base), both modelled flat with local +Z pointing
    away from the surface, a `base` vertex group (outer ring 1.0, next ring 0.5).
"""

import math

import bmesh
import bpy
from mathutils import Matrix, Vector

SEGMENTS = 32
SPHERE_RADIUS = 2.8

# (radius, z, base weight or None) from the outside in; the centre closes the disc
RAISED_RINGS = [(1.0, 0.0, 1.0), (0.82, 0.05, 0.5), (0.62, 0.30, None), (0.48, 0.47, None), (0.0, 0.49, None)]
INSET_RINGS = [(1.0, 0.0, 1.0), (0.75, 0.0, 0.5), (0.56, -0.25, None), (0.42, -0.50, None), (0.0, -0.56, None)]


def reset_scene():
    """Empty scene with default settings (the add-on's scene settings included)."""
    bpy.ops.wm.read_homefile(use_empty=True)


def make_surface(name="Surface", location=(0.0, 0.0, 0.0), radius=SPHERE_RADIUS, levels=2):
    me = bpy.data.meshes.new(name)
    bm = bmesh.new()
    bmesh.ops.create_uvsphere(bm, u_segments=32, v_segments=16, radius=radius)
    bm.to_mesh(me)
    bm.free()
    for p in me.polygons:
        p.use_smooth = True
    ob = bpy.data.objects.new(name, me)
    bpy.context.scene.collection.objects.link(ob)
    ob.location = location
    sub = ob.modifiers.new("Subdivision", 'SUBSURF')
    sub.levels = sub.render_levels = levels
    return ob


def make_patch(name="Patch", rings=RAISED_RINGS, location=(0.0, 0.0, 3.5), with_subsurf=False):
    """Flat disc built from concentric rings; the base group marks the contact rings."""
    me = bpy.data.meshes.new(name)
    bm = bmesh.new()
    deform = bm.verts.layers.deform.verify()
    ring_verts = []
    for r, z, w in rings:
        if r == 0.0:
            v = bm.verts.new((0.0, 0.0, z))
            if w is not None:
                v[deform][0] = w
            ring_verts.append([v])
            continue
        row = []
        for i in range(SEGMENTS):
            a = 2.0 * math.pi * i / SEGMENTS
            v = bm.verts.new((r * math.cos(a), r * math.sin(a), z))
            if w is not None:
                v[deform][0] = w
            row.append(v)
        ring_verts.append(row)
    for outer, inner in zip(ring_verts, ring_verts[1:]):
        for i in range(SEGMENTS):
            j = (i + 1) % SEGMENTS
            if len(inner) == 1:
                bm.faces.new((outer[i], outer[j], inner[0]))
            else:
                bm.faces.new((outer[i], outer[j], inner[j], inner[i]))
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    # visible side up (+Z): flip if the centre face points down
    centre = [f for f in bm.faces if len(f.verts) == 3][0]
    if centre.normal.z < 0:
        bmesh.ops.reverse_faces(bm, faces=bm.faces)
    bm.to_mesh(me)
    bm.free()
    ob = bpy.data.objects.new(name, me)
    bpy.context.scene.collection.objects.link(ob)
    ob.vertex_groups.new(name="base")  # deform layer index 0 -> group "base"
    ob.location = location
    if with_subsurf:
        sub = ob.modifiers.new("Subdivision", 'SUBSURF')
        sub.levels = sub.render_levels = 3
    return ob


def select(active, *others):
    for o in bpy.context.view_layer.objects:
        o.select_set(False)
    for o in (active,) + others:
        o.select_set(True)
    bpy.context.view_layer.objects.active = active


def evaluated_world_verts(ob):
    dg = bpy.context.evaluated_depsgraph_get()
    ev = ob.evaluated_get(dg)
    me = ev.to_mesh()
    out = [ev.matrix_world @ v.co for v in me.vertices]
    ev.to_mesh_clear()
    return out


def ray_down_local(ob, local_xy, from_obj):
    """Ray along from_obj's local -Z, starting above local_xy; returns the hit
    distance on `ob`'s evaluated mesh (None if it misses)."""
    mw = from_obj.matrix_world
    start = mw @ Vector((local_xy[0], local_xy[1], 2.0))
    direction = (mw.to_3x3() @ Vector((0, 0, -1))).normalized()
    dg = bpy.context.evaluated_depsgraph_get()
    ev = ob.evaluated_get(dg)
    omw = ob.matrix_world
    ok, loc, _, _ = ev.ray_cast(omw.inverted() @ start, (omw.inverted().to_3x3() @ direction).normalized())
    return ((omw @ loc) - start).length if ok else None


def rotate_about(ob, centre, angle, axis='Y'):
    c = Vector(centre)
    ob.matrix_world = Matrix.Translation(c) @ Matrix.Rotation(angle, 4, axis) @ Matrix.Translation(-c) @ ob.matrix_world
