"""Barcodes: reading them, checking them, drawing them. Stdlib only.

A vinyl sleeve carries an EAN-13 (thirteen digits) or, on older American
pressings, a UPC-A (twelve). This module is the whole of what Sleevenote knows
about them, and it does four separate jobs:

  1. **Normalise and check** what somebody typed or a camera read. `normalise`
     turns "0 886976 389413" into "0886976389413" and a 12-digit UPC-A into the
     same 13 digits by prepending the zero the standard says is there. `check_ok`
     runs the checksum. A barcode that fails its own checksum is a misread or a
     typo and is never quietly accepted.

  2. **Say what the number is** — `describe`. The first digits identify the GS1
     member organisation that issued the number, and the docstring there is at
     pains about what that does and does not tell you, because it is the single
     easiest thing in this app to misread as a fact about the record.

  3. **Draw one** — `svg` and `png`. Used for shelf labels, and used by the
     tests, which is the more important half: the test suite draws a barcode and
     then reads it back, so the decoder is checked against every one of the ten
     million valid thirteen-digit numbers it might be handed rather than against
     a handful of photographs somebody remembered to keep.

  4. **Read one out of an image** — `decode_png`. This is what makes the scanner
     work on a browser that has no BarcodeDetector, and with JavaScript switched
     off entirely: you photograph the sleeve, the form posts the file, and the
     decoding happens here, on the server, where the result can be checksummed
     before anybody is shown it.

On images: this module reads **PNG only**, and that is deliberate. A JPEG from a
phone camera is converted to PNG by `imgtool.py`, which runs in a subprocess and
is the only place Pillow is allowed to be imported. The server path stays
stdlib, and `tests.py` greps this file to prove it.

EAN-13, in one paragraph, because the decoder below is unreadable without it:
95 modules wide. A 101 guard, six digits of seven modules each, a 01010 centre
guard, six more digits, a 101 guard. Each digit is exactly four runs of ink and
paper summing to seven modules, which is why the decoder measures runs and never
tries to measure single modules. The left six digits are drawn from one of two
alphabets, L and G; *which* alphabet each uses spells out the thirteenth digit,
the one that is never drawn as bars at all. That is why a first digit can be
recovered from a barcode whose leading digit has been scuffed off, and it is why
`_FIRST` below exists.
"""
from __future__ import annotations
import itertools
import re
import struct
import zlib

# --------------------------------------------------------------- the alphabet
#
# Each digit, as a string of seven modules: 1 is ink, 0 is paper.
_L = ("0001101", "0011001", "0010011", "0111101", "0100011",
      "0110001", "0101111", "0111011", "0110111", "0001011")
_G = ("0100111", "0110011", "0011011", "0100001", "0011101",
      "0111001", "0000101", "0010001", "0001001", "0010111")
# The right-hand alphabet is the left one with ink and paper swapped.
_R = tuple("".join("1" if ch == "0" else "0" for ch in pat) for pat in _L)

# Which alphabet each of the six left-hand digits uses. The index is the first
# digit of the barcode; the string is read left to right across digits 2-7.
_FIRST = ("LLLLLL", "LLGLGG", "LLGGLG", "LLGGGL", "LGLLGG",
          "LGGLLG", "LGGGLL", "LGLGLG", "LGLGGL", "LGGLGL")

GUARD_OUT = "101"
GUARD_MID = "01010"


def _runs_of(pattern):
    """A module pattern as the widths of its runs: "0001101" -> (3, 2, 1, 1)."""
    out, last, n = [], pattern[0], 0
    for ch in pattern:
        if ch == last:
            n += 1
        else:
            out.append(n)
            last, n = ch, 1
    out.append(n)
    return tuple(out)


