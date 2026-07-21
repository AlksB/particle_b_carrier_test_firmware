#!/usr/bin/env bash
# Regenerates build_info.h (current git SHA) then flashes over Particle
# Cloud, so every flash is automatically tied to the exact commit it came
# from - no need to remember to run gen-build-info.sh separately.
#
# Usage: ./flash.sh <device_id_or_name> [particle flash options...]
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

if [ $# -lt 1 ]; then
  echo "Usage: $0 <device_id_or_name> [particle flash options...]" >&2
  exit 1
fi

DEVICE="$1"
shift

./gen-build-info.sh
particle flash "$DEVICE" ./src --target 6.4.1 "$@"
