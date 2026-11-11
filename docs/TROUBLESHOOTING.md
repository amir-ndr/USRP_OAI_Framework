# Troubleshooting

Read the failed check before raising power or changing PHY values. Use configs/lab_n78.yaml and
EXPERIMENT_COMMANDS.txt; test-fixture YAML is not a hardware troubleshooting shortcut.

| Symptom | Check / action |
|---|---|
| `nic_ue` or `nic_du1` absent | These are planned-link placeholders. Attach a real adapter, configure its route and use its actual Linux name |
| Separate radio cables but transport refusal | Inspect the shared switch-to-host uplink. Two n78 streams exceed one 1 GbE link |
| Route differs from `usrp_link` | `ip route get RADIO_IP`; apply the intended dedicated /32 route or correct the shared-link mapping |
| Gain/frequency power envelope refusal | Requalify the changed gain/profile/channel; do not edit evidence to claim an old envelope covers it |
| `tx_power_bounds.*.peak_dbm` missing | Absolute RF peak qualification is pending; `oaiho gains` and receive dBFS are not substitutes |
| `ota` planning-only refusal | Supply qualified minimum coupling/path-loss bounds, remove ota, and retain real evidence |
| UE cannot find a cell | Confirm slot B, RX2 reception, n78 antenna suitability, actual frequency and SSB configuration |
| PSS detected but no attachment | PSS alone is not complete cell decoding; inspect SIB1, random access, timing and registration |
| Random access/Msg3 fails | Inspect UE UL and gNB RX paths, reference lock, saturation and timing. Qualify timing adjustments against pinned OAI guidance |
| Authentication/registration fails | Match PLMN, subscriber K/OPc, DNN and core subscriber/profile; do not publish secrets |
| PDU up, no traffic | Inspect new tunnel address, explicit probe interface, endpoint and UPF route; rule out host-local shortcuts |
| Probe ended early / capture incomplete | Read probe.console; preserve the failed capture. Do not report a partial zero-loss capture as a successful full experiment |
| Scheduled trigger refused | Inspect trigger/session journals and serving UE/DU. Two active cells and independent attachment qualification are required |
| Acknowledged request, no completed transfer | Look for the specific CU handover outcome and target-side RRC/access; an acknowledgement is not completion |
| B appears while it should be inactive | Check the run's saved active_cells, not a later-edited base YAML; stop unrelated modem sessions |
| UHD underflow/overflow/late/sequence errors | Inspect NIC counters, actual link capacity, VM/CPU scheduling and buffers; run no-HO baseline |
| RX clipping | Lower receiver gain or change a qualified RF path. Lower RX gain does not prevent RF-input damage |
| Reference/PPS error | Check OctoClock source, REF IN/PPS cabling, ref_locked and active-DU common epoch |
| OAI pin/hash mismatch | Restore or deliberately qualify/update source and adapter together; do not disable integrity checks |
| UHD/FPGA incompatibility | Confirm actual loaded UHD 4.11 path and matching image; firmware changes require a separate controlled procedure |
| Dashboard blank or bind failure | Check dashboard.console, port 8050 ownership and HTTP loopback access in the same VM |
| No RSRP chart | Expected with periodic_reports false; do not enable unqualified reports just to fill a chart |
| `session` refuses N2 | Expected: N2 orchestration/identity mapping is not implemented in this runner |
| Core remains after run | Pre-existing containers or --keep-core are retained; inspect summary.info and docker ps |

Ctrl+C requests cleanup. `bash scripts/stop_all.sh` is deliberately broad and may interrupt other
project sessions on the host. Check for remaining modem processes afterward. Disable transmitters
before RF rewiring and investigate physical faults using the lab's RF procedure.
