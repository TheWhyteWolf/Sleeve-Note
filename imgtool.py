"""Image conversion for Sleevenote — runs ONLY as a subprocess, never in the server.

    python3 imgtool.py --mode probe
    python3 imgtool.py --mode grey --in FILE --out FILE [--max 1600]

`barcode.py` reads PNG and nothing else, on purpose: a PNG decoder is a hundred
lines of zlib and struct, and a JPEG decoder is not. A phone camera produces
JPEG, so this bridges the two — and it is the only file in the project allowed to
import Pillow.

The separation is enforced rather than promised.
`tests.py:TestPromises.test_pillow_never_enters_the_server_path` greps every
module the server imports for `PIL`, and the app finds an interpreter
that has Pillow by *probing* rather than assuming: on the machine this was written
on, the system python3 has Pillow and the virtualenv next door does not, so an
app that assumed `sys.executable` would have been wrong half the time.

If no interpreter has Pillow, nothing breaks. The photo route says so in a
sentence and points at the two paths that still work: the browser's own scanner,
and typing thirteen digits.
"""
from __future__ import annotations
import argparse
import sys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", required=True, choices=("probe", "grey"))
    ap.add_argument("--in", dest="src")
    ap.add_argument("--out", dest="dst")
    ap.add_argument("--max", type=int, default=1600)
    a = ap.parse_args()
    try:
        from PIL import Image, ImageOps
    except Exception as ex:
        print(f"no-pillow: {type(ex).__name__}", file=sys.stderr)
        return 3
    if a.mode == "probe":
        print("ok")
        return 0
    if not a.src or not a.dst:
        print("need --in and --out", file=sys.stderr)
        return 2
    try:
        with Image.open(a.src) as im:
            # A phone writes the orientation into EXIF rather than into the
            # pixels. A barcode photographed in portrait arrives rotated, and a
            # rotated barcode is one no scan line crosses.
            im = ImageOps.exif_transpose(im)
            im = im.convert("L")
            if a.max and im.width > a.max:
                h = max(1, round(im.height * a.max / im.width))
                im = im.resize((a.max, h), Image.LANCZOS)
            im.save(a.dst, format="PNG", optimize=False)
    except Exception as ex:
        print(f"convert-failed: {type(ex).__name__}: {ex}", file=sys.stderr)
        return 4
    print(f"{im.width}x{im.height}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
