# Command reference

Use [EXPERIMENT_COMMANDS.txt](EXPERIMENT_COMMANDS.txt) for the actual sequence.
Run `oaiho COMMAND --help` for argument syntax. Config commands accept repeatable `--set key=value`;
list entries can be addressed by cell name, for example `cells.du0.att_tx_db=31`.
Overrides are validated; a session saves its effective configuration.

| Command | Purpose | Side effects |
|---|---|---|
| `profiles` | List pinned PHY profiles, nominal bandwidth and sample rate | None |
| `validate CFG` | Configuration checks; `--live` adds RF/live preconditions | None; no USRP opens |
| `gains CFG` | Requested UHD gain and OAI calibration mapping | None; no absolute RF measurement |
| `budget CFG` | Sum supplied RF-path peak bounds | None; planning until inputs are qualified |
| `ota CFG` | Free-space-loss planning table | None; cannot certify receiver protection |
| `doctor CFG [--ping]` | Build, libraries, host links/routes, core and optional IP reachability | Reads host state; optional network pings |
| `inventory CFG` | Active radio identity commands | Preview; `--execute` initializes UHD without streaming |
| `clock-check CFG` | Active cell reference/PPS checks; pair check with two cells | Preview; `--execute` initializes clock settings, no RF samples |
| `rx-check CFG` | Active cells plus UE receive-only spectrum/levels | Preview; `--execute` receives, writes JSON/PNG with `--out` |
| `rx-monitor CFG` | PSS N_ID2, period and ADC-relative levels on UE radio | Preview; `--execute` receives/writes JSONL; cannot share C with nrUE |
| `tone-cal CFG` | Protected conducted weak-CW commissioning aid | Preview; `--execute` transmits unless `--dry-run`; no absolute calibration claim |
| `calibration-report` | Analyze prior receive-only calibration captures | Writes report; no radio |
| `fetch` | Copy/hash-check templates from pinned OAI checkout | Writes cache; no modem |
| `render CFG` | Generate OAI configs, hashes and command manifest | Writes a new directory; no modem |
| `run-node CFG` | Supervise one generated CU/DU/UE command | Preview; `--execute` launches OAI, hardware nodes may transmit |
| `session CFG` | Supported F1 workflow, dashboard, traffic, triggers, cleanup and artifacts | Preview; `--execute` starts services; hardware may transmit |
| `dashboard CFG` | Live file-following or saved-run replay | Opens loopback HTTP listener; optional explicit trigger controls |
| `trigger CFG` | Request manual F1/N2 transfer in an existing setup | Preview; `--execute` sends telnet control; not proof of completion |
| `echo` | UDP measurement endpoint | Starts listener immediately |
| `probe` | UDP/ICMP/ping traffic measurement | Sends traffic and writes JSONL immediately |
| `events CFG` | Extract supported F1 attempts/outcomes from saved logs | Writes CSV and details; N2 correlation needs reviewed manual events |
| `analyze` | Traffic and explicit handover-window metrics | Writes JSON/CSV/Markdown; `--plots` adds PNG/HTML |
| `compare` | Aggregate explicitly grouped independent run reports | Writes comparison CSV/PNG/HTML; mixed modes require explicit flag |
| `trace` | Generate scalar attenuation trajectory | Writes CSV; does not change RF |
| `replay CFG` | Apply a scalar trace through a qualified attenuator adapter | Preview; `--execute` changes hardware attenuation, not RFsim samples |
| `sweep CFG` | Randomize parameter conditions/repetitions | Writes configurations only; no automatic RF launch |
| `demo` | Create labelled synthetic software-test data/report | Writes synthetic artifacts, never a performance result |
| `candidates` | Filter possible relevant log lines | Writes unclassified evidence; no success inference |

The `session` runner implements F1 orchestration. Retained N2 renderer/control utilities are advanced
building blocks, not a promise of a working N2 deployment. `--no-dashboard` disables the automatic
live view; `--keep-dashboard` keeps it after the run. `--no-core` delegates core lifecycle to you;
`--keep-core` keeps session-started core containers. Already running containers are preserved by default.

New run/report directories prevent accidental overwrite. Preview is not a sandbox simulation of the
hardware: it checks configuration and prints intent. It cannot check actual physical RF paths.
