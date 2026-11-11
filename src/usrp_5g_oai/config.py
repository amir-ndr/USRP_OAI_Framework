import hashlib
import ipaddress
import json
import math
import re
from pathlib import Path
import yaml
from .profiles import PROFILES, X300_RATES_SPS, UBX, frequencies, oai_gains, stream_mbps, fspl_db, is_ubx, shifted

SCHEMA_VERSION = 2
TTT_MS = (0, 40, 64, 80, 100, 128, 160, 256, 320, 480, 512, 640, 1024, 1280, 2560, 5120)
UE_KINDS = ('cots_modem', 'iphone', 'oai_nrue', 'other')
# Hard ceiling on the UHD TX gain for radiated (over-the-air) benches, whatever the YAML says.
# With TX/RX and RX2 antennas a few cm apart, the radio's own transmitter is the strongest
# signal its RX2 port sees; see docs/RF_SAFETY.md.
OTA_TX_GAIN_CEILING_DB = 10.0
OTA_PMAX_CEILING_DBM = 10
# PCI reserved for the "periodic reports only" A3 placeholder in manual mode (never a real cell).
PLACEHOLDER_A3_PCI = 1007


class ConfigError(ValueError):
    pass


def require(ok, message):
    if not ok:
        raise ConfigError(message)


def finite(x, name):
    require(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x),
            f"{name} must be a finite number")
    return x


def digest(c):
    return hashlib.sha256(json.dumps(c, sort_keys=True).encode()).hexdigest()


def load(path):
    c = yaml.safe_load(Path(path).read_text())
    validate(c)
    return c


def known_keys(obj, allowed, label, optional=''):
    require(isinstance(obj, dict), f'{label} must be a mapping')
    unknown = set(obj) - set(allowed.split()) - set(optional.split())
    require(not unknown, f'{label}: unknown fields {sorted(unknown)}')
    missing = set(allowed.split()) - set(obj)
    require(not missing, f'{label}: missing fields {sorted(missing)}')


SECTIONS = {
    'paths': 'oai_root nr_softmodem nr_uesoftmodem ue_uicc_config uhd_prefix',
    'host': 'links', 'clock': 'octoclock_ip',
    'plmn': 'mcc mnc tac sst sd', 'core': 'amf_ip',
    'cu': 'gnb_id f1_ip n2_ip n3_ip telnet_ip telnet_port',
    'radio': 'profile clock_source time_source continuous_tx prach_target_dbm',
    'security': 'ciphering integrity drb_ciphering drb_integrity',
    'ue': 'kind',
    'handover': 'trigger periodic_reports offset_db hysteresis_db ttt_ms',
    'rfsim': 'ue_server_ip port',
    'rfsim_channel': 'enabled model ploss_db noise_power_db forgetfact offset_samples ds_tdl',
    'attenuator': 'adapter_argv readback_qualified min_db max_db safe_db minimum_interval_s maximum_lateness_s timeout_s',
    'safety': 'rf_setup max_uhd_tx_gain_db ue_pmax_dbm rf_path_reviewed spectrum_authorized clock_and_pps_verified '
              'host_time_synchronized single_cell_attach_verified gain_mapping_verified evidence_note receivers'}
# Optional fields (absent = the documented default).
#   radio.dl_center_mhz: move the carrier inside the profile's band (licensed block); default = template centre.
#   safety.licence: {id, blocks_mhz: [[low, high], ...], max_eirp_dbm} - required by the live gate for OTA.
#   paths.core_dir: docker compose folder of the 5G core used by `oaiho session` (default ~/oai-cn5g).
OPTIONAL = {'radio': 'dl_center_mhz', 'safety': 'licence tx_power_bounds', 'paths': 'core_dir'}
UE_RADIO_KEYS = 'usrp_args usrp_link daughterboard subdev tx_gain_max_db tx_atten_db rx_gain clock_source'
CELL_KEYS = ('name pci nr_cellid du_id gnb_id f1_ip n2_ip n3_ip ssb_bitmap usrp_ip usrp_link serial daughterboard '
             'subdev att_tx_db att_rx_db max_rxgain tx_gain_min_db tx_gain_max_db rx_gain_min_db rx_gain_max_db '
             'telnet_ip telnet_port')


def active_cells(c):
    """Cells whose radios run in this experiment (experiment.active_cells; default: all).

    A single-cell qualification lists one cell: only its radio counts in the transport and RF budgets,
    run-node refuses the others and no handover can be triggered."""
    names = c.get('experiment', {}).get('active_cells')
    return [x for x in c['cells'] if names is None or x['name'] in names]


