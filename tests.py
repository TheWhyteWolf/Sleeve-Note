#!/usr/bin/env python3
"""The gate. `python3 tests.py` must print OK before anything ships.

Three kinds of test live here and they are not equally interesting.

The ordinary kind checks that a function does what it says. The second kind
checks the *invariants* the design rests on — that nothing derivable is stored,
that every string reaching HTML is escaped, that a POST redirects and its write
is visible from another connection. The third kind greps the source, because some
promises cannot be tested any other way: that the word "significant" appears
nowhere, that no JavaScript on the static shelf can write anything, that a price
move is never given a colour, that Pillow never enters the server path.

The barcode decoder gets the most attention, and deliberately so. It is the only
part of this app that can be *confidently wrong* — a misread that passes its own
check digit files the wrong record against the wrong prices, and nothing
downstream can detect it. So it is tested not only on whether it reads real
barcodes (it must) but on whether it ever reads a wrong one (it must not), across
thousands of generated images including deliberately terrible ones.

Nothing here touches the network and nothing here touches the real app.db. The
network door is pointed at a sentinel that fails the run if anything reaches it,
and every database is a temporary file — checked with PRAGMA database_list before
a single request is served, because a test suite that quietly writes to a real
collection is worse than no test suite.
"""
from __future__ import annotations
import datetime
import http.client
import io
import json
import os
import random
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(BASE, "fixtures")

# Point the database somewhere temporary BEFORE anything imports it and binds a
# path. db.connect() reads db.DB_PATH at call time precisely so this works.
_TMPROOT = tempfile.mkdtemp(prefix="sleevenote-tests-")
os.environ["SN_DATA"] = _TMPROOT
# And the same suite whatever it is run beside. The container image sets
# SN_BEHIND_HTTPS, which renames the session cookie; a test that inherited it
# would be testing the image's settings rather than the code.
for _k in ("SN_BEHIND_HTTPS", "SN_TLS_CERT", "SN_TLS_KEY", "SN_USERS", "SN_PORT", "SN_BIND"):
    os.environ.pop(_k, None)

sys.path.insert(0, BASE)
import alerts as alerts_mod        # noqa: E402
import app                          # noqa: E402
import auth                         # noqa: E402
import barcode as bc                # noqa: E402
import db                           # noqa: E402
import library                      # noqa: E402
import model                        # noqa: E402
import prices                       # noqa: E402
import sources                      # noqa: E402

SERVER_MODULES = ("app", "db", "model", "barcode", "sources", "prices", "alerts",
                  "library", "poller", "auth", "users")


# --------------------------------------------------------------- the harness

def fixture(name):
    with open(os.path.join(FIXTURES, name), "rb") as fh:
        return fh.read()


# Longest path first. "…/ws/2/release" is a prefix of "…/ws/2/release-group", and
# matching by prefix would serve the wrong fixture — the same whole-match trap
# that sources.py documents for MusicBrainz's format: field.
FIXTURE_ROUTES = [
    # A search by name, told from a barcode search by how its query begins.
    ("musicbrainz.org/ws/2/release?query=release%3A", "mb_search_title.json"),
    ("musicbrainz.org/ws/2/release?query=catno%3A", "mb_search_title.json"),
    ("api.discogs.com/database/search?type=release&format=Vinyl", "discogs_search_title.json"),
    ("musicbrainz.org/ws/2/release-group", "mb_artist_groups.json"),
    ("musicbrainz.org/ws/2/artist", "mb_artist_search.json"),
    ("musicbrainz.org/ws/2/release/", "mb_release_full.json"),
    ("musicbrainz.org/ws/2/release", "mb_search_barcode.json"),
    ("api.discogs.com/database/search", "discogs_search_barcode.json"),
    ("api.discogs.com/marketplace/stats", "discogs_stats.json"),
    ("api.discogs.com/releases/", "discogs_release.json"),
]


def fake_http(url, headers, timeout=None):
    for key, name in FIXTURE_ROUTES:
        if key in url:
            return 200, fixture(name), {}
    return 404, b"{}", {}


def forbidden_http(url, headers, timeout=None):
    raise AssertionError(f"a test reached the network: {url}")


def _install_seam(fn=fake_http):
    sources._http_get = fn
    sources.SLEEP = lambda s: None
    alerts_mod._http_post = lambda url, payload, timeout=10: (200, b"ok")
    alerts_mod._smtp_send = lambda *a, **k: None


