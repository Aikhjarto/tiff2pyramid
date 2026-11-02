#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Thomas Wagner
#
# This program is free software; you can redistribute it and/or modify it under the terms of the
# GNU General Public License as published by the Free Software Foundation; either version 2 of the
# License, or (at your option) any later version. This program is distributed WITHOUT ANY WARRANTY;
# see the file LICENSE for details.
"""Convert TIFFs into JPEG-compressed pyramidal BigTIFFs for iipsrv.

Accepts pyramidal and non-pyramidal input (only the full-resolution level is
read), 8/16/32-bit integer or float samples, grey/RGB/CMYK/Lab, with or without
alpha and embedded ICC profile. The output is always 8-bit sRGB (or 8-bit grey
for grey input), alpha flattened onto a background colour, 256x256 JPEG tiles.

Needs pyvips (openSUSE: python313-pyvips, Leap 16: pip install --user pyvips).

Notes:
  * libvips writes JPEG-in-TIFF as YCbCr 4:2:0 for Q < 90 and as plain RGB
    (no chroma subsampling, ~3x larger) for Q >= 90.
  * iipsrv passes the stored JPEG tiles through unchanged, so Q is exactly the
    quality the browser gets.
"""

import argparse
import os
import signal
import sys
import time

__version__ = "0.2"

FLOAT_FORMATS = ("float", "double", "complex", "dpcomplex")
MONO = ("b-w", "grey16")


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def n_pages(im):
    return im.get("n-pages") if im.get_typeof("n-pages") else 1


def load(pyvips, path, **kwargs):
    """Open an image; for TIFF without libtiff's 50 MB memory limit.

    Very wide panoramas are often written with strips of 50 MB or more, which
    libvips refuses to read unless the limit is lifted. The input files are our
    own, so the protection against hostile files is not needed.
    """
    with open(path, "rb") as f:
        magic = f.read(4)
    if magic in (b"II*\0", b"MM\0*", b"II+\0", b"MM\0+"):  # TIFF and BigTIFF, both byte orders
        try:
            return pyvips.Image.new_from_file(path, unlimited=True, **kwargs)
        except pyvips.Error as e:
            if "unlimited" not in str(e):  # older libvips without this option: load normally
                raise
    return pyvips.Image.new_from_file(path, **kwargs)


def probe_max(pyvips, path, im):
    """Largest sample value, read from the smallest pyramid level if there is one.

    Without a pyramid the whole image is scanned once, sequentially: random
    access to a large stripped TIFF can make libvips decode it into memory.
    """
    pages = n_pages(im)
    if pages > 1:
        small = load(pyvips, path, page=pages - 1)
    else:
        small = load(pyvips, path, access="sequential")
    colour = small.extract_band(0, n=small.bands - 1) if small.hasalpha() else small
    return colour.max()


def plan_and_build(pyvips, path, args):
    """Return (image ready for saving, list of human readable steps)."""
    im = load(pyvips, path, access="sequential")
    steps = [f"input {im.width}x{im.height}, {im.format}, {im.bands} bands, {im.interpretation}"
             f"{', alpha' if im.hasalpha() else ''}{f', pyramid with {n_pages(im)} levels' if n_pages(im) > 1 else ''}"
             f"{', ICC profile' if im.get_typeof('icc-profile-data') else ''}"]
    mono = im.interpretation in MONO or im.bands in (1, 2)
    target = "b-w" if mono else "srgb"

    # 1. embedded ICC profile: convert to sRGB (8 bit, alpha is kept and rescaled)
    if im.get_typeof("icc-profile-data") and not args.ignore_icc and im.format not in FLOAT_FORMATS:
        im = im.icc_transform("srgb", embedded=True, depth=8, intent="relative")
        steps.append("ICC profile -> sRGB (8 bit)")
        if mono:
            im = im.colourspace("b-w")

    # 2. work out the white level of the data
    linear = False
    if im.format == "uchar":
        white = 255
    elif im.format == "ushort":
        white = 65535
    else:
        if args.white:
            white = args.white
        else:
            m = probe_max(pyvips, path, load(pyvips, path))
            if im.format in FLOAT_FORMATS and m <= 1.5:
                white = 1.0
            elif m <= 255.5:
                white = 255
            elif m <= 65535.5:
                white = 65535
            else:
                white = m
            steps.append(f"sample maximum {m:g} -> white level {white:g}")
        linear = (im.format in FLOAT_FORMATS and white == 1.0 and im.interpretation == "scrgb"
                  and not args.no_linear)

    # 3. flatten alpha onto the background, scaled to the white level
    if im.hasalpha():
        bg = [b * white / 255 for b in args.background]
        if len(bg) == 1:
            bg = bg * (im.bands - 1)
        im = im.flatten(background=bg[:im.bands - 1], max_alpha=white)
        steps.append(f"flatten alpha onto background {args.background}")
    elif im.bands > (1 if mono else 3) and im.interpretation not in ("cmyk",):
        im = im.extract_band(0, n=1 if mono else 3)
        steps.append(f"dropped extra bands, kept {im.bands}")

    # 4. convert to 8 bit in the target colour space
    if im.interpretation in ("cmyk", "lab", "labq", "labs", "lch", "cmc", "xyz", "yxy", "hsv"):
        steps.append(f"{im.interpretation} -> {target}")
        im = im.colourspace(target)
    if im.format == "ushort":
        im = im.copy(interpretation="grey16" if mono else "rgb16").colourspace(target)
        steps.append("16 bit -> 8 bit")
    elif im.format != "uchar":
        if linear:
            im = im.colourspace(target)
            steps.append("linear float (scRGB) -> sRGB 8 bit")
        else:
            steps.append(f"{im.format} 0..{white:g} -> 8 bit")
            im = (im * (255 / white) + 0.5).cast("uchar")
    im = im.copy(interpretation=target)
    return im, steps


