"""Build the DSSAT-CSM engine (dscsm048) from source and install it for xdssat.

Usage (from the project folder):

    python scripts/build_engine.py                      # source ../dssat-csm-os
    python scripts/build_engine.py --source <path>      # another checkout
    python scripts/build_engine.py --clone              # fetch from GitHub
    python scripts/build_engine.py --engine-dir C:/TEMP/dsn/dssat

Needs gfortran, cmake and make on PATH. On Windows: MinGW-w64 (for example
Strawberry Perl's toolchain) with ``mingw32-make``.

What it does (the manual recipe is in spikes/README.md):

1. exports the source (``git archive HEAD``) into a short build folder;
2. ``cmake -DCMAKE_BUILD_TYPE=RELEASE`` then ``make``;
3. copies ``dscsm048`` and the engine ``Data/`` files into the engine folder.
   ``cmake --install`` is not used: it fails on Windows because CMakeLists.txt
   installs ``Utilities/run_dssat``, which is only generated on other systems;
4. writes the path profile (``DSSATPRO.V48`` on Windows, ``DSSATPRO.L48``
   elsewhere) pointing every entry at the engine folder. Windows entries have
   the form ``KEY C: \\path`` (note the space after the drive).

Keep both folders short (under ~40 characters): the engine stores paths in
fixed-length character buffers.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
import config  # noqa: E402

REPO_URL = "https://github.com/DSSAT/dssat-csm-os.git"
DEFAULT_BUILD = Path("C:/TEMP/dsn-build") if config.IS_WINDOWS else Path("/tmp/xdssat-build")


def run(cmd, cwd=None):
    print("+", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], cwd=cwd, check=True)


def export_source(source: Path, dest: Path) -> str:
    """Copy the tracked files of ``source`` into ``dest``; return the commit."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    if (source / ".git").exists():
        commit = subprocess.run(["git", "-C", str(source), "rev-parse", "--short", "HEAD"],
                                capture_output=True, text=True, check=True).stdout.strip()
        with tempfile.TemporaryDirectory() as td:
            tar = Path(td) / "src.tar"
            run(["git", "-C", source, "archive", "--format=tar", "-o", tar, "HEAD"])
            with tarfile.open(tar) as tf:
                tf.extractall(dest, filter="data")
        return commit
    shutil.copytree(source, dest, dirs_exist_ok=True)
    return "unknown"


def write_profile(engine_dir: Path, data_dir: Path) -> Path:
    """Write the DSSAT path profile with every entry pointing at engine_dir."""
    if config.IS_WINDOWS:
        tmpl = (data_dir / "DSSATPRO.v48").read_text(encoding="latin-1")
        drive, rest = os.path.splitdrive(str(engine_dir.resolve()))
        target = f"{drive.upper()} {rest.replace('/', chr(92))}"
        text = re.sub(r"C: \\DSSAT48", lambda m: target, tmpl)
        out = engine_dir / "DSSATPRO.V48"
    else:
        tmpl = (data_dir / "DSSATPRO.L48.in").read_text(encoding="latin-1")
        text = tmpl.replace("@CMAKE_INSTALL_PREFIX@", str(engine_dir.resolve()))
        out = engine_dir / "DSSATPRO.L48"
    out.write_text(text, encoding="latin-1")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--source", type=Path, default=HERE.parent.parent / "dssat-csm-os",
                    help="dssat-csm-os checkout (default: sibling folder)")
    ap.add_argument("--clone", action="store_true", help="clone the source from GitHub")
    ap.add_argument("--ref", default=None, help="branch or tag to clone (with --clone)")
    ap.add_argument("--build-dir", type=Path, default=DEFAULT_BUILD)
    ap.add_argument("--engine-dir", type=Path, default=config.ENGINE_DIR)
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 4)
    args = ap.parse_args(argv)

    for tool in ("gfortran", "cmake", "mingw32-make" if config.IS_WINDOWS else "make"):
        if shutil.which(tool) is None:
            sys.exit(f"{tool} not found on PATH")

    build = args.build_dir
    source = args.source
    if args.clone:
        source = build / "clone"
        if source.exists():
            shutil.rmtree(source)
        cmd = ["git", "clone", "--depth", "1"]
        if args.ref:
            cmd += ["--branch", args.ref]
        run(cmd + [REPO_URL, source])
    if not (source / "CMakeLists.txt").exists():
        sys.exit(f"no DSSAT source at {source}; pass --source or --clone")

    src = build / "src"
    obj = build / "build"
    commit = export_source(source, src)
    if obj.exists():
        shutil.rmtree(obj)
    obj.mkdir(parents=True)

    gen = ["-G", "MinGW Makefiles"] if config.IS_WINDOWS else []
    run(["cmake", *gen, "-DCMAKE_BUILD_TYPE=RELEASE", src], cwd=obj)
    run(["mingw32-make" if config.IS_WINDOWS else "make", f"-j{args.jobs}"], cwd=obj)

    exe = obj / "bin" / config.EXE_NAME
    if not exe.exists():
        sys.exit(f"build finished but {exe} is missing")

    eng = args.engine_dir
    eng.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src / "Data", eng, dirs_exist_ok=True)
    shutil.copy2(exe, eng / config.EXE_NAME)
    prof = write_profile(eng, src / "Data")
    (eng / "BUILD_INFO.txt").write_text(
        f"source: {source}\ncommit: {commit}\nbuilt by: scripts/build_engine.py\n", encoding="ascii")
    print(f"\nInstalled {eng / config.EXE_NAME} (commit {commit})")
    print(f"Profile: {prof}")
    if str(eng.resolve()) != str(config.ENGINE_DIR.resolve()):
        print(f"Set XDSSAT_ENGINE={eng} so xdssat finds it.")


if __name__ == "__main__":
    main()
