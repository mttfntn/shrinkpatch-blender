# Shrink Patch: handover

This document is for the next person who works on the add-on. For using the add-on, see [README.md](README.md). For AI assistants, the same content in compact form is in [AGENTS.md](AGENTS.md).

## What the project is

Shrink Patch is a Blender extension (add-on) that places modelled **patches** onto a **surface**. While you place a patch, it follows the mouse. Once placed, the add-on:

- wraps the patch's base onto the surface (Shrinkwrap);
- transfers the surface normals for seamless shading (Data Transfer);
- can bend the whole patch along the curvature (Geometry Nodes);
- for **insets**, cuts the surface open with a cutter generated from the patch's own shape (Boolean).

Patches stay live: moving, duplicating, deleting or changing the surface keeps everything consistent.

- **Author:** Matteo Fontana. **License:** GPL-3.0-or-later. **Not a fork:** there is no upstream project.
- **Repository:** <https://github.com/mttfntn/shrinkpatch-blender> (public), branch `main`. Issues and pull requests use the forms in `.github/`; contributors start from [CONTRIBUTING.md](CONTRIBUTING.md).

## What you need

| What | Version / where |
|---|---|
| Blender | **5.3** (the only supported version; the manifest refuses other versions) |
| Python | Any Python 3.9+ to run `tests/run.py` (Blender brings its own Python for the tests themselves) |
| git, GitHub CLI (`gh`) | For releases and checking CI |

The tests find Blender through `--blender PATH`, then `$BLENDER`, then the usual install folders (`C:\Program Files\Blender Foundation\Blender 5.3*`, macOS `/Applications`, `blender` on `PATH`).

## How the code is organised

| Path | What it is |
|---|---|
| `shrinkpatch/__init__.py` | The whole add-on, in one module. Its docstring describes the architecture; sections are marked with `# ----` banners. |
| `shrinkpatch/blender_manifest.toml` | Extension metadata: id, version, supported Blender (5.3.0 to <5.4.0), license, build exclusions |
| `shrinkpatch/LICENSE` | Copy of `LICENSE`, shipped inside the package (CI checks the two are identical) |
| `tests/run.py` | Starts an isolated background Blender and runs a script in it (the tests by default) |
| `tests/test_shrinkpatch.py` | The test suite: 27 tests, unittest |
| `tests/fixtures.py` | Builds the test scenes procedurally: sphere surface, raised and inset patches |
| `tests/behaviour_snapshot.py` | Records what the add-on does for 192 option combinations, as JSON |
| `tests/compare_snapshots.py` | Compares two snapshots with the measured noise tolerances |
| `.github/actions/setup-blender/` | CI: downloads Blender (official server plus mirrors, retries, SHA-256), caches it |
| `.github/workflows/tests.yml` | CI on every push to `main` and every pull request (tests and build; no artifacts are stored) |
| `.github/workflows/release.yml` | Release on a `vX.Y.Z` tag |
| `README.md` | User guide |
| `CONTRIBUTING.md`, `SECURITY.md` | How to report bugs / send pull requests; how to report security problems privately |
| `.github/ISSUE_TEMPLATE/`, `.github/pull_request_template.md` | Bug and feature forms (blank issues disabled); pull request checklist |
| `HANDOVER.md`, `AGENTS.md` | This handover. Not shipped, because the package is only `shrinkpatch/`. |

Not in the repo (see `.gitignore`): `.blend` scenes (the demo scene stays local), `dist/` and `*.zip` (build output), `__pycache__`.

### Sections of `__init__.py`, in order

