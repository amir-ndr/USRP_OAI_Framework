#!/usr/bin/env python3
"""RECEIVE-ONLY level and spectrum check of one USRP. Run with OAI stopped.

No TX streamer is ever created and no TX setting is touched, so nothing is transmitted. For each
antenna x frequency x RX gain it reports average power and per-component peak in dBFS of the
sample stream (0 dBFS = converter full scale, i.e. the clipping point), the headroom left, and a
coarse spectrum. Output: one JSON document on stdout.
"""
import argparse
import json
import math
import sys
import time


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--uhd-python', help='directory containing the uhd package matching the FPGA image')
    p.add_argument('--args', required=True)
    p.add_argument('--subdev', default='B:0')
    p.add_argument('--rate', type=float, default=7.68e6)
    p.add_argument('--freqs', required=True, help='comma-separated centre frequencies in Hz')
    p.add_argument('--gains', default='0,15,28')
    p.add_argument('--antennas', default='RX2,TX/RX')
    p.add_argument('--seconds', type=float, default=0.25)
    p.add_argument('--bins', type=int, default=256)
    a = p.parse_args()
    if a.uhd_python:
        sys.path.insert(0, a.uhd_python)
    import numpy as np
    import uhd

    u = uhd.usrp.MultiUSRP(a.args + ',master_clock_rate=184.32e6')
    u.set_rx_subdev_spec(uhd.usrp.SubdevSpec(a.subdev))
    out = {'args': a.args, 'subdev': a.subdev, 'dboard': u.get_usrp_rx_info(0).get('rx_id', ''),
           'default_rx_antenna': u.get_rx_antenna(0), 'rx_antennas': list(u.get_rx_antennas(0)),
           'gain_range': [u.get_rx_gain_range(0).start(), u.get_rx_gain_range(0).stop()],
           'transmit': 'never (no TX streamer created)', 'captures': []}
    u.set_rx_rate(a.rate, 0)
    rate = u.get_rx_rate(0)
    out['sample_rate_sps'] = rate
    sa = uhd.usrp.StreamArgs('fc32', 'sc16'); sa.channels = [0]
    rx = u.get_rx_stream(sa)
    md = uhd.types.RXMetadata()
    n = int(rate * a.seconds)
    buf = np.zeros((1, rx.get_max_num_samps()), dtype=np.complex64)

    def capture():
        cmd = uhd.types.StreamCMD(uhd.types.StreamMode.num_done)
        cmd.num_samps = n; cmd.stream_now = True
        rx.issue_stream_cmd(cmd)
        x = np.empty(n, dtype=np.complex64); got = 0; errors = 0
        deadline = time.time() + a.seconds + 3
        while got < n and time.time() < deadline:
            k = rx.recv(buf, md, 1.0)
            if md.error_code not in (uhd.types.RXMetadataErrorCode.none,):
                errors += 1
                if md.error_code == uhd.types.RXMetadataErrorCode.timeout:
                    break
            k = min(k, n - got); x[got:got + k] = buf[0, :k]; got += k
        return x[:got], errors

    for ant in a.antennas.split(','):
        u.set_rx_antenna(ant, 0)
        for f in [float(v) for v in a.freqs.split(',')]:
            u.set_rx_freq(uhd.types.TuneRequest(f), 0)
            for _ in range(50):
                if u.get_rx_sensor('lo_locked', 0).to_bool():
                    break
                time.sleep(0.01)
            for g in [float(v) for v in a.gains.split(',')]:
                u.set_rx_gain(g, 0)
                time.sleep(0.05)
                capture()                                    # discard: settling after retune / gain change
                x, errors = capture()
                x = x[len(x) // 10:]
                if not len(x):
                    out['captures'].append({'antenna': ant, 'freq_hz': f, 'gain_db': g, 'error': 'no samples'}); continue
                pwr = float(np.mean(np.abs(x) ** 2))
                peak = float(max(np.max(np.abs(x.real)), np.max(np.abs(x.imag))))
                frames = len(x) // 1024
                spec = np.abs(np.fft.fftshift(np.fft.fft(x[:frames * 1024].reshape(frames, 1024) * np.hanning(1024), axis=1), axes=1)) ** 2
                spec_db = 10 * np.log10(spec.mean(axis=0) / (np.sum(np.hanning(1024) ** 2)) + 1e-20)
                coarse = spec_db.reshape(a.bins, -1).max(axis=1) if 1024 % a.bins == 0 else spec_db
                floor = float(np.median(spec_db))
                top = int(np.argmax(spec_db))
                out['captures'].append({
                    'antenna': ant, 'freq_hz': f, 'gain_db': u.get_rx_gain(0), 'samples': int(len(x)), 'stream_errors': errors,
                    'avg_dbfs': round(10 * math.log10(pwr + 1e-20), 2), 'peak_dbfs': round(20 * math.log10(peak + 1e-12), 2),
                    'headroom_db': round(-20 * math.log10(peak + 1e-12), 2),
                    'spectral_floor_db': round(floor, 2), 'strongest_db': round(float(spec_db[top]), 2),
                    'strongest_offset_hz': round((top - 512) * rate / 1024), 'strongest_above_floor_db': round(float(spec_db[top]) - floor, 2),
                    'spectrum_db': [round(float(v), 1) for v in coarse]})
    print(json.dumps(out))


if __name__ == '__main__':
    main()
