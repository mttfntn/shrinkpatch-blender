"""Shrink Patch test suite (runs inside a background Blender).

Start it with tests/run.py, which launches an isolated Blender:
    python tests/run.py [--blender PATH] [--addon-dir DIR]
    python tests/run.py --installed dist/shrinkpatch-X.Y.Z.zip

By default the add-on is imported from --addon-dir (the folder that contains
the `shrinkpatch` package; the repository root) and registered by hand. With
--installed, the built package is installed as an extension in the throw-away
profile and enabled the way Blender does it (SHRINKPATCH_MODULE is then the
extension's module name). The user's own Blender and preferences are never used.
Each test builds its own scene with tests/fixtures.py.
"""

import importlib
import math
import os
import sys
import types
import unittest

import bmesh
import bpy
from mathutils import Matrix, Vector

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fixtures as fx  # noqa: E402

INSTALLED_MODULE = os.environ.get("SHRINKPATCH_MODULE")  # e.g. bl_ext.user_default.shrinkpatch
if INSTALLED_MODULE:
    import addon_utils
    sp = addon_utils.enable(INSTALLED_MODULE, default_set=True)
    assert sp is not None, f"could not enable {INSTALLED_MODULE}"
else:
    ADDON_DIR = os.environ.get("SHRINKPATCH_ADDON_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, ADDON_DIR)
    sp = importlib.import_module("shrinkpatch")


def setUpModule():
    print(f"SHRINKPATCH_UNDER_TEST {sp.__name__} from {sp.__file__}")
    if not INSTALLED_MODULE:
        sp.register()


def tearDownModule():
    if INSTALLED_MODULE:
        addon_utils.disable(INSTALLED_MODULE, default_set=True)
    else:
        sp.unregister()


class PatchCase(unittest.TestCase):
    """Fresh scene per test: a surface and helpers to apply a patch."""

    def setUp(self):
        fx.reset_scene()
        self.settings = bpy.context.scene.shrink_patch
        self.surface = fx.make_surface()

    def apply(self, patch, **overrides):
        for k, v in overrides.items():
            setattr(self.settings, k, v)
        created = sp.setup_modifiers(patch, self.surface, "base", self.settings)
        fx.select(patch)
        with bpy.context.temp_override(active_object=patch, object=patch, selected_objects=[patch]):
            self.assertEqual(bpy.ops.object.shrink_patch_snap(), {'FINISHED'})
        bpy.context.view_layer.update()
        return created

    def base_world(self, patch):
        return patch.matrix_world @ sp._base_centroid_local(patch, "base")

    def surface_distance(self, point):
        hit, _ = sp._closest_on_target(self.surface, bpy.context.evaluated_depsgraph_get(), point)
        return (point - hit).length


class TestRegistration(unittest.TestCase):
    def test_classes_handlers_and_settings(self):
        for c in sp.classes:
            self.assertTrue(c.is_registered, c.__name__)
        self.assertTrue(hasattr(bpy.types.Scene, "shrink_patch"))
        self.assertIn(sp._on_depsgraph_update, bpy.app.handlers.depsgraph_update_post)
        self.assertIn(sp._on_load_post, bpy.app.handlers.load_post)

    def test_unregister_and_register_again(self):
        sp.unregister()
        try:
            self.assertNotIn(sp._on_depsgraph_update, bpy.app.handlers.depsgraph_update_post)
            self.assertFalse(hasattr(bpy.types.Scene, "shrink_patch"))
        finally:
            sp.register()
        self.assertTrue(sp.OBJECT_OT_shrink_patch_place.is_registered)


class TestApplyRaised(PatchCase):
    def test_modifier_stack_and_groups(self):
        p = fx.make_patch()
        created = self.apply(p)
        self.assertIn(sp.SHRINK_NAME, created)
        names = [m.name for m in p.modifiers]
        self.assertEqual(names, [sp.SHRINK_NAME, "Subdivision", sp.TRANSFER_NAME])
        sub = p.modifiers["Subdivision"]
        self.assertEqual((sub.levels, sub.render_levels, sub.show_only_control_edges), (2, 2, True))
        dt = p.modifiers[sp.TRANSFER_NAME]
        self.assertEqual(dt.vertex_group, "base_normals")
        self.assertEqual(dt.loop_mapping, 'POLYINTERP_LNORPROJ')
        normals = p.vertex_groups["base_normals"].index
        count = sum(1 for v in p.data.vertices if any(g.group == normals for g in v.groups))
        self.assertEqual(count, fx.SEGMENTS)  # the outer ring only
        self.assertIs(p.parent, self.surface)
        self.assertIsNone(sp.autocut_of(p))  # raised patch: no cut

    def test_snap_puts_base_on_surface_and_aligns_normal(self):
        p = fx.make_patch(location=(0.7, 0.2, 3.6))
        self.apply(p)
        base = self.base_world(p)
        self.assertLess(self.surface_distance(base), 1e-3)
        z = (p.matrix_world.to_3x3() @ Vector((0, 0, 1))).normalized()
        radial = (base - self.surface.matrix_world.translation).normalized()
        self.assertGreater(z.dot(radial), 0.99)

    def test_whole_patch_keeps_thickness(self):
        p = fx.make_patch()
        self.apply(p, conform_whole=True)
        self.assertEqual(p.modifiers[0].name, sp.CONFORM_NAME)
        # measure the bend alone: the Subdivision after it smooths the bent cage
        # (by design), which would hide what the conform itself does
        for n in (sp.SHRINK_NAME, sp.TRANSFER_NAME, "Subdivision"):
            p.modifiers[n].show_viewport = False
        p.modifiers[sp.CONFORM_NAME].show_viewport = False
        flat = fx.evaluated_world_verts(p)
        p.modifiers[sp.CONFORM_NAME].show_viewport = True
        bent = fx.evaluated_world_verts(p)
        base_z = sp._base_centroid_local(p, "base").z
        inv = p.matrix_world.inverted()
        scale = p.matrix_world.to_scale().x
        worst = 0.0
        dg = bpy.context.evaluated_depsgraph_get()
        for f, b in zip(flat, bent):
            hit, nor = sp._closest_on_target(self.surface, dg, b)
            worst = max(worst, abs((b - hit).dot(nor) - ((inv @ f).z - base_z) * scale))
        # the conform offsets along the smooth normal, the check uses the face normal
        # of the subdivided surface: they differ by ~0.2% of the patch size
        self.assertLess(worst, 2.5e-3)

    def test_parent_follows_surface(self):
        p = fx.make_patch()
        self.apply(p)
        before = self.base_world(p).copy()
        self.surface.location.x += 3.0
        bpy.context.view_layer.update()
        self.assertAlmostEqual((self.base_world(p) - before).x, 3.0, places=4)

    def test_order_toggle_keeps_mirror_first(self):
        p = fx.make_patch()
        p.modifiers.new("Mirror", 'MIRROR')
        self.apply(p, conform_whole=True)
        names = lambda: [m.name for m in p.modifiers]
        self.assertEqual(names(), ["Mirror", sp.CONFORM_NAME, sp.SHRINK_NAME, "Subdivision", sp.TRANSFER_NAME])
        sp.order_stack(p, False)
        self.assertEqual(names(), ["Mirror", "Subdivision", sp.CONFORM_NAME, sp.SHRINK_NAME, sp.TRANSFER_NAME])
        self.assertFalse(sp.subdiv_is_after_wrap(p))
        sp.order_stack(p, True)
        self.assertTrue(sp.subdiv_is_after_wrap(p))

    def test_detach_removes_everything_and_keeps_transform(self):
        p = fx.make_patch()
        self.apply(p)
        mw = p.matrix_world.copy()
        with bpy.context.temp_override(active_object=p, object=p, selected_objects=[p]):
            bpy.ops.object.shrink_patch_detach(patch_name=p.name)
        bpy.context.view_layer.update()
        self.assertEqual([m.name for m in p.modifiers], ["Subdivision"])
        self.assertIsNone(p.parent)
        self.assertLess((p.matrix_world.translation - mw.translation).length, 1e-5)

    def test_origin_to_base(self):
        p = fx.make_patch()
        for v in p.data.vertices:
            v.co.z += 0.3  # base no longer at the origin
        before = [p.matrix_world @ v.co for v in p.data.vertices]
        self.assertTrue(sp.set_origin_to_base(p, "base"))
        bpy.context.view_layer.update()
        after = [p.matrix_world @ v.co for v in p.data.vertices]
        self.assertLess(max((a - b).length for a, b in zip(after, before)), 1e-5)
        self.assertLess(sp._base_centroid_local(p, "base").length, 1e-6)

    def test_apply_subdiv_to_all(self):
        p = fx.make_patch()
        self.apply(p)
        self.settings.subdiv_levels = 1
        self.settings.subdiv_render_levels = 3
        self.assertEqual(bpy.ops.object.shrink_patch_apply_subdiv(), {'FINISHED'})
        sub = p.modifiers["Subdivision"]
        self.assertEqual((sub.levels, sub.render_levels), (1, 3))


class TestInsetsAndCuts(PatchCase):
    def make_inset(self, **overrides):
        p = fx.make_patch("Inset", fx.INSET_RINGS, location=(0.0, 0.0, 3.0))
        self.apply(p, **overrides)
        return p

    def test_autocut_opens_the_surface(self):
        p = self.make_inset()
        cut = sp.autocut_of(p)
        self.assertIsNotNone(cut)
        self.assertTrue(cut.hide_get())
        bm = bmesh.new()
        bm.from_mesh(cut.data)
        self.assertEqual(sum(1 for e in bm.edges if not e.is_manifold), 0)
        bm.free()
        booleans = [m for m in self.surface.modifiers if m.type == 'BOOLEAN']
        self.assertEqual([m.name for m in booleans], [sp.CUTS_NAME])
        self.assertEqual(booleans[0].operand_type, 'COLLECTION')
        self.assertEqual(booleans[0].solver, 'MANIFOLD')
        self.assertIn(cut.name, booleans[0].collection.objects)
        # patches wrap the uncut proxy
        proxy = sp.wrap_object(self.surface)
        self.assertIsNot(proxy, self.surface)
        self.assertIs(p.modifiers[sp.SHRINK_NAME].target, proxy)
        self.assertTrue(proxy.hide_viewport and proxy.hide_render)
        # centre: the surface is opened (first hit is the inset floor, well below the rim)
        self.assertGreater(fx.ray_down_local(self.surface, (0, 0), p), 2.0 + 0.4)

    def test_surface_never_shows_inside_the_inset(self):
        p = self.make_inset()
        worst = 0.0
        for i in range(-10, 11):
            for j in range(-10, 11):
                x, y = i / 10 * 0.9, j / 10 * 0.9
                if x * x + y * y > 0.9 ** 2:
                    continue
                dp, ds = fx.ray_down_local(p, (x, y), p), fx.ray_down_local(self.surface, (x, y), p)
                if dp is not None and ds is not None:
                    worst = max(worst, dp - ds)
        self.assertLess(worst, 1e-4)

    def test_inset_without_normal_transfer(self):
        # regression: with Transfer Normals off there is no edge group and the
        # cutter build raised TypeError (1.12.0)
        p = self.make_inset(transfer_normals=False)
        self.assertIsNone(p.modifiers.get(sp.TRANSFER_NAME))
        self.assertIsNotNone(sp.autocut_of(p))
        self.assertGreater(fx.ray_down_local(self.surface, (0, 0), p), 2.0 + 0.4)

    def test_proxy_in_sync_is_not_rebuilt(self):
        # regression: the cut Boolean is the surface's active modifier; comparing that
        # UI flag made the proxy look out of date forever, so it was rebuilt (and every
        # patch re-evaluated) on each check (1.12.0)
        self.make_inset()
        self.assertTrue(self.surface.modifiers[sp.CUTS_NAME].is_active)
        self.assertFalse(sp.sync_proxy_if_needed(self.surface))
        bpy.context.view_layer.update()
        self.assertEqual(sp.process_watch()["proxies"], [])

    def test_duplicate_and_delete_keep_the_collection_consistent(self):
        p = self.make_inset()
        coll = sp.cut_collection(self.surface, create=False)
        d = sp.duplicate_patch(p)
        sp.refresh_cuts(d, self.settings)
        dcut = sp.autocut_of(d)
        self.assertEqual(dcut.name, f"{d.name}_autocut")
        self.assertTrue(dcut.hide_get())
        self.assertIsNot(dcut.data, sp.autocut_of(p).data)
        self.assertEqual(len(coll.objects), 2)
        sp.delete_patch(d)
        self.assertEqual(len(coll.objects), 1)
        self.assertEqual(len([m for m in self.surface.modifiers if m.type == 'BOOLEAN']), 1)

    def test_detach_last_inset_removes_the_boolean(self):
        p = self.make_inset()
        with bpy.context.temp_override(active_object=p, object=p, selected_objects=[p]):
            bpy.ops.object.shrink_patch_detach(patch_name=p.name)
        self.assertIsNone(sp.autocut_of(p))
        self.assertEqual([m.type for m in self.surface.modifiers], ['SUBSURF'])

    def test_manual_cutter(self):
        p = fx.make_patch()
        self.apply(p)
        me = bpy.data.meshes.new("c")
        bm = bmesh.new()
        bmesh.ops.create_cube(bm, size=0.3)
        bm.to_mesh(me)
        bm.free()
        cutter = bpy.data.objects.new("MyCutter", me)
        bpy.context.scene.collection.objects.link(cutter)
        cutter.matrix_world = Matrix.Translation(self.base_world(p))
        with bpy.context.temp_override(active_object=p, object=p, selected_objects=[p, cutter]):
            self.assertEqual(bpy.ops.object.shrink_patch_add_cutter(), {'FINISHED'})
        self.assertIs(cutter.parent, p)
        self.assertIn(cutter, sp.patch_cutters(p))
        self.assertIn(cutter.name, sp.cut_collection(self.surface, create=False).objects)

    def test_autocut_toggle_operator(self):
        p = self.make_inset()
        with bpy.context.temp_override(active_object=p, object=p, selected_objects=[p]):
            bpy.ops.object.shrink_patch_autocut(patch_name=p.name, enable=False)
            self.assertIsNone(sp.autocut_of(p))
            bpy.ops.object.shrink_patch_autocut(patch_name=p.name, enable=True)
            self.assertIsNotNone(sp.autocut_of(p))
            bpy.ops.object.shrink_patch_toggle_cutters(patch_name=p.name)
            self.assertFalse(sp.autocut_of(p).hide_get())

    def test_duplicated_surface_gets_its_own_proxy_and_cuts(self):
        p = self.make_inset()
        copy = self.surface.copy()
        bpy.context.scene.collection.objects.link(copy)
        self.assertIs(sp.wrap_object(copy), copy)  # inherited properties are ignored
        self.assertIsNone(sp.cut_collection(copy, create=False))
        q = fx.make_patch("Inset2", fx.INSET_RINGS, location=(0.0, 0.0, 3.0))
        sp.setup_modifiers(q, copy, "base", self.settings)
        self.assertIsNot(sp.wrap_object(copy), sp.wrap_object(self.surface))
        self.assertNotIn(sp.autocut_of(q).name, sp.cut_collection(self.surface, create=False).objects)


class TestLiveSync(PatchCase):
    """The depsgraph handler records changes; process_watch() is what the timer runs."""

    def test_move_rebuilds_cut(self):
        p = fx.make_patch("Inset", fx.INSET_RINGS, location=(0.0, 0.0, 3.0))
        self.apply(p)
        before = [v.co.copy() for v in sp.autocut_of(p).data.vertices[:40]]
        fx.rotate_about(p, self.surface.matrix_world.translation, 0.35)
        bpy.context.view_layer.update()
        done = sp.process_watch()
        self.assertIn(p.name, done["rebuilt"])
        after = [v.co for v in sp.autocut_of(p).data.vertices[:40]]
        self.assertNotEqual(before, after)

    def test_moving_the_surface_rebuilds_nothing(self):
        p = fx.make_patch("Inset", fx.INSET_RINGS, location=(0.0, 0.0, 3.0))
        self.apply(p)
        sp.process_watch()
        self.surface.location.x += 2.0
        bpy.context.view_layer.update()
        self.assertEqual(sp.process_watch()["rebuilt"], [])

    def test_delete_with_x_removes_orphan_cutter(self):
        p = fx.make_patch("Inset", fx.INSET_RINGS, location=(0.0, 0.0, 3.0))
        self.apply(p)
        cut_name = sp.autocut_of(p).name
        bpy.data.objects.remove(p)
        bpy.context.view_layer.update()
        self.assertEqual(sp.process_watch()["orphans"], 1)
        self.assertNotIn(cut_name, bpy.data.objects)
        self.assertEqual([m.type for m in self.surface.modifiers], ['SUBSURF'])

    def test_surface_change_syncs_proxy(self):
        p = fx.make_patch("Inset", fx.INSET_RINGS, location=(0.0, 0.0, 3.0))
        self.apply(p)
        self.surface.modifiers["Subdivision"].levels = 1
        bpy.context.view_layer.update()
        self.assertIn(self.surface.name, sp.process_watch()["proxies"])
        self.assertEqual(sp.wrap_object(self.surface).modifiers["Subdivision"].levels, 1)
        self.assertEqual(sp.process_watch()["proxies"], [])  # in sync: nothing more to do


class TestUpgrade(PatchCase):
    def test_legacy_file_is_upgraded_once(self):
        p = fx.make_patch("Inset", fx.INSET_RINGS, location=(0.0, 0.0, 3.0))
        self.apply(p)
        # rebuild the pre-1.9 layout: one Boolean per cutter, no proxy, other mapping
        cut = sp.autocut_of(p)
        self.surface.modifiers.remove(self.surface.modifiers[sp.CUTS_NAME])
        old = self.surface.modifiers.new(sp.CUT_PREFIX + cut.name, 'BOOLEAN')
        old.object = cut
        proxy = sp.wrap_object(self.surface)
        p.modifiers[sp.SHRINK_NAME].target = self.surface
        p.modifiers[sp.TRANSFER_NAME].object = self.surface
        p.modifiers[sp.TRANSFER_NAME].loop_mapping = 'NEAREST_POLY'
        bpy.data.objects.remove(proxy)
        for k in (sp.PROXY_PROP, sp.CUT_COLL_PROP):
            del self.surface[k]
        self.assertGreater(sp.upgrade_existing_patches(), 0)
        self.assertEqual([m.name for m in self.surface.modifiers if m.type == 'BOOLEAN'], [sp.CUTS_NAME])
        self.assertEqual(p.modifiers[sp.TRANSFER_NAME].loop_mapping, 'POLYINTERP_LNORPROJ')
        self.assertIsNot(p.modifiers[sp.SHRINK_NAME].target, self.surface)
        self.assertEqual(sp.upgrade_existing_patches(), 0)


class TestSettings(unittest.TestCase):
    def setUp(self):
        fx.reset_scene()

    def test_stick_while_moving_restores_snapping(self):
        ts = bpy.context.scene.tool_settings
        ts.use_snap = False
        ts.snap_elements_base = {'VERTEX'}
        ts.snap_target = 'CLOSEST'
        ts.use_snap_align_rotation = False
        ts.use_snap_translate = False
        before = (ts.use_snap, set(ts.snap_elements_base), ts.snap_target, ts.use_snap_align_rotation,
                  ts.use_snap_translate)
        bpy.context.scene.shrink_patch.stick_while_moving = True
        self.assertTrue(ts.use_snap and ts.use_snap_align_rotation)
        self.assertEqual(set(ts.snap_elements_base), {'FACE'})
        bpy.context.scene.shrink_patch.stick_while_moving = False
        after = (ts.use_snap, set(ts.snap_elements_base), ts.snap_target, ts.use_snap_align_rotation,
                 ts.use_snap_translate)
        self.assertEqual(after, before)


class TestProxyCopy(unittest.TestCase):
    """The proxy copies the surface's modifiers; a property that fails to copy
    would leave the proxy different from the surface and re-sync forever."""

    def test_common_modifiers_copy_exactly(self):
        fx.reset_scene()
        s = fx.make_surface()
        helper = bpy.data.objects.new("Helper", None)
        bpy.context.scene.collection.objects.link(helper)
        for t in ('MIRROR', 'BEVEL', 'SOLIDIFY', 'WEIGHTED_NORMAL', 'TRIANGULATE', 'SIMPLE_DEFORM',
                  'DISPLACE', 'SMOOTH', 'CAST', 'ARRAY'):
            m = s.modifiers.new(t.title(), t)
            if t == 'MIRROR':
                m.mirror_object = helper
        sp.ensure_proxy(s)
        proxy = sp.wrap_object(s)
        self.assertEqual(sp._modifier_signature(proxy), sp._modifier_signature(s, skip_cuts=True))
        self.assertFalse(sp.sync_proxy_if_needed(s))


class TestPanels(PatchCase):
    """Draw every panel with a stand-in layout: catches typos in property and
    operator names without needing a window."""

    class Layout:
        def __init__(self, log):
            self.log, self.active, self.enabled, self.scale_y = log, True, True, 1.0
            self.use_property_split = self.use_property_decorate = False

        def _sub(self, *a, **k):
            return TestPanels.Layout(self.log)
        row = column = box = split = _sub

        def label(self, **k):
            pass

        def separator(self, **k):
            pass

        def prop(self, owner, name, **k):
            if not hasattr(owner, name):
                self.log.append(f"missing property {name}")

        def prop_search(self, owner, name, other, other_name, **k):
            if not (hasattr(owner, name) and hasattr(other, other_name)):
                self.log.append(f"missing property {name}/{other_name}")

        def operator(self, idname, **k):
            group, op = idname.split(".")
            if not hasattr(getattr(bpy.ops, group), op):
                self.log.append(f"missing operator {idname}")
            return types.SimpleNamespace()

    PANELS = ("VIEW3D_PT_shrink_patch", "VIEW3D_PT_shrink_patch_settings", "VIEW3D_PT_shrink_patch_subdiv",
              "VIEW3D_PT_shrink_patch_overview", "VIEW3D_PT_shrink_patch_active",
              "VIEW3D_PT_shrink_patch_deform", "VIEW3D_PT_shrink_patch_normals", "VIEW3D_PT_shrink_patch_cuts")

    def draw_all(self, active, selected):
        log, shown = [], []
        with bpy.context.temp_override(active_object=active, object=active, selected_objects=selected):
            ctx = bpy.context
            for name in self.PANELS:
                P = getattr(sp, name)
                if hasattr(P, "poll") and not P.poll(ctx):
                    continue
                shown.append(name)
                fake = types.SimpleNamespace(layout=self.Layout(log))
                if hasattr(P, "draw_header"):
                    P.draw_header(fake, ctx)
                P.draw(fake, ctx)
        return log, shown

    def test_panels_draw_in_every_context(self):
        p = fx.make_patch("Inset", fx.INSET_RINGS, location=(0.0, 0.0, 3.0))
        self.apply(p, conform_whole=True)
        q = fx.make_patch("Fresh", location=(3.0, 0.0, 3.0))
        for active, selected, expect in ((p, [p], 8), (self.surface, [self.surface], 4),
                                         (q, [self.surface, q], 4)):
            log, shown = self.draw_all(active, selected)
            self.assertEqual(log, [], (active.name, log))
            self.assertEqual(len(shown), expect, (active.name, shown))


def main():
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2, stream=sys.stdout).run(suite)
    print(f"SHRINKPATCH_TESTS ran={result.testsRun} failures={len(result.failures)} errors={len(result.errors)}")
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
