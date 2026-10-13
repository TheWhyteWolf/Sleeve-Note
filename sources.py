"""The public sources: MusicBrainz and Discogs. Stdlib only, one door out.

This module is the *only* place in Sleevenote that touches the network, and
`_http_get` is the only function in it that opens a socket. Everything else is
parsing. That is what makes the test suite able to run the whole app, including
the poller and the alert rules, against the recorded answers in `fixtures/`
without a network connection — and what makes it possible to say, precisely, what
this app sends and to whom.

**What leaves this machine.** A barcode, a release id, an artist id, an artist
name you typed, the artist, title or catalogue number you typed to look up a
record with no barcode, and a User-Agent. That is the list. The collection is never sent
anywhere. Nothing is uploaded, no account is needed, and no cover art is ever
fetched — a page shows a *link* to a cover, never an `<img>` pointing at a
third party, because an embedded image would tell that third party which of your
records you were looking at and when.

**Neither source needs a token.** Both work unauthenticated, which is why the app
is useful the moment it starts. A Discogs token is optional and buys one thing: a
rate limit of 60 requests a minute instead of 25.

**Read, never interpret.** Every adapter below turns a source's answer into the
app's own vocabulary and stops there. Where a source's field is ambiguous the
ambiguity is carried forward rather than resolved by guessing — see
`_pick_barcode`, which is the sharpest example in the file.

Two things the sources got wrong under measurement, both now designed around:

  * Discogs reports the lowest asking price for one release, in one currency,
    through two endpoints that disagree (£34.40 against 51.43, seconds apart —
    the fixtures hold both). Only `marketplace/stats` is used for prices, because
    it states the currency of its own number instead of leaving you to assume the
    one you asked for. `price_obs.feed` records which endpoint anyway.

  * MusicBrainz's `format:` search field matches a whole format name and not a
    word inside one. `format:Vinyl` finds 18 Miles Davis releases; `format:"12\"
    Vinyl"` finds 337. A query built the obvious way under-counts vinyl by a
    factor of twenty, so this module does not filter by format in a query at all.
    Watches follow *works* (MusicBrainz release groups) and ask about formats
    afterwards, per new work — which also answers a better question: not only
    "is there a new record" but "is there a vinyl pressing of it yet".
"""
from __future__ import annotations
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import db
import model

VERSION = "1.0"
MB_HOST = "musicbrainz.org"
DG_HOST = "api.discogs.com"

# Seconds between calls, and calls per minute. Both sources publish a limit and
# both enforce it — MusicBrainz answered 503 during development the moment a
# handful of requests went out back to back, which is exactly what it says it
# will do. The per-minute figures leave headroom on purpose.
LIMITS = {
    MB_HOST: (1.1, 45),
    DG_HOST: (1.0, 22),          # 25/min unauthenticated
}
LIMITS_TOKEN = {DG_HOST: (0.6, 50)}     # 60/min with a token

TIMEOUT = 12
# The longest this module will ever block a page load waiting for a rate limit to
# clear. Past this it declines and says when to try again: a person watching a
# spinner is worse served than a person told to press the button in a minute.
MAX_WAIT = 4.0

# How recent a release date has to be for a newly-seen record to be called new
# rather than newly catalogued. Eighteen months is generous, and the distinction
# is never blurred — see alerts.classify_new.
RECENT_DAYS = 548

# Replaced by tests so the rate limiter can be exercised without waiting.
SLEEP = time.sleep


def user_agent(c=None):
    """The User-Agent both sources ask for.

    MusicBrainz asks that an application identify itself and offer a way to be
    contacted. `user_agent_contact` is a setting the owner fills in if they want
    to; it is blank by default and this app will never put an address in here
    that the owner did not type into that box themselves.
    """
    contact = model.norm(db.get(c, "user_agent_contact", "") if c else "", 120)
    tail = f" ( {contact} )" if contact else ""
    return f"Sleevenote/{VERSION}{tail}"


# --------------------------------------------------------------- the one door

def _http_get(url, headers, timeout=TIMEOUT):
    """The only socket in the application. (status, body bytes, headers).

    Every test replaces this attribute on the module, which is why nothing else
    here is allowed to call urllib directly — `tests.py:TestOneDoorOut` greps
    this file to check that stays true, and points this at a sentinel that fails
    the run if anything reaches it.
    """
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as ex:
        body = b""
        try:
            body = ex.read()
        except Exception:
            pass
        return ex.code, body, dict(ex.headers or {})


# ------------------------------------------------------------- rate limiting

def _limits(c, host):
    if host == DG_HOST and model.norm(db.get(c, "discogs_token", ""), 200):
        return LIMITS_TOKEN[DG_HOST]
    return LIMITS.get(host, (1.0, 20))


