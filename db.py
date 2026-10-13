"""SQLite schema and migrations for Sleevenote. Stdlib only.

Single source of truth for the shape of the data. Every connection turns foreign
keys ON (sqlite3 leaves them OFF) and uses WAL, so a price poll can write while
somebody is reading a page.

Four rules shape this schema. They are worth reading before adding a column.

**1. A record is an edition, and a copy is a thing you can hold.**
`release` is an edition — one pressing, one catalogue number, one barcode. `copy`
is the object on the shelf. The separation is not fussiness: two copies of the
same edition in different condition are worth different money, the same album in
two editions is two different prices, and a barcode identifies the edition and
not the album. Collapsing any of those three distinctions is how a collection
database starts telling its owner things that are not true.

**2. Nothing derivable is stored.** There is no `current_grade` on `copy` — the
grade is the newest `grading` row, and a cached copy of it would drift the first
time a record was re-graded. There is no `owned` flag — a copy is owned when its
`disposed_on` is null. There is no `latest_price`, no `price_change`, no
`is_up` and no alert `headline`. An alert stores the *numbers* it fired on and
the words are written at display time, so an alert can never contradict its own
evidence. If you find yourself adding a column whose value is a function of other
rows, `tests.py:TestNothingDerivable` will fail, and it is right.

**3. What a source said is never destroyed.** `barcode_scan.raw` holds exactly
what was typed or read, including the spaces. `price_obs.payload` holds the JSON
the source answered with. `release.edition_note` holds the source's own words
about the pressing ("Limited Edition, Numbered, 180g") rather than an
interpretation of them. Everything this app *infers* sits beside the evidence,
never on top of it.

**4. Two prices are only comparable if everything about them matches.** A price
carries its source, its kind, its currency and the condition band it was quoted
against, and a series is built from rows where all four agree. There is no
currency conversion anywhere in this app, and there is no table that could hold
an exchange rate, which is the easiest way to keep that promise.

Schema v4. Migrations are additive and idempotent: an older app.db is brought
forward in place by `_migrate`, never dropped.
"""
from __future__ import annotations
import os
import sqlite3

BASE = os.path.dirname(os.path.abspath(__file__))
# Where the collection lives. Alongside the code by default, which is what a
# plain `python3 app.py` checkout wants. SN_DATA moves it onto a volume so a
# container can be rebuilt without taking the collection with it.
DATA_DIR = os.environ.get("SN_DATA") or BASE
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "app.db")

SCHEMA_VERSION = "4"

UX_PRICE_OBS = """CREATE UNIQUE INDEX IF NOT EXISTS ux_price_obs ON price_obs(
        release_id, source, feed, kind, currency, COALESCE(cond_band,-1), observed_at,
        COALESCE(copy_id,-1))"""

