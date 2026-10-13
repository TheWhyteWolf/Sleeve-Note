#!/usr/bin/env python3
"""The accounts, from the server's side. Stdlib only.

    python3 users.py                 who can log in, and who is waiting
    python3 users.py add NAME        a new account, and its setup code
    python3 users.py code NAME       a new setup code for NAME — this is also
                                     the reset for a forgotten password: the old
                                     password stops working and every device
                                     NAME was logged in on is logged out
    python3 users.py logout NAME     log NAME out everywhere, password kept

In the container:  docker exec sleevenote python3 users.py code Alice

A setup code is typed into the ordinary password box on the login page, once;
the next page asks for a password of the person's own. Codes are printed here
and nowhere else — not in the log, not on any page — so hand them over the way
you would hand over a key.
"""
from __future__ import annotations
import sys

import auth
import db


def main(argv):
    c = db.init_db()
    try:
        if not argv or argv[0] in ("list", "ls"):
            rows = c.execute("SELECT * FROM account ORDER BY name").fetchall()
            if not rows:
                print("No accounts yet.  python3 users.py add NAME")
                return 0
            words = {"ready": "has a password",
                     "waiting": "has a setup code, not used yet",
                     "closed": "cannot log in — python3 users.py code NAME"}
            for a in rows:
                n = c.execute("SELECT COUNT(*) FROM session WHERE account_id=?",
                              (a["id"],)).fetchone()[0]
                print(f"  {a['name']:<16} {words[auth.account_state(a)]}"
                      f"{f', logged in on {n} device' + ('' if n == 1 else 's') if n else ''}")
            return 0
        if len(argv) != 2 or argv[0] not in ("add", "code", "logout"):
            print(__doc__.strip())
            return 2
        verb, name = argv[0], argv[1].strip()
        acct = auth.account_by_name(c, name)
        if verb == "add":
            if acct:
                print(f"{acct['name']} already exists. For a new code: python3 users.py code {acct['name']}")
                return 1
            auth.ensure_accounts(c, [name])
            acct = auth.account_by_name(c, name)
        elif not acct:
            print(f"There is no account called {name!r}.")
            return 1
        if verb == "logout":
            c.execute("DELETE FROM session WHERE account_id=?", (acct["id"],))
            c.commit()
            print(f"{acct['name']} is logged out everywhere. Their password still works.")
            return 0
        code = auth.issue_code(c, acct["id"])
        c.commit()
        print(f"\n  {acct['name']}'s setup code:   {code}\n")
        print("  Type it as the password on the login page. It works once, and the next")
        print("  page asks for a password of their own. Until then, this code is the key.")
        if verb == "code":
            print("  Any password they had has stopped working, and they are logged out.")
        print()
        return 0
    finally:
        c.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