def wait_needed(c, host, now=None):
    """Seconds to wait before calling `host`, from what is already recorded.

    Measured from `source_call` rather than from memory, so stopping and starting
    the app cannot be used to burst past a published limit — the floor survives a
    restart because it is in the database, which is where the evidence of the
    last call already was.
    """
    gap, per_min = _limits(c, host)
    now = model.parse_iso(now) if now else None
    if now is None:
        now = model.parse_iso(model.now_iso())
    rows = c.execute("""SELECT called_at FROM source_call WHERE host=?
                        ORDER BY id DESC LIMIT ?""", (host, per_min)).fetchall()
    if not rows:
        return 0.0
    waits = [0.0]
    last = model.parse_iso(rows[0]["called_at"])
    if last:
        waits.append(gap - (now - last).total_seconds())
    if len(rows) >= per_min:
        oldest = model.parse_iso(rows[-1]["called_at"])
        if oldest:
            waits.append(60.0 - (now - oldest).total_seconds())
    return max(0.0, max(waits))


def _record(c, host, path, status, ms, note=""):
    c.execute("""INSERT INTO source_call(called_at,host,path,status,ms,note)
                 VALUES(?,?,?,?,?,?)""", (model.now_iso(), host, path, status, ms, note))
    db.prune_calls(c)
    c.commit()


def _call(c, host, path, query=None):
    """One guarded request. Returns (data, problem) — problem is "" when fine.

    Every refusal is a sentence a person can act on, never a traceback and never
    a silent empty result: "no editions matched that barcode" and "Discogs did
    not answer" are different facts and the scan page says which one happened.
    """
    if db.get_bool(c, "offline"):
        return None, ("Offline mode is on, so nothing was asked of the internet. "
                      "Turn it off in Settings to look things up.")
    wait = wait_needed(c, host)
    if wait > MAX_WAIT:
        return None, (f"{host} has been asked enough for one minute. "
                      f"Try again in about {int(wait) + 1} seconds.")
    if wait > 0:
        SLEEP(wait)
    url = f"https://{host}{path}"
    if query:
        url += "?" + urllib.parse.urlencode(query)
    headers = {"User-Agent": user_agent(c), "Accept": "application/json"}
    if host == DG_HOST:
        token = model.norm(db.get(c, "discogs_token", ""), 200)
        if token:
            headers["Authorization"] = f"Discogs token={token}"
    t0 = time.monotonic()
    try:
        status, body, resp = _http_get(url, headers, TIMEOUT)
    except Exception as ex:
        _record(c, host, path, 0, int((time.monotonic() - t0) * 1000), type(ex).__name__)
        return None, f"Could not reach {host} ({type(ex).__name__})."
    ms = int((time.monotonic() - t0) * 1000)
    _record(c, host, path, status, ms)
    if status in (429, 503):
        retry = (resp or {}).get("Retry-After", "")
        extra = f" It asked for {retry} seconds." if str(retry).strip() else ""
        return None, f"{host} is asking us to slow down.{extra} Nothing was recorded."
    if status == 401:
        return None, (f"{host} refused the request as unauthorised. If you have set a "
                      f"Discogs token, check it; the app works without one.")
    if status == 404:
        return None, ""
    if status != 200:
        return None, f"{host} answered {status}."
    try:
        return json.loads(body.decode("utf-8", "replace")), ""
    except (ValueError, AttributeError):
        return None, f"{host} sent something that was not readable as JSON."


# ------------------------------------------------------------------- formats

_VINYL_WORDS = ("vinyl", "shellac", "flexi", "lathe", "acetate", "picture disc")
_NOT_VINYL = ("cd", "cdr", "dvd", "file", "cassette", "sacd", "blu-ray", "8-track",
              "minidisc", "reel", "digital")


def read_format(words, qty=None, name=""):
    """A source's format words to (format, speed, media_kind, as_written).

    `media_kind` is 'vinyl', 'other' or 'unknown', and the third value is a real
    answer rather than a polite way of saying no: a source that does not state a
    format gets 'unknown', and the scan page says "this source did not say what
    it is" instead of filing a CD in a vinyl collection or refusing a record
    because a database was incomplete.

    The disc count decides LP against 2xLP, and a count that was never stated
    leaves the format at LP with the count absent rather than asserting one.
    """
    bits = [str(w).strip() for w in (words or []) if str(w).strip()]
    if name:
        bits.insert(0, str(name).strip())
    low = " ".join(bits).lower()
    kind = "unknown"
    if any(w in low for w in _VINYL_WORDS):
        kind = "vinyl"
    elif any(f" {w} " in f" {low} " or low == w for w in _NOT_VINYL):
        kind = "other"
    n = model.intn(qty) if qty not in (None, "") else None
    fmt = "unknown"
    if '7"' in low or "7 inch" in low:
        fmt = "7in"
    elif '10"' in low or "10 inch" in low:
        fmt = "10in"
    elif "box" in low:
        fmt = "box"
    elif "lp" in low or "album" in low:
        fmt = "LP"
    elif '12"' in low or "12 inch" in low or "maxi" in low or "ep" in low:
        fmt = "12in"
    if fmt in ("LP", "12in") and n and n >= 2:
        fmt = "2xLP" if n == 2 else "3xLP"
    speed = "unknown"
    if "45 rpm" in low or "45rpm" in low:
        speed = "45"
    elif "78 rpm" in low:
        speed = "78"
    elif "33" in low:
        speed = "33"
    return fmt, speed, kind, ", ".join(bits)


