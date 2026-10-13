"""Price series, and the one question this app is most able to get wrong.

The owner asked to be told when a price moves "significantly". That word is doing
a lot of work, and the whole of this module is an attempt to deserve it. The word
itself is banned from the codebase — `tests.py:TestPromises` greps the code for it —
because in the only sense in which it has a technical meaning, nothing here
establishes it. The strongest thing this app will say is **notable**, and what
notable means is spelled out on the page, in numbers, every time.

Six rules, and every one of them exists because the obvious alternative lies.

**1. A price is only comparable with a price exactly like it.** A series is keyed
on release, source, feed, kind, currency AND condition band. All six. Two of
those are non-obvious and both come from measurement: `feed`, because Discogs'
two endpoints report different lowest prices for the same record in the same
currency (£34.40 against 51.43 — see db.py), and `cond_band`, because a price
quoted for a Near Mint copy is not evidence about a Very Good one.

**2. Nothing is converted, ever.** There is no exchange rate in this app. Series
in different currencies are different series, shown separately and never summed.
A rate would be a number from a third party, correct for one instant, silently
rewriting the owner's history every time it changed.

**3. The comparison is against a window median, not against yesterday.** A lowest
asking price is one seller's number. It vanishes when they sell, and reappears
when they relist, so consecutive observations bounce around for reasons that have
nothing to do with what a record is worth. The median of the previous window
moves when the market moves and ignores one seller arriving or leaving.

**4. Two thresholds, both of which must be crossed.** A percentage alone shouts
about a £4 single going to £5.20. An absolute alone ignores a box set moving £60.
The default is 20% *and* £3.00. The absolute floor is read in the series' own
currency and is not converted — see rule 2 — and the page says so.

**5. A series too short to have an opinion does not get one.** Under `min_obs`
observations in the baseline window, no move is reported, and the record's page
says "not enough observations yet" rather than showing a number with no support.
Every average in this module is printed with the `n` it came from.

**6. Up is not good and down is not bad.** A rise is a rise. Whether that is
welcome depends on whether the owner is buying or selling, which this app does not
know, so it does not colour the number green or red and it never uses the words
gain or loss. It says "up" or "down", with an arrow and the word both, so the
direction survives being printed in black and white.

And one confound, printed beside every move rather than mentioned in a footnote:
how many copies were for sale at each end. A price that halved while the number of
listings went from one to twelve has not told you the record is worth less.
"""
from __future__ import annotations

import db
import model

# The columns that make two observations comparable. Changing this tuple changes
# what the app believes a price series is, so it is written once, here.
SERIES_KEY = ("source", "feed", "kind", "currency", "cond_band")

# The strongest verdict this app is allowed to reach. There is no stronger one
# above it on purpose.
VERDICT = "notable"