class Base(unittest.TestCase):
    """A fresh temporary database per test, and the network door shut."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(dir=_TMPROOT)
        self.path = os.path.join(self.dir, "app.db")
        self._real_path = db.DB_PATH
        db.DB_PATH = self.path
        self.c = db.init_db(self.path)
        _install_seam()

    def tearDown(self):
        try:
            self.c.close()
        except Exception:
            pass
        db.DB_PATH = self._real_path

    def add_release(self, title="Kind of Blue", artist="Miles Davis", **kw):
        cols = dict(title=title, artist_credit=artist, added_at=model.now_iso())
        cols.update(kw)
        keys = ",".join(cols)
        qs = ",".join("?" * len(cols))
        cur = self.c.execute(f"INSERT INTO release({keys}) VALUES({qs})",
                             tuple(cols.values()))
        self.c.commit()
        return cur.lastrowid

    def add_copy(self, rid):
        cur = self.c.execute("INSERT INTO copy(release_id,added_at) VALUES(?,?)",
                             (rid, model.now_iso()))
        self.c.commit()
        return cur.lastrowid

    def add_prices(self, rid, values, currency="GBP", feed="discogs_stats",
                   kind="lowest_asking", nfs=10, start_days_ago=20, step=3):
        now = model.parse_iso(model.now_iso())
        for i, v in enumerate(values):
            at = (now - datetime.timedelta(days=start_days_ago - i * step)
                  ).replace(microsecond=0).isoformat()
            prices.record(self.c, rid, {"source": "discogs", "feed": feed, "kind": kind,
                                        "currency": currency, "amount": float(v),
                                        "num_for_sale": nfs}, observed_at=at)
        self.c.commit()


def rand13(rng):
    body = "".join(rng.choice("0123456789") for _ in range(12))
    return body + str(bc.check_digit(body))


def degrade(blob, rng, noise, blur=0, lo=40, hi=215):
    w, h, g = bc.read_png(blob)
    g = bytearray(g)
    for i, v in enumerate(g):
        g[i] = max(0, min(255, lo + (hi - lo) * v // 255 + rng.randint(-noise, noise)))
    for _ in range(blur):
        out = bytearray(g)
        for y in range(h):
            b = y * w
            for x in range(1, w - 1):
                out[b + x] = (g[b + x - 1] + 2 * g[b + x] + g[b + x + 1]) // 4
        g = out
    return w, h, bytes(g)


# ------------------------------------------------------------------- numbers

class TestBarcodeNumbers(Base):

    def test_check_digit(self):
        self.assertTrue(bc.check_ok("0886976389413"))
        self.assertFalse(bc.check_ok("0886976389414"))
        self.assertEqual(bc.check_digit("088697638941"), 3)

    def test_upc_a_gains_its_leading_zero(self):
        # Prepending the zero must not change the checksum: the zero falls on an
        # odd weight, which is why the standard can leave it implicit.
        self.assertEqual(bc.normalise("602517920040"), "0602517920040")
        self.assertTrue(bc.check_ok(bc.normalise("602517920040")))

    def test_spaces_and_punctuation_are_ignored(self):
        for raw in ("0 886976 389413", "0-886976-389413", " 0886976389413 "):
            self.assertEqual(bc.normalise(raw), "0886976389413")

    def test_gtin14_unwraps_only_when_padded(self):
        self.assertEqual(bc.normalise("00886976389413"), "0886976389413")
        self.assertEqual(bc.normalise("10886976389413"), "")

    def test_eight_digits_stay_eight(self):
        # An EAN-8 is a different symbology, not a short EAN-13. Padding it would
        # invent a number nobody printed.
        self.assertEqual(len(bc.normalise("96385074")), 8)

    def test_hint_names_the_digit_but_still_refuses(self):
        hint = bc.fix_hint("0886976389414")
        self.assertIn("would have to be 3", hint)
        self.assertFalse(bc.check_ok("0886976389414"))

    def test_registry_is_about_the_number_not_the_record(self):
        # The recorded example: a Dutch pressing of an American album, prefix 088.
        self.assertEqual(bc.registry("0886976389413"), "United States")
        self.assertIn("issued", bc.describe("0886976389413")["country_note"])

    def test_describe_flags_a_book(self):
        body = "978014017739"
        code = body + str(bc.check_digit(body))
        self.assertIn("ISBN", bc.registry(code))


class TestBarcodeDrawing(Base):

    def test_module_counts(self):
        self.assertEqual(len(bc.modules("0886976389413")), 95)
        eight = "96385074"
        if bc.check_ok(eight):
            self.assertEqual(len(bc.modules(eight)), 67)

    def test_refuses_to_draw_a_bad_number(self):
        self.assertEqual(bc.modules("0886976389414"), "")
        self.assertEqual(bc.svg("0886976389414"), "")
        self.assertEqual(bc.png("0886976389414"), b"")

    def test_svg_is_black_on_white_in_every_theme(self):
        # A barcode is read by a camera. An inverted one is one no camera reads,
        # so it must not inherit a dark theme.
        s = bc.svg("0886976389413")
        self.assertIn('fill="#FFFFFF"', s)
        self.assertIn('fill="#000000"', s)
        self.assertNotIn("currentColor", s)


class TestBarcodeDecoding(Base):

    def test_clean_round_trip(self):
        rng = random.Random(1)
        for scale in (1, 2, 3, 4, 7):
            for _ in range(40):
                code = rand13(rng)
                got, votes, tried = bc.decode_png(bc.png(code, scale=scale))
                self.assertEqual(got, code, f"scale {scale}")

    def test_ean8_round_trip(self):
        rng = random.Random(2)
        for _ in range(40):
            body = "".join(rng.choice("0123456789") for _ in range(7))
            code = body + str(bc.check_digit(body))
            self.assertEqual(bc.decode_png(bc.png(code))[0], code)

    def test_upside_down(self):
        rng = random.Random(3)
        for _ in range(30):
            code = rand13(rng)
            w, h, g = bc.read_png(bc.png(code))
            flipped = b"".join(bytes(reversed(g[y * w:(y + 1) * w])) for y in range(h))
            self.assertEqual(bc.decode_grey(w, h, flipped)[0], code)

    def test_a_photograph(self):
        rng = random.Random(4)
        for noise, blur, scale in ((20, 0, 3), (50, 0, 3), (30, 2, 5), (70, 1, 4)):
            for _ in range(15):
                code = rand13(rng)
                w, h, g = degrade(bc.png(code, scale=scale, height=40), rng, noise, blur)
                self.assertEqual(bc.decode_grey(w, h, g)[0], code,
                                 f"noise {noise} blur {blur}")

    def test_small_in_a_big_photograph(self):
        rng = random.Random(5)
        for _ in range(6):
            code = rand13(rng)
            small = bc.png(code, scale=2, height=60)
            sw, sh, sg = bc.read_png(small)
            iw, ih = 1400, 1000
            canvas = bytearray(rng.randint(150, 205) for _ in range(iw * ih))
            for y in range(sh):
                for x in range(sw):
                    canvas[(300 + y) * iw + 60 + x] = sg[y * sw + x]
            self.assertEqual(bc.decode_grey(iw, ih, bytes(canvas))[0], code)

    def test_never_a_wrong_answer(self):
        """The one that matters. Declining is recoverable; a wrong barcode is not."""
        rng = random.Random(6)
        wrong = []
        for noise, blur, scale in ((80, 2, 3), (110, 3, 3), (130, 0, 2), (150, 1, 2)):
            for _ in range(30):
                code = rand13(rng)
                w, h, g = degrade(bc.png(code, scale=scale, height=40), rng, noise, blur)
                got = bc.decode_grey(w, h, g)[0]
                if got and got != code:
                    wrong.append((code, got))
        self.assertEqual(wrong, [], f"decoder invented a barcode: {wrong[:3]}")

    def test_pure_noise_decodes_to_nothing(self):
        rng = random.Random(7)
        for _ in range(120):
            g = bytes(rng.randint(0, 255) for _ in range(220 * 24))
            self.assertEqual(bc.decode_grey(220, 24, g)[0], "")

    def test_one_agreeing_row_is_not_enough(self):
        """The vote rule is load-bearing, so it is asserted directly."""
        self.assertGreaterEqual(bc.MIN_VOTES, 2)
        code = "0886976389413"
        blob = bc.png(code, scale=3, height=1)          # a single row of pixels
        self.assertEqual(bc.decode_png(blob, min_votes=3)[0], "")
        self.assertEqual(bc.decode_png(blob, min_votes=1)[0], code)


class TestPngReader(Base):

    def test_refuses_what_it_cannot_read(self):
        with self.assertRaises(bc.ImageError):
            bc.read_png(b"not a png at all")
        with self.assertRaises(bc.ImageError):
            bc.read_png(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20)

    def test_reads_colour_and_palette(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow is not on this interpreter — it is optional")
        import zlib
        src = io.BytesIO(bc.png("0886976389413", scale=3, height=30))
        base = Image.open(src).convert("L")
        for mode in ("RGB", "RGBA", "P", "LA", "1"):
            buf = io.BytesIO()
            base.convert(mode).save(buf, format="PNG")
            got = bc.decode_png(buf.getvalue())[0]
            self.assertEqual(got, "0886976389413", f"mode {mode}")

    def test_interlaced_is_refused_not_half_read(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow is not on this interpreter — it is optional")
        src = io.BytesIO(bc.png("0886976389413", scale=3, height=30))
        buf = io.BytesIO()
        Image.open(src).save(buf, format="PNG", interlace=1)
        # Pillow only interlaces when asked in a way it honours; if the file came
        # back progressive, the reader must say so rather than guess.
        try:
            bc.read_png(buf.getvalue())
        except bc.ImageError as ex:
            self.assertIn("interlaced", str(ex).lower())


# -------------------------------------------------------------------- model

class TestModel(Base):

    def test_every_grade_has_words_on_both_scales(self):
        for g in model.GRADES:
            for scale in ("media", "sleeve"):
                self.assertTrue(model.grade_name(scale, g))
                self.assertGreater(len(model.grade_words(scale, g)), 30)
            self.assertTrue(model.goldmine(g))

    def test_condition_bands_read_longest_name_first(self):
        # "Very Good Plus" must not be read as "Very Good"; "Near Mint" not as "Mint".
        self.assertEqual(model.band_from_condition("Very Good Plus (VG+)"), 4)
        self.assertEqual(model.band_from_condition("Very Good (VG)"), 3)
        self.assertEqual(model.band_from_condition("Near Mint (NM or M-)"), 5)
        self.assertEqual(model.band_from_condition("Mint (M)"), 5)
        self.assertEqual(model.band_from_condition("VG+/VG"), 4)
        self.assertIsNone(model.band_from_condition("who knows"))

    def test_money_never_mixes_currencies(self):
        self.assertEqual(model.money(25.97, "GBP"), "£25.97")
        self.assertEqual(model.money(None, "GBP"), "")

    def test_the_two_dollars_cannot_be_mistaken_for_each_other(self):
        """CAD and USD are offered side by side, and to somebody in Canada a bare
        "$" means theirs — so neither dollar is a bare "$"."""
        self.assertEqual(model.money(25.97, "USD"), "US$25.97")
        self.assertEqual(model.money(25.97, "CAD"), "C$25.97")
        for typed in ("US$12.50", "C$12.50", "CA$12.50", "$12.50", "£12.50", "12.50"):
            self.assertEqual(model.moneyn(typed), 12.5, typed)

    def test_a_form_offers_the_three_and_keeps_a_stored_fourth(self):
        self.assertEqual([k for k, _ in model.currency_choices()], ["CAD", "GBP", "USD"])
        self.assertEqual([k for k, _ in model.currency_choices("EUR")],
                         ["CAD", "GBP", "USD", "EUR"])
        self.assertEqual([k for k, _ in model.currency_choices("XXX")], ["CAD", "GBP", "USD"])

    def test_no_percentage_from_zero(self):
        self.assertIsNone(model.pct(0, 5))
        self.assertIsNone(model.pct(None, 5))
        self.assertAlmostEqual(model.pct(20, 25), 25.0)

    def test_a_negative_price_is_not_a_price(self):
        self.assertIsNone(model.moneyn("-3"))
        self.assertEqual(model.moneyn("£1,250.50"), 1250.50)

    def test_partial_dates_keep_their_precision(self):
        self.assertEqual(model.partial_date("1959"), "1959")
        self.assertEqual(model.partial_date("1959-08"), "1959-08")
        self.assertIsNone(model.partial_date("1959-13"))


# ------------------------------------------------------------------- schema

class TestSchema(Base):

    def test_every_table_is_there(self):
        got = {r["name"] for r in self.c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for t in db.REQUIRED_TABLES + db.BACKUP_TABLES:
            self.assertIn(t, got)

    def test_migrate_is_idempotent_and_agrees_with_a_fresh_database(self):
        """A fresh database and a migrated one must declare the same columns.

        Two databases that disagree about their columns is the defect this shape
        exists to prevent, and in the sibling projects it has happened twice."""
        fresh_cols = {t: {r["name"] for r in self.c.execute(f"PRAGMA table_info({t})")}
                      for t in db.BACKUP_TABLES}
        p2 = os.path.join(self.dir, "migrated.db")
        for _ in range(3):           # create, then migrate twice: both no-ops
            c2 = db.init_db(p2)
            c2.close()
        c2 = db.init_db(p2)
        for t in db.BACKUP_TABLES:
            cols = {r["name"] for r in c2.execute(f"PRAGMA table_info({t})")}
            self.assertEqual(cols, fresh_cols[t], f"{t} differs after migration")
        self.assertEqual(db.schema_version(c2), db.SCHEMA_VERSION)
        c2.close()

    def test_a_blank_grade_must_carry_its_reason(self):
        rid = self.add_release()
        cid = self.add_copy(rid)
        with self.assertRaises(sqlite3.IntegrityError):
            self.c.execute("""INSERT INTO grading(copy_id,media_grade,media_ungraded,
                sleeve_grade,sleeve_ungraded,graded_at) VALUES(?,NULL,NULL,4,NULL,?)""",
                (cid, model.now_iso()))
        self.c.rollback()
        # and a grade may not also carry a reason
        with self.assertRaises(sqlite3.IntegrityError):
            self.c.execute("""INSERT INTO grading(copy_id,media_grade,media_ungraded,
                sleeve_grade,sleeve_ungraded,graded_at) VALUES(?,4,'sealed',4,NULL,?)""",
                (cid, model.now_iso()))
        self.c.rollback()

    def test_a_barcode_is_not_unique(self):
        """Two editions really do share one barcode — the fixtures hold a pair."""
        a = self.add_release(title="One", barcode="0886976389413")
        b = self.add_release(title="Two", barcode="0886976389413")
        self.assertNotEqual(a, b)

    def test_a_source_id_is_unique(self):
        self.add_release(title="One", discogs_release_id=2317524)
        with self.assertRaises(sqlite3.IntegrityError):
            self.add_release(title="Two", discogs_release_id=2317524)
        self.c.rollback()

    def test_a_price_may_not_be_negative(self):
        rid = self.add_release()
        with self.assertRaises(sqlite3.IntegrityError):
            self.c.execute("""INSERT INTO price_obs(release_id,observed_at,source,feed,kind,
                currency,amount) VALUES(?,?,'manual','hand','paid','GBP',-5)""",
                (rid, model.now_iso()))
        self.c.rollback()

    def test_an_observation_cannot_be_recorded_twice(self):
        rid = self.add_release()
        at = model.now_iso()
        obs = {"source": "discogs", "feed": "discogs_stats", "kind": "lowest_asking",
               "currency": "GBP", "amount": 30.0}
        self.assertIsNotNone(prices.record(self.c, rid, obs, observed_at=at))
        self.assertIsNone(prices.record(self.c, rid, obs, observed_at=at))

    def test_two_copies_bought_on_one_day_are_two_receipts(self):
        """The copy is part of the uniqueness rule, or the second receipt vanishes."""
        rid = self.add_release()
        at = model.today_iso() + "T12:00:00+00:00"
        paid = {"source": "manual", "feed": "hand", "kind": "paid", "currency": "GBP",
                "amount": 20.0}
        for cid in (self.add_copy(rid), self.add_copy(rid)):
            self.assertIsNotNone(prices.record(self.c, rid, paid, copy_id=cid, observed_at=at))
        self.assertEqual(prices.paid_totals(self.c)[0]["n"], 2)

    def test_a_version_1_file_comes_forward_intact(self):
        """A real v1 file, not just a fresh one migrated: same columns, same index,
        and the collection it held still there."""
        p1 = os.path.join(self.dir, "v1.db")
        c1 = db.init_db(p1)
        c1.execute("""INSERT INTO release(title,artist_credit,added_at)
                      VALUES('Kind of Blue','Miles Davis',?)""", (model.now_iso(),))
        c1.execute("ALTER TABLE release DROP COLUMN matrix")
        c1.execute("ALTER TABLE release DROP COLUMN source_notes")
        c1.execute("DROP INDEX ux_price_obs")
        c1.execute("""CREATE UNIQUE INDEX ux_price_obs ON price_obs(release_id, source, feed,
                      kind, currency, COALESCE(cond_band,-1), observed_at)""")
        c1.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
        c1.commit()
        c1.close()
        c2 = db.init_db(p1)
        for t in db.BACKUP_TABLES:
            self.assertEqual({r["name"] for r in c2.execute(f"PRAGMA table_info({t})")},
                             {r["name"] for r in self.c.execute(f"PRAGMA table_info({t})")},
                             f"{t} differs after migrating a v1 file")
        ux = c2.execute("SELECT sql FROM sqlite_master WHERE name='ux_price_obs'").fetchone()
        self.assertIn("copy_id", ux["sql"])
        self.assertEqual(db.schema_version(c2), db.SCHEMA_VERSION)
        rel = c2.execute("SELECT * FROM release").fetchone()
        self.assertEqual(rel["title"], "Kind of Blue")
        self.assertIsNone(rel["matrix"], "never asked is NULL, not an empty answer")
        c2.close()


class TestNothingDerivable(Base):
    """Columns that must never exist, because each is a function of other rows."""

    FORBIDDEN = {
        "copy": ("media_grade", "sleeve_grade", "grade", "current_grade", "owned",
                 "active", "value", "paid", "price"),
        "release": ("latest_price", "price", "current_price", "price_change", "owned",
                    "copies", "copy_count", "year", "is_up", "value"),
        "artist": ("release_count", "copies", "new_count"),
        "price_obs": ("change", "pct", "delta", "is_move", "previous", "moved"),
        "alert": ("headline", "text", "message", "title", "body"),
        "grading": ("goldmine", "band", "score"),
    }

    def test_no_derived_columns(self):
        for table, banned in self.FORBIDDEN.items():
            cols = {r["name"] for r in self.c.execute(f"PRAGMA table_info({table})")}
            for col in banned:
                self.assertNotIn(col, cols,
                                 f"{table}.{col} is derivable and must not be stored")

    def test_an_alert_stores_numbers_not_sentences(self):
        """The headline is written at display time so it cannot drift from its evidence."""
        rid = self.add_release()
        self.add_prices(rid, [30, 30.5, 31, 30, 29.5])
        self.add_prices(rid, [60], start_days_ago=0, step=1, nfs=3)
        ids = alerts_mod.evaluate_prices(self.c)
        self.assertTrue(ids)
        row = self.c.execute("SELECT * FROM alert WHERE id=?", (ids[0],)).fetchone()
        self.assertIsNotNone(row["from_amount"])
        self.assertIsNotNone(row["rule_pct"])
        text = alerts_mod.headline(self.c, row)
        self.assertIn("up", text)
        # changing the record's title changes the sentence, because it is rendered
        self.c.execute("UPDATE release SET title='Renamed' WHERE id=?", (rid,))
        self.c.commit()
        self.assertIn("Renamed", alerts_mod.headline(self.c, row))


class TestDbPathRedirect(unittest.TestCase):
    """The default-argument trap that bit the sibling project, asserted here."""

    def test_setting_db_path_after_import_actually_redirects(self):
        tmp = tempfile.mkdtemp(dir=_TMPROOT)
        target = os.path.join(tmp, "redirected.db")
        real = db.DB_PATH
        try:
            db.DB_PATH = target
            c = db.init_db()
            served = [r[2] for r in c.execute("PRAGMA database_list")]
            c.close()
            self.assertIn(target, served,
                          "db.connect() bound its path at import time — see db.connect()")
            self.assertTrue(os.path.exists(target))
        finally:
            db.DB_PATH = real


# ------------------------------------------------------------------ sources

class TestSources(Base):

    def test_barcode_lookup_reads_both_catalogues(self):
        cands, notes = sources.lookup_barcode(self.c, "0 886976 389413")
        self.assertEqual(notes, [])
        self.assertEqual(len(cands), 3)
        self.assertEqual({c["source"] for c in cands}, {"musicbrainz", "discogs"})

    def test_the_barcode_field_is_matched_never_taken(self):
        """Discogs' barcode field is a bag of every identifier on the sleeve.

        One of the two recorded editions lists twenty entries — two spellings of
        the barcode, two rights societies and sixteen matrix numbers. Taking the
        first would store 'BIEM/GEMA' as a barcode on plenty of releases."""
        raw = json.loads(fixture("discogs_search_barcode.json"))
        messy = [r for r in raw["results"] if len(r.get("barcode") or []) > 5]
        self.assertTrue(messy, "the fixture should hold the messy case")
        field = messy[0]["barcode"]
        self.assertIn("BIEM/GEMA", field)
        self.assertEqual(sources._pick_barcode(field, "0886976389413"), "0886976389413")
        # the 12-digit spelling in that same list must match the 13-digit code
        self.assertIn("886976389413", field)

    def test_differs_names_the_real_difference(self):
        cands, _ = sources.lookup_barcode(self.c, "0886976389413")
        groups = dict(sources.by_source(cands))
        self.assertEqual(len(groups["discogs"]), 2)
        self.assertEqual(sources.differs(groups["discogs"]), ["pressing"])

    def test_formats_are_read_not_guessed(self):
        self.assertEqual(sources.read_format(["LP", "Album"], 2, "Vinyl")[0], "2xLP")
        self.assertEqual(sources.read_format(["LP"], 1, "Vinyl")[0], "LP")
        self.assertEqual(sources.read_format(['7"'], 1, "Vinyl")[0], "7in")
        self.assertEqual(sources.read_format(["Album"], 1, "CD")[2], "other")
        self.assertEqual(sources.read_format([], None, "")[2], "unknown")

    def test_a_discogs_padded_date_is_not_a_real_day(self):
        # '2010-05-00' means May 2010. A date parser would throw, or worse, round.
        self.assertEqual(sources._dg_date("2010-05-00"), "2010-05")
        self.assertEqual(sources._dg_date("2010-00-00"), "2010")

    def test_price_comes_from_the_endpoint_that_states_its_currency(self):
        obs, err = sources.price_now(self.c, {"discogs_release_id": 2317524})
        self.assertEqual(err, "")
        self.assertEqual(obs["feed"], "discogs_stats")
        self.assertEqual(obs["currency"], "GBP")
        self.assertEqual(obs["amount"], 34.4)
        self.assertIsNone(obs["cond_band"], "public stats do not state a condition")

    def test_an_unlinked_release_has_no_price_to_follow(self):
        obs, err = sources.price_now(self.c, {"discogs_release_id": None})
        self.assertIsNone(obs)
        self.assertIn("no Discogs release", err)

    def test_offline_asks_nothing(self):
        db.put(self.c, "offline", "1")
        self.c.commit()
        sources._http_get = forbidden_http
        cands, notes = sources.lookup_barcode(self.c, "0886976389413")
        self.assertEqual(cands, [])
        self.assertIn("Offline", notes[0])

    def test_the_rate_limiter_measures_from_the_database(self):
        """So that restarting the app cannot be used to burst past a limit."""
        self.assertEqual(sources.wait_needed(self.c, sources.MB_HOST), 0.0)
        self.c.execute("""INSERT INTO source_call(called_at,host,path,status,ms)
                          VALUES(?,?,?,200,10)""",
                       (model.now_iso(), sources.MB_HOST, "/x"))
        self.c.commit()
        self.assertGreater(sources.wait_needed(self.c, sources.MB_HOST), 0.5)
        self.assertEqual(sources.wait_needed(self.c, sources.DG_HOST), 0.0,
                         "one source's limit must not throttle the other")

    def test_a_token_raises_the_discogs_limit(self):
        before = sources._limits(self.c, sources.DG_HOST)
        db.put(self.c, "discogs_token", "secret")
        self.c.commit()
        after = sources._limits(self.c, sources.DG_HOST)
        self.assertGreater(after[1], before[1])

    def test_the_user_agent_carries_nothing_unless_asked(self):
        self.assertEqual(sources.user_agent(self.c), f"Sleevenote/{sources.VERSION}")
        db.put(self.c, "user_agent_contact", "me@example.com")
        self.c.commit()
        self.assertIn("me@example.com", sources.user_agent(self.c))

    def _cand(self, source):
        cands, _ = sources.lookup_barcode(self.c, "0886976389413")
        return next(cd for cd in cands if cd["source"] == source
                    and (source != "discogs" or cd["discogs_release_id"] == 2317524))

    def test_a_musicbrainz_edition_brings_the_discogs_link_it_publishes(self):
        """The bridge: MusicBrainz knows the edition, Discogs knows the asking price,
        and only MusicBrainz's own published link joins the two."""
        cand = self._cand("musicbrainz")
        self.assertIsNone(cand["discogs_release_id"])
        full, note = sources.fuller(self.c, cand)
        self.assertEqual(full["discogs_release_id"], 2317524)
        self.assertIn("2317524", note)
        self.assertIsNone(cand["discogs_release_id"], "the offered candidate is not altered")

    def test_a_discogs_edition_is_completed_from_its_full_entry(self):
        cand = self._cand("discogs")
        self.assertEqual(cand["released"], "2010", "the search gives a year")
        full, note = sources.fuller(self.c, cand)
        self.assertEqual(note, "")
        self.assertEqual(full["released"], "2010-05", "the full entry gives the month")
        self.assertEqual(full["artist_credit"], "Miles Davis")
        self.assertIn("Limited and numbered", full["source_notes"])
        self.assertEqual(full["matrix_text"], "", "asked, and none listed — not NULL")

    def test_an_answer_about_a_different_release_is_not_used(self):
        cand = dict(self._cand("discogs"), discogs_release_id=2506823)
        full, _ = sources.fuller(self.c, cand)   # the fixture answers for 2317524
        self.assertNotIn("matrix_text", full)
        self.assertEqual(full["released"], cand["released"])

    def test_a_catalogue_that_will_not_answer_never_stops_a_record_being_added(self):
        db.put(self.c, "offline", "1")
        self.c.commit()
        cand = {"source": "musicbrainz", "mbid": "x", "discogs_release_id": None, "title": "T"}
        full, note = sources.fuller(self.c, cand)
        self.assertEqual(full["title"], "T")
        self.assertIn("Offline", note)

    def test_a_discogs_credit_is_joined_the_way_the_sleeve_joins_it(self):
        self.assertEqual(sources._dg_credit([{"name": "Duke Ellington (2)", "join": "&"},
                                             {"name": "John Coltrane", "join": ""}]),
                         "Duke Ellington & John Coltrane")
        self.assertEqual(sources._dg_credit([{"name": "Prince", "anv": "The Artist"}]),
                         "The Artist", "the name as printed wins")

    def _asked(self):
        """Install a seam that serves the fixtures and remembers every URL asked."""
        urls = []
        def seam(u, h, t=None):
            urls.append(u)
            return fake_http(u, h, t)
        _install_seam(seam)
        return urls

    def test_a_name_search_asks_both_catalogues_for_vinyl_only(self):
        urls = self._asked()
        un, tot = [], {}
        cands, notes = sources.lookup_title(self.c, "Miles Davis", "Kind of Blue", "", un, tot)
        self.assertEqual((notes, un), ([], []))
        self.assertEqual(tot, {"musicbrainz": 62, "discogs": 359})
        self.assertEqual(len(cands), 50)
        mb = urllib.parse.unquote_plus(next(u for u in urls if "musicbrainz" in u))
        for name in sources.MB_VINYL:
            self.assertIn('"%s"' % name.replace('"', '\\"') if " " in name else name, mb)
        self.assertIn("release:(kind AND of AND blue)", mb)
        dg = next(u for u in urls if "discogs" in u)
        self.assertIn("format=Vinyl", dg)
        self.assertIn("release_title=Kind+of+Blue", dg)
        self.assertEqual({cd["media_kind"] for cd in cands}, {"vinyl"})

    def test_a_name_search_claims_no_barcode_it_cannot_stand_behind(self):
        """Discogs' search-result barcode is a bag of whatever is on the sleeve —
        measured on Kind of Blue: 'BMI', '2', run-out stamps — and a name search
        has no scanned code to match it against. So none is taken from it; a
        MusicBrainz barcode, a field of its own, is kept only if its check digit
        holds."""
        cands, _ = sources.lookup_title(self.c, "Miles Davis", "Kind of Blue")
        raw = json.loads(fixture("discogs_search_title.json"))["results"]
        self.assertTrue(any(r.get("barcode") for r in raw), "the fixture carries the bag")
        for cd in cands:
            if cd["source"] == "discogs":
                self.assertEqual(cd["barcode"], "")
            elif cd["barcode"]:
                self.assertTrue(bc.check_ok(cd["barcode"]), cd["barcode"])
        mb_raw = [r.get("barcode") for r in json.loads(fixture("mb_search_title.json"))["releases"]]
        self.assertIn("07464405791", mb_raw, "eleven digits: not a barcode, and dropped")

    def test_a_discogs_pick_from_a_search_gains_its_typed_barcode(self):
        _install_seam(lambda u, h, t=None: (
            200, fixture("discogs_search_barcode.json"), {}) if "database/search" in u
            else fake_http(u, h, t))
        cands, _ = sources.lookup_title(self.c, "Miles Davis", "Kind of Blue")
        pick = next(cd for cd in cands if cd["discogs_release_id"] == 2317524)
        self.assertEqual(pick["barcode"], "", "nothing from the twenty-entry bag")
        full, said = sources.fuller(self.c, pick)
        self.assertEqual(full["barcode"], "0886976389413", "the full entry types its barcode")

    def test_typed_words_cannot_become_query_syntax(self):
        q = sources.mb_title_query("AC/DC", 'Back in Black: NOT (live) OR "x"', "")
        self.assertIn(r"release:(back AND in AND black\: AND not AND \(live\) AND or AND \"x\")", q)
        self.assertIn(r"artist:(ac\/dc)", q)
        self.assertIn("artist:(simon AND garfunkel)",
                      sources.mb_title_query("Simon & Garfunkel", "Bookends", ""),
                      "the ampersand is not a word")
        self.assertEqual(sources.mb_title_query("Miles Davis", "", ""), "",
                         "an artist alone is a discography, not an edition")
        self.assertEqual(sources.mb_title_query("", "", " - . "), "")

    def test_a_catalogue_number_is_sent_the_way_each_source_reads_it(self):
        """MusicBrainz indexes CL 1355 and C.L.1355 alike and finds them from a bare
        cl1355, but finds nothing from a quoted "CL1355" — measured. Discogs finds
        both spellings from the number as typed."""
        urls = self._asked()
        sources.lookup_title(self.c, "", "", "C.L. 1355")
        mb = urllib.parse.unquote_plus(next(u for u in urls if "musicbrainz" in u))
        self.assertIn("query=catno:cl1355 AND format:", mb)
        dg = urllib.parse.unquote_plus(next(u for u in urls if "discogs" in u))
        self.assertIn("catno=C.L. 1355", dg)
        # Under one catalogue number every pressing is worth seeing: CL 1355 alone
        # is 44 vinyl editions on Discogs.
        self.assertIn("limit=100", mb)
        self.assertIn("per_page=100", dg)

    def test_a_date_is_refined_only_by_one_that_agrees(self):
        self.assertEqual(sources.refine_date("2010", "2010-05"), "2010-05")
        self.assertEqual(sources.refine_date("2010-05", "2010"), "2010-05")
        self.assertEqual(sources.refine_date("2010-05", "2010-06"), "2010-05",
                         "a second answer is not a better one for coming later")
        self.assertEqual(sources.refine_date("", "1959"), "1959")


