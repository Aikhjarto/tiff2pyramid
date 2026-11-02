#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Thomas Wagner
#
# This program is free software; you can redistribute it and/or modify it under the terms of the
# GNU General Public License as published by the Free Software Foundation; either version 2 of the
# License, or (at your option) any later version. This program is distributed WITHOUT ANY WARRANTY;
# see the file LICENSE for details.
"""Test the bash completion in completions/.

Runs the completion functions in bash and checks that the offered options are exactly those of
each script's --help, that only matching files (and folders) are offered, and that option values
are completed for "--opt value" and "--opt=value". Skipped on Windows and without bash.

Needs: bash.   Usage: python tests/test_completion.py
"""
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMPLETIONS = ROOT / "completions"


def complete(func, words, cwd):
    """COMPREPLY of func for the command line words (last word = the one being completed)."""
    script = (f'. {shlex.quote(str(COMPLETIONS / "ICE2pyramid"))}\n'  # loads the shared file too
              f'COMP_WORDS=({" ".join(shlex.quote(w) for w in words)}); COMP_CWORD={len(words) - 1}\n'
              f'{func}\n'
              'for w in "${COMPREPLY[@]}"; do printf "%s\\n" "$w"; done')
    out = subprocess.run(["bash", "--norc", "--noprofile", "-c", script], cwd=cwd,
                         capture_output=True, text=True, check=True).stdout
    return {line.rstrip("/") for line in out.splitlines() if line}


def help_options(script):
    text = subprocess.run([sys.executable, str(ROOT / script), "--help"], capture_output=True, text=True,
                          check=True).stdout
    options_part = text.split("options:")[-1] if "options:" in text else text.split("optional arguments:")[-1]
    return set(re.findall(r"(?<![\w-])(--?[a-zA-Z][\w-]*)", options_part))


def main():
    if os.name == "nt" or not shutil.which("bash"):
        print("skipped (needs bash, not run on Windows)")
        return
    work = Path(tempfile.mkdtemp(prefix="t2p-completion-"))
    for f in ("a.tif", "B.TIFF", "my pano.tif", "c.jpg", "pano.dzi", "pano.XML", "notes.txt"):
        (work / f).write_text("")
    (work / "pano_files").mkdir()
    (work / "sub").mkdir()
    (work / "with space").mkdir()

    checks = [
        ("tiff2pyramid options = --help", complete("_tiff2pyramid", ["tiff2pyramid", "-"], work),
         help_options("tiff2pyramid.py")),
        ("ICE2pyramid options = --help", complete("_ice2pyramid", ["ICE2pyramid", "-"], work),
         help_options("ice2pyramid.py")),
        ("tiff2pyramid offers TIFFs and folders", complete("_tiff2pyramid", ["tiff2pyramid", ""], work),
         {"a.tif", "B.TIFF", "my pano.tif", "pano_files", "sub", "with space"}),
        ("ICE2pyramid offers descriptors and folders", complete("_ice2pyramid", ["ICE2pyramid", ""], work),
         {"pano.dzi", "pano.XML", "pano_files", "sub", "with space"}),
        ("-q 8", complete("_tiff2pyramid", ["tiff2pyramid", "-q", "8"], work), {"80", "85"}),
        ("-q without a match offers no files", complete("_ice2pyramid", ["ICE2pyramid", "-q", "7x"], work), set()),
        ("--outdir=<TAB> offers folders only",
         complete("_tiff2pyramid", ["tiff2pyramid", "--outdir", "=", ""], work), {"pano_files", "sub", "with space"}),
        ("--outdir w (name with a space)", complete("_ice2pyramid", ["ICE2pyramid", "--outdir", "w"], work),
         {"with space"}),
        ("--band-rows (ICE2pyramid only)", complete("_ice2pyramid", ["ICE2pyramid", "--band-rows", ""], work),
         {"1", "2", "4", "8"}),
    ]
    failed = 0
    for name, got, expected in checks:
        ok = got == expected
        failed += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f": got {sorted(got)}, expected {sorted(expected)}"))
    registered = subprocess.run(["bash", "--norc", "--noprofile", "-c",
                                 f'. {shlex.quote(str(COMPLETIONS / "ICE2pyramid"))}; complete -p'],
                                capture_output=True, text=True, check=True).stdout
    for cmd in ("tiff2pyramid", "tiff2pyramid.py", "ICE2pyramid", "ice2pyramid.py"):
        ok = re.search(rf"complete -F \S+ {re.escape(cmd)}$", registered, re.M) is not None
        failed += not ok
        print(f"{'ok  ' if ok else 'FAIL'} completion registered for {cmd}")
    shutil.rmtree(work)
    total = len(checks) + 4
    print(f"{total - failed} of {total} checks passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
