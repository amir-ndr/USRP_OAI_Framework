"""Live (or replayed) view of the two-cell bench, served on 127.0.0.1 only.

Sources, all read-only: the runner's per-node JSONL logs (RUN/logs/<node>/*.jsonl), the CU's
nrRRC_stats.log, an optional probe file and RUN/trigger.jsonl. Nothing here talks to a radio.
"""
import csv
import json
import threading
import time
from collections import deque
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from importlib.resources import files
from pathlib import Path
from .oailog import parse, clean, oai_epoch, parse_rrc_stats
from .profiles import PROFILES, frequencies, oai_gains
from .config import profile, active_cells

HISTORY_S = 600
STATUS_ORDER = ['offline', 'starting', 'radio_config', 'radio_verified', 'f1_connected', 'serving']


class State:
    def __init__(self, c, source):
        self.lock = threading.Lock()
        self.c = c
        prof = profile(c)                  # carrier actually used (radio.dl_center_mhz applied)
        f = frequencies(prof)
        self.meta = {'experiment': c['experiment']['name'], 'mode': c['mode'], 'topology': c['topology'],
                     'trigger': c['handover']['trigger'], 'profile': prof['name'], 'source': source,
                     'sample_rate_msps': prof['sample_rate_sps'] / 1e6,
                     'dl_mhz': round(f['dl_center_hz'] / 1e6, 3), 'ul_mhz': round(f['ul_center_hz'] / 1e6, 3),
                     'duplex': prof['duplex'], 'band': prof['band'], 'prb': prof['prb'],
                     'ue_pmax_dbm': c['safety']['ue_pmax_dbm'], 'tx_gain_cap_db': c['safety']['max_uhd_tx_gain_db'],
                     'ue_kind': c['ue']['kind'], 'active_cells': [x['name'] for x in active_cells(c)], 'allow_trigger': False}
        self.cells = {}
        for i, x in enumerate(c['cells']):
            g = oai_gains(x, prof)
            self.cells[x['name']] = {'name': x['name'], 'slot': i + 1, 'pci': x['pci'], 'du_id': x['du_id'],
                                     'nr_cellid': x['nr_cellid'], 'usrp_ip': x['usrp_ip'], 'daughterboard': x['daughterboard'],
                                     'expected_tx_gain_db': g['uhd_tx_gain_db'], 'expected_rx_gain_db': g['uhd_rx_gain_db'],
                                     'status': 'offline' if x in active_cells(c) else 'inactive', 'hw': {}, 'uhd': {'U': 0, 'O': 0, 'L': 0, 'S': 0, 'D': 0, 'errors': 0},
                                     'interlock': None, 'ue': None, 'last_line': None}
        self.by_pci = {x['pci']: x['name'] for x in c['cells']}
        self.by_nci = {x['nr_cellid']: x['name'] for x in c['cells']}
        self.by_du = {x['du_id']: x['name'] for x in c['cells']}
        self.cu = {'status': 'offline', 'ngap': False}
        self.ue = {'ue_id': None, 'rnti': None, 'serving': None, 'pdu': None, 'activity_s': None, 'ho_active': None}
        self.series = {k: {x['name']: deque() for x in c['cells']} for k in ('rsrp', 'l1_rsrp', 'dl_mbps', 'ul_snr', 'ul_rssi')}
        self.rtt = deque()
        self.handovers = []
        self.feed = deque(maxlen=150)
        self.last_a3 = {}
        self.manual = deque(maxlen=50)
        self.now = None
        self.rxmon = deque()          # receive-only measurement receiver (experiment 1)
        self.rx_latest = None
        self.du_assoc = {}

    # ------------------------------------------------------------------ helpers
    def _t(self, t):
        self.now = t if self.now is None else max(self.now, t)
        return t

    def _push(self, series, name, t, v):
        q = self.series[series][name]
        q.append((t, v))
        while q and q[0][0] < t - HISTORY_S:
            q.popleft()

    def _feed(self, t, level, node, text):
        self.feed.append({'t': t, 'level': level, 'node': node, 'text': text})

    def _bump(self, name, status):
        cell = self.cells[name]
        if STATUS_ORDER.index(status) > STATUS_ORDER.index(cell['status']):
            cell['status'] = status

    def _cell_of_cellid_hex(self, h):
        return self.by_nci.get(int(h, 16))

    def _serve(self, name, t):
        if self.ue['serving'] != name:
            self.ue['serving'] = name
        for n, cell in self.cells.items():
            if cell['status'] == 'serving' and n != name:
                cell['status'] = 'f1_connected'
        if name:
            self._bump(name, 'serving')

    # ------------------------------------------------------------------ inputs
    def runner_row(self, node, row, t=None):
        """One JSONL row written by run_node()."""
        kind = row.get('kind')
        rx = row.get('epoch_ns', time.time_ns()) / 1e9 if t is None else t
        with self.lock:
            if kind == 'oai_log':
                self._line(node, row['line'], rx, replay_time=t is not None)
            elif node == 'ue' and kind in ('process_start', 'process_exit', 'interlock_abort', 'radio_verified', 'stop_requested'):
                self._t(rx)               # the OAI UE (third USRP): lifecycle and interlock only
                text = {'process_start': 'OAI UE started', 'process_exit': f"OAI UE exited (code {row.get('returncode')})",
                        'interlock_abort': 'INTERLOCK stopped the UE radio: ' + str(row.get('reason')),
                        'radio_verified': 'UE radio settings verified against the rendered config',
                        'stop_requested': 'UE stop requested'}[kind]
                self._feed(rx, {'interlock_abort': 'critical', 'radio_verified': 'good'}.get(kind, 'info'), 'ue', text)
            elif kind == 'process_start':
                self._t(rx)
                if node == 'cu':
                    self.cu.update(status='starting', ngap=False)
                else:
                    self.cells[node].update(status='starting', hw={}, interlock=None, ue=None,
                                            uhd={'U': 0, 'O': 0, 'L': 0, 'S': 0, 'D': 0, 'errors': 0})
                self._feed(rx, 'info', node, 'process started')
            elif kind == 'process_exit':
                self._t(rx)
                if node == 'cu':
                    self.cu['status'] = 'offline'
                else:
                    self.cells[node]['status'] = 'offline'
                    self.cells[node]['ue'] = None
                    if self.ue['serving'] == node:
                        self.ue['serving'] = None
                self._feed(rx, 'warning' if row.get('returncode') else 'info', node, f"process exited (code {row.get('returncode')})")
            elif kind == 'interlock_abort':
                self._t(rx)
                self.cells[node]['interlock'] = row.get('reason')
                self._feed(rx, 'critical', node, 'INTERLOCK stopped the radio: ' + str(row.get('reason')))
            elif kind == 'radio_verified':
                self._t(rx)
                self._bump(node, 'radio_verified')
                self._feed(rx, 'good', node, 'radio settings verified against the rendered config')

    def _line(self, node, line, rx, replay_time=False):
        t, _src = (rx, 'replay') if replay_time else oai_epoch(clean(line), rx)
        self._t(t)
        if node in self.cells:
            self.cells[node]['last_line'] = t
        for ev, d in parse(line):
            if node == 'cu':
                self._cu_event(ev, d, t)
            elif node == 'ue':
                if ev.startswith('hw_') and ev[3:] in ('tx_gain', 'tx_freq', 'rx_freq'):
                    self.ue.setdefault('radio', {})[ev[3:]] = d.get('v')
            elif node in self.cells:
                self._du_event(node, ev, d, t)

    def _cu_event(self, ev, d, t):
        ue = d.get('ue_id')
        if ev == 'ngap_up':
            self.cu.update(status='connected', ngap=True)
            self._feed(t, 'good', 'cu', 'NG setup with the AMF complete')
        elif ev == 'f1_setup_response':
            name = self.by_du.get(int(d['du_id']))
            if name:
                self._bump(name, 'f1_connected')
                self._feed(t, 'good', 'cu', f'{name} (DU {d["du_id"]}) connected over F1')
        elif ev == 'rrc_connected':
            name = self._cell_of_cellid_hex(d['cellid'])
            self.ue.update(ue_id=int(ue), rnti=d['rnti'])
            self._serve(name, t)
            self._feed(t, 'good', 'cu', f'UE {ue} RRC connected on {name}')
        elif ev == 'pdu_session_added':
            self.ue['pdu'] = 'added'
            self._feed(t, 'info', 'cu', f'PDU session {d["pdu"]} added')
        elif ev == 'a3_report':
            self.last_a3[ue] = t
            self._feed(t, 'warning', 'cu', f'UE {ue}: A3 measurement report (neighbour better than serving)')
        elif ev == 'neighbour_rsrp':
            name = self.by_pci.get(int(d['pci']))
            if name:
                self._push('rsrp', name, t, float(d['rsrp']))
        elif ev == 'ho_trigger':
            target = self.by_pci.get(int(d['pci']))
            kind = 'A3' if t - self.last_a3.get(ue, -1e18) <= 1.0 else ('manual' if any(0 <= t - m <= 3 for m in self.manual) else 'unattributed')
            ho = {'id': len(self.handovers) + 1, 'ue_id': int(ue), 'kind': kind, 'source': self.ue['serving'], 'target': target, 'rnti': d['rnti'],
                  't_trigger': t, 't_command': None, 't_complete': None, 'outcome': 'in progress'}
            self.handovers.append(ho)
            self.ue['ho_active'] = ho['id']
            self._feed(t, 'warning', 'cu', f'HANDOVER #{ho["id"]} started ({kind}): {ho["source"]} -> {target}')
        elif ev == 'ho_command':
            ho = self._open_ho(ue)
            if ho and ho['t_command'] is None:
                ho['t_command'] = t
                self._feed(t, 'info', 'cu', f'HO #{ho["id"]}: target DU ready, RRC reconfiguration sent to the UE')
        elif ev == 'ho_complete':
            ho = self._open_ho(ue)
            if ho:
                ho.update(t_complete=t, outcome='complete')
                self.ue.update(rnti=d['rnti'], ho_active=None)
                self._serve(ho['target'], t)
                self._feed(t, 'good', 'cu', f'HANDOVER #{ho["id"]} complete in {(t - ho["t_trigger"]) * 1e3:.0f} ms: now on {ho["target"]}')
        elif ev in ('ho_abort', 'ho_failure'):
            ho = self._open_ho(ue)
            if ho:
                ho['outcome'] = 'failed'
                self.ue['ho_active'] = None
            self._feed(t, 'critical', 'cu', f'handover for UE {ue} aborted by the CU')
        elif ev == 'reestablishment':
            self._feed(t, 'warning', 'cu', 'RRC re-establishment (radio link problem, not a clean handover)')
        elif ev == 'ho_source_release':
            self._feed(t, 'info', 'cu', f'source cell PCI {d["pci"]} releases the UE context')

    def _open_ho(self, ue):
        for ho in reversed(self.handovers):
            if ho['outcome'] == 'in progress' and (ue is None or ho['ue_id'] == int(ue)):
                return ho
        return None

    def _du_event(self, node, ev, d, t):
        cell = self.cells[node]
        if ev.startswith('hw_'):
            key = ev[3:]
            if key == 'tx_gain_request':
                cell['hw']['tx_gain_range'] = float(d['range'])
            elif key != 'rx_gain_clipped':
                cell['hw'][key] = d['v']
            self._bump(node, 'radio_config')
        elif ev == 'ext_ref_locked':
            cell['hw']['ext_locked'] = True
            self._feed(t, 'good', node, 'locked to the external 10 MHz reference')
        elif ev == 'ext_ref_failed':
            cell['hw']['ext_locked'] = False
            self._feed(t, 'critical', node, 'failed to lock to the external reference (check OctoClock cabling)')
        elif ev == 'uhd_fast_path':
            for ch in d['chars']:
                cell['uhd'][ch] = cell['uhd'].get(ch, 0) + 1
        elif ev == 'uhd_error':
            cell['uhd']['errors'] += 1
            if cell['uhd']['errors'] <= 5:
                self._feed(t, 'warning', node, 'UHD stream/radio error reported')
        elif ev == 'mac_ue':
            u = cell['ue'] = {**(cell['ue'] or {}), 'rnti': d['rnti'], 'sync': d['sync'], 'ph': int(d['ph']),
                              'pcmax': int(d['pcmax']), 't': t}
            if 'rsrp' in d:
                u['l1_rsrp'] = int(d['rsrp'])
                self._push('l1_rsrp', node, t, int(d['rsrp']))
            if 'sinr' in d:
                u['sinr'] = float(d['sinr'])
        elif ev in ('mac_dl', 'mac_ul', 'mac_csi', 'mac_goodput') and cell['ue'] is not None and cell['ue'].get('rnti') == d['rnti']:
            u = cell['ue']
            if ev == 'mac_dl':
                u.update(dl_bler=float(d['bler']), dl_mcs=int(d['mcs']))
                if 'pucch_rssi' in d:
                    u.update(pucch_snr=float(d['pucch_snr']), pucch_rssi=float(d['pucch_rssi']))
            elif ev == 'mac_ul':
                u.update(ul_bler=float(d['bler']), ul_mcs=int(d['mcs']), ul_snr=float(d['snr']))
                self._push('ul_snr', node, t, float(d['snr']))
                if 'rssi' in d:
                    u['ul_rssi'] = float(d['rssi'])
                    self._push('ul_rssi', node, t, float(d['rssi']))
                    if float(d['rssi']) > -12 and not u.get('rssi_warned'):
                        u['rssi_warned'] = True
                        self._feed(t, 'critical', node, f'uplink level {d["rssi"]} dBFS: close to ADC clipping, lower UE power or RX gain')
            elif ev == 'mac_csi':
                u['cqi'] = int(d['cqi'])
            else:
                u.update(dl_mbps=float(d['dl']), ul_mbps=float(d['ul']))
                self._push('dl_mbps', node, t, float(d['dl']))
        elif ev == 'ul_failure':
            ho = next((h for h in reversed(self.handovers) if h['source'] == node and h.get('rnti') == d['rnti']
                       and t - h['t_trigger'] <= 5), None)
            if ho:
                self._feed(t, 'info', node, f'source DU stopped hearing UE {d["rnti"]} after handover #{ho["id"]} (expected)')
            else:
                self._feed(t, 'warning', node, f'UE {d["rnti"]}: uplink failure detected by the DU')

    def rrc_stats(self, text, t):
        s = parse_rrc_stats(text)
        with self.lock:
            self._t(t)
            for du in s['dus']:
                if du['assoc'] is not None:
                    self.du_assoc[du['assoc']] = self.by_du.get(du['du_id'])
            for u in s['ues'][:1]:
                self.ue.update(ue_id=u['ue_id'], rnti=u['rnti'], activity_s=u['last_activity_s'],
                               pdu=next(iter(u['pdu'].values()), self.ue['pdu']))
                name = self.du_assoc.get(u['assoc'])
                if name and self.ue['ho_active'] is None:
                    self._serve(name, t)
                for pci, m in u['meas'].items():
                    cell = self.by_pci.get(pci)
                    if cell and 'rsrp' in m:
                        self._push('rsrp', cell, t, m['rsrp'])

    def probe_row(self, row, t=None):
        if row.get('kind') != 'packet' or row.get('status') == 'censored':
            return
        ts = row['send_epoch_ns'] / 1e9 if t is None else t
        with self.lock:
            self._t(ts)
            self.rtt.append((ts, row['rtt_ms'] if row['status'] == 'received' else None))
            while self.rtt and self.rtt[0][0] < ts - HISTORY_S:
                self.rtt.popleft()

    def rxmon_row(self, row):
        """One line of rxmon.jsonl (oaiho rx-monitor on the UE radio, receive-only)."""
        with self.lock:
            if row.get('kind') == 'rxmon_start':
                self.rx_latest = {'start': row}
                self._feed(row['epoch'], 'info', 'receiver', f"receive-only monitor started at {row['freq_hz']/1e6:.2f} MHz, "
                           f"gain {row['gain_db']} dB, 10 MHz locked: {row['ref_locked']}")
            elif row.get('kind') == 'rxmon' and 'burst_dbfs' in row:
                t = self._t(row['epoch'])
                was = (self.rx_latest or {}).get('pss_detected')
                self.rx_latest = {**(self.rx_latest or {}), **{k: v for k, v in row.items() if k != 'envelope_db'}}
                self.rxmon.append((t, row['burst_dbfs'], row.get('floor_dbfs'), row.get('pss_rho')))
                while self.rxmon and self.rxmon[0][0] < t - HISTORY_S:
                    self.rxmon.popleft()
                if row.get('pss_detected') and not was:
                    self._feed(t, 'good', 'receiver', f"cell found: PSS N_ID2={row['pss_nid2']} every {row.get('pss_period_ms')} ms")
                elif was and not row.get('pss_detected'):
                    self._feed(t, 'warning', 'receiver', 'cell lost at the measurement receiver')

    def trigger_row(self, row, t=None):
        ts = row.get('epoch_ns', 0) / 1e9 if t is None else t
        with self.lock:
            if row.get('kind') == 'trigger_sent' or (row.get('kind') == 'trigger_request' and row.get('executed')):
                self.manual.append(ts)
                # The CU logs the handover ~1 ms after the command, often before this row is read.
                for ho in self.handovers:
                    if ho['kind'] == 'unattributed' and 0 <= ho['t_trigger'] - ts <= 3:
                        ho['kind'] = 'manual'
                if row.get('kind') == 'trigger_sent':
                    self._feed(ts, 'info', 'operator', f"telnet sent: {row.get('command')}")
            if row.get('executed'):
                moved = f" (DU {row.get('pre_du_id')} -> {row.get('post_du_id')})" if 'post_du_id' in row else ''
                self._feed(ts, 'info', 'operator', f"CU replied: {str(row.get('response', '')).strip().splitlines()[0][:70] if row.get('response') else '-'}{moved}")

    # ------------------------------------------------------------------ output
    def snapshot(self):
        with self.lock:
            now = self.now or time.time()
            stale = {}
            for name, cell in self.cells.items():
                stale[name] = cell['ue'] is not None and now - cell['ue'].get('t', now) > 4.0
            bins = {}
            for ts, v in self.rtt:
                k = round(ts * 10) / 10
                b = bins.setdefault(k, [None, 0])
                if v is None:
                    b[1] += 1
                else:
                    b[0] = v if b[0] is None else max(b[0], v)
            return {'meta': self.meta, 'now': now, 'cu': dict(self.cu), 'ue': dict(self.ue),
                    'cells': [{**cell, 'ue_stale': stale[n]} for n, cell in self.cells.items()],
                    'series': {k: {n: list(q) for n, q in v.items()} for k, v in self.series.items()},
                    'rtt': [[k, b[0], b[1]] for k, b in sorted(bins.items())],
                    'handovers': self.handovers[-30:], 'feed': sorted(self.feed, key=lambda f: f['t'])[-60:],
                    'rxmon': [list(r) for r in self.rxmon], 'rx_latest': self.rx_latest}


