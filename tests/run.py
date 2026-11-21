"""Run the Shrink Patch tests in an isolated background Blender.

    python tests/run.py [--blender PATH] [--addon-dir DIR] [--script FILE] [-- extra args]

* --blender    Blender executable (default: $BLENDER, then the usual install paths)
* --addon-dir  folder that contains the `shrinkpatch` package (default: repo root)
* --script     what to run inside Blender (default: tests/test_shrinkpatch.py)

Blender is started with --factory-startup and a throw-away user profile
(BLENDER_USER_RESOURCES), so the installed add-on and your settings are never
touched. The exit code is Blender's (non-zero when a test fails).
"""

import argparse
import glob
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)


def find_blender():
    if os.environ.get("BLENDER"):
        return os.environ["BLENDER"]
    candidates = sorted(glob.glob(r"C:\Program Files\Blender Foundation\Blender 5.3*\blender.exe"), reverse=True)
    candidates += ["/Applications/Blender.app/Contents/MacOS/Blender", shutil.which("blender") or ""]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    sys.exit("Blender not found: pass --blender or set $BLENDER")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--blender", default=None)
    ap.add_argument("--addon-dir", default=REPO)
    ap.add_argument("--script", default=os.path.join(HERE, "test_shrinkpatch.py"))
    args, extra = ap.parse_known_args()
    blender = args.blender or find_blender()
    profile = tempfile.mkdtemp(prefix="shrinkpatch-profile-")
    env = dict(os.environ, BLENDER_USER_RESOURCES=profile, SHRINKPATCH_ADDON_DIR=os.path.abspath(args.addon_dir))
    cmd = [blender, "-b", "--factory-startup", "--python-exit-code", "1", "--python", args.script]
    if extra:
        cmd += ["--"] + [a for a in extra if a != "--"]
    print("running:", " ".join(cmd), flush=True)
    try:
        return subprocess.call(cmd, env=env)
    finally:
        shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