# ------------------------------------------------------------------ barcodes

def _pick_barcode(field, wanted):
    """The barcode out of a Discogs `barcode` field, matched against what we scanned.

    This function is why the app does not file records under nonsense. Discogs'
    `barcode` is a list of every identifier on the sleeve, in no order and with
    no types: for one of the two editions in this repo's fixtures it holds twenty
    entries — '0 886976 389413', '886976389413', 'BIEM/GEMA', 'BIEM/STEMRA' and
    sixteen matrix numbers stamped in the run-out groove. Taking the first entry
    is wrong on that release and would store a rights-society name as a barcode
    on plenty of others.

    So each entry is normalised to thirteen digits and compared to the code that
    was actually scanned. A match is returned; nothing else is. Note that
    '886976389413' — twelve digits, the leading zero dropped — normalises to the
    same thirteen and matches, which is the other half of why this is a
    comparison and not a lookup.
    """
    want = model.norm(wanted or "", 20)
    for entry in (field if isinstance(field, list) else [field]):
        code = __import__("barcode").normalise(str(entry or ""))
        if code and (code == want or not want):
            return code
    return ""


def _dg_candidate(r, code):
    """One Discogs search result as a candidate edition."""
    fmts = r.get("formats") or []
    fq = fmts[0] if fmts else {}
    descr = list(fq.get("descriptions") or [])
    text = model.norm(fq.get("text") or "", 200)
    fmt, speed, kind, written = read_format(
        descr + ([text] if text else []),
        fq.get("qty") or r.get("format_quantity"), fq.get("name") or "")
    labels = r.get("label") or []
    rid = model.intn(r.get("id"))
    title = model.norm(r.get("title") or "", 300)
    artist, _, rest = title.partition(" - ")
    return {
        "source": "discogs",
        "source_id": str(rid or ""),
        # Discogs writes a search result's title as "Artist - Title". The split is
        # kept AND the string it came from is kept, because a title with a dash of
        # its own would be split in the wrong place and the raw form is then the
        # only way anybody could tell.
        "title_raw": title,
        "title": model.norm(rest or title, 300),
        "artist_credit": model.norm(artist if rest else "", 300),
        "artist_mbid": "", "artist_sort": "",
        "released": model.partial_date(str(r.get("year") or "")) or "",
        "country": model.norm(r.get("country") or "", 80),
        "label": model.norm(labels[0] if labels else "", 160),
        "catno": model.norm(r.get("catno") or "", 80),
        "format": fmt, "speed": speed, "media_kind": kind,
        "format_words": written,
        "discs": model.intn(fq.get("qty") or r.get("format_quantity")),
        "edition_note": ", ".join([w for w in descr if w] + ([text] if text else [])),
        "disambiguation": "",
        "barcode": _pick_barcode(r.get("barcode"), code),
        "mbid": "",
        "discogs_release_id": rid,
        "discogs_master_id": model.intn(r.get("master_id")),
        "cover_url": model.norm(r.get("cover_image") or r.get("thumb") or "", 500),
        "url": f"https://www.discogs.com{r.get('uri') or ''}" if r.get("uri") else "",
        "tracks": None,
    }


def _mb_candidate(r, code):
    """One MusicBrainz search result as a candidate edition."""
    media = r.get("media") or []
    names = [m.get("format") for m in media if m.get("format")]
    fmt, speed, kind, written = read_format(names, len(media) if media else None)
    li = (r.get("label-info") or [{}])[0] or {}
    ac = r.get("artist-credit") or []
    credit = "".join((a.get("name") or "") + (a.get("joinphrase") or "") for a in ac)
    first = ((ac[0] or {}).get("artist") or {}) if ac else {}
    tracks = sum(model.intn(m.get("track-count")) or 0 for m in media) or None
    return {
        "source": "musicbrainz",
        "source_id": str(r.get("id") or ""),
        "title_raw": model.norm(r.get("title") or "", 300),
        "title": model.norm(r.get("title") or "", 300),
        "artist_credit": model.norm(credit, 300),
        "released": model.partial_date(str(r.get("date") or "")) or "",
        "country": model.norm(r.get("country") or "", 80),
        "label": model.norm(((li.get("label") or {}) or {}).get("name") or "", 160),
        "catno": model.norm(li.get("catalog-number") or "", 80),
        "format": fmt, "speed": speed, "media_kind": kind,
        "format_words": written,
        "discs": len(media) or None,
        "edition_note": model.norm(r.get("packaging") or "", 200),
        "disambiguation": model.norm(r.get("disambiguation") or "", 300),
        "barcode": __import__("barcode").normalise(str(r.get("barcode") or "")) or "",
        "mbid": str(r.get("id") or ""),
        "artist_mbid": str(first.get("id") or ""),
        "artist_sort": model.norm(first.get("sort-name") or "", 200),
        "discogs_release_id": None,
        "discogs_master_id": None,
        "cover_url": "",
        "url": f"https://musicbrainz.org/release/{r.get('id')}" if r.get("id") else "",
        "tracks": tracks,
        "status": model.norm(r.get("status") or "", 40),
    }


