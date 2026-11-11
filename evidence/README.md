# Retained evidence and cleanup

Cleanup date: 2026-11-09. `cleanup_manifest.json` records retained/moved/deleted paths.
A temporary rollback archive is `/tmp/usrp_framework_before_cleanup_20261109.tar.gz`; it is not a
long-term backup and may disappear with temporary-file cleanup. Existing unreadable telnet history
files were excluded; command journals and CU logs were retained in selected raw archives.

## Hardware

`hardware/` retains the third X310's pre-update FPGA image, image-loader and post-cycle probe logs,
setup/clock inventory, RX spectrum observations, OAI USRP build log and concurrent RX qualification.
**The .bit backup is recovery evidence, not disposable logging.** The obsolete proposed-shared-link
YAML is removed; its failed topology is described by the hardware qualification record instead.

Original paths were under runs/. Saved records can contain those historical absolute paths.
They are provenance, not current operating instructions. Use configs/lab_n78.yaml for new runs.

## Historical RFsim raw archives

| Archive | Why retained |
|---|---|
| `rfsim/n78_single_20261109.tar.gz` | Previous exact-profile attachment/traffic rehearsal |
| `rfsim/n78_handover_20261109.tar.gz` | Previous four-completion n78 handover evidence |
| `rfsim/n66_measurement_caveat_20261109.tar.gz` | Historical n66 run and measurement-report defects |

Archive hashes are in the manifest. Extract to a new temporary directory for inspection; do not
extract over the current workspace or run archived commands as current configurations.

Removed: duplicate e2e/re-analysis trees, old top-level run console copies, generated synthetic
report folders, stale test logs, redundant numbered experiment scripts and operator config variants.
Legacy YAML needed for software coverage remains under tests/fixtures with an explicit warning.
New verification runs remain under runs/ in the new artifact format. No raw new run was discarded
because its outcome was unfavorable.