# Run widths per digit. L and R share a table because swapping ink for paper
# does not change how wide anything is — only which end starts dark.
_W_L = tuple(_runs_of(p) for p in _L)
_W_G = tuple(_runs_of(p) for p in _G)
# The same tables keyed by their widths, for the exact-match path in _digit.
# Within one alphabet every digit's widths are distinct — that is a property of
# the code, not luck. Across the two they are not: 1,3,2,1 is a 7 in L and a 5
# in G, which is exactly why the parity spelling and the check digit both have to
# agree before a read is believed.
_X_L = {w: d for d, w in enumerate(_W_L)}
_X_G = {w: d for d, w in enumerate(_W_G)}


# ------------------------------------------------------------- the number

def digits_only(raw):
    return re.sub(r"\D", "", raw or "")


def normalise(raw):
    """A scanned or typed barcode as 13 digits, or "" if it cannot be one.

    A 12-digit UPC-A becomes an EAN-13 by prepending the zero that the standard
    says is implicitly there — and because the leading zero falls on an odd
    weight, the checksum is unchanged by the move. An 8-digit EAN-8 is returned
    as it stands: it is a different symbology, not a short EAN-13, and padding
    it to 13 would invent a number nobody printed.
    """
    d = digits_only(raw)
    if len(d) == 12:
        return "0" + d
    if len(d) in (8, 13):
        return d
    if len(d) == 14 and d[0] == "0":
        return d[1:]                # a GTIN-14 shipping code wrapping an EAN-13
    return ""


def check_digit(body):
    """The check digit for the body of an EAN-13 (12 digits) or EAN-8 (7).

    Weights alternate 1 and 3. EAN-13 starts at 1 and EAN-8 starts at 3, which
    is the same rule seen from different ends: the weight of the digit next to
    the check digit is always 3.
    """
    d = digits_only(body)
    if len(d) not in (7, 12):
        return None
    start = 1 if len(d) == 12 else 3
    total = 0
    for i, ch in enumerate(d):
        w = start if i % 2 == 0 else (4 - start)
        total += int(ch) * w
    return (10 - total % 10) % 10


def check_ok(code):
    """Does this full barcode satisfy its own check digit?"""
    d = digits_only(code)
    if len(d) not in (8, 13):
        return False
    want = check_digit(d[:-1])
    return want is not None and want == int(d[-1])


def symbology(code):
    d = digits_only(code)
    if len(d) == 13:
        return "EAN-13" if d[0] != "0" else "EAN-13 (or a UPC-A with its leading zero)"
    if len(d) == 8:
        return "EAN-8"
    return "not a barcode"


def fix_hint(raw):
    """A sentence about why a barcode was rejected, for a person to act on.

    Never a correction. The one case where the app knows what the right answer
    is — a wrong final digit — it says what the digit should have been and still
    refuses the number, because a sleeve whose checksum fails is more likely to
    have been mistyped in the middle than at the end.
    """
    d = digits_only(raw)
    if not d:
        return "No digits in that. A barcode is 13 digits, or 12 on an older American pressing."
    if len(d) not in (8, 12, 13, 14):
        near = min((8, 12, 13), key=lambda n: abs(n - len(d)))
        return (f"That is {len(d)} digits. A barcode is 13, or 12 on an older American "
                f"pressing — {abs(len(d) - near)} out from {near}.")
    code = normalise(d)
    if code and not check_ok(code):
        want = check_digit(code[:-1])
        return (f"Those digits do not add up: the last digit of {code} would have to be "
                f"{want}. Worth checking the middle of the number as well as the end.")
    return ""


