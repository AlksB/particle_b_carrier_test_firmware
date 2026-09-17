#!/usr/bin/env python3
"""
Feeds the Particle product event stream into Postgres - live from the API,
or from the SSE log written by scripts/particle_event_log.sh.

Both are the same format: `event: <name>` / `data: <json>` line pairs, with
`:ok` keepalives every few seconds. The JSON envelope carries coreid,
published_at and the product firmware version; the payload the firmware
published is a *string* inside its `data` field, so every event type gets
parsed twice and lands in its own table (see postgres/init/01_schema.sql).

Every insert is ON CONFLICT DO NOTHING on (device_id, ts), so the same
events can be fed in as many times as you like: load the historical log
once, then run --stream, and a restart or an overlap never duplicates.

Usage:
  ingest.py --stream                            # live from api.particle.io, reconnects forever
  ingest.py particle_events.log                 # one-off load of a log file
  ingest.py particle_events.log --follow        # load, then tail the file
  ingest.py particle_events.log --dry-run       # parse only, print counts
  curl -sN .../events | ingest.py -             # from stdin (a curl stream, a zcat of an archive)

Device names are not in the events; --stream fetches the product device
list on connect and every NAME_SYNC_SEC after, and --sync-names does it
once. Both need PARTICLE_TOKEN.

Connection: $DATABASE_URL (postgresql://user:pw@host:5432/db)
Stream:     $PARTICLE_TOKEN, $PARTICLE_PRODUCT_ID (default 44896)
"""

import argparse
import json
import os
import re
import signal
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone

DEVICE_ID = re.compile(r"^[0-9a-f]{24}$")
HOOK_RESPONSE = re.compile(r"^([0-9a-f]{24})/hook-response/(.+)/\d+$")
HOOK_SENT = re.compile(r"^hook-sent/(.+)$")
HOOK_ERROR = re.compile(r"^hook-error/(.+)/\d+$")

PRODUCT_ID = os.environ.get("PARTICLE_PRODUCT_ID", "44896")
API = "https://api.particle.io"

BATCH = 2000        # rows per transaction when loading a backlog
FLUSH_SEC = 2       # max age of an unflushed batch when streaming
NAME_SYNC_SEC = 15 * 60   # how often --stream re-reads the device list


# ------------------------------------------------------------------ parsing

def ts_of(env):
    # published_at is ISO-8601 with a Z suffix and millisecond precision
    return datetime.fromisoformat(env["published_at"].replace("Z", "+00:00"))


def payload(env):
    """The firmware's published string, decoded if it is JSON."""
    raw = env.get("data", "")
    if raw[:1] in "{[":
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    return raw


def to_bool(v):
    return None if v is None else bool(v)


def to_int(v):
    try:
        return None if v is None else int(v)
    except (TypeError, ValueError):
        return None


