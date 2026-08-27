#!/usr/bin/env python3
"""
Pins (locks) one product device to a specific product firmware version, so the
cloud pushes that binary automatically on the device's next handshake.

This is the queued, hands-off counterpart to `particle flash` / flash_watcher.py:
a direct flash is synchronous and needs the device online at that instant, while
a firmware lock sits in the cloud indefinitely and is delivered ~8s into the next
connection - which is the only thing that works for a device that wakes up,
reports and sleeps again.

Three cloud-side conditions have to hold for that to happen, and the script
enforces all of them instead of leaving them to be discovered one by one:
  * the binary for that version exists in the product (uploaded with --file),
  * the device is NOT a development device - those never receive product
    firmware automatically, which silently overrides even an explicit lock,
  * the device is not quarantined and has firmware updates enabled.

--file takes either a bare .bin or a compiled .zip bundle (application binary +
assets). Upload the bundle whenever the project has assets: this project turns
env.json into an OTA asset named `env` - the certified Cat-M1 band mask lives
there - and a bare .bin leaves that asset behind, so the device would fetch
firmware whose asset dependency it cannot satisfy.

The file is inspected (`particle binary inspect`) before it goes up, and the
PRODUCT_VERSION baked into the binary is what the firmware is filed under - so
--file alone is enough and --version is only for overriding it. Getting those
two out of step is the classic way to end up with a device that downloads an
update and still reports the old version forever.

Usage:
  ./scripts/pin_firmware.py <device_id>                        # show state only
  ./scripts/pin_firmware.py <device_id> --file bsom_firmware_*.zip   # version from the file
  ./scripts/pin_firmware.py <device_id> --file bsom_firmware_*.zip --wait 900
  ./scripts/pin_firmware.py <device_id> --latest               # newest one in the product
  ./scripts/pin_firmware.py <device_id> --version 24           # a specific version
  ./scripts/pin_firmware.py <device_id> --unlock               # back to release

Token: $PARTICLE_TOKEN, else ~/.particle/particle.config.json.
"""

import argparse
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

API = "https://api.particle.io"


# ---------------------------------------------------------------- HTTP helpers

def load_token():
    token = os.environ.get("PARTICLE_TOKEN")
    if token:
        return token
    path = os.path.expanduser("~/.particle/particle.config.json")
    try:
        with open(path) as f:
            return json.load(f)["access_token"]
    except (OSError, KeyError, json.JSONDecodeError):
        sys.exit("no token: set PARTICLE_TOKEN or run `particle login`")


