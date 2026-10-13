"""Sleevenote — a vinyl collection, its condition, and what it is being asked for.

One stdlib HTTP server, a hand-rolled router, f-string templates, one SQLite
file. No framework and no dependencies on the server path. The scaffolding
includes the request object, the multipart parser, the network guards, and the
two-theme palette.

What it does, in the order you would do it:

    scan a barcode  ->  which edition is this?  ->  grade it 1-5, record and
    sleeve separately  ->  follow what people are asking for it  ->  be told
    when that moves, or when an artist on the shelf puts something new out.

Three things to know before editing:

  * There is no auto-escaping. Every database or user string interpolated into
    HTML must go through e(). Every SQL value must be a ? placeholder.
  * The dispatcher never commits. A handler that writes must call c.commit()
    itself, and every POST ends in a redirect.
  * JavaScript may improve a page; it may never carry a fact. The camera scanner
    fills in a text box and nothing else — every barcode that reaches the
    database is checksummed by the server when the POST lands, and every page
    works with scripting off. Delete the static shelf and the only thing lost is
    the convenience of not typing thirteen digits.

Run it:  python3 app.py        ->  http://127.0.0.1:1200
         SN_PORT=8080 python3 app.py
         SN_BIND=local python3 app.py     (loopback only; the default binds all
                                           interfaces so a phone on the same
                                           network can reach it)
         SN_TLS_CERT=... SN_TLS_KEY=... python3 app.py    (HTTPS in process —
                                           the camera needs it on a phone)
         SN_USERS=Alice,Bob python3 app.py   (makes sure those accounts exist;
                                           `python3 users.py` hands out the
                                           setup code each one logs in with)
         SN_BEHIND_HTTPS=1                (a proxy in front terminates TLS — the
                                           cookies are then marked Secure)

Every page but the login is behind a password. See auth.py for how.
"""
from __future__ import annotations
import csv
import datetime
import html
import io
import ipaddress
import json
import os
import re
import sqlite3
import subprocess
import sys
import traceback
import urllib.parse
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import alerts as alerts_mod
import auth
import barcode as bc
import db
import library
import model
import prices
import sources

APP_NAME = model.APP_NAME
BASE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("SN_PORT") or 1200)
LOCAL_ONLY = (os.environ.get("SN_BIND") or "").lower() in ("local", "loopback", "127.0.0.1")
HOST = "127.0.0.1" if LOCAL_ONLY else "0.0.0.0"
TLS_CERT = os.environ.get("SN_TLS_CERT") or ""
TLS_KEY = os.environ.get("SN_TLS_KEY") or ""
# A proxy in front of this process terminates TLS (tailscale serve, in the
# container). Nothing here can see that from the request alone, so it is said.
BEHIND_HTTPS = (os.environ.get("SN_BEHIND_HTTPS") or "").lower() in ("1", "yes", "true")

# The whole static shelf, in one place. Both the cache-busting stamp and the
# route's whitelist read this dict, so a file added to one is added to both.
STATIC = {
    "scan.js": "application/javascript; charset=utf-8",
    "icon.svg": "image/svg+xml",
}
STATIC_DIR = os.path.join(BASE, "static")
MANIFEST_NAME = "app.webmanifest"
MAX_PHOTO = 24 * 1024 * 1024


def _static_version():
    """A stamp over the CONTENT of every static file, so ?v= changes exactly when
    a file does. A missing file hashes its own name rather than raising: the app
    must still start if somebody has deleted the shelf."""
    import hashlib
    h = hashlib.sha256()
    for name in sorted(STATIC):
        try:
            with open(os.path.join(STATIC_DIR, name), "rb") as fh:
                h.update(fh.read())
        except OSError:
            h.update(name.encode())
    return h.hexdigest()[:10]


STATIC_V = _static_version()


# ------------------------------------------------------------------ helpers

def e(x):
    """Escape for HTML. There is no auto-escaping — this is the only defence."""
    return "" if x is None else html.escape(str(x), quote=True)


def money(a, cur):
    return model.money(a, cur)


def opts(pairs, selected, blank=""):
    out = [f'<option value="">{e(blank)}</option>'] if blank else []
    for val, label in pairs:
        sel = " selected" if str(val) == str(selected or "") else ""
        out.append(f'<option value="{e(val)}"{sel}>{e(label)}</option>')
    return "".join(out)


class Req:
    """The request, as a bag of parsed values. query/form/files are all
    {name: [values]} so a repeated field name accumulates rather than overwrites."""

    def __init__(self, method, path, query, form, headers, files=None, filenames=None,
                 ip=""):
        self.method, self.path = method, path
        self.query, self.form, self.headers = query, form, headers
        self.files = files or {}
        self.filenames = filenames or {}
        self.ip = ip            # the caller, as best this process can tell
        self.user = None        # the account row, once the gate has let it in
        self.token = ""         # the session token it came in with

    def g(self, k, d=None):
        v = self.query.get(k)
        return v[0] if v else d

    def f(self, k, d=None):
        v = self.form.get(k)
        return v[0] if v else d

    def gi(self, k):
        return model.intn(self.g(k))

    def fi(self, k):
        return model.intn(self.f(k))

    def has(self, k):
        return k in self.form

    def file(self, k):
        v = self.files.get(k)
        return v[0] if v else None

    def filename(self, k):
        v = self.filenames.get(k)
        return v[0] if v else ""

    def cookie(self, k, d=None):
        raw = self.headers.get("Cookie", "") or ""
        for part in raw.split(";"):
            ck, sep, cv = part.strip().partition("=")
            if sep and ck == k:
                return cv
        return d

    def mobile(self):
        """Phone layout? The user's choice wins; the client decides otherwise.

        This app is used standing in front of a shelf with a phone in one hand
        and a record in the other, so the phone layout is the one that matters
        most. A request built with no headers is a desktop, which keeps every
        test written against the wide layout exercising the path it always did."""
        v = self.cookie("sn_view")
        if v in ("m", "d"):
            return v == "m"
        if self.headers.get("Sec-CH-UA-Mobile") == "?1":
            return True
        return "Mobi" in (self.headers.get("User-Agent", "") or "")

    def theme(self):
        """"light", "dark", or "" to follow the device. A cookie, not a setting
        row: it is a fact about this browser, not about the collection."""
        v = self.cookie("sn_theme")
        return v if v in ("light", "dark") else ""


def html_ok(body, status=200):
    return (status, [("Content-Type", "text/html; charset=utf-8")], body)


def redirect(loc):
    return (303, [("Location", loc)], b"")


def _safe_next(raw, default="/"):
    """A redirect target from a form. Must be a path on this app: anything that
    could leave the origin becomes the default. A backslash counts as a slash to
    a browser, so "/\\evil.example" is as foreign as "//evil.example". Anything
    outside printable ASCII is refused too: it cannot go in a header as it is."""
    n = raw or default
    if (not n.startswith("/") or n.startswith("//") or "\\" in n
            or any(ord(ch) < 32 or ord(ch) > 126 for ch in n)):
        return default
    return n


def flash_to(path, msg):
    """Redirect carrying one short message. The message rides in the query
    string rather than in a cookie or a table, because it is true for exactly one
    page view and storing it would outlive its truth."""
    sep = "&" if "?" in path else "?"
    return redirect(f"{path}{sep}msg={urllib.parse.quote(msg[:300])}")


# ---------------------------------------------------------------- the palette
#
# Fourteen colour roles, defined twice. Every component rule below uses the role
# and never a literal, so the dark theme is a token swap rather than a second
# stylesheet.
#
# The rules the two blocks are held to, measured rather than asserted:
#
#   * every ink-on-surface pair reads at 11:1 or better in both themes;
#   * --muted is only ever placed on paper, paper2 or page, where it reads at
#     8.6:1 or better. It is never put on a tinted surface;
#   * --wax and --gold are surfaces, borders and strokes, never text colours;
#   * a price move is NEVER coloured. Up is not green and down is not red,
#     because whether a rise is good news depends on whether you are buying or
#     selling and this app does not know which. A move is an arrow plus the word
#     "up" or "down", which survives being printed in black and white and read
#     aloud. tests.py greps the stylesheet to keep it that way.
LIGHT_TOKENS = """
  --ink:#14131A; --paper:#FFFFFF; --paper2:#FAF8F4; --page:#F4F1EA;
  --wax:#2B2733; --wax-dk:#16141C; --wax-lt:#E8E3DA;
  --gold:#E0A12E; --gold-lt:#FBF0D8;
  --ok-lt:#E4F2E0; --amber-lt:#FDF0D5; --red-lt:#FBE4E1;
  --muted:#474250; --line:#D7D0C4; --btn-ink:#14131A;
"""
# Measured against --ink #EDE9E1: page 15.7:1, paper 14.6:1, wax-lt 12.2:1, the
# three status tints 11.7-12.8:1, and --muted on paper 7.9:1.
DARK_TOKENS = """
  --ink:#EDE9E1; --paper:#191720; --paper2:#1F1C26; --page:#110F16;
  --wax:#BCB1A2; --wax-dk:#D9D0C2; --wax-lt:#2A2632;
  --gold:#E8B455; --gold-lt:#33280F;
  --ok-lt:#17301A; --amber-lt:#342A10; --red-lt:#3A1B18;
  --muted:#B3ACA0; --line:#383341; --btn-ink:#14131A;
"""

CSS = """
:root{""" + LIGHT_TOKENS + """}
@media (prefers-color-scheme:dark){html:not(.theme-light){""" + DARK_TOKENS + """}}
html.theme-dark{""" + DARK_TOKENS + """}
*{box-sizing:border-box}
[hidden]{display:none!important}
body{margin:0;font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
     color:var(--ink);background:var(--page)}
a{color:var(--ink);text-decoration:underline;text-decoration-color:var(--gold);
  text-underline-offset:2px}
a:hover{text-decoration-thickness:2px}
header{background:var(--wax-lt);border-bottom:4px solid var(--wax);padding:14px 20px}
header .row{display:flex;align-items:baseline;gap:16px;flex-wrap:wrap;max-width:1180px;margin:0 auto}
header h1{margin:0;font-size:19px;letter-spacing:.3px}
header h1 a{text-decoration:none}
header .tag{color:var(--muted);font-size:13px}
.switch{margin-left:auto;display:flex;gap:8px}
.switch a{padding:5px 12px;border:2px solid var(--wax);border-radius:999px;
          background:var(--paper);text-decoration:none;font-size:13px}
.switch a.on{background:var(--gold);border-color:var(--wax-dk)}
.subnav{background:var(--paper);border-bottom:1px solid var(--line);padding:0 20px}
.subnav div{max-width:1180px;margin:0 auto;display:flex;gap:4px;flex-wrap:wrap}
.subnav a{padding:9px 12px;text-decoration:none;font-size:14px;border-bottom:3px solid transparent}
.subnav a.on{border-bottom-color:var(--gold);font-weight:600}
main{max-width:1180px;margin:0 auto;padding:20px}
h1,h2,h3{line-height:1.25}
h1{font-size:23px;margin:0 0 6px}
h2{font-size:17px;margin:26px 0 10px;padding-bottom:6px;border-bottom:2px solid var(--wax-lt)}
h3{font-size:15px;margin:18px 0 8px}
h4{font-size:13px;margin:12px 0 2px;text-transform:uppercase;letter-spacing:.4px;color:var(--muted)}
.card{background:var(--paper);border:1px solid var(--line);border-left:4px solid var(--wax);
      border-radius:6px;padding:14px 16px;margin:12px 0}
.card h3:first-child{margin-top:0}
.card.pick{border-left-color:var(--gold)}
.tiles{display:flex;flex-wrap:wrap;gap:12px;margin:12px 0}
.tile{background:var(--paper);border:1px solid var(--line);border-top:4px solid var(--gold);
      border-radius:6px;padding:12px 16px;min-width:140px;flex:1}
.tile .lab{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.4px}
.tile .val{font-size:23px;font-weight:700;margin-top:2px}
.tile .sub{font-size:12px;color:var(--muted);margin-top:2px}
table{border-collapse:collapse;width:100%;background:var(--paper);font-size:14px}
th,td{border:1px solid var(--line);padding:7px 9px;text-align:left;vertical-align:top}
th{background:var(--wax-lt);font-weight:600}
tbody tr:nth-child(even) td{background:var(--paper2)}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.scroll{overflow-x:auto}
.pill{display:inline-block;padding:1px 8px;border:1px solid var(--wax);border-radius:999px;
      font-size:12px;background:var(--paper);white-space:nowrap}
.pill.ok{background:var(--ok-lt)} .pill.warn{background:var(--amber-lt)}
.pill.bad{background:var(--red-lt)} .pill.q{background:var(--gold-lt)}
.muted{color:var(--muted)}
small,.small{font-size:12.5px}
.flash{background:var(--ok-lt);border:1px solid var(--wax);border-radius:6px;padding:9px 13px;margin:12px 0}
.note{background:var(--amber-lt);border:1px solid var(--wax);border-radius:6px;padding:11px 14px;margin:12px 0}
.err{background:var(--red-lt);border:1px solid var(--wax);border-radius:6px;padding:9px 13px;margin:12px 0}
.quote{background:var(--paper2);border-left:3px solid var(--line);padding:8px 12px;margin:8px 0;
       white-space:pre-wrap;font-size:13.5px}
label{display:block;font-size:13px;margin:9px 0 3px;font-weight:600}
input,select,textarea{font:inherit;color:var(--ink);background:var(--paper);
  border:1px solid var(--wax);border-radius:4px;padding:6px 8px;max-width:100%}
input:focus,select:focus,textarea:focus{outline:3px solid var(--gold);outline-offset:1px}
input[type=checkbox],input[type=radio]{width:auto}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:0 16px}
.btn{display:inline-block;background:var(--gold);color:var(--btn-ink);border:2px solid var(--wax);
     border-radius:5px;padding:7px 15px;font-size:14px;font-weight:600;cursor:pointer;text-decoration:none}
.btn:hover{border-color:var(--wax-dk)}
.btn.sec{background:var(--paper);color:var(--ink);font-weight:500}
.btn.sm{padding:3px 9px;font-size:12.5px;font-weight:500}
.btn.big{padding:14px 22px;font-size:17px}
.bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:10px 0}
.right{margin-left:auto}
.legend{font-size:12.5px;color:var(--muted);margin-top:6px}
footer.count{margin:26px 0 6px;padding-top:10px;border-top:2px solid var(--wax-lt);
             font-size:13px;color:var(--muted)}
.gone td{text-decoration:line-through;color:var(--muted)}
/* ---- the grade picker: five radios per scale, each with the words that
   define it, because a bare number on a five-point scale is not comparable
   between two people or between one person and themselves next year. ---- */
.grades{display:flex;flex-direction:column;gap:6px;margin:6px 0 2px}
.grades label{display:flex;gap:10px;align-items:flex-start;font-weight:400;margin:0;
              border:1px solid var(--line);border-radius:6px;padding:9px 11px;background:var(--paper)}
.grades label:hover{border-color:var(--wax)}
.grades input{margin-top:3px}
.grades .n{font-weight:700;min-width:1.2em}
.grades .nm{font-weight:600}
.grades .w{color:var(--muted);font-size:13px;display:block}
.gold{font-variant-numeric:tabular-nums}
/* ---- a price move is never coloured. Arrow plus word, both. ---- */
.move{font-variant-numeric:tabular-nums;white-space:nowrap;font-weight:600}
.spark{vertical-align:middle;color:var(--wax)}
.bcode{max-width:100%;height:auto;background:#FFFFFF;padding:4px;border-radius:4px}
.candidates{display:flex;flex-direction:column;gap:10px}
.kv{display:grid;grid-template-columns:auto 1fr;gap:2px 12px;font-size:13.5px;margin:6px 0 0}
.kv dt{color:var(--muted)} .kv dd{margin:0}
video{width:100%;max-width:420px;border:2px solid var(--wax);border-radius:8px;background:#000}
.tabs{display:flex;gap:0;border-bottom:2px solid var(--wax-lt);margin:14px 0 0;flex-wrap:wrap}
.tabs a{padding:8px 14px;text-decoration:none;border:1px solid transparent;border-bottom:none;
        font-size:14px;border-radius:6px 6px 0 0}
.tabs a.on{background:var(--paper);border-color:var(--line);font-weight:600}
@media print{.noprint{display:none!important}}
/* ---- the phone layout ---- */
body.m header{padding:9px 14px;border-bottom-width:3px}
body.m header .row{gap:10px;max-width:520px}
body.m header h1{font-size:17px}
body.m header .row .mmore{margin-left:auto;text-decoration:none;font-size:22px;line-height:1;
  padding:2px 8px;letter-spacing:1px}
body.m main{max-width:520px;padding:12px 12px calc(84px + env(safe-area-inset-bottom,0px))}
body.m h1{font-size:20px} body.m h2{font-size:16px;margin:20px 0 8px}
body.m .card{padding:13px 14px;border-radius:10px;margin:10px 0}
body.m .grid{grid-template-columns:1fr}
body.m label{font-size:14px;margin:12px 0 4px}
/* 16px is the size below which Android offers to zoom the field. */
body.m input,body.m select,body.m textarea{width:100%;min-height:46px;font-size:16px;
  padding:9px 11px;border-radius:6px}
body.m input[type=checkbox],body.m input[type=radio]{width:auto;min-height:0}
body.m .btn{min-height:48px;padding:12px 18px;font-size:16px;border-radius:7px}
body.m .tiles{gap:8px} body.m .tile{min-width:calc(50% - 4px);padding:10px 12px}
body.m table{font-size:13px} body.m th,body.m td{padding:6px 7px}
body.m .grades label{padding:11px}
.tabbar{position:fixed;left:0;right:0;bottom:0;display:none;background:var(--wax-lt);
        border-top:2px solid var(--wax);padding:4px 0 env(safe-area-inset-bottom,0px);z-index:9}
body.m .tabbar{display:flex}
.tabbar a{flex:1;text-align:center;text-decoration:none;font-size:11px;padding:6px 2px 7px;
          color:var(--ink);line-height:1.25}
.tabbar a .ic{display:block;font-size:17px;line-height:1.1}
.tabbar a.on{font-weight:700;box-shadow:inset 0 3px 0 var(--gold)}
"""


# ------------------------------------------------------------------- chrome

NAV_SHELF = [("/shelf", "The shelf"), ("/scan", "Scan"), ("/artists", "Artists"),
             ("/labels", "Shelf labels")]
NAV_PRICES = [("/prices", "Prices"), ("/alerts", "Alerts"), ("/rules", "When to tell me")]
NAV_MORE = [("/settings", "Settings"), ("/help", "How the numbers work"),
            ("/calls", "What it asked the internet"), ("/backup", "Backup"),
            ("/account", "Your password")]
SECTIONS = {"shelf": NAV_SHELF, "prices": NAV_PRICES, "more": NAV_MORE}

TABS = [("/shelf", "Shelf", "▤"), ("/scan", "Scan", "▦"), ("/prices", "Prices", "£"),
        ("/alerts", "Alerts", "!"), ("/more", "More", "⋮")]


def _tabs(cur, unseen=0, money_icon=""):
    out = []
    for path, label, icon in TABS:
        if path == "/prices" and money_icon:
            icon = money_icon
        on = " class=\"on\"" if cur == path else ""
        badge = f" ({unseen})" if path == "/alerts" and unseen else ""
        out.append(f'<a href="{path}"{on}><span class="ic" aria-hidden="true">{icon}</span>'
                   f'{e(label)}{e(badge)}</a>')
    return f'<nav class="tabbar noprint">{"".join(out)}</nav>'


def _here(req):
    """This page's address, query and all, minus the one-view flash message — so
    switching layout on /record?id=3 comes back to record 3, not to an id-less
    "No such record"."""
    if not req:
        return "/"
    q = urllib.parse.urlencode([(k, v) for k, vs in req.query.items() if k != "msg"
                                for v in vs])
    return req.path + ("?" + q if q else "")


def page(title, body, section="", active="", flash=None, req=None, unseen=0, bare=False,
         money_icon=""):
    """The one place the phone layout and the desktop layout differ in markup.

    Everything below the shell is the same HTML for both; the phone restyles it
    through body.m and gets a tab bar instead of a header nav. `bare` is for
    somebody who has not logged in: the name of the app and nothing to click,
    because every link would only lead back to the login."""
    sub = SECTIONS.get(section, [])
    mob = bool(req and req.mobile())
    theme = req.theme() if req else ""
    cur_path = req.path if req else "/"
    fl = f'<div class="flash">{e(flash)}</div>' if flash else ""
    user = getattr(req, "user", None) if req else None

    if bare:
        chrome = f"""<header><div class="row">
  <h1><a href="/">{APP_NAME}</a></h1>
  <span class="tag">records, condition and what they are being asked for</span>
</div></header>"""
        tail = ""
    elif mob:
        chips = ""
        if sub:
            links = "".join(f'<a href="{h}" class="{"on" if h == active else ""}">{e(l)}</a>'
                            for h, l in sub)
            chips = f'<nav class="subnav noprint"><div>{links}</div></nav>'
        chrome = f"""<header><div class="row">
  <h1><a href="/">{APP_NAME}</a></h1>
  <a class="mmore noprint" href="/more" aria-label="Settings and everything else"
     title="Settings and everything else">&#8942;</a>
</div></header>{chips}"""
        tail = _tabs(cur_path, unseen, money_icon)
    else:
        subnav = ""
        if sub:
            links = "".join(f'<a href="{h}" class="{"on" if h == active else ""}">{e(l)}</a>'
                            for h, l in sub)
            subnav = f'<nav class="subnav"><div>{links}</div></nav>'
        switch = "".join(
            f'<a href="{h}" class="{"on" if section == s else ""}">{e(l)}</a>'
            for s, h, l in (("shelf", "/shelf", "Shelf"), ("prices", "/prices", "Prices"),
                            ("more", "/settings", "More")))
        vt = f"""<form method="post" action="/view" class="noprint" style="margin:0">
<input type="hidden" name="want" value="m">
<input type="hidden" name="next" value="{e(_here(req))}">
<button class="btn sec sm" title="Preview the phone layout">Phone view</button></form>"""
        who = ""
        if user:
            who = f"""<form method="post" action="/logout" class="noprint"
style="margin:0;display:flex;gap:8px;align-items:baseline">
<a href="/account" class="small" title="Your password">{e(user["name"])}</a>
<button class="btn sec sm">Log out</button></form>"""
        chrome = f"""<header><div class="row">
  <h1><a href="/">{APP_NAME}</a></h1>
  <span class="tag">records, condition and what they are being asked for</span>
  <span class="switch noprint">{switch}</span>
  {vt}
  {who}
</div></header>{subnav}"""
        tail = ""

    hcls = f' class="theme-{theme}"' if theme else ""
    return f"""<!doctype html>
<html lang="en"{hcls}><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<meta name="theme-color" content="#E8E3DA" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#2A2632" media="(prefers-color-scheme: dark)">
<link rel="manifest" href="/{MANIFEST_NAME}">
<link rel="icon" href="/static/icon.svg?v={STATIC_V}" type="image/svg+xml">
<link rel="apple-touch-icon" href="/static/icon.svg?v={STATIC_V}">
<title>{e(title)} — {APP_NAME}</title><style>{CSS}</style></head>
<body{' class="m"' if mob else ''}>
{chrome}
<main>{fl}{body}</main>
{tail}
</body></html>"""


def shell(req, c, title, body, section="", active=""):
    """page() with the alert badge filled in. Every handler uses this."""
    sym = model.CURRENCIES.get(db.get(c, "currency", "GBP"), "£").strip()
    return html_ok(page(title, body, section, active, flash=req.g("msg"), req=req,
                        unseen=alerts_mod.unseen_count(c),
                        money_icon="$" if sym.endswith("$") else sym[:1]))


def _currency_switch(c, nxt):
    """The collection's currency as three buttons, the current one filled in.

    A form and not a link, because it changes a setting; three buttons and not a
    select, because one press should be the whole job on a phone."""
    cur = db.get(c, "currency", "GBP")
    btns = []
    for k in model.CURRENCY_CHOICES:
        on = k == cur
        cls = "btn sm" if on else "btn sec sm"
        pressed = ' aria-pressed="true"' if on else ""
        btns.append(f'<button class="{cls}" name="currency" value="{e(k)}"{pressed}>'
                    f'{e(k)} {e(model.CURRENCIES[k].strip())}</button>')
    return f"""<form method="post" action="/currency" class="bar noprint" style="margin:0">
<input type="hidden" name="next" value="{e(nxt)}">
<span class="small">Prices in</span> {"".join(btns)}</form>"""


def currency_set(req, c, m):
    """Switch the collection's currency. Converts nothing — see db.set_currency."""
    want = (req.f("currency") or "").strip().upper()
    nxt = _safe_next(req.f("next"), "/prices")
    if want not in model.CURRENCY_CHOICES:
        return flash_to(nxt, "That is not one of the currencies on offer.")
    if not db.set_currency(c, want):
        return flash_to(nxt, f"Prices are already asked for in {want}.")
    c.commit()
    due = len(library.due_for_price(c, db.get_int(c, "price_max_age_hours", 20), limit=100000))
    return flash_to(nxt, f"Prices are now asked for in {want}, for everyone using this "
                         f"collection. Nothing already recorded was converted. {due} followed "
                         f"record{'' if due == 1 else 's'} will get a {want} price the next "
                         f"time prices are checked.")


