"""Shrink Patch: place patch meshes on a surface, wrap them, blend their shading
and (for insets) cut the surface open. Blender 5.3 extension, single module.

How it works
  A *patch* is a mesh with a `base` vertex group; the *surface* is any mesh.
  Applying a patch (setup_modifiers) gives it this modifier stack:
      [user modifiers] > ShrinkPatch_Conform (optional, geometry nodes)
      > ShrinkPatch_Shrinkwrap (base group) > Subdivision > ShrinkPatch_Normals
  The patch is recognised later purely from that Shrinkwrap modifier
  (patch_target), so nothing is kept in memory between sessions.

  Insets (geometry below the base) get a cutter built from their own shape
  (build_autocut_mesh). All cutters of a surface live in one collection used by
  a single Boolean on the surface (SP_Cut_All). Because the surface now has
  holes, patches wrap onto a hidden *proxy*: a copy of the surface sharing its
  mesh and modifiers minus the cuts (ensure_proxy / wrap_object).

  A depsgraph handler plus a short timer ("live sync") rebuilds cuts when a
  patch moves, removes cutters of deleted patches and re-copies the proxy when
  the surface changes. Files from older versions are upgraded on load.

Sections, in order: settings > helpers (patch/proxy/cut model, auto cutter,
geometry utilities, parenting, conform node group, stack ordering, setup) >
interactive placement operator > other operators > UI panels > live sync >
upgrade > registration.

Custom properties written to the scene data:
  surface["shrinkpatch_proxy"] / proxy["shrinkpatch_surface"]   proxy link
  surface["shrinkpatch_cut_collection"] / coll["shrinkpatch_surface"]
  cutter["shrinkpatch_cutter"], cutter["shrinkpatch_autocut"]
  scene["_shrink_patch_prev_snap"]   snap settings saved by Stick While Moving
"""

# Only used if the add-on is installed the legacy way; extensions read blender_manifest.toml.
bl_info = {
    "name": "Shrink Patch",
    "author": "Matteo Fontana",
    "version": (1, 12, 0),
    "blender": (5, 3, 0),
    "location": "View3D > N-panel > Shrink Patch  |  Object menu  |  Right-click menu",
    "description": "Interactively place patches on a surface, shrinkwrap their base and transfer normals",
    "category": "Object",
}

import math
import time

import bmesh
import bpy
import gpu
from bpy.props import BoolProperty, EnumProperty, IntProperty, PointerProperty, StringProperty
from bpy.app.handlers import persistent
from bpy_extras import view3d_utils
from gpu_extras.batch import batch_for_shader
from mathutils import Matrix, Quaternion, Vector
from mathutils.bvhtree import BVHTree

SHRINK_NAME = "ShrinkPatch_Shrinkwrap"
TRANSFER_NAME = "ShrinkPatch_Normals"
CONFORM_NAME = "ShrinkPatch_Conform"
CONFORM_GROUP = "ShrinkPatch_Conform"
CONFORM_VERSION = 1
CUT_PREFIX = "SP_Cut_"
CUTTER_PROP = "shrinkpatch_cutter"     # on cutter objects
PROXY_PROP = "shrinkpatch_proxy"       # on a surface -> its uncut proxy
SURFACE_PROP = "shrinkpatch_surface"   # on a proxy -> the real surface
AUTOCUT_PROP = "shrinkpatch_autocut"   # on the cutter generated from the patch
X = Vector((1.0, 0.0, 0.0))
Y = Vector((0.0, 1.0, 0.0))
Z = Vector((0.0, 0.0, 1.0))


# --------------------------------------------------------------------------- settings

# Per-scene defaults for new patches (Scene.shrink_patch), shown in "New Patch Settings".
class ShrinkPatchSettings(bpy.types.PropertyGroup):
    vertex_group: StringProperty(
        name="Base Group",
        description="Vertex group on the patch marking the base that touches the surface",
        default="base",
    )
    wrap_method: EnumProperty(
        name="Wrap",
        items=[
            ('PROJECT', "Project", "Project base vertices along the patch's local Z (face project)"),
            ('NEAREST_SURFACEPOINT', "Nearest Surface Point", "Move base vertices to the nearest surface point"),
            ('TARGET_PROJECT', "Target Normal Project", "Project along the target's interpolated normals"),
        ],
        default='PROJECT',
    )
    transfer_normals: BoolProperty(
        name="Transfer Normals",
        description="Copy the surface's normals onto the patch's contact edge (seamless shading)",
        default=True,
    )
    normals_group: StringProperty(
        name="Normals Group",
        description="Vertex group used by the Data Transfer (only the contact edge). If the patch "
                    "doesn't have it, it is created from the base vertices at full weight",
        default="base_normals",
    )
    shade_smooth: BoolProperty(
        name="Smooth Base Faces",
        description="Mark the base faces smooth so the transferred normals blend cleanly",
        default=True,
    )
    parent_to_surface: BoolProperty(
        name="Parent to Surface",
        description="Parent placed patches to the surface so they follow it when it moves",
        default=True,
    )
    subdiv_after_wrap: BoolProperty(
        name="Subdivision After Wrap",
        description="Put the patch's Subdivision after the Shrinkwrap (and before the normal transfer): "
                    "the cage is wrapped, then smoothed, avoiding the small dent at the base",
        default=True,
    )
    manage_subdiv: BoolProperty(
        name="Patch Subdivision",
        description="Give every new patch a Subdivision with these settings (added if missing)",
        default=True,
    )
    subdiv_levels: IntProperty(
        name="Viewport", description="Subdivision levels in the viewport", default=2, min=0, max=6,
    )
    subdiv_render_levels: IntProperty(
        name="Render", description="Subdivision levels at render time", default=2, min=0, max=6,
    )
    auto_cut: BoolProperty(
        name="Auto Cut Insets",
        description="If the patch goes below its base (an inset), generate a cutter from the patch itself "
                    "and cut the surface with a Boolean, like KIT OPS inserts",
        default=True,
    )
    conform_whole: BoolProperty(
        name="Deform Whole Patch",
        description="Bend the entire patch along the surface (keeping its thickness) instead of only "
                    "shrinkwrapping the base group",
        default=False,
    )
    stick_while_moving: BoolProperty(
        name="Stick While Moving (G / Shift+D)",
        description="Turn on Blender's face snapping (snap active origin, align rotation to target) so G and "
                    "Shift+D keep patches on the surface. Works best with the patch origin on its base",
        default=False,
        update=lambda self, context: _stick_update(self, context),
    )


def _settings(context):
    """The scene's Shrink Patch settings."""
    return context.scene.shrink_patch


# --------------------------------------------------------------------------- helpers

def patch_target(obj):
    """Surface a patch is applied to, or None."""
    if obj is None or obj.type != 'MESH':
        return None
    mod = obj.modifiers.get(SHRINK_NAME)
    if mod is None or mod.type != 'SHRINKWRAP':
        return None
    return real_surface(mod.target)


# ---- cutters & uncut proxy surface

def _obj_prop(obj, key):
    """Object stored in obj[key], or None (also None if it was deleted)."""
    v = obj.get(key) if obj is not None else None
    return v if isinstance(v, bpy.types.Object) else None


def _proxy_of(surface):
    """The surface's proxy, only if the link goes both ways (a duplicated surface
    inherits the property but is not the proxy's surface)."""
    proxy = _obj_prop(surface, PROXY_PROP)
    return proxy if proxy is not None and _obj_prop(proxy, SURFACE_PROP) is surface else None


def real_surface(obj):
    """The visible surface for obj (resolves a hidden proxy to its surface)."""
    surface = _obj_prop(obj, SURFACE_PROP)
    return surface if surface is not None and _proxy_of(surface) is obj else obj


def wrap_object(surface):
    """What patches wrap / ray-cast against: the uncut proxy if the surface has one."""
    return _proxy_of(surface) or surface


def is_cutter(obj):
    """Marked as a cutter, or a mesh with "cutter" in its name."""
    return obj is not None and obj.type == 'MESH' and (bool(obj.get(CUTTER_PROP)) or "cutter" in obj.name.lower())


def patch_cutters(patch):
    """The cutters parented to the patch (generated and hand-made)."""
    return [c for c in patch.children if is_cutter(c)] if patch is not None else []


def _copy_modifier(src, dst_obj):
    """Add a copy of modifier src to dst_obj, setting every writable property
    (properties Blender refuses to set are skipped)."""
    new = dst_obj.modifiers.new(src.name, src.type)
    for prop in src.bl_rna.properties:
        if prop.is_readonly or prop.identifier in {"name", "type", "rna_type"}:
            continue
        try:
            setattr(new, prop.identifier, getattr(src, prop.identifier))
        except Exception:
            pass
    return new


def ensure_proxy(surface):
    """Hidden copy of the surface (same mesh, same modifiers minus the cuts) so
    wrapping / normals / placement see the surface without the holes."""
    proxy = _proxy_of(surface)
    if proxy is None:
        proxy = bpy.data.objects.new(f"{surface.name}_SP_proxy", surface.data)
        for col in surface.users_collection:
            col.objects.link(proxy)
        proxy.parent = surface
        proxy.matrix_parent_inverse = Matrix.Identity(4)
        proxy.matrix_basis = Matrix.Identity(4)
        proxy.hide_viewport = True
        proxy.hide_render = True
        proxy.hide_select = True
        proxy[SURFACE_PROP] = surface
        surface[PROXY_PROP] = proxy
    if proxy.data is not surface.data:
        proxy.data = surface.data
    proxy.modifiers.clear()
    for m in surface.modifiers:
        if not m.name.startswith(CUT_PREFIX):
            _copy_modifier(m, proxy)
    return proxy


def make_cutter(obj, patch):
    """Turn obj into a hand-made cutter of the patch (child, wire, hidden in renders)."""
    parent_keep_transform(obj, patch)
    obj[CUTTER_PROP] = True
    obj.display_type = 'WIRE'
    obj.hide_render = True


CUTS_NAME = CUT_PREFIX + "All"            # the single Boolean on a surface
CUT_COLL_PROP = "shrinkpatch_cut_collection"


def cut_collection(surface, create=True):
    """Collection holding every cutter of the surface's patches. It is not linked
    to the scene: the cutters stay in their own collections too, this one only
    feeds the surface's single Boolean."""
    coll = surface.get(CUT_COLL_PROP)
    # a duplicated surface inherits the property: only the owner may use it
    if isinstance(coll, bpy.types.Collection) and coll.get(SURFACE_PROP) is surface:
        return coll
    if not create:
        return None
    coll = bpy.data.collections.new(f"{surface.name}_SP_cutters")
    coll[SURFACE_PROP] = surface
    surface[CUT_COLL_PROP] = coll
    return coll