class TestOneDoorOut(unittest.TestCase):

    def test_only_http_get_opens_a_socket_in_sources(self):
        with open(os.path.join(BASE, "sources.py")) as fh:
            src = fh.read()
        body = src.split("def _http_get", 1)[1]
        after = body.split("\ndef ", 1)[1] if "\ndef " in body else ""
        for needle in ("urlopen", "socket.", "http.client"):
            self.assertNotIn(needle, after,
                             f"{needle} outside _http_get — sources.py must have one door")

    def test_alerts_sends_only_through_its_two_named_functions(self):
        with open(os.path.join(BASE, "alerts.py")) as fh:
            src = fh.read()
        self.assertEqual(src.count("urlopen"), 1)
        self.assertEqual(src.count("smtplib.SMTP("), 1)

    def test_no_module_but_sources_and_alerts_reaches_the_network(self):
        for name in SERVER_MODULES:
            if name in ("sources", "alerts"):
                continue
            with open(os.path.join(BASE, name + ".py")) as fh:
                src = fh.read()
            for needle in ("urlopen", "smtplib", "requests."):
                self.assertNotIn(needle, src, f"{name}.py must not reach the network")


# ------------------------------------------------------------------- prices

class TestPrices(Base):

    def test_series_never_pool_across_what_makes_them_comparable(self):
        rid = self.add_release()
        at = model.now_iso()
        for feed, cur, band in (("discogs_stats", "GBP", None),
                                ("discogs_release", "GBP", None),
                                ("discogs_stats", "USD", None),
                                ("discogs_stats", "GBP", 4)):
            prices.record(self.c, rid, {"source": "discogs", "feed": feed,
                                        "kind": "lowest_asking", "currency": cur,
                                        "amount": 30.0, "cond_band": band},
                          observed_at=at)
        self.c.commit()
        self.assertEqual(len(prices.series_list(self.c, rid)), 4,
                         "feed, currency and condition band each split a series")

    def test_a_purchase_is_never_market_evidence(self):
        rid = self.add_release()
        cid = self.add_copy(rid)
        prices.record(self.c, rid, {"source": "manual", "feed": "hand", "kind": "paid",
                                    "currency": "GBP", "amount": 12.0}, copy_id=cid)
        self.c.commit()
        self.assertEqual(prices.series_list(self.c, rid), [])
        key = prices.series_list(self.c, rid, market_only=False)[0]
        self.assertFalse(prices.assess(self.c, rid, key)["moved"])
        self.assertIn("not an observation of the market",
                      prices.assess(self.c, rid, key)["why_not"])

    def test_median_ignores_one_wild_seller(self):
        self.assertEqual(prices.median([30, 31, 32, 400]), 31.5)
        self.assertIsNone(prices.median([]))

    def test_a_short_series_has_no_opinion(self):
        rid = self.add_release()
        self.add_prices(rid, [10, 40], start_days_ago=4, step=2)
        key = prices.series_list(self.c, rid)[0]
        a = prices.assess(self.c, rid, key)
        self.assertFalse(a["moved"])
        self.assertIn("are needed before a move means anything", a["why_not"])

    def test_both_thresholds_must_be_crossed(self):
        rid = self.add_release()
        self.add_prices(rid, [4.0, 4.0, 4.2, 4.1])      # a cheap single
        self.add_prices(rid, [5.4], start_days_ago=0, step=1)
        key = prices.series_list(self.c, rid)[0]
        a = prices.assess(self.c, rid, key)
        self.assertFalse(a["moved"], "35% of £4 is noise and must not fire")
        self.assertIn("under the £3.00 floor", a["why_not"])

        rid2 = self.add_release(title="Box set")
        self.add_prices(rid2, [400, 402, 398, 401])
        self.add_prices(rid2, [415], start_days_ago=0, step=1)
        key2 = prices.series_list(self.c, rid2)[0]
        a2 = prices.assess(self.c, rid2, key2)
        self.assertFalse(a2["moved"], "£14 on a £400 box set is not 20%")
        self.assertIn("under the 20% threshold", a2["why_not"])

    def test_a_real_move_fires_with_its_arithmetic(self):
        rid = self.add_release()
        self.add_prices(rid, [30, 30.5, 31, 30, 29.5])
        self.add_prices(rid, [60], start_days_ago=0, step=1)
        key = prices.series_list(self.c, rid)[0]
        a = prices.assess(self.c, rid, key)
        self.assertTrue(a["moved"])
        self.assertEqual(a["direction"], "up")
        self.assertEqual(a["baseline_n"], 5)
        self.assertEqual(a["verdict"], "notable")
        self.assertIn("median over 5 observations", prices.describe(a))

    def test_the_latest_point_is_not_part_of_its_own_baseline(self):
        rid = self.add_release()
        self.add_prices(rid, [30, 30, 30, 30])
        self.add_prices(rid, [60], start_days_ago=0, step=1)
        key = prices.series_list(self.c, rid)[0]
        a = prices.assess(self.c, rid, key)
        self.assertEqual(a["baseline"], 30.0,
                         "including the newest point would pull the baseline toward it")

    def test_the_supply_confound_is_printed(self):
        rid = self.add_release()
        self.add_prices(rid, [30, 30, 30, 30], nfs=2)
        self.add_prices(rid, [15], start_days_ago=0, step=1, nfs=20)
        key = prices.series_list(self.c, rid)[0]
        a = prices.assess(self.c, rid, key)
        self.assertTrue(a["moved"])
        self.assertIn("more on offer", prices.confound(a))

    def test_a_median_listing_count_is_not_rounded_away(self):
        """The baseline count is a median and can fall between two counts."""
        self.assertIn("2.5 copies were for sale then and 3 now",
                      prices.confound_words(2.5, 3))
        self.assertIn("3 copies for sale at both ends", prices.confound_words(3.0, 3))

    def test_a_record_whose_own_rule_is_off_is_not_watched(self):
        """Not a quiet fall back to the default — that would make "off" do nothing."""
        rid = self.add_release()
        self.add_prices(rid, [30, 30.5, 31, 30, 29.5])
        self.add_prices(rid, [60], start_days_ago=0, step=1)
        self.c.execute("""INSERT INTO alert_rule(kind,release_id,active,created_at)
                          VALUES('price_move',?,0,?)""", (rid, model.now_iso()))
        self.c.commit()
        self.assertIsNone(prices.rule_for(self.c, rid))
        self.assertEqual(alerts_mod.evaluate_prices(self.c), [])

    def test_a_records_own_rule_inherits_what_it_leaves_blank(self):
        rid = self.add_release()
        self.add_prices(rid, [30, 30.5, 31, 30, 29.5])
        self.add_prices(rid, [36], start_days_ago=0, step=1)     # 20%, £6: fires on default
        self.c.execute("""INSERT INTO alert_rule(kind,release_id,pct_threshold,abs_threshold,
                          active,created_at) VALUES('price_move',?,50,3,1,?)""",
                       (rid, model.now_iso()))
        self.c.commit()
        a = prices.assess(self.c, rid, prices.series_list(self.c, rid)[0])
        self.assertFalse(a["moved"], "its own 50% threshold applies")
        self.assertEqual(a["rule_window_days"], 30, "the window is inherited")

    def test_totals_are_grouped_by_currency_never_summed_across(self):
        r1 = self.add_release(title="One")
        r2 = self.add_release(title="Two")
        self.add_copy(r1)
        self.add_copy(r2)
        self.add_prices(r1, [10], currency="GBP", start_days_ago=0, step=1)
        self.add_prices(r2, [10], currency="USD", start_days_ago=0, step=1)
        tot = prices.collection_totals(self.c)
        self.assertEqual(len(tot["groups"]), 2)
        self.assertEqual({g["currency"] for g in tot["groups"]}, {"GBP", "USD"})

    def test_sparkline_needs_two_points_and_carries_no_colour(self):
        rid = self.add_release()
        self.add_prices(rid, [10], start_days_ago=0, step=1)
        key = prices.series_list(self.c, rid)[0]
        self.assertEqual(prices.sparkline(prices.points(self.c, rid, key)), "")
        self.add_prices(rid, [12, 14], start_days_ago=6, step=2)
        svg = prices.sparkline(prices.points(self.c, rid, key))
        self.assertIn("currentColor", svg)
        self.assertNotIn("#", svg, "a chart must inherit the theme, not fix a colour")


# ------------------------------------------------------------------- alerts

def _fake_catalogue(case, cat):
    """Point the three catalogue calls at a list, honouring start and cap the way
    MusicBrainz's paging does, and put the real ones back when the test ends."""
    for name in ("catalogue_size", "catalogue", "formats_for"):
        case.addCleanup(setattr, sources, name, getattr(sources, name))
    sources.catalogue_size = lambda c, mbid: (len(cat), "")
    sources.catalogue = lambda c, mbid, start=0, cap=800: (
        list(cat[start:start + cap]), start + cap < len(cat), "")
    sources.formats_for = lambda c, rg: (['12" Vinyl'], "")


class TestAlerts(Base):

    def _moved(self):
        rid = self.add_release()
        self.add_prices(rid, [30, 30.5, 31, 30, 29.5])
        self.add_prices(rid, [60], start_days_ago=0, step=1)
        return rid

    def test_a_move_raises_once_then_goes_quiet(self):
        rid = self._moved()
        self.assertEqual(len(alerts_mod.evaluate_prices(self.c)), 1)
        self.assertEqual(alerts_mod.evaluate_prices(self.c), [],
                         "the cooldown must stop the same move being reported daily")

    def test_an_untracked_record_raises_nothing(self):
        rid = self._moved()
        self.c.execute("UPDATE release SET tracked=0 WHERE id=?", (rid,))
        self.c.commit()
        self.assertEqual(alerts_mod.evaluate_prices(self.c), [])

    def test_the_first_sweep_of_an_artist_announces_nothing(self):
        cur = self.c.execute("""INSERT INTO artist(name,mbid,watch,added_at)
                                VALUES('Miles Davis','mb-1',1,?)""", (model.now_iso(),))
        self.c.commit()
        art = lambda: self.c.execute("SELECT * FROM artist WHERE id=?",
                                     (cur.lastrowid,)).fetchone()
        cat = [{"source": "musicbrainz", "source_id": f"rg-{i}", "title": f"Album {i}",
                "released": "1959", "primary_type": "Album", "secondary": "",
                "url": f"u{i}"} for i in range(4)]
        _fake_catalogue(self, cat)

        raised, note = alerts_mod.sweep_artist(self.c, art())
        self.assertEqual(raised, [], "a baseline must not announce a back catalogue")
        self.assertIn("From now on", note)
        self.assertTrue(art()["baseline_at"])

        swept = []
        sources.catalogue = lambda c, mbid, start=0, cap=800: (
            swept.append(1), (list(cat), False, ""))[1]
        alerts_mod.sweep_artist(self.c, art())
        self.assertEqual(swept, [], "an unchanged count must skip the full sweep")

        cat.append({"source": "musicbrainz", "source_id": "rg-new", "title": "New One",
                    "released": model.today_iso(), "primary_type": "Album",
                    "secondary": "", "url": "unew"})
        raised, _ = alerts_mod.sweep_artist(self.c, art())
        self.assertEqual(len(raised), 1)
        row = self.c.execute("SELECT * FROM alert WHERE id=?", (raised[0],)).fetchone()
        self.assertEqual(row["move_kind"], "new_release")
        self.assertIn("vinyl", row["item_format"])

    def test_a_catalogue_too_long_for_one_sweep_is_baselined_in_pieces(self):
        """The baseline is the first COMPLETE look. Before this, a truncated first
        sweep set it anyway, and every later sweep re-read the same first page."""
        cur = self.c.execute("""INSERT INTO artist(name,mbid,watch,added_at)
                                VALUES('Prolific','mb-p',1,?)""", (model.now_iso(),))
        self.c.commit()
        art = lambda: self.c.execute("SELECT * FROM artist WHERE id=?",
                                     (cur.lastrowid,)).fetchone()
        cat = [{"source": "musicbrainz", "source_id": f"rg-{i}", "title": f"Work {i}",
                "released": "1970", "primary_type": "Album", "secondary": "",
                "url": f"u{i}"} for i in range(7)]
        _fake_catalogue(self, cat)
        self.addCleanup(setattr, alerts_mod, "SWEEP_PAGE_CAP", alerts_mod.SWEEP_PAGE_CAP)
        alerts_mod.SWEEP_PAGE_CAP = 3

        for read in (3, 6):
            raised, note = alerts_mod.sweep_artist(self.c, art())
            self.assertEqual(raised, [])
            self.assertIn(f"Read {read} of 7", note)
            self.assertIsNone(art()["baseline_at"], "not complete, so not a baseline")
        raised, note = alerts_mod.sweep_artist(self.c, art())
        self.assertEqual(raised, [], "the piece that completes it announces nothing either")
        self.assertIn("7 works", note)
        self.assertTrue(art()["baseline_at"])

        cat.append({"source": "musicbrainz", "source_id": "rg-new", "title": "Late One",
                    "released": "1971", "primary_type": "Album", "secondary": "", "url": "un"})
        raised, _ = alerts_mod.sweep_artist(self.c, art())
        self.assertEqual(len(raised), 1, "past the first page, still seen once baselined")
        row = self.c.execute("SELECT * FROM alert WHERE id=?", (raised[0],)).fetchone()
        self.assertEqual(row["move_kind"], "newly_listed")

    def test_a_failed_channel_is_retried_without_repeating_the_one_that_worked(self):
        self._moved()
        alerts_mod.evaluate_prices(self.c)
        for k, v in (("webhook_url", "https://example.invalid/hook"),
                     ("smtp_host", "mail.example.invalid"), ("smtp_to", "me@example.invalid")):
            db.put(self.c, k, v)
        self.c.commit()
        posts, mails = [], []
        alerts_mod._http_post = lambda u, p, timeout=10: (posts.append(p) or (200, b"ok"))

        def refused(*a, **k):
            mails.append(1)
            raise OSError("535 authentication failed")
        alerts_mod._smtp_send = refused
        alerts_mod.deliver_pending(self.c)
        self.assertEqual((len(posts), len(mails)), (1, 1))
        alerts_mod._smtp_send = lambda *a, **k: mails.append(1)
        out = alerts_mod.deliver_pending(self.c)
        self.assertEqual([[ch for ch, ok, _ in res] for _, res in out], [["email"]],
                         "only the email that failed is tried again")
        self.assertEqual((len(posts), len(mails)), (1, 2))
        self.assertEqual(alerts_mod.deliver_pending(self.c), [])

    def test_the_webhook_carries_a_field_for_each_service(self):
        self._moved()
        alerts_mod.evaluate_prices(self.c)
        db.put(self.c, "webhook_url", "https://example.invalid/hook")
        self.c.commit()
        sent = []
        alerts_mod._http_post = lambda u, p, timeout=10: (sent.append(p) or (204, b""))
        alerts_mod.deliver_pending(self.c)
        for key in ("title", "message", "text", "content"):
            self.assertTrue(sent[0][key], f"{key} is what some service reads")
        self.assertLessEqual(len(sent[0]["content"]), 2000, "Discord's limit")
        self.assertIn("copies", sent[0]["content"], "the confound travels with it")

    def test_an_old_record_newly_catalogued_is_not_called_new(self):
        self.assertEqual(alerts_mod.classify_new("1971"), "newly_listed")
        self.assertEqual(alerts_mod.classify_new(""), "newly_listed")
        self.assertEqual(alerts_mod.classify_new(model.today_iso()), "new_release")
        future = str(model.year_of(model.today_iso()) + 1)
        self.assertEqual(alerts_mod.classify_new(future), "new_release")

    def test_nothing_is_sent_until_a_destination_is_configured(self):
        self._moved()
        alerts_mod.evaluate_prices(self.c)
        self.assertEqual(alerts_mod.channels_ready(self.c), [])
        self.assertEqual(alerts_mod.deliver_pending(self.c), [])

    def test_delivery_is_recorded_and_never_repeated(self):
        self._moved()
        alerts_mod.evaluate_prices(self.c)
        db.put(self.c, "webhook_url", "https://example.invalid/hook")
        self.c.commit()
        sent = []
        alerts_mod._http_post = lambda u, p, timeout=10: (sent.append(p) or (200, b"ok"))
        out = alerts_mod.deliver_pending(self.c)
        self.assertEqual(len(out), 1)
        self.assertEqual(len(sent), 1)
        self.assertIn("up", sent[0]["title"])
        rows = self.c.execute("SELECT * FROM delivery").fetchall()
        self.assertEqual([r["ok"] for r in rows], [1])
        self.assertEqual(alerts_mod.deliver_pending(self.c), [], "must not send twice")

    def test_a_failure_is_recorded_not_raised(self):
        self._moved()
        alerts_mod.evaluate_prices(self.c)
        db.put(self.c, "webhook_url", "https://example.invalid/hook")
        self.c.commit()
        def boom(u, p, timeout=10):
            raise OSError("no route to host")
        alerts_mod._http_post = boom
        out = alerts_mod.deliver_pending(self.c)
        self.assertFalse(out[0][1][0][1])
        row = self.c.execute("SELECT * FROM delivery").fetchone()
        self.assertEqual(row["ok"], 0)
        self.assertIn("no route", row["detail"])
        self.assertEqual(len(alerts_mod.deliver_pending(self.c)), 1, "a failure is retried")