SCHEMA = [
 # ---------------------------------------------------------------- artists
 # An artist exists here for two reasons: to group the shelf, and to be watched
 # for new records. `baseline_at` is the stamp of the first successful look at
 # what this artist has released. Until it is set, nothing about this artist can
 # be "new" — see alerts.py. Without it the first poll would announce the whole
 # back catalogue as a new release, which is the classic way a watch feature
 # trains its owner to ignore it.
 """CREATE TABLE IF NOT EXISTS artist (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        sort_name TEXT,
        mbid TEXT UNIQUE,
        discogs_id INTEGER UNIQUE,
        watch INTEGER NOT NULL DEFAULT 0 CHECK(watch IN (0,1)),
        baseline_at TEXT,
        last_checked_at TEXT,
        notes TEXT,
        added_at TEXT NOT NULL)""",

 # ---------------------------------------------------------------- releases
 # One edition. `barcode` is deliberately NOT unique: two editions genuinely
 # share a barcode — the fixtures in this repo hold a real example where one
 # thirteen-digit number belongs to two different Discogs releases of Kind of
 # Blue, one numbered and limited, one not. That fact is the whole reason the
 # scan flow asks which edition you have instead of guessing.
 #
 # `identified` records HOW the edition was settled, which is the difference
 # between "the barcode matched exactly one thing" and "somebody chose from a
 # list". Confidence is not a feeling here; it is a column.
 #
 # `matrix` and `source_notes` are Discogs' own words about the pressing: the
 # stamps in the run-out groove, one per line, and the release notes. Both are
 # NULL until Discogs has been asked and '' when it was asked and listed nothing,
 # so "never looked" and "looked, nothing there" stay two different answers.
 """CREATE TABLE IF NOT EXISTS release (
        id INTEGER PRIMARY KEY,
        title TEXT NOT NULL,
        artist_credit TEXT NOT NULL DEFAULT '',
        artist_id INTEGER REFERENCES artist(id) ON DELETE SET NULL,
        barcode TEXT,
        released TEXT,
        country TEXT,
        label TEXT,
        catno TEXT,
        format TEXT NOT NULL DEFAULT 'unknown',
        speed TEXT NOT NULL DEFAULT 'unknown',
        edition_note TEXT,
        disambiguation TEXT,
        mbid TEXT UNIQUE,
        discogs_release_id INTEGER UNIQUE,
        discogs_master_id INTEGER,
        cover_url TEXT,
        identified TEXT NOT NULL DEFAULT 'manual'
            CHECK(identified IN ('sole_match','chosen','manual','unmatched')),
        source TEXT NOT NULL DEFAULT 'manual'
            CHECK(source IN ('musicbrainz','discogs','manual','scan')),
        tracked INTEGER NOT NULL DEFAULT 1 CHECK(tracked IN (0,1)),
        last_priced_at TEXT,
        notes TEXT,
        added_at TEXT NOT NULL,
        matrix TEXT,
        source_notes TEXT)""",
 "CREATE INDEX IF NOT EXISTS ix_release_barcode ON release(barcode)",
 "CREATE INDEX IF NOT EXISTS ix_release_artist ON release(artist_id)",

 # ------------------------------------------------------------------ copies
 # A physical object. No condition columns — see `grading`. No `owned` flag: a
 # copy is owned while `disposed_on` is null. What was paid for it is not here
 # either; it is a `price_obs` of kind 'paid', because it is a price and this app
 # has exactly one place prices live.
 """CREATE TABLE IF NOT EXISTS copy (
        id INTEGER PRIMARY KEY,
        release_id INTEGER NOT NULL REFERENCES release(id) ON DELETE CASCADE,
        acquired_on TEXT,
        acquired_from TEXT,
        location TEXT,
        notes TEXT,
        disposed_on TEXT,
        disposed_to TEXT,
        disposed_note TEXT,
        added_at TEXT NOT NULL)""",
 "CREATE INDEX IF NOT EXISTS ix_copy_release ON copy(release_id)",

 # ----------------------------------------------------------------- grading
 # A dated opinion about one copy, on the owner's 1-5 scale, recorded separately
 # for the record and the packaging because they wear independently — a mint
 # record in a split sleeve is an ordinary thing to own.
 #
 # A grade may be absent, and an absent grade is not a zero. A sealed record has
 # never been seen, so grading its vinyl would be inventing a fact; the CHECK
 # below makes "no grade" and "the reason there is no grade" travel together, so
 # a blank can always be explained.
 #
 # Re-grading appends. The current grade is the newest row, never a cached copy.
 """CREATE TABLE IF NOT EXISTS grading (
        id INTEGER PRIMARY KEY,
        copy_id INTEGER NOT NULL REFERENCES copy(id) ON DELETE CASCADE,
        graded_on TEXT,
        media_grade INTEGER CHECK(media_grade IS NULL OR media_grade BETWEEN 1 AND 5),
        media_ungraded TEXT
            CHECK(media_ungraded IS NULL OR media_ungraded IN ('sealed','not_played','not_checked')),
        sleeve_grade INTEGER CHECK(sleeve_grade IS NULL OR sleeve_grade BETWEEN 1 AND 5),
        sleeve_ungraded TEXT
            CHECK(sleeve_ungraded IS NULL OR sleeve_ungraded IN ('sealed','not_played','not_checked')),
        note TEXT,
        graded_at TEXT NOT NULL,
        CHECK((media_grade IS NULL) != (media_ungraded IS NULL)),
        CHECK((sleeve_grade IS NULL) != (sleeve_ungraded IS NULL)))""",
 "CREATE INDEX IF NOT EXISTS ix_grading_copy ON grading(copy_id, graded_at)",

 # ------------------------------------------------------------------- scans
 # Every scan, whether or not it went anywhere. `raw` is byte-for-byte what was
 # typed or what the camera read, spaces and all, because a barcode that failed
 # its checksum is evidence about a misread and normalising it away destroys the
 # only clue. `candidates` is how many editions the lookup offered at that
 # moment — not derivable later, because the sources change under us — and NULL
 # when a source did not answer, because then the count is not the whole answer
 # and "nothing matched" would be a claim nobody made.
 """CREATE TABLE IF NOT EXISTS barcode_scan (
        id INTEGER PRIMARY KEY,
        raw TEXT NOT NULL,
        code TEXT,
        how TEXT NOT NULL DEFAULT 'typed' CHECK(how IN ('typed','camera','photo')),
        read_votes INTEGER,
        release_id INTEGER REFERENCES release(id) ON DELETE SET NULL,
        candidates INTEGER,
        candidates_json TEXT,
        note TEXT,
        scanned_at TEXT NOT NULL)""",
 "CREATE INDEX IF NOT EXISTS ix_scan_at ON barcode_scan(scanned_at)",

 # ------------------------------------------------------- searches by name
 # The other way into the collection, for a record with no barcode — most
 # records older than about 1980. What was typed is kept as typed, the editions
 # offered are kept exactly as they were offered, and `candidates` is NULL when
 # a source did not answer, for the same reasons as on barcode_scan. Its own
 # table rather than a kind of scan: a barcode scan's `raw` is evidence about a
 # misread, and a typed title is not a misread of anything.
 #
 # `totals_json` is how many editions each source said it lists, which is more
 # than it sent: "the first 25 of 359" tells the owner to narrow the search,
 # and that sentence needs the 359.
 """CREATE TABLE IF NOT EXISTS title_search (
        id INTEGER PRIMARY KEY,
        artist TEXT NOT NULL DEFAULT '',
        title TEXT NOT NULL DEFAULT '',
        catno TEXT NOT NULL DEFAULT '',
        release_id INTEGER REFERENCES release(id) ON DELETE SET NULL,
        candidates INTEGER,
        candidates_json TEXT,
        totals_json TEXT,
        note TEXT,
        searched_at TEXT NOT NULL)""",
 "CREATE INDEX IF NOT EXISTS ix_title_search_at ON title_search(searched_at)",

 # ------------------------------------------------------------------ prices
 # One observation of one number on one day. Never an average, never a change,
 # never a "current price" — those are all functions of these rows and live in
 # prices.py.
 #
 # `kind` says what the number IS ('lowest_asking' is one seller's hope, not a
 # valuation) and `cond_band` says which condition it was quoted against, or
 # null when the source did not say. `payload` keeps the source's own answer.
 # The UNIQUE constraint makes a poll idempotent: running the poller twice in one
 # second cannot put the same observation in twice.
 #
 # `feed` names the exact endpoint the number came from, and it earns its place.
 # Discogs will tell you the lowest asking price for one release, in one currency,
 # at one moment, through two different endpoints — and the two do not agree. The
 # recorded evidence is in this repo's own fixtures, both taken seconds apart for
 # release 2317524 with curr_abbr=GBP: marketplace/stats says the lowest price is
 # £34.40 and the release endpoint says 51.43. Whatever the explanation, they are
 # measuring different things, and a series that mixed them would show a 49%
 # crash and a 49% recovery every time the poller happened to use the other one.
 # So `feed` is part of what makes two prices comparable, and it is part of the
 # series key in prices.py and of the UNIQUE below.
 """CREATE TABLE IF NOT EXISTS price_obs (
        id INTEGER PRIMARY KEY,
        release_id INTEGER NOT NULL REFERENCES release(id) ON DELETE CASCADE,
        copy_id INTEGER REFERENCES copy(id) ON DELETE SET NULL,
        observed_at TEXT NOT NULL,
        source TEXT NOT NULL CHECK(source IN ('musicbrainz','discogs','manual','scan')),
        feed TEXT NOT NULL DEFAULT 'hand'
            CHECK(feed IN ('discogs_stats','discogs_release','hand')),
        kind TEXT NOT NULL CHECK(kind IN
            ('lowest_asking','asking_seen','sale_seen','paid','sold','suggested')),
        currency TEXT NOT NULL,
        amount REAL NOT NULL CHECK(amount >= 0),
        cond_band INTEGER CHECK(cond_band IS NULL OR cond_band BETWEEN 1 AND 5),
        cond_text TEXT,
        num_for_sale INTEGER,
        url TEXT,
        payload TEXT,
        note TEXT)""",
 # The uniqueness rule has to be exactly the series key plus the timestamp, and
 # that includes cond_band — two prices quoted for two conditions at one moment
 # are two observations, not one. It cannot be a table-level UNIQUE, because
 # SQLite counts NULLs as distinct there and cond_band is null whenever a source
 # declines to state a condition, which is most of the time: the constraint would
 # then never fire for the common case and a poller run twice would double-count.
 # COALESCE in an expression index gives both halves.
 #
 # The copy is part of it too. A market observation has no copy, so for those the
 # rule is unchanged; but two copies of one edition bought on the same day are two
 # receipts, and without the copy the second was silently dropped.
 UX_PRICE_OBS,
 """CREATE INDEX IF NOT EXISTS ix_price_series
        ON price_obs(release_id, kind, currency, feed, observed_at)""",

 # ------------------------------------------------------------- alert rules
 # A rule with a null release_id and a null artist_id is the default that applies
 # to everything tracked; a row naming one release overrides it for that release.
 # One mechanism, two uses.
 #
 # Both thresholds must be crossed, which is the point of having two: 30% of a
 # £4 single is £1.20 and is noise, while £15 on a £400 box set is not a story
 # either. `min_obs` and `window_days` are what stop a series with three points
 # in it from having opinions.
 """CREATE TABLE IF NOT EXISTS alert_rule (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL CHECK(kind IN ('price_move','new_release')),
        release_id INTEGER REFERENCES release(id) ON DELETE CASCADE,
        artist_id INTEGER REFERENCES artist(id) ON DELETE CASCADE,
        direction TEXT NOT NULL DEFAULT 'both' CHECK(direction IN ('up','down','both')),
        pct_threshold REAL,
        abs_threshold REAL,
        min_obs INTEGER,
        window_days INTEGER,
        cooldown_days INTEGER,
        active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
        note TEXT,
        created_at TEXT NOT NULL)""",
 "CREATE INDEX IF NOT EXISTS ix_rule_scope ON alert_rule(kind, release_id, artist_id)",

 # ------------------------------------------------------------------ alerts
 # A raised alert keeps the numbers it fired on AND a snapshot of the thresholds
 # that were in force, because a rule can be edited afterwards and an alert must
 # still be able to say why it fired. The sentence a person reads is written at
 # display time out of these columns — storing the sentence would let it drift
 # away from the evidence beside it.
 #
 # `move_kind` separates the two things a watch on an artist can actually see: a
 # record that has just come out, and a record that has just been added to a
 # public database and may be forty years old. Reporting the second as the first
 # is a lie the app is structured to be incapable of telling.
 """CREATE TABLE IF NOT EXISTS alert (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL CHECK(kind IN ('price_move','new_release')),
        release_id INTEGER REFERENCES release(id) ON DELETE CASCADE,
        artist_id INTEGER REFERENCES artist(id) ON DELETE CASCADE,
        rule_id INTEGER REFERENCES alert_rule(id) ON DELETE SET NULL,
        raised_at TEXT NOT NULL,
        direction TEXT CHECK(direction IS NULL OR direction IN ('up','down')),
        currency TEXT,
        from_amount REAL,
        to_amount REAL,
        from_at TEXT,
        to_at TEXT,
        baseline_n INTEGER,
        pct REAL,
        abs_change REAL,
        for_sale_then REAL,
        for_sale_now INTEGER,
        series_source TEXT,
        series_kind TEXT,
        cond_band INTEGER,
        rule_pct REAL,
        rule_abs REAL,
        rule_min_obs INTEGER,
        rule_window_days INTEGER,
        move_kind TEXT CHECK(move_kind IS NULL OR move_kind IN ('new_release','newly_listed')),
        item_title TEXT,
        item_released TEXT,
        item_url TEXT,
        item_source TEXT,
        item_format TEXT,
        seen_at TEXT,
        dismissed_at TEXT)""",
 "CREATE INDEX IF NOT EXISTS ix_alert_open ON alert(dismissed_at, raised_at)",

 # -------------------------------------------------------------- deliveries
 # Every attempt to send an alert somewhere, successful or not. A notification
 # feature that cannot say whether the email left is a notification feature
 # nobody can trust.
 """CREATE TABLE IF NOT EXISTS delivery (
        id INTEGER PRIMARY KEY,
        alert_id INTEGER NOT NULL REFERENCES alert(id) ON DELETE CASCADE,
        channel TEXT NOT NULL CHECK(channel IN ('email','webhook')),
        attempted_at TEXT NOT NULL,
        ok INTEGER NOT NULL CHECK(ok IN (0,1)),
        detail TEXT)""",
 "CREATE INDEX IF NOT EXISTS ix_delivery_alert ON delivery(alert_id)",

 # ------------------------------------------------- what an artist has out
 # The ledger that makes "new" mean something: everything a source has ever told
 # us this artist released. An addition to this table is the only evidence the
 # app has that something is new, and `first_seen_at` is when WE saw it, kept
 # separate from `released`, which is when the record came out. Conflating those
 # two is how a reissue of a 1971 album gets announced as a new album.
 """CREATE TABLE IF NOT EXISTS artist_release (
        id INTEGER PRIMARY KEY,
        artist_id INTEGER NOT NULL REFERENCES artist(id) ON DELETE CASCADE,
        source TEXT NOT NULL CHECK(source IN ('musicbrainz','discogs')),
        source_id TEXT NOT NULL,
        title TEXT NOT NULL,
        released TEXT,
        primary_type TEXT,
        format_note TEXT,
        url TEXT,
        first_seen_at TEXT NOT NULL,
        UNIQUE(artist_id, source, source_id))""",

 # ---------------------------------------------------------------- settings
 # Configuration that belongs to the collection rather than to a browser. Two
 # keys hold secrets and are named in SECRET_KEYS; the backup export skips them
 # and says so, because a backup is a file people email to themselves.
 """CREATE TABLE IF NOT EXISTS setting (
        key TEXT PRIMARY KEY,
        value TEXT,
        updated_at TEXT)""",

 # ------------------------------------------------------------ source calls
 # Every request this app makes to a public API. Two jobs: it is the floor the
 # rate limiter measures from, so restarting the app cannot be used to burst past
 # a published limit, and it is the page that answers "what has this thing been
 # telling the internet about me". Pruned to the most recent PRUNE_CALLS rows.
 """CREATE TABLE IF NOT EXISTS source_call (
        id INTEGER PRIMARY KEY,
        called_at TEXT NOT NULL,
        host TEXT NOT NULL,
        path TEXT,
        status INTEGER,
        ms INTEGER,
        note TEXT)""",
 "CREATE INDEX IF NOT EXISTS ix_call_at ON source_call(called_at)",

 """CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT)""",

 # ------------------------------------------------------------ who logs in
 # The people who can log in, all with the same rights over the one collection.
 # `pw_hash` and `setup_hash` are scrypt (auth.py): an account with neither has
 # no way in, one with only a setup code is waiting for its first entry. Both
 # tables are left out of BACKUP_TABLES — a backup is a file people email to
 # themselves, and a password hash is not something to email.
 """CREATE TABLE IF NOT EXISTS account (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE COLLATE NOCASE,
        pw_hash TEXT,
        setup_hash TEXT,
        pw_set_at TEXT,
        added_at TEXT NOT NULL)""",

 # A login. `id` is the SHA-256 of the token in the browser's cookie, never the
 # token itself, so a copy of this file cannot be replayed as a login.
 """CREATE TABLE IF NOT EXISTS session (
        id TEXT PRIMARY KEY,
        account_id INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
        created_at TEXT NOT NULL,
        seen_at TEXT NOT NULL)""",
 "CREATE INDEX IF NOT EXISTS ix_session_account ON session(account_id)",
]

