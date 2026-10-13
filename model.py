"""Vocabulary and pure functions for Sleevenote.

No I/O, no database, no network. Everything here is a constant or a function of
its arguments, which is what makes it the one place the rest of the app agrees
with itself about what a word means.

Three ideas carry most of the weight:

  * **A record is a release, and a release is an edition.** "Kind of Blue" is not
    a thing you can own. A 2010 Music On Vinyl 180g reissue with catalogue number
    MOVLP 019 is. Two editions of the same album are two different objects with
    two different prices, and the barcode on the sleeve is a property of the
    edition, not of the album. Everything in this app is organised that way.

  * **A grade is a claim, and the claim needs its words.** The owner asked for
    1-5, separately for the record and the packaging, so that is what is stored.
    But a bare "3" means nothing to anybody else, so every level carries the
    sentence that defines it (GRADE_MEDIA / GRADE_SLEEVE) and the sentence is
    shown wherever the number is entered. GOLDMINE maps each level onto the
    grade the trade actually uses, because that is the vocabulary every price
    in this app is quoted in.

  * **A price is a number plus what kind of number it is.** "Lowest asking
    price, any condition, 14 listings" and "what a copy sold for" are not the
    same fact and must never be subtracted from one another. PRICE_KINDS is the
    list of things a number in this app is allowed to be, and every one of them
    carries the sentence that says what it is not.
"""
from __future__ import annotations
import datetime
import re

APP_NAME = "Sleevenote"

# --------------------------------------------------------------- condition
#
# The owner's scale is 1-5, and 1-5 is what is stored. The descriptions are not
# decoration: a number on a five-point scale is only comparable between two
# people, or between one person and themselves two years later, if the levels
# are pinned to observable facts. These sentences are what pin them, and they
# are shown next to the radio buttons every time a grade is entered.
#
# Note what is deliberately absent: there is no 6 for Mint. Mint means sealed
# and unplayed, and a record you have played is not Mint no matter how careful
# you were. A sealed record is handled the other way, by declining to grade the
# media at all — see UNGRADED_REASONS.
GRADE_MEDIA = {
    5: ("Near Mint", "Looks unplayed. Nothing catches the light when you tilt it, "
                     "and you cannot hear the surface over the music."),
    4: ("Very Good Plus", "A few light marks visible at an angle. Plays through cleanly; "
                          "any noise stays under the music."),
    3: ("Very Good", "Clear marks and some groove wear. Surface noise is audible in the "
                     "quiet passages. No skips."),
    2: ("Good", "Plays all the way through, but there is noise throughout and perhaps a "
                "repeating tick."),
    1: ("Fair or Poor", "Heavy wear. Skips, sticks, or will not play in places."),
}

GRADE_SLEEVE = {
    5: ("Near Mint", "Square corners, no ring wear, no splits. As if it came out of the "
                     "box today."),
    4: ("Very Good Plus", "Slight shelf wear or a faint ring. Corners still square, "
                          "no splits."),
    3: ("Very Good", "Obvious ring wear, softened corners, maybe a short seam split or a "
                     "price sticker."),
    2: ("Good", "A split seam, creases, writing or tape. Still holds the record."),
    1: ("Fair or Poor", "Coming apart, heavily marked, or a piece of it is missing."),
}

# The trade's vocabulary. Every price this app can see is quoted against these
# names, so the mapping is the join between the owner's scale and the market's.
GOLDMINE = {5: "NM", 4: "VG+", 3: "VG", 2: "G", 1: "P"}

# Why a grade is absent. A missing grade is a fact about what has been looked at,
# not a zero and not a five.
UNGRADED_REASONS = {
    "sealed": "Still sealed — the record has not been seen, let alone played.",
    "not_played": "Not played yet. Graded by eye only, so the record is not graded.",
    "not_checked": "Not looked at yet.",
}

GRADES = (5, 4, 3, 2, 1)


def grade_name(scale, g):
    """The short name for a grade on either scale, or "" for an absent one."""
    table = GRADE_SLEEVE if scale == "sleeve" else GRADE_MEDIA
    row = table.get(g)
    return row[0] if row else ""