def lookup_barcode(c, code, unanswered=None):
    """Every edition either source knows of with this barcode. (candidates, notes).

    Both sources are asked, and both answers are kept side by side rather than
    merged. Merging would mean deciding that a MusicBrainz release and a Discogs
    release are the same edition, and the only honest ground for that decision is
    a link one of them has published — which is what `fuller` follows, through
    `mb_release`, when somebody actually picks one.

    `notes` carries anything the person should know: a source that would not
    answer, a rate limit, a barcode that is registered to a book. An empty
    candidate list with an empty note means both sources answered and neither had
    heard of it, which is a real answer and a common one for a small pressing.

    Pass a list as `unanswered` and the host of every source that did not answer
    is appended to it, so the caller can tell "nothing matched" from "nobody was
    asked" without reading the sentences.
    """
    unanswered = [] if unanswered is None else unanswered
    import barcode as bc
    notes, out = [], []
    code = bc.normalise(code)
    if not code:
        return [], ["That is not a barcode."]
    if not bc.check_ok(code):
        return [], [bc.fix_hint(code)]
    reg = bc.registry(code)
    if reg and ("ISBN" in reg or "ISSN" in reg or "coupon" in reg or "not a real" in reg):
        notes.append(f"That number is {reg} — it is a valid barcode, but not a product code "
                     f"for a record. Worth checking you scanned the right one.")
    d, err = _call(c, MB_HOST, "/ws/2/release",
                   {"query": f"barcode:{code}", "fmt": "json", "limit": "25"})
    if err:
        notes.append(err)
        unanswered.append(MB_HOST)
    for r in ((d or {}).get("releases") or []):
        out.append(_mb_candidate(r, code))
    d2, err2 = _call(c, DG_HOST, "/database/search", {"barcode": code, "type": "release"})
    if err2:
        notes.append(err2)
        unanswered.append(DG_HOST)
    for r in ((d2 or {}).get("results") or []):
        out.append(_dg_candidate(r, code))
    for cand in out:
        if not cand["barcode"]:
            cand["barcode"] = code
    return out, notes


# ------------------------------------------------------- looking up by name

# How many editions a name search asks each source for. Enough to find a record
# whose title is all anybody typed when it has only a few pressings; for one
# with hundreds, the page says how many there are and asks for the catalogue
# number rather than offering four pages of near-identical cards.
SEARCH_LIMIT = 25
# With a catalogue number the search is already narrow, and what is left is every
# pressing under that one number — measured: CL 1355 is 44 vinyl editions on
# Discogs, told apart only by year, country and pressing plant. All of them are
# worth showing, because the owner chooses by reading those, so the cap rises.
SEARCH_LIMIT_CATNO = 100

# MusicBrainz's vinyl formats by name. `format:` matches a whole name (see the
# module docstring), so all four are spelled out. Measured on Kind of Blue: 137
# releases unfiltered, 62 of them vinyl, and this filter returns exactly those 62.
MB_VINYL = ('Vinyl', '12" Vinyl', '7" Vinyl', '10" Vinyl')

_LUCENE_SPECIAL = set('+-&|!(){}[]^"~*?:\\/')


def _lucene_words(text):
    """Typed words as Lucene terms that must all appear, or "" if none survive.

    Lower-cased so a typed AND, OR or NOT is a word and not an operator, every
    character Lucene treats as syntax escaped, and a "word" with no letter or
    digit in it dropped — "Simon & Garfunkel" is two words, not three. All the
    words rather than the phrase, so "kind blue" still finds Kind of Blue and a
    title typed in a different order still matches.
    """
    out = []
    for w in model.norm(text or "", 300).lower().split():
        if not any(ch.isalnum() for ch in w):
            continue
        out.append("".join("\\" + ch if ch in _LUCENE_SPECIAL else ch for ch in w))
    return " AND ".join(out)


def _catno_key(text):
    """A catalogue number as MusicBrainz indexes it: letters and digits only.

    Measured: `catno:CL1355` finds CL 1355 and C.L.1355 alike, and the quoted
    `catno:"CL1355"` finds nothing, so the spaces and dots are taken out here and
    the term is sent bare. Discogs gets the number as it was typed; it finds
    both spellings either way.
    """
    return "".join(ch for ch in model.norm(text or "", 80).lower() if ch.isalnum())


