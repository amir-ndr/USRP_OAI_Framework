# Changelog

## 2026-11-11 — dedicated UE network commissioning

- Added the detected UE adapter enx9c69d381261e to the operator configuration; persistent
  NetworkManager profile oaiho-ue-x310 uses host 192.168.10.11/32 and an on-link UE /32 route.
- Original cell adapter restored by the operator; both radios answer independently and actual
  route checks pass. VM management/internet unchanged; inactive B was not opened.
- Concurrent RX-only 15.36 MS/s transport check failed with sequence errors and timeouts.
  Successful benchmark process exits do not qualify streaming. RF transmission remains blocked.
- Updated setup/command guides and saved raw network/benchmark evidence. No RF safety flag changed.

## 0.3.0 — unified laboratory workflow, 2026-11-09

- One operator configuration, configs/lab_n78.yaml, with A+C active and B inactive by default;
  matched RFsim/hardware session commands. Recorded operator authorization 3300–3800 MHz / 20 dBm.
- Minimum current gain requests: cells 0.5 dB, nrUE 0 dB. Hardware remains blocked pending qualification.
- Nominal channel bandwidth used for licensed/band containment, distinct from occupied PRB grid.
- Explicit qualified source envelopes covering profile, frequency, gain and antenna gain; free-space
  geometry/gain-derived power remain planning only. Active paths only are required for a single cell.
- Actual host route/interface/speed checks before hardware ownership; doctor follows active devices.
- RX inspection includes C and active cells at the full profile rate. PSS monitoring uses correct SCS;
  passive tools preview by default and reject executed hardware inspection in RFsim mode.
- Deterministic CU->first DU->UE attach/PCI check->second DU startup. F1 session scope is explicit.
- Failures/rejected requests/incomplete probe coverage stay visible. Relevant startup errors produce
  summaries; session-started core containers are stopped while pre-existing containers are preserved.
- CSV exports for packets, per-HO/run metrics, radio observations, counters/checks; PNG diagnostics
  and traffic report. Missing data is labelled; reply gaps are not called exact radio interruption.
- Inactive dashboard cells labelled; single-cell trigger control hidden. Calibration reports renamed
  from numbered-experiment naming and their PSS/service claims corrected.
- Consolidated numbered experiment folders/operator examples, retained legacy YAML as test fixtures,
  removed duplicate/generated logs and kept recovery/hardware and selected raw RFsim evidence.
- New plain-text command guide, command reference, concepts (including n78/n66/N6), hardware/RF guide,
  current limitations, future parts/UE options and channel-emulator assessment. No emulator implemented.
- 100 tests pass; unified single/two-cell RFsim rehearsals pass. No radio transmitted.

## Earlier 0.2.x work — historical

Pinned OAI/UHD integration, verified B:0 slots, corrected gain/subdevice/UICC include handling,
RF budget and reactive interlock, initial dashboard/event/traffic analysis, active-cell support,
OAI USRP build, approved third-X310 FPGA recovery record and receive-only qualification.
Source review identified RSRP/A3 caveats and corrected local-address traffic and censored-window
reporting. Older software/RFsim runs were qualification evidence, not hardware handover results.
Original older files are mapped or archived in evidence/; their names are not current run commands.