def _update_cut_modifier(surface):
    """One Boolean (Difference, collection operand) for all the surface's cutters:
    a single pass over the dense surface instead of one Boolean per inset."""
    for m in list(surface.modifiers):  # older versions: one Boolean per cutter
        if m.type == 'BOOLEAN' and m.name.startswith(CUT_PREFIX) and m.name != CUTS_NAME:
            surface.modifiers.remove(m)
    coll = cut_collection(surface, create=False)
    mod = surface.modifiers.get(CUTS_NAME)
    if coll is None or len(coll.objects) == 0:
        if mod is not None:
            surface.modifiers.remove(mod)
        return
    if mod is None:
        mod = surface.modifiers.new(CUTS_NAME, 'BOOLEAN')
        mod.operation = 'DIFFERENCE'
        mod.solver = 'MANIFOLD'  # fast, and fine for the closed cutters used here
    mod.operand_type = 'COLLECTION'
    mod.collection = coll


def sync_cuts(patch, surface):
    """Put the patch's cutters in the surface's cutter collection and keep the
    single Boolean up to date."""
    cutters = patch_cutters(patch)
    if cutters:
        coll = cut_collection(surface)
        for c in cutters:
            if c.name not in coll.objects:
                coll.objects.link(c)
    _update_cut_modifier(surface)
    if cutters or _proxy_of(surface) is not None:
        proxy = ensure_proxy(surface)
        retarget_patches(surface, proxy)


def set_cuts_active(patch, surface, active):
    """Take the patch's cutters out of the Boolean (while dragging) or put them back."""
    coll = cut_collection(surface, create=False) if surface is not None else None
    if coll is None:
        return
    for c in patch_cutters(patch):
        if active and c.name not in coll.objects:
            coll.objects.link(c)
        elif not active and c.name in coll.objects:
            coll.objects.unlink(c)
    _update_cut_modifier(surface)


def remove_cuts(patch, surface):
    """The patch's cutters stop cutting the surface (the objects are kept)."""
    if surface is None:
        return
    set_cuts_active(patch, surface, False)


def is_inset(patch, group_name=None):
    """True if the patch has geometry below its base (it goes into the surface)."""
    verts = patch.data.vertices
    if not verts:
        return False
    c = _base_centroid_local(patch, group_name) if group_name else Vector()
    size = _footprint_local(patch, group_name, c) if group_name else max(v.co.xy.length for v in verts)
    return min(v.co.z for v in verts) < c.z - 0.02 * size


def autocut_of(patch):
    """The cutter generated from the patch's shape, or None."""
    return next((c for c in patch.children if c.get(AUTOCUT_PROP)), None) if patch is not None else None


def _boundary_loops(bm):
    """Boundary edges grouped into connected loops."""
    edges = [e for e in bm.edges if e.is_boundary]
    parent = {}

    def find(v):
        while parent.setdefault(v, v) is not v:
            v = parent[v]
        return v
    for e in edges:
        a, b = find(e.verts[0]), find(e.verts[1])
        if a is not b:
            parent[a] = b
    loops = {}
    for e in edges:
        loops.setdefault(find(e.verts[0]), []).append(e)
    return list(loops.values())


def build_autocut_mesh(patch, mesh, depsgraph, edge_group=None):
    """Closed cutter in the patch's local space: the evaluated patch surface pushed
    slightly down along its normals, its outer rim extruded straight up and capped.
    The surface is removed where the patch dips into it and where it would poke
    through the flange. At the contact edge (edge_group, e.g. base_normals) the
    cutter is lifted instead, so the surface is NOT cut there: the cut edge, with its
    shading crease, ends up hidden under the flange instead of at the visible rim."""
    ev = patch.evaluated_get(depsgraph)
    src = ev.to_mesh()
    # Build from one Subdivision level less (4x fewer faces for the Boolean), with
    # every vertex snapped onto the real, finer patch surface so it stays exact.
    sub = next((m for m in patch.modifiers if m.type == 'SUBSURF' and m.show_viewport), None)
    coarse = sub is not None and sub.levels >= 2
    fine = None
    if coarse:
        fine = BVHTree.FromPolygons([v.co.copy() for v in src.vertices],
                                    [tuple(p.vertices) for p in src.polygons])
        ev.to_mesh_clear()
        orig_levels = sub.levels
        sub.levels = orig_levels - 1
        try:
            bpy.context.view_layer.update()
            ev = patch.evaluated_get(bpy.context.evaluated_depsgraph_get())
            src = ev.to_mesh()
            bm = bmesh.new()
            bm.from_mesh(src)
            ev.to_mesh_clear()
        finally:
            sub.levels = orig_levels
            bpy.context.view_layer.update()
        for v in bm.verts:
            loc = fine.find_nearest(v.co)[0]
            if loc is not None:
                v.co = loc
    else:
        bm = bmesh.new()
        bm.from_mesh(src)
        ev.to_mesh_clear()
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-6)
    bm.normal_update()
    size = max((v.co.xy.length for v in bm.verts), default=1.0) or 1.0
    eps = size * (0.006 if coarse else 0.004)  # a bit more margin for the coarser cutter
    # The coarse faces are flat chords between vertices that lie on the fine surface:
    # in a tightly curved bowl they rise above it. Measure how far, and sink the
    # inside of the cutter by that much more, so the surface never shows through.
    sag = 0.0
    if fine is not None:
        for f in bm.faces:
            c = f.calc_center_median()
            loc, nor = fine.find_nearest(c)[:2]
            if loc is not None:
                sag = max(sag, (c - loc).dot(nor))
    deform = bm.verts.layers.deform.active
    gi = patch.vertex_groups[edge_group].index if edge_group and edge_group in patch.vertex_groups else None
    for v, n in [(v, v.normal.copy()) for v in bm.verts]:
        w = v[deform].get(gi, 0.0) if (deform is not None and gi is not None) else 0.0
        # -(eps + sag) inside, +eps at the contact edge (the cut edge hides under the flange)
        v.co += n * (eps * (2.0 * w - 1.0) - sag * (1.0 - w))
    # lid above both the rim and the base centre: on a convex surface the rim curves
    # down while the surface under the centre stays at base height, and it must be
    # inside the cutter or a cap of surface is left over the inset
    base_z = _base_centroid_local(patch, _base_group_name(patch, bpy.context.scene.shrink_patch)).z
    top = max(max(v.co.z for v in bm.verts), base_z) + size * 0.1
    loops = _boundary_loops(bm)
    if loops:
        outer = max(loops, key=lambda l: sum(e.verts[0].co.xy.length for e in l) / len(l))
        ret = bmesh.ops.extrude_edge_only(bm, edges=outer)
        for v in (g for g in ret['geom'] if isinstance(g, bmesh.types.BMVert)):
            v.co.z = top
        bmesh.ops.holes_fill(bm, edges=[e for e in bm.edges if e.is_boundary], sides=0)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    bm.to_mesh(mesh)
    bm.free()
    mesh.update()


def ensure_autocut(patch, enable):
    """Create / refresh / remove the cutter generated from the patch."""
    cut = autocut_of(patch)
    if not enable:
        if cut is not None:
            surface = patch_target(patch)
            mesh = cut.data
            bpy.data.objects.remove(cut)  # also leaves the cutter collection
            if mesh.users == 0:
                bpy.data.meshes.remove(mesh)
            if surface is not None:
                _update_cut_modifier(surface)
        return None
    if cut is None:
        mesh = bpy.data.meshes.new(f"{patch.name}_autocut")
        cut = bpy.data.objects.new(f"{patch.name}_autocut", mesh)
        for col in patch.users_collection:
            col.objects.link(cut)
        cut.parent = patch
        cut.matrix_parent_inverse = Matrix.Identity(4)
        cut.matrix_basis = Matrix.Identity(4)
        cut[CUTTER_PROP] = True
        cut[AUTOCUT_PROP] = True
        cut.display_type = 'WIRE'
        cut.hide_render = True
        try:
            cut.hide_set(True)
        except RuntimeError:
            pass
    bpy.context.view_layer.update()
    dt = patch.modifiers.get(TRANSFER_NAME)
    edge_group = dt.vertex_group if dt is not None and dt.vertex_group else None
    build_autocut_mesh(patch, cut.data, bpy.context.evaluated_depsgraph_get(), edge_group)
    return cut


def refresh_cuts(patch, settings=None):
    """Regenerate the auto cutter (if any / if wanted) and sync the Booleans."""
    surface = patch_target(patch)
    if surface is None:
        return
    want = autocut_of(patch) is not None
    if not want and settings is not None and settings.auto_cut:
        want = is_inset(patch, _base_group_name(patch, settings))
    if want:
        ensure_autocut(patch, True)
    sync_cuts(patch, surface)
    remember_placement(patch)


def retarget_patches(surface, wrap):
    """Point every patch of the surface at the wrap object (proxy or surface)."""
    for o in bpy.data.objects:
        if o.type != 'MESH' or patch_target(o) is not surface:
            continue
        sw = o.modifiers.get(SHRINK_NAME)
        if sw is not None and sw.target is not wrap:
            sw.target = wrap
        dt = o.modifiers.get(TRANSFER_NAME)
        if dt is not None and dt.object is not wrap:
            dt.object = wrap
        cm = o.modifiers.get(CONFORM_NAME)
        if cm is not None and cm.node_group is not None:
            _set_input(cm, "Surface", wrap)


def patches_on(scene, surface):
    """Every patch in the scene applied to the surface."""
    return [o for o in scene.objects if o is not surface and patch_target(o) is surface]


def _base_group_name(patch, settings):
    """Base vertex group of the patch: the one its Shrinkwrap uses, else the
    settings' name, else the active group (None if the patch has no groups)."""
    mod = patch.modifiers.get(SHRINK_NAME)
    if mod is not None and mod.vertex_group in patch.vertex_groups:
        return mod.vertex_group
    if settings.vertex_group in patch.vertex_groups:
        return settings.vertex_group
    if patch.vertex_groups.active is not None:
        return patch.vertex_groups.active.name
    return None


