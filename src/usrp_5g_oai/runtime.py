import glob
import hashlib
import ipaddress
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from .config import require, validate, digest, active_cells, profile
from .oai import lock, command_for, expected_radio
from .profiles import X300_MASTER_CLOCK_HZ, x310_rx_cal_offset

ADAPTERS = Path(__file__).resolve().parents[2] / 'adapters'


def append(path, row):
    with open(path, 'a') as f:
        f.write(json.dumps(row, sort_keys=True) + '\n')


def verify_manifest(c, directory):
    root = Path(directory).resolve()
    m = json.loads((root / 'manifest.json').read_text())
    require(digest(c) == m['config_sha256'], 'config changed; render to a new directory')
    for name, sha in m['files'].items():
        require(hashlib.sha256((root / name).read_bytes()).hexdigest() == sha, f'generated {name} changed; render again')
    for node, argv in m['commands'].items():
        require(argv == command_for(c, node, root), f'{node}: generated command changed or moved; render again')
    if m.get('uicc_link'):
        link = root / 'private_uicc.include'
        require(link.is_symlink() and str(link.resolve()) == m['uicc_link'], 'UE subscriber link changed; render again')
    if c['mode'] == 'hardware':
        require(m.get('expected_radio') == json.loads(json.dumps(expected_radio(c))), 'expected radio settings changed; render again')
    return m


# --------------------------------------------------------------------------- radio interlock
# Lines printed by radio/USRP/usrp_lib.cpp at the pin while the device is configured, i.e.
# before sample streaming starts.
N = r'-?\d+(?:\.\d+)?'      # OAI prints '%f...': never let the trailing dots into the number
HW_PATTERNS = {
    'tx_gain_request': re.compile(rf'USRP TX_GAIN:\s*(?P<gain>{N}) gain_range:\s*(?P<range>{N}) tx_gain:\s*(?P<att>{N})'),
    'tx_gain': re.compile(rf'Actual TX gain: (?P<v>{N})'),
    'rx_gain': re.compile(rf'Actual RX gain: (?P<v>{N})'),
    'tx_freq': re.compile(rf'Actual TX frequency: (?P<v>{N})GHz'),
    'rx_freq': re.compile(rf'Actual RX frequency: (?P<v>{N})GHz'),
    'tx_rate': re.compile(rf'Actual TX sample rate: (?P<v>{N})MSps'),
    'rx_rate': re.compile(rf'Actual RX sample rate: (?P<v>{N})MSps'),
    'master_clock': re.compile(rf'Actual master clock: (?P<v>{N})MHz'),
    'clock_source': re.compile(rf'Actual clock source (?P<v>\w+)'),
    'time_source': re.compile(rf'Actual time source (?P<v>\w+)'),
    'rx_gain_clipped': re.compile(rf'RX Gain too high'),
}
REQUIRED_HW = ('tx_gain', 'tx_freq', 'rx_freq', 'tx_rate', 'rx_rate', 'master_clock', 'clock_source', 'time_source')


class Interlock:
    """Compares each DU's reported radio settings with the rendered expectation.

    check() returns an abort reason (str) or None. It is a software backstop that reacts to OAI's
    own startup report; it is not an RF power measurement and does not replace fixed attenuators.
    """

    def __init__(self, expected):
        self.e = expected
        self.seen = {}

    def check(self, line):
        for key, rx in HW_PATTERNS.items():
            m = rx.search(line)
            if not m:
                continue
            if key == 'rx_gain_clipped':
                return 'OAI clipped the RX gain (max_rxgain too high for this board)'
            if key == 'tx_gain_request':
                if abs(float(m['range']) - self.e['tx_gain_max_db']) > 0.01:
                    return f"daughterboard TX gain range {m['range']} dB differs from configured {self.e['tx_gain_max_db']} dB"
                if float(m['gain']) > self.e['tx_gain_cap_db'] + 0.25:
                    return f"requested UHD TX gain {m['gain']} dB exceeds cap {self.e['tx_gain_cap_db']} dB"
                continue
            v = m['v']
            self.seen[key] = v
            if key == 'tx_gain' and float(v) > self.e['tx_gain_cap_db'] + 0.25:
                return f'actual UHD TX gain {v} dB exceeds cap {self.e["tx_gain_cap_db"]} dB'
            if key in ('tx_freq', 'rx_freq'):
                want = self.e['tx_freq_hz' if key == 'tx_freq' else 'rx_freq_hz']
                if abs(float(v) * 1e9 - want) > 100e3:
                    return f'{key} {float(v)*1e3:.4f} MHz differs from expected {want/1e6:.4f} MHz'
            if key in ('tx_rate', 'rx_rate') and abs(float(v) * 1e6 - self.e['sample_rate_sps']) > 1e3:
                return f'{key} {v} MS/s differs from expected {self.e["sample_rate_sps"]/1e6:g} MS/s'
            want_mc = self.e.get('master_clock_hz', X300_MASTER_CLOCK_HZ)
            if key == 'master_clock' and want_mc and abs(float(v) * 1e6 - want_mc) > 1e3:
                return f'master clock {v} MHz differs from {want_mc/1e6:g} MHz'
            if key in ('clock_source', 'time_source'):
                want = self.e.get(key, 'external')
                if want and v != want:
                    return f'{key} is {v}, expected {want}'
        return None

    def verified(self):
        need = [k for k in REQUIRED_HW if not (k == 'time_source' and self.e.get('time_source', 'external') is None)]
        return all(k in self.seen for k in need)