def _schedule_note(c):
    """Whether anything checks on its own. Following a price or watching an
    artist does nothing until something asks — a button, or poller.py on a
    schedule — so the pages that depend on it say which, rather than letting the
    owner wait for an alert that nothing is in a position to raise."""
    ran = db.get(c, "poller_ran_at", "")
    if ran:
        return (f'<p class="muted small">poller.py last ran {e(model.ago(ran))}. It is what '
                f'checks without you pressing anything.</p>')
    return ('<p class="note small">Nothing is checked on its own yet. Prices, watched artists '
            'and sending alerts happen when you press a button here, or when '
            '<code>poller.py</code> runs — once a day from cron is plenty, and the README has '
            'the line under “Letting it run on its own”.</p>')


def missing(req, c, what, href="/shelf", label="Back to the shelf"):
    """The page for an id that is not there — deleted, mistyped, or an old link.
    Always with a way on, and a 404 so nothing treats it as a page that exists."""
    _, hd, body = shell(req, c, "Not found",
                        f'<h1>No such {e(what)}</h1><p class="muted">It may have been '
                        f'deleted, or the link is out of date.</p>'
                        f'<p><a href="{href}">{e(label)}</a></p>', "shelf")
    return 404, hd, body


def tile(lab, val, sub=""):
    s = f'<div class="sub">{e(sub)}</div>' if sub else ""
    return (f'<div class="tile"><div class="lab">{e(lab)}</div>'
            f'<div class="val">{e(val)}</div>{s}</div>')


def pill(text, kind=""):
    return f'<span class="pill {kind}">{e(text)}</span>'


def _pref_set(name, value, allowed, nxt):
    if value not in allowed:
        value = ""
    ck = (f"{name}=; Path=/; Max-Age=0; SameSite=Lax" if not value
          else f"{name}={value}; Path=/; Max-Age=31536000; SameSite=Lax")
    return (303, [("Location", _safe_next(nxt)), ("Set-Cookie", ck)], b"")


def view_set(req, c, m):
    return _pref_set("sn_view", (req.f("want") or "").strip(), ("m", "d"), req.f("next"))


def theme_set(req, c, m):
    return _pref_set("sn_theme", (req.f("want") or "").strip(), ("light", "dark"),
                     req.f("next"))


def more_page(req, c, m):
    links = "".join(f'<li><a href="{h}">{e(l)}</a></li>'
                    for h, l in NAV_SHELF + NAV_PRICES + NAV_MORE)
    cur = req.path
    th = req.theme()
    def tbtn(val, label):
        on = " sec" if th != val else ""
        return (f'<form method="post" action="/theme" style="display:inline;margin:0">'
                f'<input type="hidden" name="want" value="{val}">'
                f'<input type="hidden" name="next" value="/more">'
                f'<button class="btn{on} sm">{e(label)}</button></form> ')
    body = f"""<h1>More</h1>
<div class="card"><h3>Everywhere else</h3><ul>{links}</ul></div>
<div class="card"><h3>Display</h3>
<p class="muted small">Theme: {tbtn('light','Light')}{tbtn('dark','Dark')}{tbtn('','Follow the device')}</p>
<form method="post" action="/view" style="margin-top:8px">
  <input type="hidden" name="want" value="d"><input type="hidden" name="next" value="/more">
  <button class="btn sec sm">Use the wide layout</button></form>
</div>
<div class="card"><h3>You</h3>
<p class="muted small">Logged in as {e(req.user["name"]) if req.user else "nobody"}.
<a href="/account">Change your password</a>.</p>
<form method="post" action="/logout"><button class="btn sec sm">Log out</button></form>
</div>"""
    return shell(req, c, "More", body, "more")


# -------------------------------------------------------------------- landing

def landing(req, c, m):
    n = library.counts(c)
    unseen = alerts_mod.unseen_count(c)
    ungraded = library.needs_grading(c, 6)
    unlinked = library.unlinked_tracked(c, 6)
    tot = prices.collection_totals(c)

    tiles = [tile("Records", n["releases"], f"{n['copies']} copies on the shelf"),
             tile("Artists", n["artists"], f"{n['watched']} watched"),
             tile("Followed", n["tracked"], f"{n['linked']} linked to a price"),
             tile("Alerts", unseen, "unread" if unseen else "nothing new")]
    money_bits = ""
    if tot["groups"]:
        rows = "".join(
            f'<li>{e(money(g["total"], g["currency"]))} — the lowest asking prices of '
            f'{g["n"]} record{"" if g["n"] == 1 else "s"}, added up</li>'
            for g in tot["groups"])
        money_bits = f"""<div class="card"><h3>What the shelf is being asked for</h3>
<ul>{rows}</ul>
<p class="muted small">This is the sum of the cheapest copy anybody currently has for sale,
for each record, in whatever condition those copies happen to be in. It is not what your
copies are worth and it is not what they would sell for.
{e(f"{tot['unpriced']} of {tot['records']} records have no price recorded.")}
Currencies are listed separately and never added together.</p></div>"""

    paid = prices.paid_totals(c)
    if paid:
        rows = "".join(
            f'<li>{e(money(p["total"], p["currency"]))} on {p["n"]} '
            f'cop{"y" if p["n"] == 1 else "ies"}</li>' for p in paid)
        money_bits += f"""<div class="card"><h3>What you paid</h3><ul>{rows}</ul>
<p class="muted small">Added up from the receipts you recorded, including for copies you no
longer have. That is money actually spent — and it is not to be set against the asking prices
above, which are what strangers hope to get for copies in unknown condition.</p></div>"""

    todo = []
    if ungraded:
        items = "".join(f'<li><a href="/grade?copy={r["id"]}">{e(r["artist_credit"])} — '
                        f'{e(r["title"])}</a></li>' for r in ungraded)
        todo.append(f"<h4>Not graded yet</h4><ul>{items}</ul>")
    if unlinked:
        items = "".join(f'<li><a href="/record?id={r["id"]}">{e(r["artist_credit"])} — '
                        f'{e(r["title"])}</a></li>' for r in unlinked)
        todo.append("<h4>Followed, but with nothing to follow</h4>"
                    f"<ul>{items}</ul><p class=\"muted small\">These have no Discogs release "
                    "linked, so no price can be looked up. Open one and link it, or record "
                    "prices by hand.</p>")
    todo_html = f'<div class="card">{"".join(todo)}</div>' if todo else ""

    recent = alerts_mod.open_alerts(c, 5)
    recent_html = ""
    if recent:
        rows = "".join(f'<li>{e(alerts_mod.headline(c, a))} '
                       f'<span class="muted small">{e(model.ago(a["raised_at"]))}</span></li>'
                       for a in recent)
        recent_html = (f'<div class="card"><h3>Lately</h3><ul>{rows}</ul>'
                       f'<p><a href="/alerts">All alerts</a></p></div>')

    empty = ""
    if not n["releases"]:
        empty = """<div class="note"><strong>Nothing on the shelf yet.</strong>
Scan a barcode to start — point a phone at the back of a sleeve, or type the thirteen
digits in. Neither needs an account or a key: MusicBrainz and Discogs both answer
without one.</div>"""

    body = f"""<h1>{APP_NAME}</h1>
<p class="muted">A record is an edition, a copy is the object on your shelf, and a price is
somebody's asking price. This app keeps those three apart.</p>
{empty}
<p class="bar"><a class="btn big" href="/scan">Scan a record</a>
<a class="btn sec" href="/shelf">The shelf</a>
<a class="btn sec" href="/record/new">Add one by hand</a></p>
<div class="tiles">{"".join(tiles)}</div>
{recent_html}{money_bits}{todo_html}"""
    return shell(req, c, APP_NAME, body, "")


# ------------------------------------------------------------ reading photos

_PY_CACHE = None


def pillow_python():
    """An interpreter that can import Pillow, or "". Probed once, then cached.

    Probed rather than assumed. On the machine this was written on the system
    python3 has Pillow and the virtualenv beside it does not, so `sys.executable`
    is a coin flip. Each candidate is asked, in its own process, and the first
    one that answers is used."""
    global _PY_CACHE
    if _PY_CACHE is not None:
        return _PY_CACHE
    import shutil
    seen, cands = set(), []
    for p in (sys.executable, shutil.which("python3"), "/usr/bin/python3",
              "/usr/local/bin/python3"):
        if p and p not in seen:
            seen.add(p)
            cands.append(p)
    _PY_CACHE = ""
    for p in cands:
        try:
            r = subprocess.run([p, os.path.join(BASE, "imgtool.py"), "--mode", "probe"],
                               capture_output=True, text=True, timeout=20)
            if r.returncode == 0 and "ok" in r.stdout:
                _PY_CACHE = p
                break
        except Exception:
            continue
    return _PY_CACHE