def profile(c):
    """The radio profile with the carrier actually used: the pinned template, or moved to radio.dl_center_mhz."""
    name = c['radio']['profile']
    mhz = c['radio'].get('dl_center_mhz')
    if mhz is None:
        p = dict(PROFILES[name]); p['shift_steps'], p['shift_hz'] = 0, 0.0
    else:
        try:
            p = shifted(name, finite(mhz, 'radio.dl_center_mhz') * 1e6)
        except ValueError as e:
            raise ConfigError(str(e))
    p['name'] = name
    return p


def check_licence(c, live=False):
    """Carrier inside a licensed block; for a live radiated run also a peak-EIRP bound under the licence limit."""
    lic = c['safety'].get('licence')
    if lic is None:
        require(not (live and c['mode'] == 'hardware' and c['safety']['rf_setup'] == 'ota'),
                'live hardware blocked: a radiated (ota) run needs safety.licence {id, blocks_mhz, max_eirp_dbm}')
        return None
    known_keys(lic, 'id blocks_mhz max_eirp_dbm', 'safety.licence')
    blocks = lic['blocks_mhz']
    require(isinstance(blocks, list) and all(isinstance(b, list) and len(b) == 2 and finite(b[0], 'block') < finite(b[1], 'block')
                                             for b in blocks), 'safety.licence.blocks_mhz: list of [low_mhz, high_mhz]')
    f = frequencies(profile(c))
    half = profile(c)['channel_bandwidth_hz'] / 2
    out = {'id': lic['id'], 'carriers': []}
    for side in ('dl', 'ul'):
        lo, hi = (f[f'{side}_center_hz'] - half) / 1e6, (f[f'{side}_center_hz'] + half) / 1e6
        inside = any(b[0] <= lo and hi <= b[1] for b in blocks)
        out['carriers'].append({'link': side, 'low_mhz': round(lo, 4), 'high_mhz': round(hi, 4), 'inside_licence': inside})
        if blocks and c['mode'] == 'hardware':
            require(inside, f'{side.upper()} carrier {lo:.3f}-{hi:.3f} MHz is outside the licensed blocks {blocks} '
                            '(set radio.dl_center_mhz inside a block)')
    if live and c['mode'] == 'hardware' and c['safety']['rf_setup'] == 'ota':
        require(str(lic['id'] or '').strip() and blocks, 'live hardware blocked: fill safety.licence.id and blocks_mhz')
        require(lic['max_eirp_dbm'] is not None, 'live hardware blocked: fill safety.licence.max_eirp_dbm from the licence')
        qualified_tx_peaks(c, live=True)
        bound = eirp_bound(c)
        require(bound['peak_eirp_bound_dbm'] <= finite(lic['max_eirp_dbm'], 'max_eirp_dbm'),
                f"peak EIRP bound {bound['peak_eirp_bound_dbm']:.1f} dBm (UBX at the TX gain cap + antenna gain) exceeds the "
                f"licence limit {lic['max_eirp_dbm']} dBm: lower safety.max_uhd_tx_gain_db")
        out.update(bound)
    return out


def qualified_tx_peaks(c, live=False):
    """Connector peaks qualified for this profile and gain cap; no power is inferred from gain."""
    bounds = c['safety'].get('tx_power_bounds') or {}
    require(isinstance(bounds, dict), 'safety.tx_power_bounds must be a mapping')
    sources = {x['name'] for x in active_cells(c)} | {'ue'}
    out = {}
    for name in sources:
        b = bounds.get(name)
        require(b is None or isinstance(b, dict), f'tx_power_bounds.{name} must be a mapping')
        if b is None or b.get('peak_dbm') is None:
            require(not live, f'live hardware blocked: qualify safety.tx_power_bounds.{name}.peak_dbm')
            continue
        known_keys(b, 'peak_dbm maximum_gain_db profile frequency_range_mhz antenna_gain_dbi evidence', f'tx_power_bounds.{name}')
        peak = finite(b['peak_dbm'], 'peak_dbm')
        require(-200 <= peak <= 100, 'implausible qualified peak_dbm')
        require(-30 <= finite(b['antenna_gain_dbi'], 'antenna_gain_dbi') <= 50, 'implausible antenna gain')
        require(finite(b['maximum_gain_db'], 'maximum_gain_db') >= c['safety']['max_uhd_tx_gain_db'],
                f'{name}: qualified power bound does not cover the configured gain cap')
        require(b['profile'] == c['radio']['profile'], f'{name}: power calibration profile differs')
        f = frequencies(profile(c))
        center = f['ul_center_hz'] if name == 'ue' else f['dl_center_hz']
        half = profile(c)['channel_bandwidth_hz'] / 2
        fr = b['frequency_range_mhz']
        require(isinstance(fr, list) and len(fr) == 2 and finite(fr[0], 'calibration lower frequency') <= (center-half)/1e6
                and (center+half)/1e6 <= finite(fr[1], 'calibration upper frequency'), f'{name}: power calibration does not cover this channel')
        require(bool(b['evidence']) and not re.search(r'\b(?:TODO|ASSUMPTION|UNKNOWN)\b', b['evidence'], re.I),
                f'{name}: supply qualified power evidence covering frequency, waveform and startup/restart')
        out[name] = peak
    return out


