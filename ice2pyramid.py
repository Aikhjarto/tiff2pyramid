#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Thomas Wagner
#
# This program is free software; you can redistribute it and/or modify it under the terms of the
# GNU General Public License as published by the Free Software Foundation; either version 2 of the
# License, or (at your option) any later version. This program is distributed WITHOUT ANY WARRANTY;
# see the file LICENSE for details.
"""Turn a Microsoft ICE Deep Zoom export into a JPEG-compressed pyramidal BigTIFF for iipsrv.

Microsoft Image Composite Editor (ICE) can export a panorama as a "Deep Zoom tileset": a
descriptor (<name>.dzi or <name>.xml) and a folder <name>_files with one subfolder per zoom
level. When the panorama itself was never saved, these tiles are all that is left of it.
This script reads the full-resolution level, puts the tiles back together (removing their
overlap) and writes the same output as tiff2pyramid: 8-bit, 256x256 JPEG tiles, pyramid,
BigTIFF. Any other Deep Zoom tile set (e.g. from vips dzsave) works too, and so do Zoomify tile
sets (a folder with ImageProperties.xml and TileGroup<n> subfolders).

The tiles are joined in strips of --band-rows tile rows, which are written uncompressed to
--tmpdir before the pyramid is built. That keeps memory use low, but needs
width x height x 3 bytes of temporary disk space (25 GB for a 200000x42000 panorama).
"""

import argparse
import math
import os
import shutil
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

from tiff2pyramid import __version__, human, remove_quietly, start_pyvips, write_pyramid

STRIP_BYTES = 256 * 1024 ** 2       # decoded tiles per strip with automatic --band-rows


def read_descriptor(path):
    """Return (tile size, overlap, format, width, height, tile folder) of a .dzi/.xml descriptor."""
    root = ET.parse(path).getroot()
    if root.tag.split("}")[-1] != "Image":
        raise ValueError(f"{path} is not a Deep Zoom descriptor")
    size = next(e for e in root.iter() if e.tag.split("}")[-1] == "Size")
    # <name>.dzi -> <name>_files; also accept a descriptor renamed to <name>_files.dzi
    stem = os.path.splitext(path)[0]
    tiles = next((d for d in (stem + "_files", stem) if d.endswith("_files") and os.path.isdir(d)), None)
    if not tiles:
        raise ValueError(f"tile folder {stem}_files not found")
    return (int(root.get("TileSize")), int(root.get("Overlap")), root.get("Format"),
            int(size.get("Width")), int(size.get("Height")), tiles)


def find_descriptor(path):
    """Accept the descriptor itself, a Deep Zoom <name>_files folder or a Zoomify folder."""
    folder = os.path.normpath(path)  # also drops a trailing / or \
    if os.path.isfile(os.path.join(folder, "ImageProperties.xml")):
        return os.path.join(folder, "ImageProperties.xml")
    if os.path.isdir(folder) and folder.endswith("_files"):
        for candidate in (folder[:-len("_files")] + ".dzi", folder[:-len("_files")] + ".xml",
                          folder + ".dzi", folder + ".xml"):
            if os.path.isfile(candidate):
                return candidate
        raise ValueError(f"no .dzi or .xml descriptor for {path}")
    return path


def tile_extension(level_dir, fmt):
    # ICE writes Format="jpeg" but names the files .jpg
    for ext in dict.fromkeys((fmt, "jpg", "jpeg", "png")):
        if os.path.isfile(os.path.join(level_dir, f"0_0.{ext}")):
            return ext
    raise ValueError(f"no tile 0_0.* in {level_dir}")


class TileSet:
    """The full-resolution tiles of a Deep Zoom or Zoomify tile set."""

    def __init__(self, kind, source, width, height, tile_size, overlap, path):
        self.kind, self.source = kind, source
        self.width, self.height, self.tile_size, self.overlap = width, height, tile_size, overlap
        self.cols, self.rows = math.ceil(width / tile_size), math.ceil(height / tile_size)
        self.path = path  # (column, row) -> file name

    def check(self):
        """Raise ValueError if tiles are missing, else return the total size of the tiles."""
        folders, missing, size = {}, [], 0
        for r in range(self.rows):
            for c in range(self.cols):
                folder, name = os.path.split(self.path(c, r))
                if folder not in folders:
                    folders[folder] = {e.name: e for e in os.scandir(folder)} if os.path.isdir(folder) else {}
                entry = folders[folder].get(name)
                if entry is None:
                    missing.append(os.path.join(folder, name))
                else:
                    size += entry.stat().st_size
        if missing:
            raise ValueError(f"{len(missing)} of {self.cols * self.rows} tiles missing, e.g. {missing[:3]}")
        return size