def to_float(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def dig(d, *path):
    for k in path:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def route(name, env, keep_raw=False):
    """
    Turns one event into (table, row_tuple). Returns None for events that are
    only noise (hook-sent has no content beyond its existence, which the
    error/response counts already give you).
    """
    ts = ts_of(env)
    dev = env.get("coreid", "")
    fw = to_int(env.get("version"))

    # --- webhook echo -----------------------------------------------------
    m = HOOK_RESPONSE.match(name)
    if m:
        return "hook_event", (ts, "response", m.group(2), m.group(1), env.get("data"))
    m = HOOK_ERROR.match(name)
    if m:
        return "hook_event", (ts, "error", m.group(1), None, env.get("data"))
    m = HOOK_SENT.match(name)
    if m:
        return "hook_event", (ts, "sent", m.group(1), None, None)

    if not DEVICE_ID.match(dev):
        return None  # particle/firmware/create etc. - cloud-side bookkeeping
    if name.startswith("particle/device/updates/"):
        return None  # enabled/forced/pending flags, re-sent on every handshake

    p = payload(env)

    if name == "spark/status":
        return "device_status", (ts, dev, p)

    if name == "telemetry" and isinstance(p, dict):
        return "telemetry", (
            ts, dev, fw,
            to_bool(p.get("reedclosed")),
            to_int(p.get("battery")),
            to_float(p.get("vBatLoad")),
            to_float(p.get("vBatIdle")),
            to_int(p.get("signalStrength")),
            to_int(p.get("signalQuality")),
            to_int(p.get("connectAttempts")),
            to_int(p.get("connectSuccesses")),
            to_int(p.get("modemSearchSec")),
            to_int(p.get("modemReadySec")),
            to_int(p.get("modemOffgoingSec")),
            to_int(p.get("slotOffsetSec")),
        )

    if name == "rf_survey" and isinstance(p, dict):
        return "rf_survey", (
            ts, dev,
            to_int(p.get("earfcn")), to_int(p.get("band")),
            to_int(p.get("ulBw")), to_int(p.get("dlBw")),
            p.get("tac"), p.get("cellId"), to_int(p.get("pci")),
            to_int(p.get("rsrpIdx")), to_int(p.get("rsrqIdx")),
            to_float(p.get("sinr")),
            to_int(p.get("rrc")), to_int(p.get("ri")), to_int(p.get("cqi")),
            to_int(p.get("avgRsrpIdx")),
            to_int(p.get("puschPwr")), to_int(p.get("pucchPwr")),
            p.get("ucgedRaw"),
        )

    if name == "connect_failure" and isinstance(p, dict):
        return "connect_failure", (
            ts, dev, to_int(p.get("at")), to_int(p.get("ageSec")),
            to_int(p.get("searchSec")), p.get("raw"),
        )

    if name == "reed_changed" and isinstance(p, dict):
        return "reed_changed", (ts, dev, to_bool(p.get("reedclosed")), p.get("timestamp"))

    if name == "firmware_info" and isinstance(p, dict):
        return "firmware_info", (ts, dev, to_int(p.get("version")), p.get("commit"))

    if name == "spark/device/diagnostics/update" and isinstance(p, dict):
        d = p.get("device", {})
        cell = dig(d, "network", "cellular") or {}
        cgi = cell.get("cell_global_identity") or {}
        sig = dig(d, "network", "signal") or {}
        ncon = dig(d, "network", "connection") or {}
        ccon = dig(d, "cloud", "connection") or {}
        coap = dig(d, "cloud", "coap") or {}
        sysm = d.get("system") or {}
        return "diagnostics", (
            ts, dev,
            cell.get("radio_access_technology"), cell.get("operator"),
            to_int(cgi.get("mobile_country_code")), cgi.get("mobile_network_code"),
            to_int(cgi.get("location_area_code")), to_int(cgi.get("cell_id")),
            to_float(sig.get("strengthv")), to_float(sig.get("qualityv")),
            to_float(sig.get("strength")), to_float(sig.get("quality")),
            ncon.get("status"), to_int(ncon.get("attempts")),
            to_int(ncon.get("disconnects")), ncon.get("disconnect_reason"),
            ccon.get("status"), to_int(ccon.get("error")), to_int(ccon.get("attempts")),
            to_int(ccon.get("disconnects")), ccon.get("disconnect_reason"),
            to_int(coap.get("transmit")), to_int(coap.get("retransmit")),
            to_int(coap.get("round_trip")),
            to_int(dig(d, "cloud", "publish", "rate_limited")),
            dig(d, "power", "battery", "state"), dig(d, "power", "source"),
            to_int(sysm.get("uptime")),
            to_int(dig(sysm, "memory", "used")), to_int(dig(sysm, "memory", "total")),
            to_int(sysm.get("version")), to_int(dig(sysm, "panic", "code")),
            json.dumps(p) if keep_raw else None,
        )

    return "other_event", (ts, dev, name, env.get("data"))


INSERT = {
    "device_status":   "insert into device_status values (%s,%s,%s) on conflict do nothing",
    "telemetry":       "insert into telemetry values (" + ",".join(["%s"] * 15) + ") on conflict do nothing",
    "rf_survey":       "insert into rf_survey values (" + ",".join(["%s"] * 19) + ") on conflict do nothing",
    "connect_failure": "insert into connect_failure values (%s,%s,%s,%s,%s,%s) on conflict do nothing",
    "reed_changed":    "insert into reed_changed values (%s,%s,%s,%s) on conflict do nothing",
    "firmware_info":   "insert into firmware_info values (%s,%s,%s,%s) on conflict do nothing",
    "diagnostics":     "insert into diagnostics values (" + ",".join(["%s"] * 33) + ") on conflict do nothing",
    "hook_event":      "insert into hook_event values (%s,%s,%s,%s,%s) on conflict do nothing",
    "other_event":     "insert into other_event values (%s,%s,%s,%s) on conflict do nothing",
}

# device table: keep the newest fw_version/app_hash/last_reset and the
# first/last time each device was heard from
DEVICE_UPSERT = """
insert into device (device_id, fw_version, app_hash, last_reset, last_seen, first_seen)
values (%s, %s, %s, %s, %s, %s)
on conflict (device_id) do update set
    fw_version = case when excluded.last_seen >= device.last_seen
                      then coalesce(excluded.fw_version, device.fw_version)
                      else device.fw_version end,
    app_hash   = case when excluded.last_seen >= device.last_seen
                      then coalesce(excluded.app_hash, device.app_hash)
                      else device.app_hash end,
    last_reset = case when excluded.last_seen >= device.last_seen
                      then coalesce(excluded.last_reset, device.last_reset)
                      else device.last_reset end,
    last_seen  = greatest(device.last_seen, excluded.last_seen),
    first_seen = least(device.first_seen, excluded.first_seen)
"""


class Loader:
    def __init__(self, conn, dry_run=False, keep_raw=False, verbose=False):
        self.conn = conn
        self.dry_run = dry_run
        self.keep_raw = keep_raw
        self.verbose = verbose          # log every commit; for the long-running modes
        self.n_rows = 0                 # since the last flush
        self.n_skipped = 0
        self.pending = defaultdict(list)
        self.devices = {}          # device_id -> [fw, app_hash, last_reset, last_seen, first_seen]
        self.counts = Counter()
        self.last_flush = time.time()

    def feed(self, name, env):
        r = route(name, env, self.keep_raw)
        if r is None:
            self.counts["(skipped) " + name] += 1
            self.n_skipped += 1
            return
        table, row = r
        self.pending[table].append(row)
        self.counts[table] += 1
        self.n_rows += 1

        dev = env.get("coreid", "")
        if DEVICE_ID.match(dev):
            ts = row[0]
            fw = to_int(env.get("version")) or None
            d = self.devices.setdefault(dev, [None, None, None, ts, ts])
            if ts >= d[3]:
                d[3] = ts
                if fw:
                    d[0] = fw
                if name == "spark/device/app-hash":
                    d[1] = env.get("data")
                if name == "spark/device/last_reset":
                    d[2] = env.get("data")
            d[4] = min(d[4], ts)

        # batch by size, but also by age so a slow stream (stdin from a
        # live curl) does not sit on rows for minutes waiting for a full batch
        if (sum(len(v) for v in self.pending.values()) >= BATCH
                or time.time() - self.last_flush > FLUSH_SEC):
            self.flush()

    def flush_if_stale(self):
        """Called on keepalives and idle ticks, so the tail of a burst does
        not wait for the next event to be committed."""
        if (self.pending or self.n_skipped) and time.time() - self.last_flush > FLUSH_SEC:
            self.flush()

    def flush(self):
        if not self.dry_run and self.pending:
            with self.conn.cursor() as cur:
                for table, rows in self.pending.items():
                    cur.executemany(INSERT[table], rows)
                if self.devices:
                    cur.executemany(DEVICE_UPSERT, [(k, *v) for k, v in self.devices.items()])
            self.conn.commit()
        if self.verbose and (self.n_rows or self.n_skipped):
            tables = ", ".join(f"{len(v)} {k}" for k, v in self.pending.items())
            log(f"+{self.n_rows} rows ({tables}), {self.n_skipped} skipped")
        self.pending.clear()
        self.devices.clear()
        self.n_rows = self.n_skipped = 0
        self.last_flush = time.time()


def follow(path, poll_sec):
    """Reads the file from the start, then keeps yielding lines as it grows.
    Reopens on truncation/rotation (inode change or size shrink)."""
    while True:
        try:
            f = open(path, errors="replace")
        except FileNotFoundError:
            time.sleep(poll_sec)
            continue
        with f:
            ino = os.fstat(f.fileno()).st_ino
            pos = 0
            while True:
                line = f.readline()
                if line:
                    pos = f.tell()
                    yield line
                    continue
                yield None  # idle marker so the caller can flush
                time.sleep(poll_sec)
                try:
                    st = os.stat(path)
                except FileNotFoundError:
                    break
                if st.st_ino != ino:
                    # rotated: drain what was appended to the old file
                    # during the sleep, then reopen the new one from the top
                    for line in f:
                        yield line
                    break
                if st.st_size < pos:
                    break  # truncated in place (copytruncate): start over


# The init SQL only runs on an empty volume. An existing database is brought
# up to date here at startup: columns added since the first release, then
# the schema file itself, which is written to be idempotent (if not exists /
# or replace), so views pick up their newest definition.
MIGRATIONS = [
    "alter table device add column if not exists name text",
]
SCHEMA_SQL = os.environ.get("SCHEMA_SQL") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "postgres", "init", "01_schema.sql")


