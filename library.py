"""Assembling the collection for the pages. Stdlib only.

This module puts rows next to each other. It does not compute anything the tables
would not: every price comes from `prices.py`, every grade is read from the newest
`grading` row rather than from a cached column, and there is no average of
anything in here.

The one judgement it makes, and makes explicitly, is about grades. A grade is an
ordinal label — 4 is better than 3, but it is not "one better", and the distance
between a 2 and a 3 is not the distance between a 4 and a 5. So this module never
computes a mean grade. It reports the *distribution*: how many 5s, how many 4s,
how many not graded at all. A collection described as "averaging 3.7" sounds
precise and means nothing; "eleven at 5, nine at 4, two ungraded" is the same
information without the false precision.
"""
from __future__ import annotations

import db
import model
import prices


def current_grading(c, copy_id):
    """The newest grading for a copy, or None. Never a cached column.

    Newest by `graded_on` where it is known, falling back to when it was entered.
    Re-grading a record appends a row, so the history stays readable and the
    current answer is always the last word rather than a copy of it.
    """
    return c.execute("""SELECT * FROM grading WHERE copy_id=?
                        ORDER BY COALESCE(graded_on, substr(graded_at,1,10)) DESC,
                                 id DESC LIMIT 1""", (copy_id,)).fetchone()


def grade_view(g):
    """A grading row as display values, or the empty shape when there is none."""
    if not g:
        return {"has": False, "media": None, "sleeve": None,
                "media_name": "", "sleeve_name": "", "media_why": "not graded yet",
                "sleeve_why": "not graded yet", "note": "", "on": ""}
    return {
        "has": True,
        "media": g["media_grade"], "sleeve": g["sleeve_grade"],
        "media_name": model.grade_name("media", g["media_grade"]),
        "sleeve_name": model.grade_name("sleeve", g["sleeve_grade"]),
        "media_why": model.UNGRADED_REASONS.get(g["media_ungraded"] or "", ""),
        "sleeve_why": model.UNGRADED_REASONS.get(g["sleeve_ungraded"] or "", ""),
        "media_gold": model.goldmine(g["media_grade"]),
        "sleeve_gold": model.goldmine(g["sleeve_grade"]),
        "note": g["note"] or "",
        "on": g["graded_on"] or (g["graded_at"] or "")[:10],
    }


