#!/usr/bin/env python3
"""RECEIVE-ONLY live monitor of an NR cell, run on the UE radio. Nothing is transmitted.

Every interval it captures a short block, then reports (one JSON line per interval on stdout):
  - level: average/peak power in dBFS of the sample stream and headroom to clipping (0 dBFS);
  - bursts: power in 0.25 ms windows, so the 4-symbol SSB bursts stand out from the idle floor;
  - PSS: normalised correlation with the three NR primary synchronisation sequences
    (3GPP TS 38.211 section 7.4.2.2), which identifies N_ID2 = PCI mod 3, and the burst period.
The DSP functions are pure numpy so they can be tested without a radio.
"""
import argparse
import json
import math
import signal
import sys
import time

WINDOW_S = 0.25e-3          # power-envelope window: shorter than an SSB burst (4 symbols ~ 0.29 ms at 15 kHz)
PSS_THRESHOLD = 0.35         # normalised correlation; noise alone stays far below (~0.05-0.2)


def pss_sequence(nid2):
    """d_PSS(n) = 1 - 2 x(m), m = (n + 43 N_ID2) mod 127, x(i+7) = (x(i+4) + x(i)) mod 2."""
    x = [0, 1, 1, 0, 1, 1, 1]                       # x(0..6) from [x(6)..x(0)] = [1 1 1 0 1 1 0]
    while len(x) < 127:
        x.append((x[len(x) - 3] + x[len(x) - 7]) % 2)
    return [1 - 2 * x[(n + 43 * nid2) % 127] for n in range(127)]


def pss_template(nid2, fft_size, offset_sc):
    """Time-domain PSS symbol (no cyclic prefix). PSS occupies SSB subcarriers 56..182; the SSB centre
    (subcarrier 120) sits offset_sc subcarriers above the receiver's centre frequency."""
    import numpy as np
    grid = np.zeros(fft_size, dtype=np.complex64)
    for n, d in enumerate(pss_sequence(nid2)):
        grid[(n + 56 - 120 + offset_sc) % fft_size] = d
    t = np.fft.ifft(grid)
    return (t / np.linalg.norm(t)).astype(np.complex64)


