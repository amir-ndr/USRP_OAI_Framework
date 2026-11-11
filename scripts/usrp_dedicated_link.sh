#!/usr/bin/env bash
# Give ONE X310 its own host link (a second/third USB Ethernet adapter), keeping the radio's IP address.
#   usage: scripts/usrp_dedicated_link.sh IFACE RADIO_IP HOST_IP
#   e.g.   scripts/usrp_dedicated_link.sh enx00e04c6800aa 192.168.10.5 192.168.10.11   # X310 C (UE)
#          scripts/usrp_dedicated_link.sh enx00e04c6800bb 192.168.20.6 192.168.20.11   # X310 B (du1)
# Cable: adapter -> that X310's 1 GbE port directly (simplest), or through the switch.
# The radio keeps its address; a /32 route sends its traffic out IFACE, and HOST_IP (an unused address in
# the radio's subnet) is the host's address on IFACE. Strict ARP keeps each address answering only on its
# own adapter, so replies come back on the right link. Safe to run repeatedly. Nothing here talks to a radio
# except the final ping. Run scripts/usrp_link_up.sh for the first adapter as before.
set -euo pipefail
IF=${1:?usage: usrp_dedicated_link.sh IFACE RADIO_IP HOST_IP}; RADIO=${2:?radio IP}; HOST=${3:?host IP on IFACE}
ip link show "$IF" >/dev/null || { echo "$IF not present: connect the adapter to the VM (VM > Removable Devices)"; exit 1; }
command -v nmcli >/dev/null && sudo nmcli device set "$IF" managed no 2>/dev/null || true   # NetworkManager: hands off
sudo ip link set "$IF" up
sudo ip addr replace "$HOST/32" dev "$IF"
sudo ip route replace "$RADIO/32" dev "$IF" src "$HOST"
# Answer ARP only for addresses configured on the receiving interface; loose reverse-path filter.
sudo sysctl -q -w net.ipv4.conf.all.arp_ignore=1 net.ipv4.conf.all.arp_announce=2 \
  "net.ipv4.conf.$IF.rp_filter=2" net.ipv4.conf.all.rp_filter=2
sudo ethtool -G "$IF" rx 4096 2>/dev/null || echo "note: could not enlarge the RX ring"
sudo sysctl -q -w net.core.rmem_max=33554432 net.core.wmem_max=33554432
sleep 2
ip -br addr show "$IF"; ip route get "$RADIO" | head -1
echo "link: $(cat /sys/class/net/$IF/speed 2>/dev/null) Mb/s"
if ping -c 2 -W 1 -q -I "$IF" "$RADIO" >/dev/null; then echo "$RADIO answers on $IF"; else echo "$RADIO NO ANSWER on $IF"; fi
echo "Put '$IF' into host.links and as that radio's usrp_link in the experiment YAML."
