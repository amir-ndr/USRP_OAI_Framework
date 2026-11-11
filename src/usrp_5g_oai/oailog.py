"""Parsers for OAI console output and nrRRC_stats.log at the pinned revision.

Every pattern quotes a message format found in the pinned source (file noted beside it). A match is
evidence that OAI printed the message - it is never upgraded into a claim the message does not make.
"""
import re
from datetime import datetime, timezone
from .runtime import HW_PATTERNS

ANSI = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')
WALL_CLOCK = re.compile(r'^\s*(\d{9,11}\.\d{6})\s')            # --log_config.global_log_options wall_clock
UTC_TIME = re.compile(r'^\s*\[(\d\d):(\d\d):(\d\d)\.(\d{6})\]\s')  # utc_time (no date)
HEX = r'[0-9a-fA-F]+'

EVENTS = [
    # openair3/NGAP, openair2/RRC/NR (CU side)
    ('ngap_up', re.compile(r'Received NGSetupResponse from AMF')),
    ('f1_setup_request', re.compile(r'Received F1 Setup Request from gNB_DU (?P<du_id>\d+) \((?P<du_name>[^)]*)\)(?: on assoc_id (?P<assoc>\d+))?')),
    ('f1_setup_response', re.compile(r'DU (?P<du_id>\d+) \((?P<du_name>[^)]*)\): sending F1 Setup Response')),
    ('ue_context_created', re.compile(rf'\(cellID (?P<cellid>{HEX}), UE ID (?P<ue_id>\d+) RNTI (?P<rnti>{HEX})\) Create UE context')),
    ('rrc_connected', re.compile(rf'\(cellID (?P<cellid>{HEX}), UE ID (?P<ue_id>\d+) RNTI (?P<rnti>{HEX})\) Received RRCSetupComplete')),
    ('rrc_reconf_complete', re.compile(rf'\(cellID (?P<cellid>{HEX}), UE ID (?P<ue_id>\d+) RNTI (?P<rnti>{HEX})\) Received RRCReconfigurationComplete')),
    ('pdu_session_added', re.compile(r'Added PDU Session (?P<pdu>\d+)')),
    # rrc_gNB.c process_Event_Based_Measurement_Report
    ('a3_report', re.compile(r'HO LOG: Event A3 Report for UE (?P<ue_id>\d+)')),
    ('neighbour_rsrp', re.compile(r'HO LOG: Measurement Report for the neighbour (?P<pci>\d+) with RSRP: (?P<rsrp>-?\d+)')),
    # rrc_gNB_mobility.c (F1) and rrc_gNB.c (shared completion)
    ('ho_trigger', re.compile(rf'Handover triggered for UE (?P<ue_id>\d+)/RNTI (?P<rnti>{HEX}) towards cell (?P<cell>\d+)/assoc_id (?P<assoc>\d+)/PCI (?P<pci>\d+)')),
    ('ho_command', re.compile(rf'HO acknowledged: Send reconfiguration for UE (?P<ue_id>\d+)/RNTI (?P<rnti>{HEX})')),
    ('ho_complete', re.compile(rf'handover for UE (?P<ue_id>\d+)/RNTI (?P<rnti>{HEX}) complete!')),
    ('ho_failure', re.compile(rf'handover for UE (?P<ue_id>\d+)/RNTI (?P<rnti>{HEX}) failed, rollback to original cell')),
    ('ho_source_release', re.compile(r'UE (?P<ue_id>\d+) Handover: trigger release on cell PCI (?P<pci>\d+)')),
    ('ho_abort', re.compile(r'No target gNB-DU found\. Handover for UE (?P<ue_id>\d+) aborted'
                            r'|cannot (?:find UE context for UE ID|get source cell for UE|get target cell for UE) (?P<ue_id_b>\d+)')),
    # N2 (rrc_gNB_mobility.c, ngap_gNB*.c)
    ('n2_trigger', re.compile(r'UE (?P<ue_id>\d+): triggered N2 HO, source PCI=(?P<source_pci>\d+) to target PCI=(?P<pci>\d+)')),
    ('n2_request', re.compile(r'Received NG Handover Request from AMF')),
    ('n2_command', re.compile(r'Received NG Handover Command')),
    ('path_switch_ack', re.compile(r'Received NG Path Switch Request Acknowledge')),
    ('reestablishment', re.compile(r'Send RRCReestablishment|Processing NR_RRCReestablishmentComplete|RRCReestablishmentRequest')),
    ('ue_release', re.compile(r'Send UE Context Release for gnb_ue_id (?P<ue_id>\d+)|received UE Context Release Request for UE (?P<ue_id_b>\d+)')),
    # openair2/LAYER2/NR_MAC_gNB/main.c periodic per-UE block (DU side, every 128 frames)
    ('mac_frame', re.compile(r'Frame\.Slot (?P<frame>\d+)\.(?P<slot>\d+)')),
    ('mac_ue', re.compile(rf'UE RNTI (?P<rnti>{HEX}) CU-UE-ID (?P<ue_id>\d+|\(none\)) (?P<sync>in-sync|out-of-sync) PH (?P<ph>-?\d+) dB '
                          r'PCMAX (?P<pcmax>-?\d+) dBm(?:, average RSRP (?P<rsrp>-?\d+) \(\d+ meas\))?'
                          r'(?:, average SINR (?P<sinr>-?\d+\.\d) \(\d+ meas\))?')),
    ('mac_csi', re.compile(rf'UE (?P<rnti>{HEX}): CSI \[CQI (?P<cqi>\d+) RI (?P<ri>\d+)')),
    # RSSI values are dBFS at the gNB receiver (phy_procedures_nr_gNB.c: "in terms of dBFS range -128 to 0").
    ('mac_dl', re.compile(rf'UE (?P<rnti>{HEX}): dlsch_rounds (?P<rounds>[\d/]+), dlsch_errors (?P<errors>\d+), pucch0_DTX (?P<dtx>\d+)'
                          r'(?: \(SNR (?P<pucch_snr>-?\d+\.\d)[+-]\d+\.\d\) RSSI (?P<pucch_rssi>-?\d+\.\d))?'
                          r'.*?BLER (?P<bler>[\d.]+) MCS \((?P<table>\d+)\) (?P<mcs>\d+)')),
    ('mac_ul', re.compile(rf'UE (?P<rnti>{HEX}): ulsch_rounds (?P<rounds>[\d/]+), ulsch_errors (?P<errors>\d+), ulsch_DTX (?P<dtx>\d+), '
                          r'BLER (?P<bler>[\d.]+) MCS \((?P<table>\d+)\) (?P<mcs>\d+).*?SNR (?P<snr>-?\d+(?:\.\d+)?)'
                          r'(?:.*?RSSI (?P<rssi>-?\d+(?:\.\d+)?))?')),
    ('mac_goodput', re.compile(rf'UE (?P<rnti>{HEX}): LCID [\d,]+ goodput DL\s+(?P<dl>[\d.]+) UL\s+(?P<ul>[\d.]+) Mbps')),
    ('ul_failure', re.compile(rf'UE (?P<rnti>{HEX}) UL failure after transmission timeout')),
    # radio/USRP/usrp_lib.cpp
    ('ext_ref_locked', re.compile(r'USRP locked to external reference')),
    ('ext_ref_failed', re.compile(r'Failed to lock to external reference')),
    ('uhd_version', re.compile(r'UHD version (?P<v>\S+)')),
    # UHD logger lines ("[ERROR] [X300] ...") and OAI [HW] reports of stream errors.
    ('uhd_error', re.compile(r'\[(?:ERROR|WARNING)\] \[(?:UHD|X300|X3XX|RFNOC|MPMD|0/Radio)[^\]]*\]'
                             r'|\[HW\].*\b(?:[Uu]nderflow|[Oo]verflow|[Ll]ate|ERROR_CODE_\w+)')),
] + [('hw_' + k, rx) for k, rx in HW_PATTERNS.items()]