def copies_of(c, release_id, include_gone=True):
    where = "" if include_gone else " AND disposed_on IS NULL"
    rows = c.execute(f"""SELECT * FROM copy WHERE release_id=?{where}
                         ORDER BY disposed_on IS NOT NULL, id""", (release_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["grade"] = grade_view(current_grading(c, r["id"]))
        d["owned"] = not r["disposed_on"]
        d["paid"] = _copy_price(c, r["id"], "paid")
        d["sold"] = _copy_price(c, r["id"], "sold")
        out.append(d)
    return out


def _copy_price(c, copy_id, kind):
    """The first price of one kind recorded against one copy — a receipt, or a sale."""
    return c.execute("""SELECT amount, currency, observed_at FROM price_obs
                        WHERE copy_id=? AND kind=?
                        ORDER BY observed_at, id LIMIT 1""", (copy_id, kind)).fetchone()


def copy_detail(c, copy_id):
    """One copy, with its release, its grade, and what it cost and fetched."""
    cp = c.execute("SELECT * FROM copy WHERE id=?", (copy_id,)).fetchone()
    if not cp:
        return None
    d = dict(cp)
    d["release"] = c.execute("SELECT * FROM release WHERE id=?", (cp["release_id"],)).fetchone()
    d["grade"] = grade_view(current_grading(c, copy_id))
    d["owned"] = not cp["disposed_on"]
    d["paid"] = _copy_price(c, copy_id, "paid")
    d["sold"] = _copy_price(c, copy_id, "sold")
    return d


SORTS = {
    "artist": "LOWER(r.artist_credit), LOWER(r.title)",
    "title": "LOWER(r.title)",
    "added": "r.added_at DESC, r.id DESC",
    "year": "r.released IS NULL OR r.released='', r.released DESC",
    "label": "LOWER(r.label), LOWER(r.catno)",
}


def shelf(c, q="", artist_id=None, fmt="", owned="", sort="artist", tracked="", linked=""):
    """The collection as a list of rows, each carrying its copies and its price.

    `owned` is a filter over a derived fact — a copy with no disposal — and not
    over a stored flag, so selling a record is one row change and never leaves the
    shelf disagreeing with itself. `linked` is whether there is a Discogs release
    to look a price up against, which is what "followed" actually needs.
    """
    where, args = ["1=1"], []
    if q:
        where.append("(LOWER(r.title) LIKE ? OR LOWER(r.artist_credit) LIKE ? "
                     "OR LOWER(COALESCE(r.catno,'')) LIKE ? OR COALESCE(r.barcode,'') LIKE ?)")
        like = f"%{q.lower()}%"
        args += [like, like, like, f"%{q}%"]
    if artist_id:
        where.append("r.artist_id=?")
        args.append(artist_id)
    if fmt:
        where.append("r.format=?")
        args.append(fmt)
    if tracked in ("0", "1"):
        where.append("r.tracked=?")
        args.append(int(tracked))
    if linked == "yes":
        where.append("r.discogs_release_id IS NOT NULL")
    elif linked == "no":
        where.append("r.discogs_release_id IS NULL")
    if owned == "yes":
        where.append("EXISTS(SELECT 1 FROM copy cp WHERE cp.release_id=r.id "
                     "AND cp.disposed_on IS NULL)")
    elif owned == "no":
        where.append("NOT EXISTS(SELECT 1 FROM copy cp WHERE cp.release_id=r.id "
                     "AND cp.disposed_on IS NULL)")
    order = SORTS.get(sort, SORTS["artist"])
    rows = c.execute(f"""SELECT r.* FROM release r WHERE {' AND '.join(where)}
                         ORDER BY {order}""", args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["copies"] = copies_of(c, r["id"])
        d["owned_n"] = sum(1 for cp in d["copies"] if cp["owned"])
        ss = prices.series_list(c, r["id"])
        d["series"] = ss[0] if ss else None
        d["price"] = prices.latest(c, r["id"], ss[0]) if ss else None
        out.append(d)
    return out


def release_detail(c, rid):
    """Everything one record's page needs, in one dict."""
    rel = c.execute("SELECT * FROM release WHERE id=?", (rid,)).fetchone()
    if not rel:
        return None
    d = dict(rel)
    d["copies"] = copies_of(c, rid)
    d["owned_n"] = sum(1 for cp in d["copies"] if cp["owned"])
    d["series"] = prices.series_list(c, rid)
    d["all_series"] = prices.series_list(c, rid, market_only=False)
    d["assessments"] = [(s, prices.assess(c, rid, s)) for s in d["series"]]
    d["rule"] = prices.rule_for(c, rid)
    d["scans"] = c.execute("""SELECT * FROM barcode_scan WHERE release_id=?
                              ORDER BY scanned_at DESC LIMIT 10""", (rid,)).fetchall()
    d["alerts"] = c.execute("""SELECT * FROM alert WHERE release_id=?
                               ORDER BY raised_at DESC LIMIT 20""", (rid,)).fetchall()
    d["artist"] = (c.execute("SELECT * FROM artist WHERE id=?", (rel["artist_id"],)).fetchone()
                   if rel["artist_id"] else None)
    return d


def grade_distribution(c, owned_only=True):
    """How many copies sit at each grade, for the record and for the sleeve.

    A distribution and not an average — see this module's docstring. Ungraded
    copies are counted as ungraded, which is a category and not a gap: it is how
    the shelf says "you have not looked at eleven of these yet".
    """
    where = " WHERE cp.disposed_on IS NULL" if owned_only else ""
    rows = c.execute(f"SELECT cp.id FROM copy cp{where}").fetchall()
    out = {"media": {g: 0 for g in model.GRADES}, "sleeve": {g: 0 for g in model.GRADES},
           "media_ungraded": 0, "sleeve_ungraded": 0, "no_grading": 0, "n": len(rows)}
    for r in rows:
        g = current_grading(c, r["id"])
        if not g:
            out["no_grading"] += 1
            continue
        if g["media_grade"]:
            out["media"][g["media_grade"]] += 1
        else:
            out["media_ungraded"] += 1
        if g["sleeve_grade"]:
            out["sleeve"][g["sleeve_grade"]] += 1
        else:
            out["sleeve_ungraded"] += 1
    return out


def needs_grading(c, limit=50):
    """Copies with no grading row at all. The shelf's own to-do list."""
    return c.execute("""SELECT cp.*, r.title, r.artist_credit FROM copy cp
                        JOIN release r ON r.id=cp.release_id
                        WHERE cp.disposed_on IS NULL
                        AND NOT EXISTS(SELECT 1 FROM grading g WHERE g.copy_id=cp.id)
                        ORDER BY cp.id DESC LIMIT ?""", (limit,)).fetchall()


def untracked_with_copies(c, limit=50):
    """Records you own whose price nobody is following."""
    return c.execute("""SELECT r.* FROM release r WHERE r.tracked=0
                        AND EXISTS(SELECT 1 FROM copy cp WHERE cp.release_id=r.id
                                   AND cp.disposed_on IS NULL)
                        ORDER BY LOWER(r.artist_credit) LIMIT ?""", (limit,)).fetchall()


def unlinked_tracked(c, limit=50):
    """Records followed for price that have no Discogs release to follow.

    Tracking is on by default, and a record entered by hand has nothing to poll,
    so this list is how the app admits that "tracked" and "actually being followed"
    are two different things.
    """
    return c.execute("""SELECT r.* FROM release r
                        WHERE r.tracked=1 AND r.discogs_release_id IS NULL
                        ORDER BY LOWER(r.artist_credit) LIMIT ?""", (limit,)).fetchall()


def due_for_price(c, max_age_hours, limit=200):
    """Tracked, linked records whose price has not been looked at recently.

    The staleness floor is what stops a poller run from asking about every record
    every time. A record checked an hour ago is not asked about again — unless the
    collection has switched currency since, because then the last look was in a
    currency nobody is asking for any more (`db.set_currency`).
    """
    rows = c.execute("""SELECT * FROM release WHERE tracked=1
                        AND discogs_release_id IS NOT NULL
                        ORDER BY COALESCE(last_priced_at,'') LIMIT ?""", (limit * 3,)).fetchall()
    n = model.parse_iso(model.now_iso())
    since = model.parse_iso(db.get(c, "currency_since", ""))
    out = []
    for r in rows:
        d = model.parse_iso(r["last_priced_at"]) if r["last_priced_at"] else None
        if (d is None or (since and d < since)
                or (n and (n - d).total_seconds() >= max_age_hours * 3600)):
            out.append(r)
        if len(out) >= limit:
            break
    return out


def recent_searches(c, n=8):
    """The latest searches by name, with the record each became, if any."""
    return c.execute("""SELECT s.*, r.title AS r_title, r.artist_credit AS r_artist
                        FROM title_search s LEFT JOIN release r ON r.id=s.release_id
                        ORDER BY s.id DESC LIMIT ?""", (n,)).fetchall()


def artists(c):
    """Every artist, with what they hold and whether they are watched."""
    return c.execute("""SELECT a.*,
            (SELECT COUNT(*) FROM release r WHERE r.artist_id=a.id) AS releases,
            (SELECT COUNT(*) FROM copy cp JOIN release r2 ON r2.id=cp.release_id
             WHERE r2.artist_id=a.id AND cp.disposed_on IS NULL) AS copies,
            (SELECT COUNT(*) FROM artist_release ar WHERE ar.artist_id=a.id) AS known
            FROM artist a ORDER BY LOWER(COALESCE(a.sort_name, a.name))""").fetchall()


def artist_detail(c, aid):
    a = c.execute("SELECT * FROM artist WHERE id=?", (aid,)).fetchone()
    if not a:
        return None
    d = dict(a)
    d["releases"] = c.execute("""SELECT * FROM release WHERE artist_id=?
                                 ORDER BY released DESC, LOWER(title)""", (aid,)).fetchall()
    d["known"] = c.execute("""SELECT * FROM artist_release WHERE artist_id=?
                              ORDER BY released DESC, LOWER(title) LIMIT 400""",
                           (aid,)).fetchall()
    d["alerts"] = c.execute("""SELECT * FROM alert WHERE artist_id=?
                               ORDER BY raised_at DESC LIMIT 30""", (aid,)).fetchall()
    return d


def recent_scans(c, limit=30):
    return c.execute("""SELECT s.*, r.title, r.artist_credit FROM barcode_scan s
                        LEFT JOIN release r ON r.id=s.release_id
                        ORDER BY s.scanned_at DESC, s.id DESC LIMIT ?""", (limit,)).fetchall()


def counts(c):
    """The headline counts. Every one of them is a COUNT, never an average."""
    one = lambda sql, *a: c.execute(sql, a).fetchone()[0]
    return {
        "releases": one("SELECT COUNT(*) FROM release"),
        "copies": one("SELECT COUNT(*) FROM copy WHERE disposed_on IS NULL"),
        "gone": one("SELECT COUNT(*) FROM copy WHERE disposed_on IS NOT NULL"),
        "artists": one("SELECT COUNT(*) FROM artist"),
        "watched": one("SELECT COUNT(*) FROM artist WHERE watch=1"),
        "tracked": one("SELECT COUNT(*) FROM release WHERE tracked=1"),
        "linked": one("SELECT COUNT(*) FROM release WHERE discogs_release_id IS NOT NULL"),
        "observations": one("SELECT COUNT(*) FROM price_obs"),
        "scans": one("SELECT COUNT(*) FROM barcode_scan"),
    }
