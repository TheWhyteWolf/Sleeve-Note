#!/usr/bin/env python3
"""The scheduled half of Sleevenote: look prices up, sweep artists, send alerts.

    python3 poller.py                  # the lot, respecting every limit
    python3 poller.py --only prices    # prices, artists, alerts or deliver
    python3 poller.py --limit 20       # at most 20 price lookups this run
    python3 poller.py --dry-run        # say what it would do, touch nothing
    python3 poller.py --force          # sweep every watched artist in full

Run it from cron, or a systemd timer, or by hand. Once a day is plenty:

    0 7 * * *  cd /path/to/sleeve-note && /usr/bin/python3 poller.py --quiet

Nothing here does anything the app cannot also do from a button — this exists so
that it happens without anybody remembering. Three rules it keeps:

**It asks for as little as it can.** A price already looked at inside
`price_max_age_hours` is not asked about again, and an artist whose catalogue has
not changed size costs exactly one request. Both sources publish a rate limit and
`sources.py` measures against the request log in the database, so running this
twice in a minute wastes nothing and breaks no limit.

**It stops when told to slow down.** A 429 or a 503 ends the price sweep for this
run rather than pressing on. There is always tomorrow, and a source that has
asked for quiet gets it.

**It waits its turn rather than giving up.** A page load declines any wait over
`sources.MAX_WAIT` (four seconds), because a person is watching a spinner. Nobody
is watching this, so it waits up to BATCH_MAX_WAIT instead. Without that, Discogs'
25-a-minute limit would end every run after about twenty-two prices and decline
the rest — still inside the limit, but leaving most of a shelf unpriced.

**Polling and judging are separate.** Looking a price up records an observation.
Deciding whether that is worth telling anybody about is `alerts.evaluate_prices`,
which reads what is already stored and asks the internet for nothing — so you can
change a threshold and re-judge the whole shelf instantly, without spending a
single request.
"""
from __future__ import annotations
import argparse
import sys

import alerts as alerts_mod
import db
import library
import model
import prices
import sources

# The longest one request may wait for a rate limit to clear in a batch run: a
# little over the one-minute window both sources count requests in.
BATCH_MAX_WAIT = 65.0


def say(quiet, *a):
    if not quiet:
        print(*a)


def fail(*a):
    """A failure is printed even with --quiet: that is the one thing --quiet keeps."""
    print(*a)


def do_prices(c, limit, dry, quiet):
    """Look up every tracked, linked release that is due. Returns how many."""
    max_age = db.get_int(c, "price_max_age_hours", 20)
    due = library.due_for_price(c, max_age, limit=limit)
    say(quiet, f"prices: {len(due)} due (not looked at for {max_age}h)")
    got = 0
    for rel in due:
        if dry:
            say(quiet, f"  would ask about {rel['artist_credit']} — {rel['title']}")
            continue
        obs, err = sources.price_now(c, dict(rel))
        if err:
            fail(f"  {rel['title'][:40]}: {err}")
            if "slow down" in err or "Offline" in err or "asked enough" in err:
                fail("  stopping the price sweep for this run.")
                break
            continue
        if not obs:
            continue
        prices.record(c, rel["id"], obs)
        c.execute("UPDATE release SET last_priced_at=? WHERE id=?",
                  (model.now_iso(), rel["id"]))
        c.commit()
        got += 1
        n = obs.get("num_for_sale")
        say(quiet, "  {}: {}{}".format(
            rel["title"][:40], model.money(obs["amount"], obs["currency"]),
            " ({} for sale)".format(n) if n else ""))
    return got


def do_artists(c, dry, quiet, force=False):
    """Sweep every watched artist. Returns how many new things were found."""
    watched = c.execute("""SELECT * FROM artist WHERE watch=1 AND mbid IS NOT NULL
                           ORDER BY COALESCE(last_checked_at,'')""").fetchall()
    say(quiet, f"artists: {len(watched)} watched")
    total = 0
    for a in watched:
        if dry:
            say(quiet, f"  would check {a['name']}")
            continue
        raised, note = alerts_mod.sweep_artist(c, a, force=force)
        total += len(raised)
        if note:
            say(quiet, f"  {a['name']}: {note}")
        elif raised:
            say(quiet, f"  {a['name']}: {len(raised)} new")
    return total


def do_judge(c, dry, quiet):
    if dry:
        say(quiet, "alerts: would weigh every followed record against its rule")
        return 0
    raised = alerts_mod.evaluate_prices(c)
    say(quiet, f"alerts: {len(raised)} price move(s) worth reporting")
    return len(raised)


def do_deliver(c, dry, quiet):
    chans = alerts_mod.channels_ready(c)
    if not chans:
        say(quiet, "deliver: nothing configured — alerts stay in the app")
        return 0
    if dry:
        say(quiet, f"deliver: would send undelivered alerts by {', '.join(chans)}")
        return 0
    out = alerts_mod.deliver_pending(c)
    for aid, results in out:
        for ch, ok, detail in results:
            if ok:
                say(quiet, f"  alert {aid} by {ch}: sent")
            else:
                fail(f"  alert {aid} by {ch}: FAILED — {detail}")
    return len(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Sleevenote's scheduled half.")
    ap.add_argument("--only", choices=("prices", "artists", "alerts", "deliver"),
                    help="do one job instead of all four")
    ap.add_argument("--limit", type=int, default=60, help="most price lookups in one run")
    ap.add_argument("--dry-run", action="store_true", help="say what it would do, change nothing")
    ap.add_argument("--force", action="store_true", help="sweep artists in full, ignoring the count probe")
    ap.add_argument("--quiet", action="store_true", help="print nothing unless something failed")
    a = ap.parse_args(argv)

    sources.MAX_WAIT = BATCH_MAX_WAIT
    c = db.init_db()
    try:
        if db.get_bool(c, "offline") and a.only != "deliver":
            print("Offline mode is on in Settings. Nothing was asked of the internet.")
            return 0
        jobs = [a.only] if a.only else ["prices", "artists", "alerts", "deliver"]
        if "prices" in jobs:
            do_prices(c, a.limit, a.dry_run, a.quiet)
        if "artists" in jobs:
            do_artists(c, a.dry_run, a.quiet, a.force)
        if "alerts" in jobs:
            do_judge(c, a.dry_run, a.quiet)
        if "deliver" in jobs:
            do_deliver(c, a.dry_run, a.quiet)
        if not a.dry_run:
            # The pages that depend on this running say whether it ever has: a
            # followed price with nothing polling it is a promise that never fires.
            db.put(c, "poller_ran_at", model.now_iso())
        c.commit()
    finally:
        c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
