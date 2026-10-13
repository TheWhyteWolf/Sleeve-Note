# Sleevenote — how it is put together

Read this before changing anything. It covers the reasoning, the sharp edges,
and the recipes for common changes.

---

## §0 The rules

Nine, in rough order of how much damage breaking them does.

**1. Nothing derivable is stored.** There is no `current_grade` on a copy — the
grade is the newest `grading` row. There is no `owned` flag — a copy is owned
while its `disposed_on` is null. No `latest_price`, no `price_change`, no
`is_up`, and no alert `headline`. An alert stores the *numbers* it fired on and
the sentence is written at display time, so an alert can never contradict the
evidence printed beneath it. `tests.py:TestNothingDerivable` holds a list of
column names that must not exist.

**2. What a source said is never destroyed.** `barcode_scan.raw` is exactly what
was typed or read, spaces and all — a barcode that failed its checksum is the
evidence about a misread, and normalising it away throws the clue out.
`barcode_scan.candidates_json` keeps the editions that were offered.
`price_obs.payload` keeps the source's own JSON. Everything this app *infers*
sits beside the evidence, never on top of it.

**3. Two prices are comparable only if six things match.** Release, source, feed,
kind, currency and condition band. That tuple is `prices.SERIES_KEY`, it is the
unique index on `price_obs`, and it is what `assess()` works within. Two of the
six are non-obvious and both came out of measurement — see §10.

**4. No currency is ever converted.** There is no exchange rate in this app and
no table that could hold one. Two currencies are two series and two totals. The
collection's currency — CAD, GBP or USD, one setting for every account — is what
Discogs is *asked* for. `db.set_currency` switches it, converts nothing, and
stamps `currency_since`, which `library.due_for_price` reads to put every record
looked up before the switch due again.

**5. There is no auto-escaping.** Every database or user string interpolated into
HTML goes through `e()`. Every SQL value is a `?` placeholder.
`tests.py:TestEscaping` puts `<script>` and an `onerror` attribute into every
text column and loads every page.

**6. The dispatcher never commits.** A handler that writes calls `c.commit()`
itself, and every POST ends in a redirect. `tests.py:TestPRG` checks the write is
visible from a second connection. A POST that *refuses* its input and writes
nothing re-draws its form instead, with what was typed and the reason, as `/scan`
always has — a typo must cost one correction, not the whole form.

**7. JavaScript may improve a page; it may never carry a fact.** The camera
scanner fills in a text box and stops. Every barcode is checksummed by the server
when the POST lands, and every page works with scripting off. Delete `static/`
and the only thing lost is not having to type thirteen digits.
`tests.py:TestPromises` greps the shelf for `fetch`, `XMLHttpRequest`,
`sendBeacon`, `.submit(`, `localStorage` and `indexedDB`.

**8. Never write to the real `app.db` from a test or a script.** The trap:
`def connect(path=DB_PATH)` binds the value at import
time, so setting `db.DB_PATH` afterwards does nothing and every handler keeps
writing to the real collection, silently. `db.connect()` takes `path=None` and
reads the global at call time for exactly this reason;
`tests.py:TestDbPathRedirect` proves the redirect still works, and the web tests
check `PRAGMA database_list` before serving a request.

**9. Every page is private unless it is named in `PUBLIC`.** The gate in
`Handler._serve` runs before the route is looked up and before the body is
read, and it is default-deny: a route added tomorrow needs a login without
anyone remembering to ask for one. `PUBLIC` holds five entries — the login
form, the two POSTs that log in, `/healthz` and the web manifest — plus the
static shelf. `tests.py:TestTheDoor` walks every route in `ROUTES` without a
session and spells the public list out itself, so making a route public takes
an edit to the test as well. See §8, *The door*.

---

## §1 The modules

```
app.py ──────┬── library.py ──┬── prices.py ── model.py
             │                └── db.py, model.py
             ├── alerts.py ───┬── prices.py
             │                ├── sources.py ── db.py, model.py
             │                └── db.py, model.py
             ├── barcode.py           (pure; no db, no network)
             ├── sources.py
             ├── db.py ── model.py
             └── model.py             (pure; no imports of ours at all)

app.py ── auth.py ── model.py         (accounts, sessions, the throttle)
users.py ── auth, db                 (the server-side tool: setup codes, resets)
poller.py ── alerts, library, prices, sources, db, model
imgtool.py                           (subprocess only; the only Pillow importer)
```