FAST_PATH = re.compile(r'^[UOLSD]+$')   # UHD single-character stream status: Underflow, Overflow, Late, Seq, Drop


def clean(line):
    return ANSI.sub('', line).rstrip()


def oai_epoch(line, fallback_epoch):
    """OAI's own timestamp (wall_clock) when present; utc_time is completed with the fallback's UTC date."""
    m = WALL_CLOCK.match(line)
    if m:
        return float(m[1]), 'oai_wall_clock'
    m = UTC_TIME.match(line)
    if m and fallback_epoch:
        day = datetime.fromtimestamp(fallback_epoch, timezone.utc).replace(hour=int(m[1]), minute=int(m[2]),
                                                                            second=int(m[3]), microsecond=int(m[4]))
        t = day.timestamp()
        if t - fallback_epoch > 43200:      # crossed midnight UTC between log and receipt
            t -= 86400
        return t, 'oai_utc_time'
    return fallback_epoch, 'python_receive'


def parse(line):
    """Return a list of (event, fields) found in one cleaned line (usually zero or one)."""
    text = clean(line)
    if FAST_PATH.match(text.strip()) and text.strip():
        return [('uhd_fast_path', {'chars': text.strip()})]
    found = []
    for name, rx in EVENTS:
        m = rx.search(text)
        if m:
            d = {k: v for k, v in m.groupdict().items() if v is not None}
            for alt in ('ue_id_b',):
                if alt in d:
                    d['ue_id'] = d.pop(alt)
            found.append((name, d))
            if not name.startswith('hw_'):
                break
    return found


