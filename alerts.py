"""Raising alerts, and sending them somewhere. Stdlib only.

Two things are watched: the price of a record you follow, and the catalogue of an
artist you follow. Both end in a row in `alert`, and every alert stores the
numbers it fired on rather than the sentence a person reads — the sentence is
written at display time by `headline`, so it can never drift away from its own
evidence.

**The outward door.** `sources.py` is the only place the app *reads* from the
internet. This module is the only place it *writes* to it, and the two are
deliberately separate, because they have opposite privacy properties. Reading
sends a barcode. Sending a notification sends the name of a record you own to a
mail server or a URL you nominated. So there are exactly two functions here that
leave the machine — `_smtp_send` and `_http_post` — both are replaced wholesale
by the test suite, and **neither does anything at all until the owner has filled
in a destination.** Out of the box, alerts go to a page in this app and nowhere
else.

**The first look at an artist announces nothing.** `artist.baseline_at` is the
stamp of the first complete sweep. Until it is set, everything found is recorded
as already-known and no alert is raised. Without that rule, watching Miles Davis
would announce six hundred and thirty-six new albums in one afternoon, and the
owner would turn notifications off — which is the real failure mode of a watch
feature, not a missed record.

**A record that is new to a database is not a new record.** A sweep can only ever
observe that something has been *added to MusicBrainz*. Whether it is new depends
on its release date, and the two are kept apart by `classify_new`: `new_release`
for something released in the last eighteen months or still to come,
`newly_listed` for a 1971 album somebody catalogued last Tuesday. Both are worth
knowing. Confusing them is how a collection app starts announcing that Coltrane
has a new record out.
"""
from __future__ import annotations
import json
import smtplib
import ssl
import urllib.error
import urllib.request
from email.message import EmailMessage

import db
import model
import prices
import sources

# How many format lookups one sweep may spend. A newly-listed work costs one
# extra request to find out whether there is a vinyl pressing; a catalogue that
# gains forty works at once must not turn into forty requests in a burst.
FORMAT_LOOKUPS_PER_SWEEP = 5

# Works read per sweep while a baseline is still being built, and the most a
# full sweep of an already-baselined artist will read. 800 is eight requests at
# one a second; the ceiling bounds a sweep of a catalogue in the thousands.
SWEEP_PAGE_CAP = 800
FULL_SWEEP_CEILING = 2500

HTTP_TIMEOUT = 10
SMTP_TIMEOUT = 20


# ------------------------------------------------------------ price watching

def cooldown_clear(c, release_id, direction, days, now=None):
    """Has enough time passed since the last alert like this one?

    Without a cooldown a record that has genuinely moved would be reported every
    single time the poller ran, because the move is still true tomorrow. The
    baseline median catches up on its own eventually; the cooldown is what covers
    the days in between.
    """
    row = c.execute("""SELECT raised_at FROM alert WHERE kind='price_move'
                       AND release_id=? AND direction=?
                       ORDER BY raised_at DESC LIMIT 1""", (release_id, direction)).fetchone()
    if not row:
        return True
    gap = model.days_between(row["raised_at"], now or model.now_iso())
    return gap is None or gap >= days


def evaluate_prices(c, now=None, release_id=None):
    """Weigh every tracked release against its rule. Returns the alerts raised.

    Writes nothing but alerts: this reads price observations that are already
    recorded and never fetches anything. Polling and judging are separate jobs on
    purpose, so re-running the judgement after changing a threshold costs nothing
    and asks the internet for nothing.
    """
    where = "WHERE tracked=1" + (" AND id=?" if release_id else "")
    args = (release_id,) if release_id else ()
    raised = []
    for rel in c.execute(f"SELECT * FROM release {where}", args).fetchall():
        rule = prices.rule_for(c, rel["id"])
        if not rule:
            continue
        cool = int(rule["cooldown_days"] if rule["cooldown_days"] is not None
                   else db.get_int(c, "move_cooldown_days", 14))
        for key in prices.series_list(c, rel["id"]):
            a = prices.assess(c, rel["id"], key, rule, now=now)
            if not a["moved"]:
                continue
            if not cooldown_clear(c, rel["id"], a["direction"], cool, now):
                continue
            raised.append(_raise_price(c, rel, a, rule, now))
    c.commit()
    return raised