def eirp_bound(c):
    """Qualified per-transmitter EIRP when supplied, otherwise an explicitly unqualified planning estimate.

    Antenna gain is retained separately from path coupling; qualifying a measured path must not erase
    the antenna contribution to EIRP. Feed loss is not credited in this conservative bound.
    """
    cap = c['safety']['max_uhd_tx_gain_db']
    tx_max = max([x['tx_gain_max_db'] for x in active_cells(c) if x['tx_gain_max_db'] is not None] + [UBX['tx_gain_max_db']])
    gains = [p['ota']['tx_antenna_gain_dbi'] for rx in c['safety'].get('receivers') or [] for p in rx.get('paths', []) if 'ota' in p]
    ant = max(gains) if gains else 0.0
    peaks = qualified_tx_peaks(c)
    qualified = len(peaks) == len(active_cells(c)) + 1
    peak = max(peaks.values()) if qualified else UBX['max_tx_dbm'] - (tx_max - min(cap, tx_max))
    eirp = peak + ant
    if qualified:
        bounds = c['safety']['tx_power_bounds']
        ant = max(bounds[n]['antenna_gain_dbi'] for n in peaks)
        eirp = max(v + bounds[n]['antenna_gain_dbi'] for n, v in peaks.items())
    return {'qualified': qualified, 'peak_conducted_bound_dbm': round(peak, 2), 'antenna_gain_dbi': ant,
            'peak_eirp_bound_dbm': round(eirp, 2)}


