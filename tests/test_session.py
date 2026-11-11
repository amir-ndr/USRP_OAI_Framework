"""Experiment 2 (over the air, n78, three X310s) and the session runner, without radios."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from usrp_5g_oai.config import ConfigError, load, validate, budget, profile, eirp_bound, ota_loss
from usrp_5g_oai.profiles import shifted, frequencies, PROFILES
from usrp_5g_oai.session import effective_config, schedule, plan, findings

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / '.cache/oai'
EXP = ROOT / 'tests/fixtures/configs/ota_n78_legacy.yaml'


def live_ready(c, block=(3550.0, 3620.0), eirp=10.0):
    """The operator's step-3 edits, done in a copy."""
    c = copy.deepcopy(c)
    s = c['safety']
    s['licence'] = {'id': 'DEV-TEST-1', 'blocks_mhz': [list(block)], 'max_eirp_dbm': eirp}
    for k in ('rf_path_reviewed', 'spectrum_authorized', 'host_time_synchronized', 'gain_mapping_verified'):
        s[k] = True
    s['evidence_note'] = 'test'
    s['tx_power_bounds'] = {name: {'peak_dbm': -5.5, 'maximum_gain_db': 6.0, 'antenna_gain_dbi': 3.0, 'profile': c['radio']['profile'],
                               'frequency_range_mhz': [3300, 3800], 'evidence': 'synthetic test envelope'}
                            for name in ('du0', 'du1', 'ue')}
    for rx in s['receivers']:
        for p in rx['paths']:
            p['minimum_passive_loss_db'] = ota_loss(c, rx['name'], p)
            del p['ota']
            p['evidence'] = 'synthetic qualified coupling fixture'
    return c


class CarrierShift(unittest.TestCase):
    def test_n78_shift_keeps_offsets_and_raster(self):
        p = shifted('n78_24prb_30khz', 3561.6e6)
        self.assertEqual(p['shift_steps'], -30)
        self.assertEqual((p['ssb_arfcn'], p['dl_point_a_arfcn']), (640320 - 2880, 640032 - 2880))
        f, f0 = frequencies(p), frequencies('n78_24prb_30khz')
        self.assertEqual(f['ssb_start_subcarrier'], f0['ssb_start_subcarrier'])   # nrUE --ssb unchanged
        self.assertAlmostEqual((f['ssb_hz'] - 3000e6) / 1.44e6 % 1, 0.0)            # on the GSCN raster

    def test_n66_shift_keeps_duplex_spacing(self):
        f = frequencies(shifted('n66_25prb_15khz', (2152.68 + 6.0) * 1e6))
        self.assertAlmostEqual(f['dl_center_hz'] - f['ul_center_hz'], 400e6)

    def test_refuses_off_raster_and_out_of_band(self):
        with self.assertRaisesRegex(ValueError, 'nearest valid'):
            shifted('n78_24prb_30khz', 3600.0e6)
        with self.assertRaisesRegex(ValueError, 'leaves band n78'):
            shifted('n78_24prb_30khz', (3604.8 + 1.44 * 140) * 1e6)

    def test_config_profile_follows_dl_center(self):
        c = load(EXP)
        self.assertEqual(profile(c)['shift_steps'], 0)
        c['radio']['dl_center_mhz'] = 3561.6
        validate(c)
        self.assertEqual(profile(c)['ssb_arfcn'], 637440)
        self.assertEqual(PROFILES['n78_24prb_30khz']['ssb_arfcn'], 640320)          # registry untouched

    @unittest.skipUnless((CACHE / 'phy_n78_24prb.conf').exists(), 'run oaiho fetch first')
    def test_render_applies_shift_everywhere(self):
        from usrp_5g_oai.oai import render
        c = load(EXP); c['radio']['dl_center_mhz'] = 3561.6
        with tempfile.TemporaryDirectory() as t:
            m = render(c, CACHE, Path(t) / 'r')
            du = (Path(t) / 'r' / 'du0.conf').read_text()
            self.assertIn('absoluteFrequencySSB = 637440', du.replace('  ', ' '))
            self.assertIn('dl_absoluteFrequencyPointA = 637152', du.replace('  ', ' '))
            ue = m['commands']['ue']
            self.assertEqual(ue[ue.index('-C') + 1], '3561600000')
            self.assertEqual(ue[ue.index('--ssb') + 1], '24')
            self.assertAlmostEqual(m['expected_radio']['du0']['tx_freq_hz'], 3561.6e6)
            self.assertAlmostEqual(m['expected_radio']['ue']['rx_freq_hz'], 3561.6e6)


class LicenceGate(unittest.TestCase):
    def test_ota_live_needs_licence_and_passes_when_filled(self):
        c = load(EXP)
        with self.assertRaisesRegex(ConfigError, 'licence'):
            validate(c, live=True)
        validate(live_ready(c), live=True)

    def test_carrier_outside_block_refused(self):
        c = live_ready(load(EXP), block=(3700.0, 3800.0))
        with self.assertRaisesRegex(ConfigError, 'outside the licensed blocks'):
            validate(c)
        c['radio']['dl_center_mhz'] = 3604.8 + 1.44 * 70          # 3705.6 MHz: inside
        validate(c, live=True)

    def test_eirp_bound_against_licence_limit(self):
        c = load(EXP)
        self.assertEqual(eirp_bound(c)['peak_eirp_bound_dbm'], -2.5)  # -5.5 dBm at the 6 dB cap + 3 dBi
        with self.assertRaisesRegex(ConfigError, 'EIRP'):
            validate(live_ready(c, eirp=-10.0), live=True)

    def test_ota_budget_passes_only_with_separated_rx2_antennas(self):
        c = load(EXP)
        self.assertTrue(all(r['aggregate_peak_bound_dbm'] <= -25 for r in budget(c)))
        c['safety']['receivers'][0]['paths'][0]['ota']['distance_m'] = 0.2     # RX2 antenna next to TX/RX
        with self.assertRaises(ConfigError):
            budget(c)