1. **Settings:** `ShrinkPatchSettings` (`Scene.shrink_patch`), the defaults for new patches.
2. **Patch / proxy / cut model:** `patch_target`, `real_surface`, `wrap_object`, `ensure_proxy`, `cut_collection`, `_update_cut_modifier`, `sync_cuts`, `set_cuts_active`.
3. **Auto cutter:** `is_inset`, `build_autocut_mesh`, `ensure_autocut`, `refresh_cuts`.
4. **Geometry helpers:** `_closest_on_target`, `_ray_on_target`, `_place_matrix`, base centroid and footprint, normals group, parenting.
5. **Whole Patch:** `_conform_node_group` builds the Geometry Nodes group; `set_conform` adds it to a patch.
6. **Stack and setup:** `apply_subdiv_settings`, `order_stack`, and **`setup_modifiers`**, which is the entry point that turns a mesh into a patch.
7. **Operators:** `OBJECT_OT_shrink_patch_place` (the modal), then Snap, Duplicate, Detach, Delete, cutters, and so on.
8. **UI:** the panels (Sidebar › Shrink Patch) and the menu entries.
9. **Live sync:** `_on_depsgraph_update` (handler), `_watch_tick` (timer), `process_watch`.
10. **Upgrade and registration:** `upgrade_existing_patches`, `_on_load_post`, `register`, `unregister`.

## How the main feature works

```
setup_modifiers(patch, surface)          ← Place, Snap, Duplicate all go through here
  ├─ sync_cuts: cutters → surface's cutter collection → single Boolean SP_Cut_All on the surface
  │            and, if there are cuts, the hidden proxy (surface copy without the cuts)
  ├─ Shrinkwrap (base group) → targets the proxy (or the surface)
  ├─ Data Transfer (base_normals group, Projected Face Interpolated) → same target
  ├─ Conform (Geometry Nodes) if Whole Patch → raycasts the proxy along local −Z
  ├─ Subdivision (Catmull-Clark 2/2) and order_stack: Conform > Shrinkwrap > Subdivision > Normals
  ├─ parent to the surface
  └─ inset? → ensure_autocut → build_autocut_mesh → the cutter joins the collection
```

- **The cutter** (`build_autocut_mesh`) is built from the evaluated patch at one Subdivision level lower, with its vertices snapped onto the fine surface. Its inside is pushed below the patch by a margin plus the measured "sag" of the coarse faces. At the contact edge (the `base_normals` group) it is pushed *above* the surface instead, so the cut edge hides under the flange. Its rim is extruded up and capped above both the rim and the base centre.
- **The proxy** shares the surface's mesh. It copies the surface's modifiers except the `SP_Cut_*` ones, so patches wrap onto the uncut shape. `sync_proxy_if_needed` re-copies it only when a modifier setting really differs.
- **Live sync:** the depsgraph handler records moved Auto Cut patches (compared with the remembered placement relative to the surface) and changed surfaces. A timer waits 0.35 s of calm, and for any G/R/S or Place modal to finish, then runs `process_watch`: it rebuilds moved cuts, re-syncs proxies and removes the cutters of deleted patches.
- **Persistence:** nothing is kept in memory. A patch is recognised by its `ShrinkPatch_Shrinkwrap` modifier. The links between objects are stored as custom properties (they are listed in the module docstring).

## How to make a change

1. Read the module docstring and the section you're touching.
2. Make the change. If it fixes a bug, **add a test that fails before the fix** (see `TestInsetsAndCuts.test_inset_without_normal_transfer` for the pattern).
3. Run the tests from source and from the built package (below).
4. If the change could alter results, compare behaviour with the previous version:

   ```powershell
   git worktree add ..\shrinkpatch-old HEAD
   python tests/run.py --addon-dir ..\shrinkpatch-old --script tests/behaviour_snapshot.py -- old.json
   python tests/run.py --script tests/behaviour_snapshot.py -- new.json
   python tests/compare_snapshots.py old.json new.json
   git worktree remove ..\shrinkpatch-old
   ```

   0 differences means the behaviour is unchanged. Explain every difference before committing. Runs of the same version give 0 differences, so a difference is real.
5. Try it by hand in Blender (see the manual checklist below): the interactive placement can't be tested automatically.
6. Make small commits with messages that say what changed and how it was verified.

## How to run the tests

```powershell
python tests/run.py
```

```powershell
python tests/run.py --installed dist/shrinkpatch-1.13.0.zip
```

