"""Pinned upstream config adapter. Preserve upstream PHY instead of inventing it."""
import copy
import hashlib
import json
import re
import urllib.request
from importlib.resources import files
from pathlib import Path
import libconf
from .config import TTT_MS, PLACEHOLDER_A3_PCI, require, validate, digest, profile, warnings
from .profiles import PROFILES, frequencies, oai_gains, X300_MASTER_CLOCK_HZ

# OAI log header options. wall_clock = CLOCK_REALTIME epoch seconds with microseconds, so log
# events from every process on this host share one time base (utc_time prints no date).
LOG_OPTIONS = 'level,nocolor,wall_clock,line_num,function'
UICC_LINK = 'private_uicc.include'   # symlink to paths.ue_uicc_config (not hashed, not copied)


def lock():
    return json.loads(files('usrp_5g_oai').joinpath('upstream.json').read_text())


def fetch(destination, checkout=None):
    dst = Path(destination)
    dst.mkdir(parents=True, exist_ok=True)
    spec = lock()
    for key, a in spec['assets'].items():
        target = dst / (key + '.conf')
        if checkout:
            data = (Path(checkout).expanduser() / a['path']).read_bytes()
        elif target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == a['sha256']:
            continue
        else:
            url = f"https://raw.githubusercontent.com/OPENAIRINTERFACE/openairinterface5g/{spec['commit']}/{a['path']}"
            with urllib.request.urlopen(url, timeout=30) as r:
                data = r.read()
        require(hashlib.sha256(data).hexdigest() == a['sha256'],
                f'{key}: upstream checksum mismatch; is the checkout at {spec["commit"][:10]} and unmodified?')
        target.write_bytes(data)
    (dst / 'upstream.lock.json').write_text(json.dumps(spec, indent=2) + '\n')


def read_template(cache, key):
    asset = lock()['assets'][key]
    p = Path(cache) / (key + '.conf')
    require(p.exists(), f'{p} missing: run `oaiho fetch --checkout ~/openairinterface5g` first')
    data = p.read_bytes()
    require(hashlib.sha256(data).hexdigest() == asset['sha256'], f'{key}: altered upstream template')
    # libconf/Python rejects OAI's zero-padded decimal PLMN integers.
    # Normalize numeric assignments only; mnc_length preserves the original length.
    text = re.sub(r'(=\s*)0+(\d+)([Ll]?\s*[;,}])',
                  lambda m: m[1] + str(int(m[2])) + m[3], data.decode())
    # Reviewed includes: an RFsim neighbour file (replaced by our generated mapping) and an RFsim
    # satellite channel model that has no meaning on USRP hardware.
    for inc in asset.get('strip_includes', []):
        text = text.replace(f'@include "{inc}"', '')
    require('@include' not in text, 'unexpected upstream include requires adapter review')
    return libconf.loads(text)


def measurement(c):
    """nr_measurement_configuration for the CU (F1) or each gNB (N2).

    OAI at the pin builds one A3 reportConfig per neighbour whose PCI matches an A3 entry, else the
    default entry (physCellId -1), else none (rrc_gNB.c nr_rrc_get_measconfig). Manual mode with
    periodic reports therefore pins its only A3 entry to an unused PCI: the UE reports RSRP every
    1024 ms but never sends an A3 report, so no automatic handover can start.
    """
    h = c['handover']
    ttt = TTT_MS.index(h['ttt_ms'])
    if h['trigger'] == 'a3':
        a3 = {'physCellId': -1, 'offset': round(2 * h['offset_db']), 'hysteresis': round(2 * h['hysteresis_db']),
              'time_to_trigger': ttt}
    else:
        a3 = {'physCellId': PLACEHOLDER_A3_PCI, 'offset': 30, 'hysteresis': 30, 'time_to_trigger': len(TTT_MS) - 1}
    return {'Periodical': {'enable': int(h['periodic_reports']), 'includeBeamMeasurements': 1,
                           'maxNrofRS_IndexesToReport': 4},
            'A2': {'enable': 0, 'threshold': 60, 'time_to_trigger': ttt},
            'A3': (a3,)}