def convert(pyvips, path, args):
    base = os.path.splitext(os.path.basename(path))[0]
    outdir = args.outdir or os.path.dirname(os.path.abspath(path))
    out = os.path.join(outdir, (args.name if args.name else base + args.suffix.format(q=args.quality)) + ".tif")
    if os.path.abspath(out) == os.path.abspath(path):
        print(f"SKIP {path}: output would overwrite the input", file=sys.stderr)
        return False
    if os.path.exists(out) and not args.force:
        print(f"SKIP {path}: {out} exists (use -f to overwrite)", file=sys.stderr)
        return False

    print(f"== {path}")
    im, steps = plan_and_build(pyvips, path, args)
    for s in steps:
        print(f"   {s}")
    print(f"   -> {out} (JPEG Q{args.quality}, {args.tile}px tiles, pyramid, BigTIFF)")
    if args.dry_run:
        return True
    return write_pyramid(pyvips, im, out, args, os.path.getsize(path))


def remove_quietly(path):
    """Delete path if it exists; only warn if that fails (Windows: file still in use)."""
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError as e:
        print(f"   WARNING: could not remove {path}: {e}", file=sys.stderr)


def start_pyvips(tmpdir):
    """Import pyvips with temporary files in tmpdir; make kill/hangup clean up like an error."""
    # libvips writes the smaller pyramid levels to temporary files before assembling the
    # output, keep them off a small /tmp (GLib reads TMPDIR on Windows too)
    os.environ["TMPDIR"] = tmpdir
    for name in ("SIGTERM", "SIGHUP"):  # no SIGHUP on Windows
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), lambda signum, frame: sys.exit(128 + signum))
    import pyvips
    # cached operations keep their files open, which stops renaming and deleting on Windows;
    # every image here is processed only once, so the cache gains nothing
    pyvips.cache_set_max(0)
    return pyvips


def write_pyramid(pyvips, im, out, args, insize):
    """Save im as JPEG pyramid via <out>.part, check it, report; return success."""
    part = out + ".part"
    t0 = time.time()
    try:
        im.tiffsave(part, compression="jpeg", Q=args.quality, tile=True,
                    tile_width=args.tile, tile_height=args.tile, pyramid=True, bigtiff=True)
        check = pyvips.Image.new_from_file(part)
        size, levels = (check.width, check.height), n_pages(check)
        del check  # Windows cannot rename a file that is still open
        if size != (im.width, im.height) or (levels < 2 and max(im.width, im.height) > args.tile):
            raise RuntimeError(f"unexpected result {size[0]}x{size[1]}, {levels} levels")
        os.replace(part, out)
    except BaseException as e:
        remove_quietly(part)
        if not isinstance(e, Exception):  # interrupted or killed
            raise
        detail = getattr(e, "detail", "").strip()
        print(f"   FAILED: {getattr(e, 'message', e)}" + (f"\n   {detail}" if detail else ""), file=sys.stderr)
        return False
    outsize = os.path.getsize(out)
    print(f"   done in {time.time() - t0:.0f} s, {levels} levels, "
          f"{human(insize)} -> {human(outsize)} ({100 * outsize / insize:.0f} %)")
    return True


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("files", nargs="+", help="input TIFF files")
    p.add_argument("-q", "--quality", type=int, default=90, help="JPEG quality (default 90)")
    p.add_argument("-o", "--outdir", help="output directory (default: next to the input)")
    p.add_argument("-s", "--suffix", default="_jpeg_q{q}",
                   help="appended to the input name (default _jpeg_q{q}, {q} = quality)")
    p.add_argument("--name", help="output file name without .tif (only with a single input)")
    p.add_argument("-b", "--background", default="0",
                   help="background for transparent areas, 0-255, one value or R,G,B (default 0 = black)")
    p.add_argument("--white", type=float,
                   help="white level of non-8/16-bit input (default: detect, float 0..1 / 0..255 / 0..65535)")
    p.add_argument("--no-linear", action="store_true",
                   help="treat float 0..1 data labelled scRGB as already gamma encoded")
    p.add_argument("--ignore-icc", action="store_true", help="do not apply an embedded ICC profile")
    p.add_argument("--tile", type=int, default=256, help="tile size (default 256)")
    p.add_argument("--tmpdir", help="directory for libvips temporary files (default: output directory)")
    p.add_argument("-f", "--force", action="store_true", help="overwrite existing output")
    p.add_argument("-n", "--dry-run", action="store_true", help="only show what would be done")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = p.parse_args()
    if args.name and len(args.files) > 1:
        p.error("--name only works with a single input file")
    args.background = [float(v) for v in args.background.split(",")]

    pyvips = start_pyvips(args.tmpdir or args.outdir or os.path.dirname(os.path.abspath(args.files[0])))
    ok = sum(convert(pyvips, f, args) for f in args.files)
    if ok != len(args.files):
        print(f"{len(args.files) - ok} of {len(args.files)} files failed or skipped", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
