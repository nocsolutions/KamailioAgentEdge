#!/usr/bin/env python3
"""Mirror agent SIP credentials from the cgauth master store into kamailio.subscriber.

Source of truth is the login service's own SQLite database - the same file the
running app has open (verify with `ls -l /proc/$(pgrep -f agentauth/app.js)/fd`):

    10.4.100.6:/root/db/cgauth.db        <- live, ~173MB, written continuously

NOT /opt/agentauth/db/cgauth.db, which is a 40KB stub shipped with the code and
frozen since February. Pointing this tool there would mirror six-month-old
credentials and every agent would fail to register with no obvious cause. The
--min-users floor is the backstop against exactly that mistake.

Why this and not the per-cluster VICIdial `phones` tables: cgauth is the master
that provisions them, it is one source instead of 115, it carries active/deleted
so deactivated agents can be pruned, and every cluster DB user is @localhost so
the per-cluster pull could never run unattended anyway.

Read-only throughout: `sqlite3 -readonly` over ssh with the SQL on stdin, so no
credential ever reaches argv, and nothing is written to the login host.

    tools/sync_subscribers.py --dry-run
    tools/sync_subscribers.py --clusters av994
"""

import argparse
import binascii
import os
import subprocess
import sys

# Rows per statement when writing. 1000 keeps the packet well under MySQL's
# default max_allowed_packet while cutting a fleet-wide sync to ~21 statements.
CHUNK = 1000

try:
    import pymysql
except ImportError:
    sys.exit("pymysql not installed - apt-get install python3-pymysql")


# Only enabled agents with a usable password. CAST() because active/deleted are
# not reliably typed, COALESCE because deleted can be NULL on older rows.
PREDICATE = ("CAST(active AS INTEGER) = 1 "
             "AND CAST(COALESCE(deleted, 0) AS INTEGER) = 0 "
             "AND TRIM(COALESCE(phone_pass, '')) <> ''")

# Everything is hex-encoded on the wire. sqlite3's list mode does no quoting or
# escaping, so a tab or newline inside phone_pass would either silently drop a
# row or split one record across two lines - and the tail of that split can look
# like a valid row, which would INSERT a REGISTER identity that exists in no
# source system. hex() makes every field [0-9A-F] so the separator can never
# appear in the data. (-json would be cleaner but needs sqlite >= 3.33; the
# login host runs 3.31.1.)
#
# The ##ROWS## sentinel lets the source declare its own row count so a truncated
# read - ssh dying mid-stream, which still exits 0 - is detected instead of being
# mistaken for "these agents were deleted".
#
# `.timeout` is the CLI dot-command form and prints nothing; `PRAGMA
# busy_timeout = ...` would echo its value as a one-field row, which the
# integrity check below correctly rejects as unparseable.
EXTRACT_SQL = f"""
.timeout 10000
SELECT '##ROWS##', CAST(COUNT(*) AS TEXT), '', '', '', '', ''
  FROM users WHERE {PREDICATE};
SELECT hex(TRIM(COALESCE(phone1, ''))), hex(TRIM(COALESCE(phone2, ''))),
       hex(TRIM(COALESCE(phone3, ''))), hex(TRIM(COALESCE(phone4, ''))),
       hex(phone_pass), hex(COALESCE(cluster, '')), hex(COALESCE(user_id, ''))
  FROM users WHERE {PREDICATE};
"""


def load_env(path):
    vals = {}
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip()
    vals.update({k: v for k, v in os.environ.items()
                 if k.startswith(("CGAUTH_", "KAM_", "AUTH_", "ONLY_"))})
    return vals


def unhex(field):
    return binascii.unhexlify(field).decode("utf-8", "strict") if field else ""


