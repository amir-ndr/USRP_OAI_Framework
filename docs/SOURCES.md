# Sources and evidence

Sources checked 2026-11-09. Online pages can change. Implementation claims use the inspected local
OAI revision `1143f7500e5e5a9cd258148f8429230cc2759554`; templates are pinned in upstream.json.
Manufacturer capability does not prove this lab's interoperability, calibration or authorization.

| Source | Use |
|---|---|
| [OAI handover tutorial](https://github.com/OPENAIRINTERFACE/openairinterface5g/blob/develop/doc/handover-tutorial.md) | F1/N2 concepts, DU0->UE->DU1 order, control and reported COTS tests; nrUE measurement wording is stale at our pin |
| Local pinned `doc/FEATURE_SET.md:512`, RRC/PHY sources | Actual nrUE RSRP support and the A3 findings in PROJECT_REVIEW.md |
| Local pinned `radio/USRP/usrp_lib.cpp`, `executables/nr-ru.c` | Port/gain defaults, calibration offsets, accepted rates and timing initialization |
| Local pinned `common/utils/telnetsrv/telnetsrv_ci.c`, RRC mobility sources | Trigger grammar, serving-DU lookup, round-robin F1 and protocol outcome events |
| [OAI USRP guide](https://github.com/OPENAIRINTERFACE/openairinterface5g/blob/develop/radio/USRP/README.md) | Hardware driver/configuration context |
| [OAI RFsim guide](https://github.com/OPENAIRINTERFACE/openairinterface5g/blob/develop/radio/rfsimulator/README.md) | Software RF transport and timing limits |
| [TS 38.300 overview](https://www.etsi.org/deliver/etsi_ts/138300_138399/138300/18.04.00_60/ts_138300v180400p.pdf) | NR architecture and handover concepts |
| [TS 38.104 radio specification](https://www.etsi.org/deliver/etsi_ts/138100_138199/138104/18.13.00_60/ts_138104v181300p.pdf) | Bands, raster, channel bandwidth versus resource-grid configuration |
| [TS 38.331 RRC](https://www.etsi.org/deliver/etsi_ts/138300_138399/138331/18.08.00_60/ts_138331v180800p.pdf) | Measurement-event units and TTT fields |
| [Ettus UBX](https://kb.ettus.com/UBX) | RF frontends, tuning/gain ranges, antenna routing and input ceiling |
| [UHD X3x0](https://files.ettus.com/manual/page_usrp_x3x0.html), [synchronization](https://files.ettus.com/manual/page_sync.html) | Host interfaces, reference/PPS and timing |
| [OctoClock](https://kb.ettus.com/OctoClock), [RFNoC](https://files.ettus.com/manual/page_rfnoc.html) | Reference distribution and possible FPGA processing; not a turnkey channel emulator |
| [VERT2450](https://www.ettus.com/all-products/vert2450/) | Specified antenna bands, excluding n78 characterization |
| [ISED RSS-247](https://ised-isde.canada.ca/site/spectrum-management-telecommunications/en/devices-and-equipment/radio-equipment-standards/radio-standards-specifications-rss/rss-247-digital-transmission-systems-dtss-frequency-hopping-systems-fhss-and-licence-exempt-local) | Licence-exempt equipment requirements; not automatic permission for arbitrary SDR transmissions |
| [ISED developmental service](https://ised-isde.canada.ca/site/spectrum-management-telecommunications/en/licences-and-certificates/radio-authorizations/developmental-service) | Canadian experimental authorization context |
| [Apple private networks](https://support.apple.com/guide/deployment/support-for-private-5g-and-lte-networks-depac6747317/web) | Physical UE provisioning/identifiers/security; exact model/region still matters |
| [Quectel RM520N](https://www.quectel.com/product/5g-rm520n-series/), [sysmocom SJA5](https://sysmocom.de/products/sim/sysmoisim-sja5/index.html) | Candidate SA/n78 modem and programmable lab SIM |

Specific antenna/attenuator/carrier options have first-party links in FUTURE_EVALUATIONS.md.
Current operator statements are recorded as such in HARDWARE_SETUP.md; they are not manufacturer
measurements. Historical raw evidence and its original-to-current locations are indexed in evidence/.