def wants_measurements(c):
    return c['handover']['trigger'] == 'a3' or c['handover']['periodic_reports']


def neighbours(c, scc):
    p = c['plmn']
    return tuple({'nr_cellid': a['nr_cellid'], 'neighbour_cell_configuration': tuple(
        {'gNB_ID': (c['cu']['gnb_id'] if c['topology'] == 'f1' else b['gnb_id']),
         'nr_cellid': b['nr_cellid'], 'physical_cellId': b['pci'],
         'absoluteFrequencySSB': scc['absoluteFrequencySSB'], 'subcarrierSpacing': scc['subcarrierSpacing'],
         'band': profile(c)['band'],
         'plmn': {'mcc': int(p['mcc']), 'mnc': int(p['mnc']), 'mnc_length': len(p['mnc'])},
         'tracking_area_code': p['tac']}
        for b in c['cells'] if b['name'] != a['name'])} for a in c['cells'])


def channel_config(c, names):
    ch = c['rfsim_channel']
    return {'max_chan': len(names) + 4, 'modellist': 'experiment_models',
            'experiment_models': tuple({'model_name': n, 'type': ch['model'],
                                        'ploss_dB': ch['ploss_db'], 'noise_power_dB': ch['noise_power_db'],
                                        'forgetfact': ch['forgetfact'], 'offset': ch['offset_samples'], 'ds_tdl': ch['ds_tdl']}
                                       for n in names)}


def plmn(c):
    p = c['plmn']
    snssai = {'sst': p['sst']}
    if p.get('sd') is not None:
        snssai['sd'] = int(p['sd'], 16) if isinstance(p['sd'], str) else p['sd']
    return ({'mcc': int(p['mcc']), 'mnc': int(p['mnc']), 'mnc_length': len(p['mnc']), 'snssaiList': (snssai,)},)


def core_network(g, c, n2_ip, n3_ip):
    g['amf_ip_address'] = ({'ipv4': c['core']['amf_ip']},)
    g['NETWORK_INTERFACES'] = {'GNB_IPV4_ADDRESS_FOR_NG_AMF': n2_ip,
                               'GNB_IPV4_ADDRESS_FOR_NGU': n3_ip, 'GNB_PORT_FOR_NGU': 2152}


def security(c):
    s = c['security']
    return {'ciphering_algorithms': tuple(s['ciphering']), 'integrity_algorithms': tuple(s['integrity']),
            'drb_ciphering': 'yes' if s['drb_ciphering'] else 'no',
            'drb_integrity': 'yes' if s['drb_integrity'] else 'no'}


