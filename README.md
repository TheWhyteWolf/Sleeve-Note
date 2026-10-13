# Sleevenote

A vinyl collection: what you own, what condition it is in, and what people are
asking for it.

Scan the barcode on the back of a sleeve — or, for a record old enough not to
have one, look it up by name — say which edition you are holding, grade the record
and the packaging out of five, and let it follow the price. It
will tell you when a price moves in a way worth knowing about, and when an artist
on your shelf puts something new out.

It runs on your own machine, or on a small server of your own. There is no
cloud and no telemetry. The whole collection is one file, `app.db`. Every page is
behind a password, and the people who can log in all manage the same collection.

## Run it

```bash
cd sleeve-note
python3 app.py
```

Then open <http://127.0.0.1:1200>. It asks you to log in — make yourself an
account first:

```bash
python3 users.py add alice       # prints alice's one-time setup code
```

Python 3.9 or newer. **Nothing to install.** The server uses only the standard
library, and neither of the two catalogues it reads needs an account or a key.

Seven environment variables are the whole configuration:

```bash
SN_PORT=8080 python3 app.py     # serve on a different port
SN_BIND=local python3 app.py    # loopback only (the default binds all interfaces,
                                #   so your phone on the same wifi can reach it)
SN_DATA=/mnt/disk python3 app.py            # keep app.db somewhere else
SN_TLS_CERT=cert.pem SN_TLS_KEY=key.pem python3 app.py    # HTTPS, in process;
                                #   plain http:// to the same port is sent on to https://
SN_USERS=alice,bob python3 app.py   # make sure these accounts exist
SN_BEHIND_HTTPS=1 python3 app.py      # a proxy in front serves HTTPS; mark cookies Secure
```

## Logging in

Each person has an account, and every account manages the same collection.

An account starts with **no password and a one-time setup code**, printed on
the server and nowhere else:

```bash
python3 users.py                 # who can log in, and who has not yet
python3 users.py add NAME        # a new account, and its setup code
python3 users.py code NAME       # a new setup code — also the reset for a
                                 #   forgotten password; logs NAME out everywhere
python3 users.py logout NAME     # log NAME out everywhere, password kept
```

On the login page, type the code as the password. The next page asks for a
password of your own (ten characters or more — a few unrelated words is
easiest), and the code stops working. A code is needed rather than an open
"choose your password" page because whoever reached that page first would own
the account — and on a public address, that is a race with bots.

Changing your password (*More → Your password*) logs you out everywhere else.
Wrong guesses are slowed down: after five, each try waits longer, up to fifteen
minutes — except on a browser that has logged in to that account before, so
somebody guessing at your name slows themselves down, not you.

## Running it on a server

The `Dockerfile` builds an image that runs as an unprivileged user, keeps the
collection in a `/data` volume, and expects a proxy in front to serve HTTPS.
For a deployment with a Tailscale sidecar and `tailscale serve` with Funnel,
the container can be administered with:

```bash
docker exec sleevenote python3 users.py code alice    # a setup code
docker exec sleevenote python3 poller.py              # one round of checks
docker logs sleevenote                                # every login, and from where
```

### Have a look before you start

```bash
mkdir -p /tmp/sleevenote-demo
python3 demo.py --out /tmp/sleevenote-demo/app.db
SN_DATA=/tmp/sleevenote-demo python3 app.py
```

Seven records, graded, with six weeks of price history and a couple of alerts
already raised. Everything in it is invented and every row says so; the barcodes
are real, so you can scan those sleeves and get the same answer. It cannot touch
your own collection — it refuses to write to `app.db`.

## The idea, in one paragraph

**A record is an edition, and a copy is the thing on your shelf.** "Kind of Blue"
is not something you can own; a 2010 Music On Vinyl pressing, catalogue number
MOVLP 019, is — and so is the 1959 Columbia original, and they are worth wildly
different money. Two copies of one edition in different condition are two rows
here, because they are worth different money too. Keeping those three things
apart is most of what this app is.

