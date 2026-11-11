# RF protection and calibration

The software checks supplied facts. It cannot measure an incorrectly connected RF cable,
antenna coupling, absolute transmitted power or an undocumented startup transient.
Use [HARDWARE_SETUP.md](HARDWARE_SETUP.md) and the lab's RF commissioning procedure.

## Recorded authorization and low-gain starting point

The operator reports authorization for 3300–3800 MHz with maximum EIRP 20 dBm. This is recorded
in `configs/lab_n78.yaml`; the actual licence document/reference and location conditions must be
checked before RF. The current 3604.8 MHz nominal 10 MHz channel spans 3599.8–3609.8 MHz.
The code checks the whole nominal channel. A protected room does not establish zero leakage or
protect a nearby USRP input. RFsim does not need RF authorization because it transmits no RF.

The cells request UHD TX gain 0.5 dB; C requests 0 dB. The global gain ceiling is 0.5 dB.
These are minimum gain requests under the current integer cell-attenuation interface, not measured
output power or a guaranteed minimum EIRP. An EIRP limit is a ceiling and does not command power.
Broadcast p-Max is not an independent hardware output limiter; the pinned nrUE has reported PCMAX
above the configured p-Max. The runtime interlock observes OAI logs after launch and reacts to
mismatches. It is not a physical limiter and cannot certify startup/restart peaks.

## Receiver protection

Ettus specifies UBX maximum input **−15 dBm**. This is a damage ceiling, not an operating target.
The budget uses at least 6 dB margin; this lab starts with 10 dB, giving a −25 dBm guarded limit.
Compression, ADC clipping and damage are different effects. Lowering RX gain can relieve clipping
but cannot protect the RF input against an excessive connector signal. [Ettus UBX](https://kb.ettus.com/UBX).

Every active receiver needs a bound for all active transmitters, including its own TX->RX2
coupling. The model conservatively adds possible peak amplitudes. Variable attenuation is never
credited for protection: assume its minimum/failure loss. Include power and loss uncertainty.

Distance/Friis and the gain-derived `auto` peak are **planning estimates only**. They cannot open
the live OTA gate. No universal 0.5 m safe-spacing or −34 dBm guarantee is claimed. Reflections,
nearby objects, antenna mismatch/polarization, sidelobes and hardware transients matter.
VERT2450 gain at 3.6 GHz is unspecified; its specified-band 3 dBi value is not an n78 calibration.

## How to commission the RF setup

1. Keep OAI RF stopped. Document every active port, RF accessory, antenna and intended signal path.
2. Use appropriately rated fixed attenuation/terminations and calibrated lab instrumentation for the
   first source characterization. Verify instrument input limits, frequency range, detector/bandwidth
   settings and the full attenuated path before enabling a source. Do not connect a raw TX output to RX.
3. Establish a connector **peak envelope**, including waveform PAPR and startup/restart behavior,
   for each transmitter over the intended frequency/gain/profile range. A CW output figure, an average
   power reading, a gain command or uncalibrated receive dBFS alone is not this envelope.
4. Establish a conservative minimum path loss for every exposed receiver, including own-port coupling,
   all relevant antenna positions and possible bypass paths. Include uncertainty and full transmit state.
5. Replace the relevant `ota` planning mapping with `minimum_passive_loss_db` and a traceable evidence
   reference. Remove `ota` rather than supplying both. Do not reword an assumption as a measurement.
6. Fill `safety.tx_power_bounds` for active sources with `peak_dbm`, `maximum_gain_db`, `profile`,
   `frequency_range_mhz`, `antenna_gain_dbi` and `evidence`. The frequency interval must cover the full nominal channel.
   These qualified peaks override gain-derived planning values for budget/EIRP calculations.
7. Verify clock/time, actual gains/ports, antenna suitability, authorization and Ethernet. Set only
   the checks actually completed. Keep `single_cell_attach_verified` false until both cells pass alone.
8. Run `oaiho budget`, `oaiho validate --live`, `oaiho doctor`, and then the single-cell session.

Calibration is specific to a qualified envelope. Changing carrier, profile, gain cap, antenna,
RF path or accessory can invalidate it. The code checks profile/frequency/gain coverage; a person
must check the remaining physical conditions recorded in the evidence.

The `tone-cal` command is a limited commissioning aid: fixed gain 0 dB, at most −20 dBFS CW,
at most 3 seconds. `--execute --dry-run` receives/reads back settings without creating TX samples.
An executed non-dry run transmits and requires a protected conducted setup first. Its nominal
connector estimate and relative self-isolation calculation are not an absolute power calibration;
receiver-chain differences, assumed cable loss and measurement sensitivity need accounting.

## Optional conducted arrangement

You own two VAT-30A+ pads, nominal 30 dB each. Two in series provide nominal 60 dB along ONE path.
They do not provide two protected directions simultaneously. For a single-cell bidirectional
separate-port bench, a conservative full-output layout uses four pads (two more than owned):

```text
DL: A TX/RX --[30 dB]--[30 dB]-- C RX2
UL: C TX/RX --[30 dB]--[30 dB]-- A RX2
```

This protects the external desired paths under their verified minimum losses; internal/cross-port
coupling still needs qualification. Two-cell wiring needs a reviewed combiner/splitter network,
additional fixed protection and all cross-path budgets. A splitter is not an isolator. Resistive
parts can have much lower port isolation than hybrid dividers. Never join transmitters by T piece.
Use actual minimum losses, power/temperature ratings and connector limits at the operating frequency.

Potential future components and their first-party references are in FUTURE_EVALUATIONS.md.
