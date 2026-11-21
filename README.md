# Shrink Patch

A Blender add-on for placing **patches**, meaning modelled details such as bosses, rings, sockets or insets, onto any surface. Each patch blends into the surface without a visible seam.

You place a patch by moving the mouse over the surface. The add-on then:

- wraps the patch's base onto the surface,
- transfers the surface's normals so the joint shades cleanly,
- for **insets**, cuts the surface open automatically, the way KIT OPS inserts work.

Patches stay live. You can move, scale, duplicate or delete them at any time, and the cuts and wrapping keep up.

- **Version:** 1.12.0
- **Blender:** 4.2 or newer (developed and tested on 5.3)
- **Location:** 3D Viewport › Sidebar (N) › **Shrink Patch** tab. Also in the Object menu and the right-click menu.

---

## Contents

1. [Installation](#installation)
2. [Preparing a patch](#preparing-a-patch)
3. [Quick start](#quick-start)
4. [Placing a patch (interactive mode)](#placing-a-patch-interactive-mode)
5. [The panel, section by section](#the-panel-section-by-section)
6. [Insets and cutters](#insets-and-cutters)
7. [Moving and editing patches later](#moving-and-editing-patches-later)
8. [What the add-on creates in your scene](#what-the-add-on-creates-in-your-scene)
9. [Tips for good results](#tips-for-good-results)
10. [Troubleshooting](#troubleshooting)
11. [Known limitations](#known-limitations)
12. [Version history](#version-history)

---

## Installation

1. In Blender, open **Edit › Preferences › Get Extensions**.
2. Click the **⌄** menu at the top right and choose **Install from Disk…**
3. Select **`shrinkpatch.zip`**. The add-on is enabled straight away.

You can also drag `shrinkpatch.zip` into the Blender window.

**Updating:** install the new zip the same way. It replaces the old version.

**Files that already have patches:** they're recognised as soon as the add-on is enabled. Patches made with older versions of the add-on are upgraded automatically when the file opens. You don't need to re-apply anything.

---

## Preparing a patch

A patch is an ordinary mesh with **one vertex group** that marks its **base**, the part that touches the surface.

| What | How |
|---|---|
| **Orientation** | Model the patch lying flat, with its **local +Z pointing away from the surface**. The base sits around Z = 0. |
| **`base` vertex group** | The bottom ring of vertices at **weight 1.0**. Optionally the next ring in at **weight 0.5**, for a soft transition. |
| **Insets** | Any geometry that goes **below** the base (Z < 0) makes the patch an *inset*. The surface is then cut open automatically. |
| **Normals group** | Not needed: `base_normals` is created for you from the base vertices at weight 1.0. |
| **Subdivision** | Optional: the add-on adds one (Catmull-Clark 2/2) if the patch has none. |
| **Mirror modifier** | Supported. It keeps its place at the top of the stack. |

The group names can be changed under **New Patch Settings** (see below).

---

## Quick start

1. Select the **surface** (for example the sphere).
2. **Shift-click the patch**, so it is the **active** object.
3. In **Shrink Patch**, click **Place on Surface**.
4. Move the mouse over the surface. **Scroll** to scale and **Shift + scroll** to rotate.
5. **Left click** to confirm.

That's it. The patch is now wrapped, shaded and listed under **Patches on Sphere**.

---

## Placing a patch (interactive mode)

**Place on Surface**, **Move** and **Duplicate** all start this mode. The patch follows the mouse over the surface and stays aligned to the surface normal.

| Input | Action |
|---|---|
| Move the mouse | Slide the patch over the surface |
| **Wheel** / **Numpad + −** | Scale (**Alt** for fine steps) |
| **Shift + Wheel** | Rotate around the normal: 15° steps (**Alt**: 5°) |
| **Ctrl + Wheel** | Lift / sink the patch (offset) (**Alt** for fine steps) |
| **D** | Leave a copy at the current spot and keep placing ("stamp") |
| **Left click** / **Enter** / **Space** | Confirm |
| **Right click** / **Esc** | Cancel: everything goes back to how it was |
| **Middle mouse** | Orbit / pan the view while placing |

**What you see while placing:**

- an **orange ring**: the footprint of the base, projected onto the surface;
- **blue ticks**: the surface normal and the patch's X direction;
- the header bar: current scale, offset and number of stamped copies.

> While you drag an **inset**, its cut is paused so the surface isn't recalculated on every mouse move. The inset looks covered for a moment, and the cut comes back when you release.

---

## The panel, section by section

Every section folds open and closed (▸ / ▾), and Blender remembers which ones you left open.

### Shrink Patch (main)

- **Patch / Surface:** what the tool will use. The active object is the patch; the other selected object is the surface.
- **Place on Surface:** starts the interactive placement.
- **Stick While Moving (G / Shift+D):** turns on Blender's own face snapping (snap to Face, using the active object's origin, with Align Rotation to Target). Plain **G** and **Shift+D** then keep patches on the surface. Turning it off restores your previous snap settings. Works best after **Origin to Base**.

### ▸ New Patch Settings

The defaults used when a patch is applied for the first time.

| Option | Default | What it does |
|---|---|---|
| **Base** | `base` | Vertex group that touches the surface (it's shrinkwrapped). |
| **Normals** | `base_normals` | Vertex group that receives the surface normals. Created automatically if missing. |
| **Wrap** | Project | How the base is wrapped: *Project* (along the patch's Z, recommended), *Nearest Surface Point*, *Target Normal Project*. |
| **Transfer Normals** | On | Copies the surface's normals onto the contact edge, for seamless shading. |
| **Smooth Base** | On | Marks the base faces smooth so the transferred normals blend cleanly. |
| **Parent to Surface** | On | Patches become children of the surface and follow it when it moves. |
| **Whole Patch** | Off | Bends the **entire** patch along the curvature, keeping its thickness, instead of wrapping only the base. Recommended on curved surfaces. |
| **Subdivision After Wrap** | On | Stack order *Shrinkwrap → Subdivision → Normals*: the cage is wrapped first, then smoothed. This avoids a small dent at the base. |
| **Auto Cut Insets** | On | Patches that go below their base cut the surface automatically. |

#### ▸ Subdivision

- **Checkbox in the header:** let the add-on manage the patch Subdivision.
- **Viewport / Render:** levels (default **2 / 2**, Catmull-Clark, Optimal Display).
- **Apply to All Patches:** applies these settings to every patch in the scene and rebuilds their cuts.

### ▾ Patches on *Surface* (N)

Every patch applied to the selected surface, or to the surface of the selected patch. Each row has:

| Button | Action |
|---|---|
| **Name** | Select that patch |
| ✥ | Move it interactively |
| ⊡ (snap) | Snap it back onto the surface |
| ◩ (boolean) | Show / hide its cutters (only shown if it has any) |
| 🖵 | Hide / show it in the viewport |
| **✕** | Detach: removes the add-on's modifiers, parent and cuts, and keeps the object |
| 🗑 | Delete the patch, its cutters and its cuts (asks first) |

### ▾ Active Patch

Shown when the active object is a patch.

- **Scale X / Y / Z:** vertical, like the Transform panel. Drag down across the three fields to change them together.
- **Move:** interactive placement again.
- **Snap:** puts the patch back on the surface and realigns it.
- **Duplicate:** copies the patch (with its cutters) and lets you place the copy. Cancelling deletes the copy.
- **Origin to Base:** moves the origin to the centre of the base without moving the mesh.
- **Parent:** shows what the patch is parented to.
- **Select *Surface*:** jumps to the surface.

#### ▾ Deform

- **Shrinkwrap:** turns the base wrapping on or off.
- **Whole Patch: On/Off** + **Bend** (0–1): how much of the curvature the whole patch follows.
- **Subdivision After Wrap: On/Off:** switches the stack order for this patch.

#### ▸ Normals

- **Checkbox in the header:** turns the normal transfer on or off.
- **Group:** which vertex group receives the surface normals. The mapping is always *Projected Face Interpolated*.

#### ▸ Cuts

- **Auto Cut: On/Off** and **↻** (rebuild the cut now).
- **Cutters: N**, with 👁 to show or hide them, and **Add Selected** to turn the selected meshes into cutters (see below).

---

## Insets and cutters

### Auto Cut (insets)

If the patch has geometry **below its base**, the add-on builds a hidden **cutter** from the patch's own shape: the patch surface, plus walls up from its rim, plus a lid. The surface is then opened exactly where the patch dips in.

- The cut edge is kept **under the flange**, not at the visible rim. This gives a clean edge with no shading crease.
- The cutter is rebuilt automatically when the patch moves, when you confirm Place / Move, on Snap, and when you toggle Whole Patch or Subdivision After Wrap.
- **↻** in **Cuts** rebuilds it by hand.

### Cutters you model yourself

For special holes, you can model your own cutter. It must be a **closed** mesh.

1. Select the cutter mesh(es).
2. Shift-click the patch so it is **active**.
3. **Cuts › Add Selected**, or use the right-click menu.

The cutter becomes a child of the patch, displays as wire, is hidden in renders, and cuts the surface. A child object with **"cutter"** in its name is picked up automatically.

---

## Moving and editing patches later

You don't need to use the add-on's buttons for everything. It watches the scene and keeps things consistent:

| You do | The add-on does (a moment later) |
|---|---|
| Move an inset with **G** (or undo a move) | Rebuilds its cut in the new position, once you release |
| Delete a patch with **X** | Removes its generated cutter. A cutter you modelled stops cutting but is kept. |
| Change the surface's modifiers or mesh | Updates the hidden proxy (see below) |
| Move the **surface** | Nothing to do: the patches and cuts move with it |
| Open an old file | Upgrades patches made with older versions |

---

## What the add-on creates in your scene

**On each patch** (modifier stack, top to bottom):

```
[your modifiers, e.g. Mirror]
ShrinkPatch_Conform       ← only with Whole Patch (Geometry Nodes)
ShrinkPatch_Shrinkwrap    ← wraps the base group
Subdivision               ← Catmull-Clark 2/2
ShrinkPatch_Normals       ← Data Transfer: custom normals, Projected Face Interpolated
```

**On the surface:**

```
Subdivision               ← yours
SP_Cut_All                ← one Boolean (Difference, Manifold) for all the cuts
```

**Hidden helper data:**

| Name | What it is |
|---|---|
| `<Surface>_SP_proxy` | A hidden copy of the surface **without the cuts**. Patches wrap onto it, so they never sink into their own holes. |
| `<Patch>_autocut` | The generated cutter of an inset (a hidden child of the patch). |
| `<Surface>_SP_cutters` | The collection with all the surface's cutters, used by `SP_Cut_All`. |
| `ShrinkPatch_Conform` | The Geometry Nodes group used by *Whole Patch*. |

All helpers are hidden in the viewport and in renders. Don't delete them by hand; use **Detach** or **Delete** instead.

---

## Tips for good results

- **Curved surfaces:** turn on **Whole Patch**, so the whole patch follows the curvature and doesn't fold where the base meets the rigid upper part.
- **Keep Subdivision After Wrap on.** It avoids the small dent at the base.
- **Two vertex groups** (`base` for the wrap, `base_normals` for the shading) give a smoother joint than one. The add-on sets this up for you.
- **A wide, gentle flange** shades more softly than a steep one.
- **Rendering (Octane, Cycles, EEVEE):** cutters and proxies are hidden with Blender's standard *Disable in Renders*. Do a quick test render the first time you use a new render engine.

---

## Troubleshooting

| You see | Why / what to do |
|---|---|
| **A darker spot on the side of a flared patch** | Usually real shading: that part of the flare faces away from the light. Rotate the light to check. A wider, gentler flange softens it. |
| **A dark band or fold low on the patch** | The base is wrapped but the rest of the patch stays rigid. Turn on **Whole Patch**. |
| **A small dent at the base** | Turn on **Subdivision After Wrap** (Deform section). |
| **The surface covers an inset** | **Cuts › Auto Cut: On**, or press **↻**. |
| **A wireframe mesh sticking out** | That's a cutter that is visible. Hide it with 👁 in **Cuts** (it keeps cutting). |
| **An inset's cut is in the wrong place** | It's rebuilt automatically when you stop moving. If not, press **↻** or **Snap**. |
| **G-moving a patch makes it float** | With *Stick While Moving*, the origin must be on the base: click **Origin to Base**. |
| **A duplicated surface behaves oddly** | Each surface gets its own proxy and cutters. Snap a patch on the copy once to set it up. |

---

## Known limitations

- **Automatic updates** (cut rebuild, cutter cleanup, proxy sync) don't create their own undo step. After Ctrl+Z, the add-on simply realigns things to wherever the patch ends up.
- **While dragging,** an inset's cut is paused and is rebuilt when you release. With *Whole Patch* on, the cutter follows the patch's bend only when it's rebuilt.
- **Booleans** need closed (manifold) cutters. Generated cutters always are; check your own if a cut looks wrong.
- **The proxy** is a second evaluation of the surface. On very dense surfaces this costs some speed.

---

## Version history

| Version | Changes |
|---|---|
| 1.12 | Live sync: cuts follow G-moves, X cleans up cutters, the proxy follows the surface |
| 1.11 | Automatic upgrade of patches from older versions when a file opens or the add-on is enabled |
| 1.10 | Collapsible panel sections; vertical Scale; Offset field and mapping selector removed |
| 1.9 | One Boolean for all cuts (about 4× faster); cut paused while dragging; lighter cutters |
| 1.8 | Managed patch Subdivision (Catmull-Clark 2/2, Optimal Display) |
| 1.7 | Auto Cut for insets; clean cut edge under the flange; fix for duplicated surfaces |
| 1.6 | Cutters (KIT OPS-style insets) with an uncut proxy surface |
| 1.5 | Subdivision After Wrap |
| 1.4 | Separate normals group (`base_normals`), Projected Face Interpolated mapping |
| 1.3 | Parent to surface, Whole Patch bend, placement preview |
| 1.2 | Duplicate on surface, stamp (D), Stick While Moving, Origin to Base |
| 1.1 | Interactive placement, patch overview |
| 1.0 | First version: project, shrinkwrap, normal transfer |