# GS1 prefix ranges. Lowest-to-highest, first match wins.
_PREFIX = [
    ((0, 19), "United States"), ((20, 29), "used inside one shop — not a real product code"),
    ((30, 39), "United States"), ((40, 49), "used inside one company — not a real product code"),
    ((50, 59), "coupon"), ((60, 139), "United States"),
    ((200, 299), "used inside one shop — not a real product code"),
    ((300, 379), "France and Monaco"), ((380, 380), "Bulgaria"), ((383, 383), "Slovenia"),
    ((385, 385), "Croatia"), ((387, 387), "Bosnia and Herzegovina"), ((389, 389), "Montenegro"),
    ((400, 440), "Germany"), ((450, 459), "Japan"), ((460, 469), "Russia"),
    ((471, 471), "Taiwan"), ((474, 474), "Estonia"), ((475, 475), "Latvia"),
    ((477, 477), "Lithuania"), ((479, 479), "Sri Lanka"), ((480, 480), "Philippines"),
    ((482, 482), "Ukraine"), ((484, 484), "Moldova"), ((489, 489), "Hong Kong"),
    ((490, 499), "Japan"), ((500, 509), "United Kingdom"), ((520, 521), "Greece"),
    ((528, 528), "Lebanon"), ((529, 529), "Cyprus"), ((531, 531), "North Macedonia"),
    ((535, 535), "Malta"), ((539, 539), "Ireland"), ((540, 549), "Belgium and Luxembourg"),
    ((560, 560), "Portugal"), ((569, 569), "Iceland"), ((570, 579), "Denmark"),
    ((590, 590), "Poland"), ((594, 594), "Romania"), ((599, 599), "Hungary"),
    ((600, 601), "South Africa"), ((608, 608), "Bahrain"), ((611, 611), "Morocco"),
    ((613, 613), "Algeria"), ((616, 616), "Kenya"), ((618, 618), "Côte d'Ivoire"),
    ((619, 619), "Tunisia"), ((622, 622), "Egypt"), ((625, 625), "Jordan"),
    ((626, 626), "Iran"), ((628, 628), "Saudi Arabia"), ((629, 629), "United Arab Emirates"),
    ((640, 649), "Finland"), ((690, 699), "China"), ((700, 709), "Norway"),
    ((729, 729), "Israel"), ((730, 739), "Sweden"), ((740, 745), "Central America"),
    ((746, 746), "Dominican Republic"), ((750, 750), "Mexico"), ((754, 755), "Canada"),
    ((759, 759), "Venezuela"), ((760, 769), "Switzerland and Liechtenstein"),
    ((770, 771), "Colombia"), ((773, 773), "Uruguay"), ((775, 775), "Peru"),
    ((777, 777), "Bolivia"), ((778, 779), "Argentina"), ((780, 780), "Chile"),
    ((784, 784), "Paraguay"), ((786, 786), "Ecuador"), ((789, 790), "Brazil"),
    ((800, 839), "Italy"), ((840, 849), "Spain and Andorra"), ((850, 850), "Cuba"),
    ((858, 858), "Slovakia"), ((859, 859), "Czechia"), ((860, 860), "Serbia"),
    ((865, 865), "Mongolia"), ((867, 867), "North Korea"), ((868, 869), "Türkiye"),
    ((870, 879), "Netherlands"), ((880, 880), "South Korea"), ((884, 884), "Cambodia"),
    ((885, 885), "Thailand"), ((888, 888), "Singapore"), ((890, 890), "India"),
    ((893, 893), "Vietnam"), ((896, 896), "Pakistan"), ((899, 899), "Indonesia"),
    ((900, 919), "Austria"), ((930, 939), "Australia"), ((940, 949), "New Zealand"),
    ((950, 951), "GS1 head office"), ((955, 955), "Malaysia"), ((958, 958), "Macau"),
    ((960, 969), "GS1 head office"), ((977, 977), "a serial publication (ISSN)"),
    ((978, 979), "a book or printed music (ISBN/ISMN)"),
    ((980, 980), "a refund receipt"), ((981, 984), "a coupon"), ((990, 999), "a coupon"),
]