def _base_centroid_local(patch, group_name):
    """Weighted centroid (local space) of the base group (origin if empty)."""
    vg = patch.vertex_groups.get(group_name) if group_name else None
    if vg is None:
        return Vector()
    gi = vg.index
    total, wsum = Vector(), 0.0
    for v in patch.data.vertices:
        for g in v.groups:
            if g.group == gi and g.weight > 0.0:
                total += v.co * g.weight
                wsum += g.weight
    return total / wsum if wsum > 0.0 else Vector()


def _normal_to_world(mw, nor):
    """Object-space normal to world space (inverse-transpose, normalised)."""
    return (mw.to_3x3().inverted_safe().transposed() @ nor).normalized()


def _closest_on_target(target, depsgraph, world_point):
    """Closest point and normal (world space) on the target's uncut shape."""
    ev = wrap_object(target).evaluated_get(depsgraph)
    mw = target.matrix_world
    ok, loc, nor, _ = ev.closest_point_on_mesh(mw.inverted_safe() @ world_point)
    if not ok:
        return None, None
    return mw @ loc, _normal_to_world(mw, nor)


def _ray_on_target(target, depsgraph, origin, direction):
    """Ray hit and normal (world space) on the target's uncut shape, or (None, None)."""
    ev = wrap_object(target).evaluated_get(depsgraph)
    mw = target.matrix_world
    imw = mw.inverted_safe()
    ok, loc, nor, _ = ev.ray_cast(imw @ origin, (imw.to_3x3() @ direction).normalized())
    if not ok:
        return None, None
    return mw @ loc, _normal_to_world(mw, nor)


def _place_matrix(rot, scale, base_local, hit, nor, offset):
    """World matrix putting the base centroid on hit (+offset along nor)."""
    mw = Matrix.LocRotScale(Vector(), rot, scale)
    mw.translation = hit + nor * offset - (mw @ base_local)
    return mw


def ensure_normals_group(patch, base_name, normals_name):
    """Second vertex group for the Data Transfer (like the video): only the base
    vertices at full weight, so the surface normals fade out right at the contact
    edge instead of spreading up the patch with the shrinkwrap falloff."""
    if not normals_name or normals_name == base_name:
        return base_name
    if normals_name in patch.vertex_groups:
        return normals_name
    base = patch.vertex_groups.get(base_name)
    if base is None:
        return base_name
    ids = [v.index for v in patch.data.vertices
           if any(g.group == base.index and g.weight >= 0.999 for g in v.groups)]
    if not ids:
        return base_name
    vg = patch.vertex_groups.new(name=normals_name)
    vg.add(ids, 1.0, 'REPLACE')
    patch.vertex_groups.active_index = base.index
    return normals_name


def _smooth_base_faces(patch, group_name):
    """Shade smooth every face touching the base group (needed for the normal transfer)."""
    vg = patch.vertex_groups.get(group_name)
    if vg is None:
        return
    gi = vg.index
    mesh = patch.data
    in_group = [any(g.group == gi and g.weight > 0.0 for g in v.groups) for v in mesh.vertices]
    for p in mesh.polygons:
        if not p.use_smooth and any(in_group[i] for i in p.vertices):
            p.use_smooth = True
    mesh.update()


def _footprint_local(patch, group_name, centroid):
    """Radius of the base in the patch's local XY plane."""
    vg = patch.vertex_groups.get(group_name)
    gi = vg.index if vg is not None else None
    r = 0.0
    for v in patch.data.vertices:
        if gi is None or any(g.group == gi and g.weight > 0.0 for g in v.groups):
            r = max(r, (v.co - centroid).xy.length)
    return r or 1.0


# ---- parenting

def parent_keep_transform(obj, parent):
    """Parent obj to parent (or clear it with None) without moving it."""
    if obj.parent is parent:
        return
    # rebuild the world matrix from basis data (matrix_world can be stale inside scripts)
    if obj.parent is not None:
        mw = obj.parent.matrix_world @ obj.matrix_parent_inverse @ obj.matrix_basis
    else:
        mw = obj.matrix_basis.copy()
    obj.parent = parent
    if parent is not None:
        obj.matrix_parent_inverse = parent.matrix_world.inverted_safe()
    else:
        obj.matrix_parent_inverse = Matrix.Identity(4)
    obj.matrix_world = mw


# ---- whole-patch conform (geometry nodes)

def _conform_node_group():
    """Node group: ray-cast every vertex along local -Z onto the surface and
    rebuild it at its original height along the (smooth) surface normal."""
    ng = bpy.data.node_groups.get(CONFORM_GROUP)
    if ng is not None and ng.get("shrinkpatch_version") == CONFORM_VERSION:
        return ng
    if ng is None:
        ng = bpy.data.node_groups.new(CONFORM_GROUP, 'GeometryNodeTree')
    else:
        ng.nodes.clear()
        ng.interface.clear()
    ng["shrinkpatch_version"] = CONFORM_VERSION
    itf = ng.interface
    itf.new_socket("Geometry", in_out='INPUT', socket_type='NodeSocketGeometry')
    itf.new_socket("Surface", in_out='INPUT', socket_type='NodeSocketObject')
    itf.new_socket("Base Height", in_out='INPUT', socket_type='NodeSocketFloat')
    s = itf.new_socket("Search Distance", in_out='INPUT', socket_type='NodeSocketFloat')
    s.default_value, s.min_value = 1.0, 0.0
    s = itf.new_socket("Factor", in_out='INPUT', socket_type='NodeSocketFloat')
    s.default_value, s.min_value, s.max_value = 1.0, 0.0, 1.0
    s.subtype = 'FACTOR'
    itf.new_socket("Geometry", in_out='OUTPUT', socket_type='NodeSocketGeometry')

    N, L = ng.nodes, ng.links

    def node(t, x, y, **props):
        n = N.new(t)
        n.location = (x, y)
        for k, v in props.items():
            setattr(n, k, v)
        return n

    gin = node('NodeGroupInput', -1000, 0)
    gout = node('NodeGroupOutput', 900, 0)
    info = node('GeometryNodeObjectInfo', -800, 300, transform_space='RELATIVE')
    pos = node('GeometryNodeInputPosition', -1000, -300)
    sep = node('ShaderNodeSeparateXYZ', -800, -300)
    top = node('ShaderNodeMath', -600, -250, operation='ADD')
    comb = node('ShaderNodeCombineXYZ', -400, -300)
    length = node('ShaderNodeMath', -400, -500, operation='MULTIPLY')
    length.inputs[1].default_value = 4.0
    nrm = node('GeometryNodeInputNormal', -400, 200)
    ray = node('GeometryNodeRaycast', -150, 0, data_type='FLOAT_VECTOR')
    ray.inputs['Ray Direction'].default_value = (0.0, 0.0, -1.0)
    height = node('ShaderNodeMath', -150, -350, operation='SUBTRACT')
    unit = node('ShaderNodeVectorMath', 100, 150, operation='NORMALIZE')
    lift = node('ShaderNodeVectorMath', 300, 150, operation='SCALE')
    new_pos = node('ShaderNodeVectorMath', 450, 0, operation='ADD')
    delta = node('ShaderNodeVectorMath', 600, -100, operation='SUBTRACT')
    fac = node('ShaderNodeVectorMath', 750, -200, operation='SCALE')
    setp = node('GeometryNodeSetPosition', 750, 50)

    L.new(gin.outputs['Surface'], info.inputs['Object'])
    L.new(info.outputs['Geometry'], ray.inputs['Target Geometry'])
    L.new(nrm.outputs['Normal'], ray.inputs['Attribute'])
    L.new(pos.outputs['Position'], sep.inputs['Vector'])
    L.new(gin.outputs['Base Height'], top.inputs[0])
    L.new(gin.outputs['Search Distance'], top.inputs[1])
    L.new(sep.outputs['X'], comb.inputs['X'])
    L.new(sep.outputs['Y'], comb.inputs['Y'])
    L.new(top.outputs[0], comb.inputs['Z'])
    L.new(comb.outputs['Vector'], ray.inputs['Source Position'])
    L.new(gin.outputs['Search Distance'], length.inputs[0])
    L.new(length.outputs[0], ray.inputs['Ray Length'])
    L.new(sep.outputs['Z'], height.inputs[0])
    L.new(gin.outputs['Base Height'], height.inputs[1])
    L.new(ray.outputs['Attribute'], unit.inputs[0])
    L.new(unit.outputs['Vector'], lift.inputs[0])
    L.new(height.outputs[0], lift.inputs['Scale'])
    L.new(ray.outputs['Hit Position'], new_pos.inputs[0])
    L.new(lift.outputs['Vector'], new_pos.inputs[1])
    L.new(new_pos.outputs['Vector'], delta.inputs[0])
    L.new(pos.outputs['Position'], delta.inputs[1])
    L.new(delta.outputs['Vector'], fac.inputs[0])
    L.new(gin.outputs['Factor'], fac.inputs['Scale'])
    L.new(gin.outputs['Geometry'], setp.inputs['Geometry'])
    L.new(ray.outputs['Is Hit'], setp.inputs['Selection'])
    L.new(fac.outputs['Vector'], setp.inputs['Offset'])
    L.new(setp.outputs['Geometry'], gout.inputs[0])
    return ng


def _socket_id(ng, name):
    """Identifier of the node group's input socket called name."""
    for item in ng.interface.items_tree:
        if item.item_type == 'SOCKET' and item.in_out == 'INPUT' and item.name == name:
            return item.identifier
    return None


def _input_owner(mod, ident):
    """(owner, prop_name) of a geometry-nodes modifier input (Blender 5.x API)."""
    return getattr(mod.properties.inputs, ident), "value"


def _set_input(mod, name, value):
    """Set a geometry-nodes modifier input by socket name."""
    getattr(mod.properties.inputs, _socket_id(mod.node_group, name)).value = value


def set_conform(patch, target, group_name, enable):
    """Add/remove the whole-patch conform modifier (placed just before the shrinkwrap)."""
    mod = patch.modifiers.get(CONFORM_NAME)
    if not enable:
        if mod is not None:
            patch.modifiers.remove(mod)
        return False
    created = mod is None
    if created:
        mod = patch.modifiers.new(CONFORM_NAME, 'NODES')
    ng = _conform_node_group()
    mod.node_group = ng
    centroid = _base_centroid_local(patch, group_name)
    size = max((v.co - centroid).length for v in patch.data.vertices) if patch.data.vertices else 1.0
    _set_input(mod, "Surface", wrap_object(target))
    _set_input(mod, "Base Height", centroid.z)
    _set_input(mod, "Search Distance", max(size, 1e-3) * 1.5)
    sw_idx = patch.modifiers.find(SHRINK_NAME)
    idx = patch.modifiers.find(CONFORM_NAME)
    if sw_idx != -1 and idx > sw_idx:
        patch.modifiers.move(idx, sw_idx)
    patch.update_tag()
    return created