# Tables a backup carries. `source_call` is left out on purpose: it is a log of
# this machine's network traffic, it regrows on its own, and it is the one table
# whose loss costs nothing.
BACKUP_TABLES = ("artist", "release", "copy", "grading", "barcode_scan", "title_search",
                 "price_obs", "alert_rule", "alert", "delivery", "artist_release", "setting",
                 "meta")

# Settings whose values never leave this machine in an export.
SECRET_KEYS = ("discogs_token", "smtp_pass", "webhook_url")

# Tables that must exist for a restored file to be this app's database.
REQUIRED_TABLES = ("release", "copy", "grading", "price_obs", "alert", "setting")

PRUNE_CALLS = 2000

# The defaults for a price-move alert, as seeded into the one global rule. Chosen
# to be dull on purpose: see prices.py for why both thresholds exist and why a
# window median rather than the previous point is the thing compared against.
DEFAULTS = {
    "currency": "GBP",
    "move_pct": "20",
    "move_abs": "3.00",
    "move_min_obs": "3",
    "move_window_days": "30",
    "move_cooldown_days": "14",
    "price_max_age_hours": "20",
    "user_agent_contact": "",
    "discogs_token": "",
    "smtp_host": "", "smtp_port": "587", "smtp_user": "", "smtp_pass": "",
    "smtp_from": "", "smtp_to": "", "smtp_tls": "1",
    "webhook_url": "",
    "offline": "0",
}


