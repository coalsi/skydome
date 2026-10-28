#!/usr/bin/env bash
# Phase 0 bring-up report (handoff §6). Run ON THE JETSON with all five
# cameras plugged in:
#
#   tools/bringup/phase0.sh            # inventory only (fast, safe)
#   tools/bringup/phase0.sh --gst      # also the 5x capture load tests
#   DURATION=60 tools/bringup/phase0.sh --gst
#
# Writes reports/phase0-<timestamp>/ with raw command output plus
# summary.md, which is what goes into DECISIONS.md.
#
# Written before the hardware arrived: untested on a real Jetson.

set -uo pipefail

RUN_GST=0
[[ "${1:-}" == "--gst" ]] && RUN_GST=1
DURATION="${DURATION:-30}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="$ROOT/reports/phase0-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$OUT"
SUMMARY="$OUT/summary.md"

log() { printf '%s\n' "$*" | tee -a "$SUMMARY"; }
run() {  # run <outfile> <cmd...>: save output, never abort the report
    local f="$OUT/$1"; shift
    { echo "\$ $*"; "$@"; } >"$f" 2>&1 || echo "(exit $?)" >>"$f"
}

need() { command -v "$1" >/dev/null || { echo "missing: $1 (sudo apt install $2)"; MISSING=1; }; }
MISSING=0
need v4l2-ctl v4l-utils; need lsusb usbutils; need udevadm udev
[[ $RUN_GST == 1 ]] && { need gst-launch-1.0 gstreamer1.0-tools; need tegrastats nvidia-l4t-tools; }
[[ $MISSING == 1 ]] && exit 1

log "# Phase 0 bring-up — $(date -u +%Y-%m-%dT%H:%M:%SZ)"
log ""

# --- system -----------------------------------------------------------------
run system.txt bash -c 'cat /etc/nv_tegra_release; uname -a; hostname; nvpmodel -q 2>/dev/null; free -h; df -h /; lsblk'
log "## System"
log '```'
head -1 /etc/nv_tegra_release 2>/dev/null | tee -a "$SUMMARY" >/dev/null
nvpmodel -q 2>/dev/null | head -2 >>"$SUMMARY"
findmnt -n -o SOURCE / >>"$SUMMARY"
log '```'

# --- USB topology -------------------------------------------------------------
run lsusb.txt lsusb
run lsusb-tree.txt lsusb -t
log ""
log "## USB tree (cameras must show 5000M, hub uplink 10000M)"
log '```'
lsusb -t >>"$SUMMARY" 2>&1
log '```'

# --- cameras -----------------------------------------------------------------
# UVC cameras expose two nodes each (capture + metadata); keep capture nodes only
CAMS=()
for dev in /dev/video*; do
    [[ -e "$dev" ]] || continue
    if v4l2-ctl -d "$dev" --info 2>/dev/null | grep -A3 "Device Caps" | grep -q "Video Capture"; then
        CAMS+=("$dev")
    fi
done
log ""
log "## Cameras: ${#CAMS[@]} capture node(s) found"
log ""
log "| node | ID_PATH | skycam link | exposure_time_absolute | gain |"
log "|---|---|---|---|---|"
for dev in "${CAMS[@]}"; do
    n="$(basename "$dev")"
    run "$n-udev.txt" udevadm info -q property -n "$dev"
    run "$n-info.txt" v4l2-ctl -d "$dev" --all
    run "$n-formats.txt" v4l2-ctl -d "$dev" --list-formats-ext
    run "$n-ctrls.txt" v4l2-ctl -d "$dev" --list-ctrls-menus
    idp="$(udevadm info -q property -n "$dev" | sed -n 's/^ID_PATH=//p')"
    link="$(for l in /dev/skycam-*; do [[ "$(readlink -f "$l")" == "$dev" ]] && basename "$l"; done 2>/dev/null)"
    exp="$(grep -m1 -E 'exposure_time_absolute|exposure_absolute' "$OUT/$n-ctrls.txt" | sed 's/^ *//')"
    gain="$(grep -m1 -E '^ *gain ' "$OUT/$n-ctrls.txt" | sed 's/^ *//')"
    log "| $dev | \`$idp\` | ${link:-–} | ${exp:-?} | ${gain:-?} |"
done
log ""
log "Full format lists: \`video*-formats.txt\`. Check 1920x1080 YUYV is offered at >= 10 fps"
log "and 3840x2160 MJPG at 30 fps:"
log '```'
for dev in "${CAMS[@]}"; do
    n="$(basename "$dev")"
    echo "$dev:" >>"$SUMMARY"
    awk '/\[[0-9]+\]:/{fmt=$2} /Size:/{size=$3} /Interval:/{if (size=="3840x2160" || size=="1920x1080") print "  " fmt, size, $NF}' \
        "$OUT/$n-formats.txt" | sort -u | tr -d "()'" >>"$SUMMARY"
done
log '```'

# --- capture load tests ------------------------------------------------------
gst_test() {  # gst_test <name> <caps> <decode-chain>
    local name="$1" caps="$2" chain="$3"
    log ""
    log "## Load test: $name, 5 cameras at once, ${DURATION}s"
    tegrastats --interval 1000 --logfile "$OUT/tegrastats-$name.log" &
    local tpid=$! pids=()
    for dev in "${CAMS[@]}"; do
        n="$(basename "$dev")"
        timeout "$DURATION" gst-launch-1.0 -v v4l2src device="$dev" io-mode=2 ! "$caps" $chain \
            ! fpsdisplaysink video-sink=fakesink text-overlay=false sync=false \
            >"$OUT/gst-$name-$n.log" 2>&1 &
        pids+=($!)
    done
    wait "${pids[@]}" 2>/dev/null
    tegrastats --stop 2>/dev/null; kill "$tpid" 2>/dev/null
    log "| node | last reported fps | dropped |"
    log "|---|---|---|"
    for dev in "${CAMS[@]}"; do
        n="$(basename "$dev")"
        last="$(grep -o 'rendered: [0-9]*, dropped: [0-9]*, current: [0-9.]*, average: [0-9.]*' "$OUT/gst-$name-$n.log" | tail -1)"
        fps="$(sed -n 's/.*average: \([0-9.]*\).*/\1/p' <<<"$last")"
        drop="$(sed -n 's/.*dropped: \([0-9]*\).*/\1/p' <<<"$last")"
        log "| $dev | ${fps:-FAILED (see gst-$name-$n.log)} | ${drop:-?} |"
    done
    local gpu
    gpu="$(grep -o 'GR3D_FREQ [0-9]*%' "$OUT/tegrastats-$name.log" | awk '{gsub("%","",$2); s+=$2; n++} END {if (n) printf "%.0f%%", s/n}')"
    log ""
    log "Average GPU load: ${gpu:-n/a}. Full log: \`tegrastats-$name.log\`."
}

if [[ $RUN_GST == 1 && ${#CAMS[@]} -gt 0 ]]; then
    # Day/Meteor: 4K MJPEG, hardware JPEG decode (never CPU)
    gst_test mjpeg-4k30 "image/jpeg,width=3840,height=2160,framerate=30/1" "! nvv4l2decoder mjpeg=1"
    # Night/Deep: 1080p uncompressed YUY2
    gst_test yuy2-1080p15 "video/x-raw,format=YUY2,width=1920,height=1080,framerate=15/1" ""
fi

log ""
log "Report: $OUT"
echo
echo "Done. Summary: $SUMMARY"