# ---------------------------------------------------------------------- live tailing
class Tail:
    """Follow the newest file matching a glob, restarting when a newer file appears."""

    def __init__(self, pattern_dir, pattern):
        self.dir, self.pattern, self.path, self.pos, self.buf = Path(pattern_dir), pattern, None, 0, ''

    def read(self):
        files_ = sorted(self.dir.glob(self.pattern)) if self.dir.exists() else []
        if not files_:
            return []
        if files_[-1] != self.path:
            self.path, self.pos, self.buf = files_[-1], 0, ''
        try:
            with open(self.path, 'r', errors='replace') as f:
                f.seek(self.pos)
                data = f.read()
                self.pos = f.tell()
        except OSError:
            return []
        data = self.buf + data
        lines = data.split('\n')
        self.buf = lines.pop()
        return [ln for ln in lines if ln.strip()]


def live_loop(state, run_dir, packets, stop):
    run = Path(run_dir)
    tails = {node: Tail(run / 'logs' / node, '*.jsonl') for node in ['cu', 'ue'] + list(state.cells)}
    probe = Tail(Path(packets).parent, Path(packets).name) if packets else Tail(run, 'packets*.jsonl')
    trig = Tail(run, 'trigger.jsonl')
    rxm = Tail(run, 'rxmon.jsonl')
    stats_path, stats_mtime = run / 'logs' / 'cu' / 'nrRRC_stats.log', 0
    while not stop.is_set():
        for node, tail in tails.items():
            for raw in tail.read():
                try:
                    state.runner_row(node, json.loads(raw))
                except (ValueError, KeyError):
                    pass
        for raw in probe.read():
            try:
                state.probe_row(json.loads(raw))
            except (ValueError, KeyError):
                pass
        for raw in trig.read():
            try:
                state.trigger_row(json.loads(raw))
            except ValueError:
                pass
        for raw in rxm.read():
            try:
                state.rxmon_row(json.loads(raw))
            except (ValueError, KeyError):
                pass
        try:
            m = stats_path.stat().st_mtime
            if m != stats_mtime:
                stats_mtime = m
                state.rrc_stats(stats_path.read_text(errors='replace'), m)
        except OSError:
            pass
        stop.wait(0.2)