def migrate(conn):
    with conn.cursor() as cur:
        for sql in MIGRATIONS:
            cur.execute(sql)
        try:
            with open(SCHEMA_SQL) as f:
                cur.execute(f.read())
        except FileNotFoundError:
            log(f"schema file not found at {SCHEMA_SQL}, views not refreshed")
    conn.commit()


def fetch_device_names(token, product_id):
    """{device_id: name} for every device in the product, all pages."""
    names = {}
    page = 1
    while True:
        url = f"{API}/v1/products/{product_id}/devices?perPage=100&page={page}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.load(resp)
        for d in body.get("devices", []):
            if d.get("name"):
                names[d["id"]] = d["name"]
        if page >= (body.get("meta") or {}).get("total_pages", 1):
            return names
        page += 1


def sync_names(conn, token, product_id):
    """Upserts names from the API. A device the API knows but no event has
    mentioned yet gets a row with null timestamps, so the dashboard's device
    list is the product's list, not just the devices heard from."""
    try:
        names = fetch_device_names(token, product_id)
    except (urllib.error.URLError, OSError, ValueError) as e:
        log(f"name sync failed: {e}")
        return
    if conn is None:
        log(f"name sync: {len(names)} names (dry run)")
        return
    with conn.cursor() as cur:
        cur.executemany(
            "insert into device (device_id, name) values (%s, %s) "
            "on conflict (device_id) do update set name = excluded.name "
            "where device.name is distinct from excluded.name",
            list(names.items()))
    conn.commit()
    log(f"name sync: {len(names)} devices")