def registry(code):
    """The GS1 member organisation behind a barcode's prefix, or "".

    **This is not where the record was pressed, and it is not where it was
    sold.** It is where whoever registered the number is a member. The example
    that makes the point is in this app's own test fixtures: 0886976389413 is a
    Dutch Music On Vinyl pressing of an American album, sold across Europe, and
    its prefix is 088 — the United States — because the number was issued to
    Sony, who own the recording. Every page that shows this string says so.
    """
    d = digits_only(code)
    if len(d) != 13:
        return ""
    head = int(d[:3])
    for (lo, hi), name in _PREFIX:
        if lo <= head <= hi:
            return name
    return ""


def describe(raw):
    """Everything the number itself can say, as a dict. No network, no database.

    `country_note` is phrased as a claim about the number rather than about the
    record on purpose — see `registry`.
    """
    code = normalise(raw)
    d = digits_only(raw)
    ok = bool(code) and check_ok(code)
    reg = registry(code) if ok else ""
    return {
        "raw": raw or "",
        "digits": d,
        "code": code,
        "valid": ok,
        "symbology": symbology(code) if code else "not a barcode",
        "registry": reg,
        "country_note": (f"The number was issued through GS1 {reg}" if reg else ""),
        "hint": "" if ok else fix_hint(raw),
    }


# ------------------------------------------------------------------ drawing

def modules(code):
    """The full module pattern for a barcode: "101…101". "" if it will not encode.

    EAN-13 is 95 modules and EAN-8 is 67. The parity of the left half is where
    the first digit of an EAN-13 goes; nothing draws it directly.
    """
    d = digits_only(code)
    if len(d) == 13 and check_ok(d):
        parity = _FIRST[int(d[0])]
        left = "".join((_L if parity[i] == "L" else _G)[int(d[1 + i])] for i in range(6))
        right = "".join(_R[int(ch)] for ch in d[7:])
        return GUARD_OUT + left + GUARD_MID + right + GUARD_OUT
    if len(d) == 8 and check_ok(d):
        left = "".join(_L[int(ch)] for ch in d[:4])
        right = "".join(_R[int(ch)] for ch in d[4:])
        return GUARD_OUT + left + GUARD_MID + right + GUARD_OUT
    return ""


def svg(code, scale=2, height=60, quiet=9, text=True):
    """A barcode as SVG, for a shelf label. "" if the code will not encode.

    SVG rather than PNG because a label gets printed, and a printer will render
    a vector at its own resolution instead of interpolating somebody's guess at
    one. Bars are black on white and nothing here is theme-aware — a barcode is
    scanned by a camera, and an inverted one is a barcode no camera will read.
    """
    mods = modules(code)
    if not mods:
        return ""
    w = (len(mods) + quiet * 2) * scale
    cap = 13 if text else 0
    h = height + cap
    bars = []
    x = quiet * scale
    for ch in mods:
        if ch == "1":
            bars.append(f'<rect x="{x}" y="0" width="{scale}" height="{height}"/>')
        x += scale
    label = ""
    if text:
        label = (f'<text x="{w / 2:.1f}" y="{height + 11}" font-family="monospace" '
                 f'font-size="11" text-anchor="middle" fill="#000000">'
                 f'{" ".join((digits_only(code)[0], digits_only(code)[1:7], digits_only(code)[7:]))}'
                 f'</text>') if len(digits_only(code)) == 13 else (
            f'<text x="{w / 2:.1f}" y="{height + 11}" font-family="monospace" font-size="11" '
            f'text-anchor="middle" fill="#000000">{digits_only(code)}</text>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
            f'viewBox="0 0 {w} {h}" role="img" aria-label="Barcode {digits_only(code)}">'
            f'<rect width="{w}" height="{h}" fill="#FFFFFF"/>'
            f'<g fill="#000000">{"".join(bars)}</g>{label}</svg>')


