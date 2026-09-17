#!/usr/bin/env python3
"""
Loads the SSE event log written by scripts/particle_event_log.sh into Postgres.

The log is the raw Particle event stream: `event: <name>` / `data: <json>` line
pairs, plus the `:ok` keepalives and the `[...] --- connecting ---` markers the
shell script adds. The JSON envelope carries coreid, published_at and the
product firmware version; the payload the firmware published is a *string*
inside its `data` field, so every event type gets parsed twice and lands in
its own table (see postgres/init/01_schema.sql).

Every insert is ON CONFLICT DO NOTHING on (device_id, ts), so the same log
can be fed in as many times as you like - which is what makes --follow safe
to restart: it always re-reads the file from the top and only the new tail
actually inserts.

Usage:
  ingest.py particle_events.log                 # one-off load
  ingest.py particle_events.log --follow        # load, then tail forever
  ingest.py particle_events.log --dry-run       # parse only, print counts
  cat log | ingest.py -                         # from stdin
  curl -sN .../events | ingest.py -             # straight from the SSE stream, no file

Connection: $DATABASE_URL (postgresql://user:pw@host:5432/db)
"""

import argparse
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone

DEVICE_ID = re.compile(r"^[0-9a-f]{24}$")
HOOK_RESPONSE = re.compile(r"^([0-9a-f]{24})/hook-response/(.+)/\d+$")
HOOK_SENT = re.compile(r"^hook-sent/(.+)$")
HOOK_ERROR = re.compile(r"^hook-error/(.+)/\d+$")

BATCH = 2000        # rows per transaction when loading a backlog
FLUSH_SEC = 2       # max age of an unflushed batch when streaming


# ------------------------------------------------------------------ parsing

def sse_records(lines):
    """Yields (event_name, envelope_dict) pairs from the raw log lines."""
    name = None
    for line in lines:
        if line.startswith("event: "):
            name = line[7:].strip()
        elif line.startswith("data: ") and name:
            try:
                yield name, json.loads(line[6:])
            except json.JSONDecodeError:
                pass
            name = None
        # everything else: ':ok', '[ts] --- connecting ---', blank


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
    def __init__(self, conn, dry_run=False, keep_raw=False):
        self.conn = conn
        self.dry_run = dry_run
        self.keep_raw = keep_raw
        self.pending = defaultdict(list)
        self.devices = {}          # device_id -> [fw, app_hash, last_reset, last_seen, first_seen]
        self.counts = Counter()
        self.last_flush = time.time()

    def feed(self, name, env):
        r = route(name, env, self.keep_raw)
        if r is None:
            self.counts["(skipped) " + name] += 1
            return
        table, row = r
        self.pending[table].append(row)
        self.counts[table] += 1

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

    def flush(self):
        if self.dry_run:
            self.pending.clear()
            self.last_flush = time.time()
            return
        with self.conn.cursor() as cur:
            for table, rows in self.pending.items():
                cur.executemany(INSERT[table], rows)
            if self.devices:
                cur.executemany(DEVICE_UPSERT, [(k, *v) for k, v in self.devices.items()])
        self.conn.commit()
        self.pending.clear()
        self.devices.clear()
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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("logfile", help="event log path, or - for stdin")
    ap.add_argument("--follow", action="store_true", help="keep tailing the file after loading it")
    ap.add_argument("--poll", type=float, default=2.0, help="seconds between tail polls (default 2)")
    ap.add_argument("--dry-run", action="store_true", help="parse only, no database")
    ap.add_argument("--keep-raw", action="store_true",
                    help="store the full diagnostics payload in diagnostics.raw (about 2 KB per row)")
    ap.add_argument("--database-url", default=os.environ.get("DATABASE_URL"),
                    help="postgresql://user:pw@host/db (default $DATABASE_URL)")
    args = ap.parse_args()

    conn = None
    if not args.dry_run:
        if not args.database_url:
            sys.exit("error: set DATABASE_URL or pass --database-url")
        import psycopg
        conn = psycopg.connect(args.database_url)

    loader = Loader(conn, dry_run=args.dry_run, keep_raw=args.keep_raw)
    t0 = time.time()

    def load_lines(lines):
        for name, env in sse_records(lines):
            loader.feed(name, env)
        loader.flush()

    if args.logfile == "-":
        load_lines(sys.stdin)
    elif args.follow:
        name = None
        loaded_once = False
        for line in follow(args.logfile, args.poll):
            if line is None:
                loader.flush()
                if not loaded_once:
                    loaded_once = True
                    report(loader, t0)
                    print("following...", flush=True)
                continue
            if line.startswith("event: "):
                name = line[7:].strip()
            elif line.startswith("data: ") and name:
                try:
                    loader.feed(name, json.loads(line[6:]))
                except json.JSONDecodeError:
                    pass
                name = None
        return
    else:
        with open(args.logfile, errors="replace") as f:
            load_lines(f)

    report(loader, t0)


def report(loader, t0):
    total = sum(v for k, v in loader.counts.items() if not k.startswith("(skipped)"))
    print(f"{total} rows in {time.time() - t0:.1f}s", flush=True)
    for k, v in sorted(loader.counts.items(), key=lambda kv: -kv[1]):
        print(f"{v:8d}  {k}", flush=True)


if __name__ == "__main__":
    main()