def sse_stream(token, product_id, retry_sec=5, timeout_sec=60):
    """Yields lines from the product event stream, reconnecting whenever it
    drops. Particle sends `:ok` every ~9 s, so a 60 s read timeout catches a
    silently dead connection. Yields None between connections so the caller
    can flush."""
    url = f"{API}/v1/products/{product_id}/events"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}",
                                               "Accept": "text/event-stream"})
    while True:
        try:
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                log(f"connected to {url}")
                for raw in resp:
                    yield raw.decode("utf-8", errors="replace")
            log("stream ended")
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                sys.exit(f"error: {e.code} from Particle - bad or expired PARTICLE_TOKEN")
            log(f"stream error: {e}")
        except (urllib.error.URLError, OSError) as e:
            log(f"stream dropped: {e}")
        yield None
        time.sleep(retry_sec)


def consume(loader, lines, every=None):
    """Runs SSE lines through the loader. None is an idle marker (no data
    right now); any other non event/data line - the `:ok` keepalive, the
    shell script's connect markers - is a tick for the stale-batch flush.
    `every` is an optional (seconds, callable) run on ticks at that period."""
    name = None
    next_every = 0
    for line in lines:
        if every and time.time() >= next_every:
            loader.flush()
            every[1]()
            next_every = time.time() + every[0]
        if line is None:
            loader.flush()
            continue
        if line.startswith("event: "):
            name = line[7:].strip()
        elif line.startswith("data: ") and name:
            try:
                loader.feed(name, json.loads(line[6:]))
            except json.JSONDecodeError:
                pass
            name = None
        else:
            loader.flush_if_stale()
    loader.flush()


