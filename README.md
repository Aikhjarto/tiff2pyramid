# tiff2pyramid

Convert TIFFs into JPEG-compressed, tiled, pyramidal BigTIFFs for
[IIPImage](https://iipimage.sourceforge.io/) (`iipsrv`), to be shown with
[OpenSeadragon](https://openseadragon.github.io/) or any other IIIF viewer.

A Deep Zoom or Zoomify export of a large panorama is a folder with hundreds of thousands of small
files. A pyramidal TIFF holds the same tiles and zoom levels in one file, and `iipsrv` serves them
from there. With JPEG compression the stored tiles are sent to the browser as they are, without
decoding or re-encoding, so a tile takes well under a millisecond.

Panorama tools write TIFFs that are not suitable for that: LZW or Deflate compressed, 16-bit or
float, with an alpha channel, sometimes with an ICC profile, often without a pyramid.
`tiff2pyramid.py` turns any of them into 8-bit sRGB (8-bit grey for grey input), flattens the alpha
channel onto a background colour and writes a pyramid of 256×256 JPEG tiles. Pyramidal input is
accepted too; only its full-resolution level is read.

`ice2pyramid.py` (`ICE2pyramid` when installed) does the same for panoramas that only exist as a
Deep Zoom export of Microsoft Image Composite Editor (ICE), see [below](#ice2pyramid).

## Requirements

- libvips (tested with 8.18) with JPEG and TIFF support, and lcms for ICC profiles
- Python ≥ 3.8 with [pyvips](https://github.com/libvips/pyvips)

Installing the package gives the commands `tiff2pyramid` and `ICE2pyramid`:

```sh
pip install "tiff2pyramid[binary] @ git+https://github.com/Aikhjarto/tiff2pyramid"   # with a prebuilt libvips
pip install "tiff2pyramid @ git+https://github.com/Aikhjarto/tiff2pyramid"            # uses the system libvips
```

- Windows, macOS: the `binary` variant needs nothing else. On macOS, `brew install vips` and the
  plain variant work too.
- Linux: libvips from the distribution, e.g. openSUSE Tumbleweed `zypper install python313-pyvips`
  (then the scripts can also be run directly), openSUSE Leap 16 has libvips but no pyvips package,
  so `pip install --user` it.

The tests run on Linux, macOS and Windows on every push (GitHub Actions, `pyvips[binary]`).

### Bash completion

`pip install` also puts completions for both commands into `<prefix>/share/bash-completion/completions`
(with `--user`: `~/.local/share/bash-completion/completions`), where bash-completion 2 loads them
on first use. They complete the options, typical values (`-q`, `-b`, `--tile`), folders for
`-o`/`--tmpdir`, and as input only TIFFs (`tiff2pyramid`) or `.dzi`/`.xml` descriptors and folders
(`ICE2pyramid`). When running from a clone, link or source them:

```sh
mkdir -p ~/.local/share/bash-completion/completions
ln -s "$PWD"/completions/* ~/.local/share/bash-completion/completions/
# or, without bash-completion, in ~/.bashrc:  . /path/to/tiff2pyramid/completions/tiff2pyramid
```

## Usage

```sh
python tiff2pyramid.py pano.tif                         # writes pano_jpeg_q90.tif next to it
python tiff2pyramid.py -q 85 -o /srv/tiles *.tif         # several files into one folder
python tiff2pyramid.py -n pano.tif                       # only show what would be done
python tiff2pyramid.py pano.tif -o /srv/tiles --name Pano2026
```

What is done to each file is printed, for example:

```
== Panorama7.afphoto.tiff
   input 107687x5163, ushort, 4 bands, rgb16, alpha, ICC profile
   ICC profile -> sRGB (8 bit)
   flatten alpha onto background [0.0]
   -> Panorama7.afphoto_jpeg_q90.tif (JPEG Q90, 256px tiles, pyramid, BigTIFF)
   done in 31 s, 10 levels, 688.6 MB -> 242.5 MB (35 %)
```

Notes:
- `-q` (default 90) is the JPEG quality. libvips writes JPEG-in-TIFF with 4:2:0 chroma subsampling
  below Q 90 and as plain RGB without subsampling from Q 90 on. Q 90 files are therefore about
  three times the size of Q 85 files, for a difference that is hard to see. Since `iipsrv` sends
  the stored tiles unchanged, Q is also exactly what the browser downloads.
- `-b` sets the colour of transparent areas, 0–255, one value or `R,G,B` (default 0, black).
- 16-bit input is scaled to 8 bit (not clipped). An embedded ICC profile is converted to sRGB;
  `--ignore-icc` skips that.
- For float and 32-bit integer input, the white level (0–1, 0–255 or 0–65535) is detected from
  the largest value, taken from the smallest pyramid level if there is one. libvips labels most
  float RGB TIFFs as linear scRGB, even when they hold gamma-encoded 0–255 data. Float data in 0–1
  labelled scRGB is converted from linear light; `--no-linear` treats it as gamma-encoded, and
  `--white` sets the white level by hand.
- Without `--outdir` the output is written next to the input as `<name>_jpeg_q<Q>.tif`
  (`--suffix` changes the ending). Existing files are skipped unless `-f` is given.
- The result is written as `<output>.part`, checked and only then renamed, so an aborted run does
  not leave a broken TIFF behind.
- libvips writes the smaller pyramid levels to temporary files before it assembles the output.
  They go to the output folder unless `--tmpdir` is given, to keep them off a small `/tmp`.
- Input is read sequentially, so memory use stays below about 1 GB even for multi-gigapixel
  panoramas (measured: 840 MB for a 107687×5163 16-bit image).
- TIFFs are opened without libtiff's 50 MB memory limit (libvips `unlimited`). Very wide
  panoramas are often written with strips larger than that, and libvips refuses to read them
  otherwise ("Cumulated memory allocation ... is beyond the 52428800 cumulated byte limit").
  Only convert files you trust.

## ICE2pyramid

Microsoft Image Composite Editor can export a panorama as a "Deep Zoom tileset": a descriptor
(`<name>.dzi` or `<name>.xml`) and a folder `<name>_files` with one subfolder per zoom level, holding
254×254 JPEG tiles with 1 px overlap. If the panorama was never saved in another format, these tiles
are all that is left of it. `ice2pyramid.py` reads the full-resolution level, puts the tiles back
together without their overlap and writes the same kind of pyramidal TIFF as `tiff2pyramid.py`.

```sh
python ice2pyramid.py pano.dzi                           # writes pano_jpeg_q85.tif next to it
python ice2pyramid.py pano_files --tmpdir /big/disk -o /srv/tiles --name Pano2026
```

Notes:
- The descriptor or its `_files` folder can be given. A descriptor renamed to `<name>_files.dzi`
  with the tiles still in `<name>_files` is accepted too. Other files in the tile folder (earlier
  experiments, text files) are ignored; missing tiles are reported before anything is written.
- Any Deep Zoom tile set works, not only ICE's (e.g. from `vips dzsave`). ICE writes
  `Format="jpeg"` but names the tiles `.jpg`, both spellings are found.
- The tiles are joined in strips that are written uncompressed to `--tmpdir` (default: the output
  folder) before the pyramid is built. That needs width × height × 3 bytes of temporary disk space,
  23 GB for a 198948×41968 panorama, and the free space is checked first.
- libvips keeps every decoded tile of a strip until the strip is written, so the strip height is
  chosen to hold about 256 MB of tiles (`--band-rows` sets it by hand). With 8 tile rows of a
  200000 px wide panorama, memory use went beyond 2.5 GB.
- The JPEG tiles are decoded and encoded again, so a little quality is lost; `-q` and the other
  output options work as in `tiff2pyramid.py`, but the default quality is 85. ICE compresses its
  tiles quite strongly, and storing them again at Q 90 (plain RGB, see above) only makes the file
  bigger. Measured on a 108312×5013 ICE export with 42 MB of tiles:

  | `-q` | size | compared with the ICE tiles |
  |---|---|---|
  | 75 | 55 MB | 42.9 dB |
  | 85 | 76 MB | 44.6 dB |
  | 90 | 266 MB | 48.4 dB |
- When interrupted or killed (SIGTERM, SIGHUP), the temporary strips and the unfinished output are
  removed.

## Serving with iipsrv

`iipsrv` speaks FastCGI only, so a web server has to sit in front of it. Example with nginx, with
the converted files in `/srv/tiles`:

```sh
# /etc/default/iipsrv (openSUSE package)
HOST="127.0.0.1"
PORT=9000
FILESYSTEM_PREFIX=/srv/tiles/
URI_MAP="iiif=>IIIF"
```

```nginx
location /iiif/ {
    fastcgi_pass 127.0.0.1:9000;
    include fastcgi_params;
    fastcgi_param PATH_INFO $fastcgi_script_name;
    fastcgi_param HTTPS $https if_not_empty;
}
```

OpenSeadragon then gets size, tile size and zoom levels from the IIIF `info.json`:

```js
OpenSeadragon({
    id: "viewer",
    prefixUrl: "openseadragon/images/",
    tileSources: ["/iiif/Pano2026.tif/info.json"]
});
```

If the site is served over https or through a proxy, set `BASE_URL` in iipsrv, otherwise the `id`
in `info.json`, which OpenSeadragon uses for every tile request, points to the wrong place.

## Formats that were tried and not used

Measured on a 132269×22508 panorama (6.5 GB as LZW TIFF):

| Format | File size | per 256 px tile from iipsrv |
|---|---|---|
| TIFF, JPEG Q 85 | 508 MB | 0.5–1.2 ms |
| TIFF, JPEG Q 90 | 1731 MB | not measured (also passed through) |
| JPEG 2000 Q 48 (OpenJPEG), 1024 px tiles | 456 MB | 70–100 ms |

- **JPEG 2000**: about 40–50 % smaller than JPEG at the same quality, but iipsrv with OpenJPEG
  re-reads the file header and decodes and re-encodes every tile, about 100 times slower.
  Chroma-subsampled JPEG 2000 crashes iipsrv 1.3.
- **WebP inside TIFF**: libvips writes it, but libtiff has to be built with WebP support to read
  it (openSUSE's is not), and iipsrv reads TIFFs through libtiff.

## Test

```sh
python tests/test_synthetic.py
python tests/test_ice.py
python tests/test_completion.py
```

`test_synthetic.py` writes small constant-colour TIFFs for every supported input type (8/16-bit,
grey/RGB, with and without alpha, linear and 0–255 float, CMYK, pyramidal input, embedded ICC
profile), converts them and checks colour, band count, format and pyramid of each result.

`test_ice.py` cuts an image whose colours encode the pixel position into Deep Zoom tiles with ICE's
layout. From lossless PNG tiles the reassembled image must match exactly; an ICE-like copy (`.xml`
descriptor, `Format="jpeg"`, `.jpg` tiles, unrelated files in the tile folder, renamed descriptor) is
converted end to end.

`test_completion.py` runs the bash completion and checks that it offers exactly the options of
each script's `--help`, the right files and folders (also with spaces in their names) and option
values in both `--opt value` and `--opt=value` form.

## License

GPL-2.0-or-later, see [LICENSE](LICENSE).