def validate(c, live=False):
    require(isinstance(c, dict), "configuration must be a mapping")
    require(c.get('schema_version') == SCHEMA_VERSION,
            f"schema_version must be {SCHEMA_VERSION} (schema 1 files: compare with configs/lab_n78.yaml)")
    base = 'schema_version mode topology experiment ' + ' '.join(SECTIONS) + ' cells'
    known_keys({k: v for k, v in c.items() if k != 'ue_radio'}, base, 'config')
    if 'ue_radio' in c:
        known_keys(c['ue_radio'], UE_RADIO_KEYS, 'ue_radio')
        require(c['ue_radio']['clock_source'] in ('external', 'internal'), 'ue_radio.clock_source: external (OctoClock) or internal')
    for name, allowed in SECTIONS.items():
        known_keys(c[name], allowed, name, OPTIONAL.get(name, ''))
    require(c.get("mode") in ("hardware", "rfsim"), "mode: hardware or rfsim")
    require(c.get("topology") in ("f1", "n2"), "topology: f1 or n2; Xn is not implemented here")

    r = c['radio']
    require(r['profile'] in PROFILES, f"radio.profile must be one of {sorted(PROFILES)}")
    prof = profile(c)                     # also checks radio.dl_center_mhz (raster, band edges)
    require(prof['sample_rate_sps'] in X300_RATES_SPS, 'profile sample rate is not accepted by the pinned X300 driver')
    require(r.get('clock_source') == 'external' and r.get('time_source') == 'external',
            'hardware profile requires external 10 MHz and PPS (OctoClock)')
    require(r.get('continuous_tx') is False, 'continuous TX is outside the reviewed baseline')
    if r['prach_target_dbm'] is not None:
        v = finite(r['prach_target_dbm'], 'prach_target_dbm')
        require(-202 <= v <= -60 and v % 2 == 0, 'prach_target_dbm: even integer in -202..-60 (38.331)')

    cells = c.get("cells", [])
    require(isinstance(cells, list) and len(cells) >= 2, "at least two cells required")
    for key in ("name", "pci", "nr_cellid", "du_id", "f1_ip"):
        vals = [x[key] for x in cells]
        require(len(set(vals)) == len(vals), f"duplicate cell {key}")
    if c["topology"] == "n2":
        require(len({x['gnb_id'] for x in cells}) == len(cells), "N2 requires unique gnb_id")
    lmax_mask = (1 << prof['ssb_lmax']) - 1
    for x in cells:
        known_keys(x, CELL_KEYS, 'cell')
        require(isinstance(x['pci'], int) and 0 <= x['pci'] <= 1007, "PCI must be 0..1007")
        require(x['pci'] != PLACEHOLDER_A3_PCI, f'PCI {PLACEHOLDER_A3_PCI} is reserved by this framework')
        require(0 <= x['nr_cellid'] < 2**36, "NR cell identity must fit 36 bits")
        require(x['name'].replace('_', '').isalnum(), "cell names: letters, numbers, underscores")
        for k in ('f1_ip', 'usrp_ip', 'telnet_ip'):
            ipaddress.ip_address(x[k])
        ipaddress.ip_interface(x['n2_ip']); ipaddress.ip_interface(x['n3_ip'])
        require(ipaddress.ip_address(x['telnet_ip']).is_loopback, 'bind telnet only on loopback')
        b = x['ssb_bitmap']
        require(isinstance(b, int) and 1 <= b <= lmax_mask and b & (b - 1) == 0,
                f"ssb_bitmap: one-hot value within the {prof['ssb_lmax']} SSB positions of this profile")
        require(0 <= finite(x['att_tx_db'], 'att_tx_db') <= 100, "invalid TX attenuation")
        require(0 <= finite(x['att_rx_db'], 'att_rx_db') <= 100, "invalid RX attenuation")
        finite(x['max_rxgain'], 'max_rxgain')
        for key in ('att_tx_db', 'att_rx_db', 'max_rxgain'):
            require(float(x[key]).is_integer(), f'{key}: this OAI RU config uses whole-dB integer fields')
    require(len({x['ssb_bitmap'] for x in cells}) == len(cells), "use different SSB bitmaps")
    require(isinstance(c.get('experiment', {}).get('ue_receive_only', False), bool), 'experiment.ue_receive_only: true or false')
    ac = c.get('experiment', {}).get('active_cells')
    if ac is not None:
        require(isinstance(ac, list) and ac and len(set(ac)) == len(ac) and set(ac) <= {x['name'] for x in cells},
                'experiment.active_cells: non-empty list of distinct cell names')
    require(ipaddress.ip_address(c['cu']['telnet_ip']).is_loopback, 'bind CU telnet only on loopback')

    h = c['handover']
    require(h['trigger'] in ('manual', 'a3'), "trigger must be manual or a3")
    require(isinstance(h['periodic_reports'], bool), 'handover.periodic_reports: true or false')
    for key, lo, hi in [('offset_db', -15, 15), ('hysteresis_db', 0, 15)]:
        v = finite(h[key], key)
        require(lo <= v <= hi and float(v * 2).is_integer(), f"{key}: half-dB steps in [{lo}, {hi}]")
    require(h['ttt_ms'] in TTT_MS, "invalid 38.331 time-to-trigger")
    if c['mode'] == 'rfsim':
        require(h['trigger'] == 'manual', "RFsim A3 is not qualified by this framework; use manual RFsim (docs/PROJECT_REVIEW.md)")

    p = c['plmn']
    require(isinstance(p['mcc'], str) and len(p['mcc']) == 3 and p['mcc'].isdigit(), 'quote the three-digit MCC')
    require(isinstance(p['mnc'], str) and len(p['mnc']) in (2, 3) and p['mnc'].isdigit(), 'quote the two/three-digit MNC')

    s = c['security']
    require(isinstance(s['ciphering'], list) and s['ciphering'] and set(s['ciphering']) <= {'nea0', 'nea1', 'nea2', 'nea3'},
            'security.ciphering: non-empty list of nea0..nea3 (first one the UE supports is used)')
    require(isinstance(s['integrity'], list) and s['integrity'] and set(s['integrity']) <= {'nia0', 'nia1', 'nia2', 'nia3'},
            'security.integrity: non-empty list of nia0..nia3')
    require(isinstance(s['drb_ciphering'], bool) and isinstance(s['drb_integrity'], bool), 'security.drb_*: true or false')
    require(c['ue']['kind'] in UE_KINDS, f'ue.kind must be one of {UE_KINDS}')
    if c['ue']['kind'] == 'iphone':
        # Apple: "the use of null-ciphering isn't supported" and RRC/NAS need confidentiality + integrity.
        require(s['ciphering'][0] != 'nea0' and s['integrity'][0] != 'nia0' and s['drb_ciphering'],
                'iPhone private 5G SA rejects null ciphering: list nea2 (or nea1) and nia2 first, keep drb_ciphering true')

    ch = c['rfsim_channel']
    require(not (c['mode'] == 'hardware' and ch.get('enabled')), 'RFsim channel models do not affect USRP hardware')
    if ch.get('enabled'):
        require(ch.get('model') in ('AWGN', 'Rayleigh1', 'Rice8', 'TDL_A', 'TDL_B', 'TDL_C'), 'unsupported channel name')
        for k in ('ploss_db', 'noise_power_db', 'forgetfact', 'ds_tdl'):
            finite(ch[k], k)
        require(0 <= ch['forgetfact'] <= 1 and ch['ds_tdl'] >= 0, 'invalid channel parameters')

    sf = c['safety']
    require(sf['rf_setup'] in ('ota', 'conducted'), 'safety.rf_setup: ota (antennas) or conducted (cables + pads)')
    cap = finite(sf['max_uhd_tx_gain_db'], 'max_uhd_tx_gain_db')
    require(cap >= 0, 'max_uhd_tx_gain_db must be >= 0')
    if sf['rf_setup'] == 'ota':
        require(cap <= OTA_TX_GAIN_CEILING_DB,
                f'radiated bench: max_uhd_tx_gain_db may not exceed {OTA_TX_GAIN_CEILING_DB} dB (docs/RF_SAFETY.md)')
    pmax = sf['ue_pmax_dbm']
    require(isinstance(pmax, int) and not isinstance(pmax, bool) and -30 <= pmax <= 23, 'ue_pmax_dbm: integer -30..23 dBm')
    if sf['rf_setup'] == 'ota':
        require(pmax <= OTA_PMAX_CEILING_DBM, f'radiated bench: ue_pmax_dbm may not exceed {OTA_PMAX_CEILING_DBM} dBm')
    check_licence(c, live)
    if c['mode'] == 'hardware':
        hardware_checks(c)
    if live and c['mode'] == 'hardware':
        safety(c)
    return c


