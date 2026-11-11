# USRP / OAI handover laboratory

One configuration and one session command for OAI RFsim and real X310 experiments.
OAI supplies the 5G implementation; Python prepares, supervises, displays and measures it.

Start with **[the step-by-step command guide](docs/EXPERIMENT_COMMANDS.txt)** and
[the concepts reference](docs/CONCEPTS.md). The operator configuration is
**[configs/lab_n78.yaml](configs/lab_n78.yaml)**. YAML files under `tests/fixtures`
are software test inputs, not recommended lab configurations.

## Current bench

| Role | Radio | State |
|---|---|---|
| Cell A, DU0 / PCI 0 | X310 192.168.10.2, UBX-160 in slot B | TX/RX and RX2 antennas fitted |
| Cell B, DU1 / PCI 1 | X310 192.168.20.6, UBX-160 in slot B | Inactive; excluded by default |
| OAI UE C | X310 192.168.10.5, UBX-160 in slot B | TX/RX and RX2 antennas fitted |
| Ethernet | A via original adapter; C via dedicated enx9c69d381261e | Both radios respond separately; concurrent full-rate RX failed (2026-11-11) |
| Authorization | Operator reports 3300–3800 MHz, maximum EIRP 20 dBm | Recorded; document/location conditions need checking |
| RF | 3604.8 MHz, nominal 10 MHz channel | Antenna suitability and power/coupling qualification pending |

**With B inactive there is no second cell for a real handover.** Today's hardware target is
single-cell attachment and traffic, after RF and transport qualification. Two-cell handover
can be rehearsed in RFsim. VERT2450 antennas have no specified n78 performance. No RF
transmitter was enabled during this framework update.

## Run

```bash
cd ~/Downloads/USRP_5g_OAI
source .venv/bin/activate
python -m pip install -e .
E=configs/lab_n78.yaml
oaiho session "$E" --set mode=rfsim --cells du0,du1 --duration 120 --ho-every 20 --out runs/rfsim_ho_001
```

Without `--execute`, session prints its plan. Add `--execute` to start it.
RFsim does not open the USRPs. Hardware uses `--set mode=hardware` and remains blocked
until the documented live checks pass. Never change a verification flag just to bypass a refusal.

| Scenario | Selection | Status |
|---|---|---|
| RFsim attachment | `--set mode=rfsim --cells du0` | Supported |
| RFsim manual F1 handover | `--set mode=rfsim --cells du0,du1 --ho-every 20` | Supported |
| Real attachment | `--set mode=hardware --cells du0` | Implemented; RF/transport prerequisites pending |
| Real manual F1 handover | `--set mode=hardware --cells du0,du1 --ho-every 20` | Requires B and all three qualified paths |
| Passive RF inspection | `rx-check`, `rx-monitor` | Receive-only; preview unless `--execute` |
| A3 / N2 / modem or phone | [Future evaluations](docs/FUTURE_EVALUATIONS.md) | Separate qualification; session supports F1 |
| SDR channel emulator | [Assessment](docs/CHANNEL_EMULATOR.md) | No implementation |

`--ho-every` sends timed **manual network requests**; it does not implement A3 or movement.

## Live demonstration and outputs

Open **http://127.0.0.1:8050** while an executed session runs. The dashboard shows serving
cell, protocol events, RTT/loss, available radio observations and handovers. Inactive cells
are labelled. The trigger button is enabled only for two-cell live sessions. Missing
observations remain missing; nrUE periodic RSRP is unqualified at this source pin.

Each run keeps its effective configuration, OAI configs, manifest, raw logs and summary:

| Artifact | Contents |
|---|---|
| `summary.md`, `summary.json` | Findings and run status |
| `events.csv` | Protocol handover events and evidence |
| `packets.csv` | Individual probe records |
| `handover_metrics.csv` | Per-handover signaling and traffic-window metrics |
| `metrics.csv` | Run-level traffic and handover metrics |
| `radio_metrics.csv` | Available MAC measurements, units and timestamp basis |
| `transport.csv`, `checks.csv` | Stream counters and PASS/CHECK/FAIL checks |
| `handover.png`, `radio_metrics.png` | Figures; explicit no-data labels where appropriate |
| `report/traffic.png`, `report/report.html` | Traffic plot/report when usable packets exist |
| `artifacts.json` | Top-level CSV/PNG inventory |

Failures after rendering still produce diagnostic artifacts. Preflight refusals before
rendering create no measurement run. Exit code 2 indicates a FAIL finding. CHECK still
needs interpretation. Reply gaps are not exact radio interruption measurements.

Ctrl+C requests ordered shutdown. The session stops core containers it started; existing
containers remain running. `--keep-core` and `--keep-dashboard` explicitly retain services.
`scripts/stop_all.sh` is a broad emergency stop for OAI processes on this host.

## Documentation

- [Commands from setup to comparison](docs/EXPERIMENT_COMMANDS.txt)
- [Concepts: OAI, USRP, n78/n66, N6 and handover](docs/CONCEPTS.md)
- [Hardware and Ethernet](docs/HARDWARE_SETUP.md), [RF safety](docs/RF_SAFETY.md)
- [Study design](docs/EXPERIMENTS.md), [metrics](docs/RESULTS.md)
- [Future parts/options](docs/FUTURE_EVALUATIONS.md), [UE setup](docs/UE_SETUP.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md), [extending the code](docs/EXTENDING.md)
- [Sources](docs/SOURCES.md), [validation](VALIDATION.md), [retained evidence/cleanup](evidence/README.md)

Pinned OAI: `1143f7500e5e5a9cd258148f8429230cc2759554`; UHD 4.11. OAI source changes
require a deliberately qualified new pin; integrity checks remain enabled.

A matched two-cell RFsim pilot is saved in [runs/comparison_001/comparison.html](runs/comparison_001/comparison.html),
with [CSV](runs/comparison_001/comparison.csv) and [PNG](runs/comparison_001/comparison.png). One run
per condition demonstrates the workflow; it does not establish a performance conclusion.
