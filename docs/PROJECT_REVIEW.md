# Pinned OAI measurement and A3 findings

Source audit of OAI `1143f7500e5e5a9cd258148f8429230cc2759554`, 2026-11-09.
The maintained workflow is configs/lab_n78.yaml and EXPERIMENT_COMMANDS.txt. Earlier numbered
experiments/configs were consolidated; this document retains the source-specific research findings.

## Actual measurement support

The handover tutorial's statement that nrUE has no measurement reporting is stale at this pin.
Local `doc/FEATURE_SET.md:512` lists periodic/A2/A3 reports, RSRP only. Relevant implementations:

- `openair2/RRC/NR_UE/rrc_UE.c`: periodic report initialization, neighbor processing and encoding.
- `openair2/RRC/NR_UE/rrc_timers_and_constants.c`: periodic/A3 timer expiry and reporting.
- `openair1/SCHED_NR_UE/phy_procedures_nr_ue.c`: neighbor measurement task activation/results.

UHD alone does not know cellular RSRP; OAI derives it from the waveform. RSRQ/SINR are not included
in those nrUE RRC reports. MAC UL SNR, dBFS RSSI and other PHY diagnostics are different sources.

A historical n66 RFsim rehearsal encoded 2674 MeasurementReports in about 2.5 minutes despite a
configured 1024 ms period, and reported PCI 1 at serving RSRP while DU1 was absent. Its raw evidence
is retained as `evidence/rfsim/n66_measurement_caveat_20261109.tar.gz`. Treat reports as unqualified
until both identity and cadence/level response have been checked. RFsim artifacts need not predict
hardware behavior; that uncertainty is a reason to test, not a reason to trust the measurements.

## A3 units and timer behavior

ASN.1 A3 offset/hysteresis use half-dB units. With other offsets zero, entry is approximately a
neighbor advantage exceeding offset plus entry hysteresis, sustained for TTT. The generator
correctly doubles human dB values and maps human milliseconds to the TTT enum. At the pinned OAI:

1. `rrc_UE.c:3093` converts offset with integer `>> 1`, losing half-dB resolution.
2. `rrc_UE.c:3094` uses hysteresis directly as dB, doubling its intended value.
3. `openair2/RRC/NR/rrc_gNB.c:1867` scales offset by 0.5 but not hysteresis in its additional N2 check.
4. Pending nrUE A3 timing is reset against the leaving condition rather than all losses of the entry
   condition; a fluctuation into the hysteresis region can preserve the pending timer.

For YAML offset 3 dB and hysteresis 1 dB, the UE's inspected test becomes 5 dB rather than the
intended 4 dB, assuming other offsets are zero. A parameter table cannot correct an implementation
that uses different units/state behavior than the claimed standard.

`patches/oai-a3-units-1143f7500e.patch` is an **unapplied candidate** for part of these findings.
It previously passed `git apply --check`, but was not built or run and does not repair the full
per-cell event/timer implementation. Do not apply it to the clean checkout and disable pin checks.
Commit and qualify a reviewed change, update the source/template adapter, rebuild, and repeat RFsim,
independent hardware attachment, manual handover and measurement tests before A3 comparisons.

## Qualification sequence

Start manually with periodic reports disabled. Verify B absent means no B report; add B and verify
its identity; change known relative levels and check the expected response. Verify report timing,
filtering, entry/exit and TTT reset behavior near thresholds. Keep raw measurements, decision time,
request, command and actual outcome. A phone can provide another measurement implementation, but
does not fix network-side decision logic or establish automatic compatibility.

N2 comparisons additionally require explicit subscriber identity/clock correlation across gNBs.
Automatic N2 event pairing remains disabled. F1 manual is the current reference procedure.