def mb_title_query(artist="", title="", catno=""):
    """The MusicBrainz query for a name search, vinyl only. "" if nothing to ask."""
    parts = []
    t, a, k = _lucene_words(title), _lucene_words(artist), _catno_key(catno)
    if t:
        parts.append(f"release:({t})")
    if a:
        parts.append(f"artist:({a})")
    if k:
        parts.append(f"catno:{k}")
    if not (t or k):
        return ""
    fmts = " OR ".join(f'"{f}"' if " " in f else f
                       for f in (x.replace('"', '\\"') for x in MB_VINYL))
    return " AND ".join(parts) + f" AND format:({fmts})"


def lookup_title(c, artist="", title="", catno="", unanswered=None, totals=None):
    """Vinyl editions either source lists under a name. (candidates, notes).

    The way in for a record with no barcode. A title or a catalogue number is
    needed — an artist alone is a discography, not an edition — and both sources
    are asked for vinyl only, Discogs with its own `format=Vinyl` and MusicBrainz
    with the four format names it uses.

    What differs from a barcode lookup is what a candidate may claim. A barcode
    match is checked against the code that was scanned; here there is nothing to
    check against. So a MusicBrainz barcode, which is a field of its own, is kept
    only when it passes its check digit, and Discogs' search-result `barcode` is
    never used at all: on an old record it holds whatever is printed on the
    sleeve — measured on Kind of Blue, 'BMI', '2' and run-out stamps. If the
    edition is chosen, `fuller` reads the full entry, where barcodes are typed.

    Pass a list as `unanswered` for the hosts that did not answer, and a dict as
    `totals` for how many editions each source said it lists, by source name.
    """
    import barcode as bc
    unanswered = [] if unanswered is None else unanswered
    totals = {} if totals is None else totals
    artist, title, catno = (model.norm(artist or "", 200), model.norm(title or "", 300),
                            model.norm(catno or "", 80))
    q = mb_title_query(artist, title, catno)
    if not q:
        return [], ["A title or a catalogue number is needed — an artist on their own is "
                    "every record they ever made."]
    notes, out = [], []
    limit = str(SEARCH_LIMIT_CATNO if _catno_key(catno) else SEARCH_LIMIT)
    d, err = _call(c, MB_HOST, "/ws/2/release", {"query": q, "fmt": "json", "limit": limit})
    if err:
        notes.append(err)
        unanswered.append(MB_HOST)
    elif d is not None:
        totals["musicbrainz"] = model.intn(d.get("count"))
    for r in ((d or {}).get("releases") or []):
        cand = _mb_candidate(r, "")
        if cand["barcode"] and not bc.check_ok(cand["barcode"]):
            cand["barcode"] = ""
        out.append(cand)
    query = {"type": "release", "format": "Vinyl", "per_page": limit}
    for key, val in (("artist", artist), ("release_title", title), ("catno", catno)):
        if val:
            query[key] = val
    d2, err2 = _call(c, DG_HOST, "/database/search", query)
    if err2:
        notes.append(err2)
        unanswered.append(DG_HOST)
    elif d2 is not None:
        totals["discogs"] = model.intn((d2.get("pagination") or {}).get("items"))
    for r in ((d2 or {}).get("results") or []):
        cand = _dg_candidate(r, "")
        cand["barcode"] = ""
        out.append(cand)
    return out, notes


DIFF_FIELDS = (("released", "year"), ("country", "country"), ("label", "label"),
               ("catno", "catalogue number"), ("format", "format"),
               ("edition_note", "pressing"), ("title", "title"),
               ("disambiguation", "note"))


def differs(cands):
    """Which fields actually vary across a set of candidates, in words.

    This is what turns "here are four things, good luck" into "these four differ
    only in country and catalogue number". It is the single most useful thing the
    choosing screen does, because the reason two editions share a barcode is
    almost always that they are nearly the same object.

    Only ever call this on candidates from ONE source — `differs_by_source` is the
    one the pages use. Comparing across sources measures the wrong thing: the
    Kind of Blue example in the fixtures has MusicBrainz saying 2010-05 in NL and
    Discogs saying 2010 in Europe about what is very likely the same record, and
    calling that "these editions differ in year and country" would be describing
    two catalogues rather than two records.
    """
    if len(cands) < 2:
        return []
    out = []
    for key, label in DIFF_FIELDS:
        seen = {str(cd.get(key) or "").strip().lower() for cd in cands}
        if len(seen) > 1:
            out.append(label)
    return out


def by_source(cands):
    """Candidates grouped by the source that offered them, in a stable order."""
    groups = {}
    for cd in cands:
        groups.setdefault(cd.get("source") or "", []).append(cd)
    return [(src, groups[src]) for src in ("musicbrainz", "discogs") if src in groups]