def conform_factor_path(patch):
    """(owner, property) of the Whole Patch "Factor" input for the UI, or (None, None)."""
    mod = patch.modifiers.get(CONFORM_NAME)
    if mod is None or mod.node_group is None:
        return None, None
    ident = _socket_id(mod.node_group, "Factor")
    return _input_owner(mod, ident) if ident else (None, None)


# ---- placement preview

def _draw_preview(state):
    """Viewport overlay while placing: footprint ring and normal / X ticks."""
    ring, axes = state.get("ring"), state.get("axes")
    if not ring:
        return
    shader = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('NONE')
    vp = gpu.state.viewport_get()
    shader.uniform_float("viewportSize", (vp[2], vp[3]))
    shader.uniform_float("lineWidth", 2.0)
    shader.uniform_float("color", (1.0, 0.55, 0.1, 0.95))
    batch_for_shader(shader, 'LINE_STRIP', {"pos": ring}).draw(shader)
    shader.uniform_float("color", (0.25, 0.8, 1.0, 0.95))
    batch_for_shader(shader, 'LINES', {"pos": axes}).draw(shader)
    gpu.state.blend_set('NONE')


def apply_subdiv_settings(patch, settings):
    """Give the patch a Subdivision with the addon's settings (Catmull-Clark,
    viewport / render levels, Optimal Display); added if the patch has none."""
    if not settings.manage_subdiv:
        return None
    sub = next((m for m in patch.modifiers if m.type == 'SUBSURF'), None)
    if sub is None:
        sub = patch.modifiers.new("Subdivision", 'SUBSURF')
    sub.subdivision_type = 'CATMULL_CLARK'
    sub.levels = settings.subdiv_levels
    sub.render_levels = settings.subdiv_render_levels
    sub.show_only_control_edges = True  # "Optimal Display"
    return sub


def order_stack(patch, subdiv_after_wrap=True):
    """Order the Shrink Patch modifiers relative to the patch's Subdivision.
    subdiv_after_wrap: ... Conform > Shrinkwrap > Subdivision > Data Transfer
      (deform the low-poly cage, let Subdivision smooth the transition, then
      transfer normals onto the final dense mesh - avoids the small dent).
    otherwise:        ... Subdivision > Conform > Shrinkwrap > Data Transfer
    Other modifiers (e.g. Mirror) keep their place."""
    mods = patch.modifiers

    def move(name, index):
        cur = mods.find(name)
        if cur == -1:
            return
        index = max(0, min(index, len(mods) - 1))
        if cur != index:
            mods.move(cur, index)

    move(TRANSFER_NAME, len(mods) - 1)
    subsurf = next((m for m in mods if m.type == 'SUBSURF'), None)
    for name in (CONFORM_NAME, SHRINK_NAME):  # each one lands right before the next
        if mods.find(name) == -1:
            continue
        if subdiv_after_wrap and subsurf is not None:
            cur, sub_idx = mods.find(name), mods.find(subsurf.name)
            move(name, sub_idx - 1 if cur < sub_idx else sub_idx)
        else:
            dt_idx = mods.find(TRANSFER_NAME)
            end = dt_idx if dt_idx != -1 else len(mods)
            cur = mods.find(name)
            move(name, end - 1 if cur < end else end)
    # make sure Conform stays before Shrinkwrap
    c, s = mods.find(CONFORM_NAME), mods.find(SHRINK_NAME)
    if c != -1 and s != -1 and c > s:
        mods.move(c, s)


def setup_modifiers(patch, target, group_name, settings, offset=None):
    """Create/update the Shrinkwrap + Data Transfer (+ Conform) modifiers. Returns names created."""
    created = []
    target = real_surface(target)
    sync_cuts(patch, target)
    wrap = wrap_object(target)
    sw = patch.modifiers.get(SHRINK_NAME)
    if sw is None:
        sw = patch.modifiers.new(SHRINK_NAME, 'SHRINKWRAP')
        created.append(SHRINK_NAME)
        sw.wrap_method = settings.wrap_method
        sw.wrap_mode = 'ON_SURFACE'
        if sw.wrap_method == 'PROJECT':
            sw.use_project_z = True
            sw.use_project_x = sw.use_project_y = False
            sw.use_negative_direction = True
            sw.use_positive_direction = True
    sw.target = wrap
    sw.vertex_group = group_name
    if offset is not None:
        sw.offset = offset

    dt = patch.modifiers.get(TRANSFER_NAME)
    if settings.transfer_normals or dt is not None:
        normals_name = ensure_normals_group(patch, group_name, settings.normals_group)
        upgrade = dt is not None and dt.vertex_group == group_name and normals_name != group_name
        if dt is None or upgrade:
            if dt is None:
                dt = patch.modifiers.new(TRANSFER_NAME, 'DATA_TRANSFER')
                created.append(TRANSFER_NAME)
                if settings.shade_smooth:
                    _smooth_base_faces(patch, group_name)
            dt.use_object_transform = True
            dt.use_loop_data = True
            dt.data_types_loops = {'CUSTOM_NORMAL'}
            dt.loop_mapping = 'POLYINTERP_LNORPROJ'
            dt.mix_mode = 'REPLACE'
            dt.mix_factor = 1.0
            dt.vertex_group = normals_name
        dt.object = wrap
        dt.loop_mapping = 'POLYINTERP_LNORPROJ'  # the only mapping used
        if dt.vertex_group not in patch.vertex_groups:
            dt.vertex_group = normals_name

    conform = patch.modifiers.get(CONFORM_NAME)
    if conform is None and settings.conform_whole:
        set_conform(patch, target, group_name, True)
        created.append(CONFORM_NAME)
    elif conform is not None:
        set_conform(patch, target, group_name, True)  # refresh target / heights

    if created:
        if SHRINK_NAME in created:  # first time this object becomes a patch
            apply_subdiv_settings(patch, settings)
        order_stack(patch, settings.subdiv_after_wrap)

    if settings.parent_to_surface:
        parent_keep_transform(patch, target)
    if autocut_of(patch) is None and settings.auto_cut and is_inset(patch, group_name):
        ensure_autocut(patch, True)
        sync_cuts(patch, target)
        created.append(AUTOCUT_PROP)
    return created


def _resolve(context, patch_name):
    """(patch, target, error)."""
    patch = bpy.data.objects.get(patch_name) if patch_name else context.active_object
    if patch is None or patch.type != 'MESH':
        return None, None, "Active object must be the patch mesh"
    others = [o for o in context.selected_objects if o is not patch and o.type == 'MESH']
    target = real_surface(others[0]) if (not patch_name and len(others) == 1) else patch_target(patch)
    if target is None:
        return patch, None, "Select the surface, then the patch as active object"
    return patch, target, None


def duplicate_object(obj, linked=False):
    """Copy obj (modifiers included) into the same collections, like Shift+D."""
    new = obj.copy()
    if not linked and obj.data is not None:
        new.data = obj.data.copy()
    for col in obj.users_collection:
        col.objects.link(new)
    return new


def duplicate_patch(patch, linked=False):
    """Duplicate a patch together with its cutters (re-parented to the copy)."""
    new = duplicate_object(patch, linked)
    for c in patch_cutters(patch):
        auto = bool(c.get(AUTOCUT_PROP))
        nc = duplicate_object(c, linked and not auto)  # a generated cutter always gets its own mesh
        nc.parent = new
        nc.matrix_parent_inverse = c.matrix_parent_inverse.copy()
        nc.matrix_basis = c.matrix_basis.copy()
        if auto:
            nc.name = nc.data.name = f"{new.name}_autocut"
        try:
            nc.hide_set(c.hide_get())  # the hidden state is per view layer: copy() drops it
        except RuntimeError:
            pass
    return new


def delete_patch(patch):
    """Delete the patch and its cutters, and update the surface's cut."""
    remove_cuts(patch, patch_target(patch))
    for obj in patch_cutters(patch) + [patch]:
        mesh = obj.data
        bpy.data.objects.remove(obj)
        if mesh is not None and mesh.users == 0:
            bpy.data.meshes.remove(mesh)


def set_origin_to_base(obj, group_name):
    """Move the origin onto the base centroid without moving the geometry
    (compensates every object sharing the mesh)."""
    c = _base_centroid_local(obj, group_name)
    if c.length < 1e-9:
        return False
    mesh = obj.data
    users = [o for o in bpy.data.objects if o.data is mesh]
    mesh.transform(Matrix.Translation(-c))
    mesh.update()
    for o in users:
        o.matrix_world = o.matrix_world @ Matrix.Translation(c)
    return True


def _stick_update(self, context):
    """Stick While Moving: switch Blender's snapping to face + align rotation,
    saving the previous snap settings in the scene and restoring them when off."""
    ts = context.scene.tool_settings
    store = context.scene.get("_shrink_patch_prev_snap")
    if self.stick_while_moving:
        if store is None:
            context.scene["_shrink_patch_prev_snap"] = {
                "use_snap": ts.use_snap,
                "snap_elements_base": list(ts.snap_elements_base),
                "snap_elements_individual": list(ts.snap_elements_individual),
                "snap_target": ts.snap_target,
                "use_snap_align_rotation": ts.use_snap_align_rotation,
                "use_snap_translate": ts.use_snap_translate,
            }
        ts.use_snap = True
        ts.snap_elements_base = {'FACE'}
        ts.snap_elements_individual = set()
        ts.snap_target = 'ACTIVE'
        ts.use_snap_align_rotation = True
        ts.use_snap_translate = True
    elif store is not None:
        ts.use_snap = store["use_snap"]
        ts.snap_elements_base = set(store["snap_elements_base"])
        ts.snap_elements_individual = set(store["snap_elements_individual"])
        ts.snap_target = store["snap_target"]
        ts.use_snap_align_rotation = store["use_snap_align_rotation"]
        ts.use_snap_translate = store.get("use_snap_translate", True)  # older files did not store it
        del context.scene["_shrink_patch_prev_snap"]


