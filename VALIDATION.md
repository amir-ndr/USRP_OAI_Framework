# Validation status

Updated 2026-11-11. Software/RFsim qualification is separate from hardware qualification.
The software/RFsim record below is from 2026-11-09; see the dated network update.

## This framework update

- **100 tests passed** (full log: `evidence/validation/tests_20261109.txt`): configuration/render/integrity, local UDP/ping, event/censoring semantics,
  nominal-channel containment, calibration-envelope scope/EIRP, full-rate receive previews,
  30 kHz PSS detection, startup-failure artifacts, initial node order, rejected requests, incomplete
  capture findings, CSV fidelity, measurement units and owned-core cleanup behavior.
- Editable package installation was checked with `pip install -e .` in the project virtual environment.
- Hardware launch preview remains blocked by unqualified connector peak bounds. Actual-route checks
  additionally reject absent/mismatched planned interfaces. No live flags were asserted just to pass.
- Default RX preview includes active A and UE C, at 15.36 MS/s. B is excluded.
- Dashboard HTML and HTTP state were checked during the final live single-cell RFsim run and using saved replay: RFsim labelled, B inactive,
  trigger disabled for single-cell mode, and RTT bins present. The live state is saved in the final single-cell run. No new browser visual-automation qualification is claimed.
- Generated traffic PNG inspected; protocol events and CSV values checked against the raw evidence.
- An injected startup failure was tested to ensure summary, CSV and diagnostic PNGs are still written.

## Executed RFsim sessions of the unified n78 configuration

| Run | Observation duration | Attach | Traffic | Handovers |
|---|---:|---:|---:|---|
| `runs/rfsim_n78_single_002` | 45 s | 3.5 s | 2181/2181 replies, median RTT about 5.9 ms | None requested |
| `runs/rfsim_n78_two_cell_baseline_001` | 45 s | 2.5 s | 2168/2168 replies, median RTT about 6.7 ms | None requested; both DUs active |
| `runs/rfsim_n78_handover_001` | 45 s | 3.0 s | 2166/2166 replies, median RTT about 6.8 ms | 2/2 completed, PCI 0->1->0, signaling median about 33.2 ms |

Each session verified initial PCI 0, used a new tunnel address and a probe explicitly bound to
`oaitun_ue1`. Each stopped the ten core containers it started and its modem processes. Saved output
includes raw logs, events, config/manifest, CSV and PNG. Derived exports were regenerated with the
final reporting code; the underlying modem runs/raw data were not rerun or relabelled.

The single-cell run qualifies attachment; the two-cell no-handover run is the appropriate reference
for the two-cell handover run. The comparison in `runs/comparison_001` is a descriptive pilot with one run per condition.
Both conditions have zero observed lost probes; RTT p95 is about 8.09 versus 8.26 ms. These individual
values are not evidence of a significant policy effect. RFsim results are software/backend observations on one host,
not measured RF performance or proof of n78 antenna suitability.

## Hardware facts and pending work (2026-11-09 snapshot)

The user confirmed both RF ports fitted on A and C, B inactive, and separate radio cables connected
through one D-Link switch to one host uplink. Only the original adapter is visible; prior/current
host inspection identifies a shared 1 GbE path. At the n78 rate even A+C exceed that capacity.
The operator reports authorization for 3300–3800 MHz and 20 dBm maximum EIRP; document/location
conditions and RF characterization remain operator checks, not independently verified facts.

Historical evidence retained under `evidence/hardware` includes C's approved HG image update,
post-cycle probe, old image backup, reference inventory, OAI driver build and receive-only benchmarks.
At 7.68 MS/s, C alone was clean; three concurrent radios each reported 46 sequence errors/minute.
Those older checks do not qualify the new 15.36 MS/s arrangement.

Not performed in this update: USRP transmission, new RX streaming/clock configuration, real OAI UE
attachment, hardware handover, A3, N2, phone/modem provisioning, new FPGA image or channel emulator.
Actual n78 antenna performance, peak-output/coupling envelopes, concurrent full-rate transport and
single-cell RF service remain pending. See the command guide and RF_SAFETY.md.

## Historical findings retained

Selected raw older RFsim evidence is archived under `evidence/rfsim`; cleanup mapping/hashes are in
`evidence/cleanup_manifest.json`. The n66 run's unqualified measurement reporting remains relevant:
excessive periodic reports and a reported absent neighbor. The candidate A3 units patch remains
unapplied/unbuilt/unrun and does not resolve all timer behavior. See PROJECT_REVIEW.md.

## 2026-11-11 dedicated-link commissioning

After updating the UE interface configuration, all 18 tests in test_unified.py passed;
configuration-only validate passed, and all 76 maintained-file hashes were verified.
The dashboard was reopened with the title RX STREAMING FAILED / RF OFF, trigger disabled,
no serving UE, and no synthetic modem measurements.

UE C now uses enx9c69d381261e with persistent NetworkManager profile oaiho-ue-x310,
host 192.168.10.11/32 and an on-link /32 radio route. The operator restored the original
strictly unmanaged A adapter with scripts/usrp_link_up.sh. Both radios answered 3/3 pings;
actual routes pass doctor, both interfaces report 1 GbE and USB 3, and ens33 is unchanged.
This resolves the previous single-uplink topology; B remains inactive/unopened.

Full-rate **RX-only streaming FAILED**, at 15.36 MS/s sc16 with external reference:

| Phase / requested duration | Radio | RX sequence errors | RX overruns | RX timeouts | Dropped samples |
|---|---|---:|---:|---:|---:|
| Concurrent / 60 s | A | 129 | 0 | 566 | 2,242,240 |
| Concurrent / 60 s | C | 181 | 4 | 588 | 9,108,023 |
| A alone / 20 s | A | 1 | 0 | 0 | 9,828 |
| C alone / 20 s | C | 3 | 1 | 197 | 827,453 |

All benchmark processes exited zero and transmitted zero samples. The UE path stalls even
alone; adapter/USB/driver/VM investigation remains required. The adapter reports AX88179B,
0b95:1790 revision 0200, cdc_ncm on kernel 7.0.0-31-generic. This is an observed configuration,
not proof that cdc_ncm is an incorrect driver: ASIX explicitly supports CDC-NCM on this family.
No kernel/driver/FPGA change was attempted. No OAI/core/transmission was started.

Raw logs, exact commands, before/after counters and routes, CSV/PNG diagnostics and summary:
`runs/ue_link_setup_20261111T050321Z`. These unequal-duration diagnostic tests do not establish
RF throughput, full-duplex capacity, attachment or handover performance. Antenna/output/coupling
qualification and clock/PPS recheck remain pending; hardware live validation still rejects the run.

Manufacturer reference: [ASIX AX88179B](https://www.asix.com.tw/en/product/USBEthernet/Super-Speed_USB_Ethernet/AX88179B).
