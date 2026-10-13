#!/usr/bin/env python3
"""Build a demonstration collection, in its OWN file, so the app has something
to show before you have scanned anything.

    python3 demo.py                 # writes demo.db beside the code
    python3 demo.py --out /tmp/x.db
    SN_DATA=. python3 demo.py --run # print how to serve it

It never touches `app.db`. That is the whole reason this is a separate script
rather than a seed: a collection is somebody's records, and an app that quietly
puts eight invented ones on your shelf has damaged the only copy of the data it
exists to look after.

**Everything in here is invented, and it says so.** The records and barcodes are
real — you can scan these sleeves and get the same answer — but every grade is
made up and every price is made up. So no price in the demo is attributed to
Discogs or to MusicBrainz: they are all `source='manual'` and every one carries a
note saying it is demonstration data. A fake number wearing a real source's name
is exactly the kind of thing the rest of this app is built to refuse.
"""
from __future__ import annotations
import argparse
import datetime
import os
import random
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import db          # noqa: E402
import model       # noqa: E402
import prices      # noqa: E402
import alerts as alerts_mod   # noqa: E402

NOTE = "demonstration data — invented, not observed"

# Real editions, with their real barcodes. The grades and prices that follow are
# not real and are marked as such.
SHELF = [
    dict(title="Kind Of Blue", artist="Miles Davis", released="2010", country="Europe",
         label="Music On Vinyl", catno="MOVLP 019", fmt="2xLP",
         barcode="0886976389413", edition="LP, Album, Reissue, 180 Gram, Gatefold",
         media=4, sleeve=3, note="Faint ring wear. Plays clean throughout.",
         paid=24.00, start=31.0, drift=0.4, end=None),
    dict(title="Blue Train", artist="John Coltrane", released="2014", country="Europe",
         label="Blue Note", catno="B0021284-01", fmt="LP",
         barcode="0602537871681", edition="LP, Album, Reissue, 180 Gram",
         media=5, sleeve=4, note="Bought sealed, opened and played twice.",
         paid=19.50, start=22.0, drift=0.3, end=None),
    dict(title="Rumours", artist="Fleetwood Mac", released="2011", country="Europe",
         label="Warner Bros. Records", catno="RH1 3010", fmt="LP",
         barcode="0081227971502", edition="LP, Album, Reissue, Remastered",
         media=3, sleeve=2, note="Charity shop. Seam split along the bottom, taped inside.",
         paid=4.00, start=18.0, drift=0.5, end=None),
    dict(title="The Dark Side Of The Moon", artist="Pink Floyd", released="2016",
         country="Europe", label="Pink Floyd Records", catno="PFRLP8", fmt="LP",
         barcode="0190295996970", edition="LP, Album, Reissue, Remastered, 180 Gram, Gatefold",
         media=5, sleeve=5, note="Still sealed — this one is for keeping.",
         sealed=True, paid=21.00, start=23.0, drift=0.3, end=None),
    dict(title="Unknown Pleasures", artist="Joy Division", released="2015", country="UK",
         label="Factory", catno="FACT 10", fmt="LP",
         barcode="0825646183869", edition="LP, Album, Reissue, Remastered, 180 Gram",
         media=4, sleeve=4, note="", paid=18.00, start=20.0, drift=0.4,
         # the one that moves, so the alert machinery has something to show
         end=34.0),
    dict(title="Ágætis Byrjun", artist="Sigur Rós", released="2019", country="Europe",
         label="Krunk", catno="KRUNK37", fmt="2xLP",
         barcode="0855555000371", edition="2xLP, Album, Reissue, 180 Gram",
         media=None, sleeve=4, ungraded="not_played",
         note="Second-hand, graded by eye only — not played yet.",
         paid=28.00, start=30.0, drift=0.6, end=None),
    dict(title="A Love Supreme", artist="John Coltrane", released="2015", country="Europe",
         label="Impulse!", catno="0602547311610", fmt="LP",
         barcode="0602547311610", edition="LP, Album, Reissue, Remastered, 180 Gram",
         media=4, sleeve=4, note="", paid=17.50, start=21.0, drift=0.4,
         # and one that falls, because a fall is as ordinary as a rise
         end=13.5),
]