def photo_to_png(blob, name=""):
    """Any uploaded image to PNG bytes. (png, problem).

    A PNG is handed straight to the decoder. Anything else goes out to
    `imgtool.py` in a subprocess, because Pillow is not allowed in this process.
    When no interpreter has Pillow the message says so and names the two routes
    that still work, rather than failing in a way that looks like a broken
    camera."""
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        return blob, ""
    py = pillow_python()
    if not py:
        return b"", ("This photo is not a PNG, and no Python on this machine has Pillow "
                     "installed to convert it. Either use the camera button above, which "
                     "needs nothing installed, or type the thirteen digits in. "
                     "(`pip install Pillow` would also fix it.)")
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "in" + (os.path.splitext(name)[1][:8] or ".jpg"))
        dst = os.path.join(tmp, "out.png")
        with open(src, "wb") as fh:
            fh.write(blob)
        try:
            r = subprocess.run([py, os.path.join(BASE, "imgtool.py"), "--mode", "grey",
                                "--in", src, "--out", dst, "--max", "1600"],
                               capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            return b"", "That image took too long to convert."
        if r.returncode != 0 or not os.path.exists(dst):
            return b"", f"That image could not be read ({(r.stderr or '').strip()[:120]})."
        with open(dst, "rb") as fh:
            return fh.read(), ""


# --------------------------------------------------------------- the scan flow

def scan_page(req, c, m, err="", raw=""):
    raw = raw or req.g("raw") or ""
    err_html = f'<div class="err">{e(err)}</div>' if err else ""
    recent = library.recent_scans(c, 8)
    rows = ""
    for s in recent:
        # Every scan that could still become a record links back to where it
        # left off: choosing an edition, asking again, or adding it by hand.
        offered = (s["candidates_json"] or "[]") != "[]"
        if s["release_id"]:
            where = (f'<a href="/record?id={s["release_id"]}">{e(s["artist_credit"] or "")} — '
                     f'{e(s["title"] or "")}</a>')
        elif s["code"] and offered:
            where = f'<a href="/scan/choose?scan={s["id"]}">not added yet — choose the edition</a>'
        elif s["code"]:
            said = ("nothing matched" if s["candidates"] is not None
                    else "no answer — ask again")
            where = f'<a href="/scan/choose?scan={s["id"]}">{e(said)}</a>'
        else:
            where = f'<span class="muted">{e(s["note"] or "not a barcode")}</span>'
        rows += (f'<tr><td class="small">{e(model.ago(s["scanned_at"]))}</td>'
                 f'<td class="small"><code>{e(s["raw"])}</code></td>'
                 f'<td class="small">{e(s["how"])}</td><td>{where}</td></tr>')
    recent_html = (f'<h2>Lately</h2><div class="scroll"><table><thead><tr><th>When</th>'
                   f'<th>Scanned</th><th>How</th><th>Became</th></tr></thead>'
                   f'<tbody>{rows}</tbody></table></div>') if rows else ""

    body = f"""<h1>Scan a record</h1>
<p class="muted">No barcode on it? <a href="#byname">Look it up by name</a> instead.</p>
{err_html}
<div class="card pick">
  <h3>With the camera</h3>
  <p id="scanmsg" class="muted small">Reading a barcode with the camera needs JavaScript, an
  https address and a browser that can read barcodes, such as Chrome on Android. The two ways
  below work everywhere.</p>
  <p><button id="scanbtn" class="btn big" hidden>Start the camera</button></p>
  <video id="scanvid" hidden playsinline muted></video>
  <p class="muted small">The camera only fills in the box below. Nothing is sent anywhere
  until you press the button, and the digits are checked again by the server when you do.</p>
</div>

<form method="post" action="/scan" class="card">
  <h3>Or type the number under the barcode</h3>
  <input type="hidden" id="how" name="how" value="typed">
  <label for="code">Barcode — 13 digits, or 12 on an older American pressing</label>
  <input id="code" name="code" inputmode="numeric" autocomplete="off" spellcheck="false"
         value="{e(raw)}" placeholder="0886976389413" style="font-size:19px;letter-spacing:1px">
  <p class="bar"><button class="btn big">Look it up</button></p>
  <p class="muted small">Spaces are fine. So is the leading zero, or leaving it off.</p>
</form>

<form method="post" action="/scan" enctype="multipart/form-data" class="card">
  <h3>Or photograph it</h3>
  <input type="hidden" name="how" value="photo">
  <label for="photo">A photo of the barcode</label>
  <input id="photo" type="file" name="photo" accept="image/*" capture="environment">
  <p class="bar"><button class="btn sec">Read the photo</button></p>
  <p class="muted small">Read here, on this machine, by the server — so this works with
  JavaScript switched off and on a browser with no scanner of its own. Hold the sleeve
  flat and fill the frame with the barcode.</p>
</form>
{_search_form({}, "Look it up by name", "No barcode? Look it up by name")}
{recent_html}
<script src="/static/scan.js?v={STATIC_V}" defer></script>"""
    return shell(req, c, "Scan", body, "shelf", "/scan")


def scan_post(req, c, m):
    how = (req.f("how") or "typed").strip()
    if how not in ("typed", "camera", "photo"):
        how = "typed"
    raw, votes = (req.f("code") or "").strip(), None

    if how == "photo":
        blob = req.file("photo")
        if not blob:
            return scan_page(req, c, m, "No photo arrived. Pick a file and try again.")
        if len(blob) > MAX_PHOTO:
            return scan_page(req, c, m, "That image is very large. Try a smaller one.")
        png, problem = photo_to_png(blob, req.filename("photo"))
        if problem:
            return scan_page(req, c, m, problem)
        try:
            code, votes, tried = bc.decode_png(png)
        except bc.ImageError as ex:
            return scan_page(req, c, m, str(ex))
        if not code:
            return scan_page(req, c, m,
                             f"No barcode could be read from that photo — {tried} lines across "
                             f"it were tried. Filling the frame with the barcode and holding "
                             f"the sleeve flat helps most. You can also type the digits in.")
        raw = code
    if not raw:
        return scan_page(req, c, m, "Nothing to look up — type a barcode or choose a photo.")

    code = bc.normalise(raw)
    if not code or not bc.check_ok(code):
        # The raw string is kept exactly as it arrived, because a barcode that
        # failed its own checksum is the evidence about a misread.
        c.execute("""INSERT INTO barcode_scan(raw,code,how,read_votes,note,scanned_at)
                     VALUES(?,?,?,?,?,?)""",
                  (raw, "", how, votes, "failed its check digit", model.now_iso()))
        c.commit()
        return scan_page(req, c, m, bc.fix_hint(raw), raw)

    return redirect(f"/scan/choose?scan={_look_up(c, raw, code, how, votes)}")


def _look_up(c, raw, code, how, votes, said=""):
    """Ask both sources about a valid barcode and log the scan. Returns its id.

    `candidates` is left NULL when a source did not answer, which is how the
    choosing page tells "nothing matched" from "not everyone was asked"."""
    unanswered = []
    cands, notes = sources.lookup_barcode(c, code, unanswered)
    note = " ".join(([said] if said else []) + notes)
    cur = c.execute("""INSERT INTO barcode_scan(raw,code,how,read_votes,candidates,
                       candidates_json,note,scanned_at) VALUES(?,?,?,?,?,?,?,?)""",
                    (raw, code, how, votes, None if unanswered else len(cands),
                     json.dumps(cands)[:200000], note[:900], model.now_iso()))
    c.commit()
    return cur.lastrowid


def scan_again(req, c, m):
    """Ask again about a barcode whose lookup did not get an answer. A new scan
    row rather than an edit of the old one: the first lookup's failure is part of
    the record, and the read itself — typed, camera or photo — is the same one."""
    sid = req.fi("scan")
    s = c.execute("SELECT * FROM barcode_scan WHERE id=?", (sid,)).fetchone() if sid else None
    if not s or not s["code"]:
        return redirect("/scan")
    return redirect("/scan/choose?scan="
                    + str(_look_up(c, s["raw"], s["code"], s["how"], s["read_votes"],
                                   "Looked up again.")))


def _cand_card(i, cand, chosen_name="pick"):
    """One candidate edition, as a card with a button. Everything the source said."""
    bits = []
    for label, val in (("Year", cand.get("released")), ("Country", cand.get("country")),
                       ("Label", cand.get("label")), ("Catalogue no.", cand.get("catno")),
                       ("Discs", cand.get("discs")), ("Tracks", cand.get("tracks"))):
        if val:
            bits.append(f"<dt>{e(label)}</dt><dd>{e(val)}</dd>")
    if cand.get("edition_note"):
        bits.append(f'<dt>Pressing</dt><dd><strong>{e(cand["edition_note"])}</strong></dd>')
    if cand.get("disambiguation"):
        bits.append(f'<dt>Note</dt><dd>{e(cand["disambiguation"])}</dd>')
    if cand.get("format_words"):
        bits.append(f'<dt>As stated</dt><dd class="muted">{e(cand["format_words"])}</dd>')
    warn = ""
    if cand.get("media_kind") == "other":
        warn = ('<p class="note small">This does not look like a vinyl record — the source '
                'calls it something else. Worth checking before you add it.</p>')
    elif cand.get("media_kind") == "unknown":
        warn = ('<p class="muted small">The source did not say what this is pressed on.</p>')
    link = (f' · <a href="{e(cand["url"])}" rel="noreferrer noopener" target="_blank">'
            f'see it on {e(model.SOURCES.get(cand["source"], cand["source"]))}</a>'
            if cand.get("url") else "")
    return f"""<div class="card pick">
  <h3>{e(cand.get("title") or "Untitled")}</h3>
  <p class="muted small">{e(cand.get("artist_credit") or "")}{link}</p>
  <dl class="kv">{"".join(bits)}</dl>
  {warn}
  <p class="bar"><button class="btn" name="{chosen_name}" value="{i}">This is the one</button></p>
</div>"""


def scan_choose(req, c, m):
    sid = req.gi("scan")
    s = c.execute("SELECT * FROM barcode_scan WHERE id=?", (sid,)).fetchone() if sid else None
    if not s:
        return missing(req, c, "scan", "/scan", "Scan something")
    try:
        cands = json.loads(s["candidates_json"] or "[]")
    except ValueError:
        cands = []
    code = s["code"] or ""
    have = c.execute("SELECT * FROM release WHERE barcode=? ORDER BY id", (code,)).fetchall()

    have_html = ""
    if have:
        rows = "".join(
            f'<li><a href="/record?id={r["id"]}">{e(r["artist_credit"])} — {e(r["title"])}</a>'
            f' <span class="muted small">{e(r["catno"] or "")} {e(r["released"] or "")}</span> · '
            f'<a href="/copy/new?release={r["id"]}">add another copy</a></li>' for r in have)
        have_html = f"""<div class="note"><strong>You already have this barcode.</strong>
<ul>{rows}</ul>
If your new one is the same edition, add a copy to it. If it is a different pressing that
happens to share the barcode, pick it from the list below — that is a real thing that
happens.</div>"""

    notes_html = f'<div class="note">{e(s["note"])}</div>' if s["note"] else ""
    answered = s["candidates"] is not None
    again = f"""<form method="post" action="/scan/again" style="margin:0">
  <input type="hidden" name="scan" value="{sid}">
  <button class="btn">Look it up again</button></form>"""
    partial_html = ("" if answered else
                    f'<div class="bar"><span class="muted small">One source did not answer, '
                    f'so this list may be missing an edition.</span> {again}</div>')
    done_html = ""
    if s["release_id"]:
        r = c.execute("SELECT title, artist_credit FROM release WHERE id=?",
                      (s["release_id"],)).fetchone()
        if r:
            done_html = (f'<div class="flash">This scan is already on the shelf as '
                         f'<a href="/record?id={s["release_id"]}">{e(r["artist_credit"])} — '
                         f'{e(r["title"])}</a>.</div>')
    desc = bc.describe(code)
    where = (f'<p class="muted small">{e(desc["country_note"])}. That is where the number was '
             f'registered, not where the record was pressed or sold.</p>'
             if desc.get("country_note") else "")

    if not cands:
        if answered:
            head, said = (f"Nothing matched {code}",
                          "<p>Neither MusicBrainz nor Discogs has an edition with that barcode. "
                          "That is common for a small pressing, and it does not mean the barcode "
                          "is wrong.</p>")
        else:
            head, said = (f"No answer about {code} yet",
                          "<p>Not every source answered, so this is not the same as nothing "
                          "matching — the reason is above. Ask again in a moment, or add it by "
                          f'hand below.</p><div class="bar">{again}</div>')
        body = f"""<h1>{e(head)}</h1>
{done_html}{have_html}{notes_html}{where}
{said}
<form method="post" action="/scan/manual" class="card">
  <input type="hidden" name="scan" value="{sid}">
  <h3>Add it by hand</h3>
  <div class="grid">
    <div><label for="t">Title</label><input id="t" name="title" required></div>
    <div><label for="a">Artist</label><input id="a" name="artist_credit"></div>
    <div><label for="y">Year</label><input id="y" name="released" placeholder="1959"></div>
    <div><label for="co">Country</label><input id="co" name="country"></div>
    <div><label for="lb">Label</label><input id="lb" name="label"></div>
    <div><label for="cn">Catalogue no.</label><input id="cn" name="catno"></div>
    <div><label for="fm">Format</label><select id="fm" name="format">
      {opts(model.FORMATS.items(), "LP")}</select></div>
    <div><label for="sp">Speed</label><select id="sp" name="speed">
      {opts(model.SPEEDS.items(), "33")}</select></div>
  </div>
  <label for="en">Anything that marks this pressing out</label>
  <input id="en" name="edition_note" placeholder="180g, gatefold, numbered, blue vinyl">
  <p class="bar"><button class="btn">Add it</button>
  <a class="btn sec" href="/scan">Scan something else</a></p>
</form>"""
        return shell(req, c, "Nothing matched", body, "shelf", "/scan")

    groups = []
    for src in ("musicbrainz", "discogs"):
        grp = [(i, cd) for i, cd in enumerate(cands) if cd.get("source") == src]
        if not grp:
            continue
        diff = sources.differs([cd for _, cd in grp])
        head = (f'<h2>{e(model.SOURCES.get(src, src))} has '
                f'{len(grp)} edition{"" if len(grp) == 1 else "s"} with this barcode</h2>')
        if len(grp) > 1:
            head += (f'<p class="muted">They differ in: <strong>{e(", ".join(diff))}</strong>. '
                     f'Everything else about them matches.</p>' if diff else
                     '<p class="muted">Nothing this app can see tells them apart. Either is '
                     'as good a record of what you own.</p>')
        groups.append(head + '<div class="candidates">'
                      + "".join(_cand_card(i, cd) for i, cd in grp) + "</div>")

    one = len(cands) == 1
    lead = ("One edition matched. Check it is the record in your hand — the year and the "
            "catalogue number are on the back of the sleeve." if one else
            "More than one edition carries this barcode. Which one is in your hand?")
    body = f"""<h1>Which edition is this?</h1>
<p class="muted">Barcode {e(code)} · {e(desc["symbology"])}</p>
{done_html}{have_html}{notes_html}{partial_html}
<p>{e(lead)}</p>
{where}
<form method="post" action="/scan/choose">
<input type="hidden" name="scan" value="{sid}">
{"".join(groups)}
</form>
<div class="card">
  <h3>None of these</h3>
  <p class="muted small">If none of them is the record in your hand, add it by hand and keep
  the barcode.</p>
  <form method="post" action="/scan/manual">
    <input type="hidden" name="scan" value="{sid}">
    <label for="t2">Title</label><input id="t2" name="title" required>
    <label for="a2">Artist</label><input id="a2" name="artist_credit">
    <p class="bar"><button class="btn sec">Add by hand</button></p>
  </form>
</div>"""
    return shell(req, c, "Which edition?", body, "shelf", "/scan")


# ------------------------------------------------- turning a choice into a row

def find_or_make_artist(c, name, mbid="", sort=""):
    """The artist row for a credit, made if it is not there yet. None for a blank.

    Matched on the MusicBrainz id first, because that is an identity, and only
    then on the name, because a name is not. Two artists with the same name are a
    real thing and this will merge them — which is why the artist page lets you
    link an id, and why the id wins when there is one."""
    name = model.norm(name, 200)
    if not name:
        return None
    if mbid:
        r = c.execute("SELECT * FROM artist WHERE mbid=?", (mbid,)).fetchone()
        if r:
            return r["id"]
    r = c.execute("SELECT * FROM artist WHERE LOWER(name)=LOWER(?)", (name,)).fetchone()
    if r:
        if mbid and not r["mbid"]:
            c.execute("UPDATE artist SET mbid=?, sort_name=COALESCE(sort_name,?) WHERE id=?",
                      (mbid, model.norm(sort, 200) or None, r["id"]))
        return r["id"]
    cur = c.execute("""INSERT INTO artist(name,sort_name,mbid,watch,added_at)
                       VALUES(?,?,?,0,?)""",
                    (name, model.norm(sort, 200) or None, mbid or None, model.now_iso()))
    return cur.lastrowid


def release_from_candidate(c, cand, code, identified):
    """Create (or find) the release a candidate describes. Returns its id.

    If the same edition is already on the shelf — matched on the source's own id,
    never on a guess — the existing row is returned rather than a second one made.
    Scanning the same record twice must not quietly produce two of it.

    A MusicBrainz edition that links to Discogs carries both ids (see
    sources.fuller), so it finds the row a Discogs pick made earlier, and the row
    is given the id it was missing. That published link is the only ground on
    which two catalogues' entries are treated as one object."""
    mbid = cand.get("mbid") or None
    dgid = cand.get("discogs_release_id") or None
    for col, val in (("mbid", mbid), ("discogs_release_id", dgid)):
        if val:
            r = c.execute(f"SELECT * FROM release WHERE {col}=?", (val,)).fetchone()
            if r:
                for other, oval in (("mbid", mbid), ("discogs_release_id", dgid)):
                    if oval and r[other] is None and not c.execute(
                            f"SELECT 1 FROM release WHERE {other}=?", (oval,)).fetchone():
                        c.execute(f"UPDATE release SET {other}=? WHERE id=?", (oval, r["id"]))
                return r["id"]
    aid = find_or_make_artist(c, cand.get("artist_credit"), cand.get("artist_mbid") or "",
                              cand.get("artist_sort") or "")
    cur = c.execute("""INSERT INTO release(title,artist_credit,artist_id,barcode,released,
            country,label,catno,format,speed,edition_note,disambiguation,mbid,
            discogs_release_id,discogs_master_id,cover_url,identified,source,tracked,added_at,
            matrix,source_notes)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?)""",
        (model.norm(cand.get("title") or "Untitled", 300),
         model.norm(cand.get("artist_credit") or "", 300), aid,
         code or cand.get("barcode") or None, cand.get("released") or None,
         model.norm(cand.get("country") or "", 80) or None,
         model.norm(cand.get("label") or "", 160) or None,
         model.norm(cand.get("catno") or "", 80) or None,
         cand.get("format") if model.format_ok(cand.get("format") or "") else "unknown",
         cand.get("speed") if (cand.get("speed") in model.SPEEDS) else "unknown",
         model.norm(cand.get("edition_note") or "", 400) or None,
         model.norm(cand.get("disambiguation") or "", 300) or None,
         mbid, dgid, cand.get("discogs_master_id"),
         model.norm(cand.get("cover_url") or "", 500) or None,
         identified, cand.get("source") or "manual", model.now_iso(),
         cand.get("matrix_text"), cand.get("source_notes")))
    return cur.lastrowid


def scan_choose_post(req, c, m):
    sid = req.fi("scan")
    s = c.execute("SELECT * FROM barcode_scan WHERE id=?", (sid,)).fetchone() if sid else None
    if not s:
        return redirect("/scan")
    try:
        cands = json.loads(s["candidates_json"] or "[]")
    except ValueError:
        cands = []
    idx = req.fi("pick")
    if idx is None or not (0 <= idx < len(cands)):
        return redirect(f"/scan/choose?scan={sid}")
    if s["release_id"]:
        # A second press — a double tap, or the back button — is the same scan,
        # and one scan is one copy. A second copy of an edition is a second scan.
        return _already_filed(s)
    # The candidate as offered stays in candidates_json untouched — that is the
    # evidence of what the choice was between. What is filed is that candidate
    # completed from its source's full record, one request now it is chosen.
    cand, said = sources.fuller(c, cands[idx])
    rid = release_from_candidate(c, cand, s["code"],
                                 "sole_match" if len(cands) == 1 else "chosen")
    cur = c.execute("INSERT INTO copy(release_id,added_at) VALUES(?,?)",
                    (rid, model.now_iso()))
    c.execute("UPDATE barcode_scan SET release_id=? WHERE id=?", (rid, sid))
    c.commit()
    return flash_to(f"/grade?copy={cur.lastrowid}",
                    "Added. Now: what condition is it in?" + (f" {said}" if said else ""))


def _already_filed(s, what="scan"):
    return flash_to(f"/record?id={s['release_id']}",
                    f"That {what} was already added — this is the record it became. If you "
                    f"have a second copy, add it from here.")


def scan_manual(req, c, m):
    sid = req.fi("scan")
    s = c.execute("SELECT * FROM barcode_scan WHERE id=?", (sid,)).fetchone() if sid else None
    title = model.norm(req.f("title") or "", 300)
    if not title:
        return redirect(f"/scan/choose?scan={sid}" if sid else "/scan")
    if s and s["release_id"]:
        return _already_filed(s)
    cand = {
        "title": title, "artist_credit": req.f("artist_credit") or "",
        "released": model.partial_date(req.f("released") or "") or "",
        "country": req.f("country") or "", "label": req.f("label") or "",
        "catno": req.f("catno") or "", "format": req.f("format") or "unknown",
        "speed": req.f("speed") or "unknown", "edition_note": req.f("edition_note") or "",
        "source": "manual",
    }
    rid = release_from_candidate(c, cand, s["code"] if s else "", "unmatched")
    cur = c.execute("INSERT INTO copy(release_id,added_at) VALUES(?,?)", (rid, model.now_iso()))
    if s:
        c.execute("UPDATE barcode_scan SET release_id=? WHERE id=?", (rid, sid))
    c.commit()
    return flash_to(f"/grade?copy={cur.lastrowid}", "Added by hand. Now: what condition is it in?")


# --------------------------------------------------------- finding by name
#
# The way in for a record with no barcode, which is most records pressed before
# about 1980. The same shape as a scan: what was typed and what was offered are
# kept on a row (title_search), choosing completes the pick from its source, and
# a search that has become a record says so instead of filing a second copy.

def _search_vals(get):
    return {"artist": model.norm(get("artist") or "", 200),
            "title": model.norm(get("title") or "", 300),
            "catno": model.norm(get("catno") or "", 80)}


def _search_form(vals, button="Look it up", heading=""):
    v = lambda k: e(vals.get(k) or "")
    head = f'<h3 id="byname">{e(heading)}</h3>' if heading else ""
    return f"""<form method="post" action="/search" class="card">
  {head}
  <div class="grid">
    <div><label for="sa">Artist</label>
      <input id="sa" name="artist" value="{v("artist")}" autocomplete="off"></div>
    <div><label for="st">Title</label>
      <input id="st" name="title" value="{v("title")}" autocomplete="off"></div>
    <div><label for="sc">Catalogue number, if you can see one</label>
      <input id="sc" name="catno" value="{v("catno")}" autocomplete="off" spellcheck="false"
             placeholder="CL 1355"></div>
  </div>
  <p class="bar"><button class="btn">{e(button)}</button></p>
  <p class="muted small">A title or a catalogue number is enough. The catalogue number is on the
  label and usually the spine, and it is what tells one pressing from another — Kind of Blue has
  hundreds. Only vinyl editions are listed.</p>
</form>"""


def _searched(s):
    """What a search asked for, as one line."""
    head = " — ".join(x for x in (s["artist"], s["title"]) if x)
    return " · ".join(x for x in (head, s["catno"] and f"catalogue no. {s['catno']}") if x)


def search_page(req, c, m, err="", vals=None):
    vals = vals or _search_vals(req.g)
    err_html = f'<div class="err">{e(err)}</div>' if err else ""
    rows = ""
    for s in library.recent_searches(c, 8):
        if s["release_id"]:
            where = (f'<a href="/record?id={s["release_id"]}">{e(s["r_artist"] or "")} — '
                     f'{e(s["r_title"] or "")}</a>')
        elif (s["candidates_json"] or "[]") != "[]":
            where = f'<a href="/search/choose?search={s["id"]}">not added yet — choose the edition</a>'
        else:
            said = "nothing found" if s["candidates"] is not None else "no answer — ask again"
            where = f'<a href="/search/choose?search={s["id"]}">{e(said)}</a>'
        rows += (f'<tr><td class="small">{e(model.ago(s["searched_at"]))}</td>'
                 f'<td class="small">{e(_searched(s))}</td><td>{where}</td></tr>')
    recent_html = (f'<h2>Lately</h2><div class="scroll"><table><thead><tr><th>When</th>'
                   f'<th>Looked up</th><th>Became</th></tr></thead><tbody>{rows}</tbody>'
                   f'</table></div>') if rows else ""
    body = f"""<h1>Look a record up by name</h1>
<p class="muted">For a record with no barcode, or one whose barcode neither catalogue knows.
Pick the pressing from what MusicBrainz and Discogs list, and it is filed with their details
and its price is followed, just as a scan would be.</p>
{err_html}
{_search_form(vals)}
{recent_html}"""
    return shell(req, c, "Look it up by name", body, "shelf", "/scan")


def search_post(req, c, m):
    vals = _search_vals(req.f)
    if not sources.mb_title_query(vals["artist"], vals["title"], vals["catno"]):
        # Refused before anything is asked or written, and the form comes back
        # with what was typed in it.
        return search_page(req, c, m, "A title or a catalogue number is needed — an artist on "
                           "their own is every record they ever made.", vals)
    return redirect(f"/search/choose?search={_search_run(c, vals)}")


def _search_run(c, vals, said=""):
    """Ask both sources about a name and log the search. Returns its id.

    `candidates` is left NULL when a source did not answer, as on a scan."""
    unanswered, totals = [], {}
    cands, notes = sources.lookup_title(c, vals["artist"], vals["title"], vals["catno"],
                                        unanswered, totals)
    note = " ".join(([said] if said else []) + notes)
    cur = c.execute("""INSERT INTO title_search(artist,title,catno,candidates,candidates_json,
                       totals_json,note,searched_at) VALUES(?,?,?,?,?,?,?,?)""",
                    (vals["artist"], vals["title"], vals["catno"],
                     None if unanswered else len(cands), json.dumps(cands)[:200000],
                     json.dumps(totals), note[:900], model.now_iso()))
    c.commit()
    return cur.lastrowid


def search_again(req, c, m):
    """Ask again, as a new row: the search that got no answer stays in the log."""
    sid = req.fi("search")
    s = c.execute("SELECT * FROM title_search WHERE id=?", (sid,)).fetchone() if sid else None
    if not s:
        return redirect("/search")
    vals = {"artist": s["artist"], "title": s["title"], "catno": s["catno"]}
    return redirect(f"/search/choose?search={_search_run(c, vals, 'Looked up again.')}")


def _found_card(i, cand, have_rid=None):
    """One edition a name search offered. The year, country, label and catalogue
    number lead, because the title is the one thing every card on the page shares."""
    where = " ".join(x for x in (cand.get("label"), cand.get("catno")) if x)
    lead = " · ".join(x for x in (cand.get("released") or "year not given",
                                  cand.get("country"), where) if x)
    bits = []
    if cand.get("edition_note"):
        bits.append(f'<dt>Pressing</dt><dd><strong>{e(cand["edition_note"])}</strong></dd>')
    if cand.get("disambiguation"):
        bits.append(f'<dt>Note</dt><dd>{e(cand["disambiguation"])}</dd>')
    if (model.intn(cand.get("discs")) or 0) > 1:
        bits.append(f'<dt>Discs</dt><dd>{e(cand["discs"])}</dd>')
    if cand.get("tracks"):
        bits.append(f'<dt>Tracks</dt><dd>{e(cand["tracks"])}</dd>')
    # Discogs' format words are its pressing words with "Vinyl" in front, so they
    # are printed only when they say something the pressing line did not.
    words = cand.get("format_words") or ""
    if words and (cand.get("edition_note") or "") not in words:
        bits.append(f'<dt>As stated</dt><dd class="muted">{e(words)}</dd>')
    # Two Discogs pressings can match on every field a search result carries and
    # differ only in the run-out stamps, so the release number names which is which.
    said = (f"Discogs release {cand['discogs_release_id']}"
            if cand.get("source") == "discogs" and cand.get("discogs_release_id")
            else f"see it on {model.SOURCES.get(cand.get('source'), cand.get('source') or '')}")
    link = (f' · <a href="{e(cand["url"])}" rel="noreferrer noopener" target="_blank">'
            f'{e(said)}</a>' if cand.get("url") else "")
    have = (f'<p class="note small">Already on your shelf — <a href="/record?id={have_rid}">'
            f'see it</a>. Choosing it adds another copy.</p>' if have_rid else "")
    return f"""<div class="card pick">
  <h3>{e(lead)}</h3>
  <p class="small">{e(cand.get("title") or "Untitled")}
  <span class="muted">— {e(cand.get("artist_credit") or "")}</span>{link}</p>
  <dl class="kv">{"".join(bits)}</dl>
  {have}
  <p class="bar"><button class="btn" name="pick" value="{i}">This is the one</button></p>
</div>"""


def _manual_from_search(sid, vals):
    v = lambda k: e(vals.get(k) or "")
    return f"""<form method="post" action="/search/manual" class="card">
  <input type="hidden" name="search" value="{sid}">
  <h3>Or add it by hand</h3>
  <div class="grid">
    <div><label for="t">Title</label><input id="t" name="title" required value="{v("title")}"></div>
    <div><label for="a">Artist</label><input id="a" name="artist_credit" value="{v("artist")}"></div>
    <div><label for="y">Year</label><input id="y" name="released" placeholder="1959"></div>
    <div><label for="co">Country</label><input id="co" name="country"></div>
    <div><label for="lb">Label</label><input id="lb" name="label"></div>
    <div><label for="cn">Catalogue no.</label><input id="cn" name="catno" value="{v("catno")}"></div>
    <div><label for="fm">Format</label><select id="fm" name="format">
      {opts(model.FORMATS.items(), "LP")}</select></div>
    <div><label for="sp">Speed</label><select id="sp" name="speed">
      {opts(model.SPEEDS.items(), "33")}</select></div>
  </div>
  <label for="en">Anything that marks this pressing out</label>
  <input id="en" name="edition_note" placeholder="180g, gatefold, numbered, blue vinyl">
  <p class="bar"><button class="btn sec">Add it by hand</button></p>
</form>"""


def search_choose(req, c, m):
    sid = req.gi("search")
    s = c.execute("SELECT * FROM title_search WHERE id=?", (sid,)).fetchone() if sid else None
    if not s:
        return missing(req, c, "search", "/search", "Look something up")
    try:
        cands = json.loads(s["candidates_json"] or "[]")
    except ValueError:
        cands = []
    try:
        totals = json.loads(s["totals_json"] or "{}")
    except ValueError:
        totals = {}
    vals = {"artist": s["artist"], "title": s["title"], "catno": s["catno"]}
    answered = s["candidates"] is not None

    # An edition already on the shelf is marked by the source's own id — never by
    # a title that looks the same, because two editions of one album do.
    have = {}
    for r in c.execute("SELECT id, mbid, discogs_release_id FROM release").fetchall():
        if r["mbid"]:
            have[("musicbrainz", r["mbid"])] = r["id"]
        if r["discogs_release_id"]:
            have[("discogs", str(r["discogs_release_id"]))] = r["id"]

    done_html = ""
    if s["release_id"]:
        r = c.execute("SELECT title, artist_credit FROM release WHERE id=?",
                      (s["release_id"],)).fetchone()
        if r:
            done_html = (f'<div class="flash">This search is already on the shelf as '
                         f'<a href="/record?id={s["release_id"]}">{e(r["artist_credit"])} — '
                         f'{e(r["title"])}</a>.</div>')
    notes_html = f'<div class="note">{e(s["note"])}</div>' if s["note"] else ""
    again = f"""<form method="post" action="/search/again" style="margin:0">
  <input type="hidden" name="search" value="{sid}">
  <button class="btn">Look it up again</button></form>"""
    partial_html = ("" if answered else
                    f'<div class="bar"><span class="muted small">One source did not answer, '
                    f'so this list may be missing an edition.</span> {again}</div>')
    refine = _search_form(vals, "Search again", "Not there? Search again")

    if not cands:
        if answered:
            head, said = (f"Nothing found for {_searched(s)}",
                          "<p>Neither MusicBrainz nor Discogs lists a vinyl edition under that. "
                          "Fewer words often find it — the title on its own, or the catalogue "
                          "number on its own — because every word typed has to match.</p>")
        else:
            head, said = (f"No answer about {_searched(s)} yet",
                          "<p>Not every source answered, so this is not the same as nothing "
                          "being found — the reason is above. Ask again in a moment.</p>"
                          f'<div class="bar">{again}</div>')
        body = f"""<h1>{e(head)}</h1>
{done_html}{notes_html}
{said}
{refine}
{_manual_from_search(sid, vals)}"""
        return shell(req, c, "Nothing found", body, "shelf", "/scan")

    groups, more = [], False
    # Discogs first: it lists far more old pressings (359 vinyl Kind of Blues to
    # MusicBrainz's 62), and a Discogs edition is one whose price can be followed.
    for src in ("discogs", "musicbrainz"):
        grp = [(i, cd) for i, cd in enumerate(cands) if cd.get("source") == src]
        name = model.SOURCES.get(src, src)
        total = model.intn(totals.get(src))
        if not grp:
            if total == 0:
                groups.append(f'<h2>{e(name)}</h2><p class="muted">No vinyl edition listed '
                              f'under that.</p>')
            continue
        if total and total > len(grp):
            more = True
            head = (f'<h2>{e(name)} lists {total} vinyl editions</h2>'
                    f'<p class="muted small">These are the first {len(grp)}, in the order '
                    f'{e(name)} ranks them.</p>')
        else:
            head = (f'<h2>{e(name)} lists {len(grp)} vinyl '
                    f'edition{"" if len(grp) == 1 else "s"}</h2>')
        groups.append(head + '<div class="candidates">' + "".join(
            _found_card(i, cd, have.get((src, str(cd.get("source_id") or ""))))
            for i, cd in grp) + "</div>")
    if more and s["catno"]:
        missing_bits = " or ".join(w for w, have in (("the title", s["title"]),
                                                     ("the artist", s["artist"])) if not have)
        further = (f' Adding {missing_bits} <a href="#byname">below</a> narrows it further.'
                   if missing_bits else "")
        narrow = ('<p class="note">More pressings under that catalogue number than fit on one '
                  'page. These are the closest matches by each catalogue\'s own ranking.'
                  f'{further}</p>')
    elif more:
        narrow = ('<p class="note">More than one page of these. The catalogue number from the '
                  'label narrows hundreds of pressings to a handful — '
                  '<a href="#byname">add it below</a>.</p>')
    else:
        narrow = ""
    body = f"""<h1>Which pressing is yours?</h1>
<p class="muted">Looked up: {e(_searched(s))}</p>
{done_html}{notes_html}{partial_html}
<p>The label, the catalogue number and the year are on the back of the sleeve and on the
record's own label. Those are what tell these apart.</p>
{narrow}
<form method="post" action="/search/choose">
<input type="hidden" name="search" value="{sid}">
{"".join(groups)}
</form>
{refine}
{_manual_from_search(sid, vals)}"""
    return shell(req, c, "Which pressing?", body, "shelf", "/scan")


def search_choose_post(req, c, m):
    sid = req.fi("search")
    s = c.execute("SELECT * FROM title_search WHERE id=?", (sid,)).fetchone() if sid else None
    if not s:
        return redirect("/search")
    try:
        cands = json.loads(s["candidates_json"] or "[]")
    except ValueError:
        cands = []
    idx = req.fi("pick")
    if idx is None or not (0 <= idx < len(cands)):
        return redirect(f"/search/choose?search={sid}")
    if s["release_id"]:
        return _already_filed(s, "search")
    # Exactly as on a scan: the offer stays in candidates_json untouched, and
    # what is filed is the pick completed from its source's full record. Always
    # "chosen", even from one result — a name matching one thing is somebody's
    # choice, not a barcode matching one edition.
    cand, said = sources.fuller(c, cands[idx])
    rid = release_from_candidate(c, cand, "", "chosen")
    cur = c.execute("INSERT INTO copy(release_id,added_at) VALUES(?,?)", (rid, model.now_iso()))
    c.execute("UPDATE title_search SET release_id=? WHERE id=?", (rid, sid))
    c.commit()
    return flash_to(f"/grade?copy={cur.lastrowid}",
                    "Added. Now: what condition is it in?" + (f" {said}" if said else ""))


def search_manual(req, c, m):
    sid = req.fi("search")
    s = c.execute("SELECT * FROM title_search WHERE id=?", (sid,)).fetchone() if sid else None
    title = model.norm(req.f("title") or "", 300)
    if not title:
        return redirect(f"/search/choose?search={sid}" if s else "/search")
    if s and s["release_id"]:
        return _already_filed(s, "search")
    cand = {
        "title": title, "artist_credit": req.f("artist_credit") or "",
        "released": model.partial_date(req.f("released") or "") or "",
        "country": req.f("country") or "", "label": req.f("label") or "",
        "catno": req.f("catno") or "", "format": req.f("format") or "unknown",
        "speed": req.f("speed") or "unknown", "edition_note": req.f("edition_note") or "",
        "source": "manual",
    }
    rid = release_from_candidate(c, cand, "", "unmatched")
    cur = c.execute("INSERT INTO copy(release_id,added_at) VALUES(?,?)", (rid, model.now_iso()))
    if s:
        c.execute("UPDATE title_search SET release_id=? WHERE id=?", (rid, sid))
    c.commit()
    return flash_to(f"/grade?copy={cur.lastrowid}", "Added by hand. Now: what condition is it in?")


# ------------------------------------------------------------------- grading

def _grade_radios(scale, field, chosen):
    """Five grades and three reasons not to grade, as one radio group.

    One group rather than two controls, because "a 3" and "sealed, so not graded"
    are answers to the same question and only one of them can be true. The words
    beside each number are the whole point: they are what make your 3 today and
    your 3 next year the same 3."""
    table = model.GRADE_SLEEVE if scale == "sleeve" else model.GRADE_MEDIA
    out = []
    for g in model.GRADES:
        nm, words = table[g]
        sel = " checked" if str(chosen) == str(g) else ""
        out.append(f"""<label><input type="radio" name="{field}" value="{g}"{sel}>
<span class="n">{g}</span><span><span class="nm">{e(nm)}</span>
<span class="gold muted small"> · {e(model.goldmine(g))}</span>
<span class="w">{e(words)}</span></span></label>""")
    for key, words in model.UNGRADED_REASONS.items():
        sel = " checked" if str(chosen) == f"u:{key}" else ""
        label = key.replace("_", " ").capitalize()
        out.append(f"""<label><input type="radio" name="{field}" value="u:{key}"{sel}>
<span class="n">—</span><span><span class="nm">{e(label)}</span>
<span class="w">{e(words)}</span></span></label>""")
    return f'<div class="grades">{"".join(out)}</div>'


def _read_grade(raw):
    """A radio value to (grade, ungraded_reason). Exactly one is set, always."""
    v = (raw or "").strip()
    if v.startswith("u:") and v[2:] in model.UNGRADED_REASONS:
        return None, v[2:]
    n = model.intn(v)
    if model.grade_ok(n):
        return n, None
    return None, "not_checked"


def grade_form(req, c, m):
    cid = req.gi("copy")
    cp = c.execute("SELECT * FROM copy WHERE id=?", (cid,)).fetchone() if cid else None
    if not cp:
        return missing(req, c, "copy")
    rel = c.execute("SELECT * FROM release WHERE id=?", (cp["release_id"],)).fetchone()
    last = library.current_grading(c, cid)
    mv = (str(last["media_grade"]) if last and last["media_grade"]
          else (f'u:{last["media_ungraded"]}' if last and last["media_ungraded"] else ""))
    sv = (str(last["sleeve_grade"]) if last and last["sleeve_grade"]
          else (f'u:{last["sleeve_ungraded"]}' if last and last["sleeve_ungraded"] else ""))
    history = ""
    rows = c.execute("""SELECT * FROM grading WHERE copy_id=? ORDER BY id DESC""",
                     (cid,)).fetchall()
    if rows:
        items = "".join(
            f'<tr><td>{e(r["graded_on"] or (r["graded_at"] or "")[:10])}</td>'
            f'<td>{e(r["media_grade"] or model.UNGRADED_REASONS.get(r["media_ungraded"] or "", ""))}</td>'
            f'<td>{e(r["sleeve_grade"] or model.UNGRADED_REASONS.get(r["sleeve_ungraded"] or "", ""))}</td>'
            f'<td class="small">{e(r["note"] or "")}</td></tr>' for r in rows)
        history = f"""<h2>Graded before</h2><div class="scroll"><table><thead><tr>
<th>On</th><th>Record</th><th>Sleeve</th><th>Note</th></tr></thead><tbody>{items}</tbody></table></div>
<p class="muted small">Grading again adds a row. Nothing is overwritten, so a record that
was played hard for a year can show that it was.</p>"""

    body = f"""<h1>What condition is it in?</h1>
<p class="muted">{e(rel["artist_credit"])} — <a href="/record?id={rel["id"]}">{e(rel["title"])}</a>
{e(rel["catno"] or "")} {e(rel["released"] or "")}</p>
<form method="post" action="/grade">
<input type="hidden" name="copy" value="{cid}">
<div class="card"><h3>The record</h3>{_grade_radios("media", "media", mv)}</div>
<div class="card"><h3>The packaging</h3>{_grade_radios("sleeve", "sleeve", sv)}</div>
<div class="card">
  <label for="on">Graded on</label>
  <input id="on" name="graded_on" type="date" value="{e(model.today_iso())}">
  <label for="note">Anything worth saying about it</label>
  <textarea id="note" name="note" rows="3"
    placeholder="Faint ring wear, one tick in the run-out of side B, price sticker on the back"></textarea>
  <p class="muted small">Whatever you write here is kept exactly as you type it, for good.</p>
</div>
<p class="bar"><button class="btn big">Save the condition</button>
<a class="btn sec" href="/record?id={rel["id"]}">Skip for now</a></p>
</form>
{history}"""
    return shell(req, c, "Condition", body, "shelf")


def grade_save(req, c, m):
    cid = req.fi("copy")
    cp = c.execute("SELECT * FROM copy WHERE id=?", (cid,)).fetchone() if cid else None
    if not cp:
        return redirect("/shelf")
    mg, mu = _read_grade(req.f("media"))
    sg, su = _read_grade(req.f("sleeve"))
    c.execute("""INSERT INTO grading(copy_id,graded_on,media_grade,media_ungraded,
                 sleeve_grade,sleeve_ungraded,note,graded_at) VALUES(?,?,?,?,?,?,?,?)""",
              (cid, model.datestr(req.f("graded_on")), mg, mu, sg, su,
               (req.f("note") or "").strip() or None, model.now_iso()))
    c.commit()
    where = f'/record?id={cp["release_id"]}'
    return flash_to(where, "Condition saved.")


# ------------------------------------------------------------- one record

def _grade_pill(gv):
    if not gv["has"]:
        return pill("not graded", "warn")
    def one(lab, num, name, gold, why):
        if num:
            return f'{e(lab)} <strong>{num}</strong> <span class="muted">{e(name)} · {e(gold)}</span>'
        return f'{e(lab)} <span class="muted">{e(why or "not graded")}</span>'
    return (one("Record", gv["media"], gv["media_name"], gv.get("media_gold", ""), gv["media_why"])
            + " &nbsp;·&nbsp; "
            + one("Sleeve", gv["sleeve"], gv["sleeve_name"], gv.get("sleeve_gold", ""),
                  gv["sleeve_why"]))


def _series_block(c, rid, key, a):
    pts = prices.points(c, rid, key)
    spark = prices.sparkline(pts)
    head = f'{e(model.PRICE_KINDS.get(key["kind"], (key["kind"], ""))[0])} · {e(key["currency"])}'
    band = (f' · quoted for {model.goldmine(key["cond_band"])}' if key["cond_band"]
            else " · any condition")
    line = prices.describe(a)
    conf = prices.confound(a)
    conf_html = f'<p class="muted small">{e(conf)}</p>' if conf else ""
    first = pts[0] if pts else None
    last = pts[-1] if pts else None
    range_txt = ""
    if first and last and len(pts) > 1:
        range_txt = (f'{e(money(first["amount"], key["currency"]))} on {e(first["observed_at"][:10])} '
                     f'→ {e(money(last["amount"], key["currency"]))} on {e(last["observed_at"][:10])}, '
                     f'{len(pts)} observations')
    nfs = ""
    if last and last["num_for_sale"] is not None:
        nfs = f' · {last["num_for_sale"]} copies for sale'
    return f"""<div class="card">
  <h3>{head}{band}</h3>
  <p class="move">{e(line)}</p>
  {conf_html}
  <p>{spark}</p>
  <p class="muted small">{range_txt}{nfs}</p>
  <p class="muted small">{e(prices.kind_caveat(key["kind"]))}</p>
</div>"""


def _default_rule(c):
    return c.execute("""SELECT * FROM alert_rule WHERE kind='price_move' AND release_id IS NULL
                        AND artist_id IS NULL ORDER BY id DESC LIMIT 1""").fetchone()


def _own_rule(c, rid):
    """A record's own price rule, switched on or off, or None if it uses the default."""
    return c.execute("""SELECT * FROM alert_rule WHERE kind='price_move' AND release_id=?
                        ORDER BY id DESC LIMIT 1""", (rid,)).fetchone()


def _rule_values(c, rule):
    """A rule's thresholds with every blank filled from the settings, which mirror
    the default rule. A record's own rule leaves window, minimum and cooldown
    blank on purpose: those it inherits."""
    def pick(col, fallback):
        v = rule[col] if rule is not None else None
        return fallback if v is None else v
    return {"pct": float(pick("pct_threshold", db.get_float(c, "move_pct", 20.0))),
            "abs": float(pick("abs_threshold", db.get_float(c, "move_abs", 3.0))),
            "window": int(pick("window_days", db.get_int(c, "move_window_days", 30))),
            "min_obs": int(pick("min_obs", db.get_int(c, "move_min_obs", 3))),
            "direction": pick("direction", "both") or "both"}


def _rule_sentence(c, rule, cur):
    v = _rule_values(c, rule)
    way = {"up": " upward", "down": " downward"}.get(v["direction"], "")
    return (f'Told when it moves{way} at least {v["pct"]:g}% and {model.money(v["abs"], cur)} '
            f'against the median of the last {v["window"]} days, once there are '
            f'{v["min_obs"]} observations to go on.')


def _how_identified(c, rel):
    """How a record was settled, in words. Read from whichever way in came first —
    a scan or a search by name — because `identified` says how, not from what, and
    a record can be both: found by name, then a second copy scanned."""
    first = c.execute("""SELECT 'name' AS how, searched_at AS at FROM title_search
                         WHERE release_id=:id UNION ALL
                         SELECT 'scan', scanned_at FROM barcode_scan WHERE release_id=:id
                         ORDER BY at LIMIT 1""", {"id": rel["id"]}).fetchone()
    words = model.IDENTIFIED_BY_NAME if first and first["how"] == "name" else {}
    return words.get(rel["identified"]) or model.IDENTIFIED.get(rel["identified"], "")


def record_page(req, c, m):
    rid = req.gi("id")
    d = library.release_detail(c, rid) if rid else None
    if not d:
        return missing(req, c, "record")
    bits = []
    for lab, val in (("Artist", d["artist_credit"]), ("Year", d["released"]),
                     ("Country", d["country"]), ("Label", d["label"]),
                     ("Catalogue no.", d["catno"]),
                     ("Format", model.FORMATS.get(d["format"], d["format"])),
                     ("Speed", model.SPEEDS.get(d["speed"], "")),
                     ("Pressing", d["edition_note"]), ("Barcode", d["barcode"])):
        if val:
            bits.append(f"<dt>{e(lab)}</dt><dd>{e(val)}</dd>")
    bits.append(f'<dt>How it was identified</dt><dd>{e(_how_identified(c, d))}'
                f' <span class="muted">({e(model.SOURCES.get(d["source"], d["source"]))})</span></dd>')

    bar = ""
    if d["barcode"]:
        bar = (f'<p><img class="bcode" src="/barcode.svg?code={e(d["barcode"])}" alt="Barcode '
               f'{e(d["barcode"])}" width="240"></p>')

    # A link to the cover, never an <img>: embedding it would tell that host which
    # of your records you were looking at, and when. Only a web address is linked
    # — the value came from a third party.
    cover = ""
    if (d["cover_url"] or "").startswith(("https://", "http://")):
        host = urllib.parse.urlparse(d["cover_url"]).netloc
        cover = (f'<p class="small"><a href="{e(d["cover_url"])}" target="_blank" '
                 f'rel="noreferrer noopener">The cover</a> <span class="muted">— a link to '
                 f'{e(host)} rather than a picture here, because showing it would tell '
                 f'{e(host)} which of your records you were looking at.</span></p>')
    edit_link = (f'<p class="small noprint"><a href="/record/edit?id={rid}">Correct these '
                 f'details</a></p>' if d["source"] == "manual" else
                 f'<p class="muted small">As {e(model.SOURCES.get(d["source"], d["source"]))} '
                 f'lists it. Your own words go in the notes below.</p>')

    # What Discogs says about the pressing. NULL is "never asked" and '' is "asked,
    # and Discogs listed nothing", so the page can say which.
    pressing = []
    if d["matrix"]:
        lines = "".join(f"<li><code>{e(ln)}</code></li>"
                        for ln in d["matrix"].splitlines() if ln.strip())
        pressing.append(f"<h4>Run-out stamps</h4><ul>{lines}</ul>"
                        '<p class="muted small">Stamped or etched in the dead wax between the '
                        'last track and the label. Compare them with your copy: they are the '
                        'surest way to tell two near-identical pressings apart.</p>')
    elif d["matrix"] == "":
        pressing.append('<p class="muted small">Discogs lists no run-out stamps for this '
                        'release.</p>')
    if d["source_notes"]:
        pressing.append(f'<h4>Discogs\' notes</h4><div class="quote">{e(d["source_notes"])}</div>')
    ask = ""
    if d["mbid"] and not d["discogs_release_id"]:
        ask = "Ask MusicBrainz for its Discogs link"
        pressing.append('<p class="small">MusicBrainz may know which Discogs release this '
                        'is. With that link the price can be followed, and Discogs\' notes '
                        'and run-out stamps read.</p>')
    elif d["discogs_release_id"] and (d["matrix"] is None or d["source_notes"] is None):
        ask = "Fetch the run-out stamps and notes from Discogs"
    if ask:
        pressing.append(f"""<form method="post" action="/record/fetch" class="noprint">
  <input type="hidden" name="id" value="{rid}">
  <p class="bar"><button class="btn sec sm">{e(ask)}</button>
  <span class="muted small">One request.</span></p></form>""")
    pressing_html = (f'<h2>The pressing, as Discogs lists it</h2><div class="card">'
                     f'{"".join(pressing)}</div>' if pressing else "")

    copies = []
    for cp in d["copies"]:
        gv = cp["grade"]
        state = "" if cp["owned"] else pill("sold or gone", "warn")
        paid = (f'<span class="muted small">paid {e(money(cp["paid"]["amount"], cp["paid"]["currency"]))}</span>'
                if cp["paid"] else "")
        if cp["sold"]:
            paid += (f' <span class="muted small">· sold for '
                     f'{e(money(cp["sold"]["amount"], cp["sold"]["currency"]))}</span>')
        copies.append(f"""<div class="card">
  <p>{_grade_pill(gv)} {state}</p>
  <p class="muted small">{e(gv["note"] or "")}</p>
  <p class="muted small">{("graded " + e(gv["on"])) if gv["on"] else "never graded"}
    {(" · acquired " + e(cp["acquired_on"])) if cp["acquired_on"] else ""}
    {(" · from " + e(cp["acquired_from"])) if cp["acquired_from"] else ""}
    {(" · " + e(cp["location"])) if cp["location"] else ""} {paid}</p>
  <p class="bar"><a class="btn sm" href="/grade?copy={cp["id"]}">Grade it</a>
    <a class="btn sec sm" href="/copy?id={cp["id"]}">This copy</a></p>
</div>""")
    if not copies:
        copies.append('<p class="muted">No copies recorded — this record is followed but not '
                      'owned. That is a perfectly good way to use a wantlist.</p>')

    series_html = "".join(_series_block(c, rid, k, a) for k, a in d["assessments"])
    if not series_html:
        why = ("Nothing has been looked up yet." if d["discogs_release_id"]
               else "There is no Discogs release linked, so there is nothing to look up "
                    "automatically. You can link one below, or record prices by hand.")
        series_html = f'<p class="muted">{e(why)}</p>'

    cur = db.get(c, "currency", "GBP")
    own = _own_rule(c, rid)
    mode = "default" if not own else ("own" if own["active"] else "off")
    if mode == "off":
        rule_txt = ("Never told about this record's price — its own rule is switched off. "
                    "The price is still looked up and shown here.")
    elif d["rule"] is None:
        rule_txt = "The default rule is switched off, so no record raises a price alert."
    else:
        rule_txt = _rule_sentence(c, d["rule"], cur) + (
            " This record's own rule." if mode == "own" else " The default rule.")
    if not d["tracked"]:
        rule_txt += (" But its price is not being followed, so nothing is looked up and "
                     "nothing can move.")
    dv = _rule_values(c, _default_rule(c))
    fv = _rule_values(c, own if own else _default_rule(c))
    chk = lambda v: " checked" if mode == v else ""
    rule_form = f"""<form method="post" action="/record/rule" class="card noprint">
  <input type="hidden" name="id" value="{rid}">
  <h3>When to tell me about this one</h3>
  <p class="muted small">{e(rule_txt)}</p>
  <label><input type="radio" name="mode" value="default"{chk("default")}>
    The default — {dv["pct"]:g}% and {e(money(dv["abs"], cur))}, set under
    <a href="/rules">When to tell me</a></label>
  <label><input type="radio" name="mode" value="own"{chk("own")}> Its own thresholds</label>
  <div class="grid">
    <div><label for="rp">Moved at least</label>
      <input id="rp" name="pct" inputmode="decimal" value="{fv["pct"]:g}"> %</div>
    <div><label for="ra">And at least</label>
      <input id="ra" name="abs" inputmode="decimal" value="{fv["abs"]:.2f}">
      <span class="muted small">{e(cur)}</span></div>
    <div><label for="rd">Which way</label><select id="rd" name="direction">
      {opts([("both", "up or down"), ("up", "only up"), ("down", "only down")], fv["direction"])}
      </select></div>
  </div>
  <label><input type="radio" name="mode" value="off"{chk("off")}> Never tell me about this one</label>
  <p class="muted small">Its own thresholds still need both crossed, against the same window
  and minimum number of observations as the default.</p>
  <p class="bar"><button class="btn sec sm">Save</button></p>
</form>"""

    alerts_html = ""
    if d["alerts"]:
        items = "".join(f'<li>{e(alerts_mod.headline(c, a))} '
                        f'<span class="muted small">{e(a["raised_at"][:10])}</span></li>'
                        for a in d["alerts"][:8])
        alerts_html = f"<h2>What it has done</h2><ul>{items}</ul>"

    track_label = "Stop following the price" if d["tracked"] else "Follow the price"
    link_form = f"""<form method="post" action="/record/link" class="card">
  <input type="hidden" name="id" value="{rid}">
  <h3>Discogs link</h3>
  <label for="dg">Discogs release number, or its address</label>
  <input id="dg" name="discogs_release_id" autocomplete="off"
         value="{e(d["discogs_release_id"] or "")}">
  <p class="muted small">The number in a Discogs release URL — pasting the whole address
  works too. This is what a price is looked up against; without it there is nothing public
  to follow. Empty the box to remove the link.</p>
  <p class="bar"><button class="btn sec sm">Save the link</button>
  {f'<a class="btn sec sm" href="https://www.discogs.com/release/{d["discogs_release_id"]}" target="_blank" rel="noreferrer noopener">Open on Discogs</a>' if d["discogs_release_id"] else ""}
  </p></form>"""

    # An artist picked from a Discogs edition arrives without a MusicBrainz id,
    # and watching for new records needs one. Rather than guessing which of the
    # several people with that name it is, the app says so and puts the search
    # one tap away with the name already typed — the person confirms.
    watch_note = ""
    if d["artist"] and not d["artist"]["mbid"]:
        watch_note = (
            f'<div class="note noprint">To be told when {e(d["artist"]["name"])} puts out '
            f'something new, they need linking to MusicBrainz first — this edition came '
            f'from Discogs, which does not carry that identity. '
            f'<a href="/artist?id={d["artist"]["id"]}&amp;find='
            f'{urllib.parse.quote(d["artist"]["name"])}">Find them</a> and pick the right '
            f'one.</div>')
    elif d["artist"] and not d["artist"]["watch"]:
        watch_note = (
            f'<p class="muted small noprint"><a href="/artist?id={d["artist"]["id"]}">'
            f'Watch {e(d["artist"]["name"])}</a> to be told when they put out something new.</p>')

    body = f"""<h1>{e(d["title"])}</h1>
<p class="muted">{e(d["artist_credit"])}{f' · <a href="/artist?id={d["artist"]["id"]}">artist page</a>' if d["artist"] else ""}</p>
{watch_note}
<div class="bar noprint">
  <form method="post" action="/prices/check" style="margin:0">
    <input type="hidden" name="id" value="{rid}">
    <button class="btn">Check the price now</button></form>
  <form method="post" action="/record/track" style="margin:0">
    <input type="hidden" name="id" value="{rid}">
    <button class="btn sec">{e(track_label)}</button></form>
  <a class="btn sec" href="/copy/new?release={rid}">Add a copy</a>
</div>
<div class="card"><dl class="kv">{"".join(bits)}</dl>{bar}{cover}{edit_link}</div>

<h2>Your copies</h2>
{"".join(copies)}
{pressing_html}

<h2>Price</h2>
{series_html}
{rule_form}
<form method="post" action="/prices/add" class="card">
  <input type="hidden" name="id" value="{rid}">
  <h3>Record a price by hand</h3>
  <div class="grid">
    <div><label for="pk">What kind of number</label><select id="pk" name="kind">
      {opts([(k, v[0]) for k, v in model.PRICE_KINDS.items()
             if k != "lowest_asking" and k not in model.COPY_KINDS], "sale_seen")}
      </select></div>
    <div><label for="pa">Amount</label><input id="pa" name="amount" inputmode="decimal"></div>
    <div><label for="pc">Currency</label><select id="pc" name="currency">
      {opts(model.currency_choices(cur), cur)}</select></div>
    <div><label for="pb">Condition it was quoted for</label><select id="pb" name="cond_band">
      {opts([(g, f"{g} — {model.goldmine(g)}") for g in model.GRADES], "", "not stated")}
      </select></div>
    <div><label for="pd">Seen on</label><input id="pd" name="on" type="date"
         value="{e(model.today_iso())}"></div>
  </div>
  <label for="pn">Where you saw it</label><input id="pn" name="note">
  <p class="muted small">What you paid for a copy, and what you sold one for, are recorded on
  that copy's own page.</p>
  <p class="bar"><button class="btn sec">Record it</button></p>
</form>
{link_form}
<form method="post" action="/record/notes" class="card">
  <input type="hidden" name="id" value="{rid}">
  <h3>Your notes</h3>
  <textarea name="notes" rows="4">{e(d["notes"] or "")}</textarea>
  <p class="bar"><button class="btn sec sm">Save</button></p>
</form>
{alerts_html}
<form method="post" action="/record/delete" class="noprint"
      onsubmit="return confirm('Delete this record, its copies and its price history?')">
  <input type="hidden" name="id" value="{rid}">
  <p><button class="btn sec sm">Delete this record</button></p></form>"""
    return shell(req, c, d["title"], body, "shelf", "/shelf")


def record_track(req, c, m):
    rid = req.fi("id")
    r = c.execute("SELECT tracked FROM release WHERE id=?", (rid,)).fetchone()
    if not r:
        return redirect("/shelf")
    c.execute("UPDATE release SET tracked=? WHERE id=?", (0 if r["tracked"] else 1, rid))
    c.commit()
    return flash_to(f"/record?id={rid}",
                    "Not following this one any more." if r["tracked"] else "Following it.")


def record_notes(req, c, m):
    rid = req.fi("id")
    c.execute("UPDATE release SET notes=? WHERE id=?", ((req.f("notes") or "").strip() or None, rid))
    c.commit()
    return flash_to(f"/record?id={rid}", "Saved.")


def record_link(req, c, m):
    rid = req.fi("id")
    if not rid or not c.execute("SELECT 1 FROM release WHERE id=?", (rid,)).fetchone():
        return redirect("/shelf")
    dg, problem = model.discogs_release_ref(req.f("discogs_release_id"))
    if problem:
        return flash_to(f"/record?id={rid}", f"{problem} The link was left as it was.")
    try:
        c.execute("UPDATE release SET discogs_release_id=? WHERE id=?", (dg, rid))
        c.commit()
    except Exception:
        return flash_to(f"/record?id={rid}",
                        "Another record on the shelf is already linked to that Discogs release.")
    return flash_to(f"/record?id={rid}", "Linked." if dg else "Link removed.")


def record_delete(req, c, m):
    rid = req.fi("id")
    c.execute("DELETE FROM release WHERE id=?", (rid,))
    c.commit()
    return flash_to("/shelf", "Deleted.")


def record_rule(req, c, m):
    """This record's own price rule: the default, its own thresholds, or off.

    Going back to the default deletes the record's own row rather than copying
    the default's numbers into it, so a later change to the default reaches this
    record too. Alerts already raised keep the thresholds they fired on.
    """
    rid = req.fi("id")
    if not rid or not c.execute("SELECT 1 FROM release WHERE id=?", (rid,)).fetchone():
        return redirect("/shelf")
    mode = req.f("mode") or "default"
    if mode not in ("own", "off"):
        c.execute("DELETE FROM alert_rule WHERE kind='price_move' AND release_id=?", (rid,))
        c.commit()
        return flash_to(f"/record?id={rid}", "This record follows the default rule.")
    own = _own_rule(c, rid)
    have = _rule_values(c, own if own else _default_rule(c))
    pct, ab = model.numn(req.f("pct")), model.moneyn(req.f("abs"))
    direction = req.f("direction") or have["direction"]
    if direction not in ("both", "up", "down"):
        direction = "both"
    vals = (max(0.0, pct if pct is not None else have["pct"]),
            ab if ab is not None else have["abs"], direction, 1 if mode == "own" else 0)
    if own:
        c.execute("""UPDATE alert_rule SET pct_threshold=?, abs_threshold=?, direction=?,
                     active=? WHERE id=?""", vals + (own["id"],))
    else:
        c.execute("""INSERT INTO alert_rule(kind,release_id,pct_threshold,abs_threshold,
                     direction,active,note,created_at) VALUES('price_move',?,?,?,?,?,?,?)""",
                  (rid,) + vals + ("This record's own rule.", model.now_iso()))
    c.commit()
    return flash_to(f"/record?id={rid}",
                    "This record has its own rule now. Nothing is re-judged until the next "
                    "check." if mode == "own" else "You will not be told about this record's "
                    "price. It is still looked up and shown.")


def record_fetch(req, c, m):
    """Ask a source for what the record is missing. At most one request to each.

    A MusicBrainz edition with no Discogs link asks MusicBrainz for one; a record
    with a Discogs release but no run-out stamps or notes on file asks Discogs.
    Only blanks are filled. Something already on file is what a source said, and
    a second answer does not replace it — except a date, and only when the new
    one is the same date said more precisely, by the record's own source.
    """
    rid = req.fi("id")
    rel = c.execute("SELECT * FROM release WHERE id=?", (rid,)).fetchone() if rid else None
    if not rel:
        return redirect("/shelf")
    said = []
    if rel["mbid"] and not rel["discogs_release_id"]:
        full, err = sources.mb_release(c, rel["mbid"])
        dg = (full or {}).get("discogs_release_id")
        if err or not full:
            said.append(f"MusicBrainz could not be asked: {err}")
        elif not dg:
            said.append("MusicBrainz lists no Discogs link for this edition. If you know the "
                        "Discogs release number, enter it under Discogs link.")
        elif c.execute("SELECT 1 FROM release WHERE discogs_release_id=?", (dg,)).fetchone():
            said.append(f"MusicBrainz links this edition to Discogs release {dg}, but another "
                        f"record on the shelf is already linked to that one.")
        else:
            c.execute("UPDATE release SET discogs_release_id=? WHERE id=?", (dg, rid))
            c.commit()
            said.append(f"MusicBrainz links this edition to Discogs release {dg}, so its price "
                        f"can be followed now.")
        rel = c.execute("SELECT * FROM release WHERE id=?", (rid,)).fetchone()
    if rel["discogs_release_id"] and (rel["matrix"] is None or rel["source_notes"] is None):
        full, err = sources.discogs_release(c, rel["discogs_release_id"])
        if err or not full:
            said.append(f"Discogs could not be asked: {err}")
        elif full.get("discogs_release_id") == rel["discogs_release_id"]:
            matrix = "\n".join(full.get("matrix") or [])
            notes = full.get("notes_from_source") or ""
            released = (sources.refine_date(rel["released"], full.get("released"))
                        if rel["source"] == "discogs" else rel["released"])
            c.execute("""UPDATE release SET matrix=COALESCE(matrix,?),
                         source_notes=COALESCE(source_notes,?), released=? WHERE id=?""",
                      (matrix, notes, released or None, rid))
            c.commit()
            said.append("Filled in from Discogs." if (matrix or notes) else
                        "Discogs lists no run-out stamps or notes for this release.")
    return flash_to(f"/record?id={rid}", " ".join(said) or "Nothing is missing to ask for.")


def record_edit(req, c, m, err="", vals=None):
    rid = req.gi("id") or req.fi("id")
    rel = c.execute("SELECT * FROM release WHERE id=?", (rid,)).fetchone() if rid else None
    if not rel:
        return missing(req, c, "record")
    back = f'<p><a href="/record?id={rid}">Back to the record</a></p>'
    if rel["source"] != "manual":
        body = f"""<h1>{e(rel["title"])}</h1>
<p>These details are what {e(model.SOURCES.get(rel["source"], rel["source"]))} said about this
edition, and they are the evidence the barcode match and the price rest on, so they are not
edited here. Your own words go in the notes on the record's page.</p>{back}"""
        return shell(req, c, "Details", body, "shelf", "/shelf")
    # Re-drawn with what was typed when the POST refuses it, so one bad digit
    # in the barcode does not cost every other correction on the form.
    src = vals if vals is not None else {k: rel[k] for k in rel.keys()}
    v = lambda k: e(src.get(k) or "")
    err_html = f'<div class="err">{e(err)}</div>' if err else ""
    body = f"""<h1>Correct the details</h1>
<p class="muted">{e(rel["artist_credit"])} — {e(rel["title"])}. Entered by hand, so these are
yours to change.</p>
{err_html}
<form method="post" action="/record/edit" class="card">
  <input type="hidden" name="id" value="{rid}">
  <div class="grid">
    <div><label for="t">Title</label><input id="t" name="title" required value="{v("title")}"></div>
    <div><label for="a">Artist</label><input id="a" name="artist_credit" value="{v("artist_credit")}"></div>
    <div><label for="y">Year</label><input id="y" name="released" value="{v("released")}"
         placeholder="1959"></div>
    <div><label for="co">Country</label><input id="co" name="country" value="{v("country")}"></div>
    <div><label for="lb">Label</label><input id="lb" name="label" value="{v("label")}"></div>
    <div><label for="cn">Catalogue no.</label><input id="cn" name="catno" value="{v("catno")}"></div>
    <div><label for="bcd">Barcode</label><input id="bcd" name="barcode" inputmode="numeric"
         value="{v("barcode")}"></div>
    <div><label for="fm">Format</label><select id="fm" name="format">
      {opts(model.FORMATS.items(), src.get("format"))}</select></div>
    <div><label for="sp">Speed</label><select id="sp" name="speed">
      {opts(model.SPEEDS.items(), src.get("speed"))}</select></div>
  </div>
  <label for="en">Anything that marks this pressing out</label>
  <input id="en" name="edition_note" value="{v("edition_note")}">
  <p class="bar"><button class="btn">Save</button>
  <a class="btn sec" href="/record?id={rid}">Cancel</a></p>
</form>"""
    return shell(req, c, "Correct the details", body, "shelf", "/shelf")


def record_edit_post(req, c, m):
    rid = req.fi("id")
    rel = c.execute("SELECT * FROM release WHERE id=?", (rid,)).fetchone() if rid else None
    if not rel:
        return redirect("/shelf")
    if rel["source"] != "manual":
        return flash_to(f"/record?id={rid}", "Details from a catalogue are not edited here.")
    vals = {k: req.f(k) or "" for k in ("title", "artist_credit", "released", "country",
                                         "label", "catno", "barcode", "format", "speed",
                                         "edition_note")}
    title = model.norm(vals["title"], 300)
    if not title:
        return record_edit(req, c, m, "Nothing was saved. A record needs a title.", vals)
    raw = vals["barcode"]
    code = bc.normalise(raw)
    if raw.strip() and (not code or not bc.check_ok(code)):
        return record_edit(req, c, m, "Nothing was saved. "
                           + (bc.fix_hint(raw) or "That did not read as a barcode."), vals)
    credit = model.norm(req.f("artist_credit") or "", 300)
    aid = (rel["artist_id"] if credit == (rel["artist_credit"] or "")
           else find_or_make_artist(c, credit))
    fmt, speed = req.f("format") or "unknown", req.f("speed") or "unknown"
    c.execute("""UPDATE release SET title=?, artist_credit=?, artist_id=?, released=?, country=?,
                 label=?, catno=?, barcode=?, format=?, speed=?, edition_note=? WHERE id=?""",
              (title, credit, aid, model.partial_date(req.f("released") or ""),
               model.norm(req.f("country") or "", 80) or None,
               model.norm(req.f("label") or "", 160) or None,
               model.norm(req.f("catno") or "", 80) or None, code or None,
               fmt if model.format_ok(fmt) else "unknown",
               speed if speed in model.SPEEDS else "unknown",
               model.norm(req.f("edition_note") or "", 400) or None, rid))
    c.commit()
    return flash_to(f"/record?id={rid}", "Saved.")


def record_new(req, c, m, err="", vals=None):
    """The add-by-hand form. Re-drawn with what was typed and the reason when
    the POST refuses something, so a typo costs one correction, not the form."""
    vals = vals or {}
    v = lambda k: e(vals.get(k) or "")
    err_html = f'<div class="err">{e(err)}</div>' if err else ""
    body = f"""<h1>Add a record by hand</h1>
<p class="muted">For a pressing neither catalogue has heard of. A record with no barcode can
usually be <a href="/search">looked up by name</a> instead, which fills all of this in and
links the edition whose price it follows.</p>
{err_html}
<form method="post" action="/record/new" class="card">
  <div class="grid">
    <div><label for="t">Title</label><input id="t" name="title" required value="{v("title")}"></div>
    <div><label for="a">Artist</label><input id="a" name="artist_credit" value="{v("artist_credit")}"></div>
    <div><label for="y">Year</label><input id="y" name="released" placeholder="1959" value="{v("released")}"></div>
    <div><label for="co">Country</label><input id="co" name="country" value="{v("country")}"></div>
    <div><label for="lb">Label</label><input id="lb" name="label" value="{v("label")}"></div>
    <div><label for="cn">Catalogue no.</label><input id="cn" name="catno" value="{v("catno")}"></div>
    <div><label for="bcd">Barcode, if it has one</label>
      <input id="bcd" name="barcode" inputmode="numeric" value="{v("barcode")}"></div>
    <div><label for="fm">Format</label><select id="fm" name="format">
      {opts(model.FORMATS.items(), vals.get("format") or "LP")}</select></div>
    <div><label for="sp">Speed</label><select id="sp" name="speed">
      {opts(model.SPEEDS.items(), vals.get("speed") or "33")}</select></div>
    <div><label for="dg">Discogs release number, or its address</label>
      <input id="dg" name="discogs_release_id" autocomplete="off"
             value="{v("discogs_release_id")}"></div>
  </div>
  <label for="en">Anything that marks this pressing out</label>
  <input id="en" name="edition_note" placeholder="180g, gatefold, numbered, blue vinyl"
         value="{v("edition_note")}">
  <p class="bar"><button class="btn">Add it</button></p>
</form>"""
    return shell(req, c, "Add a record", body, "shelf", "/shelf")


def record_new_post(req, c, m):
    fields = ("title", "artist_credit", "released", "country", "label", "catno", "barcode",
              "format", "speed", "discogs_release_id", "edition_note")
    vals = {k: req.f(k) or "" for k in fields}
    title = model.norm(vals["title"], 300)
    if not title:
        return record_new(req, c, m, "A record needs at least a title.", vals)
    code = ""
    if vals["barcode"].strip():
        code = bc.normalise(vals["barcode"])
        if not code or not bc.check_ok(code):
            # Refused out loud rather than dropped: a barcode typed by hand and
            # silently discarded is a record that can never be found by scanning.
            return record_new(req, c, m, bc.fix_hint(vals["barcode"])
                              or "That did not read as a barcode.", vals)
    dg, problem = model.discogs_release_ref(vals["discogs_release_id"])
    if problem:
        return record_new(req, c, m, problem, vals)
    cand = {
        "title": title, "artist_credit": vals["artist_credit"],
        "released": model.partial_date(vals["released"]) or "",
        "country": vals["country"], "label": vals["label"],
        "catno": vals["catno"], "format": vals["format"] or "unknown",
        "speed": vals["speed"] or "unknown", "edition_note": vals["edition_note"],
        "discogs_release_id": dg, "source": "manual",
    }
    rid = release_from_candidate(c, cand, code, "manual")
    cur = c.execute("INSERT INTO copy(release_id,added_at) VALUES(?,?)", (rid, model.now_iso()))
    c.commit()
    return flash_to(f"/grade?copy={cur.lastrowid}", "Added. Now: what condition is it in?")


# --------------------------------------------------------------------- copies

def copy_new(req, c, m):
    rid = req.gi("release")
    rel = c.execute("SELECT * FROM release WHERE id=?", (rid,)).fetchone() if rid else None
    if not rel:
        return redirect("/shelf")
    body = f"""<h1>Add a copy</h1>
<p class="muted">{e(rel["artist_credit"])} — {e(rel["title"])}</p>
<form method="post" action="/copy/new" class="card">
  <input type="hidden" name="release" value="{rid}">
  <div class="grid">
    <div><label for="on">Acquired on</label><input id="on" name="acquired_on" type="date"></div>
    <div><label for="fr">From</label><input id="fr" name="acquired_from"
         placeholder="a shop, a fair, a friend"></div>
    <div><label for="pp">What you paid</label><input id="pp" name="paid" inputmode="decimal"></div>
    <div><label for="pc">Currency</label><select id="pc" name="currency">
      {opts(model.currency_choices(db.get(c, "currency", "GBP")), db.get(c, "currency", "GBP"))}</select></div>
    <div><label for="lo">Where it lives</label><input id="lo" name="location"
         placeholder="shelf B, crate 2"></div>
  </div>
  <label for="nt">Notes</label><textarea id="nt" name="notes" rows="2"></textarea>
  <p class="bar"><button class="btn">Add the copy</button></p>
</form>"""
    return shell(req, c, "Add a copy", body, "shelf")


def copy_new_post(req, c, m):
    rid = req.fi("release")
    if not c.execute("SELECT 1 FROM release WHERE id=?", (rid,)).fetchone():
        return redirect("/shelf")
    cur = c.execute("""INSERT INTO copy(release_id,acquired_on,acquired_from,location,notes,
                       added_at) VALUES(?,?,?,?,?,?)""",
                    (rid, model.datestr(req.f("acquired_on")),
                     model.norm(req.f("acquired_from") or "", 200) or None,
                     model.norm(req.f("location") or "", 120) or None,
                     (req.f("notes") or "").strip() or None, model.now_iso()))
    cid = cur.lastrowid
    paid = model.moneyn(req.f("paid"))
    if paid is not None:
        cur_code = req.f("currency") or db.get(c, "currency", "GBP")
        prices.record(c, rid, {"source": "manual", "feed": "hand", "kind": "paid",
                               "currency": cur_code if model.currency_ok(cur_code) else "GBP",
                               "amount": paid, "note": "what you paid for this copy"},
                      copy_id=cid,
                      observed_at=(model.datestr(req.f("acquired_on")) or model.today_iso())
                      + "T12:00:00+00:00")
    c.commit()
    return flash_to(f"/grade?copy={cid}", "Copy added. Now: what condition is it in?")


def copy_page(req, c, m):
    cid = req.gi("id")
    cp = library.copy_detail(c, cid) if cid else None
    if not cp:
        return missing(req, c, "copy")
    rel = cp["release"]
    gv = cp["grade"]
    cur = db.get(c, "currency", "GBP")
    hist = c.execute("SELECT * FROM grading WHERE copy_id=? ORDER BY id DESC", (cid,)).fetchall()
    rows = "".join(
        f'<tr><td>{e(r["graded_on"] or (r["graded_at"] or "")[:10])}</td>'
        f'<td>{e(r["media_grade"] or "—")}</td><td>{e(r["sleeve_grade"] or "—")}</td>'
        f'<td class="small">{e(r["note"] or "")}</td></tr>' for r in hist)
    hist_html = (f'<div class="scroll"><table><thead><tr><th>On</th><th>Record</th>'
                 f'<th>Sleeve</th><th>Note</th></tr></thead><tbody>{rows}</tbody></table></div>'
                 if rows else '<p class="muted">Never graded.</p>')
    gone = ""
    if cp["disposed_on"]:
        gone = (f'<div class="note">Gone on {e(cp["disposed_on"])}'
                f'{" to " + e(cp["disposed_to"]) if cp["disposed_to"] else ""}. '
                f'{e(cp["disposed_note"] or "")}</div>')
    facts = []
    for lab, val in (("Acquired on", cp["acquired_on"]), ("From", cp["acquired_from"]),
                     ("Where it lives", cp["location"]),
                     ("Paid", money(cp["paid"]["amount"], cp["paid"]["currency"])
                      if cp["paid"] else ""),
                     ("Sold for", money(cp["sold"]["amount"], cp["sold"]["currency"])
                      if cp["sold"] else "")):
        if val:
            facts.append(f"<dt>{e(lab)}</dt><dd>{e(val)}</dd>")
    notes_html = (f'<div class="quote">{e(cp["notes"])}</div>' if cp["notes"] else "")
    facts_html = (f'<dl class="kv">{"".join(facts)}</dl>' if facts else
                  '<p class="muted small">Nothing recorded about where it came from.</p>')
    paid, sold = cp["paid"], cp["sold"]
    body = f"""<h1>One copy</h1>
<p class="muted"><a href="/record?id={rel["id"]}">{e(rel["artist_credit"])} — {e(rel["title"])}</a></p>
{gone}
<div class="card"><p>{_grade_pill(gv)}</p>
<p class="muted small">{e(gv["note"] or "")}</p>
<p class="bar"><a class="btn" href="/grade?copy={cid}">Grade it again</a></p></div>
<div class="card"><h3>This copy</h3>{facts_html}{notes_html}</div>
<h2>Condition over time</h2>{hist_html}
<form method="post" action="/copy/edit" class="card">
  <input type="hidden" name="id" value="{cid}">
  <h3>Where it came from</h3>
  <div class="grid">
    <div><label for="e1">Acquired on</label><input id="e1" name="acquired_on" type="date"
         value="{e(cp["acquired_on"] or "")}"></div>
    <div><label for="e2">From</label><input id="e2" name="acquired_from"
         value="{e(cp["acquired_from"] or "")}" placeholder="a shop, a fair, a friend"></div>
    <div><label for="e3">What you paid</label><input id="e3" name="paid" inputmode="decimal"
         value="{f'{paid["amount"]:.2f}' if paid else ""}"></div>
    <div><label for="e4">Currency</label><select id="e4" name="currency">
      {opts(model.currency_choices(paid["currency"] if paid else cur), paid["currency"] if paid else cur)}</select></div>
    <div><label for="e5">Where it lives</label><input id="e5" name="location"
         value="{e(cp["location"] or "")}" placeholder="shelf B, crate 2"></div>
  </div>
  <label for="e6">Notes</label><textarea id="e6" name="notes" rows="3">{e(cp["notes"] or "")}</textarea>
  <p class="bar"><button class="btn sec">Save</button></p>
</form>
<form method="post" action="/copy/dispose" class="card">
  <input type="hidden" name="id" value="{cid}">
  <h3>Sold or otherwise gone</h3>
  <div class="grid">
    <div><label for="d1">On</label><input id="d1" name="disposed_on" type="date"
         value="{e(cp["disposed_on"] or "")}"></div>
    <div><label for="d2">To</label><input id="d2" name="disposed_to"
         value="{e(cp["disposed_to"] or "")}"></div>
    <div><label for="d3">For</label><input id="d3" name="sold" inputmode="decimal"
         value="{f'{sold["amount"]:.2f}' if sold else ""}"></div>
    <div><label for="d4">Currency</label><select id="d4" name="currency">
      {opts(model.currency_choices(sold["currency"] if sold else cur), sold["currency"] if sold else cur)}</select></div>
  </div>
  <label for="d5">Note</label><input id="d5" name="disposed_note"
    value="{e(cp["disposed_note"] or "")}">
  <p class="muted small">Leave the date empty to put it back on the shelf.</p>
  <p class="bar"><button class="btn sec">Save</button></p>
</form>
<form method="post" action="/copy/delete" onsubmit="return confirm('Delete this copy and its grades?')">
  <input type="hidden" name="id" value="{cid}">
  <p><button class="btn sec sm">Delete this copy</button></p></form>"""
    return shell(req, c, "Copy", body, "shelf")


def copy_dispose(req, c, m):
    cid = req.fi("id")
    cp = c.execute("SELECT * FROM copy WHERE id=?", (cid,)).fetchone() if cid else None
    if not cp:
        return redirect("/shelf")
    on = model.datestr(req.f("disposed_on"))
    c.execute("""UPDATE copy SET disposed_on=?, disposed_to=?, disposed_note=? WHERE id=?""",
              (on, model.norm(req.f("disposed_to") or "", 200) or None,
               (req.f("disposed_note") or "").strip() or None, cid))
    if on:
        _copy_price_set(c, cp, "sold", model.moneyn(req.f("sold")), req.f("currency"),
                        on, "what this copy sold for")
    else:
        # Back on the shelf: whatever it was sold for is no longer true of it.
        c.execute("DELETE FROM price_obs WHERE copy_id=? AND kind='sold'", (cid,))
    c.commit()
    return flash_to(f"/copy?id={cid}", "Saved." if on else "Back on the shelf.")


def _copy_price_set(c, cp, kind, amount, currency, on, note):
    """Record what one copy cost or fetched, or correct it. One per copy per kind.

    These are your own receipts, so a second entry corrects the first rather
    than sitting beside it as a second purchase of the same object. A blank
    amount leaves what is there alone.
    """
    if amount is None:
        return
    cur_code = currency if model.currency_ok(currency or "") else db.get(c, "currency", "GBP")
    at = f"{on}T12:00:00+00:00" if on else None
    have = c.execute("SELECT id FROM price_obs WHERE copy_id=? AND kind=? ORDER BY id LIMIT 1",
                     (cp["id"], kind)).fetchone()
    if have:
        c.execute("""UPDATE price_obs SET amount=?, currency=?,
                     observed_at=COALESCE(?, observed_at) WHERE id=?""",
                  (amount, cur_code, at, have["id"]))
    else:
        prices.record(c, cp["release_id"], {"source": "manual", "feed": "hand", "kind": kind,
                                            "currency": cur_code, "amount": amount, "note": note},
                      copy_id=cp["id"],
                      observed_at=at or f"{model.today_iso()}T12:00:00+00:00")


def copy_edit(req, c, m):
    cid = req.fi("id")
    cp = c.execute("SELECT * FROM copy WHERE id=?", (cid,)).fetchone() if cid else None
    if not cp:
        return redirect("/shelf")
    on = model.datestr(req.f("acquired_on"))
    c.execute("""UPDATE copy SET acquired_on=?, acquired_from=?, location=?, notes=?
                 WHERE id=?""",
              (on, model.norm(req.f("acquired_from") or "", 200) or None,
               model.norm(req.f("location") or "", 120) or None,
               (req.f("notes") or "").strip() or None, cid))
    _copy_price_set(c, cp, "paid", model.moneyn(req.f("paid")), req.f("currency"), on,
                    "what you paid for this copy")
    c.commit()
    return flash_to(f"/copy?id={cid}", "Saved.")


def copy_delete(req, c, m):
    cid = req.fi("id")
    cp = c.execute("SELECT release_id FROM copy WHERE id=?", (cid,)).fetchone()
    rid = cp["release_id"] if cp else None
    c.execute("DELETE FROM copy WHERE id=?", (cid,))
    c.commit()
    return flash_to(f"/record?id={rid}" if rid else "/shelf", "Copy deleted.")


# ---------------------------------------------------------------- the shelf

def _shelf_filters(req):
    """The shelf's filters from a query string, for the page and its CSV alike."""
    return dict(q=model.norm(req.g("q") or "", 120), artist_id=req.gi("artist"),
                fmt=req.g("fmt") or "", owned=req.g("owned") or "",
                sort=req.g("sort") or "artist", tracked=req.g("tracked") or "",
                linked=req.g("linked") or "")


def shelf_page(req, c, m):
    f = _shelf_filters(req)
    q, fmt, owned, sort = f["q"], f["fmt"], f["owned"], f["sort"]
    rows = library.shelf(c, **f)
    dist = library.grade_distribution(c)
    # Two filters with no control of their own: the Prices page links here with
    # them set, so the page says what it is showing and keeps them on the form.
    narrowed = [w for v, w in ((f["tracked"] == "1", "followed for price"),
                               (f["tracked"] == "0", "not followed for price"),
                               (f["linked"] == "no", "with no Discogs release linked"),
                               (f["linked"] == "yes", "linked to a Discogs release")) if v]
    if f["artist_id"]:
        who = c.execute("SELECT name FROM artist WHERE id=?", (f["artist_id"],)).fetchone()
        narrowed.insert(0, f'by {who["name"]}' if who else "by one artist")
    narrow_html = (f'<p class="note small">Showing only records {e(" and ".join(narrowed))}. '
                   f'<a href="/shelf">Show everything</a></p>' if narrowed else "")
    keep = "".join(f'<input type="hidden" name="{k}" value="{e(f[k])}">'
                   for k in ("tracked", "linked") if f[k])
    if f["artist_id"]:
        keep += f'<input type="hidden" name="artist" value="{f["artist_id"]}">'
    csv_q = urllib.parse.urlencode({k: v for k, v in (
        ("q", q), ("fmt", fmt), ("owned", owned), ("sort", sort), ("tracked", f["tracked"]),
        ("linked", f["linked"]), ("artist", f["artist_id"] or "")) if v})

    body_rows = []
    for r in rows:
        gv = r["copies"][0]["grade"] if r["copies"] else library.grade_view(None)
        grades = ""
        if r["copies"]:
            parts = []
            for cp in r["copies"]:
                g = cp["grade"]
                mm = g["media"] or "—"
                ss = g["sleeve"] or "—"
                parts.append(f'<span class="pill{"" if cp["owned"] else " warn"}">'
                             f'{mm}/{ss}</span>')
            grades = " ".join(parts)
        price = ""
        if r["price"] and r["series"]:
            price = e(money(r["price"]["amount"], r["series"]["currency"]))
        cls = "" if r["owned_n"] else ' class="gone"'
        body_rows.append(f"""<tr{cls}>
<td><a href="/record?id={r["id"]}">{e(r["title"])}</a>
  <div class="muted small">{e(r["artist_credit"])}</div></td>
<td class="small">{e(r["released"] or "")}</td>
<td class="small">{e(r["label"] or "")}<div class="muted small">{e(r["catno"] or "")}</div></td>
<td class="small">{e(model.FORMATS.get(r["format"], ""))}</td>
<td>{grades}</td>
<td class="num">{price}</td></tr>""")

    dist_bits = []
    for g in model.GRADES:
        dist_bits.append(f'{g}: {dist["media"][g]}')
    dist_line = (f'Records: {" · ".join(dist_bits)}'
                 f' · not graded: {dist["media_ungraded"] + dist["no_grading"]}')
    sleeve_line = (" · ".join(f'{g}: {dist["sleeve"][g]}' for g in model.GRADES)
                   + f' · not graded: {dist["sleeve_ungraded"] + dist["no_grading"]}')

    filtered = any(f[k] for k in ("q", "fmt", "owned", "tracked", "linked", "artist_id"))
    empty = ('Nothing matches these filters. <a href="/shelf">Show everything</a>.'
             if filtered else
             'Nothing on the shelf yet. <a href="/scan">Scan a record</a>, or '
             '<a href="/record/new">add one by hand</a>.')
    body = f"""<h1>The shelf</h1>
{narrow_html}
<form method="get" action="/shelf" class="bar noprint">
  {keep}
  <input name="q" value="{e(q)}" placeholder="title, artist, catalogue number, barcode"
         style="min-width:220px">
  <select name="fmt">{opts(model.FORMATS.items(), fmt, "any format")}</select>
  <select name="owned">{opts([("yes", "on the shelf"), ("no", "not owned")], owned, "owned or not")}</select>
  <select name="sort">{opts([("artist", "by artist"), ("title", "by title"), ("added", "newest first"), ("year", "by year"), ("label", "by label")], sort)}</select>
  <button class="btn sec sm">Show</button>
  <a class="btn sec sm" href="/shelf">Clear</a>
  <span class="right"><a class="btn sec sm" href="/shelf.csv?{e(csv_q)}">CSV</a></span>
</form>
<div class="scroll"><table>
<thead><tr><th>Record</th><th>Year</th><th>Label</th><th>Format</th>
<th>Grades (record/sleeve)</th><th class="num">Lowest asking</th></tr></thead>
<tbody>{"".join(body_rows) or f'<tr><td colspan="6" class="muted">{empty}</td></tr>'}</tbody>
</table></div>
<footer class="count">{len(rows)} record{"" if len(rows) == 1 else "s"} ·
grades on {dist["n"]} owned copies — {e(dist_line)}; sleeves — {e(sleeve_line)}.
<br>Grades are counted, never averaged: a 4 is better than a 3, but it is not "one better",
so a mean of them would be a number with no meaning.</footer>"""
    return shell(req, c, "The shelf", body, "shelf", "/shelf")


def shelf_csv(req, c, m):
    rows = library.shelf(c, **_shelf_filters(req))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["artist", "title", "year", "country", "label", "catno", "format", "barcode",
                "copies_owned", "record_grade", "sleeve_grade", "grade_note", "graded_on",
                "price", "price_currency", "price_kind", "price_seen"])
    for r in rows:
        first = r["copies"][0] if r["copies"] else None
        g = first["grade"] if first else library.grade_view(None)
        ser, p = r["series"], r["price"]
        w.writerow([_sk(r["artist_credit"]), _sk(r["title"]), r["released"] or "",
                    r["country"] or "", r["label"] or "", _sk(r["catno"] or ""),
                    r["format"], _sk(r["barcode"] or ""), r["owned_n"],
                    g["media"] or (g["media_why"] or ""), g["sleeve"] or (g["sleeve_why"] or ""),
                    _sk(g["note"]), g["on"],
                    p["amount"] if p else "", ser["currency"] if ser else "",
                    ser["kind"] if ser else "", p["observed_at"] if p else ""])
    return (200, [("Content-Type", "text/csv; charset=utf-8"),
                  ("Content-Disposition", 'attachment; filename="shelf.csv"')],
            buf.getvalue().encode("utf-8"))