## Scanning

Three ways in, and all three end at the same place:

1. **The phone's camera.** Tap *Start the camera* on the Scan page and point it
   at the barcode. It fills the box in and stops — **you** press the button, so
   you always see the digits before anything is looked up. Needs HTTPS (below)
   and a browser with a barcode reader of its own: Chrome on Android has one,
   Safari on the iPhone does not. Where it cannot work the button does not
   appear, and the page says why.
2. **Type the thirteen digits.** Always works. Spaces are fine, and so is
   dropping the leading zero off an American twelve-digit code.
3. **Photograph it.** Take a picture and upload it; the barcode is decoded
   *here*, by the server, in Python. This works with JavaScript switched off and
   on any browser at all. A JPEG needs Pillow on some Python on the machine — if
   there is none, the page says so and points you at the other two routes.

Then it asks the interesting question: **which edition is this?** Both
catalogues are searched, and if more than one edition carries the barcode you are
shown all of them side by side with a line saying *what actually differs* —
usually something like "They differ in: pressing". That is not hypothetical. The
barcode `0886976389413` belongs to two Discogs releases of *Kind of Blue*, one a
numbered limited edition on blue vinyl and one a gatefold, and no amount of
cleverness can tell which is in your hand. So it asks.

Once you pick one, it asks that catalogue one more question. Pick the
MusicBrainz entry and it asks MusicBrainz which Discogs release it links to — that
link is what lets the price be followed. Pick a Discogs entry and it reads the
full entry: the month it came out, Discogs' notes about the pressing, and the
stamps in the run-out groove, which are the surest way to tell two near-identical
pressings apart. If a catalogue does not answer, the record is added anyway and
its page offers to ask again.

If nothing matches, you add it by hand and it keeps the barcode. Small pressings
often are not in either catalogue. A record added by hand can be corrected later;
one that came from a catalogue keeps what the catalogue said, and your own words
go in its notes.

### No barcode

Most records pressed before about 1980 have none. For those, the Scan page has a
fourth box: **look it up by name**. Type the artist and title, or the catalogue
number printed on the label and the spine, or all three. Both catalogues are asked
for **vinyl editions only**, and you pick the pressing from the list, as with a
barcode. It is filed exactly the same way, with the catalogue's details and run-out
stamps, and its price is followed.

The catalogue number is what makes this work. *Kind of Blue* by title alone is 359
vinyl editions on Discogs, and the page shows the first 25 and says so. `CL 1355`,
the original mono catalogue number, is 44, and a search with a catalogue number
shows every one of them, each headed with its year, country and label so you can
match it to the one in your hand. Spaces and dots in the number do not matter.
An edition already on your shelf is marked, and picking it adds another copy.

## Condition

Out of five, the record and the packaging graded separately, because they wear
independently — a mint record in a split sleeve is an ordinary thing to own.

| | Record | Sleeve | Trade |
|---|---|---|---|
| **5** | Looks unplayed; nothing catches the light | Square corners, no ring wear, no splits | NM |
| **4** | A few light marks; plays cleanly under the music | Slight shelf wear or a faint ring | VG+ |
| **3** | Clear marks, some groove wear, audible in quiet passages | Obvious ring wear, soft corners | VG |
| **2** | Plays through, but noise throughout | Split seam, creases, tape or writing | G |
| **1** | Skips, sticks or will not play | Coming apart, or a piece missing | P |

The sentences are shown beside the numbers every time you grade, and that is the
point: a bare "3" is not comparable between two people, or between you now and
you in two years. The words are what make it comparable.

There is no 6 for Mint, because Mint means sealed and unplayed. A sealed record
is handled the other way round — you decline to grade the vinyl, and it records
**why** there is no grade rather than treating the blank as a 5 or a 0.

Grading again adds a row. Nothing is overwritten, so a record you played hard for
a year can show that it was.

## What a price here actually is