def differs_by_source(cands):
    """{source: [field labels that vary within that source's own candidates]}."""
    return {src: differs(group) for src, group in by_source(cands)}


# --------------------------------------------------------- one release, fully

def discogs_release(c, rid):
    """A Discogs release in full: (candidate dict with extras, problem).

    Worth one call after somebody has chosen an edition, because this is where the
    matrix and run-out numbers live — the stamps in the dead wax that are the only
    way to tell two otherwise identical pressings apart, and the thing a serious
    collector actually writes down.
    """
    d, err = _call(c, DG_HOST, f"/releases/{int(rid)}",
                   {"curr_abbr": db.get(c, "currency", "GBP")})
    if err or not d:
        return None, err or "Discogs had nothing for that release."
    cand = _dg_candidate({
        "id": d.get("id"), "title": d.get("title"), "year": d.get("released") or d.get("year"),
        "country": d.get("country"), "catno": (d.get("labels") or [{}])[0].get("catno"),
        "label": [(l or {}).get("name") for l in (d.get("labels") or [])],
        "formats": d.get("formats"), "master_id": d.get("master_id"),
        "barcode": [i.get("value") for i in (d.get("identifiers") or [])
                    if (i.get("type") or "").lower() == "barcode"],
        "uri": (d.get("uri") or "").replace("https://www.discogs.com", ""),
        "cover_image": "", "thumb": "",
    }, "")
    cand["title"] = model.norm(d.get("title") or "", 300)
    cand["artist_credit"] = _dg_credit(d.get("artists"))
    cand["released"] = model.partial_date(_dg_date(d.get("released"))) or ""
    matrix = [f"{model.norm(i.get('description') or '', 80)}: {model.norm(i.get('value') or '', 200)}".strip(": ")
              for i in (d.get("identifiers") or [])
              if (i.get("type") or "").lower().startswith("matrix")]
    cand["matrix"] = matrix
    cand["notes_from_source"] = model.norm(d.get("notes") or "", 2000)
    return cand, ""


def _dg_credit(artists):
    """A Discogs artist list as the credit printed on the sleeve.

    Each entry carries its own joining word ("&", "Feat.", ","), so two artists
    are joined the way the release joins them rather than with a comma that
    Discogs never wrote. The name as printed (`anv`) wins over the canonical one,
    and the "(2)" Discogs appends to tell two people of one name apart is
    dropped: it is Discogs' bookkeeping, not part of anybody's name.
    """
    out = ""
    for a in (artists or []):
        name = model.norm(a.get("anv") or a.get("name") or "", 120)
        name = re.sub(r"\s\(\d+\)$", "", name)
        join = model.norm(a.get("join") or "", 20)
        out += name + ("" if not join else (", " if join == "," else f" {join} "))
    return model.norm(out.strip(" ,"), 300)


def _dg_date(raw):
    """A Discogs date to a partial date. '2010-05-00' means May 2010, not the 0th.

    Discogs pads an unknown day or month with zeroes. Passing that string to a
    date parser gets you either an exception or, worse, the last day of April.
    """
    t = model.norm(raw or "", 20)
    if t.endswith("-00-00"):
        return t[:4]
    if t.endswith("-00"):
        return t[:7]
    return t


def mb_release(c, mbid):
    """A MusicBrainz release in full, including the Discogs link if it has one."""
    d, err = _call(c, MB_HOST, f"/ws/2/release/{urllib.parse.quote(str(mbid))}",
                   {"fmt": "json",
                    "inc": "url-rels+labels+media+artist-credits+release-groups"})
    if err or not d:
        return None, err or "MusicBrainz had nothing for that release."
    cand = _mb_candidate(d, "")
    cand["discogs_release_id"] = _discogs_id_from_rels(d.get("relations"))
    return cand, ""


def _discogs_id_from_rels(rels):
    """The Discogs release id out of MusicBrainz url relationships, or None.

    This is the bridge that lets price tracking work for a record identified
    through MusicBrainz: MusicBrainz knows the edition, Discogs knows what people
    are asking for it, and a link published by MusicBrainz between the two is the
    only honest reason to treat them as the same object.
    """
    for rel in (rels or []):
        res = ((rel.get("url") or {}).get("resource") or "")
        if "discogs.com/release/" in res:
            tail = res.rstrip("/").split("/release/")[-1].split("-")[0].split("?")[0]
            n = model.intn(tail)
            if n:
                return n
    return None


def refine_date(have, said):
    """The more precise of two dates, but only if they agree. Never a conflict won.

    '2010' and '2010-05' are the same claim at two precisions, so the finer one
    is kept. '2010-05' and '2010-06' are two different claims, and the one already
    held stands — a second answer is not a better one just because it came later.
    """
    have, said = (have or "").strip(), (said or "").strip()
    if said and (not have or (said.startswith(have) and len(said) > len(have))):
        return said
    return have