def _sk(s):
    """Neutralise a leading =, +, - or @ so a CSV cell cannot become a formula
    when the file is opened in a spreadsheet."""
    t = "" if s is None else str(s)
    return " " + t if t[:1] in ("=", "+", "-", "@") else t


# --------------------------------------------------------------------- prices

def prices_page(req, c, m):
    due = library.due_for_price(c, db.get_int(c, "price_max_age_hours", 20))
    unlinked = library.unlinked_tracked(c, limit=100000)
    unfollowed = library.untracked_with_copies(c, limit=100000)
    tot = prices.collection_totals(c)
    rows = []
    for r in c.execute("""SELECT * FROM release WHERE tracked=1
                          ORDER BY LOWER(artist_credit), LOWER(title)""").fetchall():
        ss = prices.series_list(c, r["id"])
        if not ss:
            rows.append((r, None, None))
            continue
        rows.append((r, ss[0], prices.assess(c, r["id"], ss[0])))
    cur = db.get(c, "currency", "GBP")
    behind = sum(1 for r, key, a in rows
                 if key and key["feed"] == "discogs_stats" and key["currency"] != cur)
    behind_html = (f'<p class="note small">{behind} record{"" if behind == 1 else "s"} still '
                   f'show{"s" if behind == 1 else ""} the last price Discogs gave in another '
                   f'currency, from before prices were asked for in {e(cur)}. Nothing is '
                   f'converted; checking them again asks Discogs for {e(cur)}.</p>'
                   if behind else "")
    trs = []
    for r, key, a in rows:
        if not key:
            trs.append(f'<tr><td><a href="/record?id={r["id"]}">{e(r["title"])}</a>'
                       f'<div class="muted small">{e(r["artist_credit"])}</div></td>'
                       f'<td class="muted small" colspan="3">nothing recorded yet</td></tr>')
            continue
        line = prices.describe(a)
        trs.append(f"""<tr><td><a href="/record?id={r["id"]}">{e(r["title"])}</a>
<div class="muted small">{e(r["artist_credit"])}</div></td>
<td class="num">{e(money(a["latest"], key["currency"]))}</td>
<td class="move">{e(line)}</td>
<td class="small muted">{e(model.ago(a["latest_at"]))}</td></tr>""")
    money_bits = "".join(
        f'<li>{e(money(g["total"], g["currency"]))} across {g["n"]} '
        f'record{"" if g["n"] == 1 else "s"}</li>' for g in tot["groups"])
    body = f"""<h1>Prices</h1>
<div class="bar noprint">
  <form method="post" action="/prices/check" style="margin:0">
    <button class="btn">Check everything that is due ({len(due)})</button></form>
  <a class="btn sec" href="/help">What these numbers are</a>
</div>
{_currency_switch(c, "/prices")}
<p class="muted small">One currency for everyone using this collection. It is what Discogs is
asked for; a price already recorded stays in the currency it was quoted in.</p>
{behind_html}
{_schedule_note(c)}
<div class="card"><h3>Added up</h3><ul>{money_bits or "<li>Nothing priced yet.</li>"}</ul>
<p class="muted small">The sum of the cheapest copy currently for sale, for each record, in
whatever condition those copies are in. Not a valuation. Currencies are never added together.
{e(f"{tot['unpriced']} of {tot['records']} records have no price.") if tot['records'] else ""}</p></div>
{f'<div class="note">{len(unlinked)} followed record(s) have no Discogs release linked, so nothing can be looked up for them. <a href="/shelf?tracked=1&amp;linked=no">Have a look</a>.</div>' if unlinked else ""}
{f'<p class="muted small">{len(unfollowed)} record(s) you own are not followed for price, by your choice. <a href="/shelf?tracked=0&amp;owned=yes">See which</a>.</p>' if unfollowed else ""}
<div class="scroll"><table><thead><tr><th>Record</th><th class="num">Latest</th>
<th>Against the last {db.get_int(c, "move_window_days", 30)} days</th><th>Seen</th></tr></thead>
<tbody>{"".join(trs) or '<tr><td colspan="4" class="muted">Nothing followed yet. Every record you add is followed for price until you say otherwise — <a href="/scan">scan one</a> to start.</td></tr>'}</tbody>
</table></div>"""
    return shell(req, c, "Prices", body, "prices", "/prices")