def fetch_rows(env, min_users):
    """Return [(ext, password, cluster, user_id)] for every station of every
    enabled agent. Fails closed on a short read or an implausibly small source."""
    db = env.get("CGAUTH_DB", "/root/db/cgauth.db")
    ssh_target = env.get("CGAUTH_SSH", "").strip()
    sudo = "sudo -n " if env.get("CGAUTH_SUDO", "1") == "1" else ""
    remote = f"{sudo}sqlite3 -readonly -noheader -separator '|' {db}"

    if ssh_target:
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
               "-p", str(env.get("CGAUTH_SSH_PORT", "22"))]
        # cron has no ssh-agent, so point at the key explicitly. The matching
        # authorized_keys entry on the login host is restricted with a forced
        # command, so this key can run nothing but the read-only cgauth query.
        key = env.get("CGAUTH_SSH_KEY", "").strip()
        if key:
            cmd += ["-i", key, "-o", "IdentitiesOnly=yes"]
        cmd += [ssh_target, remote]
    else:
        cmd = ["sh", "-c", remote]

    proc = subprocess.run(cmd, input=EXTRACT_SQL, capture_output=True,
                          text=True, timeout=180)
    if proc.returncode != 0:
        sys.exit(f"cgauth extract failed (rc={proc.returncode}): "
                 f"{proc.stderr.strip()[:400]}")

    declared = None
    users = []
    malformed = 0
    for line in proc.stdout.split("\n"):
        line = line.strip("\r")
        if not line:
            continue
        parts = line.split("|")
        if parts[0] == "##ROWS##":
            declared = int(parts[1])
            continue
        if len(parts) != 7:
            malformed += 1
            continue
        try:
            users.append(tuple(unhex(p) for p in parts))
        except (binascii.Error, UnicodeDecodeError):
            malformed += 1

    # Every one of these means we cannot trust the read, and an under-read on an
    # unscoped run turns straight into DELETEs. Refuse rather than guess.
    if declared is None:
        sys.exit("cgauth extract: no ##ROWS## sentinel - truncated or wrong query")
    if malformed:
        sys.exit(f"cgauth extract: {malformed} unparseable line(s) - refusing to "
                 f"sync from a read we cannot trust")
    if len(users) != declared:
        sys.exit(f"cgauth extract: short read - source declared {declared} agent "
                 f"rows, parsed {len(users)}. Refusing to sync.")
    if declared < min_users:
        sys.exit(f"cgauth extract: only {declared} enabled agents (expected at "
                 f"least {min_users}). Either the wrong database file is being "
                 f"read - /opt/agentauth/db/cgauth.db is a stale 40KB stub - or "
                 f"something mass-disabled agents. Refusing to sync; override "
                 f"with --min-users if this is genuinely expected.")

    rows = []
    for p1, p2, p3, p4, pw, cluster, uid in users:
        for ext in (p1, p2, p3, p4):
            if ext:
                rows.append((ext, pw, cluster, uid))
    return rows, declared


def resolve(rows, only_clusters):
    """Collapse to {ext: password}, holding back ambiguous extensions.

    An extension held by two enabled agents with DIFFERENT passwords cannot be
    served: the edge keys on username only (use_domain=0), so whichever row was
    written last would win and the other agent could never register - silently,
    and indistinguishably from a wrong password. There were 18 such extensions in
    the master store when this was written; the cause is that cgauth's user_id is
    not unique (main_idx is a plain index), so one agent can have several enabled
    rows with different phone_pass.

    They are returned separately so the caller can both skip writing them AND
    keep any existing row: deleting is strictly worse than an arbitrary winner,
    because after a delete neither agent can register.
    """
    want = None
    if only_clusters:
        want = {c.strip() for c in only_clusters.split(",") if c.strip()}

    by_ext = {}
    for ext, pw, cluster, uid in rows:
        if want and cluster not in want:
            continue
        by_ext.setdefault(ext, []).append((pw, cluster, uid))

    creds, conflicts = {}, []
    for ext, entries in by_ext.items():
        if len({e[0] for e in entries}) > 1:
            conflicts.append((ext, entries))
        else:
            creds[ext] = entries[0][0]
    return creds, conflicts