def grade_words(scale, g):
    table = GRADE_SLEEVE if scale == "sleeve" else GRADE_MEDIA
    row = table.get(g)
    return row[1] if row else ""


def goldmine(g):
    """The trade grade a level maps onto, or "" if the level is absent."""
    return GOLDMINE.get(g, "")


def grade_ok(g):
    return g in GRADE_MEDIA


# The grade bands a source's own condition string is allowed to collapse onto.
# Discogs writes "Very Good Plus (VG+)"; a listing may say "VG+/VG" meaning the
# record is VG+ and the sleeve VG. Only the record's grade is read here, because
# that is the half the price is quoted against.
_COND_PATTERNS = [
    # Near Mint before Mint, and Very Good Plus before Very Good: every one of
    # these names contains a shorter one, so the order IS the rule. Matching
    # "Very Good" inside "Very Good Plus" silently marks a copy down a grade.
    (r"near\s*mint|\bNM\b|\bM-", 5),
    (r"\bmint\b|^\s*M\s*$", 5),
    (r"very\s*good\s*plus|\bVG\+", 4),
    (r"very\s*good|\bVG\b", 3),
    (r"good\s*plus|\bG\+", 2),
    (r"\bgood\b|\bG\b", 2),
    (r"\bfair\b|\bF\b|\bpoor\b|\bP\b", 1),
]


def band_from_condition(text):
    """Read a source's condition string onto the 1-5 scale, or None.

    Ordered longest-first: "Very Good Plus" must not be read as "Very Good", and
    "Near Mint" must not be read as "Mint". Returns None rather than guessing —
    an unreadable condition is compared against nothing, which is the whole
    point of keeping the band beside the price.
    """
    t = (text or "").strip()
    if not t:
        return None
    head = t.split("/")[0]                      # "VG+/VG" — the record's half
    for rx, band in _COND_PATTERNS:
        if re.search(rx, head, re.I):
            return band
    return None


# ------------------------------------------------------------------ formats
#
# What the object physically is. Kept coarse on purpose: the fine detail that
# actually separates two editions (180g, gatefold, numbered, coloured) is prose
# that belongs in a release's `edition_note`, where the source's own words are
# preserved rather than squeezed into a dropdown.
FORMATS = {
    "LP": 'Album, 12 inch',
    "2xLP": 'Double album, 12 inch',
    "3xLP": 'Triple album or more',
    "12in": 'Single or EP, 12 inch',
    "10in": '10 inch',
    "7in": '7 inch single',
    "box": 'Box set',
    "other": 'Something else',
    "unknown": 'Not stated',
}

SPEEDS = {"33": "33⅓ rpm", "45": "45 rpm", "78": "78 rpm", "unknown": "Not stated"}


def format_ok(f):
    return f in FORMATS


# ------------------------------------------------------------------- prices
#
# Every stored amount says which of these it is. The second half of each
# sentence is the part that keeps the app honest: it says what the number is
# NOT, and it is printed next to the number on every page that shows one.
PRICE_KINDS = {
    "lowest_asking": (
        "Lowest asking price",
        "What the cheapest copy currently for sale is being asked for. It is not a "
        "sale, not an appraisal, and not necessarily for a copy in your condition."),
    "asking_seen": (
        "An asking price you saw",
        "A price someone was asking, recorded by hand. Not a sale."),
    "sale_seen": (
        "A sale you saw",
        "A price a copy actually changed hands for, recorded by hand."),
    "paid": (
        "What you paid",
        "The price on your own receipt. One sale, of one copy, on one day."),
    "sold": (
        "What you sold it for",
        "The price you got for one copy of your own, on one day. One sale, not a "
        "market price."),
    "suggested": (
        "A source's suggested price",
        "A figure a source computed for a given condition. An estimate, and the "
        "method behind it is not visible here."),
}

