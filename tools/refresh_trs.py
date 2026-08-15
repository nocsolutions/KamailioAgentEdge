#!/usr/bin/env python3
"""Refresh the trs source-IP allowlist in the kamailio `address` table.

The KEMI script trusts a SIP source only if it is a registered agent (WSS) or a
known cluster telephony server. This tool pulls the telephony-server IPs
(`trs`) for every cluster from the fleet API and writes them into the `address`
table under group TRS_GROUP, which the permissions module checks via
allow_source_address(). Run from cron; the fleet changes rarely.

After writing, it asks kamailio to reload the in-memory table
(kamcmd permissions.addressReload) so the change takes effect without a restart.
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.request

try:
    import pymysql
except ImportError:
    sys.exit("pymysql not installed - `apt-get install python3-pymysql`")


def load_env(path):
    vals = {}
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip()
    for k in list(vals) + ["CLUSTERS_API", "KAM_DB_HOST", "KAM_DB_PORT",
                           "KAM_DB_USER", "KAM_DB_PASS", "KAM_DB_NAME",
                           "TRS_GROUP"]:
        if k in os.environ:
            vals[k] = os.environ[k]
    return vals


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--env", default=os.path.join(
        os.path.dirname(__file__), ".env"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-reload", action="store_true",
                    help="skip kamcmd permissions.addressReload")
    args = ap.parse_args()

    env = load_env(args.env)
    api = env.get("CLUSTERS_API",
                  "http://zbx.noc.solutions:5000/clusters?trs=true")
    grp = int(env.get("TRS_GROUP", "1"))
    if not env.get("KAM_DB_PASS"):
        sys.exit("missing KAM_DB_PASS")

    with urllib.request.urlopen(api, timeout=20) as r:
        clusters = json.load(r).get("clusters", [])

    ips = set()
    for c in clusters:
        for ip in c.get("trs") or []:
            ips.add(ip.strip())
    if not ips:
        sys.exit("fleet API returned no trs IPs - refusing to empty the table")
    print(f"{len(clusters)} clusters, {len(ips)} telephony-server IPs")

    if args.dry_run:
        for ip in sorted(ips):
            print("  ", ip)
        print("(dry-run) address group %d would be replaced" % grp)
        return

    conn = pymysql.connect(host=env.get("KAM_DB_HOST", "localhost"),
                           port=int(env.get("KAM_DB_PORT", "3306")),
                           user=env.get("KAM_DB_USER", "kamailio"),
                           password=env["KAM_DB_PASS"],
                           database=env.get("KAM_DB_NAME", "kamailio"),
                           autocommit=False)
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM address WHERE grp = %s", (grp,))
            cur.executemany(
                "INSERT INTO address (grp, ip_addr, mask, port) "
                "VALUES (%s, %s, 32, 0)",
                [(grp, ip) for ip in sorted(ips)])
        conn.commit()
    finally:
        conn.close()
    print(f"address group {grp} replaced with {len(ips)} rows")

    if not args.no_reload:
        try:
            subprocess.run(["kamcmd", "permissions.addressReload"],
                           check=True, timeout=10)
            print("permissions table reloaded")
        except Exception as e:
            print(f"WARNING: reload failed ({e}); run "
                  "`kamcmd permissions.addressReload` manually", file=sys.stderr)


if __name__ == "__main__":
    main()