def _select_only(context, obj):
    """Select obj alone and make it active."""
    for o in context.selected_objects:
        o.select_set(False)
    obj.hide_set(False)
    obj.select_set(True)
    context.view_layer.objects.active = obj


# --------------------------------------------------------------------------- interactive placement

class OBJECT_OT_shrink_patch_place(bpy.types.Operator):
    """Interactively place / move the patch on the surface (wheel: scale, Shift+wheel: rotate, Ctrl+wheel: offset)"""
    bl_idname = "object.shrink_patch_place"
    bl_label = "Place on Surface"
    bl_options = {'REGISTER', 'UNDO', 'BLOCKING'}

    patch_name: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})
    delete_on_cancel: BoolProperty(options={'HIDDEN', 'SKIP_SAVE'})

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and context.area is not None and context.area.type == 'VIEW_3D'

    # -- setup
    def invoke(self, context, event):
        patch, target, err = _resolve(context, self.patch_name)
        if err:
            self.report({'ERROR'}, err)
            return {'CANCELLED'}
        settings = _settings(context)
        group = _base_group_name(patch, settings)
        if group is None:
            self.report({'ERROR'}, f"'{patch.name}' has no vertex group for the base")
            return {'CANCELLED'}

        self.region = next((r for r in context.area.regions if r.type == 'WINDOW'), None)
        self.rv3d = context.area.spaces.active.region_3d
        if self.region is None or self.rv3d is None:
            return {'CANCELLED'}

        self.patch_ref, self.target_ref = patch.name, target.name
        self.orig_matrix = patch.matrix_world.copy()
        self.orig_parent = patch.parent
        self.orig_parent_inv = patch.matrix_parent_inverse.copy()
        sw = patch.modifiers.get(SHRINK_NAME)
        self.orig_offset = sw.offset if sw is not None else 0.0
        self.offset = self.orig_offset
        _, self.rot, self.scale = self.orig_matrix.decompose()
        self.base_local = _base_centroid_local(patch, group)
        self.footprint = _footprint_local(patch, group, self.base_local)
        self.step = max(max(patch.dimensions), 1e-3) * 0.02
        self.created = setup_modifiers(patch, target, group, settings, self.offset)
        self.stamped = 0
        # while dragging, this patch's cutters leave the surface's Boolean: the dense
        # surface is not re-cut on every mouse move (the cut comes back on confirm)
        set_cuts_active(patch, target, False)

        _select_only(context, patch)
        self.preview = {"ring": None, "axes": None}
        _watch["placing"].add(patch.name)
        self._draw_handle = bpy.types.SpaceView3D.draw_handler_add(
            _draw_preview, (self.preview,), 'WINDOW', 'POST_VIEW')

        # start from the patch's current spot on the surface
        dg = context.evaluated_depsgraph_get()
        self.hit, self.nor = _closest_on_target(target, dg, self.orig_matrix @ self.base_local)
        self._mouse_update(context, event)
        if self.preview["ring"] is None and self.hit is not None:
            self._update_preview()

        context.window_manager.modal_handler_add(self)
        self._status(context)
        return {'RUNNING_MODAL'}

    # -- helpers
    # objects are looked up by name: an operator must not keep ID references across events
    @property
    def patch(self):
        return bpy.data.objects.get(self.patch_ref)

    @property
    def target(self):
        return bpy.data.objects.get(self.target_ref)

    def _status(self, context):
        """Header text with the current values and the controls."""
        s = self.scale
        context.area.header_text_set(
            f"Shrink Patch  |  scale {s.x:.3f}  offset {self.offset:.4f}  stamped {self.stamped}  |  "
            "Wheel: scale   Shift+Wheel: rotate   Ctrl+Wheel: offset   D: stamp a copy here   "
            "LMB/Enter: confirm   RMB/Esc: cancel   MMB: navigate")

    def _stamp(self, context):
        """Leave a copy of the patch at its current spot and keep placing the original."""
        new = duplicate_patch(self.patch)
        refresh_cuts(new)  # exact cutter for the stamped spot, and it joins the Boolean
        self.stamped += 1
        return new

    def _apply(self):
        """Move the patch to the current hit / rotation / scale / offset."""
        if self.hit is None:
            return
        self.patch.matrix_world = _place_matrix(self.rot, self.scale, self.base_local,
                                                self.hit, self.nor, self.offset)
        sw = self.patch.modifiers.get(SHRINK_NAME)
        if sw is not None:
            sw.offset = self.offset
        self._update_preview()

    def _update_preview(self):
        """Footprint ring (projected on the surface) + normal / X-axis ticks."""
        hit, nor = self.hit, self.nor
        r = self.footprint * max(self.scale)
        u, v = self.rot @ X, self.rot @ Y
        dg = bpy.context.evaluated_depsgraph_get()
        lift = r * 0.01
        ring = []
        for i in range(49):
            a = 2.0 * math.pi * i / 48
            p = hit + (u * math.cos(a) + v * math.sin(a)) * r
            q, n = _closest_on_target(self.target, dg, p)
            ring.append(tuple(q + n * lift) if q is not None else tuple(p))
        self.preview["ring"] = ring
        self.preview["axes"] = [tuple(hit), tuple(hit + nor * r * 0.6),
                                tuple(hit), tuple(hit + u * r * 0.4)]

    def _mouse_update(self, context, event):
        """Ray-cast the mouse onto the surface and slide the patch there."""
        mx, my = event.mouse_x - self.region.x, event.mouse_y - self.region.y
        if not (0 <= mx <= self.region.width and 0 <= my <= self.region.height):
            return
        co = (mx, my)
        origin = view3d_utils.region_2d_to_origin_3d(self.region, self.rv3d, co)
        direction = view3d_utils.region_2d_to_vector_3d(self.region, self.rv3d, co)
        hit, nor = _ray_on_target(self.target, context.evaluated_depsgraph_get(), origin, direction)
        if hit is None:
            return
        # parallel-transport the orientation so the twist stays stable while sliding
        cur_z = self.rot @ Z
        self.rot = (cur_z.rotation_difference(nor) @ self.rot).normalized()
        self.hit, self.nor = hit, nor
        self._apply()

    def _finish(self, context):
        """Remove the overlay and header text; let the live sync see the patch again."""
        _watch["placing"].discard(getattr(self, "patch_ref", None))
        if getattr(self, "_draw_handle", None) is not None:
            bpy.types.SpaceView3D.draw_handler_remove(self._draw_handle, 'WINDOW')
            self._draw_handle = None
        context.area.header_text_set(None)
        context.workspace.status_text_set(None)
        context.area.tag_redraw()

    # -- modal
    def modal(self, context, event):
        if self.patch is None or self.target is None:
            self._finish(context)
            return {'CANCELLED'}

        et = event.type
        if et in {'MIDDLEMOUSE', 'TRACKPADPAN', 'TRACKPADZOOM'} or et.startswith('NDOF'):
            return {'PASS_THROUGH'}

        if et in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            self._mouse_update(context, event)

        elif et in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE', 'NUMPAD_PLUS', 'NUMPAD_MINUS'} and event.value == 'PRESS':
            sign = 1.0 if et in {'WHEELUPMOUSE', 'NUMPAD_PLUS'} else -1.0
            if event.ctrl:
                self.offset += sign * self.step * (0.2 if event.alt else 1.0)
            elif event.shift:
                if self.nor is not None:
                    ang = math.radians(5.0 if event.alt else 15.0) * sign
                    self.rot = (Quaternion(self.nor, ang) @ self.rot).normalized()
            else:
                f = 1.02 if event.alt else 1.1
                self.scale = self.scale * (f if sign > 0 else 1.0 / f)
            self._apply()

        elif et == 'D' and event.value == 'PRESS':
            self._stamp(context)

        elif et in {'LEFTMOUSE', 'RET', 'NUMPAD_ENTER', 'SPACE'} and event.value == 'PRESS':
            self._finish(context)
            refresh_cuts(self.patch)
            return {'FINISHED'}

        elif et in {'RIGHTMOUSE', 'ESC'} and event.value == 'PRESS':
            patch = self.patch
            if self.delete_on_cancel:
                self._finish(context)
                delete_patch(patch)
                return {'CANCELLED'}
            if SHRINK_NAME in self.created:  # was not a patch before: drop its cuts too
                remove_cuts(patch, self.target)
            if AUTOCUT_PROP in self.created:
                ensure_autocut(patch, False)
            patch.parent = self.orig_parent
            patch.matrix_parent_inverse = self.orig_parent_inv
            patch.matrix_world = self.orig_matrix
            for name in self.created:
                mod = patch.modifiers.get(name)
                if mod is not None:
                    patch.modifiers.remove(mod)
            sw = patch.modifiers.get(SHRINK_NAME)
            if sw is not None:
                sw.offset = self.orig_offset
            if SHRINK_NAME not in self.created:  # existing patch moved back: restore its cut
                set_cuts_active(patch, self.target, True)
            self._finish(context)
            return {'CANCELLED'}

        else:
            return {'RUNNING_MODAL'}

        self._status(context)
        context.area.tag_redraw()
        return {'RUNNING_MODAL'}


# --------------------------------------------------------------------------- other operators

def subdiv_is_after_wrap(patch):
    """True if the Subdivision comes after the Shrinkwrap in the patch's stack."""
    sw = patch.modifiers.find(SHRINK_NAME)
    sub = next((i for i, m in enumerate(patch.modifiers) if m.type == 'SUBSURF'), -1)
    return sw != -1 and sub != -1 and sw < sub


class OBJECT_OT_shrink_patch_order(bpy.types.Operator):
    """Toggle the Subdivision between before and after the Shrinkwrap (Data Transfer stays last)"""
    bl_idname = "object.shrink_patch_order"
    bl_label = "Subdivision After Wrap"
    bl_options = {'REGISTER', 'UNDO'}

    patch_name: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})
    subdiv_after_wrap: BoolProperty(options={'SKIP_SAVE'})

    def execute(self, context):
        patch = bpy.data.objects.get(self.patch_name) if self.patch_name else context.active_object
        if patch_target(patch) is None:
            return {'CANCELLED'}
        order_stack(patch, self.subdiv_after_wrap)
        refresh_cuts(patch)
        return {'FINISHED'}