# Kinds that describe the market rather than the owner's own history. A price
# series is built from one kind at a time, and `paid` and `sold` are never part
# of one: a purchase or a sale of your own copy is a single event, not an
# observation of a market.
MARKET_KINDS = ("lowest_asking", "asking_seen", "sale_seen", "suggested")

# Kinds that belong to one copy rather than to the edition, recorded on the copy's
# own page and never through the record's "price you saw" form.
COPY_KINDS = ("paid", "sold")

# Every currency a stored price can be in, with the symbol it is printed with.
# The US dollar is "US$" and not "$" because Canadian dollars are on offer beside
# it, and to somebody in Canada a bare "$" means theirs.
CURRENCIES = {
    "GBP": "£", "USD": "US$", "EUR": "€", "JPY": "¥", "AUD": "A$", "CAD": "C$",
    "CHF": "CHF ", "SEK": "SEK ", "NZD": "NZ$",
}

# The currencies the collection can ask for prices in and type them in. A price
# already stored in another one — a source that answered in its own — still
# prints with its own symbol; it is just never offered as a choice.
CURRENCY_CHOICES = ("CAD", "GBP", "USD")


def currency_ok(cur):
    return cur in CURRENCIES


def currency_choices(keep=""):
    """The currencies a form offers: the three, plus `keep` if a stored price is
    already in another, so editing a receipt in euros cannot quietly relabel it
    in pounds by leaving the box on its first option."""
    out = list(CURRENCY_CHOICES)
    if keep and keep not in out and currency_ok(keep):
        out.append(keep)
    return [(k, k) for k in out]


def money(amount, currency="GBP", dp=2):
    """Format an amount with its own currency's symbol.

    There is no conversion anywhere in this app, and this function is the reason
    it is easy to keep that promise: an amount always carries the currency it was
    quoted in, so two currencies cannot be added together by accident.
    """
    if amount is None:
        return ""
    sym = CURRENCIES.get(currency, currency + " ")
    return f"{sym}{amount:,.{dp}f}"


def pct(a, b):
    """The change from a to b as a percentage of a, or None if that is meaningless.

    A move from zero, or from nothing, has no percentage. Saying "up 100%" when
    the old price was 0.00 is the kind of number that starts an argument.
    """
    if a is None or b is None:
        return None
    if a == 0:
        return None
    return (b - a) / abs(a) * 100.0


# ------------------------------------------------------------------ sources
#
# Where a fact came from. Stored beside every fact that came from outside, so a
# page can always say who said it.
SOURCES = {
    "musicbrainz": "MusicBrainz",
    "discogs": "Discogs",
    "manual": "Entered by hand",
    "scan": "Read off the barcode",
}

# How a release's identity was settled. This is provenance, and it is the
# difference between "the barcode matched exactly one edition" and "I picked the
# one that looked right", which are not the same level of confidence.
IDENTIFIED = {
    "sole_match": "One edition matched the barcode",
    "chosen": "Chosen from several editions that share the barcode",
    "manual": "Typed in by hand",
    "unmatched": "The barcode matched nothing — entered by hand",
}

# The same two values, for a record that came in through a search by name rather
# than a scan: "chosen" and "unmatched" say how, but not from what.
IDENTIFIED_BY_NAME = {
    "chosen": "Chosen from the editions a search by name found",
    "unmatched": "A search by name found nothing that fitted — entered by hand",
}


# ------------------------------------------------------------------- basics

def norm(s, limit=400):
    """Collapse whitespace and cap length. For names and short fields only.

    Prose fields are never passed through this — a note keeps its line breaks
    and its length, exactly as it was typed.
    """
    t = re.sub(r"\s+", " ", (s or "")).strip()
    return t[:limit]


def numn(s):
    """Parse to float, or None. Rejects NaN and Inf so they cannot poison a sum."""
    if s is None:
        return None
    t = str(s).strip().replace(",", "")
    if not t:
        return None
    try:
        f = float(t)
    except ValueError:
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def intn(s):
    f = numn(s)
    return None if f is None else int(round(f))


