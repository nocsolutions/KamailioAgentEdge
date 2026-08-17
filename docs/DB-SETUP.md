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