class OBJECT_OT_shrink_patch_apply_subdiv(bpy.types.Operator):
    """Apply the Subdivision settings (Catmull-Clark, levels, Optimal Display) to every patch in the scene and rebuild their cuts"""
    bl_idname = "object.shrink_patch_apply_subdiv"
    bl_label = "Apply to All Patches"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        settings = _settings(context)
        if not settings.manage_subdiv:
            self.report({'WARNING'}, "Turn on 'Patch Subdivision' first")
            return {'CANCELLED'}
        patches = [o for o in context.scene.objects if o.type == 'MESH' and patch_target(o) is not None]
        for p in patches:
            had = any(m.type == 'SUBSURF' for m in p.modifiers)
            apply_subdiv_settings(p, settings)
            if not had:
                order_stack(p, settings.subdiv_after_wrap)
            if autocut_of(p) is not None:
                refresh_cuts(p)
        self.report({'INFO'}, f"Subdivision {settings.subdiv_levels}/{settings.subdiv_render_levels} "
                              f"applied to {len(patches)} patch(es)")
        return {'FINISHED'}


class OBJECT_OT_shrink_patch_conform(bpy.types.Operator):
    """Toggle bending the whole patch along the surface (keeps thickness) vs. only the base group"""
    bl_idname = "object.shrink_patch_conform"
    bl_label = "Deform Whole Patch"
    bl_options = {'REGISTER', 'UNDO'}

    patch_name: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})
    enable: BoolProperty(options={'SKIP_SAVE'})

    def execute(self, context):
        patch = bpy.data.objects.get(self.patch_name) if self.patch_name else context.active_object
        target = patch_target(patch)
        if target is None:
            self.report({'ERROR'}, "Not a patch applied to a surface")
            return {'CANCELLED'}
        group = _base_group_name(patch, _settings(context))
        set_conform(patch, target, group, self.enable)
        refresh_cuts(patch)
        return {'FINISHED'}



class OBJECT_OT_shrink_patch_snap(bpy.types.Operator):
    """Re-stick the patch to the nearest point of its surface and realign it (use after moving it with G)"""
    bl_idname = "object.shrink_patch_snap"
    bl_label = "Snap to Surface"
    bl_options = {'REGISTER', 'UNDO'}

    patch_name: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def execute(self, context):
        patch, target, err = _resolve(context, self.patch_name)
        if err:
            self.report({'ERROR'}, err)
            return {'CANCELLED'}
        settings = _settings(context)
        group = _base_group_name(patch, settings)
        if group is None:
            self.report({'ERROR'}, f"'{patch.name}' has no vertex group for the base")
            return {'CANCELLED'}
        setup_modifiers(patch, target, group, settings)
        base_local = _base_centroid_local(patch, group)
        _, rot, scale = patch.matrix_world.decompose()
        hit, nor = _closest_on_target(target, context.evaluated_depsgraph_get(), patch.matrix_world @ base_local)
        if hit is None:
            self.report({'ERROR'}, f"No surface found on '{target.name}'")
            return {'CANCELLED'}
        rot = (rot @ Z).rotation_difference(nor) @ rot
        patch.matrix_world = _place_matrix(rot, scale, base_local, hit, nor,
                                           patch.modifiers[SHRINK_NAME].offset)
        refresh_cuts(patch)
        return {'FINISHED'}


class OBJECT_OT_shrink_patch_duplicate(bpy.types.Operator):
    """Duplicate the patch and place the copy on its surface interactively"""
    bl_idname = "object.shrink_patch_duplicate"
    bl_label = "Duplicate on Surface"
    bl_options = {'REGISTER'}

    patch_name: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})
    linked: BoolProperty(name="Linked", description="Share the mesh data (like Alt+D)", default=False)

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and context.area is not None and context.area.type == 'VIEW_3D'

    def invoke(self, context, event):
        patch = bpy.data.objects.get(self.patch_name) if self.patch_name else context.active_object
        if patch_target(patch) is None:
            self.report({'ERROR'}, "Active object is not a patch applied to a surface")
            return {'CANCELLED'}
        new = duplicate_patch(patch, self.linked)
        _select_only(context, new)
        bpy.ops.object.shrink_patch_place('INVOKE_DEFAULT', patch_name=new.name, delete_on_cancel=True)
        return {'FINISHED'}


class OBJECT_OT_shrink_patch_origin_to_base(bpy.types.Operator):
    """Move the patch origin to the centre of its base group (needed for 'Stick While Moving')"""
    bl_idname = "object.shrink_patch_origin_to_base"
    bl_label = "Origin to Base"
    bl_options = {'REGISTER', 'UNDO'}

    patch_name: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def execute(self, context):
        patch = bpy.data.objects.get(self.patch_name) if self.patch_name else context.active_object
        if patch is None or patch.type != 'MESH':
            return {'CANCELLED'}
        group = _base_group_name(patch, _settings(context))
        if group is None:
            self.report({'ERROR'}, f"'{patch.name}' has no vertex group for the base")
            return {'CANCELLED'}
        if not set_origin_to_base(patch, group):
            self.report({'INFO'}, "Origin already on the base")
        return {'FINISHED'}


class OBJECT_OT_shrink_patch_add_cutter(bpy.types.Operator):
    """Turn the selected meshes into cutters of the active patch: they are parented to it and cut the surface with a Boolean"""
    bl_idname = "object.shrink_patch_add_cutter"
    bl_label = "Add Selected as Cutters"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        a = context.active_object
        return (context.mode == 'OBJECT' and a is not None and a.type == 'MESH'
                and any(o is not a and o.type == 'MESH' for o in context.selected_objects))

    def execute(self, context):
        patch = context.active_object
        surface = patch_target(patch)
        cutters = [o for o in context.selected_objects
                   if o is not patch and o.type == 'MESH' and o is not surface and patch_target(o) is None]
        if not cutters:
            self.report({'ERROR'}, "Select the cutter meshes, then the patch as active object")
            return {'CANCELLED'}
        for c in cutters:
            make_cutter(c, patch)
        if surface is not None:
            sync_cuts(patch, surface)
        _select_only(context, patch)
        self.report({'INFO'}, f"{len(cutters)} cutter(s) added to '{patch.name}'")
        return {'FINISHED'}


class OBJECT_OT_shrink_patch_autocut(bpy.types.Operator):
    """Generate (or remove) a cutter from the patch's own shape, so the surface is cut where the patch dips in. Click again to rebuild it"""
    bl_idname = "object.shrink_patch_autocut"
    bl_label = "Auto Cut"
    bl_options = {'REGISTER', 'UNDO'}

    patch_name: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})
    enable: BoolProperty(options={'SKIP_SAVE'})

    def execute(self, context):
        patch = bpy.data.objects.get(self.patch_name) if self.patch_name else context.active_object
        surface = patch_target(patch)
        if surface is None:
            return {'CANCELLED'}
        ensure_autocut(patch, self.enable)
        sync_cuts(patch, surface)
        return {'FINISHED'}


class OBJECT_OT_shrink_patch_toggle_cutters(bpy.types.Operator):
    """Show / hide the patch's cutters in the viewport (they keep cutting)"""
    bl_idname = "object.shrink_patch_toggle_cutters"
    bl_label = "Show Cutters"
    bl_options = {'REGISTER', 'UNDO'}

    patch_name: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def execute(self, context):
        patch = bpy.data.objects.get(self.patch_name) if self.patch_name else context.active_object
        cutters = patch_cutters(patch)
        if not cutters:
            return {'CANCELLED'}
        hide = not cutters[0].hide_get()
        for c in cutters:
            c.hide_set(hide)
        return {'FINISHED'}


class OBJECT_OT_shrink_patch_delete(bpy.types.Operator):
    """Delete the patch and its cutters, and close its cut in the surface"""
    bl_idname = "object.shrink_patch_delete"
    bl_label = "Delete Patch"
    bl_options = {'REGISTER', 'UNDO'}

    patch_name: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        patch = bpy.data.objects.get(self.patch_name) if self.patch_name else context.active_object
        if patch_target(patch) is None:
            return {'CANCELLED'}
        delete_patch(patch)
        return {'FINISHED'}


class OBJECT_OT_shrink_patch_select(bpy.types.Operator):
    """Select this object and make it active"""
    bl_idname = "object.shrink_patch_select"
    bl_label = "Select"
    bl_options = {'REGISTER', 'UNDO'}

    name: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def execute(self, context):
        obj = bpy.data.objects.get(self.name)
        if obj is None or obj.name not in context.view_layer.objects:
            return {'CANCELLED'}
        _select_only(context, obj)
        return {'FINISHED'}


class OBJECT_OT_shrink_patch_detach(bpy.types.Operator):
    """Detach the patch from its surface: removes the Shrink Patch modifiers, the parent and its cuts, keeps the object"""
    bl_idname = "object.shrink_patch_detach"
    bl_label = "Detach Patch"
    bl_options = {'REGISTER', 'UNDO'}

    patch_name: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def execute(self, context):
        patch = bpy.data.objects.get(self.patch_name) if self.patch_name else context.active_object
        if patch is None:
            return {'CANCELLED'}
        target = patch_target(patch)
        ensure_autocut(patch, False)
        remove_cuts(patch, target)
        if target is not None and patch.parent is target:
            parent_keep_transform(patch, None)
        for name in (CONFORM_NAME, SHRINK_NAME, TRANSFER_NAME):
            mod = patch.modifiers.get(name)
            if mod is not None:
                patch.modifiers.remove(mod)
        return {'FINISHED'}


# --------------------------------------------------------------------------- UI

def _ui_surface(context):
    """Surface shown in the panel: the active patch's surface, or the active object."""
    active = context.active_object
    surface = patch_target(active) or real_surface(active)
    if surface is None or surface.type != 'MESH' or is_cutter(surface):
        return None
    return surface


def _ui_active_patch(context):
    """The active object if it is a patch, else None."""
    active = context.active_object
    return active if active is not None and patch_target(active) is not None else None


# Shared placement of every panel: Sidebar (N) > "Shrink Patch" tab.
# Panel docstrings would show up as tooltips, so panels are described with comments.
class _ShrinkPatchPanel:
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Shrink Patch"