`model.py` is the bottom of the stack and imports nothing of ours. `barcode.py`
imports nothing of ours either. Both are pure functions of their arguments, which
is why the interesting parts of each can be tested thousands of times over
without a database.

**Two doors to the outside, deliberately apart.** `sources._http_get` is the only
function that *reads* from the internet. `alerts._smtp_send` and
`alerts._http_post` are the only two that *write* to it. They are separate
because their privacy properties are opposite: reading sends a barcode, sending
a notification sends the name of a record you own to a server you nominated.
`tests.py:TestOneDoorOut` greps for `urlopen`, `socket.` and `smtplib` outside
those three functions, and the test suite points the read door at a sentinel that
fails the run if anything reaches it.

---

## §2 The schema (`db.py`, v4)

`SCHEMA` is a list of statements; `_migrate` brings an older file forward and is
additive and idempotent. A fresh database and a migrated one must declare the
same columns — `tests.py:TestSchema` compares them column by column.

The shape worth understanding:

```
artist ──< release ──< copy ──< grading
                │        └──< price_obs (kind='paid'/'sold', copy_id set)
                ├──< price_obs (market observations, copy_id null)
                ├──< barcode_scan
                ├──< title_search    (v4: a search by name that became this record)
                └──< alert_rule, alert
artist ──< artist_release        (the ledger that makes "new" mean something)
```

* **`release` is an edition.** `barcode` is deliberately **not** unique: two
  editions genuinely share one (§10.1). `matrix` and `source_notes` (v2) hold
  Discogs' own words about the pressing — the run-out stamps and the release
  notes. Both are NULL until Discogs has been asked and `''` when it was asked
  and listed nothing, so "never looked" and "nothing there" stay apart.
* **`copy` holds no condition columns.** Grading appends to `grading`; the
  current grade is the newest row.
* **A grade may be absent, and an absent grade is not a zero.** Two CHECK
  constraints make "no grade" and "the reason there is no grade" travel together,
  so a blank can always be explained. A sealed record has never been seen, so
  grading its vinyl would be inventing a fact.
* **All prices live in `price_obs`,** including what you paid. A purchase is a
  price; giving it a column on `copy` would mean two places prices live.
  `model.MARKET_KINDS` excludes `paid` and `sold`, so a purchase is never
  compared against the market. `model.COPY_KINDS` names those two: they are
  recorded on a copy's page, one of each per copy, and a second entry corrects
  the first rather than sitting beside it.
* **`artist.baseline_at`** is the stamp of the first complete sweep. Until it is
  set, nothing about that artist can be "new".
* **`title_search`** (v4) is the other way in: a search by artist, title and
  catalogue number, with the editions offered kept as offered and `candidates`
  NULL when a source did not answer, exactly as on `barcode_scan`. It is its own
  table rather than a kind of scan because a scan's `raw` is evidence about a
  misread, and a typed title is not a misread of anything. `totals_json` is how
  many editions each source said it lists, which is what "the first 25 of 359"
  needs.
* **`setting`** holds configuration; `db.SECRET_KEYS` names the three values the
  backup leaves out. `currency` and `currency_since` are written together, only
  by `db.set_currency`.
* **`source_call`** is every request the app has made. It is both the rate
  limiter's floor and the page that answers "what has this been telling the
  internet about me".
* **`account` and `session`** (v3) are who can log in. `account.pw_hash` and
  `setup_hash` are scrypt; an account with neither has no way in. `session.id`
  is the SHA-256 of the cookie's token, never the token, so a copy of the file
  cannot be replayed as a login. Neither table is in `BACKUP_TABLES`: a backup
  is a file people email to themselves.

---

## §3 The scan flow

```
GET  /scan          camera (JS) | type it | photograph it
POST /scan          normalise, checksum, log the scan, ask both catalogues
GET  /scan/choose   the editions, grouped by source, with what differs
POST /scan/choose   complete the pick from its source, create the release + a copy  ->  /grade
POST /scan/manual   nothing matched, or none of them  ->  /grade
POST /scan/again    a source did not answer: ask again, as a new scan row
GET/POST /grade     the two 1-5 scales, with the words
POST /record/fetch  later: ask for whatever the pick could not get at the time
```

