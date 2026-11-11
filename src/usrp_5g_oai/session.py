"""One command per experiment run, the same for RFsim and for the X310 bench.

    oaiho session CFG --out runs/NAME [--cells du0] [--set key.path=value ...]
                      [--duration 180] [--ho-every 20 --ho-count 6] [--execute]

Order: (hardware) live safety gate -> render -> 5G core -> dashboard -> CU -> each active DU (hardware:
waits for logged radio settings) -> UE (OAI UE: waits for its oaitun_ue1 address)
-> traffic probe through the UE tunnel -> manual handover triggers (scheduled with --ho-every, or from the
dashboard button / `oaiho trigger`) -> ordered stop (UE, DUs, CU) -> events, report and a plain-language
summary. Everything stays in the run folder; RUN/config.yaml is the exact configuration that ran, so any
run can be repeated or varied later with --set (the same mechanism a parameter sweep uses).

Nothing here bypasses a safety check: every OAI process is started by `oaiho run-node`, which re-runs the
live gate, the pin check and the radio interlock itself.
"""
import copy
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
import yaml
from .config import ConfigError, require, validate, active_cells, digest, gain_report, profile
from .oailog import parse

PROBE_HOST = '192.168.70.135'          # oai-ext-dn in the OAI 5G core docker deployment
TUN = 'oaitun_ue1'
CORE_CONTAINERS = ('oai-amf', 'oai-smf', 'oai-upf')


class SessionError(RuntimeError):
    pass


# ------------------------------------------------------------------------------------- configuration
def set_path(c, dotted, value):
    """--set cells.du0.att_tx_db=28 / radio.dl_center_mhz=3561.6 / experiment.active_cells=[du0]."""
    parts = dotted.split('.')
    node = c
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        if isinstance(node, list):
            if part.lstrip('-').isdigit():
                idx = int(part)
            else:
                idx = next((j for j, x in enumerate(node) if isinstance(x, dict) and x.get('name') == part), None)
            require(idx is not None and -len(node) <= idx < len(node), f'--set {dotted}: no list element {part!r}')
            if last:
                node[idx] = value
            else:
                node = node[idx]
        else:
            require(isinstance(node, dict), f'--set {dotted}: {part!r} is not inside a mapping')
            if last:
                node[part] = value
            else:
                require(part in node, f'--set {dotted}: unknown section {part!r}')
                node = node[part]


def effective_config(c, sets=(), cells=None):
    """The configuration a run uses: the experiment file plus --set overrides, validated again."""
    c = copy.deepcopy(c)
    for s in sets or ():
        key, sep, raw = s.partition('=')
        require(sep and key.strip(), f'--set {s!r}: use key.path=value')
        set_path(c, key.strip(), yaml.safe_load(raw))
    if cells:
        c['experiment']['active_cells'] = list(cells)
    validate(c)
    return c


def schedule(duration, every, count=None, first=None, tail=10.0):
    """Trigger times (s after traffic start): first, first+every, ... leaving `tail` seconds at the end."""
    if not every:
        return []
    require(every >= 5, '--ho-every: at least 5 s between handovers')
    require(first is None or first >= 0, '--ho-first must be nonnegative')
    require(count is None or isinstance(count, int) and count > 0, '--ho-count must be positive')
    t, out = (first if first is not None else every), []
    while t <= duration - tail and (count is None or len(out) < count):
        out.append(round(t, 3))
        t += every
    return out


def plan(c, duration, ho_times, probe_host):
    require(c['topology'] == 'f1', 'session currently supports F1 only; N2 requires separately qualified run-node orchestration')
    require(duration >= 5 and duration < float('inf'), '--duration must be finite and at least 5 s')
    cells = [x['name'] for x in active_cells(c)]
    runs_ue = c['mode'] == 'rfsim' or (c['ue']['kind'] == 'oai_nrue' and not c['experiment'].get('ue_receive_only'))
    g = gain_report(c)
    gate = 'not needed (RFsim)'
    if c['mode'] == 'hardware':
        try:
            validate(c, live=True)
            gate = 'PASS'
        except ConfigError as e:
            gate = f'BLOCKED: {e}'
    order = ['core', 'dashboard', 'cu', cells[0]] + (['ue'] if runs_ue else []) + cells[1:] + (['probe'] if runs_ue else [])
    require(not ho_times or len(cells) >= 2, 'handover triggers need two active cells')
    return {'mode': c['mode'], 'rf_setup': c['safety']['rf_setup'], 'profile': g['profile'],
            'carrier_mhz': {'dl': g['dl_center_mhz'], 'ul': g['ul_center_mhz'], 'ssb': g['ssb_mhz']},
            'start_order': order,
            'tx_gain_db': {x['cell']: x['uhd_tx_gain_db'] for x in g['cells'] if x['cell'] in cells},
            'tx_gain_cap_db': g['max_uhd_tx_gain_db'], 'duration_s': duration,
            'handover_triggers_s': ho_times if len(cells) >= 2 else [],
            'probe': f'ping -I {TUN} {probe_host}' if runs_ue else None, 'live_gate': gate}