def price_check(req, c, m):
    rid = req.fi("id")
    if rid:
        rel = c.execute("SELECT * FROM release WHERE id=?", (rid,)).fetchone()
        if not rel:
            return redirect("/prices")
        done, msg = _check_one(c, rel)
        raised = alerts_mod.evaluate_prices(c, release_id=rid)
        if raised:
            msg += f" That is a move worth telling you about — see Alerts."
        c.commit()
        return flash_to(f"/record?id={rid}", msg)
    due = library.due_for_price(c, db.get_int(c, "price_max_age_hours", 20), limit=25)
    got, problems = 0, []
    for rel in due:
        ok, msg = _check_one(c, rel)
        if ok:
            got += 1
        else:
            problems.append(msg)
            if "slow down" in msg or "Offline" in msg:
                break
    raised = alerts_mod.evaluate_prices(c)
    c.commit()
    msg = f"Looked up {got} of {len(due)} due."
    if raised:
        msg += f" {len(raised)} worth telling you about."
    if problems:
        msg += " " + problems[0]
    return flash_to("/prices", msg)


def _check_one(c, rel):
    """One price lookup, recorded. (ok, sentence)."""
    obs, err = sources.price_now(c, dict(rel))
    if err or not obs:
        return False, err or "Nothing came back."
    stored = prices.record(c, rel["id"], obs)
    c.execute("UPDATE release SET last_priced_at=? WHERE id=?", (model.now_iso(), rel["id"]))
    c.commit()
    n = obs.get("num_for_sale")
    across = " across {} listings".format(n) if n else ""
    extra = " " + obs["note"] if obs.get("note") else ""
    if stored is None:
        # The series key plus the timestamp is unique, so asking twice inside one
        # second stores nothing. Saying "recorded" here would be a small lie that
        # makes a missing observation look like a present one.
        extra += " (already recorded a moment ago — nothing added)"
    return True, "Lowest asking price {}{}.{}".format(
        model.money(obs["amount"], obs["currency"]), across, extra)