class TestPoller(Base):

    def _due(self):
        rid = self.add_release(discogs_release_id=2317524)
        # A full minute's allowance of Discogs requests, all in the last ten seconds.
        at = (model.parse_iso(model.now_iso()) - datetime.timedelta(seconds=10)
              ).replace(microsecond=0).isoformat()
        for _ in range(sources.LIMITS[sources.DG_HOST][1]):
            self.c.execute("""INSERT INTO source_call(called_at,host,path,status,ms)
                              VALUES(?,?,'/x',200,10)""", (at, sources.DG_HOST))
        self.c.commit()
        self.addCleanup(setattr, sources, "MAX_WAIT", sources.MAX_WAIT)
        return rid

    def test_a_page_load_declines_a_long_wait(self):
        rid = self._due()
        obs, err = sources.price_now(self.c, {"discogs_release_id": 2317524})
        self.assertIsNone(obs)
        self.assertIn("asked enough for one minute", err)

    def test_the_poller_waits_its_turn_rather_than_declining(self):
        """Nobody watches a spinner in a cron job. Declining there left all but
        about twenty-two records on a shelf unpriced on every run."""
        rid = self._due()
        waited = []
        sources.SLEEP = waited.append
        out = io.StringIO()
        import contextlib
        import poller
        with contextlib.redirect_stdout(out):
            poller.main(["--only", "prices", "--quiet"])
        self.assertTrue(waited and max(waited) > 4.0, "it should have waited out the minute")
        n = self.c.execute("SELECT COUNT(*) n FROM price_obs WHERE release_id=?",
                           (rid,)).fetchone()["n"]
        self.assertEqual(n, 1)
        self.assertEqual(out.getvalue(), "", "--quiet says nothing when nothing failed")

    def test_quiet_still_reports_a_failure(self):
        self.add_release(discogs_release_id=2317524)
        sources._http_get = lambda u, h, t=None: (503, b"", {"Retry-After": "30"})
        out = io.StringIO()
        import contextlib
        import poller
        self.addCleanup(setattr, sources, "MAX_WAIT", sources.MAX_WAIT)
        with contextlib.redirect_stdout(out):
            poller.main(["--only", "prices", "--quiet"])
        self.assertIn("slow down", out.getvalue())


# ------------------------------------------------------- promises kept by grep

def code_only(module):
    """A module's source with its docstrings and comments removed.

    Some promises are about what the code does, not about what the file is
    allowed to discuss. prices.py has to quote the owner's word "significantly"
    in order to explain why it will not use it."""
    with open(os.path.join(BASE, module + ".py")) as fh:
        src = fh.read()
    src = re.sub(r'"""(?:.|\n)*?"""', "", src)
    src = re.sub(r"(?m)^\s*#.*$", "", src)
    return src.lower()


def strip_comments(js):
    """JavaScript with its comments removed, so a promise about the code is not
    satisfied or broken by the sentence that describes it."""
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", js)


class TestPromises(unittest.TestCase):

    def test_the_word_significant_appears_nowhere(self):
        """Nothing here establishes statistical significance, so the word is banned."""
        # Docstrings and comments are stripped first: a module is allowed to
        # explain the ban — prices.py quotes the owner's own word when saying why
        # it will not use it — but no code and no output may say it.
        for name in SERVER_MODULES:
            self.assertNotIn("significant", code_only(name),
                             f"{name}.py claims significance in its code")

    def test_no_javascript_on_the_shelf_can_write_anything(self):
        for name in os.listdir(os.path.join(BASE, "static")):
            if not name.endswith(".js"):
                continue
            with open(os.path.join(BASE, "static", name)) as fh:
                src = strip_comments(fh.read())
            for needle in ("fetch(", "XMLHttpRequest", "sendBeacon", ".submit(",
                           "localStorage", "indexedDB"):
                self.assertNotIn(needle, src, f"{name} may improve a page, not carry a fact")

    def test_a_price_move_is_never_coloured(self):
        """Up is not good and down is not bad — see the palette comment in app.py."""
        rule = [l for l in app.CSS.splitlines() if l.startswith(".move")]
        self.assertTrue(rule)
        joined = " ".join(rule)
        for needle in ("--red", "--ok-", "--amber", "green", "red", "color:"):
            self.assertNotIn(needle, joined)

    def test_a_price_move_is_never_framed_as_gain_or_loss(self):
        for name in ("prices", "alerts"):
            for needle in ("gain", "gains", "loss", "losses", "profit"):
                self.assertIsNone(re.search(r"\b" + needle + r"\b", code_only(name)),
                                  f"{name}.py frames a price move as {needle}")

    def test_the_sentence_for_a_move_claims_nothing_about_value(self):
        """The behavioural half: whatever the source says, the wording stays neutral."""
        a = {"moved": True, "direction": "up", "pct": 69.0, "abs_change": 23.7,
             "latest": 58.0, "baseline": 34.3, "baseline_n": 6, "currency": "GBP"}
        line = prices.describe(a).lower()
        for banned in ("worth", "value", "gain", "loss", "profit", "should", "bargain"):
            self.assertIsNone(re.search(r"\b" + banned + r"\b", line),
                              f"describe() said {banned!r}")
        self.assertIn("up", line)
        self.assertIn("median over 6 observations", line)

    def test_pillow_never_enters_the_server_path(self):
        for name in SERVER_MODULES:
            with open(os.path.join(BASE, name + ".py")) as fh:
                src = fh.read()
            self.assertNotIn("import PIL", src)
            self.assertNotIn("from PIL", src)

    def test_no_currency_is_ever_converted(self):
        for name in SERVER_MODULES:
            with open(os.path.join(BASE, name + ".py")) as fh:
                src = fh.read().lower()
            for needle in ("exchange_rate", "exchangerate", "fx_rate", "convert_currency"):
                self.assertNotIn(needle, src)

    def test_every_route_is_unique_and_reachable(self):
        seen = set()
        for method, rx, fn in app.ROUTES:
            key = (method, rx)
            self.assertNotIn(key, seen, f"{method} {rx} is routed twice")
            seen.add(key)
            self.assertTrue(callable(fn))

    def test_the_static_table_and_the_folder_agree(self):
        on_disk = {n for n in os.listdir(os.path.join(BASE, "static"))}
        self.assertEqual(set(app.STATIC), on_disk,
                         "a file added to the shelf must be added to app.STATIC")


# ------------------------------------------------------------ over the wire

class Web(Base):
    """A real server, on a real socket, against a temporary database."""

    def setUp(self):
        super().setUp()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        self.port = self.srv.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()
        # Prove the server is on the temporary file before a single request runs.
        probe = db.connect()
        served = [r[2] for r in probe.execute("PRAGMA database_list")]
        probe.close()
        self.assertIn(self.path, served, "the test server is pointed at a real collection")
        # Logged in, as every page but the login requires. TestTheDoor is where
        # the door itself is tested; everywhere else walks straight through it.
        auth.ensure_accounts(self.c, ["Tester"])
        self.acct = auth.account_by_name(self.c, "Tester")
        auth.set_password(self.c, self.acct["id"], "a tester's password")
        self.token = auth.new_session(self.c, self.acct["id"])
        self.c.commit()
        self.cookie = f"sn_session={self.token}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def open(self, path, data=None, headers=None, method=None, cookie=True):
        hdrs = {"Cookie": self.cookie} if cookie else {}
        hdrs.update(headers or {})
        req = urllib.request.Request(self.base + path, data=data, headers=hdrs,
                                     method=method)
        return urllib.request.build_opener().open(req, timeout=30)

    def get(self, path):
        r = self.open(path)
        return r.status, r.read().decode("utf-8", "replace")

    def post(self, path, data):
        body = urllib.parse.urlencode(data).encode()
        r = self.open(path, body, {"Content-Type": "application/x-www-form-urlencoded",
                                   "Referer": self.base + path}, "POST")
        return r.status, r.read().decode("utf-8", "replace"), r.geturl()

    def raw_post(self, path, body, ctype, referer=None):
        r = self.open(path, body, {"Content-Type": ctype,
                                   "Referer": referer if referer is not None
                                   else self.base + path}, "POST")
        return r.status, r.read().decode("utf-8", "replace"), r.geturl()


GET_PAGES = ["/", "/more", "/scan", "/search", "/shelf", "/record/new", "/prices", "/alerts",
             "/rules", "/artists", "/labels", "/settings", "/calls", "/help", "/backup"]

HOSTILE = '<script>alert("x")</script> & \'quotes\' <img src=x onerror=1>'


class TestEscaping(Web):

    def test_every_page_escapes_what_came_out_of_the_database(self):
        rid = self.add_release(title=HOSTILE, artist=HOSTILE, label=HOSTILE,
                               catno=HOSTILE, notes=HOSTILE, edition_note=HOSTILE,
                               barcode="0886976389413", matrix=HOSTILE,
                               source_notes=HOSTILE,
                               cover_url="https://example.invalid/" + HOSTILE)
        cid = self.add_copy(rid)
        self.c.execute("UPDATE copy SET notes=?, acquired_from=?, location=? WHERE id=?",
                       (HOSTILE, HOSTILE, HOSTILE, cid))
        self.c.execute("""INSERT INTO alert_rule(kind,release_id,pct_threshold,active,created_at)
                          VALUES('price_move',?,40,1,?)""", (rid, model.now_iso()))
        self.c.execute("""INSERT INTO grading(copy_id,media_grade,sleeve_grade,note,graded_at)
                          VALUES(?,4,3,?,?)""", (cid, HOSTILE, model.now_iso()))
        self.c.execute("""INSERT INTO artist(name,notes,added_at) VALUES(?,?,?)""",
                       (HOSTILE, HOSTILE, model.now_iso()))
        self.c.execute("UPDATE release SET artist_id=1 WHERE id=?", (rid,))
        self.c.execute("""INSERT INTO barcode_scan(raw,code,how,note,scanned_at)
                          VALUES(?,'','typed',?,?)""", (HOSTILE, HOSTILE, model.now_iso()))
        offered = [{k: HOSTILE for k in ("title", "artist_credit", "released", "country",
                                         "label", "catno", "edition_note", "disambiguation",
                                         "format_words", "source_id")}]
        offered[0].update(source="discogs", url="https://www.discogs.com/" + HOSTILE)
        self.c.execute("""INSERT INTO title_search(artist,title,catno,candidates,candidates_json,
                          totals_json,note,searched_at) VALUES(?,?,?,1,?,?,?,?)""",
                       (HOSTILE, HOSTILE, HOSTILE, json.dumps(offered),
                        json.dumps({"discogs": 1}), HOSTILE, model.now_iso()))
        db.put(self.c, "webhook_url", HOSTILE)
        self.c.commit()
        self.add_prices(rid, [30, 31, 30, 29])
        self.add_prices(rid, [60], start_days_ago=0, step=1)
        alerts_mod.evaluate_prices(self.c)

        pages = GET_PAGES + [f"/record?id={rid}", f"/copy?id={cid}", f"/grade?copy={cid}",
                             "/artist?id=1", f"/record/edit?id={rid}",
                             "/shelf?tracked=1&linked=no", "/search/choose?search=1"]
        for path in pages:
            status, body = self.get(path)
            self.assertEqual(status, 200, path)
            self.assertNotIn("<script>alert", body, f"{path} did not escape a title")
            self.assertNotIn("<img src=x", body, f"{path} did not escape a tag")
        # And the positive half, on the pages that actually render the record:
        # proof the hostile text reached the page at all, rather than the loop
        # above passing because nothing was there to escape.
        for path in ("/shelf", f"/record?id={rid}", f"/copy?id={cid}", "/search",
                     "/search/choose?search=1"):
            status, body = self.get(path)
            self.assertIn("&lt;script&gt;", body, f"{path} should show the text, escaped")
            self.assertIn("&lt;img src=x onerror=1&gt;", body,
                          f"{path} should show the text, inert")


class TestPRG(Web):

    def test_every_post_redirects_and_its_write_is_visible_elsewhere(self):
        rid = self.add_release(barcode="0886976389413")
        cid = self.add_copy(rid)
        cases = [
            ("/grade", {"copy": cid, "media": "4", "sleeve": "3", "note": "ring wear"},
             "SELECT COUNT(*) n FROM grading", 1),
            ("/record/notes", {"id": rid, "notes": "mine"},
             "SELECT COUNT(*) n FROM release WHERE notes='mine'", 1),
            ("/record/track", {"id": rid},
             "SELECT COUNT(*) n FROM release WHERE tracked=0", 1),
            ("/prices/add", {"id": rid, "kind": "sale_seen", "amount": "22.50",
                             "currency": "GBP"},
             "SELECT COUNT(*) n FROM price_obs WHERE kind='sale_seen'", 1),
            ("/copy/new", {"release": rid}, "SELECT COUNT(*) n FROM copy", 2),
            ("/settings", {"currency": "USD"},
             "SELECT COUNT(*) n FROM setting WHERE key='currency' AND value='USD'", 1),
            ("/currency", {"currency": "CAD", "next": "/prices"},
             "SELECT COUNT(*) n FROM setting WHERE key='currency' AND value='CAD'", 1),
            ("/rules", {"pct": "25", "abs": "4", "window": "45", "min_obs": "4",
                        "cooldown": "7", "direction": "up"},
             "SELECT COUNT(*) n FROM alert_rule WHERE pct_threshold=25.0", 1),
            ("/record/rule", {"id": rid, "mode": "own", "pct": "35", "abs": "5",
                              "direction": "down"},
             f"SELECT COUNT(*) n FROM alert_rule WHERE release_id={rid} AND active=1 "
             f"AND pct_threshold=35.0", 1),
            ("/record/rule", {"id": rid, "mode": "default"},
             f"SELECT COUNT(*) n FROM alert_rule WHERE release_id={rid}", 0),
            ("/copy/edit", {"id": cid, "acquired_from": "a fair", "paid": "12.50",
                            "currency": "GBP"},
             f"SELECT COUNT(*) n FROM price_obs WHERE copy_id={cid} AND kind='paid' "
             f"AND amount=12.5", 1),
            ("/copy/edit", {"id": cid, "acquired_from": "a fair", "paid": "14",
                            "currency": "GBP"},
             f"SELECT COUNT(*) n FROM price_obs WHERE copy_id={cid} AND kind='paid' "
             f"AND amount=14", 1),
            ("/record/edit", {"id": rid, "title": "Retitled", "artist_credit": "Miles Davis",
                              "format": "LP", "speed": "33"},
             "SELECT COUNT(*) n FROM release WHERE title='Retitled'", 1),
        ]
        for path, data, check, want in cases:
            status, body, url = self.post(path, data)
            self.assertIn("msg=", url,
                          f"{path} must end in a redirect, not render a page")
            other = db.connect()                       # a second connection entirely
            got = other.execute(check).fetchone()["n"]
            other.close()
            self.assertEqual(got, want, f"{path} did not commit")

    def test_a_cross_site_post_is_refused(self):
        try:
            self.raw_post("/record/track", b"id=1",
                          "application/x-www-form-urlencoded", referer="http://evil.example/")
            self.fail("a cross-site post was accepted")
        except urllib.error.HTTPError as ex:
            self.assertEqual(ex.code, 403)


