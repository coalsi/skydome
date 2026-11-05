#!/bin/bash
# SkyDome: on Wi-Fi, move up to the best saved network in range (tools/wifi.sh
# sets the order). NetworkManager only looks at priorities when it's choosing a
# network, so left alone it stays on a lower one for as long as that works.
# systemd/skydome-wifi-prefer.timer runs this every 5 minutes.
#
# The radio is off while the cable is in (systemd/70-skydome-wifi-backup), so
# then this does nothing. A scan stalls Wi-Fi traffic for a moment, so it only
# scans when a better network is saved. A move that fails goes back to the last
# network and isn't tried again for an hour: a wrong password mustn't drop the
# link every 5 minutes.
set -u

MIN_SIGNAL=40
RETRY_S=3600
FAILED=/run/skydome-wifi-prefer.failed

[[ $(nmcli radio wifi) == enabled ]] || exit 0
[[ -e $FAILED ]] && (( $(date +%s) - $(stat -c %Y "$FAILED") < RETRY_S )) && exit 0

saved() { nmcli -g UUID,TYPE connection show "$@" | awk -F: '$2 == "802-11-wireless" { print $1 }'; }

current=$(saved --active | head -n 1)
[[ -n $current ]] || exit 0     # not connected: NetworkManager is already choosing
now=$(nmcli -g connection.autoconnect-priority connection show "$current")

better=$(saved | while read -r id; do
    prio=$(nmcli -g connection.autoconnect-priority connection show "$id")
    (( prio > now )) && echo "$prio $id"
done | sort -rn)
[[ -n $better ]] || exit 0

scan=$(nmcli -g SSID,SIGNAL device wifi list --rescan yes)

# The strongest signal seen for a network: a mesh shows up once per access point
signal() {
    local line best=0
    while IFS= read -r line; do
        [[ ${line%:*} == "$1" ]] && (( ${line##*:} > best )) && best=${line##*:}
    done <<< "$scan"
    echo "$best"
}

while read -r prio id; do
    ssid=$(nmcli -g 802-11-wireless.ssid connection show "$id")
    sig=$(signal "$ssid")
    (( sig >= MIN_SIGNAL )) || continue
    echo "moving to $ssid (priority $prio, signal $sig%)"
    nmcli --wait 45 connection up "$id" >/dev/null && exit 0
    echo "couldn't join $ssid, going back, not trying again for an hour"
    touch "$FAILED"
    nmcli --wait 45 connection up "$current" >/dev/null
    exit 0
done <<< "$better"
