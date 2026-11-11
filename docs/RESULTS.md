# Capture, analysis and interpretation

**2026-11-09 review:** see [PROJECT_REVIEW.md](PROJECT_REVIEW.md). Pass the original config with
`analyze --config` to record hardware/RFsim mode. Complete, partial and absent per-HO traffic-window
coverage are now distinguished; only complete uncensored windows enter aggregates. Missing-window
loss is null and overlapping windows do not double-count probes. Automatic N2 extraction is disabled
until identities and clocks are mapped; reviewed event CSVs can still be analyzed.

## Capture plan

Retain CU, DU/gNB, core and UE diagnostic logs, the final configuration, clock observations and the actual attenuator readback. If packet-level signaling is needed, capture relevant F1AP/NGAP/GTP-U interfaces with a tool such as tcpdump or tshark. Protect subscriber identifiers/keys in published artifacts. A pcap can contain information beyond the metrics you intend to share.

The runner writes timestamped JSONL lines under `RUN/logs/NODE/`. The `epoch_ns` is **when Python received the log line**, not necessarily when the protocol event happened. Buffering and process scheduling can bias that timestamp. OAI's own timestamp remains inside `line`. For accurate signaling measurements, use validated OAI event timestamps, packet timestamps on a defined capture clock, or instrument the relevant OAI code. Confirm granularity and synchronization before reporting milliseconds.

The source pin's F1 completion path includes source-cell context release handling. A matching log entry is useful evidence but should be correlated with target-side completion, serving-cell identity and continuing traffic. A generic `RRCReconfigurationComplete` is not automatically a handover: other reconfigurations use that message too. `oaiho candidates` only filters possible evidence; it deliberately does not label success.

## Event CSV

Required columns:

```text
epoch_s,clock_id,event,ue_id,handover_id,source_pci,target_pci,evidence
```

| Field | Meaning |
|---|---|
| `epoch_s` | Event time on your defined clock, seconds; preserve appropriate precision |
| `clock_id` | Clock/capture domain used for timing; paired events must match |
| `event` | `attempt`, `complete`, or `failure` |
| `ue_id` | Consistent identity for analysis; for N2, map local RRC IDs to the same UE explicitly |
| `handover_id` | Unique attempt identifier within that UE |
| `source_pci`, `target_pci` | Verified cell transition intended by the attempt |
| `evidence` | File/line, packet number or instrument record proving the event |

Define the start and end before comparing runs. One possible signaling interval is network procedure initiation to verified target RRC completion. Another is command transmission to completed source release. Those are different metrics; never compare them under one name. The analyzer calls the chosen interval `signaling_duration_ms` and cannot determine whether your chosen boundaries are appropriate.

Keep an `attempt` without a terminal event when the outcome is unknown/capture-censored. Use `failure` only with evidence meeting a predeclared failure criterion. Do not invent a completion to make an analysis run. Duplicate or mismatched terminal events are rejected. The same `clock_id` is a declaration of a shared timing basis, not an automatic time-synchronization correction.

## UDP metrics

The client sends sequence-numbered probes at a target interval and measures RTT with its local monotonic clock, so RTT does not require inter-host time synchronization. Lost replies are not directional measurements: either the uplink probe or downlink response can fail. The echo server's scheduling also contributes to RTT.

| Output | Exact interpretation |
|---|---|
| `loss_fraction` | Lost replies divided by received + lost records; interrupted/censored records excluded and counted separately |
| `rtt_median_ms`, `rtt_p95_ms` | RTT statistics of received probes only |
| `max_interarrival_gap_ms` | Largest gap between received replies over the whole capture |
| `loss_bursts` | Consecutive missing probes with send-time span to the next successful probe; edge bursts are marked censored |
| `probe_interval_s` | Requested cadence; actual send timestamps are retained |

An interarrival gap includes normal spacing, changing latency, scheduling and any interruption. A 20 ms probe interval cannot resolve an exact 1 ms service interruption. A lost-probe span is an outage proxy with sampling and timeout limitations. Neither is a protocol-defined L2 interruption measure. Do not subtract one nominal interval and present the answer as exact.