# Main panel: what is selected, the Place button, Stick While Moving.
class VIEW3D_PT_shrink_patch(_ShrinkPatchPanel, bpy.types.Panel):
    bl_label = "Shrink Patch"

    def draw(self, context):
        layout = self.layout
        settings = _settings(context)
        active = context.active_object
        others = [o for o in context.selected_objects if o is not active and o.type == 'MESH']
        col = layout.column(align=True)
        col.label(text=f"Patch:  {active.name if active else '-'}", icon='MESH_CIRCLE')
        col.label(text=f"Surface:  {others[0].name if len(others) == 1 else '-'}", icon='SPHERE')
        r = layout.row()
        r.scale_y = 1.4
        r.enabled = active is not None and active.type == 'MESH' and len(others) == 1
        r.operator(OBJECT_OT_shrink_patch_place.bl_idname, icon='RESTRICT_SELECT_OFF')
        layout.prop(settings, "stick_while_moving", icon='SNAP_ON' if settings.stick_while_moving else 'SNAP_OFF')


# Defaults used when a new patch is applied.
class VIEW3D_PT_shrink_patch_settings(_ShrinkPatchPanel, bpy.types.Panel):
    bl_label = "New Patch Settings"
    bl_parent_id = "VIEW3D_PT_shrink_patch"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        settings = _settings(context)
        active = context.active_object
        col = layout.column(align=True)
        if active is not None and active.type == 'MESH':
            col.prop_search(settings, "vertex_group", active, "vertex_groups", text="Base")
        else:
            col.prop(settings, "vertex_group", text="Base")
        col.prop(settings, "normals_group", text="Normals", icon='GROUP_VERTEX')
        col.prop(settings, "wrap_method")
        col = layout.column(align=True)
        row = col.row(align=True)
        row.prop(settings, "transfer_normals", toggle=True)
        sub = row.row(align=True)
        sub.active = settings.transfer_normals
        sub.prop(settings, "shade_smooth", toggle=True, text="Smooth Base")
        row = col.row(align=True)
        row.prop(settings, "parent_to_surface", toggle=True, icon='CON_CHILDOF')
        row.prop(settings, "conform_whole", toggle=True, text="Whole Patch", icon='MOD_SIMPLEDEFORM')
        col.prop(settings, "subdiv_after_wrap", toggle=True, text="Subdivision After Wrap", icon='MOD_SUBSURF')
        col.prop(settings, "auto_cut", toggle=True, icon='MOD_BOOLEAN')


class VIEW3D_PT_shrink_patch_subdiv(_ShrinkPatchPanel, bpy.types.Panel):
    bl_label = "Subdivision"
    bl_parent_id = "VIEW3D_PT_shrink_patch_settings"
    bl_options = {'DEFAULT_CLOSED'}

    def draw_header(self, context):
        self.layout.prop(_settings(context), "manage_subdiv", text="")

    def draw(self, context):
        layout = self.layout
        settings = _settings(context)
        row = layout.row(align=True)
        row.active = settings.manage_subdiv
        row.prop(settings, "subdiv_levels")
        row.prop(settings, "subdiv_render_levels")
        layout.operator(OBJECT_OT_shrink_patch_apply_subdiv.bl_idname, icon='FILE_REFRESH')


# Every patch applied to the surface, with per-patch buttons.
class VIEW3D_PT_shrink_patch_overview(_ShrinkPatchPanel, bpy.types.Panel):
    bl_label = "Patches"
    bl_parent_id = "VIEW3D_PT_shrink_patch"

    @classmethod
    def poll(cls, context):
        return _ui_surface(context) is not None

    def draw_header(self, context):
        surface = _ui_surface(context)
        self.layout.label(text=f"on {surface.name}  ({len(patches_on(context.scene, surface))})")

    def draw(self, context):
        layout = self.layout
        active = context.active_object
        items = patches_on(context.scene, _ui_surface(context))
        if not items:
            layout.label(text="No patches applied", icon='INFO')
        for p in items:
            row = layout.row(align=True)
            op = row.operator(OBJECT_OT_shrink_patch_select.bl_idname, text=p.name,
                              icon='RADIOBUT_ON' if p is active else 'RADIOBUT_OFF', depress=p is active)
            op.name = p.name
            row.operator(OBJECT_OT_shrink_patch_place.bl_idname, text="", icon='VIEW_PAN').patch_name = p.name
            row.operator(OBJECT_OT_shrink_patch_snap.bl_idname, text="", icon='SNAP_FACE').patch_name = p.name
            if patch_cutters(p):
                row.operator(OBJECT_OT_shrink_patch_toggle_cutters.bl_idname, text="",
                             icon='MOD_BOOLEAN').patch_name = p.name
            row.prop(p, "hide_viewport", text="", emboss=False)
            row.operator(OBJECT_OT_shrink_patch_detach.bl_idname, text="", icon='X').patch_name = p.name
            row.operator(OBJECT_OT_shrink_patch_delete.bl_idname, text="", icon='TRASH').patch_name = p.name


# The active patch: scale and actions; Deform / Normals / Cuts are its sub-panels.
class VIEW3D_PT_shrink_patch_active(_ShrinkPatchPanel, bpy.types.Panel):
    bl_label = "Active Patch"
    bl_parent_id = "VIEW3D_PT_shrink_patch"

    @classmethod
    def poll(cls, context):
        return _ui_active_patch(context) is not None

    def draw_header(self, context):
        self.layout.label(text=_ui_active_patch(context).name)

    def draw(self, context):
        layout = self.layout
        settings = _settings(context)
        active = _ui_active_patch(context)
        surface = patch_target(active)
        # vertical X / Y / Z like the Transform panel: drag down across them to edit all at once
        col = layout.column(align=True)
        col.use_property_split = True
        col.use_property_decorate = False
        col.prop(active, "scale", text="Scale")
        row = layout.row(align=True)
        row.operator(OBJECT_OT_shrink_patch_place.bl_idname, text="Move", icon='VIEW_PAN').patch_name = active.name
        row.operator(OBJECT_OT_shrink_patch_snap.bl_idname, text="Snap", icon='SNAP_FACE').patch_name = active.name
        row = layout.row(align=True)
        row.operator(OBJECT_OT_shrink_patch_duplicate.bl_idname, text="Duplicate", icon='DUPLICATE').patch_name = active.name
        row.operator(OBJECT_OT_shrink_patch_origin_to_base.bl_idname, icon='OBJECT_ORIGIN').patch_name = active.name
        group = _base_group_name(active, settings)
        if settings.stick_while_moving and group and _base_centroid_local(active, group).length > 1e-4:
            layout.label(text="Origin is not on the base: G will float it", icon='ERROR')
        row = layout.row(align=True)
        row.label(text=f"Parent: {active.parent.name if active.parent else 'none'}",
                  icon='CON_CHILDOF' if active.parent is surface else 'UNLINKED')
        row.operator(OBJECT_OT_shrink_patch_select.bl_idname, text=f"Select {surface.name}",
                     icon='SPHERE').name = surface.name


class VIEW3D_PT_shrink_patch_deform(_ShrinkPatchPanel, bpy.types.Panel):
    bl_label = "Deform"
    bl_parent_id = "VIEW3D_PT_shrink_patch_active"

    @classmethod
    def poll(cls, context):
        return _ui_active_patch(context) is not None

    def draw(self, context):
        layout = self.layout
        active = _ui_active_patch(context)
        col = layout.column(align=True)
        sw = active.modifiers.get(SHRINK_NAME)
        col.prop(sw, "show_viewport", text="Shrinkwrap", toggle=True)
        conform_mod = active.modifiers.get(CONFORM_NAME)
        fac_owner, fac_prop = conform_factor_path(active)
        row = col.row(align=True)
        op = row.operator(OBJECT_OT_shrink_patch_conform.bl_idname, icon='MOD_SIMPLEDEFORM',
                          text="Whole Patch: On" if conform_mod else "Whole Patch: Off",
                          depress=conform_mod is not None)
        op.patch_name, op.enable = active.name, conform_mod is None
        if fac_owner is not None:
            row.prop(fac_owner, fac_prop, text="Bend")
        if any(m.type == 'SUBSURF' for m in active.modifiers):
            after = subdiv_is_after_wrap(active)
            op = col.operator(OBJECT_OT_shrink_patch_order.bl_idname, icon='MOD_SUBSURF', depress=after,
                              text="Subdivision After Wrap: On" if after else "Subdivision After Wrap: Off")
            op.patch_name, op.subdiv_after_wrap = active.name, not after


class VIEW3D_PT_shrink_patch_normals(_ShrinkPatchPanel, bpy.types.Panel):
    bl_label = "Normals"
    bl_parent_id = "VIEW3D_PT_shrink_patch_active"
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        p = _ui_active_patch(context)
        return p is not None and p.modifiers.get(TRANSFER_NAME) is not None

    def draw_header(self, context):
        dt = _ui_active_patch(context).modifiers.get(TRANSFER_NAME)
        self.layout.prop(dt, "show_viewport", text="")

    def draw(self, context):
        active = _ui_active_patch(context)
        dt = active.modifiers.get(TRANSFER_NAME)
        col = self.layout.column()
        col.active = dt.show_viewport
        col.prop_search(dt, "vertex_group", active, "vertex_groups", text="Group")


class VIEW3D_PT_shrink_patch_cuts(_ShrinkPatchPanel, bpy.types.Panel):
    bl_label = "Cuts"
    bl_parent_id = "VIEW3D_PT_shrink_patch_active"
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return _ui_active_patch(context) is not None

    def draw(self, context):
        layout = self.layout
        active = _ui_active_patch(context)
        ac = autocut_of(active)
        row = layout.row(align=True)
        op = row.operator(OBJECT_OT_shrink_patch_autocut.bl_idname, icon='MOD_BOOLEAN', depress=ac is not None,
                          text="Auto Cut: On" if ac else "Auto Cut: Off")
        op.patch_name, op.enable = active.name, ac is None
        if ac is not None:
            op = row.operator(OBJECT_OT_shrink_patch_autocut.bl_idname, text="", icon='FILE_REFRESH')
            op.patch_name, op.enable = active.name, True
        cutters = patch_cutters(active)
        row = layout.row(align=True)
        row.label(text=f"Cutters: {len(cutters)}", icon='MOD_BOOLEAN')
        if cutters:
            row.operator(OBJECT_OT_shrink_patch_toggle_cutters.bl_idname, text="",
                         icon='HIDE_ON' if cutters[0].hide_get() else 'HIDE_OFF').patch_name = active.name
        row.operator(OBJECT_OT_shrink_patch_add_cutter.bl_idname, text="Add Selected", icon='ADD')