def connect(path=None):
    """A connection with foreign keys on, WAL, and a row factory.

    `path` defaults to None rather than to DB_PATH, and that is not a style
    choice. Binding the default as `path=DB_PATH` captures the value at import
    time, so a test that sets `db.DB_PATH` to a temporary file afterwards would
    change nothing at all and every handler would keep writing to the real
    collection, silently. Reading the global here means a redirect works.
    `tests.py:TestDbPathRedirect` proves it still does.
    """
    conn = sqlite3.connect(path or DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=8000")
    return conn


def init_db(path=None):
    """Create or bring forward the database, and return an open connection."""
    c = connect(path)
    fresh = not c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='release'").fetchone()
    for stmt in SCHEMA:
        c.execute(stmt)
    if fresh:
        _seed(c)
        c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('schema_version',?)",
                  (SCHEMA_VERSION,))
    else:
        _migrate(c)
    c.commit()
    return c


def _seed(c):
    """A new database: the settings defaults and the one global price rule.

    No records, no artists, no prices. There is no history to seed here and
    inventing a collection to look at would be inventing facts — `demo.py`
    writes a separate file for that, and says so on every page.
    """
    from model import now_iso
    now = now_iso()
    for k, v in DEFAULTS.items():
        c.execute("INSERT OR IGNORE INTO setting(key,value,updated_at) VALUES(?,?,?)",
                  (k, v, now))
    c.execute("""INSERT INTO alert_rule(kind,release_id,artist_id,direction,pct_threshold,
                     abs_threshold,min_obs,window_days,cooldown_days,active,note,created_at)
                 VALUES('price_move',NULL,NULL,'both',?,?,?,?,?,1,?,?)""",
              (float(DEFAULTS["move_pct"]), float(DEFAULTS["move_abs"]),
               int(DEFAULTS["move_min_obs"]), int(DEFAULTS["move_window_days"]),
               int(DEFAULTS["move_cooldown_days"]),
               "The default, applied to every tracked record that has no rule of its own.",
               now))
    c.execute("""INSERT INTO alert_rule(kind,release_id,artist_id,active,note,created_at)
                 VALUES('new_release',NULL,NULL,1,?,?)""",
              ("The default for watched artists.", now))