def log(msg):
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}", flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("logfile", nargs="?", help="event log path, or - for stdin")
    ap.add_argument("--stream", action="store_true",
                    help="read the live product event stream from api.particle.io instead of a file")
    ap.add_argument("--sync-names", action="store_true",
                    help="fetch device names from the product device list once, then exit")
    ap.add_argument("--follow", action="store_true", help="keep tailing the file after loading it")
    ap.add_argument("--poll", type=float, default=2.0, help="seconds between tail polls (default 2)")
    ap.add_argument("--dry-run", action="store_true", help="parse only, no database")
    ap.add_argument("--keep-raw", action="store_true",
                    help="store the full diagnostics payload in diagnostics.raw (about 2 KB per row)")
    ap.add_argument("--database-url", default=os.environ.get("DATABASE_URL"),
                    help="postgresql://user:pw@host/db (default $DATABASE_URL)")
    args = ap.parse_args()
    if bool(args.stream) + bool(args.logfile) + bool(args.sync_names) != 1:
        ap.error("give one of: a logfile, --stream, --sync-names")

    conn = None
    if not args.dry_run:
        if not args.database_url:
            sys.exit("error: set DATABASE_URL or pass --database-url")
        import psycopg
        conn = psycopg.connect(args.database_url)
        migrate(conn)

    loader = Loader(conn, dry_run=args.dry_run, keep_raw=args.keep_raw,
                    verbose=args.stream or args.follow)
    t0 = time.time()

    # PID 1 in a container gets no default SIGTERM disposition, so without
    # this `docker stop` waits out its grace period and SIGKILLs us mid-batch
    def stop(signum, frame):
        loader.flush()
        log("stopped")
        sys.exit(0)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    if args.stream or args.sync_names:
        token = os.environ.get("PARTICLE_TOKEN")
        if not token:
            sys.exit("error: set PARTICLE_TOKEN")
        if args.sync_names:
            sync_names(conn, token, PRODUCT_ID)
            return
        consume(loader, sse_stream(token, PRODUCT_ID),
                every=(NAME_SYNC_SEC, lambda: sync_names(conn, token, PRODUCT_ID)))
    elif args.logfile == "-":
        consume(loader, sys.stdin)
    elif args.follow:
        def lines():
            loaded_once = False
            for line in follow(args.logfile, args.poll):
                if line is None and not loaded_once:
                    loaded_once = True
                    loader.flush()
                    report(loader, t0)
                    log("following...")
                yield line
        consume(loader, lines())
    else:
        with open(args.logfile, errors="replace") as f:
            consume(loader, f)
        report(loader, t0)


def report(loader, t0):
    total = sum(v for k, v in loader.counts.items() if not k.startswith("(skipped)"))
    print(f"{total} rows in {time.time() - t0:.1f}s", flush=True)
    for k, v in sorted(loader.counts.items(), key=lambda kv: -kv[1]):
        print(f"{v:8d}  {k}", flush=True)


if __name__ == "__main__":
    main()