# ---------------------------------------------------------------------- replay of an earlier RFsim run
def replay_timeline(run_dir):
    """Re-time the plain-text logs of tools/handover_experiments/results/<f1>/run_N.

    Those logs carry no timestamps. events.csv recorded when selected lines appeared; lines between
    such anchors get linearly interpolated times. Good enough to exercise the dashboard, NOT a
    measurement - the page labels it as a replay.
    """
    run = Path(run_dir)
    with (run / 'events.csv').open() as fh:
        ev = list(csv.DictReader(fh))
    base = int(ev[0]['timestamp_ns'])
    starts, anchors, end = {}, {}, 0.0
    for e in ev:
        t = (int(e['timestamp_ns']) - base) / 1e9
        end = max(end, t)
        if e['component'] == 'controller' and e['event'] == 'component_started':
            starts[e['details']] = t
        elif e['component'] in ('cu', 'du0', 'du1', 'source_gnb', 'target_gnb'):
            try:
                d = json.loads(e['details'])
                anchors.setdefault(e['component'], []).append((d['line'].rstrip(), int(d.get('occurrence', 1)), t))
            except (ValueError, KeyError):
                pass
    pings = []
    if (run / 'ping.csv').exists():
        with (run / 'ping.csv').open() as fh:
            ping_rows = list(csv.DictReader(fh))
        for p in ping_rows:
            t = (int(p['timestamp_ns']) - base) / 1e9
            end = max(end, t)
            pings.append((t, 'probe', {'kind': 'packet', 'status': 'received' if p['success'] == 'True' else 'lost',
                                       'rtt_ms': float(p['rtt_ms']) if p['rtt_ms'] not in ('', 'nan') else None,
                                       'send_epoch_ns': 0}))
    items = []
    for comp in ('cu', 'du0', 'du1'):
        path = run / f'{comp}.log'
        if not path.exists():
            continue
        lines = path.read_text(errors='replace').splitlines()
        pts = [(0, starts.get(comp, 0.0))]
        for text, occ, t in anchors.get(comp, []):
            seen = 0
            for i, ln in enumerate(lines):
                if ln.rstrip() == text:
                    seen += 1
                    if seen == occ:
                        pts.append((i, t))
                        break
        pts.append((len(lines), end))
        pts.sort()
        for k in range(1, len(pts)):
            pts[k] = (pts[k][0], max(pts[k][1], pts[k - 1][1]))
        k = 0
        for i, ln in enumerate(lines):
            while k + 1 < len(pts) - 1 and pts[k + 1][0] <= i:
                k += 1
            (i0, t0), (i1, t1) = pts[k], pts[k + 1]
            t = t0 if i1 == i0 else t0 + (t1 - t0) * (i - i0) / (i1 - i0)
            items.append((t, comp, {'kind': 'oai_log', 'line': ln}))
        items.append((starts.get(comp, 0.0), comp, {'kind': 'process_start'}))
    items += pings
    items.sort(key=lambda x: x[0])
    return items


