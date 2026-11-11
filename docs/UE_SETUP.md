# UE setup: current USRP and future physical terminals

## Current selection: OAI nrUE on C

`configs/lab_n78.yaml` uses `ue.kind: oai_nrue`, X310 C at 192.168.10.5, serial 34AF675,
UBX-160 v2 in slot B. The software subscriber file remains `~/nrue.uicc.conf`; do not copy secrets
into this repository. It must match the existing core's PLMN 001/01, subscriber and DNN `oai`.

The framework passes TX attenuation explicitly: 31.5 means UHD gain 0 dB on this board.
The n78 RX setting 105 means 28 dB UHD after OAI's 77 dB offset. C receives on RX2 and transmits
on TX/RX. Both ports are fitted per the operator. External 10 MHz provides frequency reference;
serving-cell synchronization provides the UE's frame timing. A separate C PPS verification is not
required by this particular command configuration.

As of 2026-11-11, C uses dedicated adapter enx9c69d381261e, host 192.168.10.11/32, with an
on-link /32 route to 192.168.10.5. Both radios answered pings after restoring the original cell adapter
on 2026-11-11. Concurrent full-rate RX then failed; resolve this before OAI RF. See HARDWARE_SETUP.md.

nrUE implements periodic/A2/A3 RSRP reports at the pin, despite stale tutorial wording. Previous
RFsim evidence showed excessive periodic reports and a nonexistent neighbor. RSRQ/SINR are not
included in those RRC reports. Keep `periodic_reports: false` for the manual baseline. Before
using reports as measurements, turn B off/on and vary controlled link level; verify identity,
report interval and monotonic signal response. Resolve A3 scaling/timer findings in PROJECT_REVIEW.md.

Traffic originates through `oaitun_ue1` toward the core data endpoint. The session verifies the
new interface address and binds ping to that tunnel. A root-namespace ping to its own UE address
can stay local. More complex bidirectional/core-originated traffic may need explicit network namespaces;
namespace isolation is not implemented automatically by this session runner.

## Future modem

Recommended first candidate: a supported n78 SA Quectel RM520N-GL variant and carrier with SIM slot,
power/cooling, USB/Ethernet and accessible RF connectors. Obtain the exact firmware's hardware and AT
manuals. Program a test SIM to match the lab core. Configure SA, n78 and DNN `oai` using that firmware's
supported commands. Confirm its actual RF port mapping, power class, measurements and supported bandwidth.
Do not reuse a generic RF-port/power diagram for an unknown modem revision.

Start with a copy of `configs/lab_n78.yaml`; set `ue.kind: cots_modem`, then review its RF protection
and subscriber setup. This changes the radio role and power budget; it is not merely a UI label.
The session starts CU/DUs but no software UE or automatic probe for this external terminal.
Attach during a sufficiently long run, obtain its session address, and separately collect traffic.
Verify that routing crosses the UPF and terminal, not host-local addressing or a management interface.
ECM/NAT, QMI/MBIM and an external host produce different paths; document the chosen arrangement.

When the route and ICMP response are verified, a separately managed probe can be collected with:

```bash
oaiho probe --method ping --host UE_SESSION_IP --duration 120 --out RUN_DIRECTORY/external_packets.jsonl
oaiho events RUN_DIRECTORY/config.yaml --run RUN_DIRECTORY --out RUN_DIRECTORY/external_events.csv
oaiho analyze --packets RUN_DIRECTORY/external_packets.jsonl --events RUN_DIRECTORY/external_events.csv --config RUN_DIRECTORY/config.yaml --out RUN_DIRECTORY/external_report --plots
```

Replace placeholders with actual reviewed values; these are future procedure templates, not a proven
modem attachment. This version does not automatically combine that external report into session status.
A route record alone does not prove end-to-end RF passage. Independent UL/DL traffic adds useful evidence.

## Future phone

Use a lab-programmable SIM/eSIM with keys in your core. A commercial Rogers SIM will not authenticate
against a core that lacks its operator keys. For an iPhone, Apple's private-network requirements include
supported private PLMN identifiers (for example MCC 999), SUCI protection and appropriate security.
Change core, SIM and RAN coherently; setting only `ue.kind: iphone` cannot provision them.

For an iPhone copy of the config, review PLMN 999/70, non-null ciphering such as NEA2 first,
RRC/NAS integrity, SIM SUCI settings and matching core de-concealment support. Confirm the exact core
version's configuration syntax before editing it; this update does not modify the external core or SIM.
Check the particular phone region/SIM format and SA/n78 support. An eSIM-only unit needs a usable lab eSIM
provisioning route. Match your permission, handset model and available bandwidth.

First prove single-cell registration, PDU session and traffic. Test both cells independently and then
manual F1. Qualify the phone's reports before A3, and fix the relevant OAI network-side behavior too.
Use the external probe/report process above, or a phone-originated traffic tool and a documented capture.

Primary product/provisioning references and alternatives are in FUTURE_EVALUATIONS.md.
