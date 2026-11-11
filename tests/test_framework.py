import copy
import csv
import json
import math
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
import libconf
from usrp_5g_oai.config import ConfigError, load, validate, safety, budget
from usrp_5g_oai.oai import measurement, render
from usrp_5g_oai.channels import trajectory
from usrp_5g_oai.runtime import trigger, verify_manifest, Interlock
from usrp_5g_oai.analysis import demo, analyze, compare, extract_events
from usrp_5g_oai.traffic import probe, ping_probe
from usrp_5g_oai.profiles import frequencies, x310_rx_cal_offset, oai_gains, stream_mbps
from usrp_5g_oai.oailog import parse, parse_rrc_stats, oai_epoch
from usrp_5g_oai.dashboard import State, replay_timeline

ROOT = Path(__file__).resolve().parents[1]
CACHE = Path(os.environ.get('OAI_TEMPLATE_CACHE', ROOT / '.cache/oai'))
EARLIER_RUN = Path.home() / 'openairinterface5g/tools/handover_experiments/results/f1/run_002'

# Real console lines printed by OAI 2026.w37 (copied from the RFsim runs on this host).
CU_LINES = [
    '[NR_RRC] [UL] (cellID bc614e, UE ID 1 RNTI dc91) Received RRCSetupComplete (RRC_CONNECTED reached)',
    '[NR_RRC] Handover triggered for UE 1/RNTI dc91 towards cell 11111111/assoc_id 96/PCI 1',
    '[NR_RRC] HO acknowledged: Send reconfiguration for UE 1/RNTI dc91...',
    '[NR_RRC] [UL] (cellID a98ac7, UE ID 1 RNTI 6e17) Received RRCReconfigurationComplete',
    '[NR_RRC] handover for UE 1/RNTI 6e17 complete!',
]
DU_LINES = [
    '[NR_MAC] Frame.Slot 256.0',
    'UE RNTI dc91 CU-UE-ID 1 in-sync PH 48 dB PCMAX 20 dBm, average RSRP -43 (14 meas)',
    'UE dc91: dlsch_rounds 17/0/0/0, dlsch_errors 0, pucch0_DTX 0 (SNR 16.7+1.7) RSSI -37.3, BLER 0.05905 MCS (0) 0 (Qm 2) CCE fail 2',
    'UE dc91: ulsch_rounds 44/0/0/0, ulsch_errors 0, ulsch_DTX 0, BLER 0.03487 MCS (0) 0 (Qm 2 deltaMCS 0) NPRB 5 SNR 22.1 (+2.1) RSSI -41.9 CCE fail 0',
    'UE dc91: LCID 1,2,4, goodput DL    0.25 UL    0.10 Mbps',
]