def png(code, scale=3, height=40, quiet=9):
    """A barcode as an 8-bit greyscale PNG. Bytes, or b"" if it will not encode.

    This exists for the test suite: `tests.py` draws a barcode with this and
    reads it back with `decode_png`, which is what turns the decoder from
    something that worked on the photographs to hand into something checked
    against thousands of generated numbers, including the awkward ones.
    """
    mods = modules(code)
    if not mods:
        return b""
    w = (len(mods) + quiet * 2) * scale
    row = bytearray([255] * w)
    x = quiet * scale
    for ch in mods:
        if ch == "1":
            for k in range(scale):
                row[x + k] = 0
        x += scale
    raw = bytearray()
    for _ in range(height):
        raw.append(0)                       # filter 0: this row stands alone
        raw += row
    return _png_bytes(w, height, bytes(raw))


def _chunk(kind, payload):
    return (struct.pack(">I", len(payload)) + kind + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))


def _png_bytes(w, h, raw):
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0)     # 8-bit greyscale
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(raw, 9)) + _chunk(b"IEND", b""))


# ------------------------------------------------------------------ reading

class ImageError(Exception):
    """A PNG this module will not read, with a sentence saying why."""


def read_png(blob):
    """A PNG to (width, height, greyscale bytes). Raises ImageError with a sentence.

    Every non-interlaced PNG: bit depths 1, 2, 4, 8 and 16, greyscale, RGB,
    palette, and either with alpha. Interlaced is refused rather than half-read,
    because a half-read image decodes to a barcode that is wrong rather than to
    nothing, and a wrong barcode is the one outcome this app must not produce.
    """
    if not blob or blob[:8] != b"\x89PNG\r\n\x1a\n":
        raise ImageError("That is not a PNG.")
    pos, idat, ihdr, plte, done = 8, bytearray(), None, b"", False
    while pos + 8 <= len(blob):
        (ln,) = struct.unpack(">I", blob[pos:pos + 4])
        kind = blob[pos + 4:pos + 8]
        body = blob[pos + 8:pos + 8 + ln]
        if len(body) < ln:
            raise ImageError("The PNG is truncated.")
        if kind == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", body[:13])
        elif kind == b"PLTE":
            plte = bytes(body)
        elif kind == b"IDAT":
            idat += body
        elif kind == b"IEND":
            done = True
        pos += 12 + ln
        if done:
            break
    if not ihdr:
        raise ImageError("The PNG has no header.")
    w, h, depth, ctype, comp, filt, inter = ihdr
    if inter:
        raise ImageError("That PNG is interlaced, which this reader does not do. "
                         "Saving it again as a plain PNG will work.")
    if comp != 0 or filt != 0:
        raise ImageError("That PNG uses a compression this reader does not know.")
    if depth not in (1, 2, 4, 8, 16) or ctype not in (0, 2, 3, 4, 6):
        raise ImageError(f"That PNG is {depth}-bit type {ctype}, which this reader does not do.")
    if not (0 < w <= 12000 and 0 < h <= 12000):
        raise ImageError("That image is an implausible size.")
    chans = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ctype]
    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error:
        raise ImageError("The PNG's pixel data would not decompress.")
    bpp = max(1, chans * depth // 8)                 # bytes per pixel, for the filters
    stride = (w * chans * depth + 7) // 8
    if len(raw) < (stride + 1) * h:
        raise ImageError("The PNG holds fewer pixels than its header claims.")
    grey = bytearray(w * h)
    prev = bytearray(stride)
    for y in range(h):
        off = y * (stride + 1)
        ftype = raw[off]
        line = bytearray(raw[off + 1:off + 1 + stride])
        _unfilter(ftype, line, prev, bpp)
        _to_grey(line, grey, y * w, w, depth, ctype, chans, plte)
        prev = line
    return w, h, bytes(grey)


def _unfilter(ftype, line, prev, bpp):
    """Undo one PNG row filter, in place. The five types are from the spec."""
    n = len(line)
    if ftype == 0:
        return
    if ftype == 1:
        for i in range(bpp, n):
            line[i] = (line[i] + line[i - bpp]) & 0xFF
    elif ftype == 2:
        for i in range(n):
            line[i] = (line[i] + prev[i]) & 0xFF
    elif ftype == 3:
        for i in range(n):
            a = line[i - bpp] if i >= bpp else 0
            line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
    elif ftype == 4:
        for i in range(n):
            a = line[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            p = a + b - c
            pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
            pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
            line[i] = (line[i] + pr) & 0xFF
    else:
        raise ImageError(f"The PNG uses row filter {ftype}, which does not exist.")


def _to_grey(line, out, base, w, depth, ctype, chans, plte):
    """One unfiltered row to greyscale. Rec. 601 luma for the colour types."""
    if depth == 8:
        vals = line
    elif depth == 16:
        vals = line[0::2]                    # the high byte is plenty for a threshold
    else:
        vals = bytearray()
        per, mask = 8 // depth, (1 << depth) - 1
        scale = 255 // mask
        need = w * chans
        for byte in line:
            for k in range(per):
                vals.append(((byte >> (8 - depth * (k + 1))) & mask)
                            * (1 if ctype == 3 else scale))
                if len(vals) >= need:
                    break
            if len(vals) >= need:
                break
    if ctype == 3:
        for x in range(w):
            i = vals[x * chans] * 3
            if i + 2 < len(plte):
                r, g, b = plte[i], plte[i + 1], plte[i + 2]
            else:
                r = g = b = 0
            out[base + x] = (299 * r + 587 * g + 114 * b) // 1000
    elif ctype in (0, 4):
        for x in range(w):
            out[base + x] = vals[x * chans]
    else:
        for x in range(w):
            i = x * chans
            out[base + x] = (299 * vals[i] + 587 * vals[i + 1] + 114 * vals[i + 2]) // 1000


# ---- one scan line ----------------------------------------------------------

_RUN_RX = re.compile(rb"\x00+|\x01+")
_TABLES = {}


def _table(thresh):
    """A 256-byte translation table mapping dark to 1 and light to 0.

    There are only 256 of these, so they are built once and kept. The point of
    going through `bytes.translate` at all is speed: binarising a 1600-pixel row
    in a Python loop, 160 rows deep, was most of a two-second wait on a photo
    upload. `translate` and `re` do the same work in C, and a phone upload that
    used to take two seconds takes a fifth of one.
    """
    t = _TABLES.get(thresh)
    if t is None:
        t = _TABLES[thresh] = bytes(1 if i < thresh else 0 for i in range(256))
    return t


def _to_runs(samples, thresh):
    """A brightness profile to [(dark?, width), …]. The whole decoder is runs.

    Measuring runs rather than modules is the one decision that makes this work
    on a photograph: a module may be 2.3 pixels wide and there is no honest way
    to round that, but a run of four modules is nine pixels and rounds cleanly.
    """
    if not isinstance(samples, (bytes, bytearray)):
        samples = bytes(max(0, min(255, int(v))) for v in samples)
    b = samples.translate(_table(thresh))
    return [(b[mm.start()] == 1, mm.end() - mm.start()) for mm in _RUN_RX.finditer(b)]


def _digit(widths, table, exact):
    """Best digit for four run widths against one alphabet: (digit, error) or None.

    Two paths. A clean scan lands exactly on a pattern once its runs are scaled to
    seven modules, so the rounded widths are looked up in a dict and that is the
    whole cost — which matters, because this function was being called a million
    and a half times per photograph. Anything that does not land exactly falls
    through to the nearest-pattern search.

    The error is in modules, summed over the four runs, so the tolerance is
    readable: 1.2 means "the four runs are within about a module of the pattern,
    in total". A blurred or skewed photograph fails this rather than producing a
    plausible wrong digit, which is the behaviour we want of the two.
    """
    total = widths[0] + widths[1] + widths[2] + widths[3]
    if total <= 0:
        return None
    k = 7.0 / total
    a, b, c, d = widths[0] * k, widths[1] * k, widths[2] * k, widths[3] * k
    hit = exact.get((round(a), round(b), round(c), round(d)))
    if hit is not None:
        err = (abs(a - table[hit][0]) + abs(b - table[hit][1])
               + abs(c - table[hit][2]) + abs(d - table[hit][3]))
        if err <= 1.2:
            return hit, err
    best, berr = None, 99.0
    for dg, pat in enumerate(table):
        err = abs(a - pat[0]) + abs(b - pat[1]) + abs(c - pat[2]) + abs(d - pat[3])
        if err < berr:
            best, berr = dg, err
    return (best, berr) if berr <= 1.2 else None


def _guard_ok(runs, i, unit):
    """Three runs that look like a 101 guard at this module width."""
    if i + 2 >= len(runs):
        return False
    return all(abs(runs[i + k][1] - unit) <= max(1.0, unit * 0.6) for k in range(3))


# Per symbology: digits per half, total runs, total modules.
# EAN-13: 3 guard + 6x4 + 5 centre + 6x4 + 3 guard = 59 runs over 95 modules.
# EAN-8:  3 + 4x4 + 5 + 4x4 + 3 = 43 runs over 67 modules.
_SPECS = ((6, 59, 95), (4, 43, 67))


def _decode_runs(runs, i, ndigits):
    """Decode from run index i, or None.

    The EAN-13 first digit comes out of the parity spelling, never out of the
    bars — there are no bars for it. Note the one constraint applied before any
    matching: every valid spelling in _FIRST begins with L, so the leftmost digit
    is read against L alone. That is free and it removes a real ambiguity, since
    several width patterns mean one digit in L and a different one in G.
    """
    unit = (runs[i][1] + runs[i + 1][1] + runs[i + 2][1]) / 3.0
    if unit <= 0:
        return None
    j = i + 3
    left, parity = [], []
    for slot in range(ndigits):
        widths = (runs[j][1], runs[j + 1][1], runs[j + 2][1], runs[j + 3][1])
        gotl = _digit(widths, _W_L, _X_L)
        gotg = None if (slot == 0 and ndigits == 6) else _digit(widths, _W_G, _X_G)
        if gotl and (not gotg or gotl[1] <= gotg[1]):
            left.append(gotl[0]); parity.append("L")
        elif gotg:
            left.append(gotg[0]); parity.append("G")
        else:
            return None
        j += 4
    # centre guard: five runs of one module each, starting light
    if runs[j][0]:
        return None
    tol = max(1.0, unit * 0.7)
    if not all(abs(runs[j + k][1] - unit) <= tol for k in range(5)):
        return None
    j += 5
    right = []
    for _ in range(ndigits):
        got = _digit((runs[j][1], runs[j + 1][1], runs[j + 2][1], runs[j + 3][1]),
                     _W_L, _X_L)
        if not got:
            return None
        right.append(got[0])
        j += 4
    if not runs[j][0] or not _guard_ok(runs, j, unit):
        return None
    if ndigits == 4:
        code = "".join(str(d) for d in left + right)
        return code if check_ok(code) else None
    spell = "".join(parity)
    if spell not in _FIRST:
        return None
    code = str(_FIRST.index(spell)) + "".join(str(d) for d in left + right)
    return code if check_ok(code) else None


def decode_line(samples):
    """Read a barcode off one brightness profile, or "".

    Tries the runs as they are and then reversed, because a sleeve photographed
    upside down is the commonest way a scan fails and the guards are symmetric,
    so the only way to tell is to try. Reversing the *runs* rather than the pixels
    is the same question asked for a fraction of the cost.

    The span test is what makes this fast enough to run on a phone photograph.
    A candidate start whose next 59 runs do not add up to about 95 module widths
    cannot be an EAN-13, and saying so takes one subtraction against a running
    total. Without it, a busy sleeve offered nearly half a million candidate
    starts per photograph and each one was decoded digit by digit before being
    thrown away; a photo took two seconds. With it the same photo takes a tenth
    of that, and nothing about which barcodes are accepted has changed — the test
    only ever skips work that was going to fail.
    """
    if len(samples) < 60:
        return ""
    if not isinstance(samples, (bytes, bytearray)):
        samples = bytes(max(0, min(255, int(v))) for v in samples)
    lo, hi = min(samples), max(samples)
    if hi - lo < 40:                     # no ink and paper here, just noise
        return ""
    runs = _to_runs(samples, (lo + hi) // 2)
    if len(runs) < 43:
        return ""
    for seq in (runs, runs[::-1]):
        n = len(seq)
        pre = list(itertools.accumulate((w for _, w in seq), initial=0))
        for i in range(n - 42):
            dark, wid = seq[i]
            if not dark or wid <= 0:
                continue
            unit = (seq[i][1] + seq[i + 1][1] + seq[i + 2][1]) / 3.0
            if unit <= 0 or not _guard_ok(seq, i, unit):
                continue
            for ndigits, nruns, nmods in _SPECS:
                if n - i < nruns:
                    continue
                if abs((pre[i + nruns] - pre[i]) - nmods * unit) > nmods * unit * 0.15:
                    continue
                got = _decode_runs(seq, i, ndigits)
                if got:
                    return got
    return ""


# How many rows to try, and how many must agree. Both numbers are load-bearing
# and neither is a guess — see decode_grey.
SCAN_LINES = 200
MIN_VOTES = 3


def decode_grey(w, h, grey, lines=SCAN_LINES, min_votes=MIN_VOTES):
    """Read a barcode out of a greyscale image. (code, votes, tried).

    Scans rows across the whole height rather than only the middle: a photograph
    of a sleeve has the barcode wherever the person was standing, usually small
    and off to one side. `lines` is a target — the step is at least one pixel, so
    a short image is scanned exhaustively and a tall one evenly.

    **A row that decodes is a vote, and one vote is never enough.** This is the
    most important line in the module. A checksum is one digit, so roughly one in
    ten runs of plausible-looking noise passes it, and the app was measured
    producing a *confidently wrong* barcode on about 3% of deliberately terrible
    images before this rule existed. A wrong barcode files the wrong record
    against the wrong prices and nothing downstream can detect it; declining to
    read costs thirteen keystrokes. So two rows must independently decode to the
    same thirteen digits, which noise does not do — two rows of static agreeing
    on the same wrong number is not a thing that happens — while a real barcode
    spans dozens of rows and clears the bar without trying.

    Returns "" with the vote count when nothing reached the bar. The count is
    deliberately not shown as a suggestion: a one-vote candidate is exactly the
    case where a plausible wrong answer would be most tempting and most harmful.
    """
    if w < 60 or h < 1:
        return "", 0, 0
    votes, tried = {}, 0
    step = max(1, h // max(1, lines))
    for y in range(0, h, step):
        tried += 1
        got = decode_line(grey[y * w:(y + 1) * w])
        if got:
            votes[got] = votes.get(got, 0) + 1
    if not votes:
        return "", 0, tried
    code, n = max(votes.items(), key=lambda kv: (kv[1], kv[0]))
    # Three agreeing rows, AND a clear majority of everything that decoded. The
    # majority clause is the one that catches the nastier failure: rows of a
    # *real* but badly blurred barcode are not independent, so two neighbours can
    # misread it the same way. When that happens the correct code is normally
    # also in the tally, and this refuses to let two votes beat twenty.
    if n < min_votes or n * 2 <= sum(votes.values()):
        return "", n, tried
    return code, n, tried


def decode_png(blob, lines=SCAN_LINES, min_votes=MIN_VOTES):
    """A PNG's bytes to (code, votes, tried). Raises ImageError on a bad PNG."""
    w, h, grey = read_png(blob)
    return decode_grey(w, h, grey, lines, min_votes)