def price_add(req, c, m):
    rid = req.fi("id")
    if not c.execute("SELECT 1 FROM release WHERE id=?", (rid,)).fetchone():
        return redirect("/shelf")
    amount = model.moneyn(req.f("amount"))
    if amount is None:
        return flash_to(f"/record?id={rid}", "That amount did not read as a price.")
    kind = req.f("kind") or "sale_seen"
    if kind in model.COPY_KINDS:
        return flash_to(f"/record?id={rid}", "What you paid for a copy, or sold it for, is "
                        "recorded on that copy's own page.")
    if kind not in model.PRICE_KINDS:
        kind = "sale_seen"
    cur_code = req.f("currency") or db.get(c, "currency", "GBP")
    on = model.datestr(req.f("on")) or model.today_iso()
    prices.record(c, rid, {
        "source": "manual", "feed": "hand", "kind": kind,
        "currency": cur_code if model.currency_ok(cur_code) else "GBP", "amount": amount,
        "cond_band": model.intn(req.f("cond_band")),
        "note": model.norm(req.f("note") or "", 300)},
        observed_at=on + "T12:00:00+00:00")
    c.commit()
    return flash_to(f"/record?id={rid}", "Recorded.")


# --------------------------------------------------------------------- alerts

def alerts_page(req, c, m):
    rows = alerts_mod.open_alerts(c, 200)
    cards = []
    for a in rows:
        detail = "".join(f"<li>{e(s)}</li>" for s in alerts_mod.detail_lines(c, a))
        where = (f'<a href="/record?id={a["release_id"]}">the record</a>' if a["release_id"]
                 else (f'<a href="/artist?id={a["artist_id"]}">the artist</a>'
                       if a["artist_id"] else ""))
        link = (f'<a class="btn sec sm" href="{e(a["item_url"])}" target="_blank"'
                f' rel="noreferrer noopener">Look it up</a>' if a["item_url"] else "")
        new = "" if a["seen_at"] else pill("new", "q")
        cards.append(f"""<div class="card">
  <p><strong>{e(alerts_mod.headline(c, a))}</strong> {new}</p>
  <ul class="small muted">{detail}</ul>
  <p class="bar"><span class="muted small">{e(model.ago(a["raised_at"]))}</span>
    {where} {link}
    <span class="right"><form method="post" action="/alerts/dismiss" style="margin:0">
      <input type="hidden" name="id" value="{a["id"]}">
      <button class="btn sec sm">Dismiss</button></form></span></p>
</div>""")
    chans = alerts_mod.channels_ready(c)
    chan_html = (e(f"Alerts also go out by {', '.join(chans)}.") if chans else
                 'Alerts appear here only. Set up email or a webhook in '
                 '<a href="/settings">Settings</a> if you want them to reach you elsewhere.')
    # A test with nowhere to send it can only say so; the way forward is Settings.
    test_html = ('<form method="post" action="/alerts/test" style="margin:0">'
                 '<button class="btn sec sm">Send a test notification</button></form>'
                 if chans else
                 '<a class="btn sec sm" href="/settings">Set up where alerts go</a>')
    body = f"""<h1>Alerts</h1>
<p class="muted">{chan_html}</p>
<div class="bar noprint">
  <form method="post" action="/alerts/seen" style="margin:0">
    <button class="btn sec sm">Mark all as read</button></form>
  {test_html}
  <a class="btn sec sm" href="/rules">When to tell me</a>
</div>
{_schedule_note(c)}
{"".join(cards) or '<p class="muted">Nothing to report. That is the usual state of things.</p>'}"""
    out = shell(req, c, "Alerts", body, "prices", "/alerts")
    alerts_mod.mark_seen(c, [a["id"] for a in rows])
    c.commit()
    return out


def alerts_seen(req, c, m):
    alerts_mod.mark_seen(c, [r["id"] for r in alerts_mod.open_alerts(c, 500)])
    c.commit()
    return redirect("/alerts")


def alerts_dismiss(req, c, m):
    alerts_mod.dismiss(c, req.fi("id"))
    c.commit()
    return redirect("/alerts")


def alerts_test(req, c, m):
    chans = alerts_mod.channels_ready(c)
    if not chans:
        return flash_to("/settings", "Nothing is set up to send to yet. Fill in a mail server "
                                     "or a webhook below, save, and try the test again.")
    cur = c.execute("""INSERT INTO alert(kind,raised_at,move_kind,item_title,item_source)
                       VALUES('new_release',?,'newly_listed',?,'manual')""",
                    (model.now_iso(), "A test from Sleevenote"))
    c.commit()
    a = c.execute("SELECT * FROM alert WHERE id=?", (cur.lastrowid,)).fetchone()
    res = alerts_mod.deliver(c, a, chans)
    alerts_mod.dismiss(c, a["id"])
    c.commit()
    bits = "; ".join(f"{ch}: {'sent' if ok else 'failed — ' + d}" for ch, ok, d in res)
    return flash_to("/alerts", bits or "Nothing was sent.")


def rules_page(req, c, m):
    default = c.execute("""SELECT * FROM alert_rule WHERE kind='price_move'
                           AND release_id IS NULL AND artist_id IS NULL
                           ORDER BY id DESC LIMIT 1""").fetchone()
    overrides = c.execute("""SELECT ar.*, r.title, r.artist_credit FROM alert_rule ar
                             JOIN release r ON r.id=ar.release_id
                             WHERE ar.kind='price_move' ORDER BY r.artist_credit""").fetchall()
    cur = db.get(c, "currency", "GBP")
    rows = "".join(
        f'<tr><td><a href="/record?id={r["release_id"]}">{e(r["title"])}</a>'
        f'<div class="muted small">{e(r["artist_credit"])}</div></td>'
        + (f'<td class="num">{_rule_values(c, r)["pct"]:g}%</td>'
           f'<td class="num">{e(money(_rule_values(c, r)["abs"], cur))}</td>'
           f'<td>{e(r["direction"])}</td><td>on</td></tr>' if r["active"] else
           '<td class="muted small" colspan="3">never told about this one</td><td>off</td></tr>')
        for r in overrides)
    over_html = (f'<h2>Records with their own rule</h2><div class="scroll"><table><thead><tr>'
                 f'<th>Record</th><th class="num">At least</th><th class="num">And at least</th>'
                 f'<th>Direction</th><th></th></tr></thead><tbody>{rows}</tbody></table></div>'
                 f'<p class="muted small">Set on each record\'s own page, under Price.</p>'
                 if rows else
                 '<p class="muted small">Any record can have its own thresholds, or be left '
                 'out of alerts altogether — on its own page, under Price.</p>')
    d = default
    body = f"""<h1>When to tell me</h1>
<p class="muted">A move has to clear <em>both</em> thresholds. One alone is no use: a
percentage on its own shouts about a £4 single going to £5, and an amount on its own says
nothing when a £400 box set moves £20.</p>
<form method="post" action="/rules" class="card">
  <h3>The default, for every followed record</h3>
  <div class="grid">
    <div><label for="p">Moved at least</label>
      <input id="p" name="pct" inputmode="decimal" value="{d["pct_threshold"] if d else 20:g}"> %</div>
    <div><label for="ab">And at least</label>
      <input id="ab" name="abs" inputmode="decimal" value="{d["abs_threshold"] if d else 3:.2f}">
      <span class="muted small">{e(cur)}</span></div>
    <div><label for="w">Compared with the last</label>
      <input id="w" name="window" inputmode="numeric" value="{d["window_days"] if d else 30}"> days</div>
    <div><label for="mo">Needing at least</label>
      <input id="mo" name="min_obs" inputmode="numeric" value="{d["min_obs"] if d else 3}">
      observations</div>
    <div><label for="cd">Then quiet for</label>
      <input id="cd" name="cooldown" inputmode="numeric" value="{d["cooldown_days"] if d else 14}"> days</div>
    <div><label for="dir">Which way</label><select id="dir" name="direction">
      {opts([("both", "up or down"), ("up", "only up"), ("down", "only down")], d["direction"] if d else "both")}
      </select></div>
  </div>
  <p class="bar"><button class="btn">Save</button></p>
</form>
<div class="card"><h3>What the comparison actually is</h3>
<p class="small">The newest price is compared with the <strong>median</strong> of the
observations before it, inside the window. Not with yesterday's price: a lowest asking price
is one seller's number, and it jumps when that seller sells or relists, which is not the
market moving. The median ignores one seller arriving or leaving; a single previous
observation cannot.</p>
<p class="small">Below the minimum number of observations, nothing is reported at all and the
record's page says so. The amount above is read in whatever currency the prices are in —
nothing in this app converts a currency.</p></div>
{over_html}"""
    return shell(req, c, "When to tell me", body, "prices", "/rules")


def rules_save(req, c, m):
    d = c.execute("""SELECT * FROM alert_rule WHERE kind='price_move' AND release_id IS NULL
                     AND artist_id IS NULL ORDER BY id DESC LIMIT 1""").fetchone()
    pct = model.numn(req.f("pct"))
    ab = model.moneyn(req.f("abs"))
    win = model.intn(req.f("window"))
    mo = model.intn(req.f("min_obs"))
    cd = model.intn(req.f("cooldown"))
    direction = req.f("direction") or "both"
    if direction not in ("both", "up", "down"):
        direction = "both"
    vals = (max(0.0, pct if pct is not None else 20.0),
            ab if ab is not None else 3.0,
            max(2, mo if mo else 3), max(1, win if win else 30),
            max(0, cd if cd is not None else 14), direction)
    if d:
        c.execute("""UPDATE alert_rule SET pct_threshold=?, abs_threshold=?, min_obs=?,
                     window_days=?, cooldown_days=?, direction=? WHERE id=?""", vals + (d["id"],))
    else:
        c.execute("""INSERT INTO alert_rule(kind,direction,pct_threshold,abs_threshold,min_obs,
                     window_days,cooldown_days,active,created_at)
                     VALUES('price_move',?,?,?,?,?,?,1,?)""",
                  (direction, vals[0], vals[1], vals[2], vals[3], vals[4], model.now_iso()))
    for k, v in (("move_pct", vals[0]), ("move_abs", vals[1]), ("move_min_obs", vals[2]),
                 ("move_window_days", vals[3]), ("move_cooldown_days", vals[4])):
        db.put(c, k, str(v))
    c.commit()
    return flash_to("/rules", "Saved. Nothing is re-judged until the next check.")


# -------------------------------------------------------------------- artists

def artists_page(req, c, m):
    rows = library.artists(c)
    trs = []
    for a in rows:
        watch = pill("watched", "ok") if a["watch"] else pill("not watched")
        base = ("never swept" if not a["baseline_at"] else
                f'baseline {a["baseline_at"][:10]}, {a["known"]} works known')
        link = ("" if a["mbid"] else
                f'<a href="/artist?id={a["id"]}&amp;find={urllib.parse.quote(a["name"])}">'
                f'{pill("not linked — link to watch", "warn")}</a>')
        trs.append(f"""<tr><td><a href="/artist?id={a["id"]}">{e(a["name"])}</a> {link}</td>
<td class="num">{a["releases"]}</td><td class="num">{a["copies"]}</td>
<td>{watch}</td><td class="small muted">{e(base)}</td>
<td class="small muted">{e(model.ago(a["last_checked_at"]) if a["last_checked_at"] else "")}</td></tr>""")
    body = f"""<h1>Artists</h1>
<p class="muted">Watch an artist and this will tell you when something of theirs appears that
was not there before — and whether there is a vinyl pressing of it.</p>
<div class="bar noprint">
  <form method="post" action="/artist/sweep" style="margin:0">
    <button class="btn">Check every watched artist</button></form>
  <form method="post" action="/artist/new" class="bar" style="margin:0">
    <input name="name" placeholder="someone you own nothing by yet" required
           aria-label="An artist to watch">
    <button class="btn sec">Add and find on MusicBrainz</button></form>
</div>
{_schedule_note(c)}
<div class="scroll"><table><thead><tr><th>Artist</th><th class="num">Records</th>
<th class="num">Copies</th><th>Watch</th><th>Catalogue</th><th>Last checked</th></tr></thead>
<tbody>{"".join(trs) or '<tr><td colspan="6" class="muted">No artists yet. An artist appears here when you add one of their records, or when you add them by name above.</td></tr>'}</tbody>
</table></div>
<p class="muted small">The first sweep of an artist records what they already have out and
announces nothing. Without that, watching someone with a long career would announce their
entire back catalogue at once.</p>"""
    return shell(req, c, "Artists", body, "shelf", "/artists")


def artist_page(req, c, m):
    aid = req.gi("id")
    d = library.artist_detail(c, aid) if aid else None
    if not d:
        return missing(req, c, "artist", "/artists", "All the artists")
    recs = "".join(f'<li><a href="/record?id={r["id"]}">{e(r["title"])}</a> '
                   f'<span class="muted small">{e(r["released"] or "")} {e(r["catno"] or "")}</span></li>'
                   for r in d["releases"])
    known = "".join(
        f'<tr><td>{e(k["title"])}</td><td class="small">{e(k["released"] or "")}</td>'
        f'<td class="small">{e(k["primary_type"] or "")}</td>'
        f'<td class="small muted">{e(k["format_note"] or "")}</td>'
        f'<td class="small muted">{e(k["first_seen_at"][:10])}</td></tr>' for k in d["known"][:200])
    cands_html = ""
    if req.g("find"):
        found, err = sources.artist_search(c, req.g("find"))
        if err:
            cands_html = f'<div class="err">{e(err)}</div>'
        else:
            cards = "".join(f"""<div class="card pick">
<input type="hidden" name="sort_{e(f["mbid"])}" value="{e(f["sort_name"])}">
<p><strong>{e(f["name"])}</strong> <span class="muted small">{e(f["disambiguation"] or "")}
{e(f["kind"] or "")} {e(f["country"] or "")}</span></p>
<p class="bar"><button class="btn sm" name="mbid" value="{e(f["mbid"])}">Link this one</button>
<span class="muted small">match score {f["score"] or 0}</span></p></div>""" for f in found)
            relink = ("" if not d["mbid"] else
                      '<p class="note small">Linking a different MusicBrainz artist clears what '
                      'was noted about the current one, and the next check takes a fresh '
                      'baseline that announces nothing.</p>')
            cands_html = f"""<form method="post" action="/artist/link">
<input type="hidden" name="id" value="{aid}">
<h2>Which {e(req.g("find"))}?</h2>{relink}
{cards or '<p class="muted">Nobody by that name.</p>'}</form>"""
    watch_btn = "Stop watching" if d["watch"] else "Watch for new records"
    base_txt = (f'First swept {d["baseline_at"][:10]}; {len(d["known"])} works known. '
                if d["baseline_at"] else
                "Never swept — the first sweep will record what they already have out and "
                "announce nothing. ")
    body = f"""<h1>{e(d["name"])}</h1>
<p class="muted">{e(d["sort_name"] or "")}
{e("MusicBrainz " + d["mbid"]) if d["mbid"] else ""}</p>
<div class="bar noprint">
  <form method="post" action="/artist/watch" style="margin:0">
    <input type="hidden" name="id" value="{aid}">
    <button class="btn">{e(watch_btn)}</button></form>
  <form method="post" action="/artist/sweep" style="margin:0">
    <input type="hidden" name="id" value="{aid}">
    <button class="btn sec">Check now</button></form>
</div>
{"" if d["mbid"] else '<div class="note">This artist is not linked to MusicBrainz, so there is nothing to watch. Search below to link them.</div>'}
<form method="get" action="/artist" class="bar noprint">
  <input type="hidden" name="id" value="{aid}">
  <input name="find" value="{e(req.g("find") or d["name"])}" placeholder="artist name">
  <button class="btn sec sm">Find on MusicBrainz</button>
</form>
{cands_html}
<p class="muted small">{e(base_txt)}</p>
<h2>On the shelf</h2><ul>{recs or '<li class="muted">Nothing yet.</li>'}</ul>
<h2>Everything MusicBrainz lists</h2>
<div class="scroll"><table><thead><tr><th>Work</th><th>Released</th><th>Type</th>
<th>Formats</th><th>First seen here</th></tr></thead>
<tbody>{known or '<tr><td colspan="5" class="muted">Not swept yet.</td></tr>'}</tbody></table></div>"""
    return shell(req, c, d["name"], body, "shelf", "/artists")


def artist_watch(req, c, m):
    aid = req.fi("id")
    a = c.execute("SELECT watch FROM artist WHERE id=?", (aid,)).fetchone()
    if not a:
        return redirect("/artists")
    c.execute("UPDATE artist SET watch=? WHERE id=?", (0 if a["watch"] else 1, aid))
    c.commit()
    if a["watch"]:
        return flash_to(f"/artist?id={aid}", "Not watching.")
    r = c.execute("SELECT name, mbid FROM artist WHERE id=?", (aid,)).fetchone()
    if not r["mbid"]:
        # Watching someone with no MusicBrainz id can never find anything, so
        # the search that fixes it opens with the name already typed.
        return flash_to(f"/artist?id={aid}&find={urllib.parse.quote(r['name'])}",
                        "Watching — but nothing can be checked until they are linked to "
                        "MusicBrainz. Pick the right one below.")
    return flash_to(f"/artist?id={aid}", "Watching.")


def artist_new(req, c, m):
    """An artist to watch whose records you do not own yet. Made by name, then
    straight to the MusicBrainz search, because a name alone cannot be watched."""
    name = model.norm(req.f("name") or "", 200)
    if not name:
        return flash_to("/artists", "Type a name to look for.")
    aid = find_or_make_artist(c, name)
    c.commit()
    return redirect(f"/artist?id={aid}&find={urllib.parse.quote(name)}")