def hardware_checks(c):
    """Static radio checks that apply to every hardware config, live or not."""
    links = c['host']['links']
    require(isinstance(links, dict) and links, 'host.links: map each host NIC that carries USRP samples')
    for name, link in links.items():
        require(isinstance(link, dict) and set(link) == {'mbps', 'max_utilization'}, f'host.links.{name}: mbps, max_utilization')
        require(finite(link['mbps'], 'mbps') > 0 and 0 < finite(link['max_utilization'], 'max_utilization') <= 1,
                f'host.links.{name}: invalid capacity')
    load = {}
    rate = profile(c)['sample_rate_sps']
    for x in c['cells']:
        require(x['usrp_link'] in links, f"{x['name']}: usrp_link {x['usrp_link']!r} is not listed in host.links")
    for x in active_cells(c):                      # only radios that stream in this experiment load a link
        load[x['usrp_link']] = load.get(x['usrp_link'], 0) + stream_mbps(rate)
    if c['ue']['kind'] == 'oai_nrue':
        require('ue_radio' in c, 'ue.kind oai_nrue on hardware needs a ue_radio section (the third USRP)')
        ur = c['ue_radio']
        args = ur['usrp_args'] or ''
        ethernet = any(part.strip().split('=')[0].startswith('addr') for part in args.split(',') if '=' in part)
        require(not ethernet or ur['usrp_link'] is not None,
                'Ethernet ue_radio needs a usrp_link in host.links; null is only for USB or an unfilled placeholder')
        if ur['usrp_link'] is not None:          # Ethernet USRP: its streams share that link; USB (B2xx): null
            require(ur['usrp_link'] in links, f"ue_radio.usrp_link {ur['usrp_link']!r} is not listed in host.links")
            load[ur['usrp_link']] = load.get(ur['usrp_link'], 0) + stream_mbps(rate)
    for name, mbps in load.items():
        budget = links[name]['mbps'] * links[name]['max_utilization']
        require(mbps <= budget,
                f'{name}: {mbps:.0f} Mbit/s per direction for {rate/1e6:g} MS/s sc16 streams exceeds the '
                f'{budget:.0f} Mbit/s budget. Use a smaller profile (n66_25prb_15khz fits two X310s on one 1 GbE link), '
                'one NIC per radio, or 10 GbE.')
    cap = c['safety']['max_uhd_tx_gain_db']
    check_ue_radio_gain(c)
    for x in c['cells']:
        if x['tx_gain_max_db'] is None:
            continue                                   # unknown board: live checks block launch
        for k in ('tx_gain_min_db', 'tx_gain_max_db', 'rx_gain_min_db', 'rx_gain_max_db'):
            finite(x[k], f"{x['name']}.{k}")
        g = oai_gains(x, profile(c))
        tx = g['uhd_tx_gain_db']
        require(x['tx_gain_min_db'] <= tx <= x['tx_gain_max_db'],
                f"{x['name']}: att_tx_db={x['att_tx_db']} gives UHD TX gain {tx:g} dB outside the probed range")
        require(tx <= cap + 1e-9,
                f"{x['name']}: UHD TX gain {tx:g} dB (= {x['tx_gain_max_db']:g} - att_tx_db) exceeds "
                f"safety.max_uhd_tx_gain_db={cap:g}. Raise att_tx_db; never lower the cap without a measured budget.")
        rx = g['uhd_rx_gain_db']
        require(x['rx_gain_min_db'] <= rx <= x['rx_gain_max_db'],
                f"{x['name']}: max_rxgain - att_rx_db - X310 cal offset {g['rx_cal_offset_db']:g} dB = {rx:g} dB "
                f"is outside the probed RX gain range [{x['rx_gain_min_db']:g}, {x['rx_gain_max_db']:g}]; "
                'OAI would clip it silently')


