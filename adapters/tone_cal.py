#!/usr/bin/env python3
"""Loopback calibration with a weak CW tone. OAI must be stopped.

One radio (the cell) transmits a single tone at UHD TX gain 0 dB and at most -20 dBFS for at most
3 s on its TX/RX port; that radio's own RX2 and the other listed radios receive at the same time.
With the UBX's nominal +20 dBm at full gain and full scale, this is about -31.5 dBm at the connector:
below the -15 dBm input limit even with no attenuation at all.

Measured: the tone level at every receiver (dBFS), referenced to a TX-off baseline. From it:
  self isolation ~= cable-path loss + (level at the far radio - level at the cell's own RX2)
when both receivers are the same model at the same gain and frequency. --dry-run configures and reads
back everything but creates no TX streamer and sends nothing.
"""
import argparse
import json
import math
import sys
import threading
import time

MAX_AMPLITUDE = 0.1        # -20 dBFS
MAX_SECONDS = 3.0
TX_GAIN_DB = 0.0


def tone_level(x, rate, tone_hz, nfft=8192):
    """Power at the tone (dBFS, from an averaged Hann-window spectrum) and the median bin floor."""
    import numpy as np
    x = np.asarray(x, dtype=np.complex64)
    frames = len(x) // nfft
    w = np.hanning(nfft)
    spec = np.abs(np.fft.fft(x[:frames * nfft].reshape(frames, nfft) * w, axis=1)) ** 2
    spec = spec.mean(axis=0) / (np.sum(w) ** 2)          # a full-scale complex tone reads 0 dB
    k = int(round(tone_hz / rate * nfft)) % nfft
    peak = float(np.max(spec[[(k + d) % nfft for d in (-2, -1, 0, 1, 2)]]))
    floor = float(np.median(spec))
    return 10 * math.log10(peak + 1e-30), 10 * math.log10(floor + 1e-30)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--uhd-python')
    p.add_argument('--tx-args', required=True); p.add_argument('--tx-subdev', default='B:0')
    p.add_argument('--rx', action='append', default=[], help='name|uhd args|subdev of a listening radio')
    p.add_argument('--freq', type=float, required=True); p.add_argument('--tone-offset-hz', type=float, default=210e3)   # bin-centred: 224 x 937.5 Hz
    p.add_argument('--rate', type=float, default=7.68e6); p.add_argument('--rx-gain', type=float, default=20.0)
    p.add_argument('--amplitude', type=float, default=MAX_AMPLITUDE); p.add_argument('--seconds', type=float, default=2.0)
    p.add_argument('--dry-run', action='store_true')
    a = p.parse_args()
    if not (0 < a.amplitude <= MAX_AMPLITUDE and 0 < a.seconds <= MAX_SECONDS):
        raise SystemExit(f'refused: amplitude must be <= {MAX_AMPLITUDE} and seconds <= {MAX_SECONDS}')
    if a.uhd_python:
        sys.path.insert(0, a.uhd_python)
    import numpy as np
    import uhd
    mcr = ',master_clock_rate=184.32e6'
    tx = uhd.usrp.MultiUSRP(a.tx_args + mcr)
    radios = {'self': (tx, a.tx_subdev)}
    for spec in a.rx:
        name, args, sub = spec.split('|')
        radios[name] = (uhd.usrp.MultiUSRP(args + mcr), sub)
    # Configure every receiver first (RX2, same gain and frequency on all).
    for name, (u, sub) in radios.items():
        u.set_clock_source('external')
        u.set_rx_subdev_spec(uhd.usrp.SubdevSpec(sub))
        u.set_rx_rate(a.rate, 0); u.set_rx_freq(uhd.types.TuneRequest(a.freq), 0)
        u.set_rx_gain(a.rx_gain, 0); u.set_rx_antenna('RX2', 0)
    # Transmitter: gain set and READ BACK before any streamer exists.
    tx.set_tx_subdev_spec(uhd.usrp.SubdevSpec(a.tx_subdev))
    tx.set_tx_rate(a.rate, 0); tx.set_tx_freq(uhd.types.TuneRequest(a.freq), 0)
    tx.set_tx_gain(TX_GAIN_DB, 0); tx.set_tx_antenna('TX/RX', 0)
    readback = tx.get_tx_gain(0)
    out = {'tx_args': a.tx_args, 'freq_hz': a.freq, 'tone_offset_hz': a.tone_offset_hz, 'amplitude_dbfs': round(20 * math.log10(a.amplitude), 2),
           'tx_gain_readback_db': readback, 'tx_gain_range_db': [tx.get_tx_gain_range(0).start(), tx.get_tx_gain_range(0).stop()],
           'rx_gain_db': a.rx_gain, 'nominal_tx_dbm': round(20 - (tx.get_tx_gain_range(0).stop() - readback) + 20 * math.log10(a.amplitude), 1),
           'ref_locked': {n: bool(u.get_mboard_sensor('ref_locked', 0).to_bool()) for n, (u, _) in radios.items()},
           'dry_run': a.dry_run, 'receivers': {}}
    if abs(readback - TX_GAIN_DB) > 1e-6:
        out['error'] = 'TX gain readback is not 0 dB: refused before transmitting'
        print(json.dumps(out)); raise SystemExit(3)
    streams = {}
    for name, (u, _) in radios.items():
        sa = uhd.usrp.StreamArgs('fc32', 'sc16'); sa.channels = [0]
        streams[name] = u.get_rx_stream(sa)

    def capture(name, seconds=0.3):
        st = streams[name]; md = uhd.types.RXMetadata(); n = int(a.rate * seconds)
        buf = np.zeros((1, st.get_max_num_samps()), dtype=np.complex64)
        cmd = uhd.types.StreamCMD(uhd.types.StreamMode.num_done); cmd.num_samps = n; cmd.stream_now = True
        st.issue_stream_cmd(cmd)
        x = np.empty(n, dtype=np.complex64); got = 0; t0 = time.time()
        while got < n and time.time() < t0 + seconds + 2:
            k = st.recv(buf, md, 1.0)
            if md.error_code == uhd.types.RXMetadataErrorCode.timeout:
                break
            k = min(k, n - got); x[got:got + k] = buf[0, :k]; got += k
        return x[:got]

    base = {name: tone_level(capture(name), a.rate, a.tone_offset_hz) for name in radios}
    if a.dry_run:
        out['receivers'] = {n: {'baseline_tone_bin_dbfs': round(b[0], 2), 'baseline_floor_dbfs': round(b[1], 2)} for n, b in base.items()}
        print(json.dumps(out)); return
    # Transmit the tone in a thread while each receiver captures in turn.
    txs = tx.get_tx_stream(uhd.usrp.StreamArgs('fc32', 'sc16'))
    n = txs.get_max_num_samps() * 8
    t = np.arange(n) / a.rate
    tone = (a.amplitude * np.exp(2j * np.pi * a.tone_offset_hz * t)).astype(np.complex64).reshape(1, -1)
    stop = threading.Event()

    def send():
        md = uhd.types.TXMetadata(); md.start_of_burst = True; md.end_of_burst = False
        t_end = time.time() + a.seconds
        while time.time() < t_end and not stop.is_set():
            txs.send(tone, md); md.start_of_burst = False
        md.end_of_burst = True
        txs.send(np.zeros((1, 1), dtype=np.complex64), md)

    th = threading.Thread(target=send); th.start()
    time.sleep(0.3)
    try:
        on = {name: tone_level(capture(name), a.rate, a.tone_offset_hz) for name in radios}
    finally:
        stop.set(); th.join()
    for name in radios:
        level, floor = on[name]
        out['receivers'][name] = {'tone_dbfs': round(level, 2), 'floor_dbfs': round(floor, 2),
                                  'baseline_tone_bin_dbfs': round(base[name][0], 2),
                                  'tone_above_baseline_db': round(level - base[name][0], 2),
                                  'detected': level - base[name][0] >= 6}
    print(json.dumps(out))


if __name__ == '__main__':
    main()