def _cols(c, table):
    return {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}


def _migrate(c):
    """Bring an older app.db forward. Additive and idempotent, always.

    Every step is guarded by a check on what is actually there, so running this
    twice is a no-op and a database several versions behind comes forward in one
    pass. When a step adds a column, the same column must be added to SCHEMA
    above — a fresh database and a migrated one that disagree about their columns
    is the defect this shape exists to prevent, and tests.py compares them.
    """
    for k, v in DEFAULTS.items():
        c.execute("INSERT OR IGNORE INTO setting(key,value,updated_at) VALUES(?,?,?)",
                  (k, v, None))
    # v2: what Discogs says about a pressing — run-out stamps and release notes.
    have = _cols(c, "release")
    for col in ("matrix", "source_notes"):
        if col not in have:
            c.execute(f"ALTER TABLE release ADD COLUMN {col} TEXT")
    # v2: the copy joins the uniqueness rule on price_obs. CREATE ... IF NOT EXISTS
    # leaves an older index of the same name alone, so it is replaced here. The new
    # rule is strictly finer than the old, so no existing row can break it.
    ux = c.execute("""SELECT sql FROM sqlite_master WHERE type='index'
                      AND name='ux_price_obs'""").fetchone()
    if ux and "copy_id" not in (ux["sql"] or ""):
        c.execute("DROP INDEX ux_price_obs")
        c.execute(UX_PRICE_OBS)
    # v3: `account` and `session` are new tables, so the CREATE ... IF NOT EXISTS
    # pass in init_db has already made them; there is no column to add.
    # v4: `title_search` is a new table, made by that same pass.
    c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('schema_version',?)",
              (SCHEMA_VERSION,))