def fuller(c, cand):
    """A chosen candidate completed from its source's full record. (cand, note).

    One request, made after somebody has picked an edition rather than for every
    edition offered, because only then is it worth a call against a published
    rate limit.

    * **A MusicBrainz edition** is asked for the Discogs release it links to. That
      published link is the only honest ground for treating the two as one
      object, and it is what lets the price of a record identified through
      MusicBrainz be followed at all.
    * **A Discogs edition** is fetched in full: the date to the month where the
      search only gave a year, the artist and title as separate fields instead
      of split out of "Artist - Title", and the run-out stamps and release notes,
      kept as `matrix_text` and `source_notes` ('' when asked and none listed).

    The answer is used only when it is about the release that was asked for. On
    any problem the candidate comes back as it was, with a sentence saying so —
    a record must never fail to be added because a catalogue was slow.
    """
    out = dict(cand)
    src = cand.get("source")
    if src == "musicbrainz" and cand.get("mbid") and not cand.get("discogs_release_id"):
        full, err = mb_release(c, cand["mbid"])
        if err or not full:
            return out, (f"MusicBrainz could not be asked for a Discogs link: {err} "
                         f"The record's page can try again.")
        # No check that the id came back unchanged: a lookup by id is answered for
        # that release or, if MusicBrainz has merged it, for the one it became —
        # the same edition either way.
        if full.get("discogs_release_id"):
            out["discogs_release_id"] = full["discogs_release_id"]
            return out, (f"MusicBrainz links this edition to Discogs release "
                         f"{full['discogs_release_id']}, so its price can be followed.")
        return out, ("MusicBrainz lists no Discogs link for this edition, so there is no "
                     "price to follow until you link one on the record's page.")
    if src == "discogs" and cand.get("discogs_release_id"):
        full, err = discogs_release(c, cand["discogs_release_id"])
        if err or not full:
            return out, (f"The full Discogs entry could not be fetched: {err} "
                         f"The record's page can try again.")
        if full.get("discogs_release_id") != cand["discogs_release_id"]:
            return out, ""
        for key in ("title", "artist_credit", "edition_note"):
            if full.get(key):
                out[key] = full[key]
        # A pick from a name search arrives with no barcode (see lookup_title);
        # the full entry's barcodes are typed as barcodes, so one that passes its
        # check digit is the edition's own. A scanned code is never replaced.
        if not out.get("barcode") and full.get("barcode"):
            import barcode as bc
            if bc.check_ok(full["barcode"]):
                out["barcode"] = full["barcode"]
        out["released"] = refine_date(cand.get("released"), full.get("released"))
        out["matrix_text"] = "\n".join(full.get("matrix") or [])
        out["source_notes"] = full.get("notes_from_source") or ""
        return out, ""
    return out, ""


# -------------------------------------------------------------------- prices

def price_now(c, rel):
    """The current lowest asking price for a release. (observation dict, problem).

    Uses `marketplace/stats` and nothing else. The release endpoint carries a
    `lowest_price` too and it is a different number — see this module's docstring
    — but the deciding reason is that stats states the currency of the figure it
    returns and the release endpoint does not. A price whose currency has to be
    assumed is a price this app will not store.

    Note what comes back and what does not: a number, a count of listings, and no
    condition at all. Discogs' public stats do not say what condition the cheapest
    copy is in, so `cond_band` is None and every page that shows this number says
    "any condition". Pretending it referred to the owner's grade would make every
    comparison in the app quietly wrong.
    """
    rid = rel["discogs_release_id"] if isinstance(rel, dict) or hasattr(rel, "keys") else rel
    if not rid:
        return None, ("There is no Discogs release linked to this record, so there is no "
                      "public price to follow. You can link one, or record prices by hand.")
    want = db.get(c, "currency", "GBP")
    d, err = _call(c, DG_HOST, f"/marketplace/stats/{int(rid)}", {"curr_abbr": want})
    if err:
        return None, err
    if not d:
        return None, "Discogs has no marketplace figures for that release."
    if d.get("blocked_from_sale"):
        return None, "Discogs has this release blocked from sale, so there is no price."
    low = d.get("lowest_price")
    if not isinstance(low, dict):
        return None, ("Discogs returned no lowest price — usually that means no copies are "
                      "for sale right now.")
    amount = model.moneyn(low.get("value"))
    cur = model.norm(low.get("currency") or "", 8).upper()
    if amount is None or not cur:
        return None, "Discogs returned a price without a currency, so it was not recorded."
    note = ""
    if cur != want:
        note = (f"Asked for {want}; Discogs answered in {cur}. Stored as {cur} — "
                f"this app never converts a currency.")
    return {
        "source": "discogs", "feed": "discogs_stats", "kind": "lowest_asking",
        "currency": cur, "amount": amount,
        "num_for_sale": model.intn(d.get("num_for_sale")),
        "cond_band": None, "cond_text": "",
        "url": f"https://www.discogs.com/sell/release/{int(rid)}",
        "payload": json.dumps(d, sort_keys=True)[:4000],
        "note": note,
    }, ""