def _raise_price(c, rel, a, rule, now=None):
    cur = c.execute("""INSERT INTO alert(kind,release_id,rule_id,raised_at,direction,currency,
            from_amount,to_amount,from_at,to_at,baseline_n,pct,abs_change,
            for_sale_then,for_sale_now,
            series_source,series_kind,cond_band,rule_pct,rule_abs,rule_min_obs,rule_window_days)
            VALUES('price_move',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (rel["id"], rule["id"] if rule else None, now or model.now_iso(), a["direction"],
         a["currency"], a["baseline"], a["latest"], a["baseline_to"], a["latest_at"],
         a["baseline_n"], a["pct"], a["abs_change"],
         a["for_sale_then"], a["for_sale_now"], a["source"], a["kind"],
         a["cond_band"], a["rule_pct"], a["rule_abs"], a["rule_min_obs"],
         a["rule_window_days"]))
    return cur.lastrowid


# ----------------------------------------------------------- artist watching

def classify_new(released, now=None):
    """'new_release' or 'newly_listed' for something just seen. Never guesses.

    A missing release date means `newly_listed`, not `new_release`. The app can
    always see that a record appeared in a catalogue; it can only call it new if
    the catalogue said when it came out.
    """
    if not released:
        return "newly_listed"
    year = model.year_of(released)
    if year is None:
        return "newly_listed"
    stamp = released if len(released) == 10 else (
        f"{released}-01" if len(released) == 7 else f"{released}-01-01")
    age = model.days_between(f"{stamp}T00:00:00+00:00", now or model.now_iso())
    if age is None:
        return "newly_listed"
    return "new_release" if age <= sources.RECENT_DAYS else "newly_listed"


def sweep_artist(c, artist, force=False, now=None, lookups=FORMAT_LOOKUPS_PER_SWEEP):
    """Look at everything an artist has out. (new alert ids, note).

    Cheap by default. One request returns how many works MusicBrainz lists, and if
    that has not changed since the last sweep there is nothing to find and the
    expensive paging is skipped — which is what makes watching a shelf full of
    artists affordable inside a one-request-per-second limit.

    The blind spot is real and is not papered over: if one work is added and
    another removed between two looks, the count is unchanged and this will miss
    it. `force` does the full sweep regardless, the Watches page offers it as a
    button, and the poller uses it on a slow rotation.

    The baseline is the first *complete* look. A catalogue longer than one sweep
    reads is baselined in pieces, each look carrying on from the last, and
    nothing is announced until the whole of it has been read once — otherwise
    the works past the first page would be announced as new the moment anything
    reached them. Once baselined, a sweep reads from the top and as far as the
    count says the catalogue goes, up to FULL_SWEEP_CEILING.
    """
    now = now or model.now_iso()
    if not artist["mbid"]:
        return [], ("There is no MusicBrainz artist linked, so there is nothing to watch. "
                    "Search for the artist on the artist's page to link one.")
    size, err = sources.catalogue_size(c, artist["mbid"])
    if err:
        return [], err
    known = c.execute("""SELECT COUNT(*) AS n FROM artist_release
                         WHERE artist_id=? AND source='musicbrainz'""",
                      (artist["id"],)).fetchone()["n"]
    baselined = bool(artist["baseline_at"])
    if baselined and not force and size is not None and size == known:
        c.execute("UPDATE artist SET last_checked_at=? WHERE id=?", (now, artist["id"]))
        c.commit()
        return [], ""
    if baselined:
        cap = max(SWEEP_PAGE_CAP, min(FULL_SWEEP_CEILING, (size or 0) + 100))
        items, trunc, err = sources.catalogue(c, artist["mbid"], start=0, cap=cap)
    else:
        items, trunc, err = sources.catalogue(c, artist["mbid"], start=known,
                                              cap=SWEEP_PAGE_CAP)
    if err and not items:
        return [], err
    fresh = []
    for it in items:
        cur = c.execute("""INSERT OR IGNORE INTO artist_release
                (artist_id,source,source_id,title,released,primary_type,format_note,url,
                 first_seen_at) VALUES(?,?,?,?,?,?,?,?,?)""",
            (artist["id"], it["source"], it["source_id"], it["title"], it["released"],
             it["primary_type"], "", it["url"], now))
        if cur.rowcount:
            fresh.append(it)
    raised = []
    if not baselined:
        total = known + len(fresh)
        if trunc:
            # Not the whole catalogue yet, so not a baseline yet: the next look
            # carries on from here, and still announces nothing.
            c.execute("UPDATE artist SET last_checked_at=? WHERE id=?", (now, artist["id"]))
            c.commit()
            of = f" of {size}" if size else ""
            return [], (f"Read {total}{of} works by {artist['name']} so far. Nothing is "
                        f"announced until the whole catalogue has been read once; the next "
                        f"check carries on from here." + (f" {err}" if err else ""))
        # The first complete look. Everything found is "already known" from here on,
        # and nothing is announced.
        c.execute("UPDATE artist SET baseline_at=?, last_checked_at=? WHERE id=?",
                  (now, now, artist["id"]))
        c.commit()
        return [], (f"Noted what {artist['name']} already has out — {total} works. "
                    f"From now on, anything that appears is new.")
    spent = 0
    for it in fresh:
        fmt_names, fmt_err = [], ""
        looked = spent < lookups
        if looked:
            fmt_names, fmt_err = sources.formats_for(c, it["source_id"])
            spent += 1
        vinyl = sources.has_vinyl(fmt_names)
        fmt_note = (", ".join(fmt_names) if fmt_names else
                    "not looked up" if not looked else
                    "could not be looked up" if fmt_err else "none listed yet")
        c.execute("""UPDATE artist_release SET format_note=? WHERE artist_id=? AND source=?
                     AND source_id=?""", (fmt_note, artist["id"], it["source"], it["source_id"]))
        raised.append(_raise_new(c, artist, it, fmt_note, vinyl, now))
    c.execute("UPDATE artist SET last_checked_at=? WHERE id=?", (now, artist["id"]))
    c.commit()
    note = ""
    if trunc:
        note = (f"Only the first {len(items)} works in the catalogue were read this time, "
                f"so anything new past them was not seen." + (f" {err}" if err else ""))
    return raised, note


def _raise_new(c, artist, it, fmt_note, vinyl, now):
    cur = c.execute("""INSERT INTO alert(kind,artist_id,raised_at,move_kind,item_title,
            item_released,item_url,item_source,item_format)
            VALUES('new_release',?,?,?,?,?,?,?,?)""",
        (artist["id"], now, classify_new(it["released"], now), it["title"],
         it["released"], it["url"], it["source"],
         ("vinyl: " if vinyl else ("no vinyl listed: " if vinyl is False else "")) + fmt_note))
    return cur.lastrowid


# ----------------------------------------------------------------- the inbox

def is_test(a):
    """Is this the alert the "Send a test notification" button makes?

    Told apart by what it is rather than by a flag column: a catalogue alert
    that names no artist and came from no catalogue. Without this it would read
    "An artist you follow has a record newly listed", which is not true.
    """
    return (a["kind"] == "new_release" and not a["artist_id"]
            and (a["item_source"] or "") == "manual")


def headline(c, a):
    """The sentence for one alert, written now out of what was stored then.

    Never stored. An alert keeps its numbers and its thresholds, and the words are
    assembled here, so there is exactly one wording in the app and it cannot fall
    out of step with the evidence printed under it.
    """
    if is_test(a):
        return f"A test notification from {model.APP_NAME}"
    if a["kind"] == "price_move":
        rel = c.execute("SELECT title, artist_credit FROM release WHERE id=?",
                        (a["release_id"],)).fetchone()
        who = f"{rel['artist_credit']} — " if rel and rel["artist_credit"] else ""
        what = rel["title"] if rel else "a record"
        arrow = "▲" if a["direction"] == "up" else "▼"
        pctv = abs(a["pct"] or 0)
        return (f"{arrow} {who}{what}: lowest asking price {a['direction']} {pctv:.0f}% "
                f"({model.money(abs(a['abs_change'] or 0), a['currency'])}) to "
                f"{model.money(a['to_amount'], a['currency'])}")
    art = c.execute("SELECT name FROM artist WHERE id=?", (a["artist_id"],)).fetchone()
    who = art["name"] if art else "An artist you follow"
    word = ("has a new record out" if a["move_kind"] == "new_release"
            else "has a record newly listed")
    when = f" ({a['item_released']})" if a["item_released"] else ""
    return f"{who} {word}: {a['item_title']}{when}"


def detail_lines(c, a):
    """The evidence under a headline, as a list of sentences.

    This is the part that makes an alert checkable rather than merely believable:
    the thresholds that were in force when it fired, the baseline and its `n`, and
    for a catalogue alert the difference between "released" and "we first saw it".
    """
    out = []
    if is_test(a):
        return ["Sent from the Alerts page to check this channel works. If you are "
                "reading this, real alerts will reach you here too."]
    if a["kind"] == "price_move":
        out.append(f"{model.money(a['from_amount'], a['currency'])} median over "
                   f"{a['baseline_n']} observation"
                   f"{'' if a['baseline_n'] == 1 else 's'} up to {a['from_at'][:10]}, "
                   f"against {model.money(a['to_amount'], a['currency'])} on {a['to_at'][:10]}.")
        out.append(f"Rule in force: at least {a['rule_pct']:g}% and "
                   f"{model.money(a['rule_abs'], a['currency'])}, over a "
                   f"{a['rule_window_days']}-day window with at least "
                   f"{a['rule_min_obs']} observations.")
        # The confound travels with the alert, not only with the page. An alert
        # that reaches somebody by email is the version they will act on.
        out.append(prices.confound_words(a["for_sale_then"], a["for_sale_now"]))
        out.append(prices.kind_caveat(a["series_kind"] or "lowest_asking"))
        if a["cond_band"] is None:
            out.append("The source did not say what condition the cheapest copy is in, "
                       "so this is not a price for any particular grade.")
    else:
        if a["move_kind"] == "newly_listed":
            out.append("This was added to the catalogue recently; the record itself is "
                       "not new. Worth a look if you did not know it existed.")
        if a["item_format"]:
            out.append(f"Formats listed: {a['item_format']}.")
        out.append(f"First seen by this app on {a['raised_at'][:10]}.")
    return [s for s in out if s]


def open_alerts(c, limit=200, include_seen=True):
    where = "WHERE dismissed_at IS NULL" + ("" if include_seen else " AND seen_at IS NULL")
    return c.execute(f"SELECT * FROM alert {where} ORDER BY raised_at DESC, id DESC LIMIT ?",
                     (limit,)).fetchall()


def unseen_count(c):
    return c.execute("""SELECT COUNT(*) AS n FROM alert
                        WHERE dismissed_at IS NULL AND seen_at IS NULL""").fetchone()["n"]


def mark_seen(c, ids):
    now = model.now_iso()
    for i in ids:
        c.execute("UPDATE alert SET seen_at=COALESCE(seen_at,?) WHERE id=?", (now, i))


def dismiss(c, alert_id):
    c.execute("UPDATE alert SET dismissed_at=? WHERE id=?", (model.now_iso(), alert_id))


# ---------------------------------------------------------------- delivering

def _smtp_send(host, port, user, password, use_tls, msg, timeout=SMTP_TIMEOUT):
    """The only SMTP connection in the app. Replaced wholesale by the tests."""
    srv = smtplib.SMTP(host, port, timeout=timeout)
    try:
        if use_tls:
            srv.starttls(context=ssl.create_default_context())
        if user:
            srv.login(user, password)
        srv.send_message(msg)
    finally:
        try:
            srv.quit()
        except Exception:
            pass


def _http_post(url, payload, timeout=HTTP_TIMEOUT):
    """The only outbound POST in the app. Replaced wholesale by the tests."""
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json",
                 "User-Agent": f"Sleevenote/{sources.VERSION}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, (r.read() or b"")[:200]
    except urllib.error.HTTPError as ex:
        return ex.code, b""


def channels_ready(c):
    """Which delivery channels are configured. A list of names, possibly empty."""
    out = []
    if model.norm(db.get(c, "smtp_host", ""), 200) and model.norm(db.get(c, "smtp_to", ""), 200):
        out.append("email")
    if model.norm(db.get(c, "webhook_url", ""), 500).startswith("http"):
        out.append("webhook")
    return out


def _log(c, alert_id, channel, ok, detail):
    c.execute("""INSERT INTO delivery(alert_id,channel,attempted_at,ok,detail)
                 VALUES(?,?,?,?,?)""",
              (alert_id, channel, model.now_iso(), 1 if ok else 0, model.norm(detail, 400)))


def deliver(c, a, channels=None):
    """Send one alert on every configured channel. Returns [(channel, ok, detail)].

    Every attempt is recorded whether it worked or not. A notification feature
    that cannot tell you the mail server refused the password is a notification
    feature that silently stops working, and its owner finds out months later.
    """
    out = []
    for ch in (channels if channels is not None else channels_ready(c)):
        try:
            if ch == "email":
                ok, detail = _deliver_email(c, a)
            elif ch == "webhook":
                ok, detail = _deliver_webhook(c, a)
            else:
                ok, detail = False, f"No such channel: {ch}"
        except Exception as ex:                  # a bad address must not stop a sweep
            ok, detail = False, f"{type(ex).__name__}: {ex}"
        _log(c, a["id"], ch, ok, detail)
        out.append((ch, ok, detail))
    c.commit()
    return out


def _body(c, a):
    lines = [headline(c, a), ""]
    lines += detail_lines(c, a)
    if a["kind"] == "price_move" and a["release_id"]:
        lines.append("")
        lines.append("This is a lowest asking price, not a valuation and not a sale.")
    return "\n".join(lines)


def _deliver_email(c, a):
    host = model.norm(db.get(c, "smtp_host", ""), 200)
    to = model.norm(db.get(c, "smtp_to", ""), 300)
    if not host or not to:
        return False, "No mail server or no address configured."
    msg = EmailMessage()
    msg["Subject"] = f"[{model.APP_NAME}] {headline(c, a)}"[:200]
    msg["From"] = model.norm(db.get(c, "smtp_from", ""), 200) or to
    msg["To"] = to
    msg.set_content(_body(c, a))
    _smtp_send(host, db.get_int(c, "smtp_port", 587),
               model.norm(db.get(c, "smtp_user", ""), 200), db.get(c, "smtp_pass", ""),
               db.get_bool(c, "smtp_tls"), msg)
    return True, f"Sent to {to}"


def _deliver_webhook(c, a):
    url = model.norm(db.get(c, "webhook_url", ""), 500)
    if not url.startswith("http"):
        return False, "No webhook URL configured."
    payload = {
        "app": model.APP_NAME, "kind": a["kind"], "raised_at": a["raised_at"],
        "title": headline(c, a), "message": _body(c, a),
        # Each service reads its own field and ignores the rest, so all are sent:
        # ntfy reads title and message, Slack reads text, and Discord refuses a
        # post with no content (capped at its 2,000 characters).
        "topic": "", "text": headline(c, a), "content": _body(c, a)[:1900],
    }
    status, _ = _http_post(url, payload)
    return (200 <= status < 300), f"HTTP {status}"


def deliver_pending(c, limit=50):
    """Send every alert not yet delivered on every channel. [(id, results)].

    Keyed per channel on there being no successful delivery row, so a failed
    send is retried next time and a successful one is never repeated — which is
    what makes it safe to run the poller as often as you like. Per channel and
    not per alert: with email and a webhook both set up, the webhook getting
    through must not mean the email that bounced is never tried again.
    """
    chans = channels_ready(c)
    if not chans:
        return []
    pending = {}
    for ch in chans:
        for r in c.execute("""SELECT a.* FROM alert a WHERE a.dismissed_at IS NULL
                              AND NOT EXISTS (SELECT 1 FROM delivery d WHERE d.alert_id=a.id
                                              AND d.channel=? AND d.ok=1)
                              ORDER BY a.raised_at, a.id LIMIT ?""", (ch, limit)).fetchall():
            pending.setdefault(r["id"], (r, []))[1].append(ch)
    order = sorted(pending.values(), key=lambda p: (p[0]["raised_at"], p[0]["id"]))[:limit]
    return [(r["id"], deliver(c, r, chs)) for r, chs in order]
