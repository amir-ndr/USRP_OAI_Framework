#!/usr/bin/env bash
# Bring up the USRP link on this VM after plugging in / reconnecting the USB 5GbE adapter.
# NetworkManager ignores this NIC on purpose (/etc/NetworkManager/conf.d/99-usrp-x310.conf),
# so the addresses are set here. Safe to run repeatedly. Nothing here talks to the radios.
set -euo pipefail
IF=${1:-enx00e04c680014}
ip link show "$IF" >/dev/null || { echo "$IF not present: connect the adapter to the VM (VM > Removable Devices)"; exit 1; }
sudo ip link set "$IF" up
for a in 192.168.10.1/24 192.168.20.1/24; do sudo ip addr replace "$a" dev "$IF"; done
# Larger NIC receive ring (r8152 defaults to 100) and UHD-recommended socket buffers.
sudo ethtool -G "$IF" rx 4096 2>/dev/null || echo "note: could not enlarge the RX ring"
sudo sysctl -q -w net.core.rmem_max=33554432 net.core.wmem_max=33554432
sleep 2
ip -br addr show "$IF"
echo "link: $(cat /sys/class/net/$IF/speed 2>/dev/null) Mb/s"
for ip in 192.168.10.2 192.168.20.6 192.168.10.3; do
  if ping -c 1 -W 1 -q "$ip" >/dev/null; then echo "$ip answers"; else echo "$ip NO ANSWER"; fi
done