# ------------------------------------------------------------------------------------- processes
def _child_signals():
    signal.signal(signal.SIGINT, signal.SIG_DFL)


class Proc:
    def __init__(self, name, argv, console, cwd=None):
        self.name, self.argv, self.console = name, argv, Path(console)
        self.f = open(self.console, 'w')
        self.p = subprocess.Popen(argv, stdout=self.f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                  start_new_session=True, preexec_fn=_child_signals, cwd=cwd)
        self.started = time.time()

    def alive(self):
        return self.p.poll() is None

    def text(self):
        try:
            return self.console.read_text(errors='replace')
        except OSError:
            return ''

    def stop(self, sig=signal.SIGTERM, timeout=35.0):
        if self.alive():
            try:
                os.kill(self.p.pid, sig)
            except ProcessLookupError:
                pass
            try:
                self.p.wait(timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.kill(self.p.pid, signal.SIGTERM)
                    self.p.wait(10)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    try:
                        os.killpg(self.p.pid, signal.SIGKILL)
                        self.p.wait(5)
                    except (ProcessLookupError, subprocess.TimeoutExpired):
                        pass
        self.f.close()
        return self.p.poll()


def oaiho():
    exe = Path(sys.executable).parent / 'oaiho'
    return [str(exe)] if exe.exists() else [sys.executable, '-m', 'usrp_5g_oai.cli']


def oai_pids():
    r = subprocess.run(['pgrep', '-x', 'nr-softmodem|nr-uesoftmodem'], capture_output=True, text=True)
    return [int(x) for x in r.stdout.split()]


def owned_groups(run):
    groups = set()
    for f in (Path(run) / 'logs').glob('*/*.jsonl'):
        for line in f.read_text().splitlines():
            r = json.loads(line)
            if r.get('kind') == 'process_group':
                groups.add(int(r['pgid']))
    return groups


def force_stop_oai(groups):
    """Last resort: only groups recorded by this run's own runners."""
    for sig in ('TERM', 'KILL'):
        for pg in sorted(groups):
            subprocess.run(['sudo', '-n', '/usr/bin/kill', f'-{sig}', '--', f'-{pg}'], capture_output=True)
        if groups:
            time.sleep(5)
    return oai_pids()


def tun_address():
    r = subprocess.run(['ip', '-4', '-o', 'addr', 'show', 'dev', TUN], capture_output=True, text=True)
    m = re.search(r'inet (\d+\.\d+\.\d+\.\d+)', r.stdout)
    return m[1] if m else None


# ------------------------------------------------------------------------------------- the 5G core
def core_running():
    r = subprocess.run(['docker', 'ps', '--format', '{{.Names}}'], capture_output=True, text=True)
    require(r.returncode == 0, 'docker is not usable by this user: ' + r.stderr.strip()[:200])
    names = set(r.stdout.split())
    return all(n in names for n in CORE_CONTAINERS)


def compose_running(core_dir):
    r = subprocess.run(['docker', 'compose', 'ps', '--format', 'json'], cwd=Path(core_dir).expanduser(),
                       capture_output=True, text=True, timeout=15)
    require(r.returncode == 0, 'cannot inspect core project: ' + r.stderr[-300:])
    txt = r.stdout.strip()
    rows = json.loads(txt) if txt.startswith('[') else [json.loads(x) for x in txt.splitlines() if x.strip()]
    return {x['Name'] for x in rows if x.get('State') == 'running'}


def stop_owned_core(names):
    if names:
        r = subprocess.run(['docker', 'stop', '--time', '20', *sorted(names)], capture_output=True, text=True, timeout=90)
        require(r.returncode == 0, 'failed stopping session-owned core containers: ' + r.stderr[-300:])


def core_up(core_dir, say, timeout=120):
    if core_running():
        say('core: already running')
        return False
    d = Path(core_dir).expanduser()
    require((d / 'docker-compose.yaml').exists(), f'{d}/docker-compose.yaml not found (paths.core_dir)')
    say(f'core: docker compose up -d in {d}')
    r = subprocess.run(['docker', 'compose', 'up', '-d'], cwd=d, capture_output=True, text=True, timeout=300)
    require(r.returncode == 0, 'docker compose up failed: ' + r.stderr.strip()[-400:])
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = subprocess.run(['docker', 'compose', 'ps', '--format', 'json'], cwd=d, capture_output=True, text=True)
        txt = r.stdout.strip()
        rows = json.loads(txt) if txt.startswith('[') else [json.loads(x) for x in txt.splitlines() if x.strip().startswith('{')]
        if rows and all(x.get('State') == 'running' and x.get('Health') in ('', None, 'healthy') for x in rows):
            time.sleep(5)                       # AMF/SMF registration with the NRF after "running"
            say(f'core: {len(rows)} containers running/healthy after {time.time() - t0:.0f} s')
            return True
        time.sleep(2)
    raise SessionError('core did not become healthy in time (docker compose ps)')


# ------------------------------------------------------------------------------------- summaries
def node_summary(run, node):
    """Radio and transport facts of one runner's JSONL logs (works for DUs and the UE)."""
    out = {'node': node, 'radio_verified': False, 'interlock': None, 'U': 0, 'L': 0, 'O': 0, 'D': 0, 'S': 0,
           'uhd_errors': 0, 'actual_tx_gain_db': None, 'seconds': None, 'returncode': None}
    t0 = t1 = None
    for f in sorted((Path(run) / 'logs' / node).glob('*.jsonl')):
        for ln in f.read_text(errors='replace').splitlines():
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            k = r.get('kind')
            if k == 'process_start':
                t0 = r['epoch_ns'] / 1e9
            elif k == 'process_exit':
                t1 = r['epoch_ns'] / 1e9; out['returncode'] = r.get('returncode')
            elif k == 'radio_verified':
                out['radio_verified'] = True
            elif k == 'interlock_abort':
                out['interlock'] = r.get('reason')
            elif k == 'oai_log':
                for ev, d in parse(r['line']):
                    if ev == 'uhd_fast_path':
                        for ch in d['chars']:
                            out[ch] = out.get(ch, 0) + 1
                    elif ev == 'uhd_error':
                        out['uhd_errors'] += 1
                    elif ev == 'hw_tx_gain':
                        out['actual_tx_gain_db'] = float(d['v'])
    if t0 and t1:
        out['seconds'] = round(t1 - t0, 1)
    return out


def findings(c, info, analysis, nodes):
    """Plain-language PASS / CHECK / FAIL lines for the run summary."""
    F = []

    def add(status, title, detail):
        F.append({'status': status, 'title': title, 'detail': detail})

    if info.get('ue_expected'):
        if info.get('ue_ip'):
            add('PASS', 'UE attached', f"PDU session up ({TUN} {info['ue_ip']}) {info['attach_s']:.1f} s after the UE started")
        else:
            add('FAIL', 'UE attached', 'no PDU session: see ue.console (cell search / RACH / registration) and cu.console')
    t = (analysis or {}).get('traffic') or {}
    if t.get('sent_records'):
        loss = t.get('loss_fraction') or 0.0
        st = 'PASS' if loss < 0.01 else 'CHECK' if loss < 0.05 else 'FAIL'
        coverage = info.get('probe_complete', False)
        expected_duration = info.get('plan', {}).get('duration_s')
        if expected_duration is not None:
            coverage = coverage and (t.get('observed_send_span_s') or 0) >= max(1, expected_duration - 2)
        if not coverage or t.get('censored', 0):
            st = 'CHECK' if st == 'PASS' else st
        def ms(v, d=1):
            return 'n/a' if v is None else f'{v:.{d}f} ms'
        add(st, 'Traffic through the radio link',
            f"{t['received']}/{t['received'] + t['lost']} probes answered ({100 * loss:.2f} % lost); RTT median "
            f"{ms(t.get('rtt_median_ms'))}, p95 {ms(t.get('rtt_p95_ms'))}; longest reply gap {ms(t.get('max_interarrival_gap_ms'), 0)}; "
            f"capture {'complete' if coverage else 'incomplete/unverified'}; censored {t.get('censored', 0)}")
    elif info.get('ue_expected'):
        add('FAIL', 'Traffic through the radio link', 'no usable probe records; see probe.console and session.jsonl')
    h = (analysis or {}).get('handover') or {}
    sent = info.get('triggers_sent', 0)
    rejected = sum(bool(x.get('error')) for x in info.get('triggers', []))
    scheduled = len(info.get('plan', {}).get('handover_triggers_s', []))
    if sent or scheduled or rejected:
        ok = h.get('complete', 0)
        st = 'PASS' if ok == sent and sent >= scheduled and not rejected and not h.get('unknown') and not h.get('failed') else 'CHECK' if ok else 'FAIL'
        med = h.get('duration_median_ms'); gap = h.get('window_max_gap_median_ms')
        add(st, 'Manual handovers',
            f"{ok} of {sent} dispatched triggers completed; scheduled {scheduled}, request errors {rejected}, "
            f"failed {h.get('failed', 0)}, unknown {h.get('unknown', 0)}" + (f"; signalling median {med:.1f} ms" if med is not None else '')
            + (f"; median largest reply gap in HO windows {gap:.0f} ms" if gap is not None else ''))
    radios = [n for n in nodes if n['node'] != 'cu']
    if c['mode'] == 'hardware' and radios:
        bad = {n['node']: {k: n[k] for k in 'ULODS' if n[k]} | ({'errors': n['uhd_errors']} if n['uhd_errors'] else {})
               for n in radios if any(n[k] for k in 'ULODS') or n['uhd_errors']}
        add('PASS' if not bad else 'CHECK', 'Radio transport (USB/Ethernet to the X310s)',
            'no underflow / overflow / late / sequence / drop events' if not bad else
            f'stream events {bad}: investigate host transport and scheduling; traffic gaps cannot be attributed to handover alone')
        il = [n['node'] for n in radios if n['interlock']]
        unverified = [n['node'] for n in radios if not n['radio_verified']]
        add('FAIL' if il else 'CHECK' if unverified else 'PASS', 'Radio interlock',
            f'stopped {il}' if il else f'not verified: {unverified}' if unverified else
            'startup logs reported the configured gain, frequencies, rate and clock; this is a reactive log check')
    if info.get('analysis_error'):
        add('FAIL', 'Analysis', info['analysis_error'])
    if info.get('core_cleanup_error'):
        add('FAIL', 'Core cleanup', info['core_cleanup_error'])
    add('PASS' if not info.get('abort') and not info.get('leftover_oai') and not info.get('core_cleanup_error') else 'FAIL', 'Run ended cleanly',
        info.get('abort') or (f"OAI processes had to be forced: {info['leftover_oai']}" if info.get('leftover_oai') else
                              'all processes stopped in order (UE, DUs, CU)'))
    return F


def write_summary(run, c, info, analysis, nodes):
    F = findings(c, info, analysis, nodes)
    s = {'run': str(run), 'config_sha256': digest(c), 'mode': c['mode'], 'rf_setup': c['safety']['rf_setup'],
         'active_cells': [x['name'] for x in active_cells(c)], 'info': info, 'nodes': nodes, 'findings': F,
         'report': str(Path(run) / 'report' / 'report.html') if analysis else None}
    (Path(run) / 'summary.json').write_text(json.dumps(s, indent=2, default=str) + '\n')
    g = gain_report(c)
    lines = [f"# Run summary: {c['experiment']['name']}", '',
             f"{c['mode'].upper()} / {c['safety']['rf_setup']} - {g['profile']}, DL {g['dl_center_mhz']} MHz, UL {g['ul_center_mhz']} MHz - "
             f"cells {s['active_cells']} - UHD TX gain " + ', '.join(f"{x['cell']} {x['uhd_tx_gain_db']} dB" for x in g['cells']
                                                                  if x['cell'] in s['active_cells']), '',
             '| Result | Check | What it means |', '|---|---|---|']
    lines += [f"| **{x['status']}** | {x['title']} | {x['detail']} |" for x in F]
    if analysis and analysis['handover']['details']:
        lines += ['', '| # | from -> to | outcome | signalling ms | lost probes | largest gap ms |', '|---|---|---|---:|---:|---:|']
        for d in analysis['handover']['details']:
            w = d.get('traffic_window', {})
            sd = d['signaling_duration_ms']
            gap = w.get('max_gap_ms')
            lines.append(f"| {d['handover_id']} | PCI {d['source_pci']} -> {d['target_pci']} | {d['outcome']} | "
                         f"{'' if sd is None else f'{sd:.1f}'} | {w.get('lost', '')} | {'' if gap is None else f'{gap:.0f}'} |")
    lines += ['', 'Files: config.yaml (exact configuration), *.console (live output), logs/ (timestamped JSONL), '
              'packets.jsonl, trigger.jsonl, events.csv, packets.csv, handover_metrics.csv, radio_metrics.csv, '
              'checks.csv, transport.csv, handover.png, radio_metrics.png, report/traffic.png and report/report.html when traffic is available.']
    (Path(run) / 'summary.md').write_text('\n'.join(lines) + '\n')
    return s


# ------------------------------------------------------------------------------------- the run
def session(c, out, cache='.cache/oai', duration=180.0, ho_every=None, ho_count=None, ho_first=None,
            probe_host=PROBE_HOST, probe_interval=0.02, dashboard=True, keep_dashboard=False, port=8050,
            core=True, execute=False, keep_core=False):
    require(probe_interval >= 0.002 and probe_interval < float('inf'), '--probe-interval must be finite and at least 0.002 s')
    ho_times = schedule(duration, ho_every, ho_count, ho_first)
    p = plan(c, duration, ho_times, probe_host)
    if not execute:
        return {'execute': False, **p}
    from .oai import render
    from .runtime import trigger, hardware_host_preflight, serving_du
    from .analysis import extract_events, report
    if c['mode'] == 'hardware':
        validate(c, live=True)
        hardware_host_preflight(c)
    require(len(active_cells(c)) >= 2 or not ho_times, 'handover triggers need two active cells')
    require(not oai_pids(), 'OAI is already running: stop it first (scripts/stop_all.sh)')
    run = Path(out).resolve()
    render(c, cache, run)
    cfg = run / 'config.yaml'
    cfg.write_text(yaml.safe_dump(c, sort_keys=False))
    require(digest(yaml.safe_load(cfg.read_text())) == digest(c), 'config.yaml does not round-trip; report this')
    journal = run / 'session.jsonl'

    def say(msg, **kw):
        print(time.strftime('%H:%M:%S ') + msg, flush=True)
        with open(journal, 'a') as f:
            f.write(json.dumps({'epoch_ns': time.time_ns(), 'msg': msg, **kw}) + '\n')

    procs, info = {}, {'plan': p, 'ue_expected': p['probe'] is not None, 'triggers_sent': 0, 'triggers': [],
                      'probe_complete': False, 'core_started': []}
    core_before = None
    core_dir = c['paths'].get('core_dir', '~/oai-cn5g')
    stop_now = {'reason': None}

    def on_signal(signum, frame):
        stop_now['reason'] = f'stopped by {signal.Signals(signum).name}'
    previous = {s: signal.signal(s, on_signal) for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    O = oaiho()
    sudo = ['--sudo']

    def start_node(node):
        procs[node] = Proc(node, O + ['run-node', str(cfg), '--rendered', str(run), '--node', node, '--execute'] + sudo,
                           run / f'{node}.console')

    def wait(node, pattern, timeout, where=None):
        src = procs[where or node]
        t0 = time.time()
        while time.time() - t0 < timeout:
            if stop_now['reason']:
                raise SessionError(stop_now['reason'])
            text = src.text()
            if 'INTERLOCK' in procs[node].text():
                raise SessionError(f'{node}: radio interlock tripped - see {procs[node].console}')
            m = re.search(pattern, text)
            if m:
                return m
            for n, pr in procs.items():
                if n != 'probe' and not pr.alive():
                    raise SessionError(f'{n} exited (code {pr.p.returncode}) - see {pr.console}')
            time.sleep(0.5)
        raise SessionError(f'timeout ({timeout:.0f} s) waiting for {pattern!r} in {src.console.name}')

    try:
        say(f"session {c['experiment']['name']}: {c['mode']} {p['profile']} DL {p['carrier_mhz']['dl']} MHz, cells "
            f"{[x['name'] for x in active_cells(c)]}, {duration:.0f} s, triggers at {ho_times or 'manual only'}")
        if core:
            core_before = compose_running(core_dir)
            core_up(core_dir, say)
        if dashboard:
            procs['dashboard'] = Proc('dashboard', O + ['dashboard', str(cfg), '--run', str(run), '--packets', str(run / 'packets.jsonl'),
                                                        '--port', str(port), '--allow-trigger'], run / 'dashboard.console')
            wait('dashboard', r'dashboard: http://127\.0\.0\.1:', 20)
            say(f'live view: http://127.0.0.1:{port}  ' + ('(handover control available after attachment)' if len(active_cells(c)) >= 2
                                                           else '(single-cell baseline; handover control disabled)'))
        start_node('cu')
        wait('cu', r'Received NGSetupResponse', 60)
        say('CU connected to the core (NG setup done)')
        def start_cell(x):
            start_node(x['name'])
            if c['mode'] == 'hardware':
                wait(x['name'], r'radio settings verified', 120)
                say(f"{x['name']}: radio verified by the interlock (gain, frequency, rate, clock)")
            wait(x['name'], rf"\({re.escape(x['name'])}\): sending F1 Setup Response", 90, where='cu')
            say(f"{x['name']} (PCI {x['pci']}) is on the air and connected to the CU")
        cells = active_cells(c)
        start_cell(cells[0])
        if info['ue_expected']:
            t_ue = time.time()
            start_node('ue')
            if c['mode'] == 'hardware':
                wait('ue', r'radio settings verified', 120)
                say('ue: radio verified by the interlock')
            m = wait('ue', r'TUN Interface oaitun_ue1 successfully configured, IPv4 (\d+\.\d+\.\d+\.\d+)', 180)
            info['ue_ip'], info['attach_s'] = m[1], time.time() - t_ue
            say(f"UE attached: {TUN} {m[1]} after {info['attach_s']:.1f} s")
            time.sleep(1)
            require(tun_address() == info['ue_ip'], 'UE tunnel address does not match the new PDU session')
            _, initial, _ = serving_du(c)
            require(initial is not None and initial['pci'] == cells[0]['pci'], 'initial serving PCI could not be verified')
            info['initial_pci'] = initial['pci']
            say(f"initial serving PCI verified: {initial['pci']}")
        for x in cells[1:]:
            start_cell(x)
        if info['ue_expected']:
            procs['probe'] = Proc('probe', O + ['probe', '--method', 'ping', '--host', probe_host, '--device', TUN,
                                                '--out', str(run / 'packets.jsonl'), '--duration', f'{duration:g}',
                                                '--interval', f'{probe_interval:g}'], run / 'probe.console')
        else:
            say('no OAI UE in this run: connect your UE now (the CU log shows its attach)')
        t0 = time.time(); next_status = t0 + 10; pending = list(ho_times)
        say(f'measuring for {duration:.0f} s' + (f'; handover triggers at {ho_times} s' if ho_times else
            '; manual two-cell control available' if len(cells) >= 2 else '; single-cell baseline, no handover'))
        while time.time() - t0 < duration + 2:
            if stop_now['reason']:
                info['abort'] = stop_now['reason']; break
            dead = [n for n, pr in procs.items() if n != 'probe' and not pr.alive()]
            if dead:
                info['abort'] = f'{dead[0]} exited during the run (code {procs[dead[0]].p.returncode}) - see {dead[0]}.console'
                say('ABORT: ' + info['abort']); break
            el = time.time() - t0
            if pending and el >= pending[0]:
                pending.pop(0)
                try:
                    r = trigger(c, execute=True, journal=str(run / 'trigger.jsonl'))
                    info['triggers_sent'] += 1
                    info['triggers'].append({'t_s': round(el, 2), 'pre_pci': r.get('pre_pci'), 'post_pci': r.get('post_pci')})
                    say(f"HANDOVER trigger #{info['triggers_sent']} at {el:.1f} s: PCI {r.get('pre_pci')} -> {r.get('post_pci')}")
                except (ConfigError, OSError) as e:
                    info['triggers'].append({'t_s': round(el, 2), 'error': str(e)})
                    say(f'trigger at {el:.1f} s refused/failed: {e}')
            if time.time() >= next_status:
                next_status += 10
                cu = procs['cu'].text()
                done = len(re.findall(r'handover for UE \d+/RNTI \w+ complete!', cu))
                rx = lost = 0
                try:
                    for ln in (run / 'packets.jsonl').read_text().splitlines():
                        rx += '"status": "received"' in ln; lost += '"status": "lost"' in ln
                except OSError:
                    pass
                say(f'{el:5.0f}/{duration:.0f} s | probes answered {rx}, lost {lost} | handovers completed {done}'
                    + (f' of {info["triggers_sent"]} triggered' if info['triggers_sent'] else ''))
            if 'probe' in procs and not procs['probe'].alive() and el < duration - 2:
                raise SessionError('traffic probe ended early - see probe.console; capture is incomplete')
            if 'probe' in procs and not procs['probe'].alive():
                info['probe_returncode'] = procs['probe'].p.returncode
                info['probe_complete'] = procs['probe'].p.returncode == 0
            time.sleep(0.25)
        # Dashboard-button triggers are journaled in trigger.jsonl as well.
        try:
            info['triggers_sent'] = sum(1 for ln in (run / 'trigger.jsonl').read_text().splitlines()
                                        if '"trigger_sent"' in ln)
        except OSError:
            pass
    except (SessionError, ConfigError, OSError, subprocess.SubprocessError, ValueError) as e:
        info['abort'] = str(e)
        say('ABORT: ' + str(e))
    finally:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        say('stopping: probe, UE, DUs, CU')
        if 'probe' in procs:
            if not procs['probe'].alive():
                info['probe_returncode'] = procs['probe'].p.returncode
                info['probe_complete'] = not info.get('abort') and procs['probe'].p.returncode == 0
            procs['probe'].stop(signal.SIGINT, timeout=8)
        for node in ['ue'] + [x['name'] for x in reversed(active_cells(c))] + ['cu']:
            if node in procs:
                try:
                    rc = procs[node].stop()
                    say(f'{node} stopped (code {rc})')
                except (OSError, subprocess.SubprocessError) as e:
                    info['abort'] = f'{node} cleanup failed: {e}'
        left = oai_pids()
        if left:
            say(f'OAI processes still running {left}: forcing')
            info['leftover_oai'] = left
            left = force_stop_oai(owned_groups(run))
            if left:
                say(f'WARNING: still running {left}: run scripts/stop_all.sh or power the radio off')
        if 'dashboard' in procs and not keep_dashboard:
            procs['dashboard'].stop(timeout=5)
        if core_before is not None:
            try:
                owned = compose_running(core_dir) - core_before
                info['core_started'] = sorted(owned)
                if not keep_core:
                    stop_owned_core(owned)
                    say(f'core: stopped {len(owned)} containers started by this session; existing containers preserved')
                else:
                    say('core: --keep-core requested; session-started containers remain running')
            except (ConfigError, OSError, subprocess.SubprocessError, ValueError) as e:
                info['core_cleanup_error'] = str(e)
        for s, h in previous.items():
            signal.signal(s, h)
    analysis = None
    if (run / 'packets.jsonl').exists():
        try:
            events = None
            if c['topology'] == 'f1':
                extract_events(c, run, run / 'events.csv'); events = run / 'events.csv'
            analysis = report(run / 'packets.jsonl', run / 'report', events, plots=True, experiment=c)
        except (ConfigError, ValueError, OSError) as e:
            info['analysis_error'] = str(e)
            say(f'analysis failed: {e}')
    try:
        extract_events(c, run, run / 'events.csv') if not (run / 'events.csv').exists() else None
    except (ConfigError, ValueError, OSError) as e:
        info['analysis_error'] = str(e)
    nodes = [node_summary(run, n) for n in ['cu'] + [x['name'] for x in active_cells(c)] + (['ue'] if info['ue_expected'] else [])]
    s = write_summary(run, c, info, analysis, nodes)
    from .artifacts import export_run
    try:
        export_run(run, s, analysis)
    except (ValueError, OSError, ImportError) as e:
        info['analysis_error'] = f'artifact export failed: {e}'
        s = write_summary(run, c, info, analysis, nodes)
    say('summary: ' + str(run / 'summary.md'))
    for x in s['findings']:
        print(f"  {x['status']:5s} {x['title']}: {x['detail']}", flush=True)
    if keep_dashboard and 'dashboard' in procs:
        print(f'dashboard still running at http://127.0.0.1:{port} (pid {procs["dashboard"].p.pid}); stop it with kill', flush=True)
    return {'execute': True, 'run': str(run), 'findings': s['findings'], 'abort': info.get('abort')}
