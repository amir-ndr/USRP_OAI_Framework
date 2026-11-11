"""Behavioral regressions for the unified lab workflow; no USRP is opened."""
import copy
import contextlib
import csv
import io
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import yaml
from usrp_5g_oai.config import load, validate, ConfigError, check_licence, qualified_tx_peaks, eirp_bound
from usrp_5g_oai.session import effective_config, plan, findings, session, stop_owned_core
from usrp_5g_oai.runtime import rx_check, rx_monitor, clock_check
from usrp_5g_oai.analysis import demo
from usrp_5g_oai.artifacts import export_run, radio_rows
from usrp_5g_oai.cli import main

ROOT = Path(__file__).resolve().parents[1]
CFG = ROOT / 'configs/lab_n78.yaml'


class UnifiedWorkflow(unittest.TestCase):
    def setUp(self):
        self.c = load(CFG)

    def test_current_inventory_and_low_gain(self):
        self.assertEqual(self.c['experiment']['active_cells'], ['du0'])
        self.assertEqual(self.c['safety']['licence']['blocks_mhz'], [[3300, 3800]])
        self.assertEqual(self.c['safety']['licence']['max_eirp_dbm'], 20)
        self.assertEqual(self.c['safety']['max_uhd_tx_gain_db'], .5)
        self.assertEqual(self.c['ue_radio']['tx_atten_db'], 31.5)
        with self.assertRaises(ConfigError):
            validate(self.c, live=True)

    def test_rfsim_cli_override_precedes_hardware_transport_validation(self):
        c = copy.deepcopy(self.c)
        c['ue_radio']['usrp_link'] = c['cells'][0]['usrp_link']
        with self.assertRaisesRegex(ConfigError, 'exceeds'):
            validate(c)
        with tempfile.TemporaryDirectory() as t:
            cfg = Path(t) / 'shared.yaml'; cfg.write_text(yaml.safe_dump(c))
            output = io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
                main(['session', str(cfg), '--set', 'mode=rfsim', '--out', str(Path(t)/'preview')])
            self.assertEqual(json.loads(output.getvalue())['mode'], 'rfsim')
            self.assertFalse((Path(t)/'preview').exists())

    def test_analyze_cli_accepts_saved_config_without_set_option(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t); demo(root/'input')
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                main(['analyze', '--packets', str(root/'input/packets.jsonl'), '--events', str(root/'input/events.csv'),
                      '--config', str(CFG), '--out', str(root/'analysis')])
            self.assertTrue((root/'analysis/packets.csv').exists())

    def test_nominal_channel_must_fit_not_only_resource_grid(self):
        self.c['safety']['licence']['blocks_mhz'] = [[3600.3, 3609.3]]
        with self.assertRaisesRegex(ConfigError, 'outside the licensed blocks'):
            check_licence(self.c)

    def test_rfsim_does_not_need_physical_licence_for_simulated_frequency(self):
        c = effective_config(self.c, ['mode=rfsim', 'radio.profile=n66_25prb_15khz', 'radio.dl_center_mhz=null'])
        self.assertEqual(c['safety']['licence'], self.c['safety']['licence'])
        validate(c, live=True)

    def test_receive_check_active_cell_and_ue_full_sample_rate(self):
        rows = rx_check(self.c)['runs']
        self.assertEqual([r['cell'] for r in rows], ['du0', 'ue'])
        for r in rows:
            self.assertEqual(float(r['argv'][r['argv'].index('--rate') + 1]), 15.36e6)

    def test_monitor_preview_does_not_open_hardware_and_carries_numerology(self):
        with patch('usrp_5g_oai.runtime.subprocess.Popen') as popen:
            p = rx_monitor(self.c, '/tmp/unused-rxmon.jsonl')
        popen.assert_not_called()
        self.assertEqual(p['argv'][p['argv'].index('--scs-khz') + 1], '30')

    def test_clock_preview_checks_ue_reference_without_pps_or_inactive_du(self):
        rows = clock_check(self.c)['checks']
        cmds = [r['argv'] for r in rows]
        self.assertTrue(any('--reference-only' in a and self.c['ue_radio']['usrp_args'] in a for a in cmds))
        self.assertFalse(any('--pair' in a or '192.168.20.6' in ','.join(a) for a in cmds))

    def test_du0_ue_du1_order_and_single_cell_trigger_rejection(self):
        c = effective_config(self.c, ['mode=rfsim'], ['du0', 'du1'])
        self.assertEqual(plan(c, 60, [20, 40], '192.168.70.135')['start_order'],
                         ['core', 'dashboard', 'cu', 'du0', 'ue', 'du1', 'probe'])
        with self.assertRaisesRegex(ConfigError, 'two active cells'):
            plan(self.c, 60, [20], '192.168.70.135')
        c['topology'] = 'n2'
        with self.assertRaisesRegex(ConfigError, 'F1 only'):
            plan(c, 60, [], '192.168.70.135')

    def test_power_envelope_rejects_gain_or_frequency_outside_calibration(self):
        c = copy.deepcopy(self.c)
        for b in c['safety']['tx_power_bounds'].values():
            b.update(peak_dbm=-10, antenna_gain_dbi=3, evidence='synthetic test envelope')
        self.assertEqual(set(qualified_tx_peaks(c, live=True)), {'du0', 'ue'})
        c['safety']['max_uhd_tx_gain_db'] = 1
        with self.assertRaisesRegex(ConfigError, 'gain cap'):
            qualified_tx_peaks(c, live=True)
        c['safety']['max_uhd_tx_gain_db'] = .5
        c['radio']['dl_center_mhz'] += 1.44
        with self.assertRaisesRegex(ConfigError, 'does not cover this channel'):
            qualified_tx_peaks(c, live=True)

    def test_failed_probe_and_rejected_trigger_cannot_pass(self):
        c = effective_config(self.c, ['mode=rfsim'])
        info = {'ue_expected': True, 'ue_ip': '10.0.0.2', 'attach_s': 2, 'probe_complete': False,
                'plan': {'handover_triggers_s': [20]}, 'triggers': [{'error': 'refused'}], 'triggers_sent': 0}
        analysis = {'traffic': {'sent_records': 1, 'received': 1, 'lost': 0, 'loss_fraction': 0}, 'handover': {}}
        results = {r['title']: r['status'] for r in findings(c, info, analysis, [])}
        self.assertEqual(results['Traffic through the radio link'], 'CHECK')
        self.assertEqual(results['Manual handovers'], 'FAIL')

    def test_eirp_keeps_antenna_gain_when_geometry_is_replaced(self):
        c = copy.deepcopy(self.c)
        for name, peak, gain in (('du0', -10, 3), ('ue', -20, 8)):
            c['safety']['tx_power_bounds'][name].update(peak_dbm=peak, antenna_gain_dbi=gain, evidence='synthetic envelope')
        for rx in c['safety']['receivers']:
            for p in rx['paths']:
                p.pop('ota')
                p['minimum_passive_loss_db'] = 60
        b = eirp_bound(c)
        self.assertTrue(b['qualified'])
        self.assertEqual(b['peak_eirp_bound_dbm'], -7)

    def test_complete_process_without_full_probe_span_is_not_a_pass(self):
        c = effective_config(self.c, ['mode=rfsim'])
        info = {'ue_expected': True, 'ue_ip': '10.0.0.2', 'attach_s': 2, 'probe_complete': True,
                'plan': {'duration_s': 60}, 'triggers_sent': 0}
        analysis = {'traffic': {'sent_records': 1, 'received': 1, 'lost': 0, 'observed_send_span_s': 0}, 'handover': {}}
        f = {x['title']: x['status'] for x in findings(c, info, analysis, [])}
        self.assertEqual(f['Traffic through the radio link'], 'CHECK')

    def test_startup_failure_still_writes_summary_csv_and_png(self):
        c = effective_config(self.c, ['mode=rfsim'])
        with tempfile.TemporaryDirectory() as t, patch('usrp_5g_oai.session.oai_pids', return_value=[]), \
             patch('usrp_5g_oai.session.compose_running', return_value=set()), \
             patch('usrp_5g_oai.session.core_up', side_effect=ConfigError('injected core failure')):
            run = Path(t) / 'run'
            result = session(c, run, cache=ROOT / '.cache/oai', duration=10, execute=True)
            self.assertIn('injected core failure', result['abort'])
            for name in ('summary.md', 'summary.json', 'events.csv', 'checks.csv', 'packets.csv', 'handover.png', 'radio_metrics.png'):
                self.assertTrue((run / name).is_file(), name)

    def test_core_cleanup_targets_only_explicit_owned_names(self):
        with patch('usrp_5g_oai.session.subprocess.run') as run:
            run.return_value.returncode = 0
            stop_owned_core({'oai-amf', 'oai-upf'})
            argv = run.call_args.args[0]
            self.assertEqual(argv, ['docker', 'stop', '--time', '20', 'oai-amf', 'oai-upf'])
            self.assertNotIn('down', argv)

    def test_csv_preserves_packets_and_handover_metrics(self):
        with tempfile.TemporaryDirectory() as t:
            out = Path(t) / 'demo'
            demo(out)
            packets = list(csv.DictReader((out / 'report/packets.csv').read_text().splitlines()))
            self.assertEqual(len(packets), 250)
            self.assertEqual(sum(r['status'] == 'lost' for r in packets), 6)
            rows = list(csv.DictReader((out / 'report/handover_metrics.csv').read_text().splitlines()))
            self.assertEqual(rows[0]['outcome'], 'complete')
            self.assertAlmostEqual(float(rows[0]['signaling_duration_ms']), 120, places=2)

    def test_radio_csv_distinguishes_dbfs_from_dbm(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / 'logs/du0'; p.mkdir(parents=True)
            row = {'kind': 'oai_log', 'epoch_ns': 1700000000000000000,
                   'line': '1700000000.000000 [NR_MAC] UE dc91: ulsch_rounds 44/0/0/0, ulsch_errors 0, ulsch_DTX 0, BLER 0.03 MCS (0) 0 SNR 22.1 RSSI -41.9'}
            (p / '1.jsonl').write_text(json.dumps(row) + '\n')
            measured = {r['metric']: (r['value'], r['unit']) for r in radio_rows(t)}
            self.assertEqual(measured['ul_rssi_dbfs'], (-41.9, 'dBFS'))
            self.assertEqual(measured['ul_snr_db'], (22.1, 'dB'))

    def test_pss_detection_works_with_30khz_spacing(self):
        spec = importlib.util.spec_from_file_location('rxm', ROOT / 'adapters/rx_monitor.py')
        rxm = importlib.util.module_from_spec(spec); spec.loader.exec_module(rxm)
        rate = 15.36e6
        rng = np.random.default_rng(7)
        x = ((rng.standard_normal(int(rate*.08)) + 1j*rng.standard_normal(int(rate*.08))) * .001).astype(np.complex64)
        symbol = rxm.pss_template(1, 512, 24) * .1
        for ms in (3, 23, 43, 63):
            start = int(ms * rate / 1000)
            x[start:start+512] += symbol
        r = rxm.analyze(x, rate, 24, scs_khz=30)
        self.assertTrue(r['pss_detected'])
        self.assertEqual(r['pss_nid2'], 1)
        self.assertAlmostEqual(r['pss_period_ms'], 20, delta=.1)


if __name__ == '__main__':
    unittest.main()