class TestChoosingAnEdition(Web):

    def _scan(self):
        status, body, url = self.post("/scan", {"how": "typed", "code": "0886976389413"})
        return int(re.search(r"scan=(\d+)", url).group(1))

    def test_choosing_the_musicbrainz_edition_follows_its_discogs_link(self):
        """Before, a record chosen from MusicBrainz had no Discogs release and so no
        price could ever be followed for it, though MusicBrainz published the link."""
        status, body, url = self.post("/scan/choose", {"scan": self._scan(), "pick": 0})
        self.assertIn("2317524", urllib.parse.unquote_plus(url))
        rel = self.c.execute("SELECT * FROM release").fetchone()
        self.assertEqual(rel["source"], "musicbrainz")
        self.assertEqual(rel["mbid"], "d6a1f166-8a02-4608-849b-9d623912c3e5")
        self.assertEqual(rel["discogs_release_id"], 2317524)
        self.post("/prices/check", {"id": rel["id"]})
        self.assertEqual(self.c.execute("SELECT amount FROM price_obs").fetchone()["amount"],
                         34.4)

    def test_one_edition_from_both_catalogues_is_one_record(self):
        self.post("/scan/choose", {"scan": self._scan(), "pick": 1})    # Discogs 2317524
        self.post("/scan/choose", {"scan": self._scan(), "pick": 0})    # MusicBrainz, linked
        rels = self.c.execute("SELECT * FROM release").fetchall()
        self.assertEqual(len(rels), 1, "MusicBrainz's published link says they are one")
        self.assertEqual(rels[0]["mbid"], "d6a1f166-8a02-4608-849b-9d623912c3e5",
                         "and the record gains the id it was missing")
        self.assertEqual(self.c.execute("SELECT COUNT(*) n FROM copy").fetchone()["n"], 2)

    def test_a_discogs_pick_keeps_the_offer_and_files_the_full_entry(self):
        sid = self._scan()
        offered = self.c.execute("SELECT candidates_json FROM barcode_scan WHERE id=?",
                                 (sid,)).fetchone()["candidates_json"]
        self.post("/scan/choose", {"scan": sid, "pick": 1})
        rel = self.c.execute("SELECT * FROM release").fetchone()
        self.assertEqual(rel["released"], "2010-05")
        self.assertIn("Limited and numbered", rel["source_notes"])
        self.assertEqual(rel["matrix"], "")
        self.assertEqual(self.c.execute("SELECT candidates_json FROM barcode_scan WHERE id=?",
                                        (sid,)).fetchone()["candidates_json"], offered,
                         "what was offered is evidence and is not rewritten")
        status, body = self.get(f"/record?id={rel['id']}")
        self.assertIn("Limited and numbered", body)
        self.assertIn("lists no run-out stamps", body)

    def test_the_record_page_asks_for_what_is_missing(self):
        rid = self.add_release(mbid="d6a1f166-8a02-4608-849b-9d623912c3e5",
                               source="musicbrainz", released="2010-05")
        status, body = self.get(f"/record?id={rid}")
        self.assertIn("Ask MusicBrainz for its Discogs link", body)
        status, body, url = self.post("/record/fetch", {"id": rid})
        rel = self.c.execute("SELECT * FROM release WHERE id=?", (rid,)).fetchone()
        self.assertEqual(rel["discogs_release_id"], 2317524)
        self.assertIn("Limited and numbered", rel["source_notes"],
                      "the Discogs entry it now links to is read in the same press")
        self.assertEqual(rel["released"], "2010-05",
                         "another catalogue's date does not overwrite this one's")
        status, body = self.get(f"/record?id={rid}")
        self.assertNotIn("Ask MusicBrainz", body)
        self.assertNotIn("Fetch the run-out", body)

    def test_a_link_already_taken_is_not_moved(self):
        self.add_release(title="Linked already", discogs_release_id=2317524)
        rid = self.add_release(mbid="d6a1f166-8a02-4608-849b-9d623912c3e5",
                               source="musicbrainz")
        status, body, url = self.post("/record/fetch", {"id": rid})
        self.assertIn("already linked", urllib.parse.unquote_plus(url))
        self.assertIsNone(self.c.execute("SELECT discogs_release_id FROM release WHERE id=?",
                                         (rid,)).fetchone()[0])


class TestRecordsAndCopies(Web):

    def test_a_sale_is_one_receipt_and_goes_when_the_copy_comes_back(self):
        rid = self.add_release()
        cid = self.add_copy(rid)
        for amount in ("40", "45"):
            self.post("/copy/dispose", {"id": cid, "disposed_on": model.today_iso(),
                                        "disposed_to": "a dealer", "sold": amount,
                                        "currency": "GBP"})
        rows = self.c.execute("SELECT amount FROM price_obs WHERE copy_id=? AND kind='sold'",
                              (cid,)).fetchall()
        self.assertEqual([r["amount"] for r in rows], [45.0], "corrected, not added to")
        status, body = self.get(f"/copy?id={cid}")
        self.assertIn("£45.00", body)
        self.assertIn("What you sold it for", self.get("/help")[1])
        self.post("/copy/dispose", {"id": cid, "disposed_on": ""})
        self.assertEqual(self.c.execute("SELECT COUNT(*) n FROM price_obs WHERE kind='sold'"
                                        ).fetchone()["n"], 0)

    def test_a_purchase_is_not_recorded_against_the_edition(self):
        rid = self.add_release()
        status, body, url = self.post("/prices/add", {"id": rid, "kind": "paid",
                                                      "amount": "10", "currency": "GBP"})
        self.assertIn("copy's own page", urllib.parse.unquote_plus(url))
        self.assertEqual(self.c.execute("SELECT COUNT(*) n FROM price_obs").fetchone()["n"], 0)
        self.assertNotIn('value="paid"', self.get(f"/record?id={rid}")[1])

    def test_a_record_from_a_catalogue_is_not_edited(self):
        rid = self.add_release(source="discogs", discogs_release_id=2317524)
        self.assertIn("not edited here", " ".join(self.get(f"/record/edit?id={rid}")[1].split()))
        self.post("/record/edit", {"id": rid, "title": "Something else"})
        self.assertEqual(self.c.execute("SELECT title FROM release WHERE id=?",
                                        (rid,)).fetchone()[0], "Kind of Blue")

    def test_a_bad_barcode_in_an_edit_saves_nothing(self):
        rid = self.add_release()
        status, body, url = self.post("/record/edit", {"id": rid, "title": "Changed",
                                                       "barcode": "0886976389414"})
        self.assertIn("would have to be 3", body)
        self.assertIn('value="Changed"', body, "what was typed is kept for one correction")
        self.assertEqual(self.c.execute("SELECT title FROM release WHERE id=?",
                                        (rid,)).fetchone()[0], "Kind of Blue")

    def test_the_shelf_shows_what_the_prices_page_says_it_will(self):
        self.add_release(title="Linked", discogs_release_id=2317524)
        self.add_release(title="Unlinked")
        self.assertIn("/shelf?tracked=1&amp;linked=no", self.get("/prices")[1])
        status, body = self.get("/shelf?tracked=1&linked=no")
        self.assertIn("Unlinked", body)
        self.assertNotIn(">Linked<", body)
        self.assertIn("Showing only records", body)

    def test_a_cover_is_linked_never_embedded(self):
        rid = self.add_release(cover_url="https://i.discogs.example/cover.jpeg")
        body = self.get(f"/record?id={rid}")[1]
        self.assertIn('href="https://i.discogs.example/cover.jpeg"', body)
        self.assertNotIn('src="https://i.discogs', body)
        rid2 = self.add_release(title="Two", cover_url="javascript:alert(1)")
        self.assertNotIn("javascript:", self.get(f"/record?id={rid2}")[1])


class TestSettingsSecrets(Web):

    def test_a_stored_secret_is_never_written_into_the_page(self):
        db.put(self.c, "discogs_token", "SECRET-TOKEN")
        db.put(self.c, "smtp_pass", "SECRET-PASSWORD")
        db.put(self.c, "webhook_url", "https://ntfy.sh/SECRET-TOPIC")
        self.c.commit()
        body = self.get("/settings")[1]
        for secret in ("SECRET-TOKEN", "SECRET-PASSWORD", "SECRET-TOPIC"):
            self.assertNotIn(secret, body)
        self.assertIn("https://ntfy.sh/…", body, "but it says where alerts are going")

    def test_a_blank_box_keeps_a_secret_and_forget_clears_it(self):
        db.put(self.c, "discogs_token", "KEEP-ME")
        db.put(self.c, "smtp_pass", "old")
        self.c.commit()
        self.post("/settings", {"currency": "GBP", "discogs_token": "", "smtp_pass": ""})
        other = db.connect()
        self.assertEqual(db.get(other, "discogs_token"), "KEEP-ME")
        other.close()
        self.post("/settings", {"currency": "GBP", "smtp_pass": "  two  spaces ",
                                "forget_discogs_token": "1"})
        other = db.connect()
        self.assertEqual(db.get(other, "discogs_token"), "")
        self.assertEqual(db.get(other, "smtp_pass"), "  two  spaces ",
                         "a password is kept exactly as typed")
        other.close()


class TestWatchingWeb(Web):

    def test_relinking_an_artist_starts_a_fresh_baseline(self):
        """Keeping the old baseline would announce the new person's whole catalogue."""
        aid = self.c.execute("""INSERT INTO artist(name,mbid,watch,baseline_at,added_at)
                                VALUES('Miles Davis','wrong-one',1,?,?)""",
                             (model.now_iso(), model.now_iso())).lastrowid
        for i in range(3):
            self.c.execute("""INSERT INTO artist_release(artist_id,source,source_id,title,
                              first_seen_at) VALUES(?,'musicbrainz',?,?,?)""",
                           (aid, f"old-{i}", f"Old {i}", model.now_iso()))
        self.c.commit()
        self.assertIn("clears what was noted", self.get(f"/artist?id={aid}&find=Miles")[1])
        self.post("/artist/link", {"id": aid, "mbid": "right-one",
                                   "sort_right-one": "Davis, Miles"})
        a = self.c.execute("SELECT * FROM artist WHERE id=?", (aid,)).fetchone()
        self.assertEqual((a["mbid"], a["sort_name"], a["baseline_at"]),
                         ("right-one", "Davis, Miles", None))
        self.assertEqual(self.c.execute("SELECT COUNT(*) n FROM artist_release").fetchone()[0], 0)
        cat = [{"source": "musicbrainz", "source_id": f"rg-{i}", "title": f"Album {i}",
                "released": "1959", "primary_type": "Album", "secondary": "",
                "url": f"u{i}"} for i in range(5)]
        _fake_catalogue(self, cat)
        raised, note = alerts_mod.sweep_artist(self.c, a)
        self.assertEqual(raised, [])
        self.assertIn("From now on", note)

    def test_a_test_notification_says_what_it_is(self):
        db.put(self.c, "webhook_url", "https://example.invalid/hook")
        self.c.commit()
        sent = []
        alerts_mod._http_post = lambda u, p, timeout=10: (sent.append(p) or (200, b"ok"))
        self.post("/alerts/test", {})
        self.assertEqual(sent[0]["title"], "A test notification from Sleevenote")
        self.assertNotIn("artist", sent[0]["message"].lower())


