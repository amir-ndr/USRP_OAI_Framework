# Extending the framework

## Responsibilities

| Module | Responsibility |
|---|---|
| `config.py`, `profiles.py` | Schema, PHY registry, authorization, gain/transport and RF-budget validation |
| `oai.py`, `upstream.json` | Pinned source/templates, render, commands and integrity manifest |
| `runtime.py` | Node supervision, logged interlock, actual hardware-route checks and tool adapters |
| `session.py` | F1 lifecycle, initial serving cell, traffic, requests, cleanup and run findings |
| `oailog.py` | Source-specific console/statistics parsing and timestamp basis |
| `dashboard.py/html` | Loopback live display and saved-run replay |
| `traffic.py`, `analysis.py`, `artifacts.py` | Probe path/data, explicit event metrics, comparison, CSV/PNG export |
| `calibration.py`, `adapters/` | Optional receiver/commissioning tools and attenuator integration |
| `cli.py` | User command syntax and validated per-command overrides |

The maintained main path is F1. N2 rendering/trigger utilities remain advanced building blocks;
implement and validate a separate orchestration/identity mapping before enabling N2 in session.
Phone/modem setup is external provisioning and requires separately collected traffic evidence.

## Adding a profile

Add a pinned upstream serving-cell configuration and its hashes, then a registry entry with band,
duplex, PRBs, SCS/numerology, nominal channel bandwidth, sample rate, SSB/Point A and band limits.
Verify BWP/CORESET/search spaces, PRACH, TDD pattern and NR raster together. Preserve the distinction
between nominal channel bandwidth and occupied PRB grid. Do not retune an n78 template to an unrelated
band by changing only -C. Qualify the UE, hardware filtering, authorization and transport.

## Adding measurements

Define source, units, sampling interval, timestamp domain and missing-data semantics. Keep raw evidence
and detect parser failures. Add actual device-format fixtures and error-path tests. ADC RSSI dBFS is
not RSRP dBm; a protocol acknowledgement is not transfer completion. Add exports in artifacts.py and
explain definitions in RESULTS.md. Missing observations must never silently become zero or PASS.

## Adding trigger/channel behavior

Begin with a read-only observer. Before actuation, add target viability, current serving-cell lookup,
transaction/outcome timeout, minimum dwell and recovery behavior. Keep manual requests separate from
A3 policy. F1 at this pin round-robins DUs; N2 local UE identities may differ.

The RFsim channel block is shared across links; independent source/target models require explicit
schema/generator changes and validation of role/connection mapping. Hardware attenuator traces do not
modify RFsim samples. Readback/timing/disconnect tests are required for a new attenuator controller.
An SDR channel emulator is separate work (CHANNEL_EMULATOR.md), not a new YAML option.

## Updating OAI/UHD

Select and review an exact revision; update hashes and adapter behavior together. Rebuild OAI,
verify driver linkage/accepted rates/gain and port semantics, run software checks, RFsim, independent
hardware attachment and manual handover. Then qualify advanced policies. Do not disable pin/hash
checks or flash another radio as a shortcut. Keep source/FPGA/core versions with each run.

Routine low-impact docs changes need link/command checks. Safety, lifecycle, routing and metric changes
need meaningful regression tests. Avoid tests that merely restate an implementation constant.
