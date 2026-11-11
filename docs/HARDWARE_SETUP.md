# Hardware setup and current inventory

Updated 2026-11-11. The [command guide](EXPERIMENT_COMMANDS.txt) is the operating procedure.

| Role | Address / serial | RF frontend | Default |
|---|---|---|---|
| A / DU0 | 192.168.10.2 / 34BA717 | UBX-160 v2, B:0 | Active |
| B / DU1 | 192.168.20.6 / 34BA72C | UBX-160 v2, B:0 | Inactive |
| C / OAI UE | 192.168.10.5 / 34AF675 | UBX-160 v2, B:0 | Active |
| OctoClock-G | 192.168.10.3 / 3505F67 | 10 MHz / PPS distribution | Shared reference |

The user confirmed antennas on both TX/RX and RX2 of A and C. Five antennas were reported in
all, including B's existing antenna; B would need the additional RX2 antenna for the current
separate-port design. This is a count, not confirmation that those antennas work at n78.
The previously identified VERT2450 is specified for 2.4/5 GHz. Use actual n78 characterization
or matched antennas; no claim of reliable 3.6 GHz operation follows from proximity alone.

## Current Ethernet topology

```text
A 192.168.10.2 -- D-Link switch -- enx00e04c680014 -- Ubuntu VM (host 192.168.10.1/24)
C 192.168.10.5 -- direct cable -- enx9c69d381261e -- Ubuntu VM (host 192.168.10.11/32)
B 192.168.20.6: inactive; future dedicated host path still pending
```

Both adapters are now visible in Ubuntu on USB 3 links. The new UE adapter reports 1 GbE and
answered 3/3 UE pings on 2026-11-11. NetworkManager profile `oaiho-ue-x310` assigns 192.168.10.11/32,
adds an on-link /32 route to 192.168.10.5, and does not install an internet default route or IPv6.
The VM management interface ens33 was left unchanged.

The original adapter was down after reconnection. Its root-owned
`/etc/NetworkManager/conf.d/99-usrp-x310.conf` marks it strictly unmanaged. It must be restored using
`bash scripts/usrp_link_up.sh` in an Ubuntu terminal with local sudo authentication. This does not
change the UE's more-specific /32 route. This restoration was completed and A answered 3/3 pings on
2026-11-11. Both actual routes pass doctor. Current evidence is in runs/ue_link_setup_20261111T050321Z.

To reconnect the UE profile after USB passthrough:

```bash
nmcli connection up oaiho-ue-x310
ip route get 192.168.10.5
```

Separate interfaces provide enough rated bandwidth for two n78 radios, about 521 Mbit/s per
radio per direction at 15.36 MS/s. They do not prove loss-free simultaneous streaming; qualify the
actual drivers, USB/VM behavior and counters before interpreting OAI measurements. The 2026-11-11
60-second concurrent receive-only check FAILED: A reported 129 RX sequence errors and 566 RX
timeouts; C reported 181 RX sequence errors, 4 overruns and 588 RX timeouts. Both processes returned
zero despite this loss. No TX stream was created; this is not an OAI attachment result. The operator clarified that
the UE adapter is already plugged directly into the laptop. No external USB hub is reported;
the earlier hub statement was corrected. Diagnose the adapter/driver/USB/VM/radio path before RF.

Earlier all radio cables merged through one 1 GbE switch-to-host uplink, which was insufficient.
For the later three-radio stage, add a third dedicated interface or a genuinely qualified
multi-gigabit shared uplink. `nic_du1` remains a placeholder, not a detected interface. The framework
checks actual routes/speeds, including /32 routes, before hardware launch.

## Clocks and ports

Connect OctoClock 10 MHz outputs to REF IN on active radios. For both DUs, connect PPS outputs
to PPS/TRIG IN and verify reference lock, advancing PPS and common epoch. The OAI UE uses
external 10 MHz but derives frame timing from its cell; UE PPS is not required by this configuration.
The default flags are deliberately unverified after physical changes. Historical A/B common-PPS
and C reference-lock observations remain in `evidence/hardware`.

Use slot-B RF connectors. TX/RX is TX; RX2 is RX with the inspected default driver settings.
RX2 is not a second transmitter or an additional receive chain on the same UBX. The DB's antenna
selection API and `B:0` subdevice selection solve different problems. See [Ettus UBX](https://kb.ettus.com/UBX).
Never attach two transmitter outputs with a T connector. Power down before RF rewiring.

## Qualification record

C's HG FPGA was updated with explicit approval and successfully probed after power cycling.
The prior image and loader/probe records remain under `evidence/hardware/third_x310_*`.
No firmware update is needed for these sessions.

Earlier RX-only tests at 7.68 MS/s found C clean alone, but all three on the shared uplink each
reported 46 sequence-error events in 60 seconds. These observations are not a 15.36 MS/s OAI
qualification. A successful benchmark process exit is not proof that counters are zero.

Before meaningful RF measurements, record actual UHD/OAI/FPGA versions, active interfaces,
negotiated speeds, transport counters, CPU/VM conditions, antenna/port geometry, gains and calibration.
Do not open discovery or a second UHD program while OAI owns the same radio.
