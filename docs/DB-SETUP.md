# Database setup

The edge uses one local MariaDB (`kamailio`) and reads each cluster's VICIdial
`phones` table read-only.

## Local kamailio DB

`deploy/setup_db.sh` runs `kamdbctl create` (config in `etc/kamailio/kamctlrc`,
passwords from `tools/.env`). It creates the standard tables plus the three this
edge relies on:

| table        | module     | purpose                                   |
|--------------|------------|-------------------------------------------|
| `subscriber` | auth_db    | digest credentials (mirrored, read-only)  |
| `location`   | usrloc     | agent WSS registrations                    |
| `address`    | permissions| trs source-IP allowlist (group 1)         |

```bash
cp tools/.env.example tools/.env      # set KAM_DB_PASS
sudo deploy/setup_db.sh
```

The `kamailio` DB user's password must match `DBURL` in
`etc/kamailio/kamailio-local.cfg`.

## Credential source: the cgauth master store

`tools/sync_subscribers.py` reads the login service's own SQLite database - the
master that provisions every cluster's VICIdial `phones`:

```
10.4.100.6:/root/db/cgauth.db
```

**It must be that path.** `/opt/agentauth/db/cgauth.db` also exists but is a 40KB
stub frozen since February; syncing from it would mirror six-month-old
credentials and every agent would fail to register with no obvious cause.
Confirm which file the running app has open before trusting either:

```bash
ls -l /proc/$(pgrep -f 'agentauth/app.js')/fd | grep cgauth
```

Access is read-only over ssh (`sqlite3 -readonly`, SQL on stdin); nothing is
written to that host. It is `journal_mode=delete` and the `users` table is small
(~15k rows), so a read takes only a brief shared lock.

Why this rather than the per-cluster VICIdial `phones` tables: one source instead
of 115, it carries `active`/`deleted` so disabled agents get pruned, and every
cluster DB user is `@localhost` - the per-cluster pull could never run unattended.

### The invariant that matters

The browser gets its SIP password from **VICIdial** (`vicidial_users.phone_pass`,
handed to the webphone by the agent screen), while the edge validates against
**cgauth**. Those agree because cgauth is what provisions VICIdial - verified on
av994/av996, where every overlapping extension matched exactly. They diverge only
if someone edits VICIdial directly, and such an agent then cannot register.
**Change agents in cgauth, never in VICIdial.**

### Known data-quality issues in the source (as of 2026-08-17)

`user_id` is not unique - `main_idx` is a plain index, not a unique one - so the
same agent can have several enabled rows with different `phone_pass`. That
produces **18 extensions held by 2-3 enabled users with conflicting passwords**
(e.g. user 883 on pd3 has three rows; extensions 13460/13461 span av91 *and*
av58). The sync excludes those extensions rather than letting an arbitrary winner
through - writing one would leave the other agent unable to register, silently and
indistinguishably from a wrong password. Fix them in cgauth; the tool reports each
one with its user_ids and clusters.


## Running the sync on a schedule

`deploy/sync-cron.sh` is the cron wrapper; it is installed on the edge at
`/opt/KamailioAgentEdge/deploy/sync-cron.sh` and driven every 10 minutes:

```
*/10 * * * * /opt/KamailioAgentEdge/deploy/sync-cron.sh
```

It takes a lock so a slow run cannot overlap the next, sends all output to
`/var/log/coregears/sync_subscribers.log` (so cron only mails if the wrapper
itself dies), and writes `/var/lib/coregears/last_success` only on a successful
run.

**Alert on `last_success` being stale, not on a single failed run.** Every failure
mode of the sync is fail-closed - a short read, an implausibly small source, or an
over-budget delete all abort with `subscriber` untouched - so one bad run is
harmless and leaves the last good credentials serving. What is dangerous is
nobody noticing that it has not succeeded for hours:

```bash
# stale if the last success is older than 30 minutes
test $(( $(date +%s) - $(date -d "$(cat /var/lib/coregears/last_success)" +%s) )) -lt 1800
```

### Remaining prerequisite

The edge pulls from the login host over ssh with a dedicated key
(`~/.ssh/id_ed25519_cgauth`, `CGAUTH_SSH_KEY` in `tools/.env`). The edge already
trusts the login host's host key; what is missing is authorising the public key
**on 10.4.100.6**, restricted so it can do nothing but the read:

```
command="sudo -n sqlite3 -readonly -noheader -separator '|' /root/db/cgauth.db",\
no-agent-forwarding,no-port-forwarding,no-pty,no-X11-forwarding <the edge's pubkey>
```

The forced command is what makes this safe: the SQL arrives on stdin, so the key
cannot run anything else on that host. Until it is added the cron runs, fails
closed, and logs `Permission denied (publickey)` - no credentials change.

Offline fallback used during testing: export just the users table and read it
locally, which needs no ssh from the edge at all.

```bash
ssh <login-host> 'sudo sqlite3 -readonly /root/db/cgauth.db ".dump users"' > users.sql
sqlite3 /var/lib/coregears/cgauth_users.db < users.sql   # on the edge
# then in tools/.env: CGAUTH_SSH=   CGAUTH_SUDO=0   CGAUTH_DB=/var/lib/coregears/cgauth_users.db
```