The candidate list is stored on the scan row as JSON rather than re-fetched, for
two reasons: asking again costs a request against a published rate limit, and the
answer would be different. Months later you can still see what the options were
when you chose.

`differs()` is what makes the choosing screen useful — it reports which fields
actually vary. **Only ever call it within one source.** Comparing across sources
measures the wrong thing: MusicBrainz says *2010-05, NL* and Discogs says *2010,
Europe* about what is very likely the same record, and calling that "these
editions differ in year and country" describes two catalogues rather than two
records. `differs_by_source()` is the one the pages use.

A release is looked up by the source's own id before being created, so scanning
the same record twice adds a second **copy**, not a second record. And **one
scan is one copy**: once a scan has become a record, pressing a choice on it again
(a double tap, the back button) goes to that record instead of filing another.

**Finding a record by name** is the same flow with a different first step, for a
record with no barcode — most records pressed before about 1980:

```
GET/POST /search          artist, title, catalogue number  ->  both catalogues, vinyl only
GET  /search/choose       the editions, Discogs first, each headed year · country · label catno
POST /search/choose       fuller(), then release + copy, identified 'chosen'  ->  /grade
POST /search/manual       none of these, with what was typed  ->  /grade, 'unmatched'
POST /search/again        a source did not answer: a new title_search row
```

A title or a catalogue number is required: an artist alone is a discography.
Discogs is listed first because it holds far more old pressings and because a
Discogs edition is one whose price can be followed. An edition already on the
shelf is marked by the source's own id, never by a title that looks the same.
A pick is always `chosen`, even from one result — a name matching one thing is
somebody's choice, not a barcode matching one edition — and the record page words
`chosen` and `unmatched` by whichever way in came first (`_how_identified`).
A name search has no scanned code to check a barcode against, so it claims none
it cannot stand behind; see §10.10.

**"Nothing matched" and "nobody answered" are different facts.** `candidates` is
NULL when a source did not answer (offline, rate-limited, unreachable), and the
choosing page then says so and offers *Look it up again* instead of telling the
owner that neither catalogue has the barcode. `lookup_barcode(..., unanswered)`
reports which hosts failed. Asking again writes a new scan row with the same
`raw` and `how`, so the failed lookup stays in the log. The scan page's *Lately*
list links every scan that never became a record back to where it stopped.

**Once an edition is chosen, one more request completes it** (`sources.fuller`).
A MusicBrainz pick asks MusicBrainz for the Discogs release it links to — the
published link is the only honest ground for treating the two catalogues'
entries as one object, and without it a record identified through MusicBrainz
has no price to follow. Such a pick therefore carries both ids, so it finds the
row an earlier Discogs pick of the same edition made, and that row gains the id
it lacked. A Discogs pick is read in full: the month where the search gave only
a year, artist and title as separate fields rather than split out of
"Artist - Title", and the run-out stamps and notes. An answer is used only if it
is about the release that was asked for, and a source that will not answer never
stops a record being added — the record's page offers to ask again, and fills
only blanks when it does. `candidates_json` is never rewritten: it is the
evidence of what the choice was between.

---

## §4 The barcode decoder (`barcode.py`)

The only part of this app that can be *confidently wrong*. A misread that happens
to pass its own check digit files the wrong record against the wrong prices, and
nothing downstream can detect it. Everything below is shaped by that.

**Runs, not modules.** EAN-13 is 95 modules: a `101` guard, six digits of seven
modules, a `01010` guard, six more, a `101` guard. Each digit is exactly four
runs of ink and paper summing to seven modules. On a photograph a module may be
2.3 pixels wide and there is no honest way to round that, but a run of four
modules is nine pixels and rounds cleanly. So the decoder only ever measures
runs.

**The first digit is not drawn.** It is spelled out by *which* of two alphabets
each of the left six digits uses. Every valid spelling begins with `L`, so the
leftmost digit is read against `L` alone — free, and it removes a real ambiguity,
because several width patterns mean one digit in `L` and a different one in `G`.

