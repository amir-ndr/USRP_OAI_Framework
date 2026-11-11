# Concepts and terminology

This reference describes the implemented laboratory, not every optional feature in 5G.
The reproducible operating procedure is [EXPERIMENT_COMMANDS.txt](EXPERIMENT_COMMANDS.txt).

## What runs where

OAI (OpenAirInterface) supplies the real PHY, MAC, RLC, PDCP, RRC and core-facing protocols.
The Python framework supplies configuration, source/configuration integrity checks, process
supervision, a live display, traffic collection and analysis. Python does not process NR slots.

A gNB is a 5G base station. In this F1 deployment, one Central Unit (CU) controls two Distributed
Units (DUs), each connected to an RF front end. A cell is a logical radio coverage resource;
a USRP is the equipment carrying its samples. One X310 is a DU radio, another is the optional
second DU radio, and C is driven by OAI nrUE. The UE is a terminal that both receives and transmits.
It must first hear synchronization/system information, receive scheduling and network commands,
and then transmit its access requests, data and responses.

A single-cell run uses one CU, one DU and one UE. B is inactive. A two-cell F1 run adds the
second DU while keeping the same CU and UE. Adding a receive-only instrument is a different role.

## NR bands, n78, n66 and N6

An NR band name identifies a standardized radio-frequency arrangement. **5G means a cellular
generation, not 5 GHz.** Antenna bandwidth, USRP tuning range, NR band support and authorization
are separate requirements that all need to overlap.

| Term | Meaning in this project |
|---|---|
| n78 | NR TDD band, 3300–3800 MHz. Current profile: 3604.8 MHz, 10 MHz channel, 30 kHz spacing, 24 PRBs |
| n66 | NR FDD band: gNB DL 2110–2200 MHz and UE UL 1710–1780 MHz. Historical profile: DL 2152.68 / UL 1752.68 MHz, 5 MHz, 15 kHz, 25 PRBs |
| N6 | Core user-plane interface between UPF and data network. It is not an NR band |
| n46 | A 5 GHz NR band relevant to NR-U research; not an implemented/qualified operator profile here |

Your reported authorization covers n78, not the historical n66 frequencies. Keep n66 RF
experiments disabled unless separately authorized/protected under an applicable arrangement.
The 2152.68 MHz site measurement also found an existing carrier. RFsim can model n66 without RF.

TDD shares one RF frequency over different downlink/uplink time intervals. Both cells need
compatible slot patterns and aligned frame timing. FDD uses separate UL/DL frequencies.
TDD does not automatically make an arbitrary one-antenna OAI/UBX configuration work.

A physical resource block (PRB) contains 12 subcarriers. Subcarrier spacing (SCS) is the frequency
separation between adjacent OFDM subcarriers. Numerology mu=0 means 15 kHz and mu=1 means 30 kHz.
The occupied resource-grid width is PRBs × 12 × SCS: 24 × 12 × 30 kHz = 8.64 MHz.
The nominal channel is 10 MHz, including guard regions. Licensing/channel containment checks
use the nominal channel, not just that grid. Sample rate is a third quantity: 15.36 MS/s here.

ARFCN encodes an RF frequency. Point A anchors the resource grid. SSB contains synchronization
signals and PBCH; PSS/SSS help the UE find cell timing and identity. PCI is the physical cell ID;
N_ID2 from PSS alone is only PCI modulo 3. A PSS peak is not complete cell identification or
proof of attachment. The synchronization raster constrains SSB locations. The framework shifts
its template in 1.44 MHz steps in n78, moving Point A and SSB together; arbitrary center values
such as 3600.0 MHz are not reachable from this particular template without more PHY work.

## Radio hardware and ports

The X310 is the SDR motherboard/FPGA platform. UBX-160 is its RF daughterboard, covering 10 MHz
through 6 GHz, with one transmit and one receive frontend per installed board. Slot B is selected
by `B:0`; it is not the same thing as selecting antenna port TX/RX or RX2.

UHD is the driver/API between OAI and the USRP. Its host library must match the FPGA protocol;
the saved third-radio update/backup is retained in `evidence/hardware`. Do not reflash for routine runs.

The inspected OAI driver does not select a different UBX receive antenna. UHD defaults to RX2.
Thus the current configuration needs an antenna on TX/RX for TX and RX2 for RX on every active
radio. TX/RX can receive when explicitly selected in suitable operation; UBX documentation
specifies RX2 reception during full-duplex use. Shared-antenna TDD would require deliberate
configuration/driver changes and verification, not moving a cable while OAI runs.

The OctoClock distributes a common 10 MHz frequency reference and PPS timing edges. Frequency
lock, PPS observation, common epoch, frame timing and RF phase coherence are different checks.
The DUs use external reference/PPS; nrUE uses external 10 MHz and derives radio timing from the cell.
Host time synchronization matters for OAI's external-PPS initialization and comparable timestamps.

## Ethernet and samples

OAI/UHD carry complex I/Q samples: an in-phase and a quadrature value for each sample time.
The pinned driver uses sc16 on the wire: 16-bit I plus 16-bit Q, four bytes per complex sample.
At 15.36 MS/s, one direction is 491.52 Mbit/s payload, approximately 521 Mbit/s with the project's
framing allowance. Continuous receive streaming means an idle UE or TDD pattern does not justify
ignoring the receive transport budget. Separate radio cables still share a switch-to-host uplink.