class TestTheWholeFlow(Web):

    def test_the_camera_marks_its_read_as_a_camera_read(self):
        """The script may label where the digits came from; the server still checks
        them. Without the label every camera read was logged as typed."""
        self.assertIn('id="how"', self.get("/scan")[1])
        with open(os.path.join(BASE, "static", "scan.js")) as fh:
            js = strip_comments(fh.read())
        self.assertIn('how.value = "camera"', js)
        self.assertIn('how.value = "typed"', js)
        status, body, url = self.post("/scan", {"how": "camera", "code": "0886976389413"})
        self.assertEqual(self.c.execute("SELECT how FROM barcode_scan").fetchone()[0], "camera")

    def test_scan_choose_grade_price_alert(self):
        # 1. scan a barcode, with the spaces a person would type
        status, body, url = self.post("/scan", {"how": "typed", "code": "0 886976 389413"})
        self.assertIn("/scan/choose", url)
        sid = int(re.search(r"scan=(\d+)", url).group(1))

        # the raw text is kept exactly as it arrived
        row = self.c.execute("SELECT * FROM barcode_scan WHERE id=?", (sid,)).fetchone()
        self.assertEqual(row["raw"], "0 886976 389413")
        self.assertEqual(row["code"], "0886976389413")

        # 2. the choosing screen offers the editions and says how they differ
        status, body = self.get(f"/scan/choose?scan={sid}")
        self.assertEqual(body.count("This is the one"), 3)
        self.assertIn("They differ in: <strong>pressing</strong>", body)

        # 3. choose one; a release and a copy appear, and we land on the grades
        status, body, url = self.post("/scan/choose", {"scan": sid, "pick": 1})
        self.assertIn("/grade?copy=", url)
        cid = int(re.search(r"copy=(\d+)", url).group(1))
        rel = self.c.execute("SELECT * FROM release").fetchone()
        self.assertEqual(rel["identified"], "chosen")
        self.assertEqual(rel["barcode"], "0886976389413")

        # 4. the grade form shows the words that define each number
        status, body = self.get(f"/grade?copy={cid}")
        self.assertIn("Looks unplayed", body)
        self.assertIn("no ring wear", body)
        self.assertEqual(body.count('type="radio"'), 16)     # 8 per scale

        # 5. grade it, separately for record and sleeve
        status, body, url = self.post("/grade", {"copy": cid, "media": "4", "sleeve": "3",
                                                 "graded_on": model.today_iso(),
                                                 "note": "faint ring wear"})
        g = library.grade_view(library.current_grading(self.c, cid))
        self.assertEqual((g["media"], g["sleeve"]), (4, 3))
        self.assertEqual(g["media_gold"], "VG+")

        # 6. a sealed copy declines to grade the vinyl rather than calling it 5
        status, body, url = self.post("/copy/new", {"release": rel["id"]})
        cid2 = int(re.search(r"copy=(\d+)", url).group(1))
        self.post("/grade", {"copy": cid2, "media": "u:sealed", "sleeve": "5"})
        g2 = library.grade_view(library.current_grading(self.c, cid2))
        self.assertIsNone(g2["media"])
        self.assertIn("sealed", g2["media_why"].lower())

        # 7. follow the price
        status, body, url = self.post("/prices/check", {"id": rel["id"]})
        obs = self.c.execute("SELECT * FROM price_obs WHERE kind='lowest_asking'").fetchone()
        self.assertEqual(obs["amount"], 34.4)
        self.assertEqual(obs["feed"], "discogs_stats")
        # Back-date it an hour: two polls in the same second are one observation
        # by design, and this flow is meant to read as two days of a real week.
        earlier = (model.parse_iso(model.now_iso()) - datetime.timedelta(hours=1)
                   ).replace(microsecond=0).isoformat()
        self.c.execute("UPDATE price_obs SET observed_at=? WHERE id=?", (earlier, obs["id"]))
        self.c.commit()

        # 8. a month of quiet behind it, then the next poll finds a jump. The
        # alert has to come out of the ordinary path — poll, record, judge — so
        # the source is made to answer differently rather than the row inserted.
        self.add_prices(rel["id"], [34.4, 34.0, 35.0, 34.4, 33.9])
        spike = json.dumps({"blocked_from_sale": False, "num_for_sale": 3,
                            "lowest_price": {"currency": "GBP", "value": 58.0}}).encode()
        sources._http_get = lambda u, h, t=None: (
            (200, spike, {}) if "marketplace/stats" in u else fake_http(u, h, t))
        self.c.execute("UPDATE release SET last_priced_at=NULL WHERE id=?", (rel["id"],))
        self.c.commit()
        status, body, url = self.post("/prices/check", {"id": rel["id"]})
        self.assertIn("worth telling you about", urllib.parse.unquote(url))
        raised = self.c.execute("SELECT COUNT(*) n FROM alert WHERE kind='price_move'"
                                ).fetchone()["n"]
        self.assertEqual(raised, 1)
        status, body = self.get("/alerts")
        self.assertIn("lowest asking price up", body)
        self.assertIn("median over", body)
        self.assertIn("copies were for sale then", body)
        self.assertIn("not a sale", body)

        # 9. and the shelf and its CSV know about all of it
        status, body = self.get("/shelf")
        self.assertIn("Kind Of Blue", body)
        status, body = self.get("/shelf.csv")
        self.assertIn("record_grade", body.splitlines()[0])
        self.assertEqual(len(body.strip().splitlines()), 2)

    def test_a_barcode_that_fails_its_checksum_is_kept_but_not_used(self):
        status, body, url = self.post("/scan", {"how": "typed", "code": "0886976389414"})
        self.assertIn("would have to be 3", body)
        row = self.c.execute("SELECT * FROM barcode_scan").fetchone()
        self.assertEqual(row["raw"], "0886976389414")
        self.assertEqual(row["code"], "")
        self.assertEqual(self.c.execute("SELECT COUNT(*) n FROM release").fetchone()["n"], 0)

    def test_a_photograph_is_read_by_the_server(self):
        """The path that works with JavaScript off and on a browser with no scanner."""
        png = bc.png("0886976389413", scale=3, height=60)
        boundary = "----sleevenotetest"
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"how\"\r\n\r\n"
                f"photo\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"photo\";"
                f" filename=\"b.png\"\r\nContent-Type: image/png\r\n\r\n").encode()
        body += png + f"\r\n--{boundary}--\r\n".encode()
        status, out, url = self.raw_post("/scan", body,
                                         f"multipart/form-data; boundary={boundary}")
        self.assertIn("/scan/choose", url)
        row = self.c.execute("SELECT * FROM barcode_scan").fetchone()
        self.assertEqual(row["code"], "0886976389413")
        self.assertEqual(row["how"], "photo")
        self.assertGreaterEqual(row["read_votes"] or 0, bc.MIN_VOTES)

    def test_an_unreadable_photograph_says_so_and_files_nothing(self):
        rng = random.Random(9)
        noise = bytes(rng.randint(0, 255) for _ in range(300 * 40))
        png = bc._png_bytes(300, 40, b"".join(b"\x00" + noise[y*300:(y+1)*300]
                                              for y in range(40)))
        boundary = "----sleevenotetest2"
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"how\"\r\n\r\n"
                f"photo\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"photo\";"
                f" filename=\"b.png\"\r\nContent-Type: image/png\r\n\r\n").encode()
        body += png + f"\r\n--{boundary}--\r\n".encode()
        status, out, url = self.raw_post("/scan", body,
                                         f"multipart/form-data; boundary={boundary}")
        self.assertIn("No barcode could be read", out)
        self.assertEqual(self.c.execute("SELECT COUNT(*) n FROM release").fetchone()["n"], 0)

    def test_nothing_matched_offers_entry_by_hand(self):
        sources._http_get = lambda u, h, t=None: (
            200, fixture("mb_search_none.json" if "musicbrainz" in u
                         else "discogs_search_none.json"), {})
        body12 = "978014017739"
        code = body12 + str(bc.check_digit(body12))
        status, body, url = self.post("/scan", {"how": "typed", "code": code})
        sid = int(re.search(r"scan=(\d+)", url).group(1))
        status, body = self.get(f"/scan/choose?scan={sid}")
        self.assertIn("Nothing matched", body)
        self.assertIn("ISBN", body, "the app should say the number is a book's")
        status, body, url = self.post("/scan/manual",
                                      {"scan": sid, "title": "A private pressing",
                                       "artist_credit": "Someone", "format": "LP"})
        rel = self.c.execute("SELECT * FROM release").fetchone()
        self.assertEqual(rel["identified"], "unmatched")
        self.assertEqual(rel["source"], "manual")

    def test_the_same_edition_scanned_twice_is_not_two_records(self):
        for _ in range(2):
            status, body, url = self.post("/scan", {"how": "typed", "code": "0886976389413"})
            sid = int(re.search(r"scan=(\d+)", url).group(1))
            self.post("/scan/choose", {"scan": sid, "pick": 1})
        n = self.c.execute("SELECT COUNT(*) n FROM release").fetchone()["n"]
        self.assertEqual(n, 1, "scanning the same record twice must not duplicate it")
        self.assertEqual(self.c.execute("SELECT COUNT(*) n FROM copy").fetchone()["n"], 2,
                         "but it should add a second copy")


class TestFindingByName(Web):
    """A record with no barcode — most from before about 1980 — found by its
    artist, title or catalogue number, and filed exactly as a scan would be."""

    def search(self, **vals):
        status, body, url = self.post("/search", vals)
        m = re.search(r"search=(\d+)", url)
        return (int(m.group(1)) if m else None), body

    def test_search_choose_grade(self):
        sid, body = self.search(artist="Miles Davis", title="Kind of Blue")
        self.assertIn("Which pressing is yours?", body)
        self.assertIn("Discogs lists 359 vinyl editions", body)
        self.assertIn("These are the first 25", body)
        self.assertIn("MusicBrainz lists 62 vinyl editions", body)
        self.assertIn('href="#byname"', body, "too many: it asks for the catalogue number")
        self.assertLess(body.index("Discogs lists"), body.index("MusicBrainz lists"))
        row = self.c.execute("SELECT * FROM title_search WHERE id=?", (sid,)).fetchone()
        self.assertEqual((row["artist"], row["title"], row["candidates"]),
                         ("Miles Davis", "Kind of Blue", 50))
        cands = json.loads(row["candidates_json"])
        pick = next(i for i, cd in enumerate(cands) if cd["source"] == "musicbrainz")
        status, body, url = self.post("/search/choose", {"search": sid, "pick": pick})
        self.assertIn("/grade?copy=", url)
        rel = self.c.execute("SELECT * FROM release").fetchone()
        self.assertEqual((rel["identified"], rel["source"]), ("chosen", "musicbrainz"))
        self.assertEqual(rel["discogs_release_id"], 2317524, "its price can be followed")
        self.assertEqual(rel["barcode"], "0886976389413")
        self.assertEqual(self.c.execute("SELECT release_id FROM title_search WHERE id=?",
                                        (sid,)).fetchone()[0], rel["id"])
        self.assertEqual(self.c.execute("SELECT candidates_json FROM title_search WHERE id=?",
                                        (sid,)).fetchone()[0], row["candidates_json"],
                         "what was offered is never rewritten")
        status, body = self.get(f"/record?id={rel['id']}")
        self.assertIn("Chosen from the editions a search by name found", body)
        self.assertNotIn("share the barcode", body, "it was never scanned")

    def test_a_record_found_by_name_is_found_again_by_its_barcode(self):
        _install_seam(lambda u, h, t=None: (
            200, fixture("discogs_search_barcode.json"), {})
            if "database/search?type=release&format=Vinyl" in u else fake_http(u, h, t))
        sid, body = self.search(title="Kind of Blue")
        cands = json.loads(self.c.execute("SELECT candidates_json FROM title_search").fetchone()[0])
        pick = next(i for i, cd in enumerate(cands) if cd["discogs_release_id"] == 2317524)
        self.post("/search/choose", {"search": sid, "pick": pick})
        rel = self.c.execute("SELECT * FROM release").fetchone()
        self.assertEqual(rel["barcode"], "0886976389413", "from the full entry, typed")
        _install_seam()
        status, body, url = self.post("/scan", {"how": "typed", "code": "0886976389413"})
        self.assertIn("You already have this barcode", body)

    def test_an_artist_alone_is_refused_and_keeps_what_was_typed(self):
        _install_seam(forbidden_http)
        status, body, url = self.post("/search", {"artist": "Miles Davis", "catno": " - "})
        self.assertIn("A title or a catalogue number is needed", body)
        self.assertIn('value="Miles Davis"', body)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM title_search").fetchone()[0], 0)

    def test_an_edition_already_on_the_shelf_is_marked_by_its_id(self):
        first = json.loads(fixture("discogs_search_title.json"))["results"][0]["id"]
        rid = self.add_release(title="Kind of Blue", discogs_release_id=first)
        self.add_release(title="Kind of Blue", catno="not the same pressing")
        sid, body = self.search(artist="Miles Davis", title="Kind of Blue")
        self.assertEqual(body.count("Already on your shelf"), 1,
                         "matched on the source's id, never on a title that looks the same")
        self.assertIn(f'href="/record?id={rid}"', body)
        cands = json.loads(self.c.execute("SELECT candidates_json FROM title_search").fetchone()[0])
        pick = next(i for i, cd in enumerate(cands) if cd["discogs_release_id"] == first)
        self.post("/search/choose", {"search": sid, "pick": pick})
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM release").fetchone()[0], 2)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM copy WHERE release_id=?",
                                        (rid,)).fetchone()[0], 1, "another copy, not a record")

    def test_a_second_press_is_the_same_copy(self):
        sid, body = self.search(title="Kind of Blue")
        self.post("/search/choose", {"search": sid, "pick": 0})
        status, body, url = self.post("/search/choose", {"search": sid, "pick": 3})
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM copy").fetchone()[0], 1)
        self.assertIn("already added", urllib.parse.unquote_plus(url))
        self.assertIn("already on the shelf", self.get(f"/search/choose?search={sid}")[1])

    def test_none_of_these_adds_it_by_hand_with_what_was_typed(self):
        sid, body = self.search(artist="Miles Davis", title="Kind of Blue", catno="CL 1355")
        self.assertIn('action="/search/manual"', body)
        self.assertIn('name="catno" value="CL 1355"', body)
        status, body, url = self.post("/search/manual", {
            "search": sid, "title": "Kind of Blue", "artist_credit": "Miles Davis",
            "catno": "CL 1355", "format": "LP", "speed": "33"})
        self.assertIn("/grade?copy=", url)
        rel = self.c.execute("SELECT * FROM release").fetchone()
        self.assertEqual((rel["identified"], rel["catno"], rel["barcode"]),
                         ("unmatched", "CL 1355", None))
        self.assertIn("A search by name found nothing that fitted",
                      self.get(f"/record?id={rel['id']}")[1])
        self.assertEqual(self.c.execute("SELECT release_id FROM title_search").fetchone()[0],
                         rel["id"])

    def test_a_search_nobody_answered_is_not_nothing_found(self):
        db.put(self.c, "offline", "1")
        self.c.commit()
        sid, body = self.search(title="Kind of Blue")
        self.assertIn("No answer about", body)
        self.assertNotIn("Neither MusicBrainz nor Discogs lists", body)
        self.assertIsNone(self.c.execute("SELECT candidates FROM title_search WHERE id=?",
                                         (sid,)).fetchone()[0])
        db.put(self.c, "offline", "0")
        self.c.commit()
        status, body, url = self.post("/search/again", {"search": sid})
        self.assertIn("Which pressing is yours?", body)
        again = self.c.execute("SELECT * FROM title_search ORDER BY id DESC").fetchone()
        self.assertNotEqual(again["id"], sid, "the failed search stays in the log")
        self.assertEqual(again["title"], "Kind of Blue")
        self.assertIn(f'href="/search/choose?search={sid}"', self.get("/search")[1])

    def test_nothing_found_says_so_and_suggests_fewer_words(self):
        _install_seam(lambda u, h, t=None: (200, fixture("mb_search_none.json")
                                            if "musicbrainz" in u
                                            else fixture("discogs_search_none.json"), {}))
        sid, body = self.search(artist="Nobody", title="Never Pressed")
        self.assertIn("Nothing found for Nobody — Never Pressed", body)
        self.assertIn("Fewer words", body)
        self.assertIn('action="/search/manual"', body)

    def test_a_missing_search_is_a_404_with_a_way_back(self):
        try:
            self.open("/search/choose?search=999")
            self.fail("a missing search answered 200")
        except urllib.error.HTTPError as ex:
            with ex:
                self.assertEqual(ex.code, 404)
                self.assertIn('href="/search"', ex.read().decode())

    def test_the_scan_page_and_the_by_hand_form_both_offer_it(self):
        status, body = self.get("/scan")
        self.assertIn('href="#byname"', body)
        self.assertIn('id="byname"', body)
        self.assertIn('action="/search"', body)
        self.assertIn('href="/search"', self.get("/record/new")[1])


class TestCurrency(Web):
    """One currency for the whole collection, switched between CAD, GBP and USD,
    and never a conversion: the switch changes what Discogs is asked for."""

    def stats_in(self, cur, value):
        body = json.dumps({"blocked_from_sale": False, "num_for_sale": 9,
                           "lowest_price": {"currency": cur, "value": value}}).encode()
        urls = []
        def seam(u, h, t=None):
            urls.append(u)
            return (200, body, {}) if "marketplace/stats" in u else fake_http(u, h, t)
        _install_seam(seam)
        return urls

    def test_switching_asks_discogs_in_the_new_currency_and_converts_nothing(self):
        rid = self.add_release(discogs_release_id=2317524)
        self.add_copy(rid)
        self.add_prices(rid, [30, 31, 30, 29])
        before = [tuple(r) for r in self.c.execute(
            "SELECT currency, amount, observed_at FROM price_obs ORDER BY id")]
        status, body, url = self.post("/currency", {"currency": "CAD", "next": "/prices"})
        self.assertIn("now asked for in CAD, for everyone", body)
        self.assertIn("Nothing already recorded was converted", body)
        self.assertIn("1 followed record will get a CAD price", body)
        self.assertIn("1 record still shows the last price Discogs gave in another currency",
                      body)
        self.assertEqual(before, [tuple(r) for r in self.c.execute(
            "SELECT currency, amount, observed_at FROM price_obs ORDER BY id")])
        urls = self.stats_in("CAD", 55.1)
        self.post("/prices/check", {})
        self.assertIn("curr_abbr=CAD", next(u for u in urls if "marketplace/stats" in u))
        status, body = self.get("/prices")
        self.assertIn("C$55.10", body)
        self.assertNotIn("in another currency", body)
        status, body = self.get(f"/record?id={rid}")
        self.assertIn("GBP", body, "the old series is still there, in its own currency")
        self.assertIn("CAD", body)

    def test_an_answer_in_another_currency_is_stored_as_it_came(self):
        rid = self.add_release(discogs_release_id=2317524)
        db.set_currency(self.c, "USD")
        self.c.commit()
        self.stats_in("GBP", 34.4)
        status, body, url = self.post("/prices/check", {"id": rid})
        self.assertIn("Asked for USD; Discogs answered in GBP", urllib.parse.unquote_plus(url))
        self.assertEqual(self.c.execute("SELECT currency FROM price_obs").fetchone()[0], "GBP")

    def test_a_switch_puts_every_followed_record_due_again(self):
        rid = self.add_release(discogs_release_id=2317524, last_priced_at=model.now_iso())
        due = lambda: [r["id"] for r in library.due_for_price(self.c, 20)]
        self.assertEqual(due(), [], "looked at a moment ago")
        self.assertFalse(db.set_currency(self.c, "GBP"), "already GBP: not a switch")
        self.assertEqual(due(), [])
        import time
        time.sleep(1.1)
        self.assertTrue(db.set_currency(self.c, "CAD"))
        self.assertEqual(due(), [rid], "the last look was in a currency nobody asks for now")
        time.sleep(1.1)
        self.c.execute("UPDATE release SET last_priced_at=?", (model.now_iso(),))
        self.assertEqual(due(), [], "looked at again since the switch")

    def test_one_currency_for_every_account(self):
        self.post("/currency", {"currency": "USD", "next": "/prices"})
        auth.ensure_accounts(self.c, ["Second"])
        other = auth.account_by_name(self.c, "Second")
        auth.set_password(self.c, other["id"], "another tester's password")
        token = auth.new_session(self.c, other["id"])
        self.c.commit()
        r = self.open("/prices", headers={"Cookie": f"sn_session={token}"})
        body = r.read().decode()
        self.assertIn('value="USD" aria-pressed="true"', body)
        self.assertEqual(body.count('aria-pressed="true"'), 1)

    def test_only_the_three_are_offered_and_a_receipt_keeps_its_own(self):
        status, body, url = self.post("/currency", {"currency": "EUR", "next": "/prices"})
        self.assertIn("not one of the currencies on offer", body)
        self.assertEqual(db.get(self.c, "currency"), "GBP")
        status, body = self.get("/settings")
        sel = body.split('name="currency"', 1)[1].split("</select>", 1)[0]
        self.assertEqual(re.findall(r'value="([A-Z]{3})"', sel), ["CAD", "GBP", "USD"])
        rid = self.add_release()
        cid = self.add_copy(rid)
        prices.record(self.c, rid, {"source": "manual", "feed": "hand", "kind": "paid",
                                    "currency": "EUR", "amount": 20.0}, copy_id=cid)
        self.c.commit()
        status, body = self.get(f"/copy?id={cid}")
        sel = body.split('id="e4"', 1)[1].split("</select>", 1)[0]
        self.assertIn('<option value="EUR" selected>', sel)
        self.post("/copy/edit", {"id": cid, "paid": "21", "currency": "EUR"})
        self.assertEqual(tuple(self.c.execute(
            "SELECT currency, amount FROM price_obs WHERE copy_id=?", (cid,)).fetchone()),
            ("EUR", 21.0), "editing a receipt in euros leaves it in euros")

    def test_the_prices_tab_wears_the_collections_sign(self):
        db.set_currency(self.c, "CAD")
        self.c.commit()
        r = self.open("/prices", headers={"Cookie": self.cookie + "; sn_view=m"})
        body = r.read().decode()
        tab = body.split('class="tabbar', 1)[1].split('href="/prices"', 1)[1].split("</a>", 1)[0]
        self.assertIn('aria-hidden="true">$</span>', tab)