# nrRRC_stats.log writer: rrc_gNB_du.c (DU list) and rrc_gNB.c write_rrc_stats (UE list)
RRC_DU = re.compile(r'^\[\d+\] DU ID (?P<du_id>\d+) \((?P<name>[^)]*)\) (?:assoc_id (?P<assoc>\d+)|integrated DU-CU)')
RRC_CELL = re.compile(r'nrCellID (?P<nci>\d+), PCI (?P<pci>\d+)')
RRC_UE = re.compile(rf'^UE \d+ CU UE ID (?P<ue_id>\d+) DU UE ID (?P<du_ue>-?\d+) RNTI (?P<rnti>{HEX})')
RRC_ACTIVITY = re.compile(r'last RRC activity: (?P<s>\d+) seconds ago')
RRC_PDU = re.compile(r'PDU session ID (?P<id>\d+) status (?P<status>\S+)')
RRC_ASSOC = re.compile(r'associated DU:\s+(?:DU assoc ID (?P<assoc>-?\d+)|(?P<other>.*))')
RRC_SERVING = re.compile(r'servingCellId \d+ MeasResultNR for phyCellId (?P<pci>\d+)')
RRC_NEIGH = re.compile(r'neighboring cell for phyCellId (?P<pci>\d+)')
RRC_SSB = re.compile(r'resultSSB:(?: RSRP (?P<rsrp>-?\d+) dBm)?(?:.*?RSRQ (?P<rsrq>-?[\d.]+) dB)?(?:.*?SINR (?P<sinr>-?[\d.]+) dB)?')


def parse_rrc_stats(text):
    """-> {'dus': [{du_id, name, assoc, cells:[{nci, pci}]}], 'ues': [{ue_id, rnti, assoc, pdu, meas:{pci:{...}}, serving_pci}]}"""
    dus, ues, du, ue, target = [], [], None, None, None
    for raw in text.splitlines():
        line = raw.strip()
        if m := RRC_DU.match(line):
            du = {'du_id': int(m['du_id']), 'name': m['name'], 'assoc': int(m['assoc']) if m['assoc'] else None, 'cells': []}
            dus.append(du)
            ue = None
        elif (m := RRC_CELL.search(line)) and du is not None and ue is None:
            du['cells'].append({'nci': int(m['nci']), 'pci': int(m['pci'])})
        elif m := RRC_UE.match(line):
            ue = {'ue_id': int(m['ue_id']), 'rnti': m['rnti'], 'assoc': None, 'pdu': {}, 'meas': {}, 'serving_pci': None,
                  'last_activity_s': None}
            ues.append(ue)
            du = None
        elif ue is not None:
            if m := RRC_ACTIVITY.search(line):
                ue['last_activity_s'] = int(m['s'])
            elif m := RRC_PDU.search(line):
                ue['pdu'][int(m['id'])] = m['status']
            elif m := RRC_ASSOC.search(line):
                ue['assoc'] = int(m['assoc']) if m['assoc'] else None
            elif m := RRC_SERVING.search(line):
                target = int(m['pci'])
                ue['serving_pci'] = target
            elif m := RRC_NEIGH.search(line):
                target = int(m['pci'])
            elif (m := RRC_SSB.search(line)) and target is not None:
                ue['meas'][target] = {k: float(v) for k, v in m.groupdict().items() if v is not None}
                target = None
    return {'dus': dus, 'ues': ues}
