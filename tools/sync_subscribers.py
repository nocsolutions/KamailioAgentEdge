#!/usr/bin/env python3
"""Mirror agent SIP credentials from the cgauth master store into kamailio.subscriber.

Source of truth is the login service's own SQLite database - the same file the
running app has open (verify with `ls -l /proc/$(pgrep -f agentauth/app.js)/fd`):

    10.4.100.6:/root/db/cgauth.db        <- live, ~173MB, written continuously

NOT /opt/agentauth/db/cgauth.db, which is a 40KB stub shipped with the code and
frozen since February. Pointing this tool there would mirror six-month-old
credentials and every agent would fail to register with no obvious cause.

Why this and not the per-cluster VICIdial `phones` tables: cgauth is the master
that provisions them, it is one source instead of 115, it carries active/deleted
so deactivated agents can be pruned, and every cluster DB user is @localhost so
the per-cluster pull could never run unattended anyway.

Read-only throughout: `sqlite3 -readonly` over ssh, nothing is written to the
login host. The database is journal_mode=delete, so a reader takes only a shared
lock and the `users` table is small (~15k rows) - the query is a fast scan.

    tools/sync_subscribers.py --dry-run
    tools/sync_subscribers.py --clusters av994
"""

import argparse
import os
import subprocess
import sys

try:
    import pymysql
except ImportError:
    sys.exit("pymysql not installed - apt-get install python3-pymysql")


# Every non-empty phone slot of every enabled agent, one row per station.
# phone1/phone2 are populated for all active users; phone3/phone4 for a handful.
EXTRACT_SQL = """
WITH ext AS (
    SELECT phone1 AS e, phone_pass AS p, cluster AS c, user_id AS u
      FROM users WHERE active=1 AND deleted=0 AND phone1 <> '' AND phone_pass <> ''
    UNION ALL
    SELECT phone2, phone_pass, cluster, user_id
      FROM users WHERE active=1 AND deleted=0 AND phone2 <> '' AND phone_pass <> ''
    UNION ALL
    SELECT phone3, phone_pass, cluster, user_id
      FROM users WHERE active=1 AND deleted=0 AND phone3 <> '' AND phone_pass <> ''
    UNION ALL
    SELECT phone4, phone_pass, cluster, user_id
      FROM users WHERE active=1 AND deleted=0 AND phone4 <> '' AND phone_pass <> ''
)
SELECT e, p, c, u FROM ext ORDER BY e;
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


def fetch_rows(env):
    """Run the extract against cgauth.db and return [(ext, pass, cluster, user_id)].

    CGAUTH_SSH empty -> read a local copy at CGAUTH_DB instead (useful for tests).
    """
    db = env.get("CGAUTH_DB", "/root/db/cgauth.db")
    ssh_target = env.get("CGAUTH_SSH", "").strip()
    sudo = "sudo -n " if env.get("CGAUTH_SUDO", "1") == "1" else ""
    remote = f"{sudo}sqlite3 -readonly -noheader -separator '\t' {db}"

    if ssh_target:
        port = env.get("CGAUTH_SSH_PORT", "22")
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
               "-p", str(port), ssh_target, remote]
    else:
        cmd = ["sh", "-c", remote]

    proc = subprocess.run(cmd, input=EXTRACT_SQL, capture_output=True,
                          text=True, timeout=120)
    if proc.returncode != 0:
        sys.exit(f"cgauth extract failed (rc={proc.returncode}): "
                 f"{proc.stderr.strip()[:400]}")

    rows = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 4 and parts[0] and parts[1]:
            rows.append(tuple(parts))
    if not rows:
        sys.exit("cgauth returned no rows - refusing to touch subscriber")
    return rows


def resolve(rows, only_clusters):
    """Collapse to {ext: password}, dropping ambiguous extensions.

    An extension held by two enabled agents with DIFFERENT passwords cannot be
    served: the edge keys on username only (use_domain=0), so whichever row was
    written last would win and the other agent could never register - silently,
    and indistinguishably from a wrong password. There were 18 such extensions
    in the master store when this was written (some spanning two clusters, e.g.
    13460 on av91 and av58), so they are excluded and reported instead of
    letting an arbitrary winner through. Fix them in cgauth, not here.
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
        passwords = {e[0] for e in entries}
        if len(passwords) > 1:
            conflicts.append((ext, entries))
            continue
        creds[ext] = entries[0][0]
    return creds, conflicts