def open_deepzoom(descriptor):
    ts, ov, fmt, width, height, tiles = read_descriptor(descriptor)
    level_dir = os.path.join(tiles, str(math.ceil(math.log2(max(width, height)))))
    ext = tile_extension(level_dir, fmt)
    return TileSet("Deep Zoom", descriptor, width, height, ts, ov,
                   lambda c, r: os.path.join(level_dir, f"{c}_{r}.{ext}"))


def zoomify_levels(width, height, tile_size):
    """Level sizes, smallest first: halved (rounding up) until the image fits into one tile."""
    levels = [(width, height)]
    while levels[-1][0] > tile_size or levels[-1][1] > tile_size:
        w, h = levels[-1]
        levels.append(((w + 1) // 2, (h + 1) // 2))
    return levels[::-1]


def open_zoomify(descriptor, root):
    """Zoomify: tiles numbered through all levels, smallest first, 256 per TileGroup<n> folder."""
    width, height = int(root.get("WIDTH")), int(root.get("HEIGHT"))
    ts = int(root.get("TILESIZE", 256))
    levels = zoomify_levels(width, height, ts)
    counts = [math.ceil(w / ts) * math.ceil(h / ts) for w, h in levels]
    if root.get("NUMTILES") and int(root.get("NUMTILES")) != sum(counts):
        raise ValueError(f"{descriptor}: NUMTILES is {root.get('NUMTILES')}, the size gives {sum(counts)}")
    folder, top, offset, cols = os.path.dirname(descriptor), len(levels) - 1, sum(counts[:-1]), math.ceil(width / ts)
    ext = next((e for e in ("jpg", "png") if os.path.isfile(os.path.join(folder, "TileGroup0", f"0-0-0.{e}"))), "jpg")
    return TileSet("Zoomify", descriptor, width, height, ts, 0,
                   lambda c, r: os.path.join(folder, f"TileGroup{(offset + r * cols + c) // 256}",
                                             f"{top}-{c}-{r}.{ext}"))


def open_tileset(path):
    descriptor = find_descriptor(path)
    root = ET.parse(descriptor).getroot()
    tag = root.tag.split("}")[-1]
    if tag == "IMAGE_PROPERTIES":
        return open_zoomify(descriptor, root)
    if tag == "Image":
        return open_deepzoom(descriptor)
    raise ValueError(f"{descriptor} is neither a Deep Zoom nor a Zoomify descriptor")


def join_strip(pyvips, tiles, r0, r1):
    """Tiles of rows r0..r1-1 with their overlap removed, as one lazy image."""
    ts, ov, width, height = tiles.tile_size, tiles.overlap, tiles.width, tiles.height
    parts = []
    for r in range(r0, r1):
        for c in range(tiles.cols):
            t = pyvips.Image.new_from_file(tiles.path(c, r), access="sequential")
            w, h = min(ts, width - c * ts), min(ts, height - r * ts)
            parts.append(t.crop(ov if c else 0, ov if r else 0, w, h))
    # fixed grid: the narrower tiles in the last column/row are padded and cropped off again
    strip = pyvips.Image.arrayjoin(parts, across=tiles.cols, hspacing=ts, vspacing=ts)
    return strip.crop(0, 0, width, min(height, r1 * ts) - r0 * ts)


def assemble(pyvips, descriptor, tmpdir, band_rows, log=print):
    """Return (full-resolution image, temporary strip files, size of the tiles read)."""
    tiles = open_tileset(descriptor)
    insize = tiles.check()
    width, height, rows = tiles.width, tiles.height, tiles.rows
    if band_rows <= 0:
        # libvips keeps every decoded tile of a strip until the strip is written
        band_rows = max(1, STRIP_BYTES // (width * tiles.tile_size * 3))

    strips = []
    try:
        for i, r0 in enumerate(range(0, rows, band_rows)):
            t0 = time.time()
            strip = join_strip(pyvips, tiles, r0, min(rows, r0 + band_rows))
            fd, name = tempfile.mkstemp(suffix=".v", prefix="ice2pyramid-", dir=tmpdir)
            os.close(fd)
            strips.append(name)
            strip.write_to_file(name)
            log(f"   strip {i + 1}/{math.ceil(rows / band_rows)} joined in {time.time() - t0:.0f} s")
        parts = [pyvips.Image.new_from_file(s, access="sequential") for s in strips]
        full = pyvips.Image.arrayjoin(parts, across=1).crop(0, 0, width, height)
    except BaseException:
        strip = parts = full = None  # release the files before deleting them (Windows)
        remove(strips)
        raise
    return full, strips, insize


def remove(files):
    for f in files:
        remove_quietly(f)


def convert(pyvips, path, args):
    try:
        tiles = open_tileset(path)
    except (ValueError, OSError, ET.ParseError, StopIteration, TypeError) as e:
        print(f"SKIP {path}: {e}", file=sys.stderr)
        return False
    descriptor, width, height = tiles.source, tiles.width, tiles.height
    beside = os.path.dirname(os.path.abspath(descriptor))
    if tiles.kind == "Zoomify":  # the descriptor is always ImageProperties.xml inside the tile folder
        base, beside = os.path.basename(beside), os.path.dirname(beside)
    else:
        base = os.path.splitext(os.path.basename(descriptor))[0]
    outdir = args.outdir or beside
    out = os.path.join(outdir, (args.name if args.name else base + args.suffix.format(q=args.quality)) + ".tif")
    if os.path.exists(out) and not args.force:
        print(f"SKIP {path}: {out} exists (use -f to overwrite)", file=sys.stderr)
        return False

    tmpdir = args.tmpdir or outdir
    print(f"== {descriptor}")
    print(f"   {tiles.kind} {width}x{height}, {tiles.tile_size}px tiles"
          f"{f' + {tiles.overlap}px overlap' if tiles.overlap else ''}: {tiles.cols}x{tiles.rows} at full resolution")
    print(f"   temporary strips: {human(width * height * 3)} in {tmpdir} "
          f"({human(shutil.disk_usage(tmpdir).free)} free)")
    print(f"   -> {out} (JPEG Q{args.quality}, {args.tile}px tiles, pyramid, BigTIFF)")
    if args.dry_run:
        return True
    if shutil.disk_usage(tmpdir).free < width * height * 3 * 1.05:
        print(f"   FAILED: not enough space in {tmpdir} for the temporary strips (--tmpdir)", file=sys.stderr)
        return False

    strips, full = [], None
    try:
        full, strips, insize = assemble(pyvips, descriptor, tmpdir, args.band_rows,
                                        log=lambda s: print(s, flush=True))
        return write_pyramid(pyvips, full, out, args, insize)
    except (ValueError, pyvips.Error) as e:
        detail = getattr(e, "detail", "").strip()
        print(f"   FAILED: {getattr(e, 'message', e)}" + (f"\n   {detail}" if detail else ""), file=sys.stderr)
        return False
    finally:
        full = None  # release the strip files before deleting them (Windows)
        remove(strips)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("descriptors", nargs="+",
                   help="Deep Zoom descriptors (.dzi/.xml) or their _files folders, "
                   "Zoomify folders or their ImageProperties.xml")
    p.add_argument("-q", "--quality", type=int, default=85,
                   help="JPEG quality (default 85; the tiles are JPEG already, Q 90 or more "
                   "only makes the file larger)")
    p.add_argument("-o", "--outdir", help="output directory (default: next to the descriptor)")
    p.add_argument("-s", "--suffix", default="_jpeg_q{q}",
                   help="appended to the descriptor name (default _jpeg_q{q}, {q} = quality)")
    p.add_argument("--name", help="output file name without .tif (only with a single input)")
    p.add_argument("--tile", type=int, default=256, help="tile size of the output (default 256)")
    p.add_argument("--band-rows", type=int, default=0,
                   help="tile rows joined per temporary strip (default: as many as fit in about "
                   "256 MB of decoded tiles); fewer rows use less memory")
    p.add_argument("--tmpdir", help="directory for the temporary strips and libvips temporary files "
                   "(default: output directory); needs width x height x 3 bytes")
    p.add_argument("-f", "--force", action="store_true", help="overwrite existing output")
    p.add_argument("-n", "--dry-run", action="store_true", help="only show what would be done")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = p.parse_args()
    if args.name and len(args.descriptors) > 1:
        p.error("--name only works with a single input")

    pyvips = start_pyvips(args.tmpdir or args.outdir or os.path.dirname(os.path.abspath(args.descriptors[0])))
    ok = sum(convert(pyvips, d, args) for d in args.descriptors)
    if ok != len(args.descriptors):
        print(f"{len(args.descriptors) - ok} of {len(args.descriptors)} inputs failed or skipped", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
