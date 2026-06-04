# Tailscale ping-all keepalive

## Why this exists

eVOLVER-001 is reached over Tailscale on Yale WiFi, which sits behind a
**symmetric/hard NAT that reaps idle connections**. On such a network, Tailscale
reachability to the Pi is **per-peer**: a peer can only reach the Pi *after the
Pi has initiated a path to that specific peer*, and those paths go stale when
idle.

Things that do **not** fix this (all observed empirically):
- Pinging an always-on public cloud node — the Pi reaches it *directly*
  (outbound to a public IP works through symmetric NAT), so the ping is a no-op
  that never forces any path repair.
- `tailscale netcheck` / `tailscale status` — measure/read only.
- Keeping one peer (e.g. a laptop) warm — maintains *that* peer's path only. A
  different or newly-added peer still can't reach the Pi until the Pi pings it.

What works: the Pi periodically **initiates a Tailscale path to every enrolled
peer**. That is what this keepalive does.

This is a workaround for a hostile network. The real fixes are **wired
Ethernet** (removes the symmetric NAT) or a **reverse-tunnel rendezvous** through
an always-on node (the Pi holds one outbound connection; clients reach it via
that node).

## What it does

A systemd timer runs `ts-keepalive-pingall.sh` every 20 s. The script
enumerates all peers from `tailscale status --json` (the eVOLVER itself is
excluded — it is not under `.Peer`, plus an explicit self-IP guard) and
`tailscale ping`s each in parallel, with per-ping and hard outer timeouts so it
can never hang or fail its unit.

## Install (on the Pi)

```bash
cd deploy/ts-keepalive
sudo install -m 0755 ts-keepalive-pingall.sh /usr/local/bin/ts-keepalive-pingall.sh
sudo install -m 0644 ts-keepalive.service /etc/systemd/system/ts-keepalive.service
sudo install -m 0644 ts-keepalive.timer   /etc/systemd/system/ts-keepalive.timer
sudo systemctl daemon-reload
sudo systemctl enable --now ts-keepalive.timer
```

## Verify

```bash
/usr/local/bin/ts-keepalive-pingall.sh list      # peers it will ping (self excluded)
systemctl list-timers ts-keepalive.timer
journalctl -u ts-keepalive.service -n 5
```

Real test: take the laptop fully off the tailnet and confirm another enrolled
device (e.g. a phone) still reaches the dashboard/SSH with **no** manual ping.

## Caveat

The loop only pings peers the Pi already knows about. A **brand-new** device may
not be reachable until the Pi's netmap learns it; bridge that with a one-time
`tailscale ping <newdevice>` from the Pi at enrollment.
