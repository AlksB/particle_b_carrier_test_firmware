#!/usr/bin/env bash
# Regenerates compile_commands.json for clangd by wrapping the Particle
# local build (make compile-user) with bear. Versions must match
# .vscode/settings.json -> particle.firmwareVersion.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

DEVICE_OS_VERSION=6.4.1
GCC_ARM_VERSION=10.2.1
BUILDSCRIPTS_VERSION=1.17.2

export DEVICE_OS_PATH="$HOME/.particle/toolchains/deviceOS/$DEVICE_OS_VERSION"
export DEVICE_OS_VERSION
export GCC_ARM_PATH="$HOME/.particle/toolchains/gcc-arm/$GCC_ARM_VERSION/bin/"
export PLATFORM=msom
export PLATFORM_ID=35
export APPDIR="$(pwd)"
export PATH="$HOME/.particle/toolchains/gcc-arm/$GCC_ARM_VERSION/bin:$PATH"

MAKEFILE="$HOME/.particle/toolchains/buildscripts/$BUILDSCRIPTS_VERSION/Makefile"

make -f "$MAKEFILE" clean-user -s
bear -- make -f "$MAKEFILE" compile-user -s
