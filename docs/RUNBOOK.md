# Runbook entry point

The maintained executable sequence is **[EXPERIMENT_COMMANDS.txt](EXPERIMENT_COMMANDS.txt)**.
It covers software setup, matched RFsim/hardware single-cell and F1 runs, live demonstration,
CSV/PNG outputs, replay, comparison and shutdown.

Use [HARDWARE_SETUP.md](HARDWARE_SETUP.md) for current inventory/links,
[RF_SAFETY.md](RF_SAFETY.md) for commissioning, and [CONCEPTS.md](CONCEPTS.md) for terminology.
Future equipment and UE choices are in [FUTURE_EVALUATIONS.md](FUTURE_EVALUATIONS.md).

The sole operator configuration is `configs/lab_n78.yaml`. The old numbered experiment folders
were consolidated; historical raw evidence is indexed in `evidence/README.md`. Software test
fixtures remain under `tests/fixtures` and are not commissioning instructions.