def _menu(self, context):
    """Entries added to the Object menu and the right-click menu."""
    layout = self.layout
    layout.separator()
    layout.operator(OBJECT_OT_shrink_patch_place.bl_idname, icon='MOD_SHRINKWRAP')
    if patch_target(context.active_object) is not None:
        layout.operator(OBJECT_OT_shrink_patch_duplicate.bl_idname, icon='DUPLICATE')
        layout.operator(OBJECT_OT_shrink_patch_snap.bl_idname, icon='SNAP_FACE')
    layout.operator(OBJECT_OT_shrink_patch_add_cutter.bl_idname, icon='MOD_BOOLEAN')


classes = (
    ShrinkPatchSettings,
    OBJECT_OT_shrink_patch_place,
    OBJECT_OT_shrink_patch_snap,
    OBJECT_OT_shrink_patch_conform,
    OBJECT_OT_shrink_patch_order,
    OBJECT_OT_shrink_patch_apply_subdiv,
    OBJECT_OT_shrink_patch_duplicate,
    OBJECT_OT_shrink_patch_origin_to_base,
    OBJECT_OT_shrink_patch_add_cutter,
    OBJECT_OT_shrink_patch_autocut,
    OBJECT_OT_shrink_patch_toggle_cutters,
    OBJECT_OT_shrink_patch_delete,
    OBJECT_OT_shrink_patch_select,
    OBJECT_OT_shrink_patch_detach,
    VIEW3D_PT_shrink_patch,             # parents before their sub-panels
    VIEW3D_PT_shrink_patch_settings,
    VIEW3D_PT_shrink_patch_subdiv,
    VIEW3D_PT_shrink_patch_overview,
    VIEW3D_PT_shrink_patch_active,
    VIEW3D_PT_shrink_patch_deform,
    VIEW3D_PT_shrink_patch_normals,
    VIEW3D_PT_shrink_patch_cuts,
)


# --------------------------------------------------------------------------- live sync
# A depsgraph handler notes what changed; a short timer acts once things settle:
#   1. an Auto Cut patch moved on its surface (G, Stick While Moving, undo...) -> rebuild its cutter
#   2. a patch deleted with X -> its orphaned cutters stop cutting (generated ones are removed)
#   3. the surface's modifiers / mesh changed -> the uncut proxy is re-synced

WATCH_DELAY = 0.35  # seconds of calm before acting
_watch = {"patches": set(), "surfaces": set(), "rel": {}, "placing": set(),
          "armed": False, "busy": False, "last": 0.0}


def _rel_key(patch):
    """Where the patch sits relative to its surface (moving the surface itself
    moves its patches rigidly and needs no rebuild)."""
    surface = patch_target(patch)
    if surface is None:
        return None
    m = surface.matrix_world.inverted_safe() @ patch.matrix_world
    return tuple(round(x, 5) for row in m for x in row)


def remember_placement(patch):
    """Store where the patch sits now, so the live sync can tell a real move."""
    key = _rel_key(patch)
    if key is not None:
        _watch["rel"][patch.name] = key


_UI_ONLY = {"name", "rna_type", "is_active", "show_expanded"}  # interface state, not geometry


def _modifier_signature(obj, skip_cuts=False):
    """Every setting of every modifier (cuts optionally skipped): equal signatures
    mean the proxy evaluates exactly like the surface."""
    sig = []
    for m in obj.modifiers:
        if skip_cuts and m.name.startswith(CUT_PREFIX):
            continue
        vals = [m.name, m.type]
        for prop in m.bl_rna.properties:
            if prop.is_readonly or prop.identifier in _UI_ONLY or prop.type == 'COLLECTION':
                continue
            v = getattr(m, prop.identifier, None)
            if prop.type == 'POINTER':
                v = getattr(v, "name", None)
            elif hasattr(v, "__len__") and not isinstance(v, str):
                v = tuple(v)
            vals.append(v)
        sig.append(tuple(vals))
    return tuple(sig)


def sync_proxy_if_needed(surface):
    """Re-copy the proxy if the surface's mesh or modifiers changed. True if it did."""
    proxy = _proxy_of(surface)
    if proxy is None:
        return False
    if proxy.data is surface.data and _modifier_signature(proxy) == _modifier_signature(surface, skip_cuts=True):
        return False
    ensure_proxy(surface)
    return True


def cleanup_orphan_cutters():
    """Cutters whose patch is gone (deleted with X, or detached) stop cutting."""
    removed = 0
    for coll in list(bpy.data.collections):
        surface = coll.get(SURFACE_PROP)
        if not isinstance(surface, bpy.types.Object) or surface.get(CUT_COLL_PROP) is not coll:
            continue
        changed = False
        for o in list(coll.objects):
            owner = o.parent
            if owner is not None and patch_target(owner) is surface:
                continue
            if o.get(AUTOCUT_PROP):
                mesh = o.data
                bpy.data.objects.remove(o)
                if mesh is not None and mesh.users == 0:
                    bpy.data.meshes.remove(mesh)
            else:  # a cutter you modelled: keep the object, it just stops cutting
                coll.objects.unlink(o)
            changed = True
            removed += 1
        if changed:
            _update_cut_modifier(surface)
    return removed


def _transform_running():
    """True while the user is dragging (G / R / S, or the Place operator)."""
    for win in bpy.context.window_manager.windows:
        for op in win.modal_operators:
            if op.bl_idname.startswith("TRANSFORM_OT") or op.bl_idname == "OBJECT_OT_shrink_patch_place":
                return True
    return False


def process_watch():
    """Act on everything the handler noted. Returns what it did (for tests)."""
    done = {"rebuilt": [], "proxies": [], "orphans": 0}
    _watch["busy"] = True
    try:
        for name in list(_watch["patches"]):
            patch = bpy.data.objects.get(name)
            if patch is None or name in _watch["placing"] or autocut_of(patch) is None:
                continue
            key = _rel_key(patch)
            if key is not None and key != _watch["rel"].get(name):
                refresh_cuts(patch)
                done["rebuilt"].append(name)
        for name in list(_watch["surfaces"]):
            surface = bpy.data.objects.get(name)
            if surface is not None and sync_proxy_if_needed(surface):
                done["proxies"].append(name)
        done["orphans"] = cleanup_orphan_cutters()
    finally:
        _watch["patches"].clear()
        _watch["surfaces"].clear()
        _watch["busy"] = False
    return done


def _watch_tick():
    """Timer callback: wait until things are calm and no drag is running, then
    process what the depsgraph handler recorded."""
    wait = WATCH_DELAY - (time.monotonic() - _watch["last"])
    if wait > 0:
        return wait
    if _transform_running():  # still dragging with G: wait for the release
        return 0.25
    _watch["armed"] = False
    try:
        process_watch()
    except Exception as e:
        print("Shrink Patch: live sync skipped:", e)
    return None


@persistent
def _on_depsgraph_update(scene, depsgraph):
    """Record moved Auto Cut patches and changed surfaces; arm the timer."""
    if _watch["busy"]:
        return  # our own changes
    for u in depsgraph.updates:
        ob = u.id.original
        if not isinstance(ob, bpy.types.Object) or ob.type != 'MESH':
            continue
        if u.is_updated_transform and ob.name not in _watch["placing"] and autocut_of(ob) is not None:
            _watch["patches"].add(ob.name)
        if u.is_updated_geometry and _proxy_of(ob) is not None:
            _watch["surfaces"].add(ob.name)
    _watch["last"] = time.monotonic()
    if not _watch["armed"]:
        _watch["armed"] = True
        bpy.app.timers.register(_watch_tick, first_interval=WATCH_DELAY)


def upgrade_existing_patches():
    """Bring patches made with older versions of the addon up to date. Patches are
    recognised from their modifiers, so nothing needs re-applying; this only fixes
    old formats (one Boolean per cutter, other normal mappings, missing proxy
    links) and touches nothing when the file is already current."""
    changed = 0
    patches = [o for o in bpy.data.objects if o.type == 'MESH' and patch_target(o) is not None]
    for p in patches:
        dt = p.modifiers.get(TRANSFER_NAME)
        if dt is not None and dt.loop_mapping != 'POLYINTERP_LNORPROJ':
            dt.loop_mapping = 'POLYINTERP_LNORPROJ'
            changed += 1
    surfaces = {patch_target(p) for p in patches}
    for surface in surfaces:
        legacy = [m for m in surface.modifiers
                  if m.type == 'BOOLEAN' and m.name.startswith(CUT_PREFIX) and m.name != CUTS_NAME]
        mine = [p for p in patches if patch_target(p) is surface]
        needs_proxy = any(patch_cutters(p) for p in mine) and _proxy_of(surface) is None
        if legacy or needs_proxy:
            for p in mine:
                if patch_cutters(p):
                    sync_cuts(p, surface)
            _update_cut_modifier(surface)
            changed += 1
    return changed


@persistent
def _on_load_post(*_args):
    """After a file loads: upgrade old patches, remember current placements."""
    _watch["rel"].clear()
    _watch["placing"].clear()
    try:
        upgrade_existing_patches()
        for o in bpy.data.objects:
            if o.type == 'MESH' and autocut_of(o) is not None:
                remember_placement(o)
    except Exception as e:  # never block a file from loading
        print("Shrink Patch: upgrade skipped:", e)


def _upgrade_after_enable():
    """One-shot timer: upgrade the file that was already open when the add-on was enabled."""
    _on_load_post()
    return None  # run once


def register():
    """Register classes, the scene settings, menus, handlers and the one-shot upgrade."""
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.shrink_patch = PointerProperty(type=ShrinkPatchSettings)
    bpy.types.VIEW3D_MT_object.append(_menu)
    bpy.types.VIEW3D_MT_object_context_menu.append(_menu)
    bpy.app.handlers.load_post.append(_on_load_post)
    bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph_update)
    # the file already open when the addon is enabled: upgrade it once Blender is idle
    bpy.app.timers.register(_upgrade_after_enable, first_interval=0.1)


def unregister():
    """Undo register(), including handlers and pending timers."""
    if _on_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load_post)
    if _on_depsgraph_update in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_on_depsgraph_update)
    if bpy.app.timers.is_registered(_watch_tick):
        bpy.app.timers.unregister(_watch_tick)
    _watch["armed"] = False
    if bpy.app.timers.is_registered(_upgrade_after_enable):
        bpy.app.timers.unregister(_upgrade_after_enable)
    bpy.types.VIEW3D_MT_object_context_menu.remove(_menu)
    bpy.types.VIEW3D_MT_object.remove(_menu)
    del bpy.types.Scene.shrink_patch
    for c in reversed(classes):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()
