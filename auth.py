"""Who is at the door: accounts, passwords, sessions, and the throttle.

Stdlib only, like everything else on the server path.

The shape, and why:

  * **A short list of named accounts, all with the same rights.** Everyone who
    can log in manages the same collection. An account exists so that a password
    belongs to one person and can be changed or revoked without telling anyone
    else a new one.

  * **First entry uses a setup code, not an open form.** An account starts with
    no password and a one-time code, printed by `users.py` for whoever runs the
    server to hand over. The code is typed into the ordinary password box, and
    the next page asks for a password of the person's own. An open "choose your
    password" form would hand the account to whoever found the page first, and on
    a public address that is a race against bots: a new certificate is published
    in the Certificate Transparency logs, and those are watched.

  * **Passwords are scrypt, and so are setup codes.** A stolen database gives up
    neither. The comparison is constant-time, and a name that does not exist
    still costs one scrypt run, so timing does not say which names are real.

  * **A session is a random token in a cookie; the database keeps its SHA-256.**
    So a copy of app.db (a backup, a stray scp) cannot be replayed as a login.
    Changing a password ends every session for that account, and so does a reset.

  * **Guessing is slowed, and the owner is not locked out by it.** After a few
    failures each further try has to wait, doubling up to fifteen minutes. A
    browser that has logged in to an account before carries a device cookie for
    it, and its tries are counted on their own — so somebody hammering at
    "Alice" from elsewhere slows themselves down, not Alice. That is OWASP's
    device-cookie scheme. The counts live in memory: a restart forgives them,
    which only someone with a shell on the server can cause.
"""
from __future__ import annotations
import base64
import datetime
import hashlib
import hmac
import re
import secrets
import threading
import time

import model

# scrypt at 16 MiB and N=2^14: about a tenth of a second on a Raspberry Pi 4,
# which is nothing to a person logging in and a lot to a guesser.
SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_LEN = 2 ** 14, 8, 1, 32
SCRYPT_MAXMEM = 64 * 1024 * 1024

MIN_PASSWORD = 10
MAX_PASSWORD = 1024        # scrypt will take anything; a request body should not

SESSION_IDLE_DAYS = 30     # unused for this long, a session is over
SESSION_MAX_DAYS = 180     # and none outlives this, used or not
SESSION_TOUCH_SECONDS = 3600   # seen_at is refreshed at most this often

# Setup codes: twelve characters from 32, so 60 bits. No 0/O or 1/I to misread
# when it is read out over the phone.
CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
CODE_LEN = 12

COOKIE_DAYS = SESSION_MAX_DAYS
DEVICE_DAYS = 400          # the longest a browser will keep a cookie


# ---------------------------------------------------------------- hashing

def _b64(b):
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def hash_secret(secret):
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(secret.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R,
                        p=SCRYPT_P, dklen=SCRYPT_LEN, maxmem=SCRYPT_MAXMEM)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(dk)}"


def check_secret(secret, stored):
    """True only for a well-formed hash that matches. Anything malformed is a
    no, never an exception: a damaged row must lock its account, not open it."""
    try:
        kind, n, r, p, salt, want = (stored or "").split("$")
        if kind != "scrypt":
            return False
        got = hashlib.scrypt(secret.encode("utf-8"), salt=_unb64(salt), n=int(n),
                             r=int(r), p=int(p), dklen=len(_unb64(want)),
                             maxmem=SCRYPT_MAXMEM)
        return hmac.compare_digest(got, _unb64(want))
    except (ValueError, TypeError, MemoryError):
        return False


_DUMMY = None


def burn_time(secret):
    """Spend what a real check costs, for a name that has nothing to check
    against — so a wrong name and a wrong password take the same time."""
    global _DUMMY
    if _DUMMY is None:
        _DUMMY = hash_secret("not a password anyone has")
    check_secret(secret, _DUMMY)


def new_code():
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
    return "-".join(raw[i:i + 4] for i in range(0, CODE_LEN, 4))


def norm_code(s):
    """What was typed, as a code: case, spaces and dashes do not matter."""
    return re.sub(r"[^0-9A-Za-z]", "", s or "").upper()


