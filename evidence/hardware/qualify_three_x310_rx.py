"""RX-only transport comparison; no --tx_rate is supplied to benchmark_rate.

One process per radio avoids requiring an external PPS input on the UE radio.
All selected daughterboards are in slot B. Raw logs and exact argv are retained.
"""
import json
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent / 'three_x310_rx_qualification'
ROOT.mkdir(exist_ok=False)
BIN = '/usr/local/lib/uhd/examples/benchmark_rate'
NIC = 'enx00e04c680014'
DURATION = 60
DEVICES = {
    'du0': ('192.168.10.2', '34BA717'),
    'du1': ('192.168.20.6', '34BA72C'),
    'ue': ('192.168.10.5', '34AF675'),
}


def counters():
    root = Path('/sys/class/net') / NIC / 'statistics'
    out = {'epoch_ns': time.time_ns()}
    for p in root.glob('*'):
        out[p.name] = int(p.read_text())
    r = subprocess.run(['/usr/sbin/ethtool', '-S', NIC], text=True, capture_output=True)
    out['driver_stats_returncode'] = r.returncode
    out['driver_stats_raw'] = r.stdout + r.stderr
    return out


def phase(name, nodes):
    folder = ROOT / name
    folder.mkdir()
    before = counters()
    started = time.monotonic()
    children = []
    rows = []
    try:
        for node in nodes:
            ip, serial = DEVICES[node]
            args = f'type=x300,addr={ip},serial={serial},master_clock_rate=184.32e6'
            argv = [BIN, '--args', args, '--rx_subdev', 'B:0', '--rx_rate', '7.68e6',
                    '--rx_cpu', 'sc16', '--rx_otw', 'sc16', '--channels', '0',
                    '--ref', 'external', '--duration', str(DURATION)]
            assert '--tx_rate' not in argv
            path = folder / (node + '.log')
            log = path.open('x')
            proc = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT)
            children.append((node, proc, log, path, argv))
        for node, proc, log, path, argv in children:
            rc = proc.wait(timeout=DURATION + 60)
            log.close()
            raw = path.read_text()
            stats = {k.strip(): int(v) for k, v in re.findall(r'^\s*(Num [^:]+):\s*(\d+)\s*$', raw, re.M)}
            row = {'node': node, 'argv': argv, 'returncode': rc, 'log': str(path), 'statistics': stats}
            rows.append(row)
            print(json.dumps({'phase': name, **row}), flush=True)
    finally:
        for node, proc, log, path, argv in children:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            log.close()
    result = {'phase': name, 'nodes': nodes, 'duration_requested_s': DURATION,
              'elapsed_with_initialization_s': time.monotonic() - started,
              'before': before, 'after': counters(), 'radios': rows}
    (folder / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    return result


for name in ('nr-softmodem', 'nr-uesoftmodem', 'benchmark_rate'):
    check = subprocess.run(['pgrep', '-x', name], capture_output=True, text=True)
    if check.returncode != 1:
        raise SystemExit(f'Stop existing {name} before this test (or process inspection failed).')

result = {'started_utc': datetime.now(timezone.utc).isoformat(), 'transmit': False,
          'sample_rate_sps_per_radio': 7680000, 'wire_format': 'sc16', 'interface': NIC,
          'limits': ['Receive-only test; not full-duplex or OAI real-time qualification.',
                     'One 60-second observation per phase, not repeated experimental conditions.'], 'phases': []}
for name, nodes in [('ue_alone', ['ue']), ('three_concurrent', ['du0', 'du1', 'ue'])]:
    result['phases'].append(phase(name, nodes))
(ROOT / 'summary.json').write_text(json.dumps(result, indent=2) + '\n')
print('Saved ' + str(ROOT / 'summary.json'), flush=True)