def command_for(c, node, output):
    out = Path(output).resolve()
    if node == 'ue':
        require(c['mode'] == 'rfsim' or c['ue']['kind'] == 'oai_nrue', 'a hardware UE is launched only for ue.kind oai_nrue')
        prof = profile(c); f = frequencies(prof)
        argv = [c['paths']['nr_uesoftmodem'], '-O', str(out / 'ue.conf')] + (['--rfsim'] if c['mode'] == 'rfsim' else []) + [
                '-r', str(prof['prb']), '--numerology', str(prof['numerology']), '--band', str(prof['band']),
                '-C', str(round(f['dl_center_hz'])), '--ssb', str(f['ssb_start_subcarrier'])]
        if prof['duplex'] == 'FDD':
            argv += ['--CO', str(round(f['ul_center_hz'] - f['dl_center_hz']))]
        if c['mode'] == 'hardware':
            ur = c['ue_radio']
            # --ue-txgain is an ATTENUATION from the board maximum; OAI's default 0 means full power.
            argv += ['--usrp-args', ur['usrp_args'], '--ue-txgain', f"{ur['tx_atten_db']:g}", '--ue-rxgain', f"{ur['rx_gain']:g}",
                     '--ue-max-power', str(c['safety']['ue_pmax_dbm']), '--log_config.global_log_options', LOG_OPTIONS]
            argv += ['--clock-source', '1'] if ur['clock_source'] == 'external' else ['--ue-fo-compensation']
            if ur['subdev']:
                argv += ['--tx_subdev', ur['subdev'], '--rx_subdev', ur['subdev']]
        return argv
    endpoint = c['cu'] if node == 'cu' else next(x for x in c['cells'] if x['name'] == node)
    argv = [c['paths']['nr_softmodem'], '-O', str(out / (node + '.conf')), '--telnetsrv', '--telnetsrv.shrmod', 'ci',
            '--telnetsrv.listenaddr', endpoint['telnet_ip'], '--telnetsrv.listenport', str(endpoint['telnet_port']),
            '--log_config.global_log_options', LOG_OPTIONS]
    if node != 'cu':
        if c['mode'] == 'rfsim':
            argv += ['--rfsim']
        else:
            # Separate USRP write thread, as in OAI's own USRP command lines; reduces late TX packets.
            argv += ['--usrp-tx-thread-config', '1']
    return argv


def expected_radio(c):
    """What the runtime interlock must see in each DU's startup log before trusting it."""
    prof = profile(c); f = frequencies(prof)
    out = {}
    if c['ue']['kind'] == 'oai_nrue' and 'ue_radio' in c:
        ur = c['ue_radio']
        out['ue'] = {'tx_gain_cap_db': c['safety']['max_uhd_tx_gain_db'], 'uhd_tx_gain_db': ur['tx_gain_max_db'] - ur['tx_atten_db']
                     if ur['tx_gain_max_db'] is not None and ur['tx_atten_db'] is not None else None,
                     'tx_gain_max_db': ur['tx_gain_max_db'], 'tx_freq_hz': f['ul_center_hz'], 'rx_freq_hz': f['dl_center_hz'],
                     'sample_rate_sps': prof['sample_rate_sps'],
                     'master_clock_hz': X300_MASTER_CLOCK_HZ if 'x300' in ur['usrp_args'] else None,
                     'clock_source': ur['clock_source'], 'time_source': None}
    for x in c['cells']:
        g = oai_gains(x, prof)
        out[x['name']] = {'tx_gain_cap_db': c['safety']['max_uhd_tx_gain_db'], 'uhd_tx_gain_db': g['uhd_tx_gain_db'],
                          'uhd_rx_gain_db': g['uhd_rx_gain_db'], 'tx_gain_max_db': x['tx_gain_max_db'],
                          'tx_freq_hz': f['dl_center_hz'], 'rx_freq_hz': f['ul_center_hz'],
                          'sample_rate_sps': prof['sample_rate_sps'], 'master_clock_hz': X300_MASTER_CLOCK_HZ,
                          'clock_source': 'external', 'time_source': 'external'}
    return out


def check_phy(c, scc):
    """The registry's expected frequencies must match the pinned template exactly (before any carrier shift)."""
    prof = PROFILES[c['radio']['profile']]
    got = {'ssb_arfcn': scc['absoluteFrequencySSB'], 'dl_point_a_arfcn': scc['dl_absoluteFrequencyPointA'],
           'prb': scc['dl_carrierBandwidth'], 'numerology': scc['dl_subcarrierSpacing'], 'band': scc['dl_frequencyBand']}
    if prof['ul_point_a_arfcn'] is not None:
        got['ul_point_a_arfcn'] = scc['ul_absoluteFrequencyPointA']
    for k, v in got.items():
        require(prof[k] == v, f"profile {c['radio']['profile']}: template {k}={v} but registry says {prof[k]}")


