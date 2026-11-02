#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Thomas Wagner
#
# This program is free software; you can redistribute it and/or modify it under the terms of the
# GNU General Public License as published by the Free Software Foundation; either version 2 of the
# License, or (at your option) any later version. This program is distributed WITHOUT ANY WARRANTY;
# see the file LICENSE for details.
"""End-to-end test on small synthetic TIFFs with known pixel values.

Writes one constant-colour TIFF per supported input type (8/16-bit, grey/RGB, with and without
alpha, linear and 0..255 float, CMYK, pyramidal input, embedded ICC profile), converts them all
with tiff2pyramid.py and checks the colour, band count, format and pyramid of every result.

Needs: pyvips.   Usage: python tests/test_synthetic.py [workdir]
"""
import subprocess
import sys
import tempfile
from pathlib import Path

import pyvips

ROOT = Path(__file__).resolve().parent.parent
W, H = 600, 400                     # bigger than one 256 px tile, so a pyramid is built
TOLERANCE = 2                       # JPEG Q90 on a flat colour


def const(vals, fmt, interp):
    return (pyvips.Image.black(W, H, bands=len(vals)) + vals).cast(fmt).copy(interpretation=interp)


def make_inputs(d):
    """Write the test inputs, return {name: expected 8-bit colour or None (only checked for RGB)}."""
    cases = {
        "u8_rgb":        (const([200, 100, 50], "uchar", "srgb"), [200, 100, 50]),
        "u8_rgba_full":  (const([200, 100, 50, 255], "uchar", "srgb"), [200, 100, 50]),
        "u8_rgba_half":  (const([200, 100, 50, 128], "uchar", "srgb"), [100, 50, 25]),
        "u16_rgb":       (const([51400, 25700, 12850], "ushort", "rgb16"), [200, 100, 50]),
        "u16_rgba_half": (const([51400, 25700, 12850, 32896], "ushort", "rgb16"), [100, 50, 25]),
        "grey8":         (const([128], "uchar", "b-w"), [128]),
        "grey16_alpha":  (const([32896, 65535], "ushort", "grey16"), [128]),
        "f_scrgb_lin":   (const([0.2158, 0.2158, 0.2158], "float", "scrgb"), [128, 128, 128]),
        "f_scrgb_255":   (const([200, 100, 50], "float", "scrgb"), [200, 100, 50]),
        "f_rgba_255":    (const([200, 100, 50, 255], "float", "srgb"), [200, 100, 50]),
        "cmyk":          (const([0, 128, 200, 0], "uchar", "cmyk"), None),
    }
    expect = {}
    for name, (im, exp) in cases.items():
        im.tiffsave(str(d / f"{name}.tif"), compression="lzw")
        expect[name] = exp
    # pyramidal input: only the full-resolution level may be used
    cases["u16_rgba_half"][0].tiffsave(str(d / "u16_pyramid.tif"), compression="lzw", tile=True, pyramid=True)
    expect["u16_pyramid"] = [100, 50, 25]
    # 16-bit with an embedded sRGB profile; embedding shifts the stored values slightly,
    # so the expectation is taken from what was actually stored
    icc = const([51400, 25700, 12850, 65535], "ushort", "rgb16").icc_export(output_profile="srgb", depth=16)
    icc.write_to_file(str(d / "u16_icc.tif"))
    stored = pyvips.Image.new_from_file(str(d / "u16_icc.tif"))
    expect["u16_icc"] = [round(stored[i].avg() / 257) for i in range(3)]
    return expect


def main():
    work = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="tiff2pyramid-test-"))
    (work / "in").mkdir(parents=True, exist_ok=True)
    (work / "out").mkdir(exist_ok=True)
    expect = make_inputs(work / "in")

    inputs = sorted(str(p) for p in (work / "in").glob("*.tif"))
    subprocess.run([sys.executable, str(ROOT / "tiff2pyramid.py"), "-f", "-o", str(work / "out"), *inputs],
                   check=True, stdout=subprocess.DEVNULL)

    failed = 0
    for name, exp in sorted(expect.items()):
        out = pyvips.Image.new_from_file(str(work / "out" / f"{name}_jpeg_q90.tif"))
        got = [round(out[i].avg()) for i in range(out.bands)]
        problems = []
        if out.format != "uchar":
            problems.append(f"format {out.format}")
        if out.get("n-pages") < 2:
            problems.append("no pyramid")
        if exp is None:
            if out.bands != 3:
                problems.append(f"{out.bands} bands")
        elif len(got) != len(exp) or any(abs(a - b) > TOLERANCE for a, b in zip(got, exp)):
            problems.append(f"expected {exp}")
        failed += bool(problems)
        print(f"{'FAIL' if problems else 'ok  '} {name:14s} {out.bands} bands, {out.get('n-pages')} levels, "
              f"colour {got}{'  ' + ', '.join(problems) if problems else ''}")

    print(f"{len(expect) - failed} of {len(expect)} cases passed (files in {work})")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