**Three rows must agree, and hold a majority.** This is the load-bearing rule.
A checksum is one digit, so roughly one run of plausible-looking noise in ten
passes it; before the vote rule existed the decoder produced a confidently wrong
barcode on about 3% of deliberately terrible images. Two neighbouring rows of a
*real but badly blurred* barcode are not independent and can misread it the same
way, which is why three votes and a majority of everything that decoded are both
required. Measured now: **zero wrong answers** across thousands of generated
images, including hostile ones, and zero false reads from pure noise.
Declining costs thirteen keystrokes; a wrong answer costs a wrong record.

**Speed.** A candidate start whose next 59 runs do not total about 95 module
widths cannot be an EAN-13, and saying so is one subtraction against a running
total. Without that test a busy sleeve offered nearly half a million candidate
starts per photograph, each decoded digit by digit before being thrown away, and
a photo took 2.1 seconds; with it the same photo takes 0.6. The test only ever
skips work that was going to fail.

**PNG only, on the server path.** `read_png` handles every non-interlaced PNG —
bit depths 1/2/4/8/16, greyscale, RGB, palette, either with alpha — in about a
hundred lines of `zlib` and `struct`. Interlaced is *refused*, not half-read: a
half-read image decodes to something wrong rather than to nothing. JPEG goes out
to `imgtool.py` in a subprocess, which is the only file allowed to import Pillow,
and which is found by **probing** interpreters rather than assuming
`sys.executable` (on the development machine the system python3 has Pillow and
the virtualenv beside it does not).

`png()` exists mostly for the tests: they draw a barcode and read it back, which
is what turns the decoder from something that worked on the photographs to hand
into something checked against thousands of generated numbers.

---

## §5 Sources and rate limiting (`sources.py`)

Both catalogues work **without an account**. A Discogs token is optional and buys
one thing: 60 requests a minute instead of 25.

`wait_needed()` measures from `source_call` rather than from memory, so stopping
and starting the app cannot be used to burst past a published limit. A wait
longer than `MAX_WAIT` (4s) is declined with a sentence saying when to try again,
rather than blocking a page load. MusicBrainz answered `503` during development
the moment a handful of requests went out back to back — it means what it says.
`poller.py` raises `MAX_WAIT` to `BATCH_MAX_WAIT` (65s) for its own run: nobody is
watching a spinner there, and with the page-load limit Discogs' 25-a-minute ceiling
ended every run after about twenty-two prices.

Adapters normalise into one candidate shape and stop there. Where a source's
field is ambiguous the ambiguity is carried forward rather than resolved by
guessing.

**A search by name** (`lookup_title`) asks MusicBrainz with a Lucene query built
by `mb_title_query` and Discogs with its own parameters, both for vinyl only.
Typed words are lower-cased (so a typed AND is a word, not an operator), Lucene
syntax is escaped, and every word must match rather than the phrase, so a
partial or reordered title still finds the record. 25 editions per source; 100
when a catalogue number is given, because what is left then is every pressing
under one number and the owner chooses by reading them (§10.11).

---

## §6 Prices (`prices.py`)

The six rules are in the module docstring and they are the design. In short: a
series is keyed on all six comparable things; the newest point is compared
against the **median** of the previous window, which it is not itself part of
(including it pulls the median toward it and understates every move); both a
percentage and an absolute floor must be crossed; below `min_obs` observations
nothing is reported; `n` is printed beside every median; up is not good and down
is not bad.

`assess()` always returns the whole argument, not a verdict — the thresholds
applied, the baseline and its `n`, both listing counts, and the reason it did
*not* fire. That is why a record with no move still has something honest to show:
"£31.00 now. 3.3% and £1.00 — under both thresholds (20% and £3.00)" tells the
owner far more than silence.

The strongest word available is `VERDICT = "notable"`. The word "significant" is
banned from the code and `tests.py:TestPromises` greps for it, because in the
only sense in which it has a technical meaning nothing here establishes it.

`confound_words()` is written once and used twice — beside the move on a record's
page, and inside the alert that gets emailed. Those must not drift apart: the
emailed version is the one somebody acts on.

---

## §7 Alerts (`alerts.py`)

`evaluate_prices` reads what is already recorded and asks the internet for
nothing. Polling and judging are separate jobs, so changing a threshold and
re-judging the whole shelf is instant and free.

