#!/usr/bin/env python3
"""
Fires `particle flash` the moment a device announces it has come online.

Development devices only accept a direct flash while they are actually
connected, and a sleepy device's window can be shorter than any sane polling
interval - so this doesn't poll. It holds the Particle SSE event stream open
and waits for one specific thing: a `spark/status` event carrying "online".

That event marks the *start* of a connection, which is what makes it the right
trigger. Any other event only proves the device was connected at some point
during its window - possibly most of the way through it, leaving too little
time for a transfer to land. The cloud's `online` flag is useless here for the
same reason plus lag: it can read true for a device that has already gone.

By default one successful `particle flash` ends the run - the point is to get
the binary onto the device.

`Flash success!` from the CLI only means the cloud queued the request, not that
the device took it. When you pass --expect-version, the reported firmware
version is re-checked afterwards, and if it didn't move the next online event
triggers another attempt, up to --max-attempts. Without it there is nothing to
confirm against, so the version is only printed for information.

Usage:
  ./flash_watcher.py <device_id> <binary.bin>
  ./flash_watcher.py <device_id> <binary.bin> --expect-version 20
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

API = "https://api.particle.io"


def load_token():
    path = os.path.expanduser("~/.particle/particle.config.json")
    with open(path) as f:
        return json.load(f)["access_token"]


def get_device(token, device_id):
    req = urllib.request.Request(
        f"{API}/v1/devices/{device_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def do_flash(device_id, binary):
    print(f"  -> particle flash {device_id}", flush=True)
    r = subprocess.run(
        ["particle", "flash", device_id, binary],
        capture_output=True, text=True,
    )
    for line in (r.stdout + r.stderr).strip().splitlines()[-3:]:
        print(f"     {line}", flush=True)
    return r.returncode == 0


def reported_version(token, device_id):
    try:
        return get_device(token, device_id).get("firmware_version")
    except urllib.error.HTTPError as e:
        print(f"  (не смог прочитать версию: {e.code})", flush=True)
        return None


def events_url(device_id, product_id):
    """
    Product devices that aren't claimed to your account 403 on the personal
    /v1/devices/:id/events stream - their events only come through the
    product-scoped endpoint, even though /v1/devices/:id itself reads fine.
    """
    if product_id:
        return f"{API}/v1/products/{product_id}/devices/{device_id}/events"
    return f"{API}/v1/devices/{device_id}/events"


def online_events(token, url):
    """Yields once per `spark/status` event whose payload is "online"."""
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "text/event-stream"},
    )
    with urllib.request.urlopen(req, timeout=None) as resp:
        pending = None  # event name seen, waiting for its data line
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("event:"):
                pending = line[len("event:"):].strip()
            elif line.startswith("data:") and pending is not None:
                name, pending = pending, None
                if name != "spark/status":
                    continue
                try:
                    payload = json.loads(line[len("data:"):].strip())
                except json.JSONDecodeError:
                    continue
                if payload.get("data") == "online":
                    yield payload.get("published_at", "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("device_id")
    ap.add_argument("binary")
    ap.add_argument("--expect-version", default=None,
                    help="keep retrying until the device reports this firmware "
                         "version; without it a successful flash ends the run")
    ap.add_argument("--max-attempts", type=int, default=5)
    ap.add_argument("--settle", type=int, default=25,
                    help="seconds to wait before re-reading the version")
    args = ap.parse_args()

    if not os.path.isfile(args.binary):
        sys.exit(f"нет файла: {args.binary}")

    token = load_token()

    try:
        dev = get_device(token, args.device_id)
    except urllib.error.HTTPError as e:
        sys.exit(f"устройство недоступно: {e.code} {e.reason}")

    print(f"{dev.get('name')} | fw={dev.get('firmware_version')} "
          f"| development={dev.get('development')} | online={dev.get('online')}", flush=True)
    if args.expect_version and str(dev.get("firmware_version")) == str(args.expect_version):
        print("уже на нужной версии, делать нечего")
        return

    # Deliberately no flash-on-startup even if the device currently reads
    # online: that state may be stale or most of the way through its window.
    # Only a fresh spark/status "online" is worth acting on.
    url = events_url(args.device_id, dev.get("product_id"))
    print(f"поток: {url}", flush=True)
    print("жду spark/status online (Ctrl-C чтобы прервать)...", flush=True)

    attempts = 0
    while attempts < args.max_attempts:
        try:
            for published_at in online_events(token, url):
                attempts += 1
                ts = time.strftime("%H:%M:%S")
                print(f"[{ts}] spark/status online (published {published_at})", flush=True)
                print(f"[попытка {attempts}/{args.max_attempts}]", flush=True)
                ok = do_flash(args.device_id, args.binary)

                if ok:
                    time.sleep(args.settle)  # transfer lands, device reboots
                    got = reported_version(token, args.device_id)
                    print(f"  устройство рапортует firmware_version={got}", flush=True)
                    if not args.expect_version:
                        # Nothing to verify against - the binary is sent, done.
                        print("готово: бинарник отправлен")
                        return
                    if str(got) == str(args.expect_version):
                        print("готово: версия совпала")
                        return
                    reason = "версия не сдвинулась"
                else:
                    reason = "particle flash не прошёл"

                if attempts >= args.max_attempts:
                    break
                print(f"  {reason} - жду следующего online", flush=True)
        except urllib.error.HTTPError as e:
            # Auth/permission/not-found won't fix themselves - reconnecting in a
            # loop would just spin forever printing the same line.
            sys.exit(f"поток недоступен: HTTP {e.code} {e.reason} ({url})")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            # A dropped connection is routine; just reopen it.
            print(f"  (поток оборвался: {e} - переподключаюсь)", flush=True)
            time.sleep(3)

    tail = ("версия так и не подтвердилась" if args.expect_version
            else "прошивка так и не ушла")
    print(f"исчерпано {args.max_attempts} попыток - {tail}")
    sys.exit(1)


if __name__ == "__main__":
    main()