def ue_radio_gain(c):
    """nr-uesoftmodem uses the gNB convention: UHD TX gain = board maximum - --ue-txgain (default 0 = FULL power)."""
    ur = c.get('ue_radio') or {}
    if ur.get('tx_gain_max_db') is None or ur.get('tx_atten_db') is None:
        return None
    return finite(ur['tx_gain_max_db'], 'ue_radio.tx_gain_max_db') - finite(ur['tx_atten_db'], 'ue_radio.tx_atten_db')


def check_ue_radio_gain(c):
    if c['mode'] != 'hardware' or c['ue']['kind'] != 'oai_nrue':
        return
    g = ue_radio_gain(c)
    if g is None:
        return                                   # unknown board: --live blocks launch
    cap = c['safety']['max_uhd_tx_gain_db']
    require(0 <= g <= cap + 1e-9, f'ue_radio: UHD TX gain {g:g} dB (= tx_gain_max_db - tx_atten_db) must be within 0..{cap:g} dB '
                                  '(safety.max_uhd_tx_gain_db). Raise tx_atten_db.')


def warnings(c):
    """Non-fatal advice printed by validate/render."""
    out = []
    if c['ue']['kind'] == 'iphone':
        if c['plmn']['mcc'] != '999':
            out.append("iPhone: Apple lists MCC 999 (any MNC) or nationally designated private codes for private 5G; "
                       f"PLMN {c['plmn']['mcc']}/{c['plmn']['mnc']} is not on that list (docs/UE_SETUP.md).")
        out.append('iPhone: the SIM and core must use a non-null SUCI scheme (Profile A in OAI UDM), and 5G SA must be '
                   'switched on in Settings > Cellular.')
    if c['mode'] == 'hardware':
        for x in c['cells']:
            if x['serial'] in (None, '', 'UNKNOWN'):
                out.append(f"{x['name']}: serial unknown - copy it from uhd_usrp_probe before going live.")
        if c['safety']['rf_setup'] == 'ota':
            out.append('Radiated test: requires documented authorization and qualified RF paths; see docs/RF_SAFETY.md.')
    if c['handover']['trigger'] == 'manual' and not c['handover']['periodic_reports']:
        out.append('periodic_reports is false: the dashboard will not show UE-reported serving/neighbour RSRP.')
    if c['ue']['kind'] == 'oai_nrue' and c['handover']['trigger'] == 'a3':
        out.append('nrUE A3 exists at the pin, but its hysteresis scaling and TTT behavior need qualification; '
                   'do not interpret a parameter sweep as standards-conformant (docs/PROJECT_REVIEW.md).')
    return out