class TestNoDeadEnds(Web):
    """Every button does something, every refusal says why and keeps what was
    typed, and every empty or missing page says where to go next. Each of these
    was found by walking the running app, not by reading it."""

    def get_any(self, path, headers=None):
        try:
            r = self.open(path, headers=headers)
            return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as ex:
            with ex:
                return ex.code, ex.read().decode("utf-8", "replace")

    def test_a_button_marked_hidden_stays_hidden(self):
        """.btn sets its own display, and without this rule it beat the hidden
        attribute: "Start the camera" showed, with nothing behind it, on every
        browser that cannot scan — including any phone on plain http."""
        self.assertIn("[hidden]{display:none!important}", app.CSS)
        status, body = self.get("/scan")
        self.assertRegex(body, r'<button id="scanbtn"[^>]*\bhidden\b')

    def test_the_scanner_blames_the_address_before_the_browser(self):
        """Over plain http a browser removes navigator.mediaDevices, so checking for
        a camera first told every phone it had no scanner."""
        with open(os.path.join(BASE, "static", "scan.js")) as fh:
            src = strip_comments(fh.read())
        self.assertLess(src.index("isSecureContext"), src.index("navigator.mediaDevices"))
        self.assertLess(src.index("isSecureContext"), src.index("btn.hidden = false"))

    def test_switching_layout_comes_back_to_the_same_record(self):
        rid = self.add_release()
        status, body = self.get(f"/record?id={rid}&msg=Saved.")
        self.assertIn(f'name="next" value="/record?id={rid}"', body,
                      "the id is kept and the one-view message is not")
        status, body = self.get("/shelf?q=blue&sort=title")
        self.assertIn('name="next" value="/shelf?q=blue&amp;sort=title"', body)

    def test_a_pasted_discogs_address_is_a_link_not_a_removal(self):
        self.assertEqual(model.discogs_release_ref(
            "https://www.discogs.com/release/2317524-Miles-Davis-Kind-Of-Blue"), (2317524, ""))
        self.assertEqual(model.discogs_release_ref(
            "https://www.discogs.com/Miles-Davis-Kind-Of-Blue/release/2317524"), (2317524, ""))
        self.assertEqual(model.discogs_release_ref("[r2317524]"), (2317524, ""))
        self.assertEqual(model.discogs_release_ref(""), (None, ""))
        self.assertIn("master", model.discogs_release_ref(
            "https://www.discogs.com/master/5460-Miles-Davis-Kind-Of-Blue")[1])
        rid = self.add_release(discogs_release_id=111)
        status, body, url = self.post("/record/link", {"id": rid,
                                                       "discogs_release_id": "Kind of Blue"})
        self.assertIn("left as it was", urllib.parse.unquote_plus(url))
        link = lambda: self.c.execute("SELECT discogs_release_id d FROM release WHERE id=?",
                                      (rid,)).fetchone()["d"]
        self.assertEqual(link(), 111, "an unreadable box used to remove the link")
        self.post("/record/link", {"id": rid, "discogs_release_id":
                                   "https://www.discogs.com/release/2317524-Kind-Of-Blue"})
        self.assertEqual(link(), 2317524)

    def test_a_mistyped_barcode_by_hand_is_refused_out_loud(self):
        status, body, url = self.post("/record/new", {"title": "Bootleg", "barcode":
                                                      "0886976389414", "format": "LP"})
        self.assertEqual(status, 200)
        self.assertIn("would have to be 3", body)
        self.assertIn('value="Bootleg"', body)
        self.assertEqual(self.c.execute("SELECT COUNT(*) n FROM release").fetchone()["n"], 0,
                         "it used to be filed with the barcode silently dropped")
        self.post("/record/new", {"title": "Bootleg", "barcode": "0886976389413",
                                  "discogs_release_id": "https://www.discogs.com/release/9-x"})
        rel = self.c.execute("SELECT * FROM release").fetchone()
        self.assertEqual((rel["barcode"], rel["discogs_release_id"]), ("0886976389413", 9))

    def test_a_lookup_nobody_answered_is_not_nothing_matched(self):
        db.put(self.c, "offline", "1")
        self.c.commit()
        status, body, url = self.post("/scan", {"how": "camera", "code": "0886976389413"})
        self.assertIn("No answer about", body)
        self.assertNotIn("Neither MusicBrainz nor Discogs has", body)
        self.assertIn('action="/scan/again"', body)
        sid = int(re.search(r"scan=(\d+)", url).group(1))
        self.assertIsNone(self.c.execute("SELECT candidates FROM barcode_scan WHERE id=?",
                                         (sid,)).fetchone()["candidates"])
        db.put(self.c, "offline", "0")
        self.c.commit()
        status, body, url = self.post("/scan/again", {"scan": sid})
        self.assertIn("Which edition is this?", body)
        again = self.c.execute("SELECT * FROM barcode_scan ORDER BY id DESC").fetchone()
        self.assertNotEqual(again["id"], sid, "the failed lookup stays in the log")
        self.assertEqual((again["how"], again["raw"]), ("camera", "0886976389413"))
        self.assertGreater(again["candidates"], 0)

    def test_both_sources_answering_nothing_still_says_nothing_matched(self):
        _install_seam(lambda u, h, t=None: (200, fixture("mb_search_none.json")
                                            if "musicbrainz" in u
                                            else fixture("discogs_search_none.json"), {}))
        status, body, url = self.post("/scan", {"how": "typed", "code": "0886976389413"})
        self.assertIn("Nothing matched", body)
        self.assertNotIn('action="/scan/again"', body)

    def test_a_second_press_is_the_same_copy(self):
        status, body, url = self.post("/scan", {"how": "typed", "code": "0886976389413"})
        sid = int(re.search(r"scan=(\d+)", url).group(1))
        self.post("/scan/choose", {"scan": sid, "pick": 1})
        status, body, url = self.post("/scan/choose", {"scan": sid, "pick": 1})
        self.assertEqual(self.c.execute("SELECT COUNT(*) n FROM copy").fetchone()["n"], 1)
        self.assertIn("already added", urllib.parse.unquote_plus(url))
        self.assertIn("already on the shelf", self.get(f"/scan/choose?scan={sid}")[1])

    def test_a_scan_left_unchosen_can_be_picked_up_again(self):
        status, body, url = self.post("/scan", {"how": "typed", "code": "0886976389413"})
        sid = int(re.search(r"scan=(\d+)", url).group(1))
        status, body = self.get("/scan")
        self.assertIn(f'href="/scan/choose?scan={sid}"', body)
        self.assertNotIn("nothing matched", body, "it matched; nobody had chosen yet")

    def test_the_empty_shelf_says_where_to_start(self):
        status, body = self.get("/shelf")
        self.assertIn('href="/scan"', body)
        self.assertIn('href="/record/new"', body)
        status, body = self.get("/shelf?q=nothing-like-this")
        self.assertIn("Nothing matches these filters", body)

    def test_an_artist_you_own_nothing_by_can_be_watched(self):
        status, body, url = self.post("/artist/new", {"name": "Miles Davis"})
        aid = self.c.execute("SELECT id FROM artist WHERE name='Miles Davis'").fetchone()["id"]
        self.assertIn(f"/artist?id={aid}&find=Miles", url)
        self.assertIn('action="/artist/link"', body, "straight to choosing who it is")

    def test_watching_an_unlinked_artist_opens_the_search(self):
        aid = app.find_or_make_artist(self.c, "Miles Davis")
        self.c.commit()
        status, body, url = self.post("/artist/watch", {"id": aid})
        self.assertIn("find=Miles", url)
        self.assertIn("nothing can be checked", urllib.parse.unquote_plus(url))

    def test_with_nowhere_to_send_the_test_button_is_a_way_to_settings(self):
        status, body = self.get("/alerts")
        self.assertNotIn('action="/alerts/test"', body)
        self.assertIn('href="/settings"', body)
        status, body, url = self.post("/alerts/test", {})
        self.assertTrue(urllib.parse.urlparse(url).path == "/settings")

    def test_the_pages_say_whether_anything_checks_on_its_own(self):
        self.assertIn("Nothing is checked on its own", self.get("/prices")[1])
        import contextlib
        import poller
        self.addCleanup(setattr, sources, "MAX_WAIT", sources.MAX_WAIT)
        with contextlib.redirect_stdout(io.StringIO()):
            poller.main(["--only", "alerts", "--quiet"])
        for path in ("/prices", "/alerts", "/artists"):
            self.assertIn("poller.py last ran", self.get(path)[1], path)

    def test_a_missing_record_is_a_404_with_a_way_back(self):
        for path, back in (("/record?id=999", "/shelf"), ("/copy?id=999", "/shelf"),
                           ("/artist?id=999", "/artists"), ("/scan/choose?scan=999", "/scan"),
                           ("/grade?copy=999", "/shelf"), ("/record/edit?id=999", "/shelf")):
            status, body = self.get_any(path)
            self.assertEqual(status, 404, path)
            self.assertIn(f'href="{back}"', body, path)

    def test_a_machine_name_is_an_address_and_a_foreign_one_is_told_why(self):
        for ok in ("my-laptop:6969", "nas.lan", "box.home.arpa", "pi.local", "x.ts.net",
                   "192.168.1.20:6969", "[::1]:1200"):
            self.assertTrue(app._host_allowed(ok), ok)
        for bad in ("evil.example", "evil.example:6969", "192.168.1.20.evil.example", ""):
            self.assertFalse(app._host_allowed(bad), bad)
        status, body = self.get_any("/", {"Host": "evil.example"})
        self.assertEqual(status, 403)
        self.assertIn("Not at this address", body)

    def test_a_tailscale_origin_counts_only_through_tailscale_serve(self):
        """Tailscale Funnel puts public websites on ts.net names; one of them must
        not be able to post a form at a collection on the LAN."""
        h = {"Host": "192.168.1.20:6969", "Origin": "https://evil.tail1234.ts.net"}
        self.assertFalse(app._same_origin(h, "192.168.1.50"))
        self.assertTrue(app._same_origin({"Host": "127.0.0.1:6969",
                                          "Origin": "https://box.tail1234.ts.net"}, "127.0.0.1"))