A configured `usrp_link` is the actual host interface carrying a radio's sample traffic.
An address/route determines which interface Linux uses; the code checks the real route and link
speed before hardware launch. A VLAN or a second YAML link name does not increase physical capacity.
Underflow, overflow, late, sequence and drop counters indicate transport/scheduling problems;
they need investigation and are not interchangeable with radio packet loss or handover failure.

## Power and measurement units

A decibel (dB) is a ratio. dBm is power relative to 1 mW; 0 dBm is 1 mW and 20 dBm is 100 mW.
dBi is antenna gain relative to an isotropic radiator. EIRP combines conducted power, antenna
gain and feed losses. A maximum permitted EIRP is a ceiling, not a requested operating level.
dBFS is relative to digital converter full scale; converting it to input dBm needs calibration.

For the probed UBX, OAI TX gain = 31.5 − attenuation. Cell `att_tx_db: 31` requests 0.5 dB UHD
gain; nrUE `tx_atten_db: 31.5` requests 0 dB. OAI's default UE attenuation 0 would request maximum
gain, so the framework passes it explicitly. These numbers are not transmitted dBm. RX settings
also include OAI calibration offsets: the n78 example requests 105 − 77 = 28 dB UHD RX gain.
The startup log interlock is reactive; it is not a physical limiter or absolute RF power sensor.

RSRP is reference-signal received power; RSRQ is a reference-signal quality ratio; SINR describes
signal versus interference/noise. They are not derived simply by subtracting RX gain. The pinned
nrUE supports RSRP reports, with observed quality/timing defects; it does not provide RSRQ/SINR
in those RRC reports. DU UL SNR and RSSI dBFS are separate observations. Preserve source and units.

## Registration, traffic and identities

PLMN is MCC plus MNC; the lab starts with 001/01 to match its existing core and software SIM.
A TAC identifies a tracking area. IMSI/SUPI identifies a subscriber. Authentication keys and
OPc must match the core; real secrets belong outside this repository. The software UE uses its
private UICC file; a phone/modem needs a suitably provisioned physical SIM/eSIM.

RRC connection, NAS registration and PDU session establishment are different milestones.
A PDU session gives user-plane connectivity to a data network/DNN. The UE's `oaitun_ue1` interface
carries its IP traffic. The probe binds to that tunnel and targets the core data endpoint.
Pinging a UE address that is local to the same Linux namespace can bypass RF entirely.
A route record supports the path choice; packet/protocol evidence and negative controls strengthen it.

PCI, NR cell identity, DU ID, gNB ID, RRC UE ID and RNTI identify different objects/scopes.
A UE can change RNTI during handover; for N2, local UE IDs can also differ between gNBs.

## Handover and experiment policies

F1 handover moves a connected UE between DUs under one CU. N2 handover involves separate gNBs
and core signaling. Manual and A3 describe the trigger policy, not that architecture.
An acknowledgement accepts a request; successful target access and the matching completion
are later events. RLF/re-establishment or fresh registration is not a completed handover.

A manual request asks the network to initiate the procedure. `--ho-every` schedules these requests.
A3 responds to filtered neighbor-versus-serving measurements with offsets, hysteresis and TTT.
Hysteresis is an entry/exit margin. TTT tests how long an entry condition persists; it is not
handover signaling duration. The framework encodes half-dB fields and TTT enumeration values,
but the pinned OAI implementation has known scaling/timer issues (PROJECT_REVIEW.md).
Changing those parameters in a manual/no-report run does not evaluate A3.

RFsim exchanges software samples. A scalar attenuator changes path level; it does not implement
multipath delays or Doppler. A real channel emulator processes waveforms along controlled paths.
A third X310 does not become one automatically. See CHANNEL_EMULATOR.md.

## Interpreting results

Signaling duration uses explicitly paired protocol boundaries and a consistent timestamp basis.
Probe RTT includes radio, core, queueing and endpoint work. Loss is missing replies, not a directional
RF loss measurement. A reply gap includes the ordinary send interval and scheduling. A 20 ms probe
cannot resolve exact 1 ms radio interruption. Missing/censored windows stay missing rather than zero.

Independent runs are the comparison units; thousands of correlated packets are not thousands of
independent handover trials. Preserve failures/unknowns, raw logs, condition settings and provenance.

Primary references: [3GPP NR overview](https://www.etsi.org/deliver/etsi_ts/138300_138399/138300/18.04.00_60/ts_138300v180400p.pdf),
[NR radio specification](https://www.etsi.org/deliver/etsi_ts/138100_138199/138104/18.13.00_60/ts_138104v181300p.pdf),
[OAI handover tutorial](https://github.com/OPENAIRINTERFACE/openairinterface5g/blob/develop/doc/handover-tutorial.md),
[Ettus UBX](https://kb.ettus.com/UBX), [UHD X3x0](https://files.ettus.com/manual/page_usrp_x3x0.html).
For the tutorial's stale nrUE claims, use the inspected pinned feature list/source and PROJECT_REVIEW.md.
