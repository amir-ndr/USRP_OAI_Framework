# Experiment entry point

The numbered experiment directories were consolidated into one workflow.

- Configuration: [configs/lab_n78.yaml](../configs/lab_n78.yaml)
- Step-by-step RFsim and USRP commands: [EXPERIMENT_COMMANDS.txt](../docs/EXPERIMENT_COMMANDS.txt)
- Scenario design and comparisons: [EXPERIMENTS.md](../docs/EXPERIMENTS.md)
- Retained historical evidence: [evidence/README.md](../evidence/README.md)

Use --cells du0 for the current hardware roles, and --set mode=rfsim --cells du0,du1 for a
software handover rehearsal. Do not use test-fixture YAML as a recommended physical setup.