def password_problem(pw, again, name="", code=""):
    """A sentence saying what is wrong with a new password, or ""."""
    pw = pw or ""
    if code and norm_code(pw) == norm_code(code):
        return "Use a password of your own, not the setup code."
    if len(pw) < MIN_PASSWORD:
        return (f"A password needs at least {MIN_PASSWORD} characters. A few unrelated "
                "words in a row is the easiest kind to remember.")
    if len(pw) > MAX_PASSWORD:
        return f"That is longer than {MAX_PASSWORD} characters."
    if pw != (again or ""):
        return "The two passwords were not the same."
    low = pw.lower()
    if name and name.lower() in low and len(low) < len(name) + MIN_PASSWORD:
        return "That is mostly your name. Pick something nobody could guess from it."
    if len(set(low)) <= 2:
        return "That is the same character over and over."
    for run in ("0123456789", "1234567890", "qwertyuiop", "asdfghjkl", "abcdefghij",
                "password"):
        if run in low and len(low) - len(run) < 6:
            return "That is mostly a run off the keyboard. Pick something less common."
    return ""


# ---------------------------------------------------------------- accounts

def ensure_accounts(c, names):
    """Make sure each named account exists. Never renames or removes one — an
    account dropped from the list keeps its row and its password until someone
    deletes it on purpose. Returns the names that were created."""
    made = []
    for name in names:
        name = (name or "").strip()
        if not name:
            continue
        if not c.execute("SELECT 1 FROM account WHERE name=?", (name,)).fetchone():
            c.execute("INSERT INTO account(name,added_at) VALUES(?,?)",
                      (name, model.now_iso()))
            made.append(name)
    return made


def parse_names(raw):
    return [n.strip() for n in (raw or "").split(",") if n.strip()]


def account_by_name(c, name):
    name = (name or "").strip()
    if not name or len(name) > 100:
        return None
    return c.execute("SELECT * FROM account WHERE name=?", (name,)).fetchone()


def account_state(acct):
    """'ready', 'waiting' for a first entry, or 'closed' (no way in at all)."""
    if acct["pw_hash"]:
        return "ready"
    return "waiting" if acct["setup_hash"] else "closed"


def issue_code(c, acct_id):
    """A fresh setup code for an account. Its password, its old code and every
    session are gone — this is the reset as well as the first issue."""
    code = new_code()
    c.execute("UPDATE account SET pw_hash=NULL, pw_set_at=NULL, setup_hash=? WHERE id=?",
              (hash_secret(norm_code(code)), acct_id))
    c.execute("DELETE FROM session WHERE account_id=?", (acct_id,))
    return code


def set_password(c, acct_id, pw):
    """Set a password, spend the setup code, end every session."""
    c.execute("""UPDATE account SET pw_hash=?, setup_hash=NULL, pw_set_at=? WHERE id=?""",
              (hash_secret(pw), model.now_iso(), acct_id))
    c.execute("DELETE FROM session WHERE account_id=?", (acct_id,))


# ---------------------------------------------------------------- sessions

def _token_id(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_session(c, acct_id):
    token = secrets.token_urlsafe(32)
    now = model.now_iso()
    c.execute("INSERT INTO session(id,account_id,created_at,seen_at) VALUES(?,?,?,?)",
              (_token_id(token), acct_id, now, now))
    return token


def _ago(days=0, seconds=0):
    t = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        days=days, seconds=seconds)
    return t.replace(microsecond=0).isoformat()


def session_account(c, token):
    """The account behind a session token, or None. A session idle too long, or
    older than SESSION_MAX_DAYS, is deleted on sight."""
    if not token or len(token) > 200:
        return None
    sid = _token_id(token)
    row = c.execute("""SELECT s.id sid, s.created_at, s.seen_at, a.*
                       FROM session s JOIN account a ON a.id = s.account_id
                       WHERE s.id=?""", (sid,)).fetchone()
    if not row:
        return None
    if row["seen_at"] < _ago(days=SESSION_IDLE_DAYS) or \
            row["created_at"] < _ago(days=SESSION_MAX_DAYS) or not row["pw_hash"]:
        c.execute("DELETE FROM session WHERE id=?", (sid,))
        c.commit()
        return None
    if row["seen_at"] < _ago(seconds=SESSION_TOUCH_SECONDS):
        c.execute("UPDATE session SET seen_at=? WHERE id=?", (model.now_iso(), sid))
        c.commit()
    return row