def ota_loss(c, rx_name, p):
    """Minimum passive loss of an over-the-air path: free-space loss minus antenna gains plus fixed pads."""
    o = p['ota']
    require(isinstance(o, dict) and set(o) == {'distance_m', 'tx_antenna_gain_dbi', 'rx_antenna_gain_dbi'},
            f'{rx_name}<-{p["source"]}: ota needs distance_m, tx_antenna_gain_dbi, rx_antenna_gain_dbi')
    d = finite(o['distance_m'], 'distance_m')
    require(d >= 0.5, f'{rx_name}<-{p["source"]}: below 0.5 m free-space loss is not credible; measure the coupling '
                      'and enter minimum_passive_loss_db instead')
    f = frequencies(profile(c))
    freq = f['ul_center_hz'] if p['source'] == 'ue' else f['dl_center_hz']
    pad = finite(p.get('fixed_pad_db', 0), 'fixed_pad_db')
    return fspl_db(d, freq) - finite(o['tx_antenna_gain_dbi'], 'tx gain') - finite(o['rx_antenna_gain_dbi'], 'rx gain') + pad


def source_peak(c, p, cells_by_name):
    bounds = c['safety'].get('tx_power_bounds') or {}
    if p['source'] in bounds and bounds[p['source']].get('peak_dbm') is not None:
        return qualified_tx_peaks(c)[p['source']]
    v = p['source_peak_dbm']
    if v == 'auto' and p['source'] == 'ue':
        ur = c.get('ue_radio') or {}
        require(c['ue']['kind'] == 'oai_nrue' and is_ubx(ur), "source_peak_dbm 'auto' for the UE needs a UBX-based ue_radio")
        cap = c['safety']['max_uhd_tx_gain_db']
        return UBX['max_tx_dbm'] - (ur['tx_gain_max_db'] - min(cap, ur['tx_gain_max_db']))
    if v == 'auto':
        x = cells_by_name.get(p['source'])
        require(x is not None and is_ubx(x), "source_peak_dbm 'auto' is only defined for UBX cell transmitters")
        # Budget at the CAP, not the current setting, so any att_tx inside the cap stays covered.
        cap = c['safety']['max_uhd_tx_gain_db']
        return UBX['max_tx_dbm'] - (x['tx_gain_max_db'] - min(cap, x['tx_gain_max_db']))
    return finite(v, 'source_peak_dbm')


def safety(c):
    """Go-live gate: operator-confirmed checks plus a passing RF budget. This is NOT an RF power sensor."""
    s = c['safety']
    for key in ('rf_path_reviewed', 'spectrum_authorized', 'clock_and_pps_verified',
                'host_time_synchronized', 'gain_mapping_verified'):
        require(s.get(key) is True, f"live hardware blocked: safety.{key} is not true")
    require(bool(s.get('evidence_note')), 'record calibration / authorization evidence_note')
    active = {x['name'] for x in active_cells(c)} | {'ue'}
    peaks = qualified_tx_peaks(c, live=s['rf_setup'] == 'ota')
    for rx in s['receivers']:
        if rx['name'] not in active:
            continue
        for p in rx.get('paths', []):
            if p['source'] not in active:
                continue
            require(p['source_peak_dbm'] != 'auto' or p['source'] in peaks,
                    'live hardware blocked: source_peak_dbm auto is a gain-based planning estimate; '
                    'supply a qualified connector peak bound covering startup/restart (docs/PROJECT_REVIEW.md)')
            require(not re.search(r'\b(?:TODO|ASSUMPTION|UNKNOWN)\b', p.get('evidence', ''), re.I),
                    'live hardware blocked: replace RF path assumptions/placeholders with qualified evidence')
            require('ota' not in p, 'live hardware blocked: free-space geometry is planning only; replace ota with '
                    'a qualified minimum_passive_loss_db and coupling evidence')
    for x in active_cells(c):
        require(x.get('serial') not in (None, '', 'UNKNOWN'), f"record {x['name']} USRP serial")
    if c['ue']['kind'] == 'oai_nrue':
        ur = c['ue_radio']
        require(ur['daughterboard'] not in (None, '', 'UNKNOWN') and ue_radio_gain(c) is not None,
                'ue_radio: identify the third USRP (daughterboard, tx_gain_max_db from uhd_usrp_probe, tx_atten_db)')
    return budget(c)