def sync(kam, realm, creds, hold, dry_run, may_prune, prune_floor, max_delete):
    """Apply creds to subscriber in one transaction.

    `hold` are extensions we must neither write nor delete (ambiguous in the
    source). The delete budget is checked BEFORE any write is issued, so hitting
    it unwinds through the rollback with the table exactly as it was found.
    """
    conn = pymysql.connect(host=kam["host"], port=int(kam["port"]),
                           user=kam["user"], password=kam["password"],
                           database=kam["db"], connect_timeout=8,
                           autocommit=False)
    added = updated = removed = 0
    skipped = None  # set below from the computed sets
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT username, password FROM subscriber WHERE domain=%s",
                        (realm,))
            existing = {str(u): str(p) for (u, p) in cur.fetchall()}

            # Pruning compares the WHOLE table against the source, so it only
            # means anything on a whole-fleet run; under --clusters every other
            # cluster's agent would look stale. Ambiguous extensions are held out
            # so a data-entry mistake in cgauth can never revoke a working login.
            stale = sorted(set(existing) - set(creds) - hold)
            if not may_prune:
                stale, skipped = [], "scoped"
            elif existing and len(creds) < len(existing) * prune_floor:
                stale, skipped = [], "floor"
            elif len(stale) > max_delete:
                sys.exit(f"refusing to delete {len(stale)} subscriber row(s) - "
                         f"over the --max-delete budget of {max_delete}. Nothing "
                         f"was written. Re-run with a higher budget only if this "
                         f"many agents really were removed.")

            to_add = sorted(set(creds) - set(existing))
            to_upd = sorted(e for e in set(creds) & set(existing)
                            if existing[e] != creds[e])
            added, updated, removed = len(to_add), len(to_upd), len(stale)

            # Batched deliberately. One statement per row meant ~20k round-trips
            # for a fleet-wide run, which took over four minutes when the tool
            # ran anywhere other than the edge itself. `account_idx` is UNIQUE on
            # (username, domain), so adds and password changes collapse into one
            # multi-row upsert; deletes go out as chunked IN-lists. That is ~21
            # statements instead of ~20700.
            if not dry_run:
                rows = [(e, realm, creds[e]) for e in to_add + to_upd]
                for i in range(0, len(rows), CHUNK):
                    cur.executemany(
                        "INSERT INTO subscriber (username, domain, password) "
                        "VALUES (%s, %s, %s) "
                        "ON DUPLICATE KEY UPDATE password = VALUES(password)",
                        rows[i:i + CHUNK])
                for i in range(0, len(stale), CHUNK):
                    batch = stale[i:i + CHUNK]
                    cur.execute(
                        "DELETE FROM subscriber WHERE domain = %s AND username IN "
                        f"({','.join(['%s'] * len(batch))})", [realm] + batch)
        if dry_run:
            conn.rollback()
        else:
            conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    return added, updated, removed, skipped, len(existing)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", default=os.path.join(os.path.dirname(__file__), ".env"))
    ap.add_argument("--dry-run", action="store_true", help="report, write nothing")
    ap.add_argument("--clusters", help="only these clusters (comma list); implies no pruning")
    ap.add_argument("--min-users", type=int, default=9000,
                    help="refuse to sync if the source has fewer enabled agents "
                         "than this (default 9000; the fleet has ~10365)")
    ap.add_argument("--max-delete", type=int, default=200,
                    help="refuse to sync if more rows than this would be deleted "
                         "(default 200)")
    ap.add_argument("--prune-floor", type=float, default=0.5,
                    help="skip pruning if the source has less than this fraction "
                         "of the rows already present (default 0.5)")
    args = ap.parse_args()

    env = load_env(args.env)
    realm = env.get("AUTH_REALM", "avatar.tech")
    only = args.clusters or env.get("ONLY_CLUSTERS", "")
    if not env.get("KAM_DB_PASS"):
        sys.exit("missing KAM_DB_PASS")
    kam = dict(host=env.get("KAM_DB_HOST", "localhost"),
               port=env.get("KAM_DB_PORT", "3306"),
               user=env.get("KAM_DB_USER", "kamailio"),
               password=env["KAM_DB_PASS"],
               db=env.get("KAM_DB_NAME", "kamailio"))

    rows, agents = fetch_rows(env, args.min_users)
    creds, conflicts = resolve(rows, only)
    hold = {ext for ext, _ in conflicts}
    print(f"cgauth: {agents} enabled agents, {len(rows)} station rows"
          + (f" (filtered to clusters: {only})" if only else "")
          + f" -> {len(creds)} usable extensions")

    if conflicts:
        print(f"\n!! {len(conflicts)} extension(s) held by multiple enabled agents "
              f"with DIFFERENT passwords. Not written, and any existing row is "
              f"left alone - these agents cannot register until it is fixed:")
        for ext, entries in sorted(conflicts)[:20]:
            print(f"     {ext}: " + ", ".join(f"user {u}@{c}" for _, c, u in entries))
        if len(conflicts) > 20:
            print(f"     ... and {len(conflicts) - 20} more")
        print("   Cause is usually duplicate rows for one agent (user_id is not "
              "unique in cgauth). Fix at the source.\n")

    added, updated, removed, skipped, had = sync(
        kam, realm, creds, hold, args.dry_run, not only,
        args.prune_floor, args.max_delete)
    tag = "(dry-run) " if args.dry_run else ""
    print(f"{tag}subscriber: {had} row(s) before -> "
          f"+{added} added, ~{updated} updated, -{removed} removed")
    if skipped == "scoped":
        print("note: --clusters set, so nothing was pruned. Run without "
              "--clusters to prune against the whole fleet.")
    elif skipped == "floor":
        print(f"WARNING: pruning skipped - the source yielded {len(creds)} "
              f"extensions against {had} already present, which looks like a "
              f"truncated read. Nothing was deleted.", file=sys.stderr)


if __name__ == "__main__":
    main()
