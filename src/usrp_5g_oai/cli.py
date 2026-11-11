import argparse
import copy
import itertools
import json
import random
import re
import sys
from pathlib import Path
import yaml
from .config import validate, budget, require, warnings, gain_report
from .oai import fetch, render
from .runtime import run_node, trigger, inventory, doctor, clock_check, rx_check, tone_cal, rx_monitor
from .channels import write_trace, replay
from .traffic import echo_server, probe, ping_probe
from .analysis import demo, report, compare, extract_events
from .profiles import fspl_db, PROFILES


def sweep(base, spec, output):
    grid = yaml.safe_load(Path(spec).read_text())
    axes = grid['axes']; jobs = []
    for values in itertools.product(*axes.values()):
        for repetition in range(grid['repetitions']):
            c = copy.deepcopy(base)
            for path, value in zip(axes, values):
                keys = path.split('.'); target = c
                for k in keys[:-1]:
                    target = target[int(k)] if isinstance(target, list) else target[k]
                k = keys[-1]
                require((isinstance(target, list) and k.isdigit()) or k in target, 'unknown sweep field: ' + path)
                if isinstance(target, list):
                    target[int(k)] = value
                else:
                    target[k] = value
            c['experiment']['repetition'] = repetition
            c['experiment']['seed'] = grid['seed'] + repetition
            # Every new hardware condition needs a reviewed calibration envelope.
            c['safety']['gain_mapping_verified'] = False
            validate(c); jobs.append(c)
    random.Random(grid['seed']).shuffle(jobs)
    out = Path(output); out.mkdir(parents=True, exist_ok=False)
    for i, c in enumerate(jobs):
        (out / f'run_{i:04d}.yaml').write_text(yaml.safe_dump(c, sort_keys=False))
    (out / 'schedule.json').write_text(json.dumps({'seed': grid['seed'], 'count': len(jobs), 'automatic_execution': False}, indent=2) + '\n')
    return {'jobs': len(jobs), 'directory': str(out)}


def candidates(log, output):
    pattern = re.compile(r'handover|\bHO\b|ReconfigurationComplete|re-establish|Reestablish|RLF|underflow|overflow|late packet|Actual [RT]X|USRP clock|INTERLOCK', re.I)
    with open(output, 'x') as f:
        for number, line in enumerate(Path(log).read_text().splitlines(), 1):
            row = json.loads(line)
            if pattern.search(row.get('line', '') + row.get('reason', '')):
                row.update(evidence=f'{log}:{number}', classification='candidate_only')
                f.write(json.dumps(row) + '\n')
    return {'output': output, 'automatic_handover_success_inference': False}


def ota_table(c, distances):
    """Free-space loss at the configured DL/UL centre frequencies, for filling safety.receivers."""
    from .profiles import frequencies
    from .config import profile
    f = frequencies(profile(c))
    return [{'distance_m': d, 'fspl_dl_db': round(fspl_db(d, f['dl_center_hz']), 1), 'fspl_ul_db': round(fspl_db(d, f['ul_center_hz']), 1)}
            for d in distances]


