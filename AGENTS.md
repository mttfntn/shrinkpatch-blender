# AGENTS.md: Shrink Patch

Compact guide for AI coding assistants. The human version, with more context, is [HANDOVER.md](HANDOVER.md).

## Project

- **What it is:** a Blender **5.3-only** extension (single module `shrinkpatch/__init__.py`, GPL-3.0-or-later, author Matteo Fontana, not a fork). It places patch meshes on a surface: Shrinkwrap the `base` group, Data Transfer custom normals from `base_normals`, optional Geometry Nodes bend, auto-cut insets with one collection Boolean on the surface, wrap onto a hidden uncut proxy, live sync via a depsgraph handler and a timer.
- **Repo:** `https://github.com/mttfntn/shrinkpatch-blender` (private). Branch `main`; release tags `vX.Y.Z` (annotated; the message becomes the release notes).

## Commands

Run from the repo root:

| Task | Command |
|---|---|
| Tests from source | `python tests/run.py` |
| Build | `mkdir dist` then `blender -b --factory-startup --command extension build --source-dir shrinkpatch --output-dir dist` |
| Tests on the built package | `python tests/run.py --installed dist/shrinkpatch-X.Y.Z.zip` |
| Validate the manifest | `blender -b --factory-startup --command extension validate shrinkpatch` |
| Behaviour snapshot | `python tests/run.py [--addon-dir OLD] --script tests/behaviour_snapshot.py -- out.json` |
| Compare snapshots | `python tests/compare_snapshots.py old.json new.json` (0 differences = same behaviour) |

- **Blender path:** `--blender PATH` or `$BLENDER`. On Windows it defaults to `C:\Program Files\Blender Foundation\Blender 5.3*\blender.exe`.
- **Isolation:** `run.py` always uses `--factory-startup` plus a temporary `BLENDER_USER_RESOURCES`. **Never** test against the user's running Blender or profile.
- **Expected result:** `SHRINKPATCH_TESTS ran=27 failures=0 errors=0`.

## Architecture (`__init__.py`, top to bottom)

settings → model (`patch_target`, `real_surface`/`wrap_object`/`_proxy_of`, `ensure_proxy`, `cut_collection`, `_update_cut_modifier`, `sync_cuts`, `set_cuts_active`) → auto cutter (`is_inset`, `build_autocut_mesh`, `ensure_autocut`, `refresh_cuts`) → geometry helpers → conform node group (`_conform_node_group`, `set_conform`) → `order_stack` → **`setup_modifiers`** (entry point) → operators (`OBJECT_OT_shrink_patch_place` is the modal) → panels → live sync (`_on_depsgraph_update`, `_watch_tick`, `process_watch`) → `upgrade_existing_patches` and `_on_load_post` → `register`.

## Data flow

1. **Stack on the patch:** `[user mods] > ShrinkPatch_Conform? > ShrinkPatch_Shrinkwrap > Subdivision > ShrinkPatch_Normals`.
2. **On the surface:** `... > SP_Cut_All` (Boolean, `MANIFOLD`, operand = the `<surface>_SP_cutters` collection, which is not linked to the scene).
3. **Patches target the proxy** `<surface>_SP_proxy`, which shares the mesh and copies the modifiers except `SP_Cut_*`.
4. **Links are custom properties, and must point both ways:** `shrinkpatch_proxy`/`shrinkpatch_surface`, `shrinkpatch_cut_collection`, `shrinkpatch_cutter`, `shrinkpatch_autocut`.
5. **A patch is identified only by its Shrinkwrap modifier.** No state survives in memory except `_watch` (live sync).

## Rules

- **Supported version is Blender 5.3 only:** don't add fallbacks for other versions. Keep `BLENDER_VERSION` and `BLENDER_SHA256` in both workflows in step with the manifest.
- **Behaviour-preserving changes** must give 0 differences in `compare_snapshots.py`. Explain any difference.
- **Every bug fix gets a regression test that fails before the fix.**
- **Panels:** no docstrings (they become tooltips); use `#` comments. Operator docstrings are user tooltips: keep them accurate.
- **Never delete** the user's `.blend` / `.blend1` files. They're git-ignored and stay local.
- **The version lives in three places:** the manifest, `bl_info`, and README (version line, static release badge, history).
- **Commits:** small, with a message that says how the change was verified.

## Pitfalls (each one happened here)

- **GN modifier inputs in 5.3:** `mod.properties.inputs.<identifier>.value`. `mod["Socket_N"]` raises `TypeError`.
- **Manifest:** the tagline must be ≤ 64 characters, and `extension build` needs an existing output directory.
- **Stale `matrix_world`** after edits inside one script: rebuild it from `matrix_basis` (`parent_keep_transform`).
- **`Object.copy()` loses the hidden state**: copy it with `hide_set(src.hide_get())`.
- **A duplicated surface inherits the link properties**: always validate both directions.
- **Modifier `is_active` / `show_expanded` are UI state**: exclude them from comparisons (`_UI_ONLY`).
- **A coarse cutter's chords rise above curved bowls**: `build_autocut_mesh` adds the measured sag to the margin.
- **Manifold Boolean output isn't bit-identical between runs**: compare cut surfaces by shape.
- **Background Blender:** depsgraph handlers fire, timers don't. Call `process_watch()` in tests.
- **`None in bpy_prop_collection` raises `TypeError`**: guard optional names (`edge_group and edge_group in …`).

## How changes were verified (cleanup to 1.13.0)

- A baseline commit (`588078b`), then the test suite was written against it: 23/25 passed, and the failures were real bugs.
- The behaviour snapshot covers 192 cases. Three runs of the same version gave 0 differences between them, which set the tolerances.
- **Final:** 27/27 tests from source and from the installed package. Against the baseline, 48/192 cases differ, all explained by the fixes (24 crashes fixed, 24 insets with a lower cavity floor and no needless proxy rebuild).

## Release checklist

1. Bump the version in the manifest, `bl_info` and README (including the static badge and the history).
2. Run the tests from source, build, run the tests on the package.
3. `git tag -a vX.Y.Z -m "<notes>"`, then `git push origin main vX.Y.Z`.
4. `release.yml`: tests → checks the tag equals the manifest version → fetches the annotated tag → builds → `gh release create` with the zip and the tag message as notes.
5. Verify with `gh run list --workflow release.yml` and `gh release view vX.Y.Z`.

## Git setup

- **Remote:** `origin https://github.com/mttfntn/shrinkpatch-blender.git` (HTTPS, authenticated with `gh`). The default branch is `main`.
- **Ignored:** `*.blend*`, `dist/`, `*.zip`, `__pycache__/`.
- **CI** (`tests.yml`) runs on pushes to `main`, pull requests, and when called by `release.yml`. Artifacts are kept 3 days. The Blender download is cached by version and checksum.
