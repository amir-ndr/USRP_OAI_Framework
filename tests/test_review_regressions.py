"""Failures found by the source/data-path audit; no radio sessions are opened."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from usrp_5g_oai.analysis import analyze, compare, extract_events, window_metrics
from usrp_5g_oai.config import ConfigError, load, validate, safety
from usrp_5g_oai.dashboard import State
from usrp_5g_oai.runtime import inventory
from usrp_5g_oai.traffic import probe_route, ping_probe

ROOT = Path(__file__).resolve().parents[1]


class ReviewRegressions(unittest.TestCase):
    def setUp(self):
        self.c = load(ROOT / 'tests/fixtures/configs/bench_nrue_f1_manual.yaml')

    def test_ethernet_ue_cannot_bypass_transport_budget(self):
        self.c['ue_radio']['usrp_args'] = 'type=x300,addr=192.168.30.2'
        self.c['ue_radio']['usrp_link'] = None          # an Ethernet radio may not leave its link blank
        with self.assertRaisesRegex(ConfigError, 'Ethernet ue_radio'):
            validate(self.c)
        self.c['ue_radio']['usrp_link'] = 'enx00e04c680014'
        with self.assertRaisesRegex(ConfigError, 'exceeds'):
            validate(self.c)

    def test_inventory_includes_identified_ue(self):
        self.c['ue_radio']['usrp_args'] = 'type=b200,serial=TEST'
        rows = inventory(self.c)
        self.assertEqual(rows[-1]['argv'][-1], 'type=b200,serial=TEST')
        self.assertNotIn('output', rows[-1])

    def test_live_gate_rejects_auto_power_and_placeholder_evidence(self):
        for key in ('rf_path_reviewed', 'spectrum_authorized', 'clock_and_pps_verified',
                    'host_time_synchronized', 'gain_mapping_verified'):
            self.c['safety'][key] = True
        self.c['safety']['evidence_note'] = 'regression fixture only'
        path = self.c['safety']['receivers'][0]['paths'][0]
        with self.assertRaisesRegex(ConfigError, 'planning estimate'):
            safety(self.c)
        path['source_peak_dbm'] = 20
        with self.assertRaisesRegex(ConfigError, 'qualified evidence'):
            safety(self.c)

    def test_dashboard_accepts_ue_console_lines(self):
        s = State(self.c, 'test')
        s.runner_row('ue', {'kind': 'oai_log', 'epoch_ns': 1700000000000000000,
                            'line': '[HW] Actual TX gain: 0.500000...'})
        self.assertEqual(s.snapshot()['ue']['radio']['tx_gain'], '0.500000')

    def test_rollback_is_a_failure_in_events_and_dashboard(self):
        lines = [
            '[NR_RRC] (cellID bc614e, UE ID 1 RNTI dc91) Received RRCSetupComplete',
            '[NR_RRC] Handover triggered for UE 1/RNTI dc91 towards cell 11111111/assoc_id 96/PCI 1',
            '[NR_RRC] handover for UE 1/RNTI dc91 failed, rollback to original cell',
            '[NR_RRC] handover for UE 1/RNTI dc91 complete!',
        ]
        with tempfile.TemporaryDirectory() as t:
            root = Path(t); logs = root / 'logs/cu'; logs.mkdir(parents=True)
            rows = [{'kind': 'oai_log', 'clock_id': 'host', 'epoch_ns': int((1700000000 + i) * 1e9),
                     'line': f'{1700000000 + i:.6f} {line}'} for i, line in enumerate(lines)]
            (logs / '1.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in rows))
            r = extract_events(self.c, root, root / 'events.csv')
            self.assertEqual((r['attempts'], r['failure'], r['complete']), (1, 1, 0))
            s = State(self.c, 'test')
            for row in rows:
                s.runner_row('cu', row)
            snap = s.snapshot()
            self.assertEqual(snap['handovers'][0]['outcome'], 'failed')
            self.assertEqual(snap['ue']['serving'], 'du0')

    def test_n2_needs_identity_mapping_before_event_pairing(self):
        self.c['topology'] = 'n2'
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaisesRegex(ConfigError, 'N2 event pairing is disabled'):
                extract_events(self.c, t, Path(t) / 'events.csv')
            self.assertFalse((Path(t) / 'events.csv').exists())

    def test_missing_window_is_not_zero_loss(self):
        w = window_metrics([], 100, 100.1, .02)
        self.assertEqual(w['coverage'], 'none')
        self.assertIsNone(w['lost'])
        self.assertIsNone(w['max_gap_ms'])

    def test_partial_window_is_excluded_from_aggregate(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            # One probe late in the requested [99, 102.1] interval is partial evidence.
            meta = {'kind': 'metadata', 'interval_s': .02, 'clock_id': 'host', 'synthetic': True}
            pkt = {'kind': 'packet', 'seq': 1, 'status': 'received', 'send_epoch_ns': 102000000000,
                   'send_monotonic_ns': 0, 'recv_monotonic_ns': 10000000, 'rtt_ms': 10}
            p = root / 'p.jsonl'; p.write_text(json.dumps(meta) + '\n' + json.dumps(pkt) + '\n')
            e = root / 'events.csv'
            e.write_text('epoch_s,clock_id,event,ue_id,handover_id,source_pci,target_pci,evidence\n'
                         '100,host,attempt,1,h1,0,1,test\n100.1,host,complete,1,h1,0,1,test\n')
            r = analyze(p, e, self.c)
            self.assertEqual(r['handover']['details'][0]['traffic_window']['coverage'], 'partial')
            self.assertEqual(r['handover']['traffic_windows_complete'], 0)
            self.assertIsNone(r['handover']['window_max_gap_median_ms'])
            self.assertIsNone(r['handover']['window_lost_total'])
            self.assertEqual(r['experiment']['mode'], 'hardware')

    def test_comparison_rejects_rfsim_and_hardware_mix(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            for name in ('hardware', 'rfsim'):
                (root / (name + '.json')).write_text(json.dumps({'synthetic': False, 'experiment': {'mode': name}}))
            m = root / 'm.csv'
            m.write_text('condition,summary\na,hardware.json\nb,rfsim.json\n')
            with self.assertRaisesRegex(ConfigError, 'experiment modes'):
                compare(m, root / 'report')

    def test_timestamp_basis_change_rejects_signaling_duration(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t); logs = root / 'logs/cu'; logs.mkdir(parents=True)
            lines = [
                '1700000000.000000 [NR_RRC] Handover triggered for UE 1/RNTI dc91 towards cell 11111111/assoc_id 96/PCI 1',
                '[NR_RRC] handover for UE 1/RNTI dc91 complete!',
            ]
            rows = [{'kind': 'oai_log', 'clock_id': 'host', 'epoch_ns': int((1700000000 + i) * 1e9), 'line': line}
                    for i, line in enumerate(lines)]
            (logs / '1.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in rows))
            extract_events(self.c, root, root / 'events.csv')
            details = json.loads((root / 'events_details.json').read_text())
            self.assertFalse(details[0]['timing_valid'])
            self.assertIsNone(details[0]['total_ms'])
            packet_file = root / 'packets.jsonl'
            packet_file.write_text(json.dumps({'kind': 'metadata', 'interval_s': .02, 'clock_id': 'host', 'synthetic': True}) + '\n')
            with self.assertRaisesRegex(ConfigError, 'same clock_id'):
                analyze(packet_file, root / 'events.csv')

    def test_overlapping_windows_do_not_double_count_lost_probe(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            rows = [{'kind': 'metadata', 'interval_s': .02, 'clock_id': 'host', 'synthetic': True}]
            for i in range(451):
                ts = round((97 + i * .02) * 1e9); lost = i == 160
                rows.append({'kind': 'packet', 'seq': i, 'status': 'lost' if lost else 'received',
                             'send_epoch_ns': ts, 'send_monotonic_ns': ts,
                             'recv_monotonic_ns': None if lost else ts + 10000000,
                             'rtt_ms': None if lost else 10})
            p = root / 'p.jsonl'; p.write_text(''.join(json.dumps(r) + '\n' for r in rows))
            e = root / 'e.csv'
            e.write_text('epoch_s,clock_id,event,ue_id,handover_id,source_pci,target_pci,evidence\n'
                         '100,host,attempt,1,h1,0,1,test\n100.1,host,complete,1,h1,0,1,test\n'
                         '100.5,host,attempt,1,h2,1,0,test\n100.6,host,complete,1,h2,1,0,test\n')
            r = analyze(p, e)
            self.assertEqual(r['handover']['traffic_windows_complete'], 2)
            self.assertEqual([d['traffic_window']['lost'] for d in r['handover']['details']], [1, 1])
            self.assertEqual(r['handover']['window_lost_total'], 1)

    def test_local_session_ip_is_rejected(self):
        result = subprocess.CompletedProcess([], 0, '[{"type":"local","dev":"lo"}]', '')
        with patch('usrp_5g_oai.traffic.subprocess.run', return_value=result):
            with self.assertRaisesRegex(ConfigError, 'bypasses the radios'):
                probe_route('10.0.0.2')

    def test_route_record_does_not_claim_rf_verification(self):
        result = subprocess.CompletedProcess([], 0, '[{"dev":"oaitun_ue1"}]', '')
        with patch('usrp_5g_oai.traffic.subprocess.run', return_value=result):
            r = probe_route('10.0.0.2')
        self.assertEqual(r['route']['dev'], 'oaitun_ue1')
        self.assertFalse(r['radio_path_verified'])

    def test_failed_ping_cannot_silently_become_empty_measurement(self):
        with tempfile.TemporaryDirectory() as t:
            with patch('usrp_5g_oai.traffic.subprocess.Popen') as launch, \
                    patch('usrp_5g_oai.traffic.shutil.which', return_value='/usr/bin/ping'):
                # Use a closable empty stream, as Popen actually returns.
                import io
                launch.return_value.stdout = io.StringIO('ping: Operation not permitted\n')
                launch.return_value.wait.return_value = 2
                with self.assertRaisesRegex(ConfigError, 'usable probe records'):
                    ping_probe('127.0.0.1', Path(t) / 'p.jsonl', duration=1)


if __name__ == '__main__':
    unittest.main()