The `traffic` section covers the **complete input capture**. Each paired HO event also has a window
from attempt−1 s to terminal-event+2 s when probe and event clocks match. Check its coverage status;
partial/absent evidence is excluded from window aggregates. Failure windows end relative to failure,
not a fabricated completion; unknown attempts have no paired window. Do not attribute an unrelated
earlier capture-wide gap to a later handover.

Use a lightweight probe stream for interruption/latency observations and separate controlled throughput experiments. A saturated link can create queuing delay that dominates handover behaviour. For optional iperf3 measurements, retain both server and client output, offered rate, direction, duration and JSON:

```bash
# On the data endpoint:
iperf3 -s
# On the UE-side host, with the actual session address:
iperf3 -c DATA_ENDPOINT_IP -B UE_SESSION_IP -t 90 -i 1 -J > iperf_ul.json
iperf3 -c DATA_ENDPOINT_IP -B UE_SESSION_IP -t 90 -i 1 -R -J > iperf_dl.json
```

The framework does not currently parse iperf3, BLER, SINR or vendor-specific modem measurements automatically. These require explicit adapters and validation of units/definitions. Do not label modeled attenuation as measured RSRP or infer SINR from RX gain.

## Handover metrics

Success fraction is computed among **known terminal outcomes** only, with a Wilson 95% interval. Attempts, completions, failures and unknowns are all reported. A high fraction with many unknown outcomes is not evidence of high reliability. The Wilson interval assumes the relevant Bernoulli observations are sufficiently independent; repeated correlated transitions on the same run can violate that assumption.

The included ping-pong definition counts a verified reverse transition for the same UE, on the same clock domain, within five seconds of the preceding completed transition. It is a project definition, not an asserted universal standard. A long triangular return can be a deliberate mobility event rather than undesirable ping-pong; label the scenario.

The software demo has 250 synthetic probes, six deliberately lost replies and one explicitly synthetic 120 ms event interval. These numbers are fixtures for verification, not performance targets or estimates for your equipment.

## Compare repeated runs

Create a manifest such as:

```csv
condition,summary
F1_A3_TTT40,run01/summary.json
F1_A3_TTT40,run02/summary.json
F1_A3_TTT160,run03/summary.json
F1_A3_TTT160,run04/summary.json
```

Paths are relative to this CSV. Then:

```bash
oaiho compare --manifest comparisons.csv --out comparison_report
```

The comparison computes means and sample standard deviations of **per-run metrics** and records the number of available run values. A mean of per-run p95 RTTs is not a pooled packet p95. Missing duration values are counted through the available-run number; failed/unknown outcomes remain visible. Duplicate summary paths and mixtures of synthetic/measured summaries are rejected.

Use matched capture windows, traffic load, event definitions and initial state. For a final study, show the run distribution or empirical CDF as well as a summary, and use an appropriate across-run confidence interval/bootstrap with the experimental dependence structure preserved. Do not draw a substantive conclusion from the synthetic example or one successful hardware transfer.

## Unified session artifact locations

The current entry point is configs/lab_n78.yaml and EXPERIMENT_COMMANDS.txt. At session end,
top-level packets.csv, handover_metrics.csv, metrics.csv, radio_metrics.csv, transport.csv and
checks.csv accompany events.csv and raw logs. handover.png/radio_metrics.png explicitly identify
absent observations. report/traffic.png and report/report.html require usable traffic records.
observed_send_span_s reports the actual first-to-last probe-send timestamp span; a process that
exited successfully without covering the planned interval cannot receive a traffic PASS.

radio_metrics.csv preserves source event and units: UL RSSI is dBFS, UL SNR is dB, reported RSRP
is identified as UE-reported, and unconverted neighbor log values remain explicitly raw.
MAC goodput/BLER observations have OAI's reporting semantics; they are not an iperf or calibrated
RF analyzer measurement. The pinned nrUE report defects remain unresolved.

The top-level summary.json describes session execution/findings. For compare, use the analytical
report/summary.json. CSV/PNG formats match between RFsim and hardware; matching formats do not
establish matched physical timing. The matched two-cell pilot in runs/comparison_001 has only one run per condition; it demonstrates
the comparison workflow and does not establish statistical significance.

`compare` also saves comparison.png with per-run dots, available sample counts and explicit labels
for absent metrics. The PNG includes RTT, reply gap, handover and loss panels; its bars are means,
not confidence intervals.