def end_session(c, token):
    if token:
        c.execute("DELETE FROM session WHERE id=?", (_token_id(token),))


def prune_sessions(c):
    c.execute("DELETE FROM session WHERE seen_at < ? OR created_at < ?",
              (_ago(days=SESSION_IDLE_DAYS), _ago(days=SESSION_MAX_DAYS)))


# ---------------------------------------------------------------- devices

def device_cookie(acct):
    """'<account id>.<nonce>.<mac>', keyed on the account's password hash, so
    changing the password retires every device cookie it ever issued."""
    nonce = secrets.token_urlsafe(12)
    return f"{acct['id']}.{nonce}.{_device_mac(acct, nonce)}"


def _device_mac(acct, nonce):
    key = (acct["pw_hash"] or "").encode("utf-8")
    return hmac.new(key, f"{acct['id']}.{nonce}".encode(), hashlib.sha256).hexdigest()[:32]


def device_nonce(raw, acct):
    """The nonce of a device cookie that is genuinely for this account, or ""."""
    if not raw or not acct or not acct["pw_hash"]:
        return ""
    parts = raw.split(".")
    if len(parts) != 3 or parts[0] != str(acct["id"]):
        return ""
    if not hmac.compare_digest(parts[2], _device_mac(acct, parts[1])):
        return ""
    return parts[1]


# ---------------------------------------------------------------- throttle

class Throttle:
    """Failures per key, and how long the next try has to wait.

    `begin` counts the attempt *before* the password is checked, under the lock,
    so a burst of parallel requests cannot all slip in ahead of the first
    failure being recorded. `succeeded` clears the keys it is given."""

    FREE = 5            # tries before any waiting
    CAP = 15 * 60       # the longest wait between tries
    FORGET = 24 * 3600  # a key with no failure for this long is dropped
    MAX_KEYS = 20000

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.fails = {}          # key -> (count, last)

    def _wait(self, key, now):
        count, last = self.fails.get(key, (0, 0.0))
        if count < self.FREE:
            return 0
        delay = min(2 ** (count - self.FREE), self.CAP)
        return max(0, int(last + delay - now + 0.999))

    def begin(self, keys):
        """Seconds to wait, or 0 — in which case the attempt is now counted."""
        now = self.clock()
        with self.lock:
            wait = max((self._wait(k, now) for k in keys), default=0)
            if wait:
                return wait
            if len(self.fails) > self.MAX_KEYS:
                self._prune(now)
            for k in keys:
                count, _ = self.fails.get(k, (0, 0.0))
                self.fails[k] = (count + 1, now)
            return 0

    def succeeded(self, keys):
        with self.lock:
            for k in keys:
                self.fails.pop(k, None)

    def _prune(self, now):
        for k in [k for k, (_, last) in self.fails.items() if now - last > self.FORGET]:
            del self.fails[k]
        if len(self.fails) > self.MAX_KEYS:            # still full: keep the newest half
            keep = sorted(self.fails.items(), key=lambda kv: kv[1][1])[len(self.fails) // 2:]
            self.fails = dict(keep)


THROTTLE = Throttle()


def throttle_keys(name, ip, nonce):
    """A known device is counted on its own; anyone else is counted by where
    they are and by which name they are trying."""
    if nonce:
        return [("device", nonce)]
    return [("ip", ip or "?"), ("name", (name or "").strip().casefold()[:100])]


def wait_words(seconds):
    if seconds < 90:
        return f"{seconds} second{'' if seconds == 1 else 's'}"
    m = (seconds + 59) // 60
    return f"{m} minute{'' if m == 1 else 's'}"