def artist_link(req, c, m):
    """Link an artist to a MusicBrainz identity — or to a different one.

    A different one starts again from nothing. The works noted so far, and the
    baseline that made them "already known", describe somebody else; keeping the
    baseline would make the new person's whole catalogue "new" on the next sweep,
    which is exactly the flood the baseline exists to prevent.
    """
    aid = req.fi("id")
    mbid = model.norm(req.f("mbid") or "", 60)
    a = c.execute("SELECT * FROM artist WHERE id=?", (aid,)).fetchone() if aid else None
    if not a or not mbid:
        return redirect("/artists")
    sort = model.norm(req.f(f"sort_{mbid}") or "", 200) or None
    if a["mbid"] == mbid:
        c.execute("UPDATE artist SET sort_name=COALESCE(sort_name,?) WHERE id=?", (sort, aid))
        c.commit()
        return flash_to(f"/artist?id={aid}", "Already linked to that one.")
    try:
        c.execute("UPDATE artist SET mbid=?, sort_name=COALESCE(?,sort_name), baseline_at=NULL "
                  "WHERE id=?", (mbid, sort, aid))
    except sqlite3.IntegrityError:
        c.rollback()
        return flash_to(f"/artist?id={aid}", "Another artist is already linked to that one.")
    if a["mbid"]:
        c.execute("DELETE FROM artist_release WHERE artist_id=? AND source='musicbrainz'", (aid,))
    c.commit()
    if a["mbid"]:
        return flash_to(f"/artist?id={aid}",
                        "Linked to a different MusicBrainz artist. What was noted about the "
                        "old one is cleared, and the next check takes a fresh baseline.")
    return flash_to(f"/artist?id={aid}", "Linked. Check now to take the first baseline.")


def artist_sweep(req, c, m):
    aid = req.fi("id")
    if aid:
        a = c.execute("SELECT * FROM artist WHERE id=?", (aid,)).fetchone()
        if not a:
            return redirect("/artists")
        raised, note = alerts_mod.sweep_artist(c, a, force=True)
        msg = note or (f"{len(raised)} new thing(s) found." if raised else "Nothing new.")
        return flash_to(f"/artist?id={aid}", msg)
    watched = c.execute("SELECT * FROM artist WHERE watch=1 AND mbid IS NOT NULL").fetchall()
    total, notes = 0, []
    for a in watched:
        raised, note = alerts_mod.sweep_artist(c, a)
        total += len(raised)
        if note:
            notes.append(note)
    msg = f"Checked {len(watched)} artist(s); {total} new thing(s)."
    if notes:
        msg += " " + notes[0]
    return flash_to("/artists", msg)


# ------------------------------------------------------------------- settings

def _secret_input(c, key, label, kind="password", placeholder=""):
    """A box for a secret that never shows the secret.

    The page is served to anything on the network by default, so a stored token,
    password or webhook address is never written into it — the same three the
    backup leaves out. Blank keeps what is stored; the checkbox forgets it.
    """
    have = bool(db.get(c, key, ""))
    hint = ""
    if have and key == "webhook_url":
        p = urllib.parse.urlparse(db.get(c, key, ""))
        if p.scheme in ("http", "https") and p.netloc:
            hint = f" Currently sends to {p.scheme}://{p.netloc}/…"
    ph = "set — leave blank to keep it" if have else placeholder
    forget = (f'<label class="small" style="font-weight:400"><input type="checkbox" '
              f'name="forget_{key}" value="1"> Forget it</label>' if have else "")
    note = (f'<span class="muted small">{e(("Set." if have else "") + hint)}</span>'
            if have else "")
    return (f'<label for="s_{key}">{e(label)}</label>'
            f'<input id="s_{key}" type="{kind}" name="{key}" value="" placeholder="{e(ph)}" '
            f'autocomplete="{"new-password" if kind == "password" else "off"}"> {note}{forget}')


def settings_page(req, c, m):
    g = lambda k: e(db.get(c, k, ""))
    cur = db.get(c, "currency", "GBP")
    cur_opts = [(k, f"{k} {model.CURRENCIES.get(k, '').strip()}")
                for k, _ in model.currency_choices(cur)]
    body = f"""<h1>Settings</h1>
<form method="post" action="/settings" class="card">
  <h3>Money</h3>
  <label for="cu">Ask for prices in</label>
  <select id="cu" name="currency">{opts(cur_opts, cur)}</select>
  <p class="muted small">For everyone using this collection, and also on the Prices page.
  Switching asks Discogs for the new currency from then on. Prices already recorded stay in
  the currency they were quoted in, and a source that answers in another is stored as it
  answered — nothing here converts one currency into another, ever.</p>
  <h3>Looking things up</h3>
  <label for="ua">Contact for the User-Agent (optional)</label>
  <input id="ua" name="user_agent_contact" value="{g("user_agent_contact")}"
         placeholder="you@example.com, or a URL">
  <p class="muted small">MusicBrainz asks that apps say who to contact if one misbehaves.
  This is blank unless you fill it in, and it is the only thing about you that is ever sent.</p>
  {_secret_input(c, "discogs_token", "Discogs token (optional)")}
  <p class="muted small">Everything works without one. A token raises Discogs' limit from 25
  requests a minute to 60. It is kept in this database, never shown on this page, and left out
  of backups.</p>
  <label><input type="checkbox" name="offline" value="1" {"checked" if db.get_bool(c, "offline") else ""}>
    Offline — never touch the network</label>
  <h3>Where to send alerts</h3>
  <p class="muted small">Leave all of this empty and alerts appear in this app only,
  which is the default.</p>
  <div class="grid">
    <div><label for="sh">Mail server</label><input id="sh" name="smtp_host" value="{g("smtp_host")}"></div>
    <div><label for="sp">Port</label><input id="sp" name="smtp_port" value="{g("smtp_port")}"></div>
    <div><label for="su">Username</label><input id="su" name="smtp_user" value="{g("smtp_user")}" autocomplete="off"></div>
    <div>{_secret_input(c, "smtp_pass", "Password")}</div>
    <div><label for="sf">From</label><input id="sf" name="smtp_from" value="{g("smtp_from")}"></div>
    <div><label for="st">To</label><input id="st" name="smtp_to" value="{g("smtp_to")}"></div>
  </div>
  <label><input type="checkbox" name="smtp_tls" value="1" {"checked" if db.get_bool(c, "smtp_tls") else ""}>
    Use STARTTLS</label>
  {_secret_input(c, "webhook_url", "Or a webhook URL", "url", "https://ntfy.sh/your-topic")}
  <p class="muted small">A JSON POST. ntfy, Discord and Slack all accept one. Whoever has the
  address can post to it, so it is treated like a password.</p>
  <h3>How often</h3>
  <label for="ma">Do not look a price up again within</label>
  <input id="ma" name="price_max_age_hours" value="{g("price_max_age_hours")}" inputmode="numeric"> hours
  <p class="bar"><button class="btn">Save</button></p>
</form>
<div class="card"><h3>The database</h3>
<p class="small">Everything lives in one file: <code>{e(db.DB_PATH)}</code>.
Schema version {e(db.schema_version(c))}. Copy it and you have copied the collection.</p>
<p class="bar"><a class="btn sec sm" href="/backup">Backup</a>
<a class="btn sec sm" href="/calls">What it asked the internet</a></p></div>"""
    return shell(req, c, "Settings", body, "more", "/settings")


def settings_save(req, c, m):
    db.set_currency(c, (req.f("currency") or "").strip())
    for k in ("user_agent_contact", "smtp_host", "smtp_user", "smtp_from", "smtp_to"):
        db.put(c, k, model.norm(req.f(k) or "", 500))
    # The page never shows a stored secret, so a blank box means "keep it". A
    # password is kept exactly as typed: collapsing its spaces would change it.
    for k in db.SECRET_KEYS:
        typed = req.f(k) or ""
        typed = typed[:500] if k == "smtp_pass" else model.norm(typed, 500)
        if typed.strip():
            db.put(c, k, typed)
        elif req.has(f"forget_{k}"):
            db.put(c, k, "")
    db.put(c, "smtp_port", str(model.intn(req.f("smtp_port")) or 587))
    db.put(c, "price_max_age_hours", str(max(1, model.intn(req.f("price_max_age_hours")) or 20)))
    db.put(c, "smtp_tls", "1" if req.has("smtp_tls") else "0")
    db.put(c, "offline", "1" if req.has("offline") else "0")
    c.commit()
    return flash_to("/settings", "Saved.")


def calls_page(req, c, m):
    rows = c.execute("""SELECT * FROM source_call ORDER BY id DESC LIMIT 200""").fetchall()
    trs = "".join(
        f'<tr><td class="small">{e(r["called_at"][:19].replace("T", " "))}</td>'
        f'<td class="small">{e(r["host"])}</td><td class="small">{e(r["path"])}</td>'
        f'<td class="num">{e(r["status"])}</td><td class="num">{e(r["ms"])}</td>'
        f'<td class="small muted">{e(r["note"] or "")}</td></tr>' for r in rows)
    waits = [(h, sources.wait_needed(c, h)) for h in (sources.MB_HOST, sources.DG_HOST)]
    wait_txt = " · ".join(f"{h}: {'ready' if w <= 0 else f'{w:.1f}s to wait'}" for h, w in waits)
    body = f"""<h1>What it asked the internet</h1>
<p class="muted">Every request this app has made, most recent first. What goes out is a
barcode, a release number, an artist number, a name, title or catalogue number you typed, and
a User-Agent. Your
collection is never sent anywhere, and no cover image is ever loaded from a third party —
which is why a record's page links to a cover rather than showing one.</p>
<p class="muted small">Right now — {e(wait_txt)}.</p>
<div class="scroll"><table><thead><tr><th>When</th><th>Host</th><th>Path</th>
<th class="num">Status</th><th class="num">ms</th><th></th></tr></thead>
<tbody>{trs or '<tr><td colspan="6" class="muted">Nothing yet.</td></tr>'}</tbody></table></div>
<p class="muted small">Kept to the most recent {db.PRUNE_CALLS}. This log is also how the
rate limiter works: it measures from what is written here, so restarting the app cannot be
used to get around a published limit.</p>"""
    return shell(req, c, "Requests", body, "more", "/calls")


def help_page(req, c, m):
    cur = db.get(c, "currency", "GBP")
    d = c.execute("""SELECT * FROM alert_rule WHERE kind='price_move' AND release_id IS NULL
                     ORDER BY id DESC LIMIT 1""").fetchone()
    grades = "".join(
        f'<tr><td class="num"><strong>{g}</strong></td><td>{e(model.GRADE_MEDIA[g][0])}</td>'
        f'<td class="small">{e(model.GRADE_MEDIA[g][1])}</td>'
        f'<td class="small">{e(model.GRADE_SLEEVE[g][1])}</td>'
        f'<td>{e(model.goldmine(g))}</td></tr>' for g in model.GRADES)
    kinds = "".join(f'<tr><td>{e(v[0])}</td><td class="small">{e(v[1])}</td></tr>'
                    for v in model.PRICE_KINDS.values())
    body = f"""<h1>How the numbers work</h1>

<h2>A record, an edition and a copy</h2>
<p>"Kind of Blue" is not a thing you can own. A 2010 Music On Vinyl pressing with catalogue
number MOVLP 019 is, and so is the 1959 Columbia original, and they are worth wildly
different money. This app calls that an <strong>edition</strong>, and a
<strong>copy</strong> is the object on your shelf. You can own two copies of one edition in
different condition, and they are two rows.</p>
<p>A barcode identifies an edition — but not always uniquely. Two editions genuinely share
one barcode often enough that this app asks which one you have rather than guessing. That
is what the choosing screen is for.</p>
<p>Most records pressed before about 1980 have no barcode at all. Those are
<a href="/search">looked up by name</a>: the artist and title, or better, the catalogue
number printed on the label, and you pick the pressing from the list. The catalogue number
matters because a famous album can have hundreds of pressings under one title.</p>

<h2>Condition</h2>
<p>Five points, the record and the packaging graded separately, because they wear
independently — a mint record in a split sleeve is an ordinary thing to own. The words
matter more than the number: they are what make your 3 today and your 3 in two years the
same 3.</p>
<div class="scroll"><table><thead><tr><th class="num">Grade</th><th>Name</th>
<th>The record</th><th>The sleeve</th><th>Trade grade</th></tr></thead>
<tbody>{grades}</tbody></table></div>
<p class="muted small">There is no 6 for Mint, because Mint means sealed and unplayed and a
record you have played is not Mint however careful you were. A sealed record is handled the
other way round: you decline to grade the vinyl, and the app records <em>why</em> it is not
graded rather than treating the blank as a 5 or a 0.</p>

<h2>What a price here actually is</h2>
<div class="scroll"><table><thead><tr><th>Kind</th><th>And what it is not</th></tr></thead>
<tbody>{kinds}</tbody></table></div>
<p>Nearly every automatic price in this app is the first row: the <strong>lowest asking
price</strong> on Discogs. It is what the cheapest seller hopes to get today. It is not a
sale, not an appraisal, and — because Discogs' public figures do not say — not a price for
any particular condition. Every page that shows one says so.</p>

<h2>When you get told</h2>
<p>A move must clear <strong>both</strong> {d["pct_threshold"] if d else 20:g}% and
{e(money(d["abs_threshold"] if d else 3.0, cur))}, measured against the
<strong>median</strong> of the previous {d["window_days"] if d else 30} days, and only once
there are at least {d["min_obs"] if d else 3} observations to compare against. Then it goes
quiet for {d["cooldown_days"] if d else 14} days so the same move is not reported daily.</p>
<p>The median rather than yesterday's price, because a lowest asking price is one seller's
number: it jumps when that seller sells or relists, which is not the market moving. Both
thresholds, because a percentage alone shouts about a cheap single and an amount alone
ignores a box set.</p>
<p>Every move is printed with how many copies were for sale at each end. A price that halved
while the number of listings went from one to twelve is a story about supply, not about what
a record is worth, and you are entitled to see that before drawing a conclusion.</p>
<p><strong>Up is not good and down is not bad.</strong> Whether a rise is welcome depends on
whether you are buying or selling, which this app does not know. So a move is an arrow and a
word, never a colour, and the words "gain", "loss" and "value" do not appear.</p>

<h2>What this app will not say</h2>
<ul>
<li>It will not tell you what your collection is worth. It will add up the cheapest copy
currently for sale for each record and tell you that is what it did.</li>
<li>It will not convert a currency. Two currencies are two lists. Switching between CAD,
GBP and USD on the Prices page changes what Discogs is asked for from then on, for everyone;
the prices already recorded stay in the currency they were quoted in.</li>
<li>It will not average your grades. Counts, never a mean — a 4 is better than a 3, but not
"one better", so the average of a shelf would be a number with no meaning.</li>
<li>It will not call a move statistically anything. There is no test here, no trend line and
no forecast. The strongest word it uses is "notable", and it shows you the arithmetic.</li>
<li>It will not claim a record was pressed where its barcode was registered. The barcode on
a Dutch pressing of an American album starts 088, which is the United States, because that
is where the label registered the number.</li>
</ul>

<h2>Where the facts come from</h2>
<p>MusicBrainz and Discogs, both without an account. What leaves this machine is a barcode,
a release number, an artist number, a name, title or catalogue number you typed, and a
User-Agent — see
<a href="/calls">the request log</a>, which lists every one.</p>"""
    return shell(req, c, "How the numbers work", body, "more", "/help")


# --------------------------------------------------------------------- labels

def barcode_svg(req, c, m):
    """A barcode as SVG. Black on white in both themes — see barcode.svg()."""
    code = bc.normalise(req.g("code") or "")
    if not code or not bc.check_ok(code):
        return (404, [("Content-Type", "text/plain; charset=utf-8")], b"not a barcode")
    scale = max(1, min(6, model.intn(req.g("scale")) or 2))
    body = bc.svg(code, scale=scale, height=max(20, min(120, model.intn(req.g("h")) or 54)))
    return (200, [("Content-Type", "image/svg+xml; charset=utf-8"),
                  ("Cache-Control", "public, max-age=86400")], body.encode("utf-8"))


def labels_page(req, c, m):
    rows = library.shelf(c, model.norm(req.g("q") or "", 120), req.gi("artist"),
                         req.g("fmt") or "", "yes", req.g("sort") or "artist")
    cards = []
    for r in rows:
        code = r["barcode"] or ""
        img = (f'<img class="bcode" src="/barcode.svg?code={e(code)}&amp;scale=2&amp;h=46" '
               f'alt="{e(code)}" width="210">' if code and bc.check_ok(code) else
               '<span class="muted small">no barcode</span>')
        gv = r["copies"][0]["grade"] if r["copies"] else library.grade_view(None)
        g = (f'{gv["media"] or "—"}/{gv["sleeve"] or "—"}' if gv["has"] else "not graded")
        cards.append(f"""<div class="card" style="break-inside:avoid;width:250px">
<p style="margin:0"><strong>{e(r["title"])}</strong></p>
<p class="muted small" style="margin:2px 0">{e(r["artist_credit"])}</p>
<p class="muted small" style="margin:2px 0">{e(r["label"] or "")} {e(r["catno"] or "")}
{e(r["released"] or "")} · {e(g)}</p>
<p style="margin:4px 0 0">{img}</p></div>""")
    body = f"""<h1 class="noprint">Shelf labels</h1>
<p class="muted noprint">One card per record you own, with its barcode drawn from the
number on file. Print this page. The barcodes are black on white whatever theme you are
using, because an inverted barcode is one no camera will read.</p>
<p class="bar noprint"><a class="btn sec sm" href="/shelf">Back to the shelf</a></p>
<div style="display:flex;flex-wrap:wrap;gap:10px">{"".join(cards) or '<p class="muted">Nothing owned yet.</p>'}</div>"""
    return shell(req, c, "Shelf labels", body, "shelf", "/labels")


# --------------------------------------------------------------------- backup

def backup_page(req, c, m):
    n = library.counts(c)
    body = f"""<h1>Backup</h1>
<p>Everything is in one SQLite file. The simplest backup is to copy
<code>{e(db.DB_PATH)}</code> somewhere else.</p>
<p>The download below is a zip of every table as CSV, which is readable by anything and
will outlive this app.</p>
<p class="bar"><a class="btn" href="/backup.zip">Download the CSVs</a></p>
<p class="muted small">Two things are deliberately left out. Your Discogs token, your mail
password and your webhook URL are not in the download — a backup is a file people email to
themselves. The request log is left out too; it regrows on its own.</p>
<div class="card"><h3>What is in it</h3>
<p class="small">{n["releases"]} records, {n["copies"]} copies on the shelf,
{n["observations"]} price observations, {n["scans"]} scans, {n["artists"]} artists.</p></div>"""
    return shell(req, c, "Backup", body, "more", "/backup")


def backup_zip(req, c, m):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for table in db.BACKUP_TABLES:
            rows = c.execute(f"SELECT * FROM {table}").fetchall()
            out = io.StringIO()
            w = csv.writer(out)
            cols = [d[0] for d in c.execute(f"SELECT * FROM {table} LIMIT 0").description]
            w.writerow(cols)
            for r in rows:
                if table == "setting" and r["key"] in db.SECRET_KEYS:
                    continue
                w.writerow([_sk(r[k]) for k in cols])
            z.writestr(f"{table}.csv", out.getvalue())
        z.writestr("README.txt",
                   f"{APP_NAME} backup, schema version {db.schema_version(c)}, "
                   f"taken {model.now_iso()}.\n\n"
                   f"One CSV per table. Left out on purpose: {', '.join(db.SECRET_KEYS)} "
                   f"(secrets) and the source_call log (regrows on its own).\n")
    return (200, [("Content-Type", "application/zip"),
                  ("Content-Disposition",
                   f'attachment; filename="sleevenote-{model.today_iso()}.zip"')],
            buf.getvalue())


# ------------------------------------------------------------------- the door
#
# Five routes answer without a login: the login form, the two POSTs that log
# in, the health probe, the web manifest, and the static shelf. Everything else
# is behind the gate in Handler._run, which is default-deny: a route added later
# is private unless somebody adds it to PUBLIC on purpose.

def _cookie_names():
    """__Host- cookies cannot be set by a sibling domain or sent over plain
    http, but a browser refuses them without Secure — so plain http (a laptop on
    the LAN, the test suite) gets plain names."""
    if _secure():
        return "__Host-sn_session", "__Host-sn_device"
    return "sn_session", "sn_device"


def _secure():
    return bool(TLS_CERT and TLS_KEY) or BEHIND_HTTPS


def _cookie(name, value, max_age, same_site="Lax"):
    flags = "; Secure" if _secure() else ""
    return f"{name}={value}; Path=/; Max-Age={max_age}; HttpOnly; SameSite={same_site}{flags}"


AUTH_LOG = None     # a stream to write login events to; None means stderr


def _auth_log(what, req, name=""):
    """One line per login event, to stderr — which is `docker logs` in the
    container. Never the password; the name is cut short and repr'd, because
    whoever typed it may not be friendly."""
    print(f"{model.now_iso()} auth {what} name={name[:40]!r} from {req.ip}",
          file=AUTH_LOG or sys.stderr, flush=True)


def _login_form(req, err="", name="", status=200):
    nxt = _safe_next(req.f("next") or req.g("next"))
    flash = req.g("msg") if req.method == "GET" else None
    err_html = f'<div class="err">{e(err)}</div>' if err else ""
    body = f"""<div style="max-width:440px;margin:24px auto">
<h1>Log in</h1>
{err_html}
<form method="post" action="/login" class="card">
  <input type="hidden" name="next" value="{e(nxt)}">
  <label for="nm">Name</label>
  <input id="nm" name="name" value="{e(name)}" autocomplete="username"
         autocapitalize="none" spellcheck="false" required>
  <label for="pw">Password</label>
  <input id="pw" name="password" type="password" autocomplete="current-password" required>
  <p class="bar"><button class="btn">Log in</button></p>
</form>
<p class="muted small">First time? Type the setup code you were given as your
password. The next page asks you to choose your own.</p>
</div>"""
    return html_ok(page("Log in", body, flash=flash, req=req, bare=True), status)


def _welcome_form(req, acct, code, err=""):
    nxt = _safe_next(req.f("next"))
    err_html = f'<div class="err">{e(err)}</div>' if err else ""
    body = f"""<div style="max-width:460px;margin:24px auto">
<h1>Welcome, {e(acct["name"])}</h1>
<p>That setup code works once. Choose the password you will use from now on.</p>
{err_html}
<form method="post" action="/welcome" class="card">
  <input type="hidden" name="next" value="{e(nxt)}">
  <input type="hidden" name="code" value="{e(code)}">
  <input type="text" name="name" value="{e(acct["name"])}" autocomplete="username"
         hidden readonly>
  <label for="np">New password</label>
  <input id="np" name="password" type="password" minlength="{auth.MIN_PASSWORD}"
         autocomplete="new-password" required>
  <label for="na">The same again</label>
  <input id="na" name="again" type="password" autocomplete="new-password" required>
  <p class="bar"><button class="btn">Save it and go in</button></p>
</form>
<p class="muted small">At least {auth.MIN_PASSWORD} characters. A few unrelated words
in a row is long enough and easy to remember.</p>
</div>"""
    return html_ok(page("Choose a password", body, req=req, bare=True))


def _go_in(req, c, acct, dest, msg=""):
    """A new session for this account, the cookies that carry it, and the page
    it was going to."""
    token = auth.new_session(c, acct["id"])
    auth.prune_sessions(c)
    c.commit()
    fresh = c.execute("SELECT * FROM account WHERE id=?", (acct["id"],)).fetchone()
    sess_name, dev_name = _cookie_names()
    loc = _safe_next(dest)
    if msg:
        loc += ("&" if "?" in loc else "?") + "msg=" + urllib.parse.quote(msg[:300])
    return (303, [("Location", loc),
                  ("Set-Cookie", _cookie(sess_name, token, auth.COOKIE_DAYS * 86400)),
                  ("Set-Cookie", _cookie(dev_name, auth.device_cookie(fresh),
                                         auth.DEVICE_DAYS * 86400, "Strict"))], b"")


def _attempt(req, acct, name):
    """The throttle keys for this try, and how long it must wait (0 = go)."""
    _, dev_name = _cookie_names()
    keys = auth.throttle_keys(name, req.ip, auth.device_nonce(req.cookie(dev_name), acct))
    return keys, auth.THROTTLE.begin(keys)


def _too_soon(req, wait, name):
    _auth_log("throttled", req, name)
    return _login_form(req, f"Too many wrong tries. Wait {auth.wait_words(wait)} and try "
                            "again — nothing typed in the meantime is checked.", name, 429)


def login_page(req, c, m):
    if req.user:
        return redirect(_safe_next(req.g("next")))
    return _login_form(req)


def login_post(req, c, m):
    name = (req.f("name") or "").strip()
    pw = req.f("password") or ""
    acct = auth.account_by_name(c, name)
    keys, wait = _attempt(req, acct, name)
    if wait:
        return _too_soon(req, wait, name)
    if acct and acct["pw_hash"]:
        if auth.check_secret(pw[:auth.MAX_PASSWORD], acct["pw_hash"]):
            auth.THROTTLE.succeeded(keys)
            _auth_log("ok", req, acct["name"])
            return _go_in(req, c, acct, req.f("next"))
    elif acct and acct["setup_hash"]:
        code = auth.norm_code(pw[:100])
        if auth.check_secret(code, acct["setup_hash"]):
            auth.THROTTLE.succeeded(keys)
            _auth_log("setup code ok", req, acct["name"])
            return _welcome_form(req, acct, code)
    else:
        auth.burn_time(pw[:auth.MAX_PASSWORD])
    _auth_log("refused", req, name)
    return _login_form(req, "That name and password do not go together.", name)