def analyze(x, rate, offset_sc, fft_size=None, expected_nid2=None, scs_khz=15):
    """Level, burst envelope and PSS detections of one capture (complex samples, full scale = 1.0)."""
    import numpy as np
    fft_size = fft_size or int(round(rate / (scs_khz * 1e3)))
    x = np.asarray(x, dtype=np.complex64)
    out = {'samples': int(len(x))}
    pwr = float(np.mean(np.abs(x) ** 2)) + 1e-20
    peak = float(max(np.max(np.abs(x.real)), np.max(np.abs(x.imag)))) + 1e-12
    out.update(avg_dbfs=round(10 * math.log10(pwr), 2), peak_dbfs=round(20 * math.log10(peak), 2),
               headroom_db=round(-20 * math.log10(peak), 2))
    w = max(1, int(rate * WINDOW_S))
    env = np.abs(x[:len(x) // w * w].reshape(-1, w)) ** 2
    env_db = 10 * np.log10(env.mean(axis=1) + 1e-20)
    floor, burst = float(np.percentile(env_db, 20)), float(np.max(env_db))
    out.update(floor_dbfs=round(floor, 2), burst_dbfs=round(burst, 2), burst_above_floor_db=round(burst - floor, 2))
    # Sliding-window energy for the normalised correlation.
    n = fft_size
    cs = np.concatenate([[0.0], np.cumsum(np.abs(x) ** 2)])
    energy = np.sqrt(np.maximum(cs[n:] - cs[:-n], 1e-20))
    nfft = 1 << int(math.ceil(math.log2(len(x) + n)))
    X = np.fft.fft(x, nfft)
    best = None
    for nid2 in (0, 1, 2):
        t = pss_template(nid2, n, offset_sc)
        corr = np.abs(np.fft.ifft(X * np.conj(np.fft.fft(t, nfft)))[:len(x) - n + 1]) / energy
        peak_rho = float(np.max(corr))
        if best is None or peak_rho > best[1]:
            best = (nid2, peak_rho, corr)
    nid2, rho, corr = best
    hits = []
    for i in np.argsort(corr)[::-1]:                 # strongest peaks first, at least 5 ms apart
        if corr[i] < PSS_THRESHOLD:
            break
        if all(abs(int(i) - h) > rate * 5e-3 for h in hits):
            hits.append(int(i))
        if len(hits) >= 64:
            break
    hits.sort()
    gaps = [(b - a) / rate * 1e3 for a, b in zip(hits, hits[1:])]
    out.update(pss_nid2=nid2, pss_rho=round(rho, 3), pss_detections=len(hits),
               pss_period_ms=round(float(np.median(gaps)), 2) if gaps else None,
               pss_detected=rho >= PSS_THRESHOLD,
               expected_nid2=expected_nid2,
               nid2_matches=(expected_nid2 is None or nid2 == expected_nid2) if rho >= PSS_THRESHOLD else None)
    # 200 values of the envelope (decimated) for the report's example trace.
    step = max(1, len(env_db) // 400)
    out['envelope_db'] = [round(float(v), 1) for v in env_db[::step][:400]]
    out['envelope_step_ms'] = WINDOW_S * 1e3 * step
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--uhd-python'); p.add_argument('--args', required=True); p.add_argument('--subdev', default='B:0')
    p.add_argument('--freq', type=float, required=True); p.add_argument('--ssb-offset-hz', type=float, required=True)
    p.add_argument('--rate', type=float, default=7.68e6); p.add_argument('--gain', type=float, default=28.0)
    p.add_argument('--scs-khz', type=int, choices=(15, 30, 60), default=15)
    p.add_argument('--antenna', default='RX2'); p.add_argument('--interval', type=float, default=1.0)
    p.add_argument('--capture-ms', type=float, default=200.0); p.add_argument('--duration', type=float, default=60.0)
    p.add_argument('--expected-pci', type=int)
    a = p.parse_args()
    if a.uhd_python:
        sys.path.insert(0, a.uhd_python)
    import numpy as np
    import uhd
    stop = []
    for s in (signal.SIGINT, signal.SIGTERM):
        signal.signal(s, lambda *_: stop.append(1))
    u = uhd.usrp.MultiUSRP(a.args + ',master_clock_rate=184.32e6')
    u.set_clock_source('external')
    u.set_rx_subdev_spec(uhd.usrp.SubdevSpec(a.subdev))
    u.set_rx_rate(a.rate, 0); rate = u.get_rx_rate(0)
    u.set_rx_freq(uhd.types.TuneRequest(a.freq), 0)
    u.set_rx_gain(a.gain, 0); u.set_rx_antenna(a.antenna, 0)
    sa = uhd.usrp.StreamArgs('fc32', 'sc16'); sa.channels = [0]
    rx = u.get_rx_stream(sa)                          # receive streamer only: no TX streamer exists
    md = uhd.types.RXMetadata()
    buf = np.zeros((1, rx.get_max_num_samps()), dtype=np.complex64)
    n = int(rate * a.capture_ms / 1e3)
    offset_sc = int(round(a.ssb_offset_hz / (a.scs_khz * 1e3)))
    nid2_expected = None if a.expected_pci is None else a.expected_pci % 3
    print(json.dumps({'kind': 'rxmon_start', 'epoch': time.time(), 'args': a.args, 'freq_hz': a.freq,
                      'gain_db': u.get_rx_gain(0), 'antenna': u.get_rx_antenna(0), 'rate': rate,
                      'ref_locked': bool(u.get_mboard_sensor('ref_locked', 0).to_bool()),
                      'transmit': 'never (receive streamer only)'}), flush=True)
    t_end = time.time() + a.duration
    while not stop and time.time() < t_end:
        t0 = time.time()
        cmd = uhd.types.StreamCMD(uhd.types.StreamMode.num_done); cmd.num_samps = n; cmd.stream_now = True
        rx.issue_stream_cmd(cmd)
        x = np.empty(n, dtype=np.complex64); got = 0; errors = 0
        while got < n and time.time() < t0 + a.capture_ms / 1e3 + 2:
            k = rx.recv(buf, md, 1.0)
            if md.error_code != uhd.types.RXMetadataErrorCode.none:
                errors += 1
                if md.error_code == uhd.types.RXMetadataErrorCode.timeout:
                    break
            k = min(k, n - got); x[got:got + k] = buf[0, :k]; got += k
        row = {'kind': 'rxmon', 'epoch': t0, 'stream_errors': errors, 'gain_db': a.gain}
        if got:
            row.update(analyze(x[:got], rate, offset_sc, expected_nid2=nid2_expected, scs_khz=a.scs_khz))
        print(json.dumps(row), flush=True)
        time.sleep(max(0.0, a.interval - (time.time() - t0)))
    print(json.dumps({'kind': 'rxmon_stop', 'epoch': time.time()}), flush=True)


if __name__ == '__main__':
    main()