def render(c, cache, output):
    validate(c)
    if c['mode'] == 'hardware' and c['ue']['kind'] == 'oai_nrue':
        ur = c['ue_radio']
        require(ur['usrp_args'] not in (None, '', 'TODO') and ur['tx_gain_max_db'] is not None and ur['tx_atten_db'] is not None,
                'ue_radio: fill usrp_args, tx_gain_max_db and tx_atten_db (from uhd_usrp_probe) before rendering; '
                "OAI's own default --ue-txgain 0 would be FULL power")
    out = Path(output).resolve()
    out.mkdir(parents=True, exist_ok=False)
    prof = profile(c)
    phy_doc = read_template(cache, prof['phy_template'])
    phy_g = phy_doc['gNBs'][0]
    phy = phy_g['servingCellConfigCommon']
    check_phy(c, phy[0])
    if prof['shift_steps']:
        # radio.dl_center_mhz: move SSB and Point A together by whole sync-raster steps (profiles.shifted).
        phy[0]['absoluteFrequencySSB'] = prof['ssb_arfcn']
        phy[0]['dl_absoluteFrequencyPointA'] = prof['dl_point_a_arfcn']
        if prof['ul_point_a_arfcn'] is not None:
            phy[0]['ul_absoluteFrequencyPointA'] = prof['ul_point_a_arfcn']
    # gNB-level PHY/MAC parameters that travel with the hardware template.
    extras = {'min_rxtxtime': phy_g.get('min_rxtxtime', 6)}
    if 'TIMERS' in phy_g:
        extras['TIMERS'] = copy.deepcopy(phy_g['TIMERS'])
    macrlc = {k: v for k, v in phy_doc['MACRLCs'][0].items() if k in ('pusch_TargetSNRx10', 'pucch_TargetSNRx10')}
    l1 = {k: v for k, v in phy_doc['L1s'][0].items() if k in ('prach_dtx_threshold', 'pucch0_dtx_threshold')}
    nodes = {}
    if c['topology'] == 'f1':
        d = read_template(cache, 'cu')
        d.pop('e2_agent', None)
        g = d['gNBs'][0]
        g.update(gNB_ID=c['cu']['gnb_id'], gNB_name='cu', plmn_list=plmn(c), tracking_area_code=c['plmn']['tac'],
                 local_s_address=c['cu']['f1_ip'], remote_s_address='0.0.0.0')
        d['Active_gNBs'] = ('cu',)
        d['security'] = security(c)
        core_network(g, c, c['cu']['n2_ip'], c['cu']['n3_ip'])
        if wants_measurements(c):
            g['neighbour_list'] = neighbours(c, phy[0])
            g['nr_measurement_configuration'] = measurement(c)
        nodes['cu'] = d
    for cell in c['cells']:
        d = read_template(cache, 'du' if c['topology'] == 'f1' else 'gnb')
        d.pop('e2_agent', None)
        g = d['gNBs'][0]
        name = cell['name']
        d['Active_gNBs'] = (name,)
        g.update(gNB_ID=c['cu']['gnb_id'] if c['topology'] == 'f1' else cell['gnb_id'],
                 gNB_name=name, nr_cellid=cell['nr_cellid'], plmn_list=plmn(c),
                 tracking_area_code=c['plmn']['tac'], servingCellConfigCommon=copy.deepcopy(phy), **copy.deepcopy(extras))
        s = g['servingCellConfigCommon'][0]
        s['physCellId'], s['ssb_PositionsInBurst_Bitmap'] = cell['pci'], cell['ssb_bitmap']
        s['pMax'] = c['safety']['ue_pmax_dbm']          # UE transmit-power ceiling broadcast in SIB1
        if c['radio']['prach_target_dbm'] is not None:
            s['preambleReceivedTargetPower'] = int(c['radio']['prach_target_dbm'])
        d['MACRLCs'][0].update(macrlc)
        d['L1s'][0].update(l1)
        if c['topology'] == 'f1':
            g['gNB_DU_ID'] = cell['du_id']
            d['MACRLCs'][0].update(local_n_address=cell['f1_ip'], remote_n_address=c['cu']['f1_ip'])
        else:
            core_network(g, c, cell['n2_ip'], cell['n3_ip'])
            d['security'] = security(c)
            g['neighbour_list'] = neighbours(c, phy[0])
            if wants_measurements(c):
                g['nr_measurement_configuration'] = measurement(c)
            else:
                g.pop('nr_measurement_configuration', None)
        ru = d['RUs'][0]
        ru.update(nb_tx=1, nb_rx=1, att_tx=int(cell['att_tx_db']), att_rx=int(cell['att_rx_db']),
                  max_rxgain=int(cell['max_rxgain']), bands=[prof['band']], clock_src='external', time_src='external')
        ru['sdr_addrs'] = f"type=x300,addr={cell['usrp_ip']}"
        if cell.get('serial') not in (None, '', 'UNKNOWN'):
            ru['sdr_addrs'] += ',serial=' + str(cell['serial'])
        ru['tx_subdev'] = ru['rx_subdev'] = cell['subdev']
        if c['mode'] == 'hardware':
            d.pop('rfsimulator', None)
        else:
            d['rfsimulator'] = ({'serveraddr': c['rfsim']['ue_server_ip'], 'serverport': c['rfsim']['port'], 'options': ()},)
            if c['rfsim_channel']['enabled']:
                d['rfsimulator'][0]['options'] = ('chanmod',)
                d['channelmod'] = channel_config(c, ['rfsimu_channel_enB0'])
        nodes[name] = d
    commands = {}
    for name, d in nodes.items():
        text = '# Derived from pinned OAI configuration; upstream LicenseRef-CSSL-1.0 applies.\n' + libconf.dumps(d)
        libconf.loads(text)  # Structural round-trip, not an OAI runtime test.
        (out / (name + '.conf')).write_text(text)
        commands[name] = command_for(c, name, out)
    if c['mode'] == 'hardware' and c['ue']['kind'] == 'oai_nrue':
        (out / UICC_LINK).symlink_to(Path(c['paths']['ue_uicc_config']).expanduser().resolve())
        (out / 'ue.conf').write_text(f'@include "{UICC_LINK}"\n')
        commands['ue'] = command_for(c, 'ue', out)
    if c['mode'] == 'rfsim':
        ue_cfg = {'rfsimulator': ({'serveraddr': 'server', 'serverport': c['rfsim']['port'], 'options': ()},)}
        if c['rfsim_channel']['enabled']:
            ue_cfg['rfsimulator'][0]['options'] = ('chanmod',)
            ue_cfg['channelmod'] = channel_config(c, [f'rfsimu_channel_ue{i}' for i in range(len(c['cells']))])
        # Include the private subscriber file at runtime; never copy SIM keys into the project.
        # OAI sets libconfig's include dir to the config's own folder and libconfig 1.5 prefixes it even to
        # absolute paths, so link the private file next to ue.conf and include it by relative name.
        (out / UICC_LINK).symlink_to(Path(c['paths']['ue_uicc_config']).expanduser().resolve())
        (out / 'ue.conf').write_text(f'@include "{UICC_LINK}"\n' + libconf.dumps(ue_cfg))
        commands['ue'] = command_for(c, 'ue', out)
    manifest = {'schema_version': 2, 'mode': c['mode'], 'topology': c['topology'], 'profile': c['radio']['profile'],
                'config_sha256': digest(c), 'upstream': lock(), 'commands': commands,
                'expected_radio': expected_radio(c) if c['mode'] == 'hardware' else {},
                'files': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in out.glob('*.conf')},
                'uicc_link': str(Path(c['paths']['ue_uicc_config']).expanduser().resolve()) if 'ue' in commands else None,
                'warnings': warnings(c), 'hardware_validated': False}
    (out / 'experiment.json').write_text(json.dumps(c, indent=2) + '\n')
    (out / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest
