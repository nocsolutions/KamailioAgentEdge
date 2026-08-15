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

## Read-only access to the cluster VICIdial DBs

`tools/sync_subscribers.py` reads `phones` from every cluster DB
(`database_private` from the fleet API). Give it a dedicated read-only user on
each cluster DB, scoped to exactly what it needs and to the edge's IP:

```sql
-- run on each cluster's VICIdial MySQL (e.g. av996 db = 10.4.59.37)
CREATE USER 'coregears_ro'@'10.4.100.147' IDENTIFIED BY '<pw>';
GRANT SELECT ON vicidial.phones TO 'coregears_ro'@'10.4.100.147';
FLUSH PRIVILEGES;
```

Then put the password in `tools/.env` (`VICI_DB_USER` / `VICI_DB_PASS`).

Network is already open edge→cluster-DB (verified: `10.4.100.147` reaches
`10.4.59.37:3306`). The grant is the only prerequisite; the tool never writes to
the cluster DB.

> Status: on the av996 test cluster this grant is not yet created (needs
> cluster-DB admin), so `sync_subscribers.py` has been validated for its
> destination behaviour (the `subscriber` upsert) and query shape, not yet
> against live av996 data. The trs tool (`refresh_trs.py`) and the whole SIP
> path are validated — see docs/TESTING.md.
