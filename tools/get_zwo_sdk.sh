#!/bin/bash
# Fetch ZWO's ASI camera SDK into third_party/zwo (DECISIONS.md D-029): the
# header and the 64-bit ARM library skydome-live links against. Not kept in
# git; ZWO's licence (MIT style) is copied alongside.
#
#   tools/get_zwo_sdk.sh            # then rebuild pipeline/native
#
# The camera also needs ZWO's udev rule once (lets it be opened without root
# and raises the USB buffer to 200 MB), which takes sudo:
#
#   sudo install -m 644 third_party/zwo/asi.rules /etc/udev/rules.d/ && sudo udevadm control --reload
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
DEST=$ROOT/third_party/zwo
URL="https://dl.zwoastro.com/software?app=DeveloperCameraSdk&platform=linux&region=Overseas"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

echo "downloading ZWO's camera SDK (about 110 MB)..."
curl -fsSL -o "$WORK/sdk.zip" "$URL"
mkdir "$WORK/zip" "$WORK/sdk"
# The download is every platform's SDK; only the Linux/Mac one is unpacked
unzip -q "$WORK/sdk.zip" 'ASI_Camera_SDK/ASI_linux_mac_SDK_*.tar.bz2' -d "$WORK/zip"
tar xjf "$WORK"/zip/ASI_Camera_SDK/ASI_linux_mac_SDK_*.tar.bz2 -C "$WORK/sdk"
SDK=$(echo "$WORK"/sdk/ASI_linux_mac_SDK_*)

rm -rf "$DEST"
mkdir -p "$DEST/include" "$DEST/lib"
cp "$SDK/include/ASICamera2.h" "$DEST/include/"
cp -P "$SDK"/lib/armv8/libASICamera2.so* "$DEST/lib/"
cp "$SDK/lib/asi.rules" "$SDK/license.txt" "$DEST/"
basename "$SDK" > "$DEST/VERSION"
echo "ZWO SDK $(cat "$DEST/VERSION") in $DEST"