# ------------------------------------------------------------------- artists

def artist_search(c, name):
    """Artists matching a typed name. (candidates, problem)."""
    q = model.norm(name, 200)
    if not q:
        return [], ""
    d, err = _call(c, MB_HOST, "/ws/2/artist",
                   {"query": q, "fmt": "json", "limit": "10"})
    out = []
    for a in ((d or {}).get("artists") or []):
        out.append({
            "mbid": str(a.get("id") or ""),
            "name": model.norm(a.get("name") or "", 200),
            "sort_name": model.norm(a.get("sort-name") or "", 200),
            "disambiguation": model.norm(a.get("disambiguation") or "", 200),
            "country": model.norm(a.get("country") or "", 8),
            "kind": model.norm(a.get("type") or "", 40),
            "score": model.intn(a.get("score")),
        })
    return out, err


def catalogue_size(c, artist_mbid):
    """How many works MusicBrainz lists for this artist. (count, problem).

    The cheap probe a watch runs every time. One request returns the total, and if
    the total has not moved since the last look then nothing has been added and
    the expensive sweep is skipped — which is what makes watching twenty artists
    affordable at one request per second.

    The blind spot is stated rather than hidden: if something is added and
    something else removed between two looks, the count can come back the same and
    this will miss it. `alerts.sweep_artist` therefore does a full sweep on a
    schedule as well as on a change, and the Watches page says when each artist
    was last swept in full.
    """
    d, err = _call(c, MB_HOST, "/ws/2/release-group",
                   {"artist": str(artist_mbid), "fmt": "json", "limit": "1"})
    if err or d is None:
        return None, err or "MusicBrainz had nothing for that artist."
    return model.intn(d.get("release-group-count")) or 0, ""


def catalogue(c, artist_mbid, start=0, cap=800):
    """The works MusicBrainz lists for an artist. (items, truncated, problem).

    Follows *works* rather than editions on purpose: "Miles Davis has a new
    record out" is a statement about a work, and there are 636 works against
    1,972 editions, so this is both the right question and the cheaper one.
    Whether a work has a vinyl pressing is a separate question, asked per new
    work by `formats_for` — because asking it in the query here would have meant
    filtering on `format:`, which under-counts vinyl twentyfold.

    Reads at most `cap` works from `start`. `truncated` is True whenever it
    stopped before the end of the list — at the cap or on a refusal — which is
    what lets a baseline too long for one sweep carry on from where it stopped
    rather than reading the same first page every time.
    """
    items, offset, trunc = [], max(0, int(start or 0)), False
    end = offset + cap
    while offset < end:
        d, err = _call(c, MB_HOST, "/ws/2/release-group",
                       {"artist": str(artist_mbid), "fmt": "json",
                        "limit": "100", "offset": str(offset)})
        if err:
            return items, True, err
        groups = (d or {}).get("release-groups") or []
        total = model.intn((d or {}).get("release-group-count")) or 0
        for g in groups:
            items.append({
                "source": "musicbrainz",
                "source_id": str(g.get("id") or ""),
                "title": model.norm(g.get("title") or "", 300),
                "released": model.partial_date(str(g.get("first-release-date") or "")) or "",
                "primary_type": model.norm(g.get("primary-type") or "", 40),
                "secondary": ", ".join(model.norm(s, 40) for s in (g.get("secondary-types") or [])),
                "url": f"https://musicbrainz.org/release-group/{g.get('id')}",
            })
        offset += len(groups)
        if not groups or offset >= total:
            break
    else:
        trunc = True
    return items, trunc, ""


def formats_for(c, rg_mbid):
    """Which formats exist for one work. (list of names, problem).

    Asked only about a work that has just appeared, which is why it is affordable.
    It is what lets an alert say "there is a vinyl pressing" or, just as usefully
    and just as honestly, "no vinyl pressing is listed yet".
    """
    d, err = _call(c, MB_HOST, "/ws/2/release",
                   {"release-group": str(rg_mbid), "fmt": "json",
                    "limit": "100", "inc": "media"})
    if err or d is None:
        return [], err
    names = []
    for r in (d.get("releases") or []):
        for m in (r.get("media") or []):
            nm = model.norm(m.get("format") or "", 60)
            if nm and nm not in names:
                names.append(nm)
    return names, ""


def has_vinyl(names):
    """Does this list of format names include a vinyl one? None if nothing was said."""
    if not names:
        return None
    low = " ".join(str(n).lower() for n in names)
    return any(w in low for w in _VINYL_WORDS)
