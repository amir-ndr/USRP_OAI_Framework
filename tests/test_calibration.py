"""Experiment 1 tools without a radio: PSS detection, tone level, report and the receive-only guard."""
import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
import numpy as np
from usrp_5g_oai.config import ConfigError, load, validate, budget
from usrp_5g_oai.calibration import report

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / '.cache/oai'


def adapter(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'adapters' / f'{name}.py')
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod


RXM, TONE = adapter('rx_monitor'), adapter('tone_cal')
RATE = 7.68e6


def synthetic_cell(nid2, snr_db, ms=200, period_ms=20, offset_sc=18, seed=1):
    """Noise plus a PSS symbol (with cyclic prefix) every period_ms, as a cell with no UE would send."""
    rng = np.random.default_rng(seed)
    n = int(RATE * ms / 1e3)
    noise = (rng.standard_normal(n) + 1j * rng.standard_normal(n)) / np.sqrt(2) * 1e-3
    t = RXM.pss_template(nid2, 512, offset_sc)
    sym = np.concatenate([t[-36:], t])                    # 36-sample normal CP at 7.68 MS/s
    amp = 1e-3 * 10 ** (snr_db / 20) / np.sqrt(np.mean(np.abs(t) ** 2))
    x = noise.astype(np.complex64)
    for k in range(int(ms // period_ms)):
        i = int(RATE * (k * period_ms + 3.1) / 1e3)
        x[i:i + len(sym)] += amp * sym
    return x


class Exp1Tests(unittest.TestCase):
    def test_pss_sequence_is_38211(self):
        d0 = RXM.pss_sequence(0)
        self.assertEqual(len(d0), 127)
        self.assertEqual(d0[:8], [1, -1, -1, 1, -1, -1, -1, -1])     # 1 - 2x(m), x = 0,1,1,0,1,1,1,1,...
        self.assertEqual(RXM.pss_sequence(1)[0], RXM.pss_sequence(0)[43])

    def test_pss_detects_identity_and_period(self):
        for nid2 in (0, 1, 2):
            r = RXM.analyze(synthetic_cell(nid2, 10), RATE, 18, expected_nid2=0)
            self.assertTrue(r['pss_detected'])
            self.assertEqual(r['pss_nid2'], nid2)
            self.assertAlmostEqual(r['pss_period_ms'], 20.0, delta=0.1)
            self.assertEqual(r['nid2_matches'], nid2 == 0)
            self.assertGreater(r['burst_above_floor_db'], 3)

    def test_noise_only_is_not_a_cell(self):
        rng = np.random.default_rng(3)
        x = ((rng.standard_normal(int(RATE * 0.2)) + 1j * rng.standard_normal(int(RATE * 0.2))) * 1e-3).astype(np.complex64)
        r = RXM.analyze(x, RATE, 18)
        self.assertFalse(r['pss_detected']); self.assertLess(r['pss_rho'], 0.25)

    def test_tone_level(self):
        t = np.arange(int(RATE * 0.3)) / RATE
        x = (0.01 * np.exp(2j * np.pi * 210e3 * t)).astype(np.complex64)   # -40 dBFS tone, bin-centred
        level, floor = TONE.tone_level(x, RATE, 210e3)
        self.assertAlmostEqual(level, -40.0, delta=0.5)
        self.assertLess(floor, -100)

    def test_receive_only_guard_and_budget(self):
        c = load(ROOT / 'tests/fixtures/configs/receive_only_n66.yaml')
        self.assertTrue(c['experiment']['ue_receive_only'])
        with self.assertRaises(ConfigError):
            budget(c)                                   # self-isolation still to be measured in step A
        c2 = copy.deepcopy(c)
        c2['safety']['receivers'][0]['paths'][0]['minimum_passive_loss_db'] = 55.0
        r = {x['receiver']: x for x in budget(c2)}
        self.assertLessEqual(r['ue']['aggregate_peak_bound_dbm'], -25)   # +20 dBm through 58 dB, no software credit
        self.assertLessEqual(r['du0']['aggregate_peak_bound_dbm'], -25)

    @unittest.skipUnless((CACHE / 'phy_n66_25prb.conf').exists(), 'run oaiho fetch first')
    def test_run_node_refuses_ue_in_experiment1(self):
        from usrp_5g_oai.oai import render
        from usrp_5g_oai.runtime import run_node
        c = load(ROOT / 'tests/fixtures/configs/receive_only_n66.yaml')
        with tempfile.TemporaryDirectory() as t:
            render(c, CACHE, Path(t) / 'r')
            with self.assertRaisesRegex(ConfigError, 'receive-only'):
                run_node(c, Path(t) / 'r', 'ue')
            self.assertFalse(run_node(c, Path(t) / 'r', 'du0')['execute'])

    def fake_step(self, root, att, level):
        c = load(ROOT / 'tests/fixtures/configs/receive_only_n66.yaml'); c['cells'][0]['att_tx_db'] = att
        d = root / f'B_att{att}'; (d / 'logs' / 'du0').mkdir(parents=True)
        (d / 'experiment.json').write_text(json.dumps(c))
        rows = [{'kind': 'rxmon_start', 'gain_db': 28, 'ref_locked': True, 'args': 'x', 'freq_hz': 2152.68e6}]
        for i in range(20):
            rows.append({'kind': 'rxmon', 'epoch': 1e9 + i, 'stream_errors': 0, 'burst_dbfs': level, 'floor_dbfs': -70.0,
                         'burst_above_floor_db': level + 70, 'peak_dbfs': level + 8, 'headroom_db': -(level + 8),
                         'pss_detected': True, 'pss_nid2': 0, 'pss_rho': 0.8, 'pss_period_ms': 20.0,
                         'envelope_db': [-70.0] * 50, 'envelope_step_ms': 0.25})
        (d / 'rxmon.jsonl').write_text('\n'.join(json.dumps(r) for r in rows) + '\n')
        logs = [{'kind': 'process_start', 'epoch_ns': 0}, {'kind': 'radio_verified', 'epoch_ns': 1},
                {'kind': 'oai_log', 'epoch_ns': 2, 'line': 'UU'}, {'kind': 'process_exit', 'epoch_ns': int(60e9)}]
        (d / 'logs' / 'du0' / '1.jsonl').write_text('\n'.join(json.dumps(r) for r in logs) + '\n')
        return d

    def test_report_interprets_linear_steps(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            steps = [self.fake_step(t, 31, -55.0), self.fake_step(t, 28, -52.0), self.fake_step(t, 26, -50.0)]
            cal = t / 'cal.json'
            cal.write_text(json.dumps({'derived': {'self_isolation_db': 62.0, 'cable_path_loss_assumed_db': 58.0}}))
            s = report(steps, t / 'rep', cal)
            checks = {x['title']: x['status'] for x in s['checks']}
            self.assertEqual(checks['The cell is on the air'], 'PASS')
            self.assertEqual(checks['TX gain changes the output as configured'], 'PASS')
            self.assertAlmostEqual(s['slope_db_per_db'], 1.0, places=2)
            self.assertEqual(checks['Radio transport'], 'CHECK')            # two underflows in the fake log
            self.assertEqual(checks["Cell's own TX -> RX2 isolation"], 'PASS')
            self.assertTrue((t / 'rep' / 'report.html').exists())


if __name__ == '__main__':
    unittest.main()