class SessionPlanning(unittest.TestCase):
    def test_set_overrides(self):
        c = load(EXP)
        e = effective_config(c, ['mode=rfsim', 'cells.du0.att_tx_db=28', 'radio.dl_center_mhz=3561.6'], ['du0'])
        self.assertEqual((e['mode'], e['cells'][0]['att_tx_db'], e['experiment']['active_cells']), ('rfsim', 28, ['du0']))
        self.assertEqual(c['mode'], 'hardware')                                    # base untouched
        with self.assertRaises(ConfigError):
            effective_config(c, ['radio.no_such_field=1'])
        with self.assertRaises(ConfigError):
            effective_config(c, ['cells.du0.att_tx_db=20'])                       # 11.5 dB > 6 dB cap

    def test_schedule(self):
        self.assertEqual(schedule(120, 20), [20, 40, 60, 80, 100])
        self.assertEqual(schedule(120, 20, count=2, first=30), [30, 50])
        self.assertEqual(schedule(120, None), [])
        with self.assertRaises(ConfigError):
            schedule(60, 2)

    def test_plan_orders_nodes_and_gates(self):
        c = load(EXP)
        p = plan(effective_config(c, cells=['du0']), 60, [], '192.168.70.135')
        self.assertEqual(p['start_order'], ['core', 'dashboard', 'cu', 'du0', 'ue', 'probe'])
        self.assertTrue(p['live_gate'].startswith('BLOCKED'))
        p = plan(effective_config(c, ['mode=rfsim']), 60, schedule(60, 20), '192.168.70.135')
        self.assertEqual(p['handover_triggers_s'], [20, 40]); self.assertEqual(p['live_gate'], 'not needed (RFsim)')

    def test_findings_language(self):
        c = load(EXP)
        info = {'ue_expected': True, 'ue_ip': '10.0.0.2', 'attach_s': 8.2, 'triggers_sent': 3}
        analysis = {'traffic': {'sent_records': 100, 'received': 99, 'lost': 1, 'loss_fraction': 0.01, 'rtt_median_ms': 9.0,
                                'rtt_p95_ms': 20.0, 'max_interarrival_gap_ms': 80.0},
                    'handover': {'complete': 2, 'duration_median_ms': 30.0, 'window_max_gap_median_ms': 70.0}}
        nodes = [{'node': 'du0', 'U': 0, 'L': 2, 'O': 0, 'D': 0, 'S': 0, 'uhd_errors': 0, 'interlock': None, 'radio_verified': True}]
        F = {x['title']: x['status'] for x in findings(c, info, analysis, nodes)}
        self.assertEqual(F['UE attached'], 'PASS')
        self.assertEqual(F['Traffic through the radio link'], 'CHECK')
        self.assertEqual(F['Manual handovers'], 'CHECK')
        self.assertEqual(F['Radio transport (USB/Ethernet to the X310s)'], 'CHECK')
        self.assertEqual(F['Radio interlock'], 'PASS')


if __name__ == '__main__':
    unittest.main()


class ReplayAndCompare(unittest.TestCase):
    def test_replay_keeps_recorded_order_and_kinds(self):
        from usrp_5g_oai.dashboard import replay_run_timeline
        with tempfile.TemporaryDirectory() as t:
            run = Path(t); (run / 'logs' / 'cu').mkdir(parents=True); (run / 'manifest.json').write_text('{}')
            (run / 'logs' / 'cu' / '1.jsonl').write_text(json.dumps({'kind': 'oai_log', 'epoch_ns': 2_000_000_000, 'line': 'x'}) + '\n')
            (run / 'packets.jsonl').write_text(json.dumps({'kind': 'metadata', 'epoch_ns': 1}) + '\n' +
                                               json.dumps({'kind': 'packet', 'send_epoch_ns': 1_000_000_000, 'rtt_ms': 10.0}) + '\n')
            (run / 'trigger.jsonl').write_text(json.dumps({'kind': 'trigger_sent', 'epoch_ns': 3_000_000_000}) + '\n')
            items = replay_run_timeline(run)
            self.assertEqual([n for _, n, _ in items], ['probe', 'cu', 'trigger'])
            self.assertAlmostEqual(items[0][0], 1.01)                    # probe shown at its reply time

    def test_compare_mixed_modes_only_when_asked(self):
        from usrp_5g_oai.analysis import compare
        base = {'synthetic': False, 'traffic': {'loss_fraction': 0.0, 'rtt_p95_ms': 9.0, 'max_interarrival_gap_ms': 40.0},
                'handover': {'duration_median_ms': 38.0, 'window_max_gap_median_ms': 38.0, 'attempts': 4, 'complete': 4,
                             'failed': 0, 'unknown': 0}}
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            for name, mode in (('a.json', 'rfsim'), ('b.json', 'hardware')):
                (t / name).write_text(json.dumps({**base, 'experiment': {'mode': mode}}))
            (t / 'm.csv').write_text('condition,summary\nrfsim,a.json\nota,b.json\n')
            with self.assertRaises(ConfigError):
                compare(t / 'm.csv', t / 'out1')
            rows = compare(t / 'm.csv', t / 'out2', mixed_modes=True)
            self.assertEqual({r['condition']: r['mode'] for r in rows}, {'rfsim': 'rfsim', 'ota': 'hardware'})
