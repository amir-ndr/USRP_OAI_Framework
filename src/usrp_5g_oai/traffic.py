"""Sequence-numbered RTT probes; one host's monotonic clock measures RTT.

UDP mode needs `oaiho echo` on the far end. ICMP mode needs nothing on the UE: run it on the
core host towards the UE's PDU-session address (route 10.0.0.0/16 via the UPF), which is the
practical choice for an iPhone or a 5G router.
"""
import json
import ipaddress
import math
import os
import re
import selectors
import shutil
import socket
import struct
import subprocess
import time
from .config import require
from .runtime import append

MAGIC = b'OAIH'
HEADER = struct.Struct('!4sQ')


def probe_route(host, device=None):
    """Record the kernel route and reject a local PDU address that would bypass the UE radio."""
    ip = socket.gethostbyname(host)
    if ipaddress.ip_address(ip).is_loopback:
        return {'destination': ip, 'status': 'loopback_test', 'radio_path_verified': False}
    argv = ['ip', '-j', 'route', 'get', ip]
    if device:
        argv += ['oif', device]
    try:
        r = subprocess.run(argv, text=True, capture_output=True, timeout=5)
        require(r.returncode == 0, f'cannot inspect probe route: {r.stderr.strip()}')
        routes = json.loads(r.stdout)
    except FileNotFoundError:
        return {'destination': ip, 'status': 'ip_tool_unavailable', 'radio_path_verified': False}
    require(routes, 'no probe route returned')
    route = routes[0]
    require(route.get('type') != 'local',
            f'{ip} is local to this network namespace; probing it bypasses the radios. '
            'Use a separate UE host/network namespace (docs/PROJECT_REVIEW.md).')
    return {'destination': ip, 'status': 'route_recorded', 'route': route, 'radio_path_verified': False}


def echo_server(bind, port):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind((bind, port))
        try:
            while True:
                data, addr = s.recvfrom(65535)
                if len(data) >= HEADER.size and data[:4] == MAGIC:
                    s.sendto(data, addr)
        except KeyboardInterrupt:
            return


def checksum(data):
    if len(data) % 2:
        data += b'\0'
    total = sum(struct.unpack(f'!{len(data)//2}H', data))
    total = (total >> 16) + (total & 0xffff)
    total += total >> 16
    return ~total & 0xffff


class IcmpChannel:
    """Echo request/reply over an unprivileged ICMP datagram socket, or a raw socket as root."""

    def __init__(self, host, bind=None):
        self.host = host
        try:
            self.s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP)
            self.raw = False
        except PermissionError:
            try:
                self.s = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)
                self.raw = True
            except PermissionError:
                raise PermissionError("ICMP probe needs root, or allow unprivileged ping sockets until reboot with: "
                                      "sudo sysctl -w net.ipv4.ping_group_range='0 2147483647'") from None
        self.ident = os.getpid() & 0xffff
        if bind:
            self.s.bind((bind, 0))
        self.s.connect((host, 0))

    def send(self, seq, size):
        payload = HEADER.pack(MAGIC, seq) + bytes(max(0, size - HEADER.size))
        head = struct.pack('!BBHHH', 8, 0, 0, self.ident, seq & 0xffff)
        pkt = head + payload
        pkt = pkt[:2] + struct.pack('!H', checksum(pkt)) + pkt[4:]
        self.s.send(pkt)

    def recv(self):
        data = self.s.recv(65535)
        if self.raw:
            data = data[(data[0] & 0x0f) * 4:]          # strip IPv4 header
            if len(data) < 8 or data[0] != 0 or struct.unpack('!H', data[4:6])[0] != self.ident:
                return None
        elif len(data) < 8 or data[0] != 0:
            return None
        body = data[8:]
        if len(body) >= HEADER.size and body[:4] == MAGIC:
            return HEADER.unpack_from(body)[1]
        return None