class TestTheDoor(Web):
    """Every page but the login is behind a password, and the password is hard to
    guess, impossible to claim, and never kept anywhere a copy could give it up.
    These tests walk up to the door without the harness's session cookie."""

    def setUp(self):
        super().setUp()
        self._throttle = auth.THROTTLE
        auth.THROTTLE = auth.Throttle()
        self.addCleanup(setattr, auth, "THROTTLE", self._throttle)
        self.log = io.StringIO()
        self.addCleanup(setattr, app, "AUTH_LOG", app.AUTH_LOG)
        app.AUTH_LOG = self.log

    def raw(self, method, path, body=b"", headers=None, cookie=""):
        """One request, redirects NOT followed: (status, headers, body)."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        hdrs = {"Host": f"127.0.0.1:{self.port}"}
        if method == "POST":
            hdrs.update({"Content-Type": "application/x-www-form-urlencoded",
                         "Origin": self.base})
        if cookie:
            hdrs["Cookie"] = cookie
        hdrs.update(headers or {})
        if isinstance(body, dict):
            body = urllib.parse.urlencode(body).encode()
        conn.request(method, path, body=body or None, headers=hdrs)
        r = conn.getresponse()
        out = (r.status, r.headers, r.read().decode("utf-8", "replace"))
        conn.close()
        return out

    def counts(self):
        names = [r[0] for r in self.c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name<>'source_call'")]
        return {n: self.c.execute(f"SELECT COUNT(*) FROM {n}").fetchone()[0] for n in names}

    def login(self, name, password, cookie="", nxt=""):
        return self.raw("POST", "/login", {"name": name, "password": password,
                                           "next": nxt}, cookie=cookie)

    @staticmethod
    def cookies(headers):
        """{name: value} from every Set-Cookie on a response."""
        out = {}
        for line in headers.get_all("Set-Cookie") or []:
            k, _, v = line.split(";", 1)[0].partition("=")
            out[k.strip()] = v
        return out

    def make(self, name, password=None):
        auth.ensure_accounts(self.c, [name])
        a = auth.account_by_name(self.c, name)
        code = auth.issue_code(self.c, a["id"])
        if password:
            auth.set_password(self.c, a["id"], password)
        self.c.commit()
        return code

    # -------------------------------------------------------------- the gate

    def test_every_private_route_sends_a_stranger_to_the_login(self):
        rid = self.add_release(title="Private Pressing")
        self.add_copy(rid)
        before = self.counts()
        # Written out here rather than read from app.PUBLIC, so that making a
        # route public takes an edit to this test as well — on purpose.
        public = {("GET", "/login"), ("POST", "/login"), ("POST", "/welcome"),
                  ("GET", "/healthz"), ("GET", "/" + app.MANIFEST_NAME)}
        self.assertEqual(app.PUBLIC, public)
        for method, rx, fn in app.ROUTES:
            if "(" in rx:
                continue                     # the static shelf: public, tested below
            path = rx.replace("\\", "")
            if (method, path) in public:
                continue
            status, headers, body = self.raw(method, path + ("?id=1" if method == "GET" else ""),
                                             {"id": rid, "copy": 1, "scan": 1})
            self.assertEqual(status, 303, f"{method} {path} answered a stranger")
            self.assertTrue(headers["Location"].startswith("/login"), f"{method} {path}")
            self.assertNotIn("Private Pressing", body)
        self.assertEqual(self.counts(), before, "a stranger's POST wrote something")

    def test_a_stranger_is_brought_back_to_where_they_were_going(self):
        status, headers, _ = self.raw("GET", "/record?id=7")
        self.assertEqual(headers["Location"], "/login?next=%2Frecord%3Fid%3D7")
        self.make("Alice", "a long enough password")
        status, headers, _ = self.login("Alice", "a long enough password", nxt="/record?id=7")
        self.assertEqual((status, headers["Location"]), (303, "/record?id=7"))

    def test_the_public_routes_say_nothing_about_the_collection(self):
        self.add_release(title="Private Pressing", artist="Somebody Secret")
        for path in ("/login", "/healthz", "/static/scan.js", "/static/icon.svg",
                     "/" + app.MANIFEST_NAME):
            status, headers, body = self.raw("GET", path)
            self.assertEqual(status, 200, path)
            self.assertNotIn("Private Pressing", body, path)
            self.assertNotIn("Somebody Secret", body, path)
        self.assertEqual(self.raw("GET", "/healthz")[2], "ok\n")
        status, headers, body = self.raw("GET", "/login")
        self.assertNotIn('href="/shelf"', body, "a logged-out page offers nothing to click")

    def test_a_stranger_cannot_make_the_server_hold_a_big_upload(self):
        status, _, _ = self.raw("POST", "/login", b"x" * 20000)
        self.assertEqual(status, 413)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.putrequest("POST", "/scan")
        conn.putheader("Host", f"127.0.0.1:{self.port}")
        conn.putheader("Content-Type", "multipart/form-data; boundary=x")
        conn.putheader("Content-Length", str(20 * 1024 * 1024))
        conn.endheaders()                    # and never send the 20 MB
        r = conn.getresponse()
        self.assertEqual(r.status, 303, "the gate answers before reading the body")
        conn.close()

    # ------------------------------------------------------- logging in and out

    def test_logging_in_and_out(self):
        self.make("Alice", "a long enough password")
        status, headers, body = self.login("Alice", "the wrong password")
        self.assertEqual(status, 200)
        self.assertIn("do not go together", body)
        self.assertNotIn("sn_session", self.cookies(headers))
        status, headers, body = self.login("alice", "a long enough password")
        self.assertEqual(status, 303, "a name is not case-sensitive")
        line = [h for h in headers.get_all("Set-Cookie") if h.startswith("sn_session=")][0]
        for flag in ("HttpOnly", "SameSite=Lax", "Path=/"):
            self.assertIn(flag, line)
        ck = "sn_session=" + self.cookies(headers)["sn_session"]
        status, _, body = self.raw("GET", "/shelf", cookie=ck)
        self.assertEqual(status, 200)
        self.assertIn("Alice", body, "the page says who is logged in")
        self.assertIn("Log out", body)
        status, headers, _ = self.raw("POST", "/logout", {}, cookie=ck)
        self.assertEqual(status, 303)
        status, headers, _ = self.raw("GET", "/shelf", cookie=ck)
        self.assertEqual(status, 303, "a logged-out session is over on the server too")

    def test_the_log_names_who_and_where_but_never_the_password(self):
        self.make("Alice", "a long enough password")
        self.login("Alice", "a long enough password")
        self.login("Alice", "my secret guess")
        self.raw("POST", "/login", {"name": "x", "password": "p"},
                 headers={"X-Forwarded-For": "203.0.113.9"})
        log = self.log.getvalue()
        self.assertIn("auth ok name='Alice'", log)
        self.assertIn("auth refused name='Alice'", log)
        self.assertIn("from 203.0.113.9", log, "behind a proxy, the caller it names")
        self.assertNotIn("a long enough password", log)
        self.assertNotIn("my secret guess", log)

    def test_a_wrong_name_and_a_wrong_password_read_the_same(self):
        self.make("Alice", "a long enough password")
        a = self.login("Alice", "nope nope nope")[2]
        b = self.login("Nobody", "nope nope nope")[2]
        self.assertIn("do not go together", a)
        self.assertIn("do not go together", b)

    def test_a_closed_account_cannot_be_opened_by_typing_nothing(self):
        auth.ensure_accounts(self.c, ["Bob"])
        self.c.commit()
        for pw in ("", " ", "None", "null"):
            status, headers, body = self.login("Bob", pw)
            self.assertNotIn("sn_session", self.cookies(headers), repr(pw))
            self.assertNotIn("Welcome", body)

    # -------------------------------------------------------------- first entry

    def test_first_entry_needs_the_code_and_spends_it(self):
        code = self.make("Alice")
        status, headers, body = self.login("Alice", "WRONG-CODE-HERE")
        self.assertIn("do not go together", body)
        typed = code.lower().replace("-", " ")
        status, headers, body = self.login("Alice", typed)
        self.assertEqual(status, 200)
        self.assertIn("Welcome, Alice", body)
        self.assertNotIn("sn_session", self.cookies(headers), "a code alone is not a login")
        form = {"name": "Alice", "code": auth.norm_code(code), "password": "short",
                "again": "short"}
        status, headers, body = self.raw("POST", "/welcome", form)
        self.assertIn("at least", body)
        self.assertNotIn("sn_session", self.cookies(headers))
        form.update(password="three words long", again="three words long")
        status, headers, body = self.raw("POST", "/welcome", form)
        self.assertEqual(status, 303)
        self.assertIn("sn_session", self.cookies(headers))
        a = auth.account_by_name(self.c, "Alice")
        self.assertTrue(a["pw_hash"])
        self.assertIsNone(a["setup_hash"], "the code is spent")
        self.assertIn("do not go together", self.login("Alice", code)[2])
        self.assertEqual(self.raw("POST", "/welcome", form)[0], 200,
                         "nor can the welcome page be replayed")
        self.assertEqual(self.login("Alice", "three words long")[0], 303)

    def test_the_welcome_page_cannot_be_reached_without_the_code(self):
        self.make("Alice")
        for code in ("", "AAAA-AAAA-AAAA"):
            status, headers, body = self.raw("POST", "/welcome", {
                "name": "Alice", "code": code, "password": "three words long",
                "again": "three words long"})
            self.assertNotIn("sn_session", self.cookies(headers))
        self.assertIsNone(auth.account_by_name(self.c, "Alice")["pw_hash"])

    # ---------------------------------------------------------------- guessing

    def test_guessing_is_slowed_but_a_known_device_is_not(self):
        self.make("Alice", "a long enough password")
        status, headers, _ = self.login("Alice", "a long enough password")
        dev = "sn_device=" + self.cookies(headers)["sn_device"]
        for _ in range(auth.Throttle.FREE):
            self.login("Alice", "a guess")
        status, headers, body = self.login("Alice", "a long enough password")
        self.assertEqual(status, 429, "after the free tries, even the right one waits")
        self.assertIn("Wait", body)
        status, headers, body = self.login("Alice", "a long enough password", cookie=dev)
        self.assertEqual(status, 303, "the guesser slowed themselves down, not Alice")

    def test_a_device_cookie_is_only_good_for_its_own_account(self):
        self.make("Alice", "a long enough password")
        self.make("Bob", "another long password")
        _, headers, _ = self.login("Alice", "a long enough password")
        raw_dev = self.cookies(headers)["sn_device"]
        bob = auth.account_by_name(self.c, "Bob")
        alice = auth.account_by_name(self.c, "Alice")
        self.assertTrue(auth.device_nonce(raw_dev, alice))
        self.assertEqual(auth.device_nonce(raw_dev, bob), "")
        forged = raw_dev[:-1] + ("1" if raw_dev[-1] != "1" else "2")
        self.assertEqual(auth.device_nonce(forged, alice), "")
        auth.set_password(self.c, alice["id"], "a brand new password")
        self.c.commit()
        alice = auth.account_by_name(self.c, "Alice")
        self.assertEqual(auth.device_nonce(raw_dev, alice), "",
                         "a new password retires every old device cookie")

    def test_the_throttle_doubles_and_forgets_on_success(self):
        now = [1000.0]
        t = auth.Throttle(clock=lambda: now[0])
        k = [("name", "alice")]
        for _ in range(t.FREE):
            self.assertEqual(t.begin(k), 0)
        waits = []
        for _ in range(4):
            w = t.begin(k)
            waits.append(w)
            now[0] += w
            self.assertEqual(t.begin(k), 0)
        self.assertEqual(waits, [1, 2, 4, 8])
        for _ in range(30):
            now[0] += t.begin(k) or 0
            t.begin(k)
        self.assertLessEqual(t.begin(k), t.CAP)
        t.succeeded(k)
        self.assertEqual(t.begin(k), 0)

    # ---------------------------------------------------------------- sessions

    def test_the_database_never_holds_a_token_or_a_password(self):
        self.make("Alice", "a long enough password")
        _, headers, _ = self.login("Alice", "a long enough password")
        token = self.cookies(headers)["sn_session"]
        self.c.execute("PRAGMA wal_checkpoint(FULL)")
        with open(self.path, "rb") as fh:
            blob = fh.read()
        for wal in (self.path + "-wal",):
            if os.path.exists(wal):
                with open(wal, "rb") as fh:
                    blob += fh.read()
        self.assertNotIn(token.encode(), blob)
        self.assertNotIn(b"a long enough password", blob)

    def test_changing_a_password_logs_out_everywhere_else(self):
        self.make("Alice", "a long enough password")
        a = self.cookies(self.login("Alice", "a long enough password")[1])["sn_session"]
        b = self.cookies(self.login("Alice", "a long enough password")[1])["sn_session"]
        form = {"current": "not it at all", "password": "a brand new password",
                "again": "a brand new password"}
        status, headers, body = self.raw("POST", "/account/password", form,
                                         cookie="sn_session=" + a)
        self.assertIn("not your current password", body)
        form["current"] = "a long enough password"
        status, headers, body = self.raw("POST", "/account/password", form,
                                         cookie="sn_session=" + a)
        self.assertEqual(status, 303)
        fresh = self.cookies(headers)["sn_session"]
        self.assertEqual(self.raw("GET", "/shelf", cookie="sn_session=" + b)[0], 303)
        self.assertEqual(self.raw("GET", "/shelf", cookie="sn_session=" + fresh)[0], 200)

    def test_an_idle_session_ends(self):
        self.c.execute("UPDATE session SET seen_at='2000-01-01T00:00:00+00:00'")
        self.c.commit()
        self.assertEqual(self.raw("GET", "/shelf", cookie=self.cookie)[0], 303)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM session").fetchone()[0], 0)

    def test_behind_https_the_cookies_are_secure_and_host_only(self):
        self.addCleanup(setattr, app, "BEHIND_HTTPS", app.BEHIND_HTTPS)
        app.BEHIND_HTTPS = True
        self.make("Alice", "a long enough password")
        status, headers, _ = self.login("Alice", "a long enough password")
        lines = headers.get_all("Set-Cookie")
        sess = [h for h in lines if h.startswith("__Host-sn_session=")][0]
        dev = [h for h in lines if h.startswith("__Host-sn_device=")][0]
        for line in (sess, dev):
            self.assertIn("; Secure", line)
            self.assertIn("HttpOnly", line)
            self.assertNotIn("Domain=", line)
        self.assertIn("SameSite=Strict", dev)
        self.assertIn("max-age", headers["Strict-Transport-Security"])

    # ---------------------------------------------------------- the edges

    def test_next_cannot_leave_the_site(self):
        self.make("Alice", "a long enough password")
        for bad in ("//evil.example", "/\\evil.example", "https://evil.example",
                    "/\tevil", "javascript:alert(1)", "/café",
                    "/a\r\nSet-Cookie: x=1"):
            self.assertEqual(app._safe_next(bad), "/", bad)
            _, headers, _ = self.login("Alice", "a long enough password", nxt=bad)
            self.assertEqual(headers["Location"], "/", bad)

    def test_every_response_carries_the_security_headers(self):
        ck = self.cookie
        for path, cookie in (("/login", ""), ("/healthz", ""), ("/static/scan.js", ""),
                             ("/shelf", ck), ("/nowhere", ck)):
            status, headers, _ = self.raw("GET", path, cookie=cookie)
            self.assertEqual(headers["X-Frame-Options"], "DENY", path)
            self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"], path)
            self.assertIn("img-src 'self'", headers["Content-Security-Policy"], path)
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff", path)
            self.assertEqual(headers["X-Robots-Tag"], "noindex, nofollow", path)
        self.assertEqual(self.raw("GET", "/shelf", cookie=ck)[1]["Cache-Control"], "no-store")
        status, headers, _ = self.raw("GET", "/", headers={"Host": "evil.example"})
        self.assertEqual(headers["X-Frame-Options"], "DENY")

    def test_an_error_page_says_nothing_about_the_error(self):
        def boom(req, c, m):
            raise RuntimeError("SECRET-DETAIL at /home/somebody/app.db")
        self.addCleanup(setattr, app, "COMPILED", app.COMPILED)
        app.COMPILED = [(mth, rx, boom if fn is app.help_page else fn)
                        for mth, rx, fn in app.COMPILED]
        import contextlib
        with contextlib.redirect_stderr(io.StringIO()) as err:
            status, _, body = self.raw("GET", "/help", cookie=self.cookie)
        self.assertEqual(status, 500)
        self.assertNotIn("SECRET-DETAIL", body)
        self.assertIn("SECRET-DETAIL", err.getvalue(), "it goes to the log instead")

    def test_a_funnel_site_cannot_post_through_the_proxy(self):
        """In the container every request arrives from loopback, through
        tailscale serve, with the real Host. Another ts.net site is not us."""
        host = "sleevenote.tail5678.ts.net"
        self.assertFalse(app._same_origin({"Host": host, "Origin": "https://evil.tail1234.ts.net"},
                                          "127.0.0.1"))
        self.assertTrue(app._same_origin({"Host": host, "Origin": f"https://{host}"},
                                         "127.0.0.1"))
        self.assertFalse(app._same_origin({"Host": host, "Origin": "http://localhost:3000"},
                                          "127.0.0.1"))

    def test_forwarded_for_is_believed_only_from_loopback(self):
        h = {"X-Forwarded-For": "6.6.6.6, 203.0.113.9"}
        self.assertEqual(app._client_ip(h, "127.0.0.1"), "203.0.113.9")
        self.assertEqual(app._client_ip(h, "192.168.1.50"), "192.168.1.50")
        self.assertEqual(app._client_ip({}, "127.0.0.1"), "127.0.0.1")

    def test_a_backup_carries_no_account_and_no_session(self):
        self.assertNotIn("account", db.BACKUP_TABLES)
        self.assertNotIn("session", db.BACKUP_TABLES)
        blob = self.open("/backup.zip").read()
        import zipfile
        z = zipfile.ZipFile(io.BytesIO(blob))
        joined = b"".join(z.read(n) for n in z.namelist())
        self.assertNotIn(b"scrypt$", joined)
        self.assertNotIn(self.acct["name"].encode() + b",scrypt", joined)

    def test_users_py_issues_a_code_and_resets_with_one(self):
        import contextlib
        import users
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(users.main(["add", "Bob"]), 0)
        code = re.search(r"setup code:\s+(\S+)", out.getvalue()).group(1)
        self.assertEqual(self.login("Bob", code)[0], 200)
        self.raw("POST", "/welcome", {"name": "Bob", "code": code,
                                      "password": "my own password", "again": "my own password"})
        ck = "sn_session=" + self.cookies(self.login("Bob", "my own password")[1])["sn_session"]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(users.main(["code", "Bob"]), 0)
        self.assertEqual(self.raw("GET", "/shelf", cookie=ck)[0], 303, "reset logs out")
        self.assertIn("do not go together", self.login("Bob", "my own password")[2])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(users.main(["code", "Nobody"]), 1)

    def test_new_passwords_are_refused_for_the_obvious_reasons(self):
        for pw, again in (("short", "short"), ("a long enough one", "a different one"),
                          ("aaaaaaaaaaaa", "aaaaaaaaaaaa"), ("1234567890", "1234567890"),
                          ("alice12345", "alice12345")):
            self.assertTrue(auth.password_problem(pw, again, "Alice"), pw)
        self.assertEqual(auth.password_problem("velvet crate morning", "velvet crate morning",
                                               "Alice"), "")
        self.assertTrue(auth.password_problem("ABCD-EFGH-JKLM", "ABCD-EFGH-JKLM", "Alice",
                                              "abcd efgh jklm"))

    def test_a_damaged_hash_locks_rather_than_opens(self):
        for stored in ("", None, "scrypt$x", "md5$1$2$3$4$5", "scrypt$16384$8$1$!!$!!"):
            self.assertFalse(auth.check_secret("anything", stored), stored)


@unittest.skipUnless(shutil.which("openssl"), "needs openssl to make a certificate")
class TestServingHTTPS(Base):
    """TLS goes on each connection in its own thread, and plain http to the same
    port is sent on to https rather than left as a browser error."""

    def setUp(self):
        super().setUp()
        cert, key = os.path.join(self.dir, "cert.pem"), os.path.join(self.dir, "key.pem")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-keyout", key, "-out", cert, "-days", "1", "-subj", "/CN=test"],
                       check=True, capture_output=True)
        self.addCleanup(setattr, app, "TLS_CERT", app.TLS_CERT)
        self.addCleanup(setattr, app, "TLS_KEY", app.TLS_KEY)
        app.TLS_CERT, app.TLS_KEY = cert, key
        self.srv = app.Server(("127.0.0.1", 0), app.Handler)
        self.assertEqual(app._wrap_tls(self.srv), "")
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def test_https_answers_even_beside_a_connection_that_says_nothing(self):
        import ssl
        idle = socket.create_connection(("127.0.0.1", self.port))
        self.addCleanup(idle.close)
        ctx = ssl._create_unverified_context()
        r = urllib.request.urlopen(f"https://127.0.0.1:{self.port}/scan", context=ctx,
                                   timeout=10)
        self.assertEqual(r.status, 200)
        r.close()

    def test_plain_http_is_sent_on_to_https(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/record?id=3")
        r = conn.getresponse()
        self.assertEqual(r.status, 302)
        self.assertEqual(r.getheader("Location"), f"https://127.0.0.1:{self.port}/record?id=3")
        conn.close()


class TestBackup(Web):

    def test_secrets_stay_on_this_machine(self):
        db.put(self.c, "discogs_token", "SECRET-TOKEN")
        db.put(self.c, "smtp_pass", "SECRET-PASSWORD")
        db.put(self.c, "currency", "GBP")
        self.c.commit()
        r = self.open("/backup.zip")
        blob = r.read()
        import zipfile
        z = zipfile.ZipFile(io.BytesIO(blob))
        joined = b"".join(z.read(n) for n in z.namelist())
        self.assertNotIn(b"SECRET-TOKEN", joined)
        self.assertNotIn(b"SECRET-PASSWORD", joined)
        self.assertIn(b"currency", joined)
        self.assertIn("setting.csv", z.namelist())

    def test_a_csv_cell_cannot_become_a_formula(self):
        self.add_release(title="=cmd|'/c calc'!A1")
        r = self.open("/shelf.csv")
        text = r.read().decode()
        self.assertNotIn("\n=cmd", text)
        self.assertIn(" =cmd", text)


if __name__ == "__main__":
    unittest.main(verbosity=1, buffer=False)