def schema_version(c):
    r = c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    return r["value"] if r else ""


# ------------------------------------------------------------------ settings

def get(c, key, default=""):
    r = c.execute("SELECT value FROM setting WHERE key=?", (key,)).fetchone()
    if r is None or r["value"] is None:
        return DEFAULTS.get(key, default)
    return r["value"]


def get_int(c, key, default=0):
    try:
        return int(str(get(c, key, "")).strip())
    except (TypeError, ValueError):
        return default


def get_float(c, key, default=0.0):
    try:
        return float(str(get(c, key, "")).strip())
    except (TypeError, ValueError):
        return default


def get_bool(c, key):
    return str(get(c, key, "0")).strip() in ("1", "true", "yes", "on")


def put(c, key, value):
    from model import now_iso
    c.execute("""INSERT INTO setting(key,value,updated_at) VALUES(?,?,?)
                 ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                 updated_at=excluded.updated_at""", (key, value, now_iso()))


def set_currency(c, cur):
    """Ask for prices in `cur` from now on, for everyone. True if it changed.

    One setting for the whole collection, because every account manages the same
    shelf. Nothing already recorded is touched: a price is stored in the currency
    it was quoted in, so after a switch the old series simply stops growing and a
    new one starts. The moment of the switch is kept as `currency_since`, because
    a price looked up yesterday in the old currency is not the one the owner just
    asked to see — `library.due_for_price` treats every record not looked up since
    then as due. Anything but the three choices is refused and changes nothing.
    """
    from model import CURRENCY_CHOICES, now_iso
    if cur not in CURRENCY_CHOICES or cur == get(c, "currency", "GBP"):
        return False
    put(c, "currency", cur)
    put(c, "currency_since", now_iso())
    return True


def prune_calls(c, keep=PRUNE_CALLS):
    c.execute("""DELETE FROM source_call WHERE id NOT IN
                 (SELECT id FROM source_call ORDER BY id DESC LIMIT ?)""", (keep,))