def welcome_post(req, c, m):
    """The second half of a first entry. The setup code comes back with the new
    password and is checked again here, so this page holds no state of its own
    and cannot be reached without the code."""
    name = (req.f("name") or "").strip()
    code = auth.norm_code((req.f("code") or "")[:100])
    acct = auth.account_by_name(c, name)
    keys, wait = _attempt(req, acct, name)
    if wait:
        return _too_soon(req, wait, name)
    if not (acct and not acct["pw_hash"] and acct["setup_hash"]
            and auth.check_secret(code, acct["setup_hash"])):
        if not acct:
            auth.burn_time(code)
        _auth_log("welcome refused", req, name)
        return _login_form(req, "That setup code has already been used, or was never "
                                "right. Log in again, or ask for a new code.", name)
    auth.THROTTLE.succeeded(keys)
    pw, again = req.f("password") or "", req.f("again") or ""
    problem = auth.password_problem(pw, again, acct["name"], code)
    if problem:
        return _welcome_form(req, acct, code, problem)
    auth.set_password(c, acct["id"], pw)
    c.commit()
    _auth_log("password chosen", req, acct["name"])
    return _go_in(req, c, acct, req.f("next"),
                  f"Welcome, {acct['name']}. That password is yours now.")


def logout_post(req, c, m):
    auth.end_session(c, req.token)
    c.commit()
    sess_name, _ = _cookie_names()
    return (303, [("Location", "/login?msg=" + urllib.parse.quote("Logged out.")),
                  ("Set-Cookie", _cookie(sess_name, "", 0))], b"")


def account_page(req, c, m, err=""):
    u = req.user
    others = c.execute("SELECT name, pw_hash, setup_hash FROM account WHERE id<>? "
                       "ORDER BY name", (u["id"],)).fetchall()
    who = "".join(f"<li>{e(o['name'])} — "
                  f"{'has a password' if o['pw_hash'] else 'has not logged in yet' if o['setup_hash'] else 'cannot log in until a setup code is issued'}</li>"
                  for o in others)
    n_sess = c.execute("SELECT COUNT(*) n FROM session WHERE account_id=?",
                       (u["id"],)).fetchone()["n"]
    err_html = f'<div class="err">{e(err)}</div>' if err else ""
    body = f"""<h1>Your password</h1>
<div class="card"><h3>Logged in as {e(u["name"])}</h3>
<p class="muted small">Password set {e((u["pw_set_at"] or "")[:10]) or "—"}.
Logged in on {n_sess} device{"" if n_sess == 1 else "s"}.</p>
<form method="post" action="/logout"><button class="btn sec sm">Log out</button></form>
</div>
<div class="card"><h3>Change it</h3>
{err_html}
<form method="post" action="/account/password">
  <input type="text" name="name" value="{e(u["name"])}" autocomplete="username" hidden readonly>
  <label for="cp">The one you use now</label>
  <input id="cp" name="current" type="password" autocomplete="current-password" required>
  <label for="np">New password</label>
  <input id="np" name="password" type="password" minlength="{auth.MIN_PASSWORD}"
         autocomplete="new-password" required>
  <label for="na">The same again</label>
  <input id="na" name="again" type="password" autocomplete="new-password" required>
  <p class="bar"><button class="btn">Change it</button></p>
</form>
<p class="muted small">Changing it logs you out everywhere else.</p>
</div>
<div class="card"><h3>Everyone who can log in</h3>
<p class="muted small">Every account manages the same collection.</p>
<ul><li>{e(u["name"])} — you</li>{who}</ul>
<p class="muted small">Forgotten passwords are reset on the server:
<code>python3 users.py code NAME</code> prints a new setup code.</p>
</div>"""
    return shell(req, c, "Your password", body, "more", "/account")


def account_password(req, c, m):
    u = req.user
    keys = [("change", u["id"])]
    wait = auth.THROTTLE.begin(keys)
    if wait:
        return account_page(req, c, m, f"Too many wrong tries. Wait {auth.wait_words(wait)}.")
    if not auth.check_secret((req.f("current") or "")[:auth.MAX_PASSWORD], u["pw_hash"]):
        _auth_log("change refused", req, u["name"])
        return account_page(req, c, m, "That is not your current password. Nothing changed.")
    auth.THROTTLE.succeeded(keys)
    problem = auth.password_problem(req.f("password"), req.f("again"), u["name"])
    if problem:
        return account_page(req, c, m, problem + " Nothing changed.")
    auth.set_password(c, u["id"], req.f("password"))
    c.commit()
    _auth_log("password changed", req, u["name"])
    return _go_in(req, c, u, "/account",
                  "Password changed. Every other device has been logged out.")


def healthz(req, c, m):
    """For the container's healthcheck and the portal. Says it is up and
    nothing else — it answers anybody."""
    c.execute("SELECT 1").fetchone()
    return (200, [("Content-Type", "text/plain; charset=utf-8")], b"ok\n")


# --------------------------------------------------------------------- static

def static_file(req, c, m):
    name = os.path.basename(m.group(1))
    ctype = STATIC.get(name)
    if not ctype:
        return html_ok(page("Not found", "<h1>Not found</h1>", req=req), 404)
    try:
        with open(os.path.join(STATIC_DIR, name), "rb") as fh:
            blob = fh.read()
    except OSError:
        return html_ok(page("Not found", "<h1>Not found</h1>", req=req), 404)
    return (200, [("Content-Type", ctype), ("Cache-Control", "public, max-age=86400"),
                  ("X-Content-Type-Options", "nosniff")], blob)


def manifest(req, c, m):
    data = {
        "name": APP_NAME, "short_name": APP_NAME, "start_url": "/",
        "display": "standalone", "background_color": "#F4F1EA", "theme_color": "#E8E3DA",
        "description": "A vinyl collection, its condition, and what it is being asked for.",
        "icons": [{"src": f"/static/icon.svg?v={STATIC_V}", "sizes": "any",
                   "type": "image/svg+xml", "purpose": "any"}],
    }
    return (200, [("Content-Type", "application/manifest+json")],
            json.dumps(data).encode("utf-8"))


# --------------------------------------------------------------------- routes

ROUTES = [
    ("GET",  r"/",                      landing),
    ("GET",  r"/more",                  more_page),
    ("POST", r"/view",                  view_set),
    ("POST", r"/theme",                 theme_set),
    # scanning
    ("GET",  r"/scan",                  scan_page),
    ("POST", r"/scan",                  scan_post),
    ("GET",  r"/scan/choose",           scan_choose),
    ("POST", r"/scan/choose",           scan_choose_post),
    ("POST", r"/scan/manual",           scan_manual),
    ("POST", r"/scan/again",            scan_again),
    ("GET",  r"/search",                search_page),
    ("POST", r"/search",                search_post),
    ("GET",  r"/search/choose",         search_choose),
    ("POST", r"/search/choose",         search_choose_post),
    ("POST", r"/search/manual",         search_manual),
    ("POST", r"/search/again",          search_again),
    # the shelf
    ("GET",  r"/shelf",                 shelf_page),
    ("GET",  r"/shelf\.csv",            shelf_csv),
    ("GET",  r"/record",                record_page),
    ("GET",  r"/record/new",            record_new),
    ("POST", r"/record/new",            record_new_post),
    ("POST", r"/record/track",          record_track),
    ("POST", r"/record/notes",          record_notes),
    ("POST", r"/record/link",           record_link),
    ("POST", r"/record/fetch",          record_fetch),
    ("POST", r"/record/rule",           record_rule),
    ("GET",  r"/record/edit",           record_edit),
    ("POST", r"/record/edit",           record_edit_post),
    ("POST", r"/record/delete",         record_delete),
    # copies and condition
    ("GET",  r"/copy",                  copy_page),
    ("GET",  r"/copy/new",              copy_new),
    ("POST", r"/copy/new",              copy_new_post),
    ("POST", r"/copy/edit",             copy_edit),
    ("POST", r"/copy/dispose",          copy_dispose),
    ("POST", r"/copy/delete",           copy_delete),
    ("GET",  r"/grade",                 grade_form),
    ("POST", r"/grade",                 grade_save),
    # prices
    ("GET",  r"/prices",                prices_page),
    ("POST", r"/prices/check",          price_check),
    ("POST", r"/prices/add",            price_add),
    ("POST", r"/currency",              currency_set),
    # alerts
    ("GET",  r"/alerts",                alerts_page),
    ("POST", r"/alerts/seen",           alerts_seen),
    ("POST", r"/alerts/dismiss",        alerts_dismiss),
    ("POST", r"/alerts/test",           alerts_test),
    ("GET",  r"/rules",                 rules_page),
    ("POST", r"/rules",                 rules_save),
    # artists
    ("GET",  r"/artists",               artists_page),
    ("GET",  r"/artist",                artist_page),
    ("POST", r"/artist/watch",          artist_watch),
    ("POST", r"/artist/new",            artist_new),
    ("POST", r"/artist/link",           artist_link),
    ("POST", r"/artist/sweep",          artist_sweep),
    # everything else
    ("GET",  r"/labels",                labels_page),
    ("GET",  r"/barcode\.svg",          barcode_svg),
    ("GET",  r"/settings",              settings_page),
    ("POST", r"/settings",              settings_save),
    ("GET",  r"/calls",                 calls_page),
    ("GET",  r"/help",                  help_page),
    ("GET",  r"/backup",                backup_page),
    ("GET",  r"/backup\.zip",           backup_zip),
    ("GET",  r"/static/([A-Za-z0-9._-]+)", static_file),
    ("GET",  r"/" + MANIFEST_NAME.replace(".", r"\."), manifest),
    # the door
    ("GET",  r"/login",                 login_page),
    ("POST", r"/login",                 login_post),
    ("POST", r"/welcome",               welcome_post),
    ("POST", r"/logout",                logout_post),
    ("GET",  r"/account",               account_page),
    ("POST", r"/account/password",      account_password),
    ("GET",  r"/healthz",               healthz),
]
COMPILED = [(mth, re.compile(rx), fn) for mth, rx, fn in ROUTES]

# The routes that answer without a login. Everything not named here is private.
# The manifest is here because a browser fetches it without cookies; it says
# nothing but the app's name. The static shelf is two files anybody may read.
PUBLIC = {("GET", "/login"), ("POST", "/login"), ("POST", "/welcome"),
          ("GET", "/healthz"), ("GET", "/" + MANIFEST_NAME)}


def _is_public(method, path):
    return (method, path) in PUBLIC or (method == "GET" and path.startswith("/static/"))


# Every response carries these. The CSP allows exactly what the app uses: its
# own scan.js, its inline <style>, and images from itself — "no page ever
# embeds a remote image" is enforced by the browser as well as by the code.
SECURITY_HEADERS = [
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "same-origin"),
    ("X-Frame-Options", "DENY"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Permissions-Policy", "camera=(self), microphone=(), geolocation=()"),
    # On a public address the login page is findable by anyone who reads the
    # certificate logs. It need not also be findable by searching.
    ("X-Robots-Tag", "noindex, nofollow"),
    ("Content-Security-Policy",
     "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
     "img-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; "
     "frame-ancestors 'none'"),
]


# ------------------------------------------------------------ network guards

def _host_only(raw):
    h = (raw or "").strip()
    if h.startswith("["):
        return h[1:h.index("]")] if "]" in h else h[1:]
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


def _is_ip(h):
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        return False


# Names nobody can register on the public internet, which is what a rebinding
# attack needs: mDNS, Tailscale, and the suffixes set aside for home networks.
PRIVATE_SUFFIXES = (".local", ".ts.net", ".lan", ".home.arpa", ".internal", ".localdomain")


def _host_allowed(raw):
    """Reject a Host we do not recognise. This is the DNS-rebinding defence: a
    hostile page that resolves its own name to this address cannot drive the app.
    Loopback, a bare IP, a one-word machine name (a hosts-file entry, a router's
    DHCP name, Tailscale's short MagicDNS name) and PRIVATE_SUFFIXES are allowed."""
    h = _host_only(raw).lower().rstrip(".")
    if not h:
        return False
    if h in ("localhost", "127.0.0.1", "::1", "ts.net"):
        return True
    if "." not in h and re.fullmatch(r"[a-z0-9-]{1,63}", h):
        return True
    if h.endswith(PRIVATE_SUFFIXES):
        return True
    return _is_ip(h)


LOOPBACK_NAMES = ("localhost", "127.0.0.1", "::1")
LOOPBACK_PEERS = ("127.0.0.1", "::1", "::ffff:127.0.0.1")


def _same_origin(headers, peer=""):
    """Block a cross-site form post. A missing Origin and Referer is allowed,
    because a plain non-browser client sends neither — and such a client has no
    session cookie, so it gets no further than the login.

    The page and the server must name the same host. The one exception is a
    proxy on this machine that rewrites Host to loopback (`tailscale serve`
    pointed at 127.0.0.1 on the host, rather than in a sidecar): then a ts.net
    origin is accepted from a loopback peer, and only when Host is loopback too.
    A ts.net name is never trusted against a real Host, because Tailscale Funnel
    puts anybody's public website on a ts.net name."""
    host = _host_only(headers.get("Host", "")).lower()
    for key in ("Origin", "Referer"):
        val = headers.get(key)
        if not val:
            continue
        try:
            src = _host_only(urllib.parse.urlparse(val).netloc).lower()
        except ValueError:
            return False
        if src == host:
            return True
        if host in LOOPBACK_NAMES:
            return src in LOOPBACK_NAMES or (src.endswith(".ts.net") and peer in LOOPBACK_PEERS)
        return False
    return True


def _client_ip(headers, peer):
    """Who is asking. Behind a proxy on this machine every request arrives from
    loopback, and the proxy says who it was for in X-Forwarded-For; the LAST
    entry is the one it added, which a caller cannot forge. From anywhere else
    the header is ignored."""
    if peer in LOOPBACK_PEERS:
        xff = headers.get("X-Forwarded-For", "") or ""
        last = xff.split(",")[-1].strip()
        if last and len(last) <= 64:
            return last
    return peer


def _parse_multipart(ctype, body):
    """Hand-rolled multipart/form-data parser — the stdlib cgi module is gone.

    Fields accumulate in submission order so a repeated name behaves like
    parse_qs. Only the framing CRLFs are stripped, so an uploaded photo survives
    byte for byte — which matters, because the barcode decoder is fed exactly
    these bytes."""
    form, files, filenames = {}, {}, {}
    mm = re.search(r'boundary="?([^";]+)"?', ctype or "")
    if not mm:
        return form, files, filenames
    boundary = ("--" + mm.group(1)).encode()
    for part in body.split(boundary):
        if not part or part in (b"--\r\n", b"--", b"\r\n"):
            continue
        head, sep, payload = part.partition(b"\r\n\r\n")
        if not sep:
            continue
        head_txt = head.decode("utf-8", "replace")
        nm = re.search(r'name="([^"]*)"', head_txt)
        if not nm:
            continue
        name = nm.group(1)
        payload = payload[:-2] if payload.endswith(b"\r\n") else payload
        fn = re.search(r'filename="([^"]*)"', head_txt)
        if fn:
            files.setdefault(name, []).append(payload)
            filenames.setdefault(name, []).append(fn.group(1))
        else:
            form.setdefault(name, []).append(payload.decode("utf-8", "replace"))
    return form, files, filenames


class Handler(BaseHTTPRequestHandler):
    server_version = "Sleevenote"
    sys_version = ""
    # Seconds a read or write may block. A client that opens a connection and
    # then trickles (or sends nothing) gives up its thread after this.
    timeout = 60

    def log_message(self, *a):
        pass

    def _run(self, method):
        try:
            if not _host_allowed(self.headers.get("Host", "")):
                return self._send(403, [("Content-Type", "text/html; charset=utf-8")], page(
                    "Not this address",
                    "<h1>Not at this address</h1><p>Sleevenote only answers to addresses "
                    "that cannot belong to a website elsewhere: this machine's IP address, "
                    "localhost, a one-word machine name, a <code>.local</code> or "
                    "<code>.lan</code> name, or a Tailscale name. Open it at one of those — "
                    "the terminal it was started from lists them.</p>"
                    '<p class="muted small">This stops a page on another site from steering '
                    "the app through a name it borrowed.</p>", bare=True))
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            peer = self.client_address[0]
            req = Req(method, path, query, {}, self.headers, ip=_client_ip(self.headers, peer))
            conn = db.connect()
            try:
                return self._serve(method, path, req, peer, conn)
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        except Exception:                  # a traceback must never reach the browser
            traceback.print_exc()          # it goes to the log instead
            req = locals().get("req")
            body = page("Error", "<h1>Something went wrong</h1>"
                                 '<p class="muted">The details are in the server\'s log.</p>'
                                 '<p><a href="/">Back to the start</a></p>',
                        req=req, bare=not getattr(req, "user", None))
            return self._send(500, [("Content-Type", "text/html; charset=utf-8")], body)

    def _serve(self, method, path, req, peer, conn):
        """The gate, then the body, then the route. The gate comes before the
        body is read, so somebody who has not logged in cannot make this process
        hold 24 MB of their upload in memory."""
        sess_name, _ = _cookie_names()
        req.token = req.cookie(sess_name) or ""
        req.user = auth.session_account(conn, req.token) if req.token else None
        public = _is_public(method, path)
        if not public and not req.user:
            if method == "GET":
                q = urllib.parse.urlparse(self.path).query
                here = path + ("?" + q if q else "")
                dest = "/login" if here in ("/", "") else \
                    "/login?next=" + urllib.parse.quote(here, safe="")
                return self._send(303, [("Location", dest)], b"")
            return self._send(303, [("Location", "/login?msg=" + urllib.parse.quote(
                "Log in first — nothing was changed."))], b"")
        if method == "POST":
            if not _same_origin(self.headers, peer):
                return self._send(403, [("Content-Type", "text/html; charset=utf-8")], page(
                    "Refused",
                    "<h1>That change came from somewhere else</h1><p>A form on another "
                    "site, or at another address for this one, tried to change the "
                    "collection, so nothing was changed. If it was you, go back and "
                    "use the app at one address throughout.</p>"
                    '<p><a href="/">Back to the start</a></p>', req=req,
                    bare=not req.user))
            ctype = self.headers.get("Content-Type", "")
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._send(400, [("Content-Type", "text/plain")], b"bad length")
            if not req.user:
                cap = 16 * 1024            # a login form, and nothing bigger
            elif ctype.startswith("multipart/"):
                cap = MAX_PHOTO + 1024
            else:
                cap = 2 * 1024 * 1024
            if length < 0 or length > cap:
                return self._send(413, [("Content-Type", "text/plain")], b"too large")
            body = self.rfile.read(length) if length else b""
            if ctype.startswith("multipart/"):
                req.form, req.files, req.filenames = _parse_multipart(ctype, body)
            else:
                req.form = urllib.parse.parse_qs(body.decode("utf-8", "replace"),
                                                 keep_blank_values=True)
        for mth, rx, fn in COMPILED:
            if mth != method or not rx.fullmatch(path):
                continue
            status, headers, body = fn(req, conn, rx.fullmatch(path))
            return self._send(status, headers, body)
        return self._send(404, [("Content-Type", "text/html; charset=utf-8")],
                          page("Not found", "<h1>Not found</h1>"
                               '<p><a href="/">Back to the start</a></p>', req=req,
                               bare=not req.user))

    def _send(self, status, headers, body):
        """Every response goes out through here, so the security headers are on
        all of them. Nothing private is cached: a page that names your records
        must not come back from a shared computer's history after a logout."""
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        named = {k.lower() for k, _ in headers}
        for k, v in headers:
            if k.lower() not in ("x-content-type-options",):
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        for k, v in SECURITY_HEADERS:
            self.send_header(k, v)
        if _secure():
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        if "cache-control" not in named:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        self._run("GET")

    def do_POST(self):
        self._run("POST")


def _scheme():
    return "https" if (TLS_CERT and TLS_KEY) else "http"


def _lan_urls():
    import socket
    out = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        out.append(f"{_scheme()}://{s.getsockname()[0]}:{PORT}")
        s.close()
    except Exception:
        pass
    return out


HANDSHAKE_TIMEOUT = 30.0


class ToHTTPS(BaseHTTPRequestHandler):
    """Plain http that arrived at the https port: sent on to the same address
    over https, so an old bookmark or a typed http:// still lands somewhere
    instead of on a browser error about a connection it cannot read."""
    server_version = "Sleevenote"

    def log_message(self, *a):
        pass

    def _go(self):
        host = self.headers.get("Host", "")
        if not _host_allowed(host):
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(302)
        self.send_header("Location", f"https://{host}{self.path}")
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = do_POST = do_HEAD = _go


class Server(ThreadingHTTPServer):
    """The HTTP server, with TLS put on each connection in that connection's
    own thread rather than on the listening socket.

    Wrapping the listening socket does the handshake inside accept(), on the one
    thread that accepts everybody — so a phone that opens a connection and says
    nothing (browsers open spare ones speculatively) stalls every other request
    until it gives up. Here the first byte is looked at in the worker thread: a
    TLS handshake starts with 0x16, and anything else is plain http, which is
    answered with a redirect to https at the same address."""

    tls = None

    def handle_error(self, request, client_address):
        # A browser hanging up mid-request is not an error worth a traceback —
        # under TLS 1.3 it is what declining a self-signed certificate looks
        # like, after the handshake has already finished on this side.
        if isinstance(sys.exc_info()[1], OSError):
            return
        super().handle_error(request, client_address)

    def finish_request(self, request, client_address):
        if self.tls is None:
            return super().finish_request(request, client_address)
        import socket
        import ssl
        request.settimeout(HANDSHAKE_TIMEOUT)
        try:
            first = request.recv(1, socket.MSG_PEEK)
        except OSError:
            return
        if not first:
            return
        if first != b"\x16":
            request.settimeout(None)
            ToHTTPS(request, client_address, self)
            return
        try:
            conn = self.tls.wrap_socket(request, server_side=True)
        except (ssl.SSLError, OSError):
            return      # most often a browser declining a self-signed certificate
        conn.settimeout(None)
        try:
            self.RequestHandlerClass(conn, client_address, self)
        finally:
            self.shutdown_request(conn)


def _wrap_tls(srv):
    """Give the server a TLS context. Returns an error string, or "" — a
    missing certificate is a sentence, not a traceback, because the person
    reading it is usually holding a phone."""
    import ssl
    for label, path in (("SN_TLS_CERT", TLS_CERT), ("SN_TLS_KEY", TLS_KEY)):
        if not os.path.exists(path):
            return f"{label} points at {path}, which does not exist."
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(TLS_CERT, TLS_KEY)
    except (ssl.SSLError, OSError) as ex:
        return f"That certificate and key could not be loaded: {ex}"
    srv.tls = ctx
    return ""


def main():
    c = db.init_db()
    made = auth.ensure_accounts(c, auth.parse_names(os.environ.get("SN_USERS")))
    c.commit()
    accounts = c.execute("SELECT * FROM account ORDER BY name").fetchall()
    c.close()
    try:
        srv = Server((HOST, PORT), Handler)
    except OSError as ex:
        if getattr(ex, "errno", None) in (98, 48):
            print(f"Port {PORT} is already in use. Either stop what is there, or:\n\n"
                  f"    SN_PORT={PORT + 1} python3 app.py\n")
        elif getattr(ex, "errno", None) == 13:
            print(f"Not allowed to listen on port {PORT}. Ports below 1024 need special\n"
                  f"permission. Use a higher one:\n\n    SN_PORT=8080 python3 app.py\n")
        else:
            print(f"Could not start the server: {ex}")
        return 1
    if TLS_CERT or TLS_KEY:
        if not (TLS_CERT and TLS_KEY):
            print("Set both SN_TLS_CERT and SN_TLS_KEY, or neither.")
            return 1
        err = _wrap_tls(srv)
        if err:
            print(f"{err}\n\nStart without them to serve plain HTTP — see README.")
            return 1
    print(f"\n  {APP_NAME} — a vinyl collection\n")
    for name in made:
        print(f"  Made the account {name}.")
    if not accounts:
        print("  Nobody can log in yet. Make an account and its setup code with:\n\n"
              "      python3 users.py add NAME\n")
    for a in accounts:
        if auth.account_state(a) == "closed":
            print(f"  {a['name']} cannot log in until given a setup code: "
                  f"python3 users.py code {a['name']}")
    if made or accounts:
        print()
    print(f"    {_scheme()}://127.0.0.1:{PORT}")
    if not LOCAL_ONLY:
        for u in _lan_urls():
            print(f"    {u}")
        print("\n  Listening on all interfaces. SN_BIND=local restricts it to this machine.")
    if BEHIND_HTTPS and _scheme() == "http":
        print("\n  Plain HTTP here, behind a proxy that serves HTTPS (SN_BEHIND_HTTPS), so the\n"
              "  cookies are marked Secure — open it at the proxy's https:// address.")
    elif _scheme() == "http":
        print("\n  Plain HTTP. Typing a barcode and reading a photo both work. The phone's\n"
              "  own camera scanner needs HTTPS — see README, 'Scanning with a phone'.")
    else:
        print("\n  HTTPS. Plain http:// to this port is sent on to https://.")
    print("\n  Ctrl-C to stop.\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
