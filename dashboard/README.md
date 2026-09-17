# Fleet dashboard

Grafana over Postgres, fed live from the Particle product event stream.

```
api.particle.io (SSE) ──► ingest --stream ──► Postgres ──► Grafana
particle_events.log   ──► ingest <file>  ──┘   (history, once)
```

## Run

```sh
cd dashboard
cp .env.example .env      # passwords, PARTICLE_TOKEN, where the old log lives
docker compose up -d --build
docker compose run --rm ingest /data/particle_events.log    # backfill history
```

Grafana: http://localhost:3000, user `admin`, password from `.env`. The
**Fleet** dashboard is provisioned and opens on the last 7 days.

`ingest` holds an SSE connection to `api.particle.io` and reconnects when
it drops (a dead connection is noticed within 60 s - Particle keepalives
come every ~9 s). Events are committed within two seconds of arriving.
`docker compose logs -f ingest` shows connects and drops.

The backfill and the stream write the same tables with `on conflict do
nothing` on `(device_id, ts)`, so the overlap between the log's tail and
the stream's start is harmless, and so is loading the same file twice.
Nothing replays what happened while neither was running: the SSE API has
no history, so a gap in the stream is a gap in the data.

## Deploying on a server

Sized for 2 000 devices at a report every 6 h: 2 vCPU / 4 GB / 40 GB is
plenty (Hetzner CX22 or similar), Debian 12.

```sh
# 1. box: firewall, docker, a non-root user
apt update && apt install -y ufw git unattended-upgrades
ufw allow OpenSSH && ufw allow 80 && ufw allow 443 && ufw enable
curl -fsSL https://get.docker.com | sh
adduser fleet && usermod -aG docker fleet
# then: PasswordAuthentication no in /etc/ssh/sshd_config, key in ~fleet/.ssh

# 2. code + secrets (as the fleet user)
git clone <repo> /opt/fleet && cd /opt/fleet/dashboard
cp .env.example .env
openssl rand -base64 24   # x3, for the three passwords
```

In `.env`: the three passwords, `PARTICLE_TOKEN` (make a separate token
for the server with `particle token create` so it can be revoked on its
own), `DOMAIN`, `GRAFANA_ROOT_URL=https://$DOMAIN`, `GRAFANA_BIND=127.0.0.1`.
Point an A record for the domain at the server, then:

```sh
# 3. up, with HTTPS
docker compose --profile https up -d --build
docker compose logs -f ingest          # "connected to ..." then commits

# 4. history, once: copy the old log over and load it
scp particle_events.log fleet@server:/opt/fleet/
docker compose run --rm ingest /data/particle_events.log

# 5. nightly backup (crontab -e)
0 3 * * * cd /opt/fleet/dashboard && ./scripts/backup.sh >> backups/backup.log 2>&1
```

Firewall note: Docker publishes ports with its own iptables rules that
bypass ufw, which is why Grafana is bound to 127.0.0.1 behind Caddy and
Postgres is not published at all. Only Caddy's 80/443 face the internet.

Keeping it running: `restart: unless-stopped` plus Docker starting on boot
covers reboots. The one thing to alert on is the "Log freshness" tile
(time since the newest event) - if it passes ~8 h the stream or the ingest
is dead. Updates: `docker compose pull && docker compose --profile https up
-d` now and then; `unattended-upgrades` handles the OS.

## The ingest without Docker

```sh
pip install 'psycopg[binary]'
export DATABASE_URL=postgresql://fleet:pw@localhost:5432/fleet
PARTICLE_TOKEN=... python3 ingest/ingest.py --stream           # live
python3 ingest/ingest.py ../particle_events.log                # a log file, once
python3 ingest/ingest.py ../particle_events.log --follow       # a log file someone keeps appending to
python3 ingest/ingest.py ../particle_events.log --dry-run      # just count
curl -sN -H "Authorization: Bearer $PARTICLE_TOKEN" \
     https://api.particle.io/v1/products/44896/events | python3 ingest/ingest.py -
```

The last form is what `--stream` does internally, minus the reconnect
loop; useful with `tee -a` if you also want a raw log on disk.

## What ends up where

| Table | From | Notes |
|---|---|---|
| `telemetry` | `telemetry` | counters are lifetime totals, use `telemetry_delta` |
| `rf_survey` | `rf_survey` | parsed +UCGED |
| `diagnostics` | `spark/device/diagnostics/update` | RSRP/RSRQ dBm, operator, cell, cloud stats; `--keep-raw` stores the whole payload |
| `device_status` | `spark/status` | online / offline / auto-update |
| `connect_failure` | `connect_failure` | AT dump at budget expiry |
| `reed_changed`, `firmware_info` | same-named events | |
| `hook_event` | `hook-sent/*`, `hook-response/*`, `hook-error/*` | webhook delivery audit |
| `other_event` | `spark/flash/status`, `app-hash`, `last_reset` | |
| `device` | every device event + the product device list | name, last_seen, fw_version, app_hash, last_reset |

Views: `fleet_now` (one row per device, latest of everything),
`telemetry_delta` (per-cycle differences of the lifetime counters),
`device_label` (name, or the id when there is none) which every panel
joins for its series labels, `connections` (one row per report with the
serving cell: band, EARFCN → DL MHz, TAC, cell id split into eNodeB and
sector, PCI, RSRP/RSRQ in dBm/dB, SINR, plus operator and the OS's RSRP
from the diagnostics of the same wake cycle) and `connect_failures`
(the AT dump of a failed connect taken apart: CEER cause, CGATT, and the
cell the modem was camped on).

Device names come from the Particle API, not from events: `--stream`
fetches the product device list on connect and every 15 minutes, so a
rename in the console shows up within that. `ingest.py --sync-names` does
it once. Renaming is also how a device gets a label before its first
event.

Schema changes: `01_schema.sql` is written to be idempotent and the ingest
re-runs it (plus any `alter table` in `MIGRATIONS`) at startup, so after
`git pull` a `docker compose up -d --build` brings an existing database
up to date - nothing to run by hand.
`particle/device/updates/*` is dropped as noise.

Sizes, for the 15-day / 20-device log: 39 MB of text becomes ~34 MB in
Postgres, half of it the webhook echo in `hook_event`.

## Dashboard

`grafana/dashboards/fleet.json` is generated by `grafana/gen_fleet.py`; edit
the Python, regenerate, restart Grafana (or wait ~10 s, it re-reads the
file). Rows: Fleet (stat tiles + fleet-now table), Connectivity (online
timeline, RSRP, SINR, attach time per cycle, connect failures), Radio
(serving cell and band per device over time, every cell seen with how
often and how well, connections per cell per day, RSRP by band, and the
per-report connections table), Power, Backend (webhook errors by host,
deliveries, firmware versions).

## Alerts

Not provisioned yet. The obvious ones, all one-liners against `fleet_now`:
`silent_sec > 2*6*3600` (missed two reports), `battery < 20`,
`failures_24h > 3`; plus `hook_event` errors per hour for the backend.
Grafana → Alerting → Contact points for Telegram/email first.