Almost every price this app collects is the **lowest asking price** on Discogs:
what the cheapest seller is hoping to get today. It is worth being blunt about
what that is not.

* It is **not a sale.** Nobody has paid it.
* It is **not an appraisal** of your copy.
* It is **not for any particular condition.** Discogs' public figures do not say
  what condition the cheapest copy is in, so this number might be for a battered
  one.

Every page that shows the number says so. You can also record prices by hand —
a sale you saw or an asking price you saw, on the record's page; what you paid and
what you sold a copy for, on that copy's page — and those are kept separate,
because they are different kinds of fact.

**No currency is ever converted.** Two currencies are two lists, never one total.

**Prices are in CAD, GBP or USD — one choice for the whole collection**, switched
with three buttons at the top of the Prices page (or in Settings). It is
what Discogs is *asked* for, and Discogs answers in it. Switching converts nothing
already recorded: those prices stay in the currency they were quoted in, and every
followed record becomes due for a price in the new one, so the next check
brings the whole shelf across. Until then the Prices page says how many records still show
the old currency. US dollars print as `US$` and Canadian as `C$`, so neither is
ever a bare `$`.

## Being told when something moves

A move is reported only when it clears **both** thresholds — 20% **and** £3.00 by
default — measured against the **median** of the previous 30 days, and only once
there are at least three observations to compare against. Then it goes quiet for
a fortnight so the same move is not reported daily. All of that is adjustable
under *When to tell me*, and any one record can have thresholds of its own, or be
left out of alerts altogether, from its own page.

Why it is built that way:

* **The median, not yesterday's price.** A lowest asking price is one seller's
  number. It jumps when they sell and jumps back when someone relists. The median
  moves when the market moves and ignores one seller coming and going.
* **Both thresholds.** A percentage alone shouts about a £4 single going to
  £5.20. An amount alone ignores a box set moving £60.
* **A minimum number of observations.** A series with two points in it has no
  idea what normal looks like, so it does not get an opinion.

Every alert is printed with its arithmetic — the baseline, how many observations
it came from, the thresholds in force — and with **how many copies were for sale
at each end**. A price that halved while the listings went from one to twelve is
a story about supply, not about what the record is worth, and you get to see that
before you draw a conclusion.

**Up is not good and down is not bad.** Whether a rise is welcome depends on
whether you are buying or selling, which this app does not know. So a move is an
arrow and a word — never a colour, never "gain" or "loss".

## Watching an artist

Watch an artist and you will be told when something of theirs appears that was
not there before, along with whether a vinyl pressing is listed yet.

Two things worth knowing:

* **The first look announces nothing.** It records what they already have out and
  says so. A catalogue too long for one look is read over several, and nothing is
  announced until all of it has been read once. Without that, watching Miles Davis would announce 799 new albums in one
  afternoon and you would turn notifications off that evening.
* **New to a catalogue is not the same as new.** A record added to MusicBrainz
  last Tuesday might have come out in 1971. Those are reported as two different
  things — *has a new record out* and *has a record newly listed* — and never
  confused.

An artist needs linking to MusicBrainz before they can be watched. If a record
came in from Discogs its artist arrives unlinked; the record's page says so and
puts the search one tap away, and you pick the right person. It will not guess
which of the four people with that name you meant. If you picked the wrong one,
link the right one: it forgets what it noted about the first and starts a fresh,
silent baseline.

## Letting it run on its own

The app does everything from buttons, but `poller.py` does it without you
remembering:

```bash
python3 poller.py                # prices, artists, judging, sending
python3 poller.py --dry-run      # say what it would do, change nothing
python3 poller.py --only prices --limit 20
```

Once a day is plenty:

```cron
0 7 * * *  cd /path/to/sleeve-note && /usr/bin/python3 poller.py --quiet
```