def main(argv=None):
    p = argparse.ArgumentParser(description='Unified OAI/X310 framework; session and radio actions require --execute')
    sub = p.add_subparsers(dest='command', required=True)
    for name in ('validate', 'budget', 'gains', 'doctor', 'render', 'run-node', 'trigger', 'inventory', 'clock-check', 'rx-check', 'tone-cal', 'rx-monitor',
                 'replay', 'sweep', 'events', 'dashboard', 'ota'):
        q = sub.add_parser(name); q.add_argument('config')
        q.add_argument('--set', action='append', default=[], metavar='KEY=VALUE', help='override a config value for this command')
        if name == 'validate': q.add_argument('--live', action='store_true')
        if name == 'doctor': q.add_argument('--ping', action='store_true', help='also ping radios, OctoClock and AMF')
        if name == 'render': q.add_argument('--cache', default='.cache/oai'); q.add_argument('--out', required=True)
        if name == 'run-node':
            q.add_argument('--rendered', required=True); q.add_argument('--node', required=True)
            q.add_argument('--sudo', action='store_true', help='launch OAI via sudo -n (lab sudoers rule), stop it via sudo kill')
        if name == 'trigger':
            q.add_argument('--node'); q.add_argument('--ue-id', type=int); q.add_argument('--target-pci', type=int)
            q.add_argument('--journal', default='trigger.jsonl')
        if name == 'replay': q.add_argument('--trace', required=True); q.add_argument('--journal', required=True)
        if name == 'sweep': q.add_argument('--grid', required=True); q.add_argument('--out', required=True)
        if name == 'events': q.add_argument('--run', required=True); q.add_argument('--out', required=True)
        if name == 'ota': q.add_argument('--distances', default='0.5,1,2,3,5')
        if name == 'rx-check':
            q.add_argument('--gains', help='comma list of UHD RX gains (default 0,15,planned)')
            q.add_argument('--antennas', default='RX2,TX/RX'); q.add_argument('--out'); q.add_argument('--seconds', type=float, default=0.25)
        if name == 'dashboard':
            q.add_argument('--run', help='rendered run directory (live mode)'); q.add_argument('--packets')
            q.add_argument('--replay', help='earlier RFsim run directory (tools/handover_experiments/results/f1/run_00N)')
            q.add_argument('--speed', type=float, default=1.0); q.add_argument('--port', type=int, default=8050)
            q.add_argument('--allow-trigger', action='store_true', help='show a guarded "trigger handover" button')
        if name == 'tone-cal':
            q.add_argument('--out'); q.add_argument('--dry-run', action='store_true', help='configure and read back, send nothing')
            q.add_argument('--rx-gain', type=float, default=20.0)
        if name == 'rx-monitor':
            q.add_argument('--out', required=True); q.add_argument('--gain', type=float, default=28.0)
            q.add_argument('--duration', type=float, default=60.0); q.add_argument('--interval', type=float, default=1.0)
            q.add_argument('--antenna', default='RX2')
        if name in ('run-node', 'trigger', 'inventory', 'replay', 'clock-check', 'rx-check', 'tone-cal', 'rx-monitor'): q.add_argument('--execute', action='store_true')
    q = sub.add_parser('fetch'); q.add_argument('--out', default='.cache/oai'); q.add_argument('--checkout')
    q = sub.add_parser('trace'); q.add_argument('--kind', choices=['ramp', 'step', 'triangle', 'shadow'], default='ramp')
    q.add_argument('--out', required=True)
    for name, default in [('duration', 60), ('step', 1), ('low', 10), ('high', 40), ('sigma', 2), ('correlation-s', 5)]:
        q.add_argument('--' + name, type=float, default=default)
    q.add_argument('--seed', type=int, default=1)
    q = sub.add_parser('echo'); q.add_argument('--bind', default='127.0.0.1'); q.add_argument('--port', type=int, default=5202)
    q = sub.add_parser('probe'); q.add_argument('--host', required=True); q.add_argument('--port', type=int, default=5202)
    q.add_argument('--out', required=True); q.add_argument('--bind'); q.add_argument('--duration', type=float, default=60)
    q.add_argument('--interval', type=float, default=0.02); q.add_argument('--timeout', type=float, default=1)
    q.add_argument('--size', type=int, default=128)
    q.add_argument('--method', choices=['udp', 'icmp', 'ping'], default='udp',
                   help='udp: needs `oaiho echo` at --host; icmp: raw ICMP (root or ping_group_range); '
                        'ping: system ping, no privileges (use for an iPhone/router UE)')
    q.add_argument('--device', help='ping: source interface (-I), e.g. oaitun_ue1 for the RFsim UE')
    q = sub.add_parser('analyze'); q.add_argument('--packets', required=True); q.add_argument('--events'); q.add_argument('--out', required=True); q.add_argument('--plots', action='store_true')
    q.add_argument('--config', help='record experiment mode/topology and config hash; use the run\'s original YAML')
    q = sub.add_parser('demo'); q.add_argument('--out', required=True)
    q = sub.add_parser('candidates'); q.add_argument('--log', required=True); q.add_argument('--out', required=True)
    q = sub.add_parser('compare'); q.add_argument('--manifest', required=True); q.add_argument('--out', required=True)
    q.add_argument('--mixed-modes', action='store_true', help='allow RFsim and hardware conditions side by side')
    q = sub.add_parser('calibration-report'); q.add_argument('--steps', nargs='+', required=True); q.add_argument('--cal')
    q.add_argument('--out', required=True)
    q = sub.add_parser('session', help='one experiment run end to end (RFsim or hardware); preview unless --execute')
    q.add_argument('config'); q.add_argument('--out', required=True, help='new run folder, e.g. runs/rfsim_ho_001')
    q.add_argument('--cells', help='comma list of active cells for this run, e.g. du0 (default: experiment.active_cells)')
    q.add_argument('--set', action='append', default=[], metavar='KEY=VALUE',
                   help='override one config value for this run, e.g. cells.du0.att_tx_db=28 or mode=rfsim (repeatable)')
    q.add_argument('--duration', type=float, default=180.0, help='seconds of traffic after the UE attached')
    q.add_argument('--ho-every', type=float, help='trigger a manual F1 handover every N seconds')
    q.add_argument('--ho-count', type=int); q.add_argument('--ho-first', type=float)
    q.add_argument('--probe-host', default='192.168.70.135'); q.add_argument('--probe-interval', type=float, default=0.02)
    q.add_argument('--port', type=int, default=8050); q.add_argument('--no-dashboard', action='store_true')
    q.add_argument('--keep-dashboard', action='store_true'); q.add_argument('--no-core', action='store_true')
    q.add_argument('--keep-core', action='store_true', help='leave containers started by this session running')
    q.add_argument('--cache', default='.cache/oai'); q.add_argument('--execute', action='store_true')
    q = sub.add_parser('profiles')
    a = p.parse_args(argv)
    try:
        c = yaml.safe_load(Path(a.config).read_text()) if getattr(a, 'config', None) else None
        if c is not None:
            from .session import effective_config
            cells = a.cells.split(',') if a.command == 'session' and a.cells else None
            c = effective_config(c, getattr(a, 'set', []), cells)
        for w in (warnings(c) if c else []):
            print('NOTE: ' + w, file=sys.stderr)
        if a.command == 'validate':
            validate(c, live=a.live); result = {'valid': True, 'scope': 'live preconditions' if a.live else 'configuration only'}
        elif a.command == 'budget': result = budget(c)
        elif a.command == 'gains': result = gain_report(c)
        elif a.command == 'doctor': result = doctor(c, a.ping)
        elif a.command == 'fetch': fetch(a.out, a.checkout); result = {'cache': a.out}
        elif a.command == 'render': result = render(c, a.cache, a.out)
        elif a.command == 'run-node': result = run_node(c, a.rendered, a.node, a.execute, a.sudo)
        elif a.command == 'trigger': result = trigger(c, a.node, a.ue_id, a.target_pci, a.execute, a.journal)
        elif a.command == 'inventory': result = inventory(c, a.execute)
        elif a.command == 'clock-check': result = clock_check(c, a.execute)
        elif a.command == 'rx-check': result = rx_check(c, a.execute, a.gains, a.antennas, a.out, a.seconds)
        elif a.command == 'tone-cal': result = tone_cal(c, a.execute, a.dry_run, a.out, a.rx_gain)
        elif a.command == 'rx-monitor': result = rx_monitor(c, a.out, a.gain, a.duration, a.interval, a.antenna, a.execute)
        elif a.command == 'calibration-report':
            from .calibration import report as exp1_report
            r = exp1_report(a.steps, a.out, a.cal)
            result = {'report': a.out, 'checks': [f"{x['status']}: {x['title']}" for x in r['checks']]}
        elif a.command == 'trace':
            kw = vars(a).copy(); kw.pop('command'); out = kw.pop('out'); write_trace(out, **kw); result = {'trace': out}
        elif a.command == 'replay': result = replay(c, a.trace, a.journal, a.execute)
        elif a.command == 'sweep': result = sweep(c, a.grid, a.out)
        elif a.command == 'events': result = extract_events(c, a.run, a.out)
        elif a.command == 'ota': result = ota_table(c, [float(x) for x in a.distances.split(',')])
        elif a.command == 'dashboard':
            from .dashboard import serve
            require(a.run or a.replay, 'give --run RUN_DIR (live) or --replay EARLIER_RUN_DIR')
            result = serve(c, a.run, a.packets, a.port, a.replay, a.speed, a.allow_trigger)
        elif a.command == 'echo': echo_server(a.bind, a.port); return
        elif a.command == 'probe':
            require(a.method == 'ping' or a.device is None, '--device applies only to --method ping; use --bind for udp/icmp')
            require(a.method != 'ping' or a.bind is None, 'ping uses --device (interface or source address), not --bind')
            if a.method == 'ping':
                result = ping_probe(a.host, a.out, a.duration, a.interval, a.timeout, a.device, a.size)
            else:
                result = probe(a.host, a.port, a.out, a.duration, a.interval, a.timeout, a.bind, a.size, a.method == 'icmp')
        elif a.command == 'analyze': result = report(a.packets, a.out, a.events, a.plots, c)
        elif a.command == 'demo': result = demo(a.out)
        elif a.command == 'candidates': result = candidates(a.log, a.out)
        elif a.command == 'compare': result = compare(a.manifest, a.out, a.mixed_modes)
        elif a.command == 'profiles': result = PROFILES
        elif a.command == 'session':
            from .session import session
            result = session(c, a.out, a.cache, a.duration, a.ho_every, a.ho_count, a.ho_first, a.probe_host,
                             a.probe_interval, not a.no_dashboard, a.keep_dashboard, a.port, not a.no_core, a.execute, a.keep_core)
        print(json.dumps(result, indent=2))
        if a.command == 'run-node' and result.get('returncode', 0): raise SystemExit(result['returncode'])
        if a.command == 'session' and a.execute and any(f['status'] == 'FAIL' for f in result['findings']): raise SystemExit(2)
        if a.command == 'doctor' and not result['passed']: raise SystemExit(1)
        if a.command == 'clock-check' and a.execute and not result.get('passed'): raise SystemExit(2)
    except (ValueError, KeyError, OSError, RuntimeError, yaml.YAMLError) as e:
        p.exit(2, f'ERROR: {e}\n')


if __name__ == '__main__': main()
