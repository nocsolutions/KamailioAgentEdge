#!/usr/bin/env python3
"""Mirror VICIdial webphone credentials into the edge's kamailio.subscriber.

Read-only against the fleet: it SELECTs the webphone rows from each cluster's
VICIdial `phones` table and upserts (username, domain, password) into the local
`subscriber` table that auth_db checks. The login service is never touched; the
credentials it provisioned into VICIdial are simply read back.

  username = phones.login      (the SIP extension)
  password = phones.pass       (phone_pass, cleartext; auth_db calculate_ha1=1
                                turns it into ha1 = MD5(ext:realm:pass) per auth)
  domain   = AUTH_REALM         (the realm the edge challenges with)

Extensions are globally unique across the fleet (login.avatar.tech hands them
out from one incrementing pool), so username+domain never collides between
clusters and one flat subscriber table is correct.

Config comes from tools/.env (see .env.example). Run from cron every few minutes
and on demand. --dry-run prints the delta without writing.
"""

import argparse
import json
import os
import sys
import urllib.request

try:
    import pymysql
except ImportError:
    sys.exit("pymysql not installed - `apt-get install python3-pymysql` "
             "or `pip install pymysql`")


def load_env(path):
    """Minimal .env loader: KEY=VALUE lines, real env wins."""
    vals = {}
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip()
    vals.update({k: v for k, v in os.environ.items() if k in vals or k.startswith(
        ("CLUSTERS_", "VICI_", "KAM_", "AUTH_", "ONLY_"))})
    return vals


def get(env, key, default=None, required=False):
    v = env.get(key, default)
    if required and (v is None or v == ""):
        sys.exit(f"missing required config: {key}")
    return v


def load_clusters(api_url, only):
    with urllib.request.urlopen(api_url, timeout=20) as r:
        data = json.load(r)
    clusters = data.get("clusters", [])
    if only:
        want = {c.strip() for c in only.split(",") if c.strip()}
        clusters = [c for c in clusters if c["name"] in want]
    return clusters


def fetch_phones(host, port, user, password, db):
    """Return {ext: phone_pass} for active webphone stations on one cluster."""
    conn = pymysql.connect(host=host, port=int(port), user=user,
                           password=password, database=db,
                           connect_timeout=8, read_timeout=15,
                           cursorclass=pymysql.cursors.Cursor)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT login, pass FROM phones "
                "WHERE template_id = 'webphone' AND active = 'Y' "
                "AND login <> '' AND pass <> ''")
            return {str(login): str(pw) for (login, pw) in cur.fetchall()}
    finally:
        conn.close()


def sync_subscriber(kam, realm, creds, dry_run, allow_prune):
    """Upsert creds into subscriber; prune stale rows only when allow_prune.

    allow_prune is false when any cluster failed to read - otherwise a transient
    DB outage on one cluster would delete every agent it owns. Adds/updates are
    always safe (they only reflect data we actually read)."""
    conn = pymysql.connect(host=kam["host"], port=int(kam["port"]),
                           user=kam["user"], password=kam["password"],
                           database=kam["db"], connect_timeout=8,
                           autocommit=False)
    added = updated = removed = 0
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT username, password FROM subscriber "
                        "WHERE domain = %s", (realm,))
            existing = {str(u): str(p) for (u, p) in cur.fetchall()}

            want = set(creds)
            have = set(existing)

            for ext in sorted(want - have):
                added += 1
                if not dry_run:
                    cur.execute(
                        "INSERT INTO subscriber (username, domain, password) "
                        "VALUES (%s, %s, %s)", (ext, realm, creds[ext]))
            for ext in sorted(want & have):
                if existing[ext] != creds[ext]:
                    updated += 1
                    if not dry_run:
                        cur.execute(
                            "UPDATE subscriber SET password = %s "
                            "WHERE username = %s AND domain = %s",
                            (creds[ext], ext, realm))
            if allow_prune:
                for ext in sorted(have - want):
                    removed += 1
                    if not dry_run:
                        cur.execute("DELETE FROM subscriber "
                                    "WHERE username = %s AND domain = %s",
                                    (ext, realm))
        if dry_run:
            conn.rollback()
        else:
            conn.commit()
    finally:
        conn.close()
    return added, updated, removed


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--env", default=os.path.join(
        os.path.dirname(__file__), ".env"))
    ap.add_argument("--dry-run", action="store_true",
                    help="report the delta, write nothing")
    ap.add_argument("--clusters",
                    help="comma list to override ONLY_CLUSTERS (e.g. av996)")
    args = ap.parse_args()

    env = load_env(args.env)
    api_url = get(env, "CLUSTERS_API",
                  "http://zbx.noc.solutions:5000/clusters?trs=true")
    only = args.clusters or get(env, "ONLY_CLUSTERS", "")
    realm = get(env, "AUTH_REALM", "avatar.tech")

    vici = dict(user=get(env, "VICI_DB_USER", required=True),
                password=get(env, "VICI_DB_PASS", required=True),
                db=get(env, "VICI_DB_NAME", "vicidial"),
                port=get(env, "VICI_DB_PORT", "3306"))
    kam = dict(host=get(env, "KAM_DB_HOST", "localhost"),
               port=get(env, "KAM_DB_PORT", "3306"),
               user=get(env, "KAM_DB_USER", "kamailio"),
               password=get(env, "KAM_DB_PASS", required=True),
               db=get(env, "KAM_DB_NAME", "kamailio"))

    clusters = load_clusters(api_url, only)
    if not clusters:
        sys.exit("no clusters matched - check CLUSTERS_API / ONLY_CLUSTERS")

    creds = {}
    ok = skipped = 0
    for c in clusters:
        host = c.get("database_private") or c.get("database_public")
        try:
            got = fetch_phones(host, vici["port"], vici["user"],
                               vici["password"], vici["db"])
            creds.update(got)
            ok += 1
            print(f"  {c['name']:8} {host:15} {len(got):5} webphones")
        except Exception as e:
            skipped += 1
            print(f"  {c['name']:8} {host:15} SKIP {type(e).__name__}: {e}")

    print(f"clusters: {ok} read, {skipped} skipped; "
          f"{len(creds)} unique extensions")

    allow_prune = (skipped == 0)
    added, updated, removed = sync_subscriber(
        kam, realm, creds, args.dry_run, allow_prune)
    tag = "(dry-run) " if args.dry_run else ""
    print(f"{tag}subscriber: +{added} added, ~{updated} updated, "
          f"-{removed} removed")
    if not allow_prune:
        print("NOTE: %d cluster(s) skipped - pruning disabled this run so no "
              "agent is wrongly deleted; stale rows clear on a clean run."
              % skipped, file=sys.stderr)


if __name__ == "__main__":
    main()