class UdpChannel:
    def __init__(self, host, port, bind):
        self.s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if bind:
            self.s.bind((bind, 0))
        self.s.connect((host, port))

    def send(self, seq, size):
        self.s.send(HEADER.pack(MAGIC, seq) + bytes(size - HEADER.size))

    def recv(self):
        data = self.s.recv(65535)
        if len(data) >= HEADER.size:
            magic, i = HEADER.unpack_from(data)
            if magic == MAGIC:
                return i
        return None


PING_REPLY = re.compile(r'^\[(?P<ts>\d+\.\d+)\] \d+ bytes from [^:]+: icmp_seq=(?P<seq>\d+) .*?time=(?P<rtt>[\d.]+) ms(?P<dup> \(DUP!\))?')
PING_NOANSWER = re.compile(r'^\[(?P<ts>\d+\.\d+)\] no answer yet for icmp_seq=(?P<seq>\d+)')


def ping_probe(host, output, duration=60., interval=0.02, timeout=1., device=None, size=128):
    """Drive the system `ping` (it carries cap_net_raw), so no root or sysctl change is needed.

    RTTs are ping's own. Send times are reply time minus RTT; unanswered probes get the scheduled time.
    Timestamps are CLOCK_REALTIME (ping -D), used for both 'monotonic' fields within this one run.
    """
    require(duration > 0 and interval >= 0.005 and timeout > 0 and 16 <= size <= 1400, 'invalid probe settings')
    require(shutil.which('ping'), 'ping not found')
    route = probe_route(host, device)
    argv = ['ping', '-D', '-n', '-O', '-i', f'{interval:g}', '-w', str(math.ceil(duration)), '-s', str(size - 8), host]
    if device:
        argv[1:1] = ['-I', device]
    with open(output, 'x') as f:
        f.write(json.dumps({'kind': 'metadata', 'synthetic': False, 'epoch_ns': time.time_ns(), 'interval_s': interval,
                            'timeout_s': timeout, 'payload_bytes': size, 'method': 'ping', 'remote': host, 'device': device,
                            'argv': argv, 'clock_id': socket.gethostname(), 'route_check': route,
                            'time_source': 'CLOCK_REALTIME via ping -D'}) + '\n')
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    pending, done, origin, highest = {}, set(), None, 0

    def emit(seq, sent_s, recv_s=None):
        done.add(seq)
        sent = round(sent_s * 1e9)
        append(output, {'kind': 'packet', 'seq': seq, 'send_epoch_ns': sent, 'send_relative_s': sent_s - (origin or sent_s),
                        'send_monotonic_ns': sent, 'recv_monotonic_ns': None if recv_s is None else round(recv_s * 1e9),
                        'rtt_ms': None if recv_s is None else (recv_s - sent_s) * 1e3,
                        'status': 'received' if recv_s is not None else 'lost', 'error': None if recv_s is not None else 'timeout'})
    try:
        for line in proc.stdout:
            if m := PING_REPLY.match(line):
                seq, ts, rtt = int(m['seq']), float(m['ts']), float(m['rtt']) / 1e3
                if origin is None:
                    origin = ts - rtt - (seq - 1) * interval
                highest = max(highest, seq)
                if m['dup'] or seq in done:
                    append(output, {'kind': 'late_or_duplicate', 'seq': seq, 'recv_monotonic_ns': round(ts * 1e9)})
                elif rtt > timeout:
                    pending.pop(seq, None); emit(seq, ts - rtt)          # answered after the timeout: lost
                else:
                    pending.pop(seq, None); emit(seq, ts - rtt, ts)
                now = ts
            elif m := PING_NOANSWER.match(line):
                seq, now = int(m['seq']), float(m['ts'])
                highest = max(highest, seq)
                pending.setdefault(seq, now - interval)
            else:
                continue
            for seq, sent_s in list(pending.items()):
                if now - sent_s >= timeout:
                    del pending[seq]; emit(seq, sent_s)
        rc = proc.wait()
        require(rc in (0, 1) and highest > 0,
                f'ping did not produce usable probe records (exit {rc}); check privileges, route and endpoint')
    except KeyboardInterrupt:
        proc.terminate(); proc.wait()
    finally:
        proc.stdout.close()
        for seq, sent_s in pending.items():
            append(output, {'kind': 'packet', 'seq': seq, 'send_epoch_ns': round(sent_s * 1e9), 'send_relative_s': None,
                            'send_monotonic_ns': round(sent_s * 1e9), 'recv_monotonic_ns': None, 'rtt_ms': None, 'status': 'censored'})
    return {'packets_sent': highest, 'output': output, 'method': 'ping'}