def build(path, seed=17):
    rng = random.Random(seed)
    if os.path.exists(path):
        os.remove(path)
    for tail in ("-wal", "-shm"):
        if os.path.exists(path + tail):
            os.remove(path + tail)
    c = db.init_db(path)
    now = model.parse_iso(model.now_iso())
    cur_code = db.get(c, "currency", "GBP")

    for row in SHELF:
        aid = c.execute("SELECT id FROM artist WHERE name=?", (row["artist"],)).fetchone()
        if aid:
            aid = aid["id"]
        else:
            aid = c.execute("""INSERT INTO artist(name,watch,added_at) VALUES(?,0,?)""",
                            (row["artist"], model.now_iso())).lastrowid
        rid = c.execute("""INSERT INTO release(title,artist_credit,artist_id,barcode,released,
                country,label,catno,format,speed,edition_note,identified,source,tracked,
                notes,added_at)
                VALUES(?,?,?,?,?,?,?,?,?,'33',?,'chosen','manual',1,?,?)""",
            (row["title"], row["artist"], aid, row["barcode"], row["released"],
             row["country"], row["label"], row["catno"], row["fmt"], row["edition"],
             NOTE, model.now_iso())).lastrowid
        cid = c.execute("""INSERT INTO copy(release_id,acquired_on,acquired_from,location,
                notes,added_at) VALUES(?,?,?,?,?,?)""",
            (rid, (now - datetime.timedelta(days=rng.randint(40, 900))).date().isoformat(),
             rng.choice(["a record fair", "the shop on the corner", "a charity shop",
                         "eBay", "a friend clearing out"]),
             rng.choice(["shelf A", "shelf B", "the crate by the door"]),
             NOTE, model.now_iso())).lastrowid
        c.execute("""INSERT INTO grading(copy_id,graded_on,media_grade,media_ungraded,
                sleeve_grade,sleeve_ungraded,note,graded_at) VALUES(?,?,?,?,?,NULL,?,?)""",
            (cid, model.today_iso(), row.get("media"),
             ("sealed" if row.get("sealed") else row.get("ungraded"))
             if row.get("media") is None else None,
             row["sleeve"], row["note"] or None, model.now_iso()))
        if row.get("paid"):
            prices.record(c, rid, {"source": "manual", "feed": "hand", "kind": "paid",
                                   "currency": cur_code, "amount": row["paid"],
                                   "note": NOTE}, copy_id=cid,
                          observed_at=(now - datetime.timedelta(days=400)
                                       ).replace(microsecond=0).isoformat())
        # Six weeks of a plausible-looking asking price, twice a week. Recorded as
        # entered by hand, because that is what it is: nobody observed these.
        n = 13
        for i in range(n):
            days = (n - 1 - i) * 3
            amount = row["start"] + rng.uniform(-row["drift"], row["drift"])
            if row["end"] is not None and i == n - 1:
                amount = row["end"]
            prices.record(c, rid, {
                "source": "manual", "feed": "hand", "kind": "asking_seen",
                "currency": cur_code, "amount": round(amount, 2),
                "num_for_sale": rng.randint(4, 14) if row["end"] is None else
                                (3 if i == n - 1 and (row["end"] or 0) > row["start"] else 12),
                "note": NOTE},
                observed_at=(now - datetime.timedelta(days=days)
                             ).replace(microsecond=0).isoformat())
    c.commit()
    raised = alerts_mod.evaluate_prices(c)
    counts = {t: c.execute(f"SELECT COUNT(*) n FROM {t}").fetchone()["n"]
              for t in ("release", "copy", "grading", "price_obs", "alert", "artist")}
    c.close()
    return counts, raised


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build a demonstration collection.")
    ap.add_argument("--out", default=os.path.join(BASE, "demo.db"),
                    help="where to write it (default: demo.db beside the code)")
    ap.add_argument("--seed", type=int, default=17)
    a = ap.parse_args(argv)
    out = os.path.abspath(a.out)
    real = os.path.abspath(os.path.join(db.DATA_DIR, "app.db"))
    if out == real:
        print(f"Refusing to write over the real collection at {real}.\n"
              f"Pick another path with --out.")
        return 1
    counts, raised = build(out)
    print(f"\n  Wrote {out}\n")
    print(f"    {counts['release']} records · {counts['copy']} copies · "
          f"{counts['grading']} gradings · {counts['price_obs']} price observations")
    print(f"    {counts['artist']} artists · {len(raised)} price alert(s) raised\n")
    print("  Everything in it is invented and every row says so. The barcodes are real.\n")
    print("  Serve it without touching your own collection:\n")
    print(f"      SN_DATA={os.path.dirname(out)} python3 app.py")
    if os.path.basename(out) != "app.db":
        print(f"\n  (SN_DATA expects the file to be called app.db, so either rename it\n"
              f"   or build it straight into an empty folder:\n\n"
              f"      mkdir -p /tmp/sleevenote-demo\n"
              f"      python3 demo.py --out /tmp/sleevenote-demo/app.db\n"
              f"      SN_DATA=/tmp/sleevenote-demo python3 app.py)")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