It asks for as little as it can: a price looked at in the last twenty hours is
not asked about again, and an artist whose catalogue has not changed size costs
exactly one request. When it reaches a source's per-minute limit it waits its turn
rather than skipping records; if a source asks it to slow down, it stops for that
run. With `--quiet` it prints nothing unless something failed. The Prices, Alerts
and Artists pages say when it last ran — or that it never has, in which case
nothing is checked unless you press a button.

## Where alerts go

Out of the box, to a page in this app and nowhere else. In Settings you can add
either or both:

* **Email** — an ordinary SMTP server, with STARTTLS.
* **A webhook** — a JSON POST. ntfy, Discord and Slack all take one.

Every attempt is recorded, successful or not, so you can see that the mail server
refused the password rather than finding out months later. A failure is tried
again on the channel that failed, and never repeated on one that worked. There is
a *Send a test notification* button.

## Scanning with a phone

On the server deployment this is already true — Tailscale serves it over https
with a real certificate — and this section is for running it on a laptop.

The phone's own camera scanner needs a secure context — an `https://` address, or
`localhost`. Over plain http the browser will not hand the camera to the page at
all. Typing digits and photographing the barcode both work over plain http, and on
a phone the photo box opens the camera too.

The easy way, if you use Tailscale:

```bash
tailscale serve --bg 1200
```

That gives you an https URL with a real certificate, renewed for you. Otherwise
make a self-signed certificate, naming the addresses you will use:

```bash
mkdir -p tls && openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
  -keyout tls/key.pem -out tls/cert.pem -subj "/CN=Sleevenote" \
  -addext "subjectAltName=IP:192.168.1.20,IP:127.0.0.1,DNS:localhost"
SN_TLS_CERT=tls/cert.pem SN_TLS_KEY=tls/key.pem python3 app.py
```

The phone will warn about the certificate once; accept it and the camera works.
Plain `http://` to the same port is redirected to `https://`, so an old bookmark
still lands. Keep `tls/` out of anything you share — the key is in it.

## Privacy

What leaves the machine is a barcode, a release number, an artist number, a name,
title or catalogue number you typed, and a User-Agent. That is the list, and **What it asked the internet**
shows you every request it has ever made.

Your collection is never sent anywhere. **No cover image is ever loaded from a
third party** — a record's page links to the artwork rather than showing it,
because an embedded image would tell Discogs which of your records you were
looking at and when.

A Discogs token is optional and only raises the rate limit from 25 requests a
minute to 60. It, your mail password and your webhook URL are never shown on the
Settings page, even to someone logged in, and are left out of the backup
download, because a backup is a file people email to themselves. So are the
accounts: password hashes and sessions never leave the server.

## Backing up

The whole collection is `app.db`. Copy it. That is the backup.

The *Backup* page also downloads a zip of every table as CSV, readable by
anything and likely to outlive this app.

## The files

| | |
|---|---|
| `app.py` | the server, the routing, the login gate and every page |
| `auth.py` | accounts, passwords, sessions, setup codes, and the guessing throttle |
| `users.py` | handing out setup codes and resetting passwords, from the server |
| `db.py` | the schema, and the only place it is described |
| `model.py` | the vocabulary: grades, formats, kinds of price |
| `barcode.py` | EAN-13 and UPC-A: checking, drawing, and reading one out of a photo |
| `sources.py` | MusicBrainz and Discogs — the only place the app reads from the internet |
| `prices.py` | price series, and what counts as a move worth reporting |
| `alerts.py` | raising alerts and sending them — the only place it writes to the internet |
| `library.py` | assembling the collection for the pages |
| `poller.py` | the scheduled half, for cron |
| `imgtool.py` | JPEG to PNG, in a subprocess; the only file allowed to use Pillow |
| `demo.py` | builds a demonstration collection in its own file |
| `tests.py` | the gate — `python3 tests.py` must say OK |
| `fixtures/` | real recorded API answers, so the tests never need the network |
| `Dockerfile` | the image: stdlib server, Pillow for `imgtool.py`, never root |

Before changing anything, read `ARCHITECTURE.md`.

```
python3 tests.py        # 190 tests, OK
```
