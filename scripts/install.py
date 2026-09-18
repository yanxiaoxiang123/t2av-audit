#!/usr/bin/env python3
"""Install this shared skill for Claude Code and prepare its image runtime."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import venv

NAME = "t2av-audit"


def canonical_root():
    here = Path(__file__).resolve().parents[1]
    shared = Path.home() / ".agents" / "skills" / NAME
    return shared if shared.is_dir() else here


def runtime_python():
    runtime = Path.home() / ".agents" / "skill-runtimes" / NAME
    return runtime / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def sync_claude(source):
    target = Path.home() / ".claude" / "skills" / NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        if target.resolve() != source.resolve():
            raise ValueError(f"Existing Claude skill link points elsewhere: {target}")
        return "linked"
    if target.exists():
        marker = target / ".t2av-managed-copy"
        if not marker.is_file():
            raise ValueError(f"Existing Claude skill is not managed by this installer: {target}")
        shutil.rmtree(target)
    if os.name != "nt":
        target.symlink_to(source, target_is_directory=True)
        return "linked"
    shutil.copytree(source, target, ignore=shutil.ignore_patterns(".runtime", "__pycache__", "*.pyc"))
    (target / ".t2av-managed-copy").write_text(str(source.resolve()) + "\n", encoding="utf-8")
    return "copied"


def setup_runtime():
    py = runtime_python()
    if not py.is_file():
        venv.EnvBuilder(with_pip=True).create(py.parent.parent)
    check = subprocess.run([str(py), "-c", "import PIL; print(PIL.__version__)"],
                           capture_output=True, text=True)
    if check.returncode:
        subprocess.run([str(py), "-m", "pip", "install", "Pillow>=10,<13"], check=True)
    return py


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-deps", action="store_true", help="Only connect skill directories")
    args = parser.parse_args()
    source = canonical_root()
    if not (source / "SKILL.md").is_file():
        parser.error("Canonical skill source is missing")
    try:
        mode = sync_claude(source)
        py = None if args.no_deps else setup_runtime()
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Installation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"canonical_skill": str(source), "claude_installation": mode,
                      "codex_discovery": str(source), "dsh_discovery": str(source),
                      "python": str(py) if py else None}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