- The **first command** imports the add-on from the repo.
- The **second** installs the built package as an extension and tests that. Build it first, see below.
- **Isolation:** Blender runs in background with `--factory-startup` and a throw-away profile (`BLENDER_USER_RESOURCES` pointing to a temporary folder), so your own Blender, add-ons and preferences are never used.
- **Expected result:** `SHRINKPATCH_TESTS ran=27 failures=0 errors=0`, with exit code 0.

**Manual checklist** (needs a window, not covered by the tests): Place on Surface (mouse, Wheel, Shift/Ctrl/Alt+Wheel, D, LMB, RMB), the orange preview ring, Duplicate on Surface, G with Stick While Moving on an inset, X on an inset, and how the panel looks.

## How to make a release

1. Set the new version in **three places**: `shrinkpatch/blender_manifest.toml` (`version`), `bl_info["version"]` in `__init__.py`, and `README.md` (the "Version" line and the version history).
2. Run both test commands above. Build locally to check:

   ```powershell
   mkdir dist; & "C:\Program Files\Blender Foundation\Blender 5.3\blender.exe" -b --factory-startup --command extension build --source-dir shrinkpatch --output-dir dist
   ```

3. Commit, then create an **annotated** tag whose message becomes the release notes, and push:

   ```powershell
   git tag -a v1.2.3 -m "Shrink Patch 1.2.3: what changed"
   ```

   ```powershell
   git push origin main v1.2.3
   ```

4. GitHub Actions (`release.yml`) runs the full tests, checks that the tag matches the manifest version, builds the zip, and publishes the release with the zip attached. Check it with `gh run list` and `gh release view v1.2.3`.
5. **The README badges update themselves**: release, license and language come from GitHub, the Blender version from the manifest on `main`, and `tests` is GitHub's own badge. Nothing to edit by hand.

## Updates from the original project

Not applicable: Shrink Patch is not a fork, so there is no upstream to merge from.

## Things to know (pitfalls found in this project)

- **Blender 5.3, Geometry Nodes modifier inputs:** set them with `mod.properties.inputs.<identifier>.value`. The old `mod["Socket_2"] = …` raises `TypeError` in 5.3.
- **Panel docstrings become tooltips.** Describe panels with `#` comments. Operator docstrings are tooltips too, so keep them accurate and written for users.
- **`matrix_world` can be stale inside a script** right after changing location, scale or parent. `parent_keep_transform` rebuilds the matrix from `matrix_basis` for this reason.
- **`Object.copy()` drops the hidden state** (it is per view layer). `duplicate_patch` copies it explicitly.
- **A duplicated surface inherits the custom properties** pointing to the original's proxy and cutter collection. The links are only trusted if they point both ways (`_proxy_of`, `cut_collection`).
- **Modifier properties include UI state** (`is_active`, `show_expanded`). Comparing those made the proxy look out of date forever. `_UI_ONLY` excludes them.
- **The manifest tagline is limited to 64 characters**, otherwise `extension validate` and `extension build` fail. Also, `extension build` doesn't create the output folder.
- **The Manifold Boolean triangulates slightly differently on each run.** For that reason the behaviour snapshot records the cut surface by its shape (area and ray hits), not by its vertices.
- **In background mode timers don't run but depsgraph handlers do.** The tests call `process_watch()` directly where the timer would.
- **`build_autocut_mesh` lowers the patch's Subdivision by one level for a moment** (two extra evaluations) and restores it in a `finally`.
- **The contact edge is deliberately not cut.** The surface and the flange coincide there within less than 0.001.

## Known limits

- **The interactive placement modal** (`OBJECT_OT_shrink_patch_place`) has no automated test: it needs a window and mouse events.
- **Refreshing one inset's cut re-evaluates every patch on the same surface** (measured: 9 objects for 6 insets, about 40 ms). The cause wasn't pinned down: the obvious candidate, re-copying the proxy each time, was ruled out by measurement. Worth investigating if scenes get large.
- **The proxy evaluates the surface's modifiers a second time.** On very dense surfaces this costs speed.
- **Automatic updates** (cut rebuild, cutter cleanup, proxy sync) don't create their own undo step.
- **While dragging an inset its cut is paused**, so the surface covers it until release.
- **Hand-made cutters must be closed meshes**, as the Manifold Boolean requires.