def record(c, release_id, obs, copy_id=None, observed_at=None):
    """Store one observation. Returns the row id, or None if it was already there.

    The UNIQUE constraint on the series key plus the timestamp (plus the copy, for
    a receipt) makes this idempotent to the second: running the poller twice
    cannot double-count a price, and a retry after a half-finished sweep is safe.
    """
    at = observed_at or model.now_iso()
    cur = c.execute("""INSERT OR IGNORE INTO price_obs
        (release_id,copy_id,observed_at,source,feed,kind,currency,amount,
         cond_band,cond_text,num_for_sale,url,payload,note)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (release_id, copy_id, at, obs["source"], obs.get("feed", "hand"), obs["kind"],
         obs["currency"], obs["amount"], obs.get("cond_band"), obs.get("cond_text") or "",
         obs.get("num_for_sale"), obs.get("url") or "", obs.get("payload") or "",
         obs.get("note") or ""))
    return cur.lastrowid if cur.rowcount else None


def series_list(c, release_id, market_only=True):
    """Every distinct series recorded for a release, newest activity first.

    A release usually has one. It can have more — a hand-recorded sale beside a
    followed asking price, or two currencies because a source answered in its own
    — and they are listed separately because that is what they are.
    """
    rows = c.execute(f"""SELECT {','.join(SERIES_KEY)}, COUNT(*) AS n,
                                MIN(observed_at) AS first_at, MAX(observed_at) AS last_at
                         FROM price_obs WHERE release_id=?
                         GROUP BY {','.join(SERIES_KEY)}
                         ORDER BY last_at DESC""", (release_id,)).fetchall()
    out = []
    for r in rows:
        d = {k: r[k] for k in SERIES_KEY}
        d.update(n=r["n"], first_at=r["first_at"], last_at=r["last_at"])
        if market_only and d["kind"] not in model.MARKET_KINDS:
            continue
        out.append(d)
    return out


def points(c, release_id, key, limit=2000):
    """Every observation in one series, oldest first."""
    where = " AND ".join(f"{k} IS ?" for k in SERIES_KEY)
    args = [release_id] + [key.get(k) for k in SERIES_KEY]
    return c.execute(f"""SELECT observed_at, amount, num_for_sale, url, note
                         FROM price_obs WHERE release_id=? AND {where}
                         ORDER BY observed_at LIMIT ?""", args + [limit]).fetchall()


def median(xs):
    """The middle value, or None for an empty list.

    A median rather than a mean throughout. One seller asking £400 for a £30
    record moves a mean and does not move a median, and that seller exists.
    """
    vals = sorted(v for v in xs if v is not None)
    if not vals:
        return None
    mid = len(vals) // 2
    if len(vals) % 2:
        return float(vals[mid])
    return (vals[mid - 1] + vals[mid]) / 2.0


def latest(c, release_id, key=None):
    """The newest observation in a series, or in the best series if none is named."""
    if key is None:
        ss = series_list(c, release_id)
        if not ss:
            return None
        key = ss[0]
    pts = points(c, release_id, key)
    return pts[-1] if pts else None


def window(pts, before_at, days):
    """The observations in the `days` before `before_at`, excluding it.

    The latest point is deliberately not part of its own baseline: comparing a
    number against a median it is inside pulls the median toward the number and
    understates every move.
    """
    end = model.parse_iso(before_at)
    if end is None:
        return []
    out = []
    for p in pts:
        at = model.parse_iso(p["observed_at"])
        if at is None or at >= end:
            continue
        if (end - at).days <= days:
            out.append(p)
    return out


def rule_for(c, release_id, kind="price_move"):
    """The rule in force for a release: its own if it has one, else the default.

    One mechanism. A row naming this release wins; the row naming nothing is the
    default. A rule switched off means the release is not watched for moves —
    None, not a quiet fall back to the default, or switching one record off would
    do nothing at all — and an absent default means nothing is.
    """
    r = c.execute("""SELECT * FROM alert_rule WHERE kind=? AND release_id=?
                     ORDER BY id DESC LIMIT 1""", (kind, release_id)).fetchone()
    if r:
        return r if r["active"] else None
    return c.execute("""SELECT * FROM alert_rule WHERE kind=? AND release_id IS NULL
                        AND artist_id IS NULL AND active=1 ORDER BY id DESC LIMIT 1""",
                     (kind,)).fetchone()


def _num(v, fallback):
    return fallback if v is None else v


def assess(c, release_id, key, rule=None, now=None):
    """Weigh one series against a rule. Always returns a dict; `moved` says whether.

    The dict is the whole argument, not a verdict: the thresholds that were
    applied, the baseline and the `n` behind it, both listing counts, and every
    reason it did *not* fire. The pages render this directly, which is why a
    record with no move still has something honest to show — "£31.00 today
    against a £30.50 median over 12 observations, which is 1.6% and under both
    thresholds" tells the owner far more than silence.
    """
    rule = rule or rule_for(c, release_id)
    pct_t = _num(rule["pct_threshold"] if rule else None, db.get_float(c, "move_pct", 20.0))
    abs_t = _num(rule["abs_threshold"] if rule else None, db.get_float(c, "move_abs", 3.0))
    min_obs = int(_num(rule["min_obs"] if rule else None, db.get_int(c, "move_min_obs", 3)))
    win = int(_num(rule["window_days"] if rule else None, db.get_int(c, "move_window_days", 30)))
    direction_want = (rule["direction"] if rule else "both") or "both"

    out = {
        "moved": False, "why_not": "", "direction": "", "pct": None, "abs_change": None,
        "latest": None, "latest_at": "", "baseline": None, "baseline_n": 0,
        "baseline_from": "", "baseline_to": "", "currency": key.get("currency") or "",
        "kind": key.get("kind") or "", "cond_band": key.get("cond_band"),
        "feed": key.get("feed") or "", "source": key.get("source") or "",
        "rule_pct": pct_t, "rule_abs": abs_t, "rule_min_obs": min_obs,
        "rule_window_days": win, "rule_direction": direction_want,
        "n_total": 0, "for_sale_then": None, "for_sale_now": None, "verdict": "",
    }
    if key.get("kind") not in model.MARKET_KINDS:
        out["why_not"] = ("This is your own record of a purchase or a sale, not an "
                          "observation of the market, so it is never compared.")
        return out
    pts = points(c, release_id, key)
    out["n_total"] = len(pts)
    if not pts:
        out["why_not"] = "Nothing has been recorded for this series yet."
        return out
    newest = pts[-1]
    out["latest"] = newest["amount"]
    out["latest_at"] = newest["observed_at"]
    out["for_sale_now"] = newest["num_for_sale"]
    base = window(pts, newest["observed_at"], win)
    out["baseline_n"] = len(base)
    if len(base) < min_obs:
        out["why_not"] = (f"{len(base)} earlier observation"
                          f"{'' if len(base) == 1 else 's'} in the last {win} days, and "
                          f"{min_obs} are needed before a move means anything.")
        return out
    out["baseline"] = median([p["amount"] for p in base])
    out["baseline_from"] = base[0]["observed_at"]
    out["baseline_to"] = base[-1]["observed_at"]
    out["for_sale_then"] = median([p["num_for_sale"] for p in base
                                   if p["num_for_sale"] is not None])
    if out["baseline"] in (None, 0):
        out["why_not"] = "The earlier prices give no baseline to compare against."
        return out
    change = newest["amount"] - out["baseline"]
    out["abs_change"] = round(change, 2)
    out["pct"] = model.pct(out["baseline"], newest["amount"])
    out["direction"] = "up" if change > 0 else ("down" if change < 0 else "")
    if not out["direction"]:
        out["why_not"] = "Exactly the baseline."
        return out
    if direction_want != "both" and out["direction"] != direction_want:
        out["why_not"] = f"This rule only reports moves {direction_want}."
        return out
    over_pct = abs(out["pct"] or 0) >= pct_t
    over_abs = abs(change) >= abs_t
    if not over_pct and not over_abs:
        out["why_not"] = (f"{abs(out['pct'] or 0):.1f}% and "
                          f"{model.money(abs(change), out['currency'])} — under both "
                          f"thresholds ({pct_t:g}% and "
                          f"{model.money(abs_t, out['currency'])}).")
        return out
    if not over_pct:
        out["why_not"] = (f"{abs(out['pct'] or 0):.1f}% is under the {pct_t:g}% threshold, "
                          f"even though {model.money(abs(change), out['currency'])} clears "
                          f"the {model.money(abs_t, out['currency'])} floor.")
        return out
    if not over_abs:
        out["why_not"] = (f"{model.money(abs(change), out['currency'])} is under the "
                          f"{model.money(abs_t, out['currency'])} floor, even though "
                          f"{abs(out['pct'] or 0):.1f}% clears {pct_t:g}%.")
        return out
    out["moved"] = True
    out["verdict"] = VERDICT
    return out


def describe(a):
    """One sentence for an assessment. Written here so every page words it the same.

    Note what the sentence never says: not "gained", not "lost", not "worth". A
    lowest asking price is what somebody hopes to get, and the sentence says which
    direction it went and against what.
    """
    if not a or a.get("latest") is None:
        return ""
    cur = a.get("currency") or ""
    if not a.get("moved"):
        return f"{model.money(a['latest'], cur)} now. {a.get('why_not') or ''}".strip()
    arrow = "▲" if a["direction"] == "up" else "▼"
    return (f"{arrow} {a['direction']} {abs(a['pct']):.0f}% "
            f"({model.money(abs(a['abs_change']), cur)}) — "
            f"{model.money(a['latest'], cur)} now against a "
            f"{model.money(a['baseline'], cur)} median over {a['baseline_n']} "
            f"observation{'' if a['baseline_n'] == 1 else 's'}")


def confound_words(then, now):
    """The listing-count caveat, in one sentence. "" if there is nothing to say.

    Written once, here, because it has to appear in two places that must not
    drift apart: beside the move on a record's page, and inside the alert that
    gets emailed. A price that halved while the number of copies for sale went
    from one to twelve is a story about supply, and the owner is entitled to see
    that wherever they meet the number.
    """
    if then is None or now is None:
        return "How many copies were for sale is not recorded for both ends of this move."
    # `then` is the median over the baseline window and may fall between two
    # counts. Printed as it is: rounding 2.5 down to 2 would understate the change
    # in supply that this sentence exists to show.
    if float(then) == float(now):
        return f"{now:g} copies for sale at both ends of this move."
    word = "more" if now > then else "fewer"
    return (f"{then:g} copies were for sale then and {now:g} now — "
            f"{word} on offer, which moves a lowest asking price on its own.")


def confound(a):
    """The listing-count caveat for an assessment. Printed next to every move."""
    if not a or not a.get("moved"):
        return ""
    return confound_words(a.get("for_sale_then"), a.get("for_sale_now"))


def kind_caveat(kind):
    """What this kind of number is not. Printed wherever the number is."""
    row = model.PRICE_KINDS.get(kind)
    return row[1] if row else ""


# ------------------------------------------------------------------- roll-ups

def collection_totals(c, owned_only=True):
    """The sum of the latest asking price per record, grouped by currency.

    Grouped, never totalled across currencies, and returned with the counts that
    make it readable: how many records have a price and how many do not. A single
    "collection value" number would be the most-looked-at figure in the app and
    the least defensible one — it would be a sum of asking prices for copies in
    unknown condition, so this returns the parts and lets the page say so.
    """
    join = ("JOIN copy cp ON cp.release_id=r.id AND cp.disposed_on IS NULL"
            if owned_only else "")
    rows = c.execute(f"""SELECT DISTINCT r.id FROM release r {join}""").fetchall()
    ids = [r["id"] for r in rows]
    by_cur, priced = {}, 0
    for rid in ids:
        ss = series_list(c, rid)
        if not ss:
            continue
        p = latest(c, rid, ss[0])
        if not p:
            continue
        priced += 1
        cur = ss[0]["currency"]
        d = by_cur.setdefault(cur, {"currency": cur, "total": 0.0, "n": 0})
        d["total"] += p["amount"]
        d["n"] += 1
    return {"groups": sorted(by_cur.values(), key=lambda g: -g["total"]),
            "records": len(ids), "priced": priced, "unpriced": len(ids) - priced}


def paid_totals(c):
    """What was paid, by currency. A sum of receipts, which is a real number.

    Every receipt counts, including for copies since sold or given away: it is
    what was spent, not what is on the shelf.
    """
    rows = c.execute("""SELECT currency, SUM(amount) AS total, COUNT(*) AS n
                        FROM price_obs WHERE kind='paid' GROUP BY currency
                        ORDER BY total DESC""").fetchall()
    return [{"currency": r["currency"], "total": r["total"], "n": r["n"]} for r in rows]


# ------------------------------------------------------------------ the chart

def sparkline(pts, w=220, h=44):
    """A price series as an inline SVG, or "" if there is nothing to draw.

    A picture of the numbers that are already printed beside it, never instead of
    them. Uses `currentColor` so it inherits the theme, and it carries no axis
    labels because a 44-pixel-high chart cannot label an axis honestly — the page
    prints the first and last value and the count as text.
    """
    vals = [p["amount"] for p in pts if p["amount"] is not None]
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    pad = 3
    step = (w - pad * 2) / (len(vals) - 1)
    pieces = []
    for i, v in enumerate(vals):
        x = pad + i * step
        y = h - pad - (v - lo) / span * (h - pad * 2)
        pieces.append(f"{x:.1f},{y:.1f}")
    last_x, last_y = pieces[-1].split(",")
    return (f'<svg class="spark" width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
            f'role="img" aria-label="{len(vals)} observations, '
            f'{lo:.2f} to {hi:.2f}" focusable="false">'
            f'<polyline fill="none" stroke="currentColor" stroke-width="1.5" '
            f'stroke-linejoin="round" points="{" ".join(pieces)}"/>'
            f'<circle cx="{last_x}" cy="{last_y}" r="2.5" fill="currentColor"/></svg>')