def sync(kam, realm, creds, dry_run, prune_floor, may_prune):
    conn = pymysql.connect(host=kam["host"], port=int(kam["port"]),
                           user=kam["user"], password=kam["password"],
                           database=kam["db"], connect_timeout=8,
                           autocommit=False)
    added = updated = removed = 0
    skipped_prune = None
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT username, password FROM subscriber WHERE domain=%s",
                        (realm,))
            existing = {str(u): str(p) for (u, p) in cur.fetchall()}

            for ext in sorted(set(creds) - set(existing)):
                added += 1
                if not dry_run:
                    cur.execute("INSERT INTO subscriber (username, domain, password) "
                                "VALUES (%s, %s, %s)", (ext, realm, creds[ext]))
            for ext in sorted(set(creds) & set(existing)):
                if existing[ext] != creds[ext]:
                    updated += 1
                    if not dry_run:
                        cur.execute("UPDATE subscriber SET password=%s "
                                    "WHERE username=%s AND domain=%s",
                                    (creds[ext], ext, realm))

            stale = sorted(set(existing) - set(creds))
            # Pruning compares the WHOLE subscriber table against the source, so
            # it is only meaningful when the source is the whole fleet. Under
            # --clusters every other cluster's agent looks "stale" and would be
            # deleted - so scoped runs never prune. (subscriber has no column to
            # record which cluster a row came from: username/domain/password/
            # ha1/ha1b only.)
            #
            # Even unscoped, a truncated read that still exits 0 would look like
            # "everyone was deleted", so require the source to be plausibly
            # complete before removing anything.
            if not may_prune:
                skipped_prune = "scoped"
            elif existing and len(creds) < len(existing) * prune_floor:
                skipped_prune = "floor"
            else:
                for ext in stale:
                    removed += 1
                    if not dry_run:
                        cur.execute("DELETE FROM subscriber "
                                    "WHERE username=%s AND domain=%s", (ext, realm))
        conn.rollback() if dry_run else conn.commit()
    finally:
        conn.close()
    return added, updated, removed, skipped_prune, len(stale)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", default=os.path.join(os.path.dirname(__file__), ".env"))
    ap.add_argument("--dry-run", action="store_true", help="report, write nothing")
    ap.add_argument("--clusters", help="only these clusters (comma list)")
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

    rows = fetch_rows(env)
    creds, conflicts = resolve(rows, only)
    print(f"cgauth: {len(rows)} station rows"
          + (f" (filtered to clusters: {only})" if only else "")
          + f" -> {len(creds)} usable extensions")

    if conflicts:
        print(f"\n!! {len(conflicts)} extension(s) held by multiple enabled agents "
              f"with DIFFERENT passwords - EXCLUDED, these agents cannot register:")
        for ext, entries in sorted(conflicts)[:20]:
            who = ", ".join(f"user {u}@{c}" for _, c, u in entries)
            print(f"     {ext}: {who}")
        if len(conflicts) > 20:
            print(f"     ... and {len(conflicts) - 20} more")
        print("   Fix in cgauth (one extension per enabled agent).\n")

    added, updated, removed, skipped_prune, stale = sync(
        kam, realm, creds, args.dry_run, args.prune_floor, may_prune=not only)
    tag = "(dry-run) " if args.dry_run else ""
    print(f"{tag}subscriber: +{added} added, ~{updated} updated, -{removed} removed")
    if skipped_prune == "scoped":
        print(f"note: --clusters set, so nothing was pruned ({stale} row(s) in "
              f"subscriber are outside the selected clusters). Run without "
              f"--clusters to prune against the whole fleet.")
    elif skipped_prune == "floor":
        print(f"WARNING: pruning skipped - the source yielded {len(creds)} "
              f"extensions but subscriber holds {stale + len(creds)}, which looks "
              f"like a truncated read. {stale} stale row(s) left in place; re-run "
              f"once the source looks complete.", file=sys.stderr)


if __name__ == "__main__":
    main()
