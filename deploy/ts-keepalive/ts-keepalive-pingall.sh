#!/usr/bin/env bash
# Reachability to this node is per-peer on symmetric-NAT/hostile WiFi and only
# refreshes when THIS node sends outbound. So each cycle we initiate a Tailscale
# path to every known peer (online or offline) to keep them all warm.
#
# `ts-keepalive-pingall.sh list` prints the peers it would ping (debug aid).
set -u
TS="$(command -v tailscale || echo /usr/bin/tailscale)"

ips="$("$TS" status --json 2>/dev/null | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
self_ips = set((d.get("Self") or {}).get("TailscaleIPs") or [])
for p in (d.get("Peer") or {}).values():
    for ip in (p.get("TailscaleIPs") or []):
        if "." in ip and ip not in self_ips:   # IPv4 only, never self
            print(ip)
            break
')"

if [ "${1:-}" = "list" ]; then printf '%s\n' "$ips"; exit 0; fi

[ -z "$ips" ] && exit 0

# Parallel + per-ping timeout + hard outer cap: one slow/offline peer can't
# stall the cycle, and a misbehaving ping can't hang the service.
for ip in $ips; do
    timeout 6 "$TS" ping --c 1 --timeout 4s "$ip" >/dev/null 2>&1 &
done
wait
exit 0