def discogs_release_ref(s):
    """A Discogs release number from what a person pastes. (number, problem).

    People paste the address of the page rather than the number in it —
    /release/1234567-Miles-Davis-Kind-Of-Blue, the older /Miles-Davis/release/
    1234567, Discogs' own [r1234567] — and all of those mean one number. A box
    that does not read as one is a problem to say out loud, never a blank: a
    blank is how a link is removed, and an unreadable paste must not remove the
    link it was meant to set. A master is refused by name, because it is every
    edition of an album at once and a price is about one."""
    raw = norm(s, 500).strip("[]() ")
    if not raw:
        return None, ""
    m = re.fullmatch(r"[rR]?\s*(\d{1,10})", raw)
    if m:
        num = int(m.group(1))
    else:
        u = re.search(r"/(?:sell/)?(release|master)s?/(\d{1,10})(?!\d)", raw, re.I)
        if u and u.group(1).lower() == "master":
            return None, ("That is a Discogs master — every edition of the album at once. "
                          "Open the edition you own on Discogs and use that release's "
                          "number instead.")
        num = int(u.group(2)) if u else 0
    if num <= 0:
        return None, ("That did not read as a Discogs release number. Use the number, or "
                      "the address of the release's page on Discogs.")
    return num, ""


def moneyn(s):
    """Parse a money amount. Strips a leading symbol, refuses the negative.

    A negative price is not a price. It is almost always a minus typed by
    accident, and storing it would put a nonsense point in a series.
    """
    if s is None:
        return None
    t = str(s).strip()
    for sym in ("US$", "CA$", "NZ$", "A$", "C$", "£", "$", "€", "¥"):
        if t.startswith(sym):
            t = t[len(sym):]
            break
    f = numn(t)
    if f is None or f < 0:
        return None
    return round(f, 2)


def datestr(s):
    """Keep an ISO date, drop anything else. A half-parsed date is worse than none."""
    t = (s or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", t):
        try:
            datetime.date.fromisoformat(t)
            return t
        except ValueError:
            return None
    return None


def partial_date(s):
    """Keep a date that may be a year, a month or a day: 1959, 1959-08, 1959-08-17.

    Release dates come this way and rounding one up to a full date invents a
    precision the source never claimed. Stored as the source stated it.
    """
    t = (s or "").strip()
    if re.fullmatch(r"\d{4}", t):
        return t
    if re.fullmatch(r"\d{4}-\d{2}", t):
        return t if 1 <= int(t[5:7]) <= 12 else None
    return datestr(t)


def year_of(s):
    """The year out of a partial date, or None. Never stored — derived at read time."""
    t = (s or "").strip()
    return int(t[:4]) if re.match(r"\d{4}", t) else None


def now_iso():
    """UTC, to the second, with the offset spelled out.

    Every timestamp in the database is UTC. A price series that silently mixes
    local winter and summer time has an hour-wide hole in it twice a year.
    """
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def today_iso():
    return datetime.datetime.now(datetime.timezone.utc).date().isoformat()


def parse_iso(s):
    """A stored timestamp back to an aware datetime, or None if it will not parse."""
    if not s:
        return None
    try:
        d = datetime.datetime.fromisoformat(str(s))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=datetime.timezone.utc)


def days_between(a, b):
    """Whole days from a to b, or None. Both are ISO strings."""
    da, dbb = parse_iso(a), parse_iso(b)
    if da is None or dbb is None:
        return None
    return (dbb - da).days


def ago(stamp, now=None):
    """"3 days ago", in words, for a stored timestamp. Display only."""
    d = parse_iso(stamp)
    if d is None:
        return ""
    ref = parse_iso(now) if now else datetime.datetime.now(datetime.timezone.utc)
    secs = (ref - d).total_seconds()
    if secs < 0:
        return "just now"
    for limit, div, word in ((90, 1, "second"), (5400, 60, "minute"),
                             (172800, 3600, "hour"), (5184000, 86400, "day")):
        if secs < limit:
            n = int(round(secs / div))
            return f"{n} {word}{'s' if n != 1 else ''} ago"
    n = int(round(secs / 2592000))
    return f"{n} month{'s' if n != 1 else ''} ago"