def probe(host, port, output, duration=60., interval=0.02, timeout=1., bind=None, size=128, icmp=False):
    require(duration > 0 and interval >= 0.005 and timeout > 0 and HEADER.size <= size <= 1400, 'invalid probe settings')
    route = probe_route(host)
    channel = IcmpChannel(host, bind) if icmp else UdpChannel(host, port, bind)
    # Refuse to mix runs in one file.
    with open(output, 'x') as f:
        f.write(json.dumps({'kind': 'metadata', 'synthetic': False, 'epoch_ns': time.time_ns(),
                            'interval_s': interval, 'timeout_s': timeout, 'payload_bytes': size,
                            'method': 'icmp' if icmp else 'udp', 'remote': host, 'port': None if icmp else port,
                            'clock_id': socket.gethostname(), 'route_check': route}) + '\n')
    s = channel.s
    s.setblocking(False)
    with selectors.DefaultSelector() as sel:
        sel.register(s, selectors.EVENT_READ)
        start = time.monotonic(); end = start + duration; next_send = start; seq = 0; pending = {}

        def finish(i, received=None, error=None):
            sent, epoch, relative = pending.pop(i)
            append(output, {'kind': 'packet', 'seq': i, 'send_epoch_ns': epoch, 'send_relative_s': relative,
                            'send_monotonic_ns': sent, 'recv_monotonic_ns': received,
                            'rtt_ms': None if received is None else (received - sent) / 1e6,
                            'status': 'received' if received is not None else 'lost', 'error': error})
        try:
            while time.monotonic() < end or pending:
                now = time.monotonic()
                if now >= next_send and now < end:
                    seq += 1; mono = time.monotonic_ns()
                    pending[seq] = (mono, time.time_ns(), now - start)
                    try:
                        channel.send(seq, size)
                    except OSError as e:
                        finish(seq, error=str(e))
                    # Never burst to catch up if the host missed a scheduling deadline.
                    next_send = max(next_send + interval, now + interval)
                if sel.select(timeout=min(interval, 0.01)):
                    while True:
                        try:
                            i = channel.recv()
                        except BlockingIOError:
                            break
                        except OSError as e:
                            append(output, {'kind': 'socket_error', 'message': str(e), 'epoch_ns': time.time_ns()}); break
                        received = time.monotonic_ns()
                        if i is None:
                            continue
                        if i in pending:
                            finish(i, received=received)
                        else:
                            append(output, {'kind': 'late_or_duplicate', 'seq': i, 'recv_monotonic_ns': received})
                now_ns = time.monotonic_ns()
                for i, (sent, _, _) in list(pending.items()):
                    if (now_ns - sent) / 1e9 >= timeout:
                        finish(i, error='timeout')
        finally:
            # Interrupted outstanding packets are censored, not declared lost.
            for i, (sent, epoch, relative) in pending.items():
                append(output, {'kind': 'packet', 'seq': i, 'send_epoch_ns': epoch, 'send_relative_s': relative,
                                'send_monotonic_ns': sent, 'recv_monotonic_ns': None, 'rtt_ms': None, 'status': 'censored'})
            s.close()
    return {'packets_sent': seq, 'output': output, 'method': 'icmp' if icmp else 'udp'}
