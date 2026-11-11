# Experiment design

Use the unified configuration and commands in EXPERIMENT_COMMANDS.txt. This document explains
what to compare and when a comparison is meaningful. It does not promise results before RF commissioning.

## Stages and evidence

| Stage | Change | Required evidence |
|---|---|---|
| RFsim single cell | Software backend, one DU | New PDU session, initial PCI, tunnel-bound traffic, complete capture |
| RFsim two cells | Add DU1, fixed manual schedule | Matched attempt/completion events, actual source/target transition, continuing traffic |
| Real single cell | Hardware backend, A+C; later B+C | Qualified RF/transport, independent attach and traffic on each cell |
| Real manual F1 | Add both qualified cells | Same session survives transfer; failures/unknowns retained |
| Controlled level crossing | Qualified programmable loss on both cell branches | Actual actuator readback/timestamps; viable target and source baseline |
| A3 | Validated measurements and corrected OAI implementation | Report condition/TTT consistent with network decision and outcome |
| N2 | Separately qualified two-gNB/core orchestration | Subscriber identity/clock mapping and explicit comparable procedure boundaries |

Current B is inactive: hardware stage remains single cell. A3, N2 and SDR emulation are future work.

## First comparison

Keep BOTH DUs active in the no-handover reference. Run it and a manual-handover condition with
the same mode, PHY, UE,
traffic, geometry, gains, CPU/NIC setup, warm-up and observation duration. Use independent new
sessions as repetitions. Randomize condition order and record repetition/seed metadata using --set.
Do not count toggles within one session as independent experimental repetitions.

Start with a small pilot to estimate variability and failure modes, then choose the number of
independent repetitions needed for the effect of interest. Five repetitions is a starting pilot,
not a statistical guarantee. Preserve invalid/failed run records with explicit reasons.

## Useful factors

| Factor | Appropriate study | Caveat |
|---|---|---|
| Manual requests versus no requests | Baseline added signaling/traffic disruption | Keep target viability and initial cell fixed |
| TX/RX gain | Link/gain sensitivity inside a qualified envelope | TX gain is not dBm; avoid RX clipping and recalibrate changed conditions |
| Traffic load/direction | Light latency probe, then separate controlled load | Saturation/queueing can dominate measured gaps |
| Attenuation ramp/step/return | Repeatable slow coverage scenarios | Fixed protection remains; scalar loss is not multipath/Doppler |
| A3 offset/hysteresis/TTT | Automatic decision tradeoffs after qualification | These do not drive timed manual requests |
| F1 versus N2 | Procedure architecture after both independently work | Match boundaries, identity mapping, PHY and RF conditions |

The current minimum gain cap is 0.5 dB. A higher-gain sweep requires a new qualified power/coupling
envelope; do not copy earlier 6 dB examples as automatic approval. Do not tune sample rate independently
of PHY/numerology/transport. Changing profile is a different condition, not a latency optimization knob.

## Metrics to report

- Planned/dispatched/rejected requests and protocol attempts, completed/failed/unknown outcomes.
- Signaling duration with its exact boundaries and clock basis; median/p95 across suitable observations.
- Probe loss, censored count, RTT distribution, largest reply gaps and loss bursts.
- Per-HO window coverage; exclude partial/censored windows from aggregate window comparisons.
- Initial/target cell identities, session continuity and RLF/re-establishment events.
- Available RSRP/SNR/RSSI/BLER/goodput with source, units and qualification status.
- UHD and NIC errors plus host workload; do not attribute transport-induced gaps solely to handover.

Compare per-run values/distributions. Known-outcome success excludes unknowns; always show their
count. Confidence intervals require a justified independence structure. Reply gaps from a 20 ms
probe are an application-level outage proxy, not exact L2 interruption. RFsim and hardware can be
shown side by side with mode labels, not pooled into one physical-performance claim.

See RESULTS.md for metric definitions and CHANNELS.md for what each channel actuator models.
