#!/bin/bash
# The Jetson's saved Wi-Fi networks. Run it on the Jetson, or from another
# machine with ssh -t so it can ask questions:
#
#   ssh -t skydome@skydome.local skydome/tools/wifi.sh add
#
# To give a name with a space in it over ssh, put the whole command in single
# quotes: ssh skydome@skydome.local 'skydome/tools/wifi.sh remove "Old network"'
#
#   wifi.sh                         list them, highest priority first
#   wifi.sh add [name] [priority]   add one, or change its password (asks for what's missing)
#   wifi.sh priority <name> <n>     change a network's priority
#   wifi.sh remove <name>           forget one
#
# When several are in range the highest number wins (0 is the default), and
# tools/wifi_prefer.sh moves the rig up to a better one that comes into range.
# Networks can be added while the Wi-Fi radio is off;
# systemd/70-skydome-wifi-backup turns it on when the cable is out.
#
# The password goes in through nmcli's stdin: on the command line it would land
# in the sudo log.
set -eu

known() { nmcli -g connection.id connection show id "$1" >/dev/null 2>&1; }

need_known() {
    known "$1" && return
    echo "No saved network called \"$1\"." >&2
    exit 1
}

need_number() {
    [[ $1 =~ ^-?[0-9]+$ ]] && return
    echo "The priority has to be a whole number, not \"$1\"." >&2
    exit 1
}

list() {
    echo "Wi-Fi radio: $(nmcli radio wifi)"
    printf '%-9s %-10s %s\n' PRIORITY STATE NETWORK
    nmcli -g UUID,TYPE connection show | awk -F: '$2 == "802-11-wireless" { print $1 }' | while read -r id; do
        state=$(nmcli -g GENERAL.STATE connection show "$id")
        printf '%-9s %-10s %s\n' "$(nmcli -g connection.autoconnect-priority connection show "$id")" \
            "${state:-saved}" "$(nmcli -g connection.id connection show "$id")"
    done | sort -rn
}

add() {
    local name=$1 prio=${2:-0} pass again
    need_number "$prio"
    read -rsp "Password for $name (blank for an open network): " pass; echo
    if [[ -n $pass ]]; then
        read -rsp "Same password again: " again; echo
        [[ $pass == "$again" ]] || { echo "They don't match, nothing changed." >&2; exit 1; }
        (( ${#pass} >= 8 && ${#pass} <= 63 )) || { echo "A Wi-Fi password is 8-63 characters, nothing changed." >&2; exit 1; }
    fi
    known "$name" || sudo nmcli connection add type wifi con-name "$name" ssid "$name" \
        connection.interface-name "" >/dev/null
    sudo nmcli connection modify id "$name" connection.autoconnect yes connection.autoconnect-priority "$prio"
    if [[ -n $pass ]]; then
        sudo nmcli connection modify id "$name" wifi-sec.key-mgmt wpa-psk
        printf 'set wifi-sec.psk %s\nsave\nyes\nquit\n' "$pass" | sudo nmcli connection edit id "$name" >/dev/null
    else
        sudo nmcli connection modify id "$name" remove 802-11-wireless-security
    fi
    list
}

case ${1:-list} in
    list) list ;;
    add)
        name=${2:-} prio=${3:-}
        if [[ -z $name ]]; then
            read -rp "Network name, exactly as your phone shows it: " name
            read -rp "Priority, highest wins (Enter for 0): " prio
        fi
        [[ -n $name ]] || { echo "No name, nothing changed." >&2; exit 1; }
        add "$name" "${prio:-0}" ;;
    priority)
        [[ $# -eq 3 ]] || { echo "wifi.sh priority <name> <n>" >&2; exit 1; }
        need_known "$2"; need_number "$3"
        sudo nmcli connection modify id "$2" connection.autoconnect-priority "$3"
        list ;;
    remove)
        [[ $# -eq 2 ]] || { echo "wifi.sh remove <name>" >&2; exit 1; }
        need_known "$2"
        sudo nmcli connection delete id "$2" >/dev/null
        list ;;
    *) sed -n '/^#   wifi.sh/s/^#   //p' "$0"; exit 1 ;;
esac
