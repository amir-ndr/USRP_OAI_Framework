# Future evaluations and equipment options

Manufacturer information checked 2026-11-09. These are candidates, not purchases or certified
interoperability results. Confirm exact connector, regional SKU, firmware, bandwidth and lab requirements.

## Practical sequence

1. Finish A+C single-cell commissioning: n78-compatible antennas, qualified output/coupling and enough
   host transport reliability. Separate A/C links are now configured, but the 2026-11-11 concurrent
   full-rate RX check failed with packet errors/timeouts. Diagnose USB/driver/VM performance before
   RF operation. Keep the lowest working gain and establish a no-handover baseline.
2. Qualify B alone with the same UE. Add its RX2 antenna and Ethernet capacity; then manual F1 with A+B+C.
3. Introduce a protected programmable attenuator network for repeatable source/target level crossovers.
4. Qualify measurement reporting with the neighbor off, on, and at controlled relative levels. Fix the
   pinned OAI A3 unit/timer defects before interpreting offset/hysteresis/TTT comparisons.
5. Add a modem/phone UE and qualify its provisioning, measurements, maximum power and traffic path.
6. Qualify N2 separately if needed. The session runner deliberately refuses N2; the lower-level renderer
   and trigger support alone do not establish a working inter-gNB experiment.

## Antennas

Five antennas are currently reported; A and C each have both RF ports fitted. With the current
separate-port routing, full three-radio handover needs six suitable antennas in total. If the existing
units are all VERT2450, adding only one more VERT2450 does not resolve their unspecified n78 performance.
Choose characterized antennas for all active n78 ports, or measure the actual units appropriately.

| Candidate | Manufacturer information | Lab consideration |
|---|---|---|
| Taoglas TG.55.8113 | Cellular/5G terminal antenna with SMA male, including sub-6 coverage | Check its actual n78 gain/VSWR and mounting/ground-plane conditions; a monopole is affected by installation |
| TE/Linx ANT-5GW-SPS1 series | SMA-cabled cellular antenna; manufacturer data include 3300–4200 MHz | Cable can separate RX2 from TX; include feed loss, mounting and actual gain in the budget |

Sources: [Taoglas product](https://www.taoglas.com/product/tg-55-8113-5g-4g-terminal-mount-monopole-antenna/),
[TE manufacturer antenna catalog](https://www.te.com/content/dam/te-com/documents/consumer-devices/global/te-connectivity-antenna.pdf).
These are alternatives with published relevant-band data, not guarantees of this installation.
Use standard SMA matching the radio, not an RP-SMA Wi-Fi substitute.

## Fixed protection, splitters and controlled loss

You own **two VAT-30A+ 30 dB pads**. A 60 dB one-way connection uses both; the separate-port,
bidirectional single-cell layout in RF_SAFETY.md needs four pads. A two-cell protected network needs
additional protection and reviewed splitting/combining. Do not assume any proposed part's isolation
makes two transmitters safe to combine. Keep fixed protection even if a programmable attenuator fails to 0 dB.

| Candidate | Verified feature | Appropriate use / limitation |
|---|---|---|
| Mini-Circuits VAT-30A+ | 30 dB, 50 ohm, DC–6 GHz | Additional fixed pads matching the existing parts; use current power/temperature specifications |
| RCDAT-6000-60 | One channel, 1 MHz–6 GHz, 60 dB range, 0.25 dB steps | A single controlled path; two independent reciprocal branches need two devices or a suitable multi-channel unit |
| RC4DAT-6G-95 | Four independent channels, 1 MHz–6 GHz, 95 dB range, USB/Ethernet | Candidate for separate A/C and B/C UL/DL paths; adapter protocol must match the actual model/controller revision |
| ZFRSC-42-S+ resistive divider | DC–4.2 GHz | Illustrates an n78-capable splitter, but only about 6 dB port isolation and about 6 dB split loss; do not treat it as an isolating hybrid or a turnkey safe network |

Sources: [VAT-30A+](https://www.minicircuits.com/WebStore/dashboard.html?model=VAT-30A%2B),
[RCDAT-6000-60](https://minicircuits.com/WebStore/dashboard.html?model=RCDAT-6000-60),
[RC4DAT-6G-95](https://www.minicircuits.com/WebStore/dashboard.html?model=RC4DAT-6G-95),
[ZFRSC-42 datasheet](https://www.minicircuits.com/pdfs/ZFRSC-42.pdf).
The existing USB adapter is not physically qualified with these models. Test identification,
readback, timing and disconnect behavior before using it in a run.

Scalar attenuation supports slow ramps, steps and reverse crossings. It does not reproduce frequency-selective
multipath, delay spread or Doppler. A free-space antenna movement experiment is a separate channel condition.

## Modem and phone options

| Option | Why consider it | Preparation |
|---|---|---|
| Quectel RM520N-GL with a suitable USB/M.2 carrier | Vendor specifies 5G SA and n78; accessible RF connectors and diagnostics make controlled studies practical | Carrier with adequate power/cooling and SIM slot, correct MHF4 pigtails, antenna-port mapping, programmable SIM; confirm firmware AT measurements |
| Quectel RM520N-GL router/carrier from Waveshare | Integrated host interface can simplify connection | Confirm modem SKU/firmware, SIM access, SA configuration and diagnostic access; router NAT changes the measurement path |
| Existing iPhone 16 Pro | Avoids acquiring a separate modem; Apple supports private SA on relevant models | Region-specific n78/SIM support, test SIM/eSIM, suitable private PLMN, SUCI and non-null ciphering; provisioning work is substantial |
| Samsung Galaxy S23 Ultra | OAI's handover tutorial reports testing this phone family | Exact regional band/SA firmware and test-SIM behavior must be checked; model name alone does not prove your variant works |
| sysmocom sysmoISIM-SJA5 plus PC/SC reader | Programmable test SIM for modem/phone trials | Program lab IMSI/K/OPc and required 5G files outside the repository; cannot clone a commercial carrier SIM |

Sources: [Quectel RM520N](https://www.quectel.com/product/5g-rm520n-series/),
[Waveshare carrier](https://www.waveshare.com/product/rm520n-gl-5g-router.htm),
[Apple private-network requirements](https://support.apple.com/guide/deployment/support-for-private-5g-and-lte-networks-depac6747317/web),
[OAI handover tutorial](https://github.com/OPENAIRINTERFACE/openairinterface5g/blob/develop/doc/handover-tutorial.md),
[sysmocom SJA5](https://sysmocom.de/products/sim/sysmoisim-sja5/index.html).

A modem may support higher-power classes on n78 depending on SKU/firmware. Use the exact maximum
power and measured behavior in receiver protection; do not automatically assume 23 dBm or trust p-Max
as physical protection. Changing the UE also changes the experiment: report it as a separate condition.

The first modem target is independent single-cell SA attachment, diagnostics and a verified traffic
path. Then manual F1, then measurement-driven handover. A phone does not automatically repair network-side
A3 defects. The framework starts no nrUE/probe automatically for a physical phone; attach and traffic
collection require the separately prepared external-UE procedure in UE_SETUP.md.
