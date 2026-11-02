#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Thomas Wagner
#
# This program is free software; you can redistribute it and/or modify it under the terms of the
# GNU General Public License as published by the Free Software Foundation; either version 2 of the
# License, or (at your option) any later version. This program is distributed WITHOUT ANY WARRANTY;
# see the file LICENSE for details.
"""Test ice2pyramid.py on synthetic Deep Zoom and Zoomify tile sets.

The source image encodes each pixel's position in its colour, so any shift or wrong overlap
handling changes pixel values. It is cut into tiles with ICE's layout (254 px, 1 px overlap) by
vips dzsave, once as lossless PNG (reassembly must be exact) and once like an ICE export
(.xml descriptor, Format="jpeg", .jpg tiles, unrelated files in the tile folder), which is
converted end to end. Zoomify tile sets of two sizes must also be reassembled exactly.

Needs: pyvips.   Usage: python tests/test_ice.py [workdir]
"""
import math
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pyvips

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import ice2pyramid  # noqa: E402

W, H = 3001, 1709                   # not a multiple of the tile size, so edge tiles are partial
pyvips.cache_set_max(0)             # as in the scripts: cached operations keep files open (Windows)


def coordinate_image():
    xy = pyvips.Image.xyz(W, H)
    x, y = xy[0], xy[1]
    rgb = (x % 256).bandjoin([y % 256, ((x >> 8) * 16 + (y >> 8)) % 256])
    return rgb.cast("uchar").copy(interpretation="srgb")


def main():
    work = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="ice2pyramid-test-"))
    work.mkdir(parents=True, exist_ok=True)
    src = coordinate_image()
    failed = 0

    # 1. lossless tiles: the reassembled image must equal the source exactly
    src.dzsave(str(work / "png"), tile_size=254, overlap=1, suffix=".png")
    full, strips, _ = ice2pyramid.assemble(pyvips, str(work / "png.dzi"), str(work), band_rows=3, log=lambda s: None)
    diff = (full.cast("int") - src.cast("int")).abs().max()
    ok = (full.width, full.height) == (W, H) and diff == 0
    failed += not ok
    print(f"{'ok  ' if ok else 'FAIL'} exact reassembly: {full.width}x{full.height}, max difference {diff:g}")
    full = None  # release the strip files before deleting them (Windows)
    ice2pyramid.remove(strips)
    left = list(work.glob("ice2pyramid-*.v"))
    failed += bool(left)
    print(f"{'FAIL' if left else 'ok  '} temporary strips removed")

    # 2. ICE-like export, converted end to end
    src.dzsave(str(work / "ice"), tile_size=254, overlap=1, suffix=".jpg[Q=98]")
    dzi = (work / "ice.dzi").read_text().replace('Format="jpg"', 'Format="jpeg"')
    (work / "ice.dzi").unlink()
    (work / "ice.xml").write_text(dzi)
    top = math.ceil(math.log2(max(W, H)))
    (work / "ice_files" / "13.txt").write_text("unrelated file\n")
    shutil.copy(work / "ice_files" / str(top) / "0_0.jpg", work / "ice_files" / f"{top}.jpg")
    subprocess.run([sys.executable, str(ROOT / "ice2pyramid.py"), "-f", "-q", "95", str(work / "ice_files")],
                   check=True, stdout=subprocess.DEVNULL)
    out = pyvips.Image.new_from_file(str(work / "ice_jpeg_q95.tif"))
    d = out.cast("float") - src.cast("float")
    psnr = 10 * math.log10(255 ** 2 / (d * d).avg())
    ok = (out.width, out.height) == (W, H) and out.get("n-pages") >= 2 and psnr > 35
    failed += not ok
    print(f"{'ok  ' if ok else 'FAIL'} ICE-like export: {out.width}x{out.height}, {out.get('n-pages')} levels, "
          f"PSNR {psnr:.1f} dB")

    # 3. descriptor renamed to <name>_files.dzi, tiles still in <name>_files
    shutil.copy(work / "png.dzi", work / "png_files.dzi")
    for arg in (work / "png_files.dzi", work / "png_files"):
        try:
            tiles = ice2pyramid.read_descriptor(ice2pyramid.find_descriptor(str(arg)))[5]
        except ValueError as e:
            tiles = str(e)
        ok = tiles in (str(work / "png_files"),)
        failed += not ok
        print(f"{'ok  ' if ok else 'FAIL'} renamed descriptor, given as {arg.name}: tiles in {Path(tiles).name}")

    # 4. Zoomify (no overlap, tiles numbered through all levels in TileGroup folders): exact, at two
    #    sizes where rounding the level sizes down instead of up would give a different numbering
    for zw, zh in ((W, H), (1025, 513)):
        zsrc = src.crop(0, 0, zw, zh)
        zsrc.dzsave(str(work / f"zoom{zw}"), layout="zoomify", suffix=".png")
        full, strips, _ = ice2pyramid.assemble(pyvips, str(work / f"zoom{zw}"), str(work), band_rows=2,
                                               log=lambda s: None)
        diff = (full.cast("int") - zsrc.cast("int")).abs().max()
        ok = (full.width, full.height) == (zw, zh) and diff == 0
        failed += not ok
        print(f"{'ok  ' if ok else 'FAIL'} Zoomify {zw}x{zh} exact reassembly: max difference {diff:g}")
        full = None
        ice2pyramid.remove(strips)

    # 5. Zoomify end to end, given as folder; output named after the folder
    src.dzsave(str(work / "myzoom"), layout="zoomify")
    subprocess.run([sys.executable, str(ROOT / "ice2pyramid.py"), "-f", str(work / "myzoom")],
                   check=True, stdout=subprocess.DEVNULL)
    out = pyvips.Image.new_from_file(str(work / "myzoom_jpeg_q85.tif"))
    ok = (out.width, out.height) == (W, H) and out.get("n-pages") >= 2
    failed += not ok
    print(f"{'ok  ' if ok else 'FAIL'} Zoomify end to end: {out.width}x{out.height}, {out.get('n-pages')} levels")

    print(f"{8 - failed} of 8 checks passed (files in {work})")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