def budget(c):
    """Worst-case peak input at every receiver from the supplied paths (works before the flags are set)."""
    s = c['safety']
    cells_by_name = {x['name']: x for x in c['cells']}
    for x in c['cells']:
        require(x.get('daughterboard') not in (None, '', 'UNKNOWN'), f"identify {x['name']} daughterboard")
        for k in ('tx_gain_min_db', 'tx_gain_max_db', 'rx_gain_min_db', 'rx_gain_max_db'):
            finite(x[k], k)
    require(s.get('receivers'), 'complete receiver RF budget required')
    cells = {x['name'] for x in active_cells(c)}
    # Radios that are not part of this experiment are powered off / not cabled: neither receivers nor sources.
    receivers = {x['name']: x for x in s['receivers'] if x['name'] in cells or x['name'] == 'ue'}
    require(cells | {'ue'} <= set(receivers), 'RF budget must cover each active radio and the UE')
    report = []
    for name, rx in receivers.items():
        limit = finite(rx['max_input_dbm'], 'max_input_dbm')
        margin = finite(rx['margin_db'], 'margin_db')
        require(margin >= 6, 'choose at least 6 dB engineering margin; larger may be necessary')
        require(rx.get('limit_source'), f'{name}: cite exact input limit source')
        paths = rx.get('paths', [])
        require(paths, f'{name}: no source-to-receiver paths supplied')
        sources = {x['source'] for x in paths}
        # A radio's own transmitter (TX/RX port) couples into its own RX2 port: always budget it.
        # A USRP-based UE also couples its own TX into its RX port.
        needed = (cells | {'ue'}) if (name in cells or c['ue']['kind'] == 'oai_nrue') else cells
        require(needed <= sources, f'{name}: cover every transmitter: {sorted(needed - sources)} missing '
                                   '(cells include their own TX->RX2 self-coupling)')
        amplitude_bound = 0.0
        detail = []
        for p in paths:
            if p['source'] != 'ue' and p['source'] not in cells:
                continue                               # inactive radio: not powered/cabled in this phase
            peak = source_peak(c, p, cells_by_name)
            if 'ota' in p:
                require(p.get('minimum_passive_loss_db') is None, f'{name}<-{p["source"]}: give ota OR minimum_passive_loss_db')
                loss = ota_loss(c, name, p)
            else:
                loss = finite(p['minimum_passive_loss_db'], 'minimum_passive_loss_db')
            uncertainty = finite(p['uncertainty_db'], 'uncertainty_db')
            require(loss >= 0 and uncertainty >= 0, 'invalid loss/uncertainty')
            require(-200 <= peak <= 100 and loss <= 300 and uncertainty <= 100, 'implausible RF budget magnitude; verify units')
            require(p.get('evidence'), 'each RF path needs measurement/design evidence')
            # Variable attenuation is deliberately NOT credited: assume it fails to 0 dB.
            bound = peak - loss + uncertainty
            amplitude_bound += 10 ** (bound / 20)
            detail.append({'source': p['source'], 'source_peak_dbm': round(peak, 2), 'min_loss_db': round(loss, 2),
                           'bound_dbm': round(bound, 2)})
        # Worst-case aligned peak amplitudes; average-power addition would not be a conservative bound.
        aggregate = 20 * math.log10(amplitude_bound)
        require(aggregate <= limit - margin, f'{name}: aggregate {aggregate:.2f} dBm exceeds guarded limit {limit-margin:.2f} dBm')
        report.append({'receiver': name, 'aggregate_peak_bound_dbm': round(aggregate, 2),
                       'guarded_limit_dbm': limit - margin, 'paths': detail})
    return report


def gain_report(c):
    """Human-readable view of what OAI will request from UHD, for `oaiho gains`."""
    prof = profile(c)
    f = frequencies(prof)
    out = {'profile': prof['name'], 'sample_rate_msps': prof['sample_rate_sps'] / 1e6, 'band': prof['band'],
           'duplex': prof['duplex'], 'carrier_shift_mhz': round(prof['shift_hz'] / 1e6, 4),
           'dl_center_mhz': round(f['dl_center_hz'] / 1e6, 4), 'ul_center_mhz': round(f['ul_center_hz'] / 1e6, 4),
           'ssb_mhz': round(f['ssb_hz'] / 1e6, 4), 'ue_pmax_dbm': c['safety']['ue_pmax_dbm'],
           'max_uhd_tx_gain_db': c['safety']['max_uhd_tx_gain_db'], 'cells': []}
    for x in c['cells']:
        g = oai_gains(x, prof)
        out['cells'].append({'cell': x['name'], 'usrp_ip': x['usrp_ip'], 'att_tx_db': x['att_tx_db'],
                             'uhd_tx_gain_db': g['uhd_tx_gain_db'], 'tx_peak_bound_dbm': g['tx_peak_bound_dbm'],
                             'max_rxgain': x['max_rxgain'], 'att_rx_db': x['att_rx_db'],
                             'rx_cal_offset_db': g['rx_cal_offset_db'], 'uhd_rx_gain_db': g['uhd_rx_gain_db']})
    return out