A cooldown covers the days between a real move happening and the baseline median
catching up with it; without one, a record that has genuinely moved is reported
every time the poller runs, because the move is still true tomorrow.

**A record can have its own rule** (`/record/rule`, on the record's page): its
own percentage, floor and direction, inheriting window, minimum and cooldown from
the default. Switched off, `rule_for` returns None — not the default — so "never
tell me about this one" actually means that. Back to the default deletes the
record's row, so later changes to the default reach it.

For artists, `sweep_artist` follows **works** (MusicBrainz release groups), not
editions: "Miles Davis has a new record out" is a statement about a work, and
there are 799 works against 1,972 editions. A count probe makes the common case
one request; the blind spot — one added and one removed between two looks — is
stated rather than hidden, and `force` does the full sweep.

`classify_new` keeps *released* and *first seen here* apart. A record added to a
catalogue last Tuesday may have come out in 1971, and reporting that as a new
release is a lie the app is structured to be incapable of telling.

The baseline is the first **complete** sweep. A catalogue longer than one sweep
reads (`SWEEP_PAGE_CAP`) is baselined in pieces, each look carrying on from the
offset the last one reached, and nothing is announced until all of it has been
read once. Once baselined, a sweep reads as far as the count says the catalogue
goes, up to `FULL_SWEEP_CEILING`. Linking an artist to a *different*
MusicBrainz identity clears the works noted for the old one and the baseline with
them; keeping the baseline would make the new person's whole catalogue "new".

Delivery records every attempt, successful or not, because a notification feature
that cannot say the mail server refused the password is one that silently stops
working and whose owner finds out months later. `deliver_pending` keys, **per
channel**, on there being no *successful* delivery row, so a failure is retried
and a success is never repeated — and a webhook getting through does not mean the
email that bounced is never tried again.

---

## §8 The pages (`app.py`)

One stdlib server, a hand-rolled router, f-string templates. `page()` forks once,
on `Req.mobile()`, into a phone shell with a tab bar or a wide shell with a
header nav; everything below that is the same markup restyled through `body.m`.
The phone layout matters most here — this app is used standing in front of a
shelf with a record in one hand.

**The palette is fourteen roles defined twice** (`LIGHT_TOKENS` / `DARK_TOKENS`).
Every component rule uses a role, never a literal. The rules those two blocks are
held to, measured rather than asserted:

* every ink-on-surface pair reads at 11:1 or better in both themes;
* `--muted` only ever sits on `paper`, `paper2` or `page`, where it clears 8.6:1;
* `--wax` and `--gold` are surfaces, borders and strokes, never text colours;
* **a price move is never coloured.** Up is not green and down is not red,
  because whether a rise is good news depends on whether you are buying or
  selling. A move is an arrow *plus* the word, which survives being printed in
  black and white and read aloud. `tests.py:TestPromises` greps the `.move` rule.

Two colours are deliberately fixed rather than theme-aware, because they are
scanned rather than read: a barcode is always `#000000` on `#FFFFFF`. An inverted
barcode is one no camera will read.

**No secret is ever written into a page.** Even behind the login, the Discogs
token, mail password and webhook URL —
`db.SECRET_KEYS`, the same three the backup leaves out — are never rendered on
Settings. A blank box keeps what is stored; a checkbox forgets it.

**No remote images, ever.** A record's page links to its cover; it never embeds
one. An embedded image would tell a third party which of your records you were
looking at and when.

**No dead ends.** Every page reached by a stale id is `missing()`: a 404 with a
link onward. Every empty list says how it stops being empty. The layout switch
returns to the same address, query and all (`_here`), minus the flash. A pasted
Discogs address is read for its number (`model.discogs_release_ref`); a box that
does not read as one is refused out loud, because a blank is how a link is
removed. The pages that depend on `poller.py` say whether it has ever run
(`poller_ran_at`, written at the end of every real run), so nobody waits for an
alert that nothing is in a position to raise. `tests.py:TestNoDeadEnds` holds
one test per dead end that was found by walking the running app.

**The network guards.** `_host_allowed` is the DNS-rebinding defence: loopback, a
bare IP, a one-word machine name and `PRIVATE_SUFFIXES` (`.local`, `.ts.net`,
`.lan`, `.home.arpa`, `.internal`) — names nobody can register publicly. Anything
else gets a page saying which addresses work. `_same_origin` is the cross-site
form check, and it wants the page and the server to name the same host. The one
exception is a proxy on this machine that rewrites Host to loopback: then a
`.ts.net` origin is accepted from a loopback peer. A ts.net origin is never
trusted against a real Host, because Tailscale Funnel hosts anybody's public
website on a ts.net name — and in the container *every* request arrives from
loopback, through `tailscale serve`, which preserves Host (measured: a forged
ts.net Origin gets a 403 through serve).

**The door.** `auth.py` holds the parts, `app.py` the routes.

* *Accounts* come from `SN_USERS` (made on start if missing, never removed) or
  `users.py add`. All of them manage the one collection.
* *First entry needs a setup code.* An account starts with no password and a
  one-time code from `users.py` — 12 characters from 32, about 60 bits, with no
  0/O or 1/I. It is typed into the ordinary password box; the next page
  (`/welcome`) asks for a password of the person's own, and the code comes back
  with it and is checked again, so that page keeps no state and cannot be
  reached without the code. An open "choose your password" form was the
  alternative and it is a race: a new Funnel certificate appears in the public
  Certificate Transparency logs, which bots watch. `users.py code NAME` is also
  the reset — the old password and every session end.
* *Passwords and codes are scrypt* (N=2^14, r=8, ~0.1 s on a Pi 4), compared
  in constant time. A name that does not exist still costs one scrypt run
  (`burn_time`), and a wrong name and a wrong password say the same sentence.
* *A session* is `secrets.token_urlsafe(32)` in an HttpOnly, SameSite=Lax cookie
  — `__Host-sn_session` with `Secure` when TLS is on or `SN_BEHIND_HTTPS` says a
  proxy terminates it, `sn_session` otherwise, because a browser drops
  `__Host-` cookies without Secure. It ends after 30 idle days or 180 in all,
  on logout, and on any password change or reset.
* *Guessing is slowed, not locked out.* `auth.Throttle` allows five tries per
  key, then makes each wait double, up to fifteen minutes. The attempt is
  counted *before* the password is checked, under a lock, so a parallel burst
  cannot slip in ahead of its first failure. A browser that has logged in to an
  account carries `sn_device`, an HMAC keyed on that account's password hash,
  and its tries are counted on their own key; everyone else is counted by IP
  and by the name tried. So a guesser hammering "Alice" slows down strangers
  trying "Alice", never Alice on a phone that has logged in before (OWASP's
  device-cookie scheme). A new password retires every device cookie, since the
  key changes. Counts are in memory; a restart forgives them.
* *The caller's address* is `_client_ip`: from a loopback peer, the last entry of
  X-Forwarded-For, which is the one the proxy added; from anywhere else the
  header is ignored. Measured through serve: a forged X-Forwarded-For is
  replaced by the real tailnet address.
* *Every login event is one line on stderr* (`docker logs`): what happened, the
  name tried (cut short and repr'd) and the address — never the password.
* *Every response* carries a CSP that allows exactly the app's own script, its
  inline `<style>` and images from itself; `X-Frame-Options: DENY`; `nosniff`;
  `Referrer-Policy: same-origin`; `Cache-Control: no-store` unless the handler
  set its own (only the static shelf does); and HSTS when secure. An unhandled
  exception is a page that says "something went wrong" and nothing else — the
  traceback goes to the log. Somebody not logged in can POST at most 16 KB,
  and the gate answers before a byte of the body is read.

**HTTPS is per connection.** `Server.finish_request` peeks at the first byte in
the worker thread: `0x16` is a TLS handshake and is wrapped there; anything else
is plain http and gets a redirect to the https address (`ToHTTPS`). Wrapping the
listening socket instead did every handshake inside `accept()`, so one phone
holding a silent spare connection stalled every request behind it.

---

## §9 The tests

```
python3 tests.py        # 190 tests, OK
```

Three kinds. Ordinary unit tests; **invariant** tests (nothing derivable, every
page escapes, every POST redirects and commits); and **grep** tests, for promises
that cannot be checked any other way — that "significant" appears nowhere, that
no JavaScript on the shelf can write anything, that a move is never coloured,
that Pillow never enters the server path, that no module but the two door-keepers
reaches the network.

`fixtures/` holds **real recorded answers** from both catalogues, including the
awkward ones: the barcode that matches two editions, the twenty-entry barcode
field, the empty result. Tests run against those, so the suite needs no network
and the adapters are checked against what the sources actually send rather than
what they are documented to send.

A grep test that examines code must strip docstrings and comments first
(`code_only()`), so a module can explain a ban without tripping it.

---

## §10 Sharp edges, all of them measured

**§10.1 One barcode, two editions.** `0886976389413` is two Discogs releases of
*Kind of Blue* — one "Limited Edition, Numbered, Blue, 180 gram", one "Stereo,
180 Gram, Gatefold". This is why `release.barcode` is not unique and why the scan
flow asks instead of guessing. Both are in `fixtures/`.

**§10.2 Discogs' `barcode` field is a bag of everything on the sleeve.** For one
of those two editions it holds twenty entries: two spellings of the barcode
(`'0 886976 389413'` and `'886976389413'`), two rights societies (`BIEM/GEMA`,
`BIEM/STEMRA`) and sixteen matrix numbers. Taking `barcode[0]` is wrong on that
release and stores a rights-society name as a barcode on plenty of others.
`_pick_barcode` normalises each entry and **matches it against the code that was
scanned**. Note that the twelve-digit spelling normalises to the same thirteen
digits, which is the other half of why this is a comparison and not a lookup.

**§10.3 Discogs reports two different lowest prices.** For release 2317524, with
`curr_abbr=GBP`, seconds apart: `marketplace/stats` said **£34.40** and the
release endpoint said **51.43**. Both fixtures are in the repo. Whatever the
explanation, they measure different things, and a series mixing them would show a
49% crash and a 49% recovery depending on which endpoint the poller happened to
use. Hence `price_obs.feed`, which is part of the series key. Only
`marketplace/stats` is used, because it states the currency of its own number
instead of leaving you to assume the one you asked for.

**§10.4 MusicBrainz's `format:` matches a whole format name, not a word in one.**
`format:Vinyl` finds 18 Miles Davis releases. `format:"12\" Vinyl"` finds 337. A
query built the obvious way under-counts vinyl twentyfold. So this app does not
filter by format in a query at all: watches follow works and ask about formats
per new work, which also answers the better question — not only "is there a new
record" but "is there a vinyl pressing of it yet".

**§10.5 Discogs pads unknown date parts with zeroes.** `2010-05-00` means May
2010. Handing that to a date parser gets you an exception or, worse, the last day
of April. `_dg_date` trims it back to the precision the source actually claimed.

**§10.6 SQLite counts NULLs as distinct in a UNIQUE constraint.** `cond_band` is
null whenever a source declines to state a condition, which is most of the time,
so a table-level `UNIQUE(..., cond_band, ...)` would never fire for the common
case and a poller run twice would double-count. The uniqueness rule is an
expression index over `COALESCE(cond_band,-1)` instead. The copy is in it too, as
`COALESCE(copy_id,-1)` (v2): market observations have no copy, so nothing changes
for them, but without it two copies of one edition bought on the same day were
one receipt — the second `INSERT OR IGNORE` was silently dropped.

**§10.7 A GS1 prefix is not where a record was pressed.** `0886976389413` starts
088 — the United States — and it is a Dutch pressing of an American album sold
across Europe, because Sony registered the number. Every page that shows the
registry says so.

**§10.8 `again` contains `gain`.** A grep test for banned words must use word
boundaries. It cost one confusing failure.

**§10.9 An author `display` beats the `hidden` attribute.** The browser hides
`[hidden]` with a user-agent rule, and any author rule that sets `display` wins
over it — `.btn{display:inline-block}` did. So "Start the camera", hidden in the
HTML until `scan.js` decides the browser can scan, showed on every browser, and on
the ones that cannot scan (any phone on plain http among them) it did nothing at
all. The stylesheet carries `[hidden]{display:none!important}`, and `scan.js`
checks for a secure context *before* the camera, because over http a browser
removes `navigator.mediaDevices` and the old order blamed the browser.

**§10.10 Discogs' search-result barcode on an old record is not a barcode.**
For the 1959 *Kind of Blue* pressings it holds `'BMI'`, `'2'`, `'x"LP" 47324'`
and run-out stamps — the same untyped bag as §10.2, with nothing scanned to match
it against. A name search therefore takes no barcode from a Discogs search
result at all, and keeps a MusicBrainz barcode (a field of its own) only if its
check digit holds; `07464405791`, eleven digits, is in the recorded fixture and
is dropped. When a Discogs edition is chosen, `fuller` reads the full entry,
whose identifiers are typed, and a barcode there that passes its check is kept —
so a record found by name is found again by scanning a later copy.

**§10.11 MusicBrainz finds a catalogue number only unquoted.** `catno:CL1355`
finds CL 1355 and C.L.1355 alike; `catno:"CL1355"` finds nothing, and
`catno:(cl AND 1355)` finds nothing either. So `_catno_key` strips everything
but letters and digits and sends the term bare. Discogs finds both spellings
from the number as typed (44 for `CL1355`, 50 for `cl 1355`). And one
catalogue number can be many pressings: `CL 1355` is 44 vinyl editions on
Discogs, told apart only by year, country and plant — hence the higher cap.

**§10.12 The vinyl filter in a name search is exact, measured.** *Kind of
Blue* by Miles Davis is 137 MusicBrainz releases unfiltered, 62 of them on
vinyl; `format:(Vinyl OR "12\" Vinyl" OR "7\" Vinyl" OR "10\" Vinyl")`
returns exactly those 62, missing none and adding none. §10.4 still holds for
watches, which do not filter at all; a search can, because it spells out every
vinyl format name. Unfiltered, the first page is mostly CDs.

**§10.13 Two dollars.** With CAD and USD offered side by side, a bare `$` is
ambiguous, and to somebody in Canada it means theirs. `model.CURRENCIES` prints
`US$` and `C$`, and `moneyn` reads either back.

---

## §11 Recipes

**Add a column.** Put it in `SCHEMA` *and* add a guarded `ALTER TABLE` step at
the tail of `_migrate`, then bump `SCHEMA_VERSION`. `tests.py:TestSchema`
compares a fresh database against a migrated one; a column in one and not the
other is the defect that shape exists to prevent. If the column is a function of
other rows, stop — see §0.1.

**Add a page.** A handler `(req, c, m) -> (status, headers, body)`, an entry in
`ROUTES`, and an entry in one of `NAV_SHELF` / `NAV_PRICES` / `NAV_MORE` so it is
reachable. Return through `shell()` so the alert badge is filled in. Escape
everything with `e()`. If it writes, commit and redirect.

**Make a route public.** Think twice: it answers the whole internet. Add it to
`PUBLIC` in `app.py` *and* to the list spelled out in
`tests.py:TestTheDoor.test_every_private_route_sends_a_stranger_to_the_login`,
and make sure it renders nothing from the collection.

**Add a kind of price.** `model.PRICE_KINDS` (with the sentence saying what it is
*not* — that sentence is printed wherever the number is), the `CHECK` on
`price_obs.kind`, and `model.MARKET_KINDS` only if it is an observation of the
market rather than of your own wallet.

**Add a source.** An adapter returning the candidate shape, a `LIMITS` entry, and
a value in the three `CHECK(source IN ...)` constraints. Route every request
through `_call` — never call `_http_get` directly, and never call urllib.
Record a real response in `fixtures/` and test the adapter against it.

**Offer another currency.** Add it to `model.CURRENCY_CHOICES` (it must
already be in `model.CURRENCIES`, with a symbol nobody can mistake for another),
and check Discogs accepts it as `curr_abbr`. Nothing else: every form, the
switch and `db.set_currency` read the tuple.

**Change the move rule.** `prices.assess` and nowhere else. Every page and the
alerts render what it returns, so a new reason not to fire needs only a new
`why_not` sentence. Add a case to `tests.py:TestPrices`.

**Change a delivery channel.** `alerts.channels_ready`, a `_deliver_*` function,
and the `CHECK` on `delivery.channel`. Never let a failure escape `deliver()` —
a bad address must not stop a sweep.