def replay_run_timeline(run_dir):
    """A run folder written by this framework (oaiho session / run-node): every row already carries its
    host epoch, so the replay is the recorded order and spacing of the runner logs, probes and triggers."""
    run = Path(run_dir)
    items = []
    for d in sorted(p for p in (run / 'logs').iterdir() if p.is_dir()):
        for f in sorted(d.glob('*.jsonl')):
            for ln in f.read_text(errors='replace').splitlines():
                try:
                    row = json.loads(ln)
                except ValueError:
                    continue
                if 'epoch_ns' in row:
                    items.append((row['epoch_ns'] / 1e9, d.name, row))
    for name, node, key in (('packets.jsonl', 'probe', 'send_epoch_ns'), ('trigger.jsonl', 'trigger', 'epoch_ns')):
        if (run / name).exists():
            for ln in (run / name).read_text().splitlines():
                try:
                    row = json.loads(ln)
                except ValueError:
                    continue
                if row.get(key) and (node != 'probe' or row.get('kind') == 'packet'):
                    t = row[key] / 1e9 + ((row.get('rtt_ms') or 0) / 1e3 if node == 'probe' else 0)
                    items.append((t, node, row))
    items.sort(key=lambda x: x[0])
    return items


def replay_loop(state, items, speed, stop):
    wall0 = time.time()
    t_first = items[0][0] if items else 0
    for t, node, row in items:
        if stop.is_set():
            return
        delay = (t - t_first) / speed - (time.time() - wall0)
        if delay > 0:
            stop.wait(delay)
        # Delivery is paced by `speed`, but rows keep their recorded spacing, so durations stay true
        # (the dashboard's 'now' is the latest event time, not the wall clock).
        ts = wall0 + (t - t_first)
        if node == 'probe':
            row = {**row, 'send_epoch_ns': int(ts * 1e9)}
            state.probe_row(row, ts)
        elif node == 'trigger':
            state.trigger_row({**row, 'epoch_ns': int(ts * 1e9)}, ts)
        else:
            state.runner_row(node, row, ts)
    with state.lock:
        state._feed(time.time(), 'info', 'replay', 'replay finished')