def api(token, method, path, body=None, multipart=None):
    """Returns (status, parsed_json_or_text). Never raises on 4xx/5xx."""
    headers = {"Authorization": f"Bearer {token}"}
    data = None
    if multipart is not None:
        boundary = uuid.uuid4().hex
        data = encode_multipart(multipart, boundary)
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(API + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, parse(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, parse(e.read())
    except urllib.error.URLError as e:
        sys.exit(f"network unreachable: {e.reason}")


def parse(raw):
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return raw.decode("utf-8", "replace")


def encode_multipart(fields, boundary):
    """fields: list of (name, value) or (name, filename, bytes)."""
    out = bytearray()
    for field in fields:
        out += f"--{boundary}\r\n".encode()
        if len(field) == 2:
            name, value = field
            out += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
            out += str(value).encode() + b"\r\n"
        else:
            name, filename, blob = field
            ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            out += (f'Content-Disposition: form-data; name="{name}"; '
                    f'filename="{filename}"\r\n').encode()
            out += f"Content-Type: {ctype}\r\n\r\n".encode()
            out += blob + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out)


def err_text(payload):
    if isinstance(payload, dict):
        return payload.get("error_description") or payload.get("error") or json.dumps(payload)
    return str(payload)[:300]


# --------------------------------------------------------------- cloud objects

def get_device(token, device_id, product=None):
    # The personal endpoint answers for product devices too and is the only way
    # to learn the product id before you know it; fall back to the product
    # endpoint when the device isn't claimed to this account.
    status, payload = api(token, "GET", f"/v1/devices/{device_id}")
    if status == 200:
        return payload
    if product:
        status, payload = api(token, "GET", f"/v1/products/{product}/devices/{device_id}")
        if status == 200:
            return payload
    sys.exit(f"device {device_id} unreachable: HTTP {status} {err_text(payload)}"
             + ("" if product else "\nif it isn't claimed to this account, pass --product <id>"))


def product_firmware(token, product):
    status, payload = api(token, "GET", f"/v1/products/{product}/firmware")
    if status != 200:
        sys.exit(f"cannot read the firmware list of product {product}: "
                 f"HTTP {status} {err_text(payload)}")
    return payload


def upload_firmware(token, product, version, path, title, description):
    with open(path, "rb") as f:
        blob = f.read()
    fields = [("version", version), ("title", title)]
    if description:
        fields.append(("description", description))
    # 'file' is the field name particle-api-js uses, with the filename hardcoded
    # to firmware.bin. Keep that for a bare binary, but send a bundle under its
    # real .zip name so nothing downstream has to guess from the content alone.
    name = "firmware.bin" if path.lower().endswith(".bin") else os.path.basename(path)
    fields.append(("file", name, blob))
    status, payload = api(token, "POST", f"/v1/products/{product}/firmware", multipart=fields)
    if status not in (200, 201):
        sys.exit(f"upload failed: HTTP {status} {err_text(payload)}")
    return payload


def update_device(token, product, device_id, **fields):
    status, payload = api(token, "PUT", f"/v1/products/{product}/devices/{device_id}", body=fields)
    if status != 200:
        sys.exit(f"could not update the device: HTTP {status} {err_text(payload)}")
    return payload


# ------------------------------------------------------------------ local file

# Only platforms this project could plausibly be built for; anything else just
# skips the check rather than guessing.
PLATFORMS = {12: "argon", 13: "boron", 23: "bsom", 25: "b5som", 26: "tracker",
             32: "p2", 35: "msom"}


def inspect_file(path):
    """
    Reads PRODUCT_VERSION, platform and asset list out of a .bin or .zip via
    `particle binary inspect`. Returns None when the CLI isn't installed - the
    upload is still fine, it just goes up unchecked.
    """
    if not shutil.which("particle"):
        return None
    r = subprocess.run(["particle", "binary", "inspect", path],
                       capture_output=True, text=True)
    out = r.stdout + r.stderr
    if r.returncode != 0:
        sys.exit(f"the file does not parse as Particle firmware:\n{out.strip()}")
    info = {"raw": out, "version": None, "platform": None, "assets": [], "env": {}}
    m = re.search(r"product id (\d+) at version (\d+)", out)
    if m:
        info["version"] = int(m.group(2))
    m = re.search(r"Compiled for (\S+)", out)
    if m:
        info["platform"] = m.group(1)
    tail = out.split("It depends on assets:", 1)
    if len(tail) == 2:
        for line in tail[1].splitlines():
            line = line.strip()
            if not line or line.startswith("It ") or ":" in line and "hash" not in line:
                continue
            # "env (hash ...)" in a bundle, "env failed (hash should be ...)"
            # when the same binary is inspected without its assets.
            name = line.split(" (")[0].strip()
            info["assets"].append(name[:-7].strip() if name.endswith(" failed") else name)
    envs = out.split("Environment variables:", 1)
    if len(envs) == 2:
        for line in envs[1].splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("It "):
                break
            if ":" in line:
                k, v = line.split(":", 1)
                info["env"][k.strip()] = v.strip()
    return info


def read_file(path):
    """Checks the extension, inspects the file and prints what's inside it."""
    if not path.lower().endswith((".bin", ".zip")):
        sys.exit(f"expected a .bin or a .zip bundle, not {os.path.basename(path)}")
    info = inspect_file(path)
    if info is None:
        print("  (particle CLI not found - cannot read the version out of the file, "
              "it goes up unchecked)")
        return None
    print(f"  contents : platform={info['platform']} PRODUCT_VERSION={info['version']}"
          + (f" assets={', '.join(info['assets'])}" if info["assets"] else " assets=none"))
    for k, v in info["env"].items():
        print(f"             env {k}={v}")
    return info


def check_file(path, version, device, force, info):
    if info is None:
        return
    is_zip = path.lower().endswith(".zip")
    problems = []
    if info["version"] is not None and info["version"] != version:
        problems.append(f"the binary has PRODUCT_VERSION={info['version']} but it would be filed "
                        f"as v{version}: the cloud would serve it as v{version} while the device "
                        f"keeps reporting {info['version']} - the update never converges")
    want = PLATFORMS.get(device.get("platform_id"))
    if want and info["platform"] and info["platform"] != want:
        problems.append(f"built for {info['platform']}, but the device is a {want}")
    if info["assets"] and not is_zip:
        problems.append("the binary depends on assets (" + ", ".join(info["assets"]) +
                        ") but a bare .bin is being uploaded - upload the .zip bundle instead")
    if problems:
        for p in problems:
            print(f"  ERROR: {p}")
        if not force:
            sys.exit("aborted (--force to upload anyway)")
        print("  --force: uploading regardless")


# --------------------------------------------------------------------- display

def show(dev):
    print(f"  device   : {dev.get('name')} ({dev.get('id')})")
    print(f"  product  : {dev.get('product_id')}   online={dev.get('online')}   "
          f"last_heard={dev.get('last_heard')}")
    print(f"  firmware : running={dev.get('firmware_version')}   "
          f"pinned(desired)={dev.get('desired_firmware_version')}   "
          f"release={dev.get('targeted_firmware_release_version')}")
    print(f"  flags    : development={dev.get('development')}   "
          f"quarantined={dev.get('quarantined')}   "
          f"updates_enabled={dev.get('firmware_updates_enabled')}   "
          f"groups={dev.get('groups')}")


def wait_for(token, device_id, product, version, seconds, interval=30):
    print(f"\nwaiting up to {seconds}s for the device to report version {version} "
          f"(Ctrl-C to stop)...", flush=True)
    deadline = time.time() + seconds
    while time.time() < deadline:
        time.sleep(interval)
        dev = get_device(token, device_id, product)
        got = dev.get("firmware_version")
        left = int(deadline - time.time())
        print(f"  [{time.strftime('%H:%M:%S')}] firmware_version={got} "
              f"online={dev.get('online')} ({left}s left)", flush=True)
        if str(got) == str(version):
            print("done: the device took the firmware")
            return True
    print("timed out - the device hasn't connected yet, or hasn't taken the update")
    return False


# ------------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=__doc__)
    ap.add_argument("device_id")
    ap.add_argument("--version", type=int,
                    help="product firmware version to lock the device to; with --file it is read "
                         "out of the binary (PRODUCT_VERSION) and only needs to be given to "
                         "override that")
    ap.add_argument("--file", "--binary", dest="file", metavar="PATH",
                    help=".bin or .zip bundle to upload, if that version isn't in the product yet")
    ap.add_argument("--title", help="title for the uploaded binary (default: v<version>)")
    ap.add_argument("--description", default="", help="description for the uploaded binary")
    ap.add_argument("--latest", action="store_true",
                    help="lock to the highest firmware version already in the product")
    ap.add_argument("--product", help="product id, if the device isn't claimed to this account")
    ap.add_argument("--unlock", action="store_true",
                    help="drop the lock; the device follows the product release again")
    ap.add_argument("--flash-now", action="store_true",
                    help="also ask the cloud to flash immediately (only lands if the device "
                         "is online right now; the lock alone already covers the next handshake)")
    ap.add_argument("--keep-development", action="store_true",
                    help="don't clear the development flag (leaves automatic OTA disabled)")
    ap.add_argument("--wait", type=int, metavar="SECONDS",
                    help="after pinning, poll until the device reports the version")
    ap.add_argument("--force", action="store_true",
                    help="upload even if the file fails the pre-flight checks")
    ap.add_argument("--dry-run", action="store_true", help="show what would change, change nothing")
    args = ap.parse_args()

    if args.unlock and (args.version or args.file or args.latest):
        sys.exit("--unlock doesn't combine with --version/--file/--latest")
    if args.latest and (args.version or args.file):
        sys.exit("--latest doesn't combine with --version/--file")
    if args.file and not os.path.isfile(args.file):
        sys.exit(f"no such file: {args.file}")

    token = load_token()
    dev = get_device(token, args.device_id, args.product)
    product = args.product or dev.get("product_id")
    if not product:
        sys.exit("the device isn't in a product - firmware locks only exist for product devices")

    print("current state:")
    show(dev)

    if not (args.version or args.file or args.latest or args.unlock):
        print("\n(nothing changed: pass --file <bin|zip>, --version N or --latest to pin "
              "firmware, or --unlock to drop the lock)")
        return

    # ---- which version are we aiming at? ------------------------------------
    # The binary knows its own PRODUCT_VERSION, so --file alone is enough;
    # --version stays available for the rare case of overriding it.
    info = None
    version = args.version
    if args.file:
        print(f"\nfile: {args.file}")
        info = read_file(args.file)
        if version is None:
            if info is None or info["version"] is None:
                sys.exit("could not read PRODUCT_VERSION out of the file - pass --version")
            version = info["version"]
            print(f"  version taken from the file: v{version}")
        elif info and info["version"] != version:
            print(f"  warning: --version {version} disagrees with PRODUCT_VERSION={info['version']}")

    # ---- unlock -------------------------------------------------------------
    if args.unlock:
        if dev.get("desired_firmware_version") is None:
            print("\nno firmware is pinned - nothing to do")
            return
        if args.dry_run:
            print("\n[dry-run] would clear desired_firmware_version")
            return
        update_device(token, product, args.device_id, desired_firmware_version=None)
        print("\nlock dropped - the device follows the product release again")
        show(get_device(token, args.device_id, product))
        return

    # ---- make sure the binary for that version exists ------------------------
    versions = {int(f["version"]): f for f in product_firmware(token, product)
                if isinstance(f, dict) and "version" in f}
    if args.latest:
        if not versions:
            sys.exit(f"\nproduct {product} has no firmware at all - nothing to pin")
        version = max(versions)
        print(f"\n--latest: newest version in the product is v{version}")
    if version in versions:
        f = versions[version]
        print(f"\nv{version} is already in the product: "
              f"{f.get('name')} ({f.get('size')} bytes, {f.get('device_os_version')}, "
              f"uploaded {f.get('uploaded_on')})")
        if args.file:
            print("  ignoring --file: the cloud won't overwrite an existing version. "
                  "For a new binary, bump the version number.")
    elif args.file:
        title = args.title or f"v{version}"
        print(f"\nv{version} isn't in the product - uploading {os.path.basename(args.file)} "
              f"as \"{title}\"")
        check_file(args.file, version, dev, args.force, info)
        if args.dry_run:
            print("[dry-run] upload skipped")
        else:
            up = upload_firmware(token, product, version, args.file, title, args.description)
            print(f"  uploaded: {up.get('name')} ({up.get('size')} bytes)")
    else:
        have = ", ".join(str(v) for v in sorted(versions, reverse=True)) or "none"
        sys.exit(f"\nproduct {product} has no firmware v{version} (it has: {have}).\n"
                 f"A version that doesn't exist can't be pinned - add --file <.bin or .zip>.")

    # ---- preflight ----------------------------------------------------------
    if dev.get("quarantined"):
        sys.exit("\nthe device is quarantined - nothing will be delivered until it is "
                 "approved into the product")
    if dev.get("firmware_updates_enabled") is False:
        print("\nwarning: firmware_updates_enabled=false - the device will refuse OTA until "
              "the running firmware clears that flag (System.enableUpdates())")

    fields = {"desired_firmware_version": version}
    if dev.get("development") and not args.keep_development:
        # Development devices never receive product firmware automatically - this
        # flag outranks the lock, so clearing it is part of the same operation.
        fields["development"] = False
        print("\nthe device is marked as a development device - clearing that flag "
              "(automatic OTA does not work at all otherwise)")
    elif dev.get("development"):
        print("\nwarning: development=true kept because of --keep-development - "
              "there will be no automatic update")
    if args.flash_now:
        fields["flash"] = True

    if args.dry_run:
        print(f"\n[dry-run] would send PUT /v1/products/{product}/devices/"
              f"{args.device_id} {json.dumps(fields)}")
        return

    update_device(token, product, args.device_id, **fields)
    print(f"\nfirmware v{version} is pinned to the device")
    show(get_device(token, args.device_id, product))
    print("\nthe cloud will hand it over on the device's next handshake (~8-10s after connect); "
          "nothing else has to be run by hand.")

    if args.wait:
        ok = wait_for(token, args.device_id, product, version, args.wait)
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