class FrameworkTests(unittest.TestCase):
    def setUp(self):
        self.c = load(ROOT / 'tests/fixtures/configs/hardware_f1_manual.yaml')
        self.b = load(ROOT / 'tests/fixtures/configs/bench_f1_manual.yaml')

    # ------------------------------------------------------------ configuration
    def test_default_hardware_blocked(self):
        with self.assertRaises(ConfigError): validate(self.c, live=True)
        with self.assertRaises(ConfigError): validate(self.b, live=True)

    def test_duplicate_pci(self):
        self.c['cells'][1]['pci'] = 0
        with self.assertRaises(ConfigError): validate(self.c)

    def test_unknown_radio_field(self):
        self.c['radio']['samplerate'] = 61440000
        with self.assertRaises(ConfigError): validate(self.c)

    def test_unsupported_sample_rate(self):
        self.c['radio']['sample_rate_hz'] = 1e6
        with self.assertRaises(ConfigError): validate(self.c)

    def test_overlapping_ssb_positions(self):
        self.c['cells'][1]['ssb_bitmap'] = 3
        with self.assertRaises(ConfigError): validate(self.c)

    def test_ssb_bitmap_respects_profile_lmax(self):
        self.b['cells'][1]['ssb_bitmap'] = 16          # n66 15 kHz has only 4 SSB positions
        with self.assertRaises(ConfigError): validate(self.b)

    def test_a3_units(self):
        c = copy.deepcopy(self.b); c['handover'].update(trigger='a3', offset_db=3, hysteresis_db=1.5, ttt_ms=160)
        a = measurement(c)['A3'][0]
        self.assertEqual((a['physCellId'], a['offset'], a['hysteresis'], a['time_to_trigger']), (-1, 6, 3, 6))

    def test_manual_measurement_never_matches_a_cell(self):
        m = measurement(self.b)
        self.assertEqual(m['Periodical']['enable'], 1)
        self.assertNotIn(m['A3'][0]['physCellId'], [x['pci'] for x in self.b['cells']] + [-1])

    def test_ttt_reject_milliseconds_as_enum(self):
        self.c['handover']['ttt_ms'] = 6
        with self.assertRaises(ConfigError): validate(self.c)

    def test_fractional_gain_rejected(self):
        self.c['cells'][0]['att_tx_db'] = 20.5
        with self.assertRaises(ConfigError): validate(self.c)

    def test_nan_rejected(self):
        self.c['cells'][0]['att_tx_db'] = float('nan')
        with self.assertRaises(ConfigError): validate(self.c)

    def test_rf_model_not_silently_applied_to_hardware(self):
        self.c['rfsim_channel']['enabled'] = True
        with self.assertRaises(ConfigError): validate(self.c)

    def test_no_nrue_a3_claim(self):
        self.c['mode'] = 'rfsim'; self.c['handover']['trigger'] = 'a3'
        with self.assertRaises(ConfigError): validate(self.c)

    # ------------------------------------------------------------ radio safety rules
    def test_transport_budget_on_shared_link(self):
        self.assertLess(2 * stream_mbps(7.68e6), 700)
        c = copy.deepcopy(self.b); c['radio']['profile'] = 'n78_24prb_30khz'
        for x in c['cells']: x['max_rxgain'] = 105; x['ssb_bitmap'] = x['ssb_bitmap']
        with self.assertRaisesRegex(ConfigError, 'exceeds'): validate(c)
        c['host']['links']['second_nic'] = {'mbps': 1000, 'max_utilization': 0.7}
        c['cells'][1]['usrp_link'] = 'second_nic'
        validate(c)                                     # one 1 GbE link per radio is enough for 10 MHz

    def test_tx_gain_cap(self):
        self.b['cells'][0]['att_tx_db'] = 20            # UHD gain 11.5 dB > cap 6
        with self.assertRaisesRegex(ConfigError, 'exceeds safety.max_uhd_tx_gain_db'): validate(self.b)

    def test_ota_cap_ceiling(self):
        self.b['safety']['max_uhd_tx_gain_db'] = 15
        with self.assertRaises(ConfigError): validate(self.b)

    def test_ota_pmax_ceiling(self):
        self.b['safety']['ue_pmax_dbm'] = 15
        with self.assertRaises(ConfigError): validate(self.b)

    def test_rx_gain_clipping_rejected(self):
        self.b['cells'][0]['max_rxgain'] = 120          # 120 - 82 = 38 dB > 31.5 dB UBX maximum
        with self.assertRaisesRegex(ConfigError, 'clip'): validate(self.b)

    def test_iphone_requires_ciphering(self):
        c = load(ROOT / 'tests/fixtures/configs/bench_iphone_f1_manual.yaml')
        c['security']['ciphering'] = ['nea0', 'nea2']
        with self.assertRaisesRegex(ConfigError, 'null ciphering'): validate(c)

    def test_profile_frequencies_and_cal(self):
        f = frequencies('n66_25prb_15khz')
        self.assertAlmostEqual(f['dl_center_hz'], 2152.68e6, places=0)
        self.assertAlmostEqual(f['ul_center_hz'], 1752.68e6, places=0)
        self.assertEqual(frequencies('n78_106prb_30khz')['ssb_start_subcarrier'], 516)   # OAI tutorial: --ssb 516
        self.assertAlmostEqual(frequencies('n78_106prb_30khz')['dl_center_hz'], 3450.72e6, places=0)
        self.assertEqual(x310_rx_cal_offset(1752.68e6), 82.0)
        self.assertEqual(x310_rx_cal_offset(3450.72e6), 77.0)
        g = oai_gains(self.b['cells'][0], 'n66_25prb_15khz')
        self.assertEqual((g['uhd_tx_gain_db'], g['uhd_rx_gain_db'], g['tx_peak_bound_dbm']), (0.5, 28, -11.0))

    def approved_fixture(self):
        c = copy.deepcopy(self.c)
        for k in ['rf_path_reviewed', 'spectrum_authorized', 'clock_and_pps_verified', 'host_time_synchronized', 'gain_mapping_verified']:
            c['safety'][k] = True
        c['safety']['evidence_note'] = 'unit-test synthetic fixture, not a calibration'
        for x in c['cells']:
            x.update(serial='TEST' + x['name'], daughterboard='UBX', tx_gain_min_db=0, tx_gain_max_db=31.5, rx_gain_min_db=0, rx_gain_max_db=31.5)
        for rx in c['safety']['receivers']:
            rx.update(max_input_dbm=-15, margin_db=10, limit_source='synthetic_test')
            for path in rx['paths']:
                path.update(source_peak_dbm=20, minimum_passive_loss_db=60, uncertainty_db=0, evidence='synthetic_test')
        return c

    def test_power_addition_and_no_variable_credit(self):
        c = self.approved_fixture(); r = safety(c)
        self.assertAlmostEqual(r[0]['aggregate_peak_bound_dbm'], round(-40 + 20 * math.log10(3), 2))   # self + other cell + UE
        c['safety']['receivers'][0]['paths'][0]['minimum_passive_loss_db'] = 40
        with self.assertRaises(ConfigError): safety(c)

    def test_missing_self_coupling_path(self):
        c = self.approved_fixture()
        c['safety']['receivers'][0]['paths'] = [p for p in c['safety']['receivers'][0]['paths'] if p['source'] != 'du0']
        with self.assertRaisesRegex(ConfigError, 'self-coupling'): safety(c)

    def test_missing_leakage_path(self):
        c = self.approved_fixture(); c['safety']['receivers'][0]['paths'].pop()
        with self.assertRaises(ConfigError): safety(c)

    def test_bench_budget_passes_with_assumptions(self):
        for r in budget(self.b):
            self.assertLessEqual(r['aggregate_peak_bound_dbm'], r['guarded_limit_dbm'])
        b = copy.deepcopy(self.b)
        for rx in b['safety']['receivers'][:2]:
            for p in rx['paths']:
                if 'fixed_pad_db' in p: p['fixed_pad_db'] = 0.0
        with self.assertRaises(ConfigError): budget(b)  # without the RX2 pads the UE at 1 m is too strong

    def test_interlock(self):
        exp = {'tx_gain_cap_db': 6.0, 'tx_gain_max_db': 31.5, 'tx_freq_hz': 2152.68e6, 'rx_freq_hz': 1752.68e6,
               'sample_rate_sps': 7.68e6}
        ok = Interlock(exp)
        for line in ['[HW]   USRP TX_GAIN:0.50 gain_range:31.50 tx_gain:31.00', '[HW]     Actual TX gain: 0.500000...',
                     '[HW]     Actual TX frequency: 2.152680GHz...', '[HW]     Actual RX frequency: 1.752680GHz...',
                     '[HW]     Actual TX sample rate: 7.680000MSps...', '[HW]     Actual RX sample rate: 7.680000MSps...',
                     '[HW]   Actual master clock: 184.320000MHz...', '[HW]   Actual clock source external...',
                     '[HW]   Actual time source external...']:
            self.assertIsNone(ok.check(line), line)
        self.assertTrue(ok.verified())
        self.assertIn('exceeds cap', Interlock(exp).check('[HW]   USRP TX_GAIN:31.50 gain_range:31.50 tx_gain:0.00'))
        self.assertIn('differs', Interlock(exp).check('[HW]     Actual TX frequency: 3.450720GHz...'))
        self.assertIn('external', Interlock(exp).check('[HW]   Actual clock source internal...'))
        self.assertIn('gain range', Interlock(exp).check('[HW]   USRP TX_GAIN:0.50 gain_range:25.00 tx_gain:24.50'))

    def nrue_bench(self):
        c = copy.deepcopy(self.b)
        c['host']['links']['ue_nic'] = {'mbps': 1000, 'max_utilization': 0.7}
        c['ue']['kind'] = 'oai_nrue'
        c['ue_radio'] = {'usrp_args': 'type=x300,addr=192.168.30.2', 'usrp_link': 'ue_nic', 'daughterboard': 'UBX-160',
                         'subdev': 'B:0', 'tx_gain_max_db': 31.5, 'tx_atten_db': 31, 'rx_gain': 110, 'clock_source': 'external'}
        return c

    def test_hardware_nrue_gain_cap(self):
        c = self.nrue_bench(); validate(c)
        c['ue_radio']['tx_atten_db'] = 0                # OAI's default: full power
        with self.assertRaisesRegex(ConfigError, 'ue_radio: UHD TX gain'): validate(c)
        c = self.nrue_bench(); c['ue_radio']['usrp_link'] = 'enx00e04c680014'
        with self.assertRaisesRegex(ConfigError, 'exceeds'): validate(c)       # a third radio overloads the shared link

    @unittest.skipUnless((CACHE / 'phy_n66_25prb.conf').exists(), 'run oaiho fetch first')
    def test_render_hardware_nrue(self):
        c = self.nrue_bench()
        with tempfile.TemporaryDirectory() as t:
            m = render(c, CACHE, Path(t) / 'u'); verify_manifest(c, Path(t) / 'u')
            argv = m['commands']['ue']
            self.assertEqual(argv[argv.index('--ue-txgain') + 1], '31')
            self.assertIn('--clock-source', argv); self.assertNotIn('--rfsim', argv)
            self.assertEqual(argv[argv.index('--CO') + 1], '-400000000')
            e = m['expected_radio']['ue']
            self.assertEqual((e['tx_freq_hz'], e['rx_freq_hz'], e['uhd_tx_gain_db']), (1752.68e6, 2152.68e6, 0.5))
            lock = Interlock(e)
            self.assertIsNone(lock.check('[HW]     Actual TX frequency: 1.752680GHz...'))
            self.assertIn('differs', lock.check('[HW]     Actual TX frequency: 2.152680GHz...'))
            self.assertIsNone(lock.check('[HW]   Actual time source internal...'))

    def test_single_cell_phase(self):
        c = load(ROOT / 'tests/fixtures/configs/bench_nrue_single_cell.yaml')
        self.assertEqual(c['experiment']['active_cells'], ['du0'])
        self.assertEqual([r['receiver'] for r in budget(c)], ['du0', 'ue'])     # du1 is off and uncabled
        with self.assertRaisesRegex(ConfigError, 'two active cells'): trigger(c, ue_id=1)
        everything = copy.deepcopy(c); del everything['experiment']['active_cells']
        with self.assertRaisesRegex(ConfigError, 'exceeds'): validate(everything)  # three radios on one link
        bad = copy.deepcopy(c); bad['experiment']['active_cells'] = ['du7']
        with self.assertRaises(ConfigError): validate(bad)

    @unittest.skipUnless((CACHE / 'phy_n66_25prb.conf').exists(), 'run oaiho fetch first')
    def test_single_cell_refuses_inactive_node(self):
        from usrp_5g_oai.runtime import run_node
        c = load(ROOT / 'tests/fixtures/configs/bench_nrue_single_cell.yaml')
        with tempfile.TemporaryDirectory() as t:
            render(c, CACHE, Path(t) / 'r')
            self.assertFalse(run_node(c, Path(t) / 'r', 'du0')['execute'])
            with self.assertRaisesRegex(ConfigError, 'active_cells'): run_node(c, Path(t) / 'r', 'du1')
            ue = run_node(c, Path(t) / 'r', 'ue')['argv']
            self.assertEqual(ue[ue.index('--ue-txgain') + 1], '31')
            self.assertIn('type=x300,addr=192.168.10.5,serial=34AF675', ue)

    # ------------------------------------------------------------ control
    def test_manual_command_matches_pinned_oai(self):
        self.assertEqual(trigger(self.c, ue_id=1, target_pci=1)['command'], 'ci trigger_f1_ho 1')
        self.assertEqual(trigger(self.c)['command'], 'ci trigger_f1_ho')

    def test_n2_command(self):
        self.c['topology'] = 'n2'
        self.assertEqual(trigger(self.c, node='du0', ue_id=2, target_pci=1)['command'], 'ci trigger_n2_ho 1,2')

    def test_trace_reproducible(self):
        a = list(trajectory('shadow', seed=4)); b = list(trajectory('shadow', seed=4))
        self.assertEqual(a, b)
        self.assertTrue(all(10 <= x[k] <= 40 for x in a for k in ('du0_db', 'du1_db')))

    # ------------------------------------------------------------ logs and analysis
    def test_log_parser_on_real_lines(self):
        evs = [parse(x)[0] for x in CU_LINES]
        self.assertEqual([e[0] for e in evs], ['rrc_connected', 'ho_trigger', 'ho_command', 'rrc_reconf_complete', 'ho_complete'])
        self.assertEqual(evs[1][1]['pci'], '1'); self.assertEqual(int(evs[0][1]['cellid'], 16), 12345678)
        d = {e: f for line in DU_LINES for e, f in parse(line)}
        self.assertEqual(d['mac_ue']['rsrp'], '-43'); self.assertEqual(d['mac_ue']['pcmax'], '20')
        self.assertEqual(d['mac_ul']['snr'], '22.1'); self.assertEqual(d['mac_dl']['bler'], '0.05905')
        self.assertEqual(d['mac_goodput']['dl'], '0.25')
        self.assertEqual(oai_epoch('1789390399.830368 [NR_RRC] I (rrc:1) x', 5.0), (1789390399.830368, 'oai_wall_clock'))
        self.assertEqual(parse('UUUU')[0], ('uhd_fast_path', {'chars': 'UUUU'}))

    def test_rrc_stats_parser(self):
        text = ('2 connected DUs \n[1] DU ID 3584 (du0) assoc_id 94: 1 cell\n  [1] nrCellID 12345678, PCI 0, Mode FDD, SSB ARFCN 430590\n'
                '[2] DU ID 3585 (du1) assoc_id 96: 1 cell\n  [1] nrCellID 11111111, PCI 1, Mode FDD, SSB ARFCN 430590\n'
                'UE 0 CU UE ID 1 DU UE ID 56465 RNTI dc91 random identity 0000000000000001:\n    last RRC activity: 0 seconds ago\n'
                '    PDU session ID 1 status established\n    associated DU:  DU assoc ID 94\n'
                '    servingCellId 0 MeasResultNR for phyCellId 0:\n      resultSSB: RSRP -80 dBm RSRQ -10.5 dB SINR 20.0 dB\n'
                '    neighboring cell for phyCellId 1:\n      resultSSB: RSRP -95 dBm RSRQ -14.0 dB SINR 3.5 dB\n')
        s = parse_rrc_stats(text)
        self.assertEqual([d['assoc'] for d in s['dus']], [94, 96])
        u = s['ues'][0]
        self.assertEqual((u['ue_id'], u['assoc'], u['serving_pci'], u['pdu'][1]), (1, 94, 0, 'established'))
        self.assertEqual((u['meas'][0]['rsrp'], u['meas'][1]['rsrp'], u['meas'][1]['sinr']), (-80, -95, 3.5))

    def fake_run(self, root):
        cu = root / 'logs' / 'cu'; cu.mkdir(parents=True)
        t = 1789390400.0
        rows = [{'kind': 'process_start', 'epoch_ns': int(t * 1e9)}]
        for k, line in enumerate(CU_LINES):
            ts = t + [0, 5.0, 5.012, 5.07, 5.0701][k]
            rows.append({'kind': 'oai_log', 'epoch_ns': int((ts + 0.002) * 1e9), 'clock_id': 'benchhost', 'line': f'{ts:.6f} {line}'})
        (cu / '1.jsonl').write_text('\n'.join(json.dumps(r) for r in rows) + '\n')
        return t

    def test_extract_events_and_window(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); t = self.fake_run(root)
            r = extract_events(self.b, root, root / 'events.csv')
            self.assertEqual((r['attempts'], r['complete'], r['failure']), (1, 1, 0))
            with (root / 'events.csv').open() as fh:
                ev = list(csv.DictReader(fh))
            self.assertEqual([(e['event'], e['source_pci'], e['target_pci']) for e in ev], [('attempt', '0', '1'), ('complete', '0', '1')])
            det = json.loads((root / 'events_details.json').read_text())[0]
            self.assertAlmostEqual(det['decision_to_command_ms'], 12.0, places=2)
            self.assertAlmostEqual(det['total_ms'], 70.1, places=2)
            pk = root / 'packets.jsonl'
            rows = [{'kind': 'metadata', 'synthetic': False, 'interval_s': 0.02, 'clock_id': 'benchhost'}]
            for i in range(400):
                s = t + 1 + i * 0.02; lost = 200 <= i < 205
                rows.append({'kind': 'packet', 'seq': i, 'send_epoch_ns': int(s * 1e9), 'send_monotonic_ns': int(s * 1e9),
                             'recv_monotonic_ns': None if lost else int((s + 0.01) * 1e9), 'rtt_ms': None if lost else 10.0,
                             'status': 'lost' if lost else 'received', 'send_relative_s': i * 0.02})
            pk.write_text('\n'.join(json.dumps(x) for x in rows) + '\n')
            a = analyze(pk, root / 'events.csv')
            w = a['handover']['details'][0]['traffic_window']
            self.assertEqual(w['lost'], 5); self.assertAlmostEqual(w['max_gap_ms'], 120.0, places=3)

    def test_demo_metrics(self):
        with tempfile.TemporaryDirectory() as t:
            r = demo(Path(t) / 'demo')
            self.assertTrue(r['synthetic']); self.assertEqual(r['traffic']['lost'], 6)
            self.assertEqual(r['handover']['complete'], 1)
            self.assertAlmostEqual(r['handover']['duration_median_ms'], 120, places=3)
            self.assertTrue((Path(t) / 'demo/report/report.html').exists())

    def test_no_completion_inferred_from_packets(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t) / 'demo'; demo(d); r = analyze(d / 'packets.jsonl')
            self.assertEqual(r['handover']['complete'], 0)
            self.assertIsNone(r['handover']['success_fraction_known_outcomes'])

    def test_comparison_rejects_mixed_provenance(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t); d = t / 'demo'; demo(d)
            r = json.loads((d / 'report/summary.json').read_text()); r['synthetic'] = False
            (t / 'measured.json').write_text(json.dumps(r))
            (t / 'comparison.csv').write_text('condition,summary\na,demo/report/summary.json\nb,measured.json\n')
            with self.assertRaises(ConfigError): compare(t / 'comparison.csv', t / 'comparison')

    def test_unmatched_attempt_remains_unknown(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t) / 'demo'; demo(d)
            lines = (d / 'events.csv').read_text().splitlines(); (d / 'events.csv').write_text('\n'.join(lines[:2]) + '\n')
            r = analyze(d / 'packets.jsonl', d / 'events.csv')
            self.assertEqual(r['handover']['unknown'], 1)
            self.assertIsNone(r['handover']['success_fraction_known_outcomes'])

    # ------------------------------------------------------------ dashboard
    def test_dashboard_state_follows_handover(self):
        s = State(self.b, 'test')
        t = 1789390400.0
        s.runner_row('du0', {'kind': 'process_start', 'epoch_ns': int(t * 1e9)})
        for k, line in enumerate(DU_LINES):
            s.runner_row('du0', {'kind': 'oai_log', 'epoch_ns': int((t + 1) * 1e9), 'line': line})
        for k, line in enumerate(CU_LINES):
            s.runner_row('cu', {'kind': 'oai_log', 'epoch_ns': int((t + 2 + k * 0.01) * 1e9), 'line': line})
        snap = s.snapshot()
        self.assertEqual(snap['ue']['serving'], 'du1')
        ho = snap['handovers'][0]
        self.assertEqual((ho['source'], ho['target'], ho['outcome']), ('du0', 'du1', 'complete'))
        self.assertEqual(snap['cells'][0]['ue']['pcmax'], 20)
        self.assertEqual(snap['series']['dl_mbps']['du0'][-1][1], 0.25)
        json.dumps(snap)

    @unittest.skipUnless((EARLIER_RUN / 'events.csv').exists(), 'earlier RFsim run not present')
    def test_replay_timeline_of_earlier_run(self):
        items = replay_timeline(EARLIER_RUN)
        times = [t for t, _, _ in items]
        self.assertEqual(times, sorted(times))
        s = State(load(ROOT / 'tests/fixtures/configs/rfsim_f1.yaml'), 'replay')
        for t, node, row in items:
            if node == 'probe': s.probe_row({**row, 'send_epoch_ns': int(t * 1e9)}, t)
            else: s.runner_row(node, row, t)
        snap = s.snapshot()
        self.assertEqual([h['outcome'] for h in snap['handovers']], ['complete'])
        self.assertEqual(snap['ue']['serving'], 'du1')

    # ------------------------------------------------------------ rendering (needs `oaiho fetch`)
    @unittest.skipUnless((CACHE / 'phy_n66_25prb.conf').exists(), 'run oaiho fetch --checkout ~/openairinterface5g first')
    def test_render_every_config_and_manifest(self):
        with tempfile.TemporaryDirectory() as t:
            for p in sorted((ROOT / 'tests/fixtures/configs').glob('*.yaml')):
                if p.name.startswith('sweep'): continue
                c = load(p); out = Path(t) / p.stem
                if c['ue']['kind'] == 'oai_nrue' and c['mode'] == 'hardware' and c['ue_radio']['tx_atten_db'] is None:
                    with self.assertRaisesRegex(ConfigError, 'FULL power'): render(c, CACHE, out)
                    continue
                m = render(c, CACHE, out); verify_manifest(c, out)
                for name in m['commands']:
                    if name == 'ue': continue
                    d = libconf.loads((out / (name + '.conf')).read_text())
                    if name != 'cu':
                        scc = d['gNBs'][0]['servingCellConfigCommon'][0]
                        self.assertEqual(d['RUs'][0]['nb_tx'], 1)
                        self.assertIsInstance(d['RUs'][0]['att_tx'], int)
                        self.assertEqual(scc['pMax'], c['safety']['ue_pmax_dbm'])
                        self.assertEqual(list(d['RUs'][0]['bands']), [scc['dl_frequencyBand']])
                altered = copy.deepcopy(m); altered['commands']['du0'].append('--unexpected-option')
                (out / 'manifest.json').write_text(json.dumps(altered))
                with self.assertRaises(ConfigError): verify_manifest(c, out)
                (out / 'manifest.json').write_text(json.dumps(m))
                (out / 'du0.conf').write_text('changed')
                with self.assertRaises(ConfigError): verify_manifest(c, out)

    @unittest.skipUnless((CACHE / 'phy_n66_25prb.conf').exists(), 'run oaiho fetch first')
    def test_render_bench(self):
        with tempfile.TemporaryDirectory() as t:
            m = render(self.b, CACHE, Path(t) / 'b')
            du = libconf.loads((Path(t) / 'b/du1.conf').read_text())
            ru, scc = du['RUs'][0], du['gNBs'][0]['servingCellConfigCommon'][0]
            self.assertEqual((ru['att_tx'], ru['max_rxgain'], ru['sdr_addrs'], ru['clock_src'], ru['time_src']),
                             (31, 110, 'type=x300,addr=192.168.20.6,serial=34BA72C', 'external', 'external'))
            self.assertEqual((ru['tx_subdev'], ru['rx_subdev']), ('B:0', 'B:0'))      # UBX sits in slot B on both X310s
            self.assertEqual((scc['absoluteFrequencySSB'], scc['dl_carrierBandwidth'], scc['pMax'], scc['physCellId']), (430590, 25, 0, 1))
            self.assertEqual(scc['preambleReceivedTargetPower'], -100)
            cu = libconf.loads((Path(t) / 'b/cu.conf').read_text())
            self.assertEqual(cu['gNBs'][0]['nr_measurement_configuration']['A3'][0]['physCellId'], 1007)
            self.assertEqual(cu['gNBs'][0]['neighbour_list'][0]['neighbour_cell_configuration'][0]['band'], 66)
            self.assertEqual(m['expected_radio']['du0']['tx_freq_hz'], 2152.68e6)
            self.assertIn('--usrp-tx-thread-config', m['commands']['du0'])
            self.assertNotIn('-E', m['commands']['du0'])

    @unittest.skipUnless((CACHE / 'cu.conf').exists(), 'run oaiho fetch first')
    def test_rfsim_role_mapping(self):
        with tempfile.TemporaryDirectory() as t:
            c = load(ROOT / 'tests/fixtures/configs/rfsim_f1.yaml'); c['rfsim_channel']['enabled'] = True
            out = Path(t) / 'sim'; m = render(c, CACHE, out)
            d = libconf.loads((out / 'du0.conf').read_text())
            self.assertEqual(d['channelmod']['experiment_models'][0]['model_name'], 'rfsimu_channel_enB0')
            self.assertIn('rfsimu_channel_ue1', (out / 'ue.conf').read_text())
            self.assertEqual(m['commands']['ue'][-2:], ['--ssb', '516'])

    # ------------------------------------------------------------ traffic
    def test_udp_loopback(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.bind(('127.0.0.1', 0)); port = s.getsockname()[1]
        p = subprocess.Popen([sys.executable, '-m', 'usrp_5g_oai.cli', 'echo', '--port', str(port)])
        try:
            deadline = time.monotonic() + 5
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.settimeout(0.1)
                while True:
                    s.sendto(b'OAIH' + bytes(8), ('127.0.0.1', port))
                    try:
                        s.recvfrom(64); break
                    except socket.timeout:
                        if time.monotonic() >= deadline: self.fail('echo server did not become ready')
            with tempfile.TemporaryDirectory() as t:
                f = Path(t) / 'packets.jsonl'; probe('127.0.0.1', port, str(f), duration=0.4, interval=0.02, timeout=0.2)
                r = analyze(f); self.assertGreater(r['traffic']['received'], 5); self.assertEqual(r['traffic']['lost'], 0)
        finally:
            p.terminate(); p.wait(timeout=3)

    @unittest.skipUnless(shutil.which('ping'), 'ping missing')
    def test_ping_probe_loopback(self):
        with tempfile.TemporaryDirectory() as t:
            f = Path(t) / 'p.jsonl'; ping_probe('127.0.0.1', str(f), duration=1, interval=0.05)
            r = analyze(f)
            self.assertGreaterEqual(r['traffic']['received'], 15); self.assertEqual(r['traffic']['lost'], 0)
            self.assertEqual(r['probe_method'], 'ping')


if __name__ == '__main__':
    unittest.main()