# ---------------------------------------------------------------------- HTTP
def serve(c, run_dir=None, packets=None, port=8050, replay=None, speed=1.0, allow_trigger=False, bind='127.0.0.1'):
    source = (f'replay of {replay} (recorded timing, {c["mode"]} run)' if replay and (Path(replay) / 'manifest.json').exists()
              else f'replay of {replay} (timing approximate, RFsim data)' if replay else f'live: {run_dir}')
    state = State(c, source)
    state.meta['allow_trigger'] = bool(allow_trigger and not replay and len(active_cells(c)) >= 2)
    stop = threading.Event()
    if replay:
        timeline = replay_run_timeline(replay) if (Path(replay) / 'manifest.json').exists() else replay_timeline(replay)
        th = threading.Thread(target=replay_loop, args=(state, timeline, speed, stop), daemon=True)
    else:
        th = threading.Thread(target=live_loop, args=(state, run_dir, packets, stop), daemon=True)
    th.start()
    page = files('usrp_5g_oai').joinpath('dashboard.html').read_bytes()
    trigger_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path in ('/', '/index.html'):
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(page)))
                self.end_headers()
                self.wfile.write(page)
            elif self.path == '/api/state':
                self._json(state.snapshot())
            elif self.path == '/api/stream':
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Cache-Control', 'no-cache')
                self.end_headers()
                try:
                    while not stop.is_set():
                        self.wfile.write(b'data: ' + json.dumps(state.snapshot()).encode() + b'\n\n')
                        self.wfile.flush()
                        time.sleep(1.0)
                except (BrokenPipeError, ConnectionResetError):
                    return
            else:
                self.send_error(404)

        def do_POST(self):
            if self.path != '/api/trigger' or not state.meta['allow_trigger']:
                return self._json({'ok': False, 'error': 'trigger disabled (start the dashboard with --allow-trigger)'}, 403)
            if self.headers.get('X-Requested-By') != 'oaiho-dashboard':
                return self._json({'ok': False, 'error': 'missing request header'}, 400)
            if not trigger_lock.acquire(blocking=False):
                return self._json({'ok': False, 'error': 'a trigger request is already running'}, 409)
            try:
                from .runtime import trigger
                with state.lock:
                    ue_id, serving = state.ue['ue_id'], state.ue['serving']
                target = next((x['pci'] for x in c['cells'] if x['name'] != serving), None) if serving else None
                row = trigger(c, ue_id=ue_id, target_pci=target, execute=True, journal=str(Path(run_dir) / 'trigger.jsonl'))
                self._json({'ok': True, 'row': row})
            except Exception as e:                       # report every refusal to the page
                self._json({'ok': False, 'error': str(e)}, 409)
            finally:
                trigger_lock.release()

    httpd = ThreadingHTTPServer((bind, port), Handler)
    httpd.daemon_threads = True
    print(f'dashboard: http://{bind}:{port}/  ({source})  Ctrl+C to stop', flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        httpd.server_close()
    return {'stopped': True}