def stop_group(proc, grace=10.0, sudo=False):
    """SIGINT (OAI's clean-exit handler), then SIGTERM, then SIGKILL to the child's process group.

    With sudo, OAI runs as root but the sudo front-end keeps the caller's real UID, so a plain SIGINT to
    it is permitted and sudo relays it to OAI. Escalation then uses `sudo -n /usr/bin/kill -SIG -- -PGID`,
    the form a lab sudoers rule can whitelist. (OAI at the pin catches SIGTERM but its exit can stall.)
    """
    if proc.poll() is not None:
        return
    for sig, wait in ((signal.SIGINT, grace), (signal.SIGTERM, 5), (signal.SIGKILL, 5)):
        try:
            if sudo and sig != signal.SIGINT:
                subprocess.run(['sudo', '-n', '/usr/bin/kill', f'-{sig.name[3:]}', '--', f'-{proc.pid}'],
                               capture_output=True, timeout=5)
            elif sudo:
                os.kill(proc.pid, sig)          # the sudo front-end relays it to OAI
            else:
                os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError, subprocess.TimeoutExpired):
            pass
        try:
            proc.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


class _Stop(Exception):
    pass


def _raise_stop(signum, frame):
    raise _Stop(signal.Signals(signum).name)


def run_node(c, directory, node, execute=False, sudo=False):
    m = verify_manifest(c, directory)
    require(node in m['commands'], f'unknown node {node}')
    if node not in ('cu', 'ue'):
        require(node in [x['name'] for x in active_cells(c)],
                f'{node} is not in experiment.active_cells: its radio is not part of this run')
    if node == 'ue':
        require(not c.get('experiment', {}).get('ue_receive_only'),
                'experiment.ue_receive_only: the UE radio is a receive-only instrument in this experiment')
    argv = m['commands'][node]
    expected = m.get('expected_radio', {}).get(node)
    if not execute:
        return {'execute': False, 'argv': (['sudo', '-n'] if sudo else []) + argv, 'interlock': expected}
    validate(c, live=True)
    sha = subprocess.check_output(['git', '-C', c['paths']['oai_root'], 'rev-parse', 'HEAD'], text=True).strip()
    require(sha == lock()['commit'], 'OAI checkout does not match the source pin')
    dirty = subprocess.check_output(['git', '-C', c['paths']['oai_root'], 'diff', '--name-only', 'HEAD'], text=True).strip()
    require(not dirty, 'tracked OAI edits detected; qualify and update adapter/pin before use')
    require(Path(argv[0]).is_file(), 'OAI binary missing; build it first')
    if c['mode'] == 'hardware' and node != 'cu':
        hardware_host_preflight(c)
        require((Path(argv[0]).parent / 'liboai_usrpdevif.so').is_file(),
                'liboai_usrpdevif.so missing: rebuild OAI with -w USRP (docs/RUNBOOK.md step 2)')
    work = Path(directory).resolve() / 'logs' / node
    work.mkdir(parents=True, exist_ok=True)
    log = work / f'{time.time_ns()}.jsonl'
    host = socket.gethostname()
    append(log, {'kind': 'process_start', 'node': node, 'argv': argv, 'epoch_ns': time.time_ns(),
                 'binary_sha256': hashlib.sha256(Path(argv[0]).read_bytes()).hexdigest(), 'clock_id': host,
                 'interlock': expected})
    env = os.environ.copy()
    env['LD_LIBRARY_PATH'] = str(Path(argv[0]).parent) + ':' + env.get('LD_LIBRARY_PATH', '')
    # sudo drops LD_LIBRARY_PATH; OAI finds its modules through its RPATH/working layout anyway.
    launch = (['sudo', '-n'] + argv) if sudo else argv
    proc = subprocess.Popen(launch, cwd=work, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1, start_new_session=True, errors='replace')
    append(log, {'kind': 'process_group', 'node': node, 'pgid': proc.pid, 'epoch_ns': time.time_ns()})
    lock_ = Interlock(expected) if expected else None
    announced = False
    rc = None
    # Background shells start us with SIGINT ignored and SIGTERM/SIGHUP would kill Python without cleanup,
    # orphaning a root-owned radio process. Route all three into the shutdown path below.
    previous = {s: signal.signal(s, _raise_stop) for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    try:
        for line in proc.stdout:
            append(log, {'kind': 'oai_log', 'epoch_ns': time.time_ns(), 'monotonic_ns': time.monotonic_ns(),
                         'clock_id': host, 'line': line.rstrip()})
            print(line, end='', flush=True)
            if lock_:
                reason = lock_.check(line)
                if reason:
                    append(log, {'kind': 'interlock_abort', 'epoch_ns': time.time_ns(), 'reason': reason})
                    print(f'\n*** INTERLOCK: {reason} -> stopping {node} ***\n', file=sys.stderr, flush=True)
                    stop_group(proc, grace=2.0, sudo=sudo)
                    rc = 3
                    break
                if not announced and lock_.verified():
                    announced = True
                    append(log, {'kind': 'radio_verified', 'epoch_ns': time.time_ns(), 'seen': lock_.seen})
                    print(f'\n*** {node}: radio settings verified against the rendered config ***\n', file=sys.stderr, flush=True)
        if rc is None:
            rc = proc.wait()
    except (KeyboardInterrupt, _Stop) as e:
        rc = 130
        append(log, {'kind': 'stop_requested', 'epoch_ns': time.time_ns(), 'signal': str(e) or 'SIGINT'})
    finally:
        signal.signal(signal.SIGINT, signal.SIG_IGN)       # do not interrupt our own cleanup
        stop_group(proc, sudo=sudo)
        append(log, {'kind': 'process_exit', 'returncode': proc.returncode, 'epoch_ns': time.time_ns()})
        for s, h in previous.items():
            signal.signal(s, h)
    return {'returncode': rc, 'log': str(log)}


# --------------------------------------------------------------------------- telnet control
def telnet_cmd(host, port, cmd, timeout=3.0):
    """OAI's line-oriented telnet: drain the banner, send one command, read the reply."""
    raw = bytearray()
    with socket.create_connection((host, port), timeout=3) as s:
        s.settimeout(0.2)
        try:
            s.recv(8192)
        except socket.timeout:
            pass
        s.sendall((cmd + '\n').encode())
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                part = s.recv(8192)
                if not part:
                    break
                raw.extend(part)
            except socket.timeout:
                if raw:
                    break
    return raw.decode(errors='replace')


def serving_du(c, ue_id=None):
    """`ci fetch_du_by_ue_id` (telnetsrv_ci.c at the pin) -> (gNB_DU_id, cell dict or None, raw reply)."""
    cu = c['cu']
    reply = telnet_cmd(cu['telnet_ip'], cu['telnet_port'], 'ci fetch_du_by_ue_id' + (f' {int(ue_id)}' if ue_id is not None else ''))
    m = re.search(r'gNB_DU_id (\d+) is connected to ue_id (\d+)', reply)
    if not m:
        return None, None, reply
    du = int(m[1])
    return du, next((x for x in c['cells'] if x['du_id'] == du), None), reply


def trigger(c, node=None, ue_id=None, target_pci=None, execute=False, journal='trigger.jsonl', settle_s=2.0):
    """Telnet control stays local; use an SSH tunnel for a remote CU/gNB."""
    pcis = [x['pci'] for x in c['cells']]
    if target_pci is not None:
        require(target_pci in pcis, 'target PCI not configured')
    if ue_id is not None:
        require(ue_id >= 0, 'invalid UE ID')
    require(len(active_cells(c)) >= 2, 'handover needs two active cells (experiment.active_cells lists fewer)')
    if c['topology'] == 'f1':
        endpoint = c['cu']
        # The pinned OAI only has `trigger_f1_ho [rrc_ue_id]`: the CU moves the UE to the next DU.
        # With two DUs that is always "the other cell", so a target PCI can be checked, not chosen.
        require(target_pci is None or len(c['cells']) == 2,
                'this OAI pin cannot target a PCI; with more than two DUs omit --target-pci')
        cmd = 'ci trigger_f1_ho' + (f' {int(ue_id)}' if ue_id is not None else '')
    else:
        require(node is not None and ue_id is not None and target_pci is not None, 'N2 needs source --node, --ue-id, --target-pci')
        endpoint = next((x for x in c['cells'] if x['name'] == node), None)
        require(endpoint is not None, 'unknown source node')
        cmd = f'ci trigger_n2_ho {int(target_pci)},{int(ue_id)}'
    host, port = endpoint['telnet_ip'], endpoint['telnet_port']
    require(ipaddress.ip_address(host).is_loopback, 'control endpoint must be loopback; use a local SSH tunnel')
    row = {'kind': 'trigger_request', 'epoch_ns': time.time_ns(), 'command': cmd, 'host': host, 'port': port,
           'executed': execute, 'ue_id': ue_id, 'expected_target_pci': target_pci, 'handover_completed': None}
    if not execute:
        return row
    validate(c, live=True)
    if c['mode'] == 'hardware':
        require(c['safety']['single_cell_attach_verified'] is True, 'verify independent attachment and traffic on each cell before handover')
    if c['topology'] == 'f1':
        du, cell, reply = serving_du(c, ue_id)
        row.update(pre_du_id=du, pre_pci=cell['pci'] if cell else None, pre_reply=reply.strip()[-300:])
        require(du is not None, 'CU reports no single attached UE (pass --ue-id from nrRRC_stats.log)')
        require(target_pci is None or cell is None or cell['pci'] != target_pci, f'UE already served by PCI {target_pci}')
        row['epoch_ns'] = time.time_ns()
    # Journal the send BEFORE the CU reacts (~1 ms later), so live views can attribute the handover.
    append(journal, {'kind': 'trigger_sent', 'epoch_ns': row['epoch_ns'], 'command': cmd, 'ue_id': ue_id,
                     'expected_target_pci': target_pci})
    reply = telnet_cmd(host, port, cmd)
    row['response'] = reply
    row['response_received_epoch_ns'] = time.time_ns()
    # telnetsrv_ci.c at the pin: "RRC F1 handover triggered for UE %u" / "RRC N2 handover triggered for UE %u ...".
    # This acknowledges the REQUEST only; completion is the CU log line "handover for UE x/RNTI y complete!".
    acknowledged = 'handover triggered for UE' in reply
    if acknowledged and c['topology'] == 'f1':
        time.sleep(settle_s)
        du, cell, post = serving_du(c, ue_id)
        row.update(post_du_id=du, post_pci=cell['pci'] if cell else None, post_reply=post.strip()[-300:],
                   du_changed=(du is not None and du != row.get('pre_du_id')))
    append(journal, row)
    require(bool(reply), 'no control response; inspect CU/gNB logs')
    require(acknowledged, 'trigger not acknowledged; inspect response journal')
    return row


# --------------------------------------------------------------------------- environment checks
def uhd_bin(c, name):
    p = Path(c['paths']['uhd_prefix']) / 'bin' / name
    return str(p) if p.exists() else (shutil.which(name) or name)


def uhd_python_path(c):
    for d in sorted(glob.glob(str(Path(c['paths']['uhd_prefix']) / 'lib' / 'python3*' / '*-packages'))):
        if (Path(d) / 'uhd').is_dir():
            return d
    return None


def inventory(c, execute=False):
    require(not execute or c['mode'] == 'hardware', 'inventory is hardware inspection; use mode=hardware')
    commands = [[uhd_bin(c, 'uhd_config_info'), '--version']]
    for x in active_cells(c):
        commands.append([uhd_bin(c, 'uhd_usrp_probe'), '--args', f"type=x300,addr={x['usrp_ip']}"])
    if c['mode'] == 'hardware' and c['ue']['kind'] == 'oai_nrue':
        args = c['ue_radio']['usrp_args']
        if args not in (None, '', 'TODO'):
            commands.append([uhd_bin(c, 'uhd_usrp_probe'), '--args', args])
    results = []
    for argv in commands:
        row = {'argv': argv}
        if execute:
            r = subprocess.run(argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
            row.update(returncode=r.returncode, output=r.stdout)
        results.append(row)
    return results


def clock_check(c, execute=False):
    """OctoClock status, per-radio external lock + PPS, then a two-radio common-PPS test. OAI must be stopped."""
    require(not execute or c['mode'] == 'hardware', 'clock-check is hardware inspection; use mode=hardware')
    pypath = uhd_python_path(c)
    script = str(ADAPTERS / 'check_clock.py')
    py = '/usr/bin/python3' if Path('/usr/bin/python3').exists() else sys.executable
    runs = [[py, '-I', script, '--octoclock', c['clock']['octoclock_ip']]]
    cells = active_cells(c)
    runs += [[py, '-I', script, '--args', f"type=x300,addr={x['usrp_ip']}"] for x in cells]
    if len(cells) >= 2:
        runs.append([py, '-I', script, '--pair'] + [x['usrp_ip'] for x in cells[:2]])
    ur = c.get('ue_radio') or {}
    if c['ue']['kind'] == 'oai_nrue' and ur.get('clock_source') == 'external':
        runs.append([py, '-I', script, '--args', ur['usrp_args'], '--reference-only'])
    if execute:
        require(_no_oai_running(), 'stop every OAI process before clock-check')
    out = {'uhd_python_path': pypath, 'checks': []}
    for argv in runs:
        row = {'argv': argv}
        if execute:
            require(pypath, f"UHD Python bindings not found under {c['paths']['uhd_prefix']}")
            # -I ignores PYTHONPATH, so pass the matching bindings explicitly.
            argv = argv[:2] + [script, '--uhd-python', pypath] + argv[3:]
            r = subprocess.run(argv, text=True, capture_output=True, timeout=90)
            row.update(returncode=r.returncode, stdout=r.stdout[-4000:], stderr=r.stderr[-2000:])
            try:
                row['result'] = json.loads(r.stdout)
            except ValueError:
                pass
        out['checks'].append(row)
    if execute:
        # The OctoClock distributes 10 MHz/PPS without any host control, and older firmware does not
        # answer UHD at all, so its status query is informational. Radio lock + common PPS decide.
        out['checks'][0]['informational'] = True
        out['passed'] = all(x.get('returncode') == 0 for x in out['checks'][1:])
    return out


def _run(argv, timeout=10):
    try:
        r = subprocess.run(argv, text=True, capture_output=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, str(e)


def hardware_host_preflight(c):
    """Check actual routes/link speeds before OAI can own a radio; never opens UHD."""
    radios = [(x['name'], x['usrp_ip'], x['usrp_link']) for x in active_cells(c)]
    if c['ue']['kind'] == 'oai_nrue' and c.get('ue_radio'):
        ur = c['ue_radio']
        addr = re.search(r'(?:^|,)addr=([^,]+)', ur['usrp_args'])
        if addr:
            radios.append(('ue', addr[1], ur['usrp_link']))
    rows = []
    for name, addr, link in radios:
        require(link and (Path('/sys/class/net') / link).exists(), f'{name}: configured host interface {link!r} is absent')
        rc, txt = _run(['ip', '-j', 'route', 'get', addr])
        require(rc == 0, f'{name}: route inspection failed: {txt}')
        routes = json.loads(txt)
        require(routes and routes[0].get('dev') == link, f'{name}: actual route to {addr} differs from usrp_link {link}: {txt}')
        sysd = Path('/sys/class/net') / link
        require((sysd / 'operstate').read_text().strip() == 'up', f'{name}: {link} is not up')
        try:
            speed = int((sysd / 'speed').read_text())
        except (ValueError, OSError):
            speed = -1
        require(speed >= c['host']['links'][link]['mbps'], f'{name}: actual {link} speed {speed} is below the configured budget')
        rows.append({'node': name, 'address': addr, 'interface': link, 'speed_mbps': speed, 'route': routes[0]})
    return rows


def doctor(c, ping=False):
    """Read-only preflight of this host. Never opens a USRP session or changes anything."""
    rows = []

    def add(check, status, detail):
        rows.append({'check': check, 'status': status, 'detail': detail})

    root = Path(c['paths']['oai_root'])
    pin = lock()['commit']
    rc, head = _run(['git', '-C', str(root), 'rev-parse', 'HEAD'])
    add('oai_checkout_pin', 'ok' if head == pin else 'fail', f'HEAD {head[:12]} vs pin {pin[:12]}')
    rc, dirty = _run(['git', '-C', str(root), 'diff', '--name-only', 'HEAD'])
    add('oai_checkout_clean', 'ok' if rc == 0 and not dirty else 'fail', dirty or 'no tracked edits')
    build = Path(c['paths']['nr_softmodem']).parent
    add('nr_softmodem', 'ok' if Path(c['paths']['nr_softmodem']).is_file() else 'fail', c['paths']['nr_softmodem'])
    tel = all((build / f).is_file() for f in ('libtelnetsrv.so', 'libtelnetsrv_ci.so'))
    add('telnet_libs', 'ok' if tel else 'fail', 'libtelnetsrv + ci module' if tel else 'rebuild with --build-lib telnetsrv')
    usrp = build / 'liboai_usrpdevif.so'
    if c['mode'] == 'hardware':
        add('oai_usrp_driver', 'ok' if usrp.is_file() else 'fail',
            str(usrp) if usrp.is_file() else 'not built: ./build_oai --ninja -w USRP --gNB --nrUE --build-lib telnetsrv '
            f"--cmake-opt -DUHD_DIR={c['paths']['uhd_prefix']}/lib/cmake/uhd   (NO -I: it apt-removes UHD)")
        if usrp.is_file():
            rc, ldd = _run(['ldd', str(usrp)])
            lib = next((ln.split('=>')[1].split()[0] for ln in ldd.splitlines() if 'libuhd' in ln and '=>' in ln), '')
            good = lib.startswith(str(Path(c['paths']['uhd_prefix'])))
            add('oai_links_uhd', 'ok' if good else 'fail', f'{lib or "libuhd not found"} (expected under {c["paths"]["uhd_prefix"]})')
    rc, ver = _run([uhd_bin(c, 'uhd_config_info'), '--version'])
    add('uhd_version', 'ok' if rc == 0 else 'fail', ver.splitlines()[-1] if ver else 'uhd_config_info missing')
    rc, dpkg = _run(['dpkg-query', '-W', '-f', '${db:Status-Abbrev} ${Package} ${Version}\\n', 'libuhd*', 'uhd-host', 'python3-uhd'])
    apt = [' '.join(ln.split()[1:]) for ln in dpkg.splitlines() if ln.startswith('ii')]
    local = glob.glob(str(Path(c['paths']['uhd_prefix']) / 'lib' / 'libuhd.so.*'))
    if apt and local:
        add('uhd_duplicates', 'warn', f'apt UHD ({"; ".join(apt)}) AND {local[0]}: always use {c["paths"]["uhd_prefix"]}/bin tools; '
            'plain `python3 -c "import uhd"` loads the apt bindings')
    images = Path(c['paths']['uhd_prefix']) / 'share' / 'uhd' / 'images' / 'usrp_x310_fpga_HG.bit'
    add('x310_images', 'ok' if images.exists() else 'warn', str(images))
    pyp = uhd_python_path(c)
    clock_api = bool(pyp) and bool(glob.glob(str(Path(pyp) / 'uhd' / 'usrp_clock*')))
    add('uhd_python', 'ok' if clock_api else 'warn',
        f'{pyp} (has OctoClock API)' if clock_api else f'{pyp or "not found"}: no uhd.usrp_clock, OctoClock check unavailable')
    rc, virt = _run(['systemd-detect-virt'])
    if virt and virt != 'none':
        add('virtualization', 'warn', f'{virt} guest: real-time X310 streaming must be qualified (benchmark first)')
    rc, addrs = _run(['ip', '-j', 'addr'])
    try:
        ifs = {i['ifname']: i for i in json.loads(addrs)}
    except ValueError:
        ifs = {}
    if c['mode'] == 'hardware':
        try:
            for row in hardware_host_preflight(c):
                add(f"{row['node']} transport route", 'ok', f"{row['address']} via {row['interface']} at {row['speed_mbps']} Mbit/s")
        except (ValueError, OSError) as e:
            add('active radio transport routes', 'fail', str(e))
    local_ips = {a['local'] for i in ifs.values() for a in i.get('addr_info', [])}
    n2 = str(ipaddress.ip_interface(c['cu']['n2_ip']).ip)
    add('cu_n2_address', 'ok' if n2 in local_ips else 'fail', f'{n2} {"present" if n2 in local_ips else "missing: start the core (docker compose up -d)"}')
    rc, ps = _run(['docker', 'ps', '--format', '{{.Names}}'])
    up = set(ps.split()) if rc == 0 else set()
    need = {'oai-amf', 'oai-smf', 'oai-upf', 'oai-nrf', 'oai-udm', 'oai-udr', 'oai-ausf', 'mysql'}
    add('core_containers', 'ok' if need <= up else 'warn',
        f'docker inspection failed: {ps}' if rc != 0 else
        (f'missing: {sorted(need - up)}' if need - up else 'all running'))
    try:
        lo, hi = map(int, Path('/proc/sys/net/ipv4/ping_group_range').read_text().split())
        ok = lo <= os.getgid() <= hi
    except (OSError, ValueError):
        ok = False
    add('icmp_probe', 'ok' if ok else 'info', 'unprivileged ICMP allowed' if ok else
        "ICMP probe needs sudo, or: sudo sysctl -w net.ipv4.ping_group_range='0 2147483647'")
    if ping:
        targets = {x['name']: x['usrp_ip'] for x in active_cells(c)}
        if c['ue']['kind'] == 'oai_nrue' and c.get('ue_radio'):
            addr = re.search(r'(?:^|,)addr=([^,]+)', c['ue_radio']['usrp_args'])
            if addr:
                targets['ue'] = addr[1]
        targets['octoclock'] = c['clock']['octoclock_ip']
        targets['amf'] = c['core']['amf_ip']
        for name, ip in targets.items():
            rc, _ = _run(['ping', '-c', '2', '-W', '1', '-q', ip], timeout=6)
            add(f'ping {name}', 'ok' if rc == 0 else 'warn', f'{ip} {"answers" if rc == 0 else "no answer (powered off?)"}')
    return {'passed': not any(r['status'] == 'fail' for r in rows), 'checks': rows}


def rx_check(c, execute=False, gains=None, antennas='RX2,TX/RX', out=None, seconds=0.25):
    """RECEIVE-ONLY noise/level/spectrum check per radio at the profile's UL and DL frequencies.

    Uses adapters/rx_check.py with the configured UHD bindings. It never creates a TX streamer.
    """
    require(not execute or c['mode'] == 'hardware', 'rx-check receives from hardware; use mode=hardware')
    from .profiles import frequencies, oai_gains
    f = frequencies(profile(c))
    freqs = ','.join(str(round(v)) for v in dict.fromkeys((f['ul_center_hz'], f['dl_center_hz'])))
    py = '/usr/bin/python3' if Path('/usr/bin/python3').exists() else sys.executable
    runs = []
    for x in active_cells(c):
        planned = oai_gains(x, profile(c))['uhd_rx_gain_db']
        g = gains or f'0,15,{planned:g}'
        args = f"type=x300,addr={x['usrp_ip']}" + (f",serial={x['serial']}" if x['serial'] not in (None, '', 'UNKNOWN') else '')
        runs.append({'cell': x['name'], 'argv': [py, '-I', str(ADAPTERS / 'rx_check.py'), '--args', args, '--subdev', x['subdev'],
                                                 '--freqs', freqs, '--gains', g, '--antennas', antennas, '--seconds', str(seconds),
                                                 '--rate', str(profile(c)['sample_rate_sps'])]})
    if c['ue']['kind'] == 'oai_nrue' and c.get('ue_radio'):
        ur = c['ue_radio']
        planned = ur['rx_gain'] - x310_rx_cal_offset(f['dl_center_hz'])
        runs.append({'cell': 'ue', 'argv': [py, '-I', str(ADAPTERS / 'rx_check.py'), '--args', ur['usrp_args'],
                     '--subdev', ur['subdev'], '--freqs', freqs, '--gains', gains or f'0,15,{planned:g}',
                     '--antennas', antennas, '--seconds', str(seconds), '--rate', str(profile(c)['sample_rate_sps'])]})
    if not execute:
        return {'execute': False, 'transmits': False, 'runs': runs}
    require(_no_oai_running(), 'OAI is running: stop every gNB and UE first (one UHD owner per radio)')
    pypath = uhd_python_path(c)
    require(pypath, f"UHD Python bindings not found under {c['paths']['uhd_prefix']}")
    result = {'transmits': False, 'radios': []}
    for r in runs:
        argv = r['argv'][:3] + ['--uhd-python', pypath] + r['argv'][3:]
        p = subprocess.run(argv, text=True, capture_output=True, timeout=180)
        require(p.returncode == 0, f"{r['cell']}: rx_check failed: {p.stderr[-600:]}")
        d = json.loads(p.stdout.strip().splitlines()[-1]); d['cell'] = r['cell']
        result['radios'].append(d)
    if out:
        o = Path(out); o.mkdir(parents=True, exist_ok=False)
        (o / 'rx_check.json').write_text(json.dumps(result, indent=1) + '\n')
        try:
            from .analysis import rx_check_figure
            rx_check_figure(result, o / 'rx_check.png', f)
        except ImportError:
            pass
        result['saved'] = str(o)
    return result


# --------------------------------------------------------------------------- experiment 1 tools
def _no_oai_running():
    rc, ps = _run(['pgrep', '-a', '-f', 'nr-softmodem|nr-uesoftmodem'])
    return rc != 0


def _cell_args(x):
    return f"type=x300,addr={x['usrp_ip']}" + (f",serial={x['serial']}" if x['serial'] not in (None, '', 'UNKNOWN') else '')


def tone_cal(c, execute=False, dry_run=False, out=None, rx_gain=20.0):
    """Loopback calibration: the single active cell sends a -20 dBFS tone at TX gain 0 dB; its own RX2 and
    the UE radio listen. Yields the cable-path level and the cell's own TX/RX->RX2 isolation."""
    require(not execute or c['mode'] == 'hardware', 'tone-cal operates hardware; use mode=hardware')
    from .profiles import frequencies
    cells = active_cells(c)
    require(len(cells) == 1, 'tone-cal: experiment.active_cells must list exactly one (transmitting) cell')
    require(c['safety']['rf_setup'] == 'conducted', 'tone-cal is for the cabled bench (safety.rf_setup: conducted)')
    require('ue_radio' in c and c['ue_radio']['usrp_args'] not in (None, '', 'TODO'), 'tone-cal: fill ue_radio (the listening radio)')
    x, ur = cells[0], c['ue_radio']
    f = frequencies(profile(c))['dl_center_hz']
    py = '/usr/bin/python3' if Path('/usr/bin/python3').exists() else sys.executable
    argv = [py, '-I', str(ADAPTERS / 'tone_cal.py'), '--tx-args', _cell_args(x), '--tx-subdev', x['subdev'],
            '--rx', f"ue|{ur['usrp_args']}|{ur['subdev'] or 'A:0'}", '--freq', f'{f:.0f}', '--rx-gain', f'{rx_gain:g}']
    if dry_run:
        argv.append('--dry-run')
    plan = {'transmitter': x['name'], 'tx_gain_db': 0, 'tone_dbfs': -20, 'nominal_connector_dbm': -31.5, 'seconds': 2,
            'listeners': [f"{x['name']} RX2", 'ue RX2'], 'transmits': not dry_run}
    if not execute:
        return {'execute': False, 'plan': plan, 'argv': argv}
    require(_no_oai_running(), 'stop every OAI process first (one UHD owner per radio)')
    pypath = uhd_python_path(c)
    require(pypath, 'UHD Python bindings not found')
    argv = argv[:3] + ['--uhd-python', pypath] + argv[3:]
    p = subprocess.run(argv, text=True, capture_output=True, timeout=120)
    lines = [ln for ln in p.stdout.splitlines() if ln.startswith('{')]
    require(lines, f'tone_cal produced no result: {p.stderr[-600:]}')
    r = json.loads(lines[-1]); r['plan'] = plan
    require(p.returncode == 0, f"tone_cal refused/failed: {r.get('error') or p.stderr[-400:]}")
    if not dry_run:
        # Cable-path loss as entered for the UE receiver (pads), used to turn the level difference into isolation.
        ue_rx = next((rx for rx in c['safety']['receivers'] if rx['name'] == 'ue'), {})
        path = next((pp.get('minimum_passive_loss_db') for pp in ue_rx.get('paths', []) if pp['source'] == x['name']), None)
        me, far = r['receivers']['self'], r['receivers']['ue']
        d = {'cable_path_loss_assumed_db': path, 'far_tone_detected': far['detected'], 'self_tone_detected': me['detected']}
        if path is not None and far['detected']:
            if me['detected']:
                d['self_isolation_db'] = round(path + far['tone_dbfs'] - me['tone_dbfs'], 1)
            else:   # not visible above the floor: only a lower bound
                d['self_isolation_lower_bound_db'] = round(path + far['tone_dbfs'] - (me['baseline_tone_bin_dbfs'] + 6), 1)
        r['derived'] = d
    if out:
        o = Path(out); o.mkdir(parents=True, exist_ok=False)
        (o / 'cal.json').write_text(json.dumps(r, indent=2) + '\n')
        r['saved'] = str(o / 'cal.json')
    return r


def rx_monitor(c, out, gain=28.0, duration=60.0, interval=1.0, antenna='RX2', execute=False):
    """RECEIVE-ONLY live monitor of the active cell on the UE radio; JSONL to `out`, one summary line per second."""
    require(not execute or c['mode'] == 'hardware', 'rx-monitor receives from hardware; use mode=hardware')
    from .profiles import frequencies
    require('ue_radio' in c and c['ue_radio']['usrp_args'] not in (None, '', 'TODO'), 'rx-monitor: fill ue_radio')
    f = frequencies(profile(c))
    ur, pci = c['ue_radio'], active_cells(c)[0]['pci']
    py = '/usr/bin/python3' if Path('/usr/bin/python3').exists() else sys.executable
    argv = [py, '-I', str(ADAPTERS / 'rx_monitor.py'), '--args', ur['usrp_args'],
            '--subdev', ur['subdev'] or 'A:0', '--freq', f'{f["dl_center_hz"]:.0f}',
            '--ssb-offset-hz', f'{f["ssb_hz"] - f["dl_center_hz"]:.0f}', '--gain', f'{gain:g}', '--antenna', antenna,
            '--interval', f'{interval:g}', '--duration', f'{duration:g}', '--expected-pci', str(pci),
            '--rate', str(profile(c)['sample_rate_sps']), '--scs-khz', str(profile(c)['scs_khz'])]
    if not execute:
        return {'execute': False, 'transmits': False, 'argv': argv, 'output': str(out)}
    rc, _ = _run(['pgrep', '-f', 'nr-uesoftmodem'])
    require(rc != 0, 'the OAI UE is using the UE radio; stop it first')
    pypath = uhd_python_path(c)
    require(pypath, 'UHD Python bindings not found')
    argv = argv[:3] + ['--uhd-python', pypath] + argv[3:]
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
    previous = {s: signal.signal(s, _raise_stop) for s in (signal.SIGINT, signal.SIGTERM)}
    rows = 0
    try:
        with open(out, 'x') as fh:
            for line in proc.stdout:
                if not line.startswith('{'):
                    continue
                fh.write(line); fh.flush()
                r = json.loads(line)
                if r.get('kind') == 'rxmon':
                    rows += 1
                    pss = (f"PSS FOUND N_ID2={r['pss_nid2']} rho={r['pss_rho']:.2f} period={r['pss_period_ms']} ms"
                           if r.get('pss_detected') else f"PSS not found (best rho={r.get('pss_rho', 0):.2f})")
                    print(f"{time.strftime('%H:%M:%S')}  {pss} | SSB burst {r.get('burst_dbfs')} dBFS "
                          f"(+{r.get('burst_above_floor_db')} dB over floor) | peak {r.get('peak_dbfs')} dBFS, "
                          f"headroom {r.get('headroom_db')} dB | stream errors {r['stream_errors']}", flush=True)
                elif r.get('kind') == 'rxmon_start':
                    print(f"receive-only monitor on {r['args']} at {r['freq_hz']/1e6:.2f} MHz, gain {r['gain_db']} dB, "
                          f"antenna {r['antenna']}, 10 MHz locked: {r['ref_locked']}", flush=True)
        require(proc.wait() == 0 and rows > 0, 'rx-monitor failed or returned no captures; inspect UHD bindings and radio link')
    except (KeyboardInterrupt, _Stop):
        proc.terminate(); proc.wait(timeout=10)
    finally:
        for s, h in previous.items():
            signal.signal(s, h)
    return {'output': str(out), 'measurements': rows, 'transmits': False}
