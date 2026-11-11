"""Receive-only calibration report: is the cell on the air, and how strong is it at the UE radio?

Inputs: the step run directories (one OAI cell start per TX gain, each with `rxmon.jsonl` written by
`oaiho rx-monitor`) and optionally the loopback calibration `cal.json`. Output: summary.json,
report.md and a self-contained report.html with plots and a plain-language interpretation.
dBFS values are relative to the receiver's converter (not dBm); differences between steps are what
is reliable, because the receiver gain is the same in every step.
"""
import base64
import html
import io
import json
import statistics
from pathlib import Path
from .config import active_cells, require
from .oailog import parse

INK, INK_2, MUTED, GRID, BLUE, CRITICAL = '#0b0b0b', '#52514e', '#898781', '#e1e0d9', '#2a78d6', '#d03b3b'


def _median(v):
    v = [x for x in v if x is not None]
    return statistics.median(v) if v else None


def step_summary(run_dir):
    run = Path(run_dir)
    cfg = json.loads((run / 'experiment.json').read_text())
    cell = active_cells(cfg)[0]
    rows = [json.loads(ln) for ln in (run / 'rxmon.jsonl').read_text().splitlines() if ln.startswith('{')]
    start = next((r for r in rows if r.get('kind') == 'rxmon_start'), {})
    m = [r for r in rows if r.get('kind') == 'rxmon' and 'burst_dbfs' in r]
    require(m, f'{run}: no receiver measurements in rxmon.jsonl')
    det = [r for r in m if r.get('pss_detected')]
    nid2 = statistics.mode([r['pss_nid2'] for r in det]) if det else None
    du = {'radio_verified': False, 'interlock': None, 'U': 0, 'L': 0, 'O': 0, 'D': 0, 'S': 0, 'uhd_errors': 0,
          'actual_tx_gain_db': None, 'seconds': None}
    t0 = t1 = None
    for f in sorted((run / 'logs' / cell['name']).glob('*.jsonl')):
        for ln in f.read_text().splitlines():
            r = json.loads(ln)
            k = r.get('kind')
            if k == 'process_start':
                t0 = r['epoch_ns'] / 1e9
            elif k == 'process_exit':
                t1 = r['epoch_ns'] / 1e9
            elif k == 'radio_verified':
                du['radio_verified'] = True
            elif k == 'interlock_abort':
                du['interlock'] = r.get('reason')
            elif k == 'oai_log':
                for ev, d in parse(r['line']):
                    if ev == 'uhd_fast_path':
                        for ch in d['chars']:
                            du[ch] = du.get(ch, 0) + 1
                    elif ev == 'uhd_error':
                        du['uhd_errors'] += 1
                    elif ev == 'hw_tx_gain':
                        du['actual_tx_gain_db'] = float(d['v'])
    if t0 and t1:
        du['seconds'] = round(t1 - t0, 1)
    return {'run': str(run), 'cell': cell['name'], 'pci': cell['pci'], 'expected_nid2': cell['pci'] % 3,
            'att_tx_db': cell['att_tx_db'], 'uhd_tx_gain_db': cell['tx_gain_max_db'] - cell['att_tx_db'],
            'rx_gain_db': start.get('gain_db'), 'rx_ref_locked': start.get('ref_locked'), 'captures': len(m),
            'pss_detected_fraction': round(len(det) / len(m), 3), 'pss_nid2': nid2,
            'pss_rho_median': _median([r['pss_rho'] for r in m]),
            'pss_period_ms_median': _median([r.get('pss_period_ms') for r in det]),
            'burst_dbfs_median': _median([r['burst_dbfs'] for r in m]),
            'floor_dbfs_median': _median([r['floor_dbfs'] for r in m]),
            'burst_above_floor_db_median': _median([r['burst_above_floor_db'] for r in m]),
            'peak_dbfs_max': max(r['peak_dbfs'] for r in m), 'headroom_db_min': min(r['headroom_db'] for r in m),
            'rx_stream_errors': sum(r.get('stream_errors', 0) for r in m), 'du': du,
            'example_envelope': (sorted(m, key=lambda r: r['burst_dbfs'])[len(m) // 2].get('envelope_db'),
                                 m[0].get('envelope_step_ms'))}


def interpret(steps, cal):
    """Each check: (status, title, explanation). Status: PASS / CHECK / FAIL."""
    out = []
    on_air = all(s['pss_detected_fraction'] >= 0.8 and s['pss_nid2'] == s['expected_nid2'] and
                 s['pss_period_ms_median'] is not None and abs(s['pss_period_ms_median'] - 20) <= 1 for s in steps)
    out.append(('PASS' if on_air else 'FAIL', 'The cell is on the air',
                'In every step the listening radio found the NR primary synchronisation signal expected for PCI '
                f"{steps[0]['pci']} (N_ID2 = {steps[0]['expected_nid2']}) repeating every ~20 ms, the configured SSB period. "
                'PSS identifies only N_ID2; SSS/PBCH decoding and attachment are needed to establish full cell identity and service.' if on_air else
                'The synchronisation signal was not found reliably with the expected identity and 20 ms period. '
                'Check the cable path (TX/RX of X310 A to RX2 of X310 C, slot-B connectors), that the DU printed '
                '"radio settings verified", and raise the rx-monitor gain.'))
    pts = [(s['uhd_tx_gain_db'], s['burst_dbfs_median']) for s in steps if s['pss_detected_fraction'] >= 0.8]
    if len({g for g, _ in pts}) >= 2:
        import numpy as np
        g, y = np.array([p[0] for p in pts]), np.array([p[1] for p in pts])
        slope, icpt = np.polyfit(g, y, 1)
        r2 = 1 - float(np.sum((y - (slope * g + icpt)) ** 2)) / max(float(np.sum((y - y.mean()) ** 2)), 1e-12)
        ok = abs(slope - 1) <= 0.25 and r2 >= 0.9
        out.append(('PASS' if ok else 'CHECK', 'TX gain changes the output as configured',
                    f'The received burst level changed by {slope:.2f} dB per dB of UHD TX gain (ideal 1.00, R^2 = {r2:.2f}). '
                    + ('So att_tx_db really sets the transmit level: evidence for safety.gain_mapping_verified.' if ok else
                       'A different slope means the burst level is limited by noise or saturation, or the gain step is not '
                       'what was configured; look at the floor and headroom columns.')))
    else:
        slope = r2 = None
        out.append(('CHECK', 'TX gain linearity not measured', 'Run at least two TX gains with the cell detected.'))
    worst_head = min(s['headroom_db_min'] for s in steps)
    out.append(('PASS' if worst_head >= 6 else 'CHECK', 'No saturation at the UE radio',
                f'The largest sample stayed {worst_head:.1f} dB below full scale. ' +
                ('Comfortable.' if worst_head >= 6 else 'Close to clipping: lower the rx-monitor gain.')))
    low = min(steps, key=lambda s: s['uhd_tx_gain_db'])
    snr = low['burst_above_floor_db_median'] or 0
    out.append(('PASS' if snr >= 10 else 'CHECK' if snr >= 3 else 'FAIL', 'Enough signal for a UE at the lowest gain',
                f"At UHD TX gain {low['uhd_tx_gain_db']:g} dB the SSB bursts stand {snr:.1f} dB above the idle floor "
                '(0.25 ms windows). ' + ('Promising for cell search; this does not establish uplink viability or low-rate data service.' if snr >= 10 else
                                         'Marginal: use the next TX gain step for the attach experiment.' if snr >= 3 else
                                         'Too weak: check the cables before raising any gain.')))
    errs = sum(sum(s['du'][k] for k in 'ULODS') for s in steps)
    secs = sum(s['du']['seconds'] or 0 for s in steps) or 1
    rxe = sum(s['rx_stream_errors'] for s in steps)
    out.append(('PASS' if errs == 0 and rxe == 0 else 'CHECK', 'Radio transport',
                f'DU stream events (underflow/late/overflow/drop): {errs} in {secs:.0f} s; receiver capture errors: {rxe}. ' +
                ('Clean.' if errs == 0 and rxe == 0 else 'These come from the host/USB link, not from the radio channel; '
                 'record them, and expect them to show up as gaps in later traffic tests.')))
    ver = all(s['du']['radio_verified'] and not s['du']['interlock'] for s in steps)
    out.append(('PASS' if ver else 'FAIL', 'Interlock',
                'Every DU start reported the expected gain, frequencies, rate and external clock.' if ver else
                'A DU start was not verified or the interlock stopped it: read the DU log of that step.'))
    if cal:
        d = cal.get('derived', {})
        iso = d.get('self_isolation_db', d.get('self_isolation_lower_bound_db'))
        bound = 'at least ' if 'self_isolation_lower_bound_db' in d and 'self_isolation_db' not in d else ''
        if iso is None:
            out.append(('CHECK', 'Loopback calibration', 'The tone was not seen at the UE radio; the isolation could not be computed.'))
        else:
            st = 'PASS' if iso >= 48 else 'CHECK' if iso >= 30 else 'FAIL'
            out.append((st, "Cell's own TX -> RX2 isolation",
                        f'{bound}{iso:.1f} dB (from the tone levels, assuming the cable path is '
                        f"{d.get('cable_path_loss_assumed_db')} dB). " +
                        ('Even the UBX hardware maximum (+20 dBm) would stay below the guarded -25 dBm at its own RX2.' if iso >= 48 else
                         'Fine with the software TX-gain cap; not enough on its own at full hardware power.' if iso >= 30 else
                         'Unexpectedly low: stop and inspect the radio.')))
    return out, slope, r2


def _style(plt):
    plt.rcParams.update({'font.family': 'sans-serif', 'font.size': 9, 'axes.edgecolor': '#c3c2b7', 'axes.labelcolor': INK_2,
                         'xtick.color': MUTED, 'ytick.color': MUTED, 'axes.grid': True, 'grid.color': GRID,
                         'axes.spines.top': False, 'axes.spines.right': False, 'axes.titlesize': 10, 'axes.titlecolor': INK})


def _png(fig):
    b = io.BytesIO(); fig.savefig(b, format='png', dpi=150); return base64.b64encode(b.getvalue()).decode()


def report(step_dirs, out, cal_file=None):
    out = Path(out); out.mkdir(parents=True, exist_ok=False)
    steps = sorted((step_summary(d) for d in step_dirs), key=lambda s: s['uhd_tx_gain_db'])
    cal = json.loads(Path(cal_file).read_text()) if cal_file else None
    checks, slope, r2 = interpret(steps, cal)
    summary = {'experiment': 'receive_only_calibration', 'steps': [{k: v for k, v in s.items() if k != 'example_envelope'} for s in steps],
               'calibration': cal, 'slope_db_per_db': slope, 'r2': r2,
               'checks': [{'status': a, 'title': b, 'explanation': c} for a, b, c in checks],
               'units': 'dBFS = relative to the receiver converter full scale; not calibrated dBm'}
    (out / 'summary.json').write_text(json.dumps(summary, indent=2, default=float) + '\n')
    head = '| UHD TX gain (dB) | PSS found | N_ID2 | period (ms) | burst (dBFS) | above floor (dB) | headroom (dB) | DU U/L/O/D |'
    lines = ['# Receive-only cell calibration', '', head, '|---:|---:|---:|---:|---:|---:|---:|---|']
    for s in steps:
        du = s['du']
        lines.append(f"| {s['uhd_tx_gain_db']:g} | {s['pss_detected_fraction']:.0%} | {s['pss_nid2']} | {s['pss_period_ms_median']} | "
                     f"{s['burst_dbfs_median']} | {s['burst_above_floor_db_median']} | {s['headroom_db_min']} | "
                     f"{du['U']}/{du['L']}/{du['O']}/{du['D']} |")
    lines += ['', '## Interpretation', ''] + [f'- **{a}** {b}: {c}' for a, b, c in checks]
    lines += ['', 'dBFS values are relative to the receiver; differences between steps are reliable, absolute dBm is not measured.']
    (out / 'report.md').write_text('\n'.join(lines) + '\n')
    imgs = []
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        _style(plt)
        fig, ax = plt.subplots(1, 2, figsize=(11, 3.6), layout='constrained')
        g = [s['uhd_tx_gain_db'] for s in steps]; y = [s['burst_dbfs_median'] for s in steps]
        ax[0].plot(g, y, 'o', color=BLUE, ms=7, markeredgecolor='#fcfcfb', markeredgewidth=1.5)
        if slope is not None:
            import numpy as np
            xx = np.linspace(min(g), max(g), 10)
            gm, ym = float(np.mean(g)), float(np.mean(y))            # least-squares line passes through the means
            ax[0].plot(xx, slope * (xx - gm) + ym, color=INK_2, linewidth=1)
            ax[0].annotate(f'{slope:.2f} dB per dB', (xx[-1], slope * (xx[-1] - gm) + ym), xytext=(-70, 8),
                           textcoords='offset points', fontsize=8, color=INK)
        ax[0].set(xlabel='UHD TX gain at X310 A (dB)', ylabel='SSB burst level at X310 C (dBFS)')
        ax[0].set_title('Received level follows the TX gain', loc='left')
        env, step_ms = steps[0]['example_envelope']
        if env:
            ax[1].plot([i * step_ms for i in range(len(env))], env, color=BLUE, linewidth=1.2)
            ax[1].set(xlabel='time (ms)', ylabel='power in 0.25 ms windows (dBFS)')
            ax[1].set_title(f"SSB bursts every 20 ms (TX gain {steps[0]['uhd_tx_gain_db']:g} dB)", loc='left')
        fig.savefig(out / 'calibration.png', dpi=150)
        imgs.append(_png(fig)); plt.close(fig)
    except ImportError:
        pass
    badge = {'PASS': '#0ca30c', 'CHECK': '#fab219', 'FAIL': '#d03b3b'}
    items = ''.join(f'<li><span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:{badge[a]};margin-right:6px"></span>'
                    f'<b>{a}</b> {html.escape(b)}: {html.escape(c)}</li>' for a, b, c in checks)
    table = ''.join('<tr>' + ''.join(f'<td>{html.escape(str(v))}</td>' for v in (
        s['uhd_tx_gain_db'], f"{s['pss_detected_fraction']:.0%}", s['pss_nid2'], s['pss_period_ms_median'], s['burst_dbfs_median'],
        s['burst_above_floor_db_median'], s['headroom_db_min'], f"{s['du']['U']}/{s['du']['L']}/{s['du']['O']}/{s['du']['D']}")) + '</tr>'
        for s in steps)
    page = f'''<!doctype html><meta charset="utf-8"><title>Receive-only calibration</title>
<style>body{{font:14px system-ui,-apple-system,"Segoe UI",sans-serif;max-width:1100px;margin:24px auto;padding:0 16px;color:#0b0b0b;background:#fcfcfb}}
table{{border-collapse:collapse;font-variant-numeric:tabular-nums}}td,th{{border-bottom:1px solid #e1e0d9;padding:4px 10px;text-align:left}}
li{{margin:6px 0}}img{{max-width:100%}}@media (prefers-color-scheme:dark){{body{{background:#1a1a19;color:#f0efec}}td,th{{border-color:#2c2c2a}}img{{background:#fcfcfb}}}}</style>
<h1>Receive-only cell calibration</h1><h2>Interpretation</h2><ul>{items}</ul>
{''.join(f'<img alt="Received level versus TX gain, and SSB burst trace" src="data:image/png;base64,{i}">' for i in imgs)}
<h2>Steps</h2><div style="overflow-x:auto"><table><tr><th>UHD TX gain (dB)</th><th>PSS found</th><th>N_ID2</th><th>period (ms)</th>
<th>burst (dBFS)</th><th>above floor (dB)</th><th>headroom (dB)</th><th>DU U/L/O/D</th></tr>{table}</table></div>
<p>dBFS is relative to the receiver's converter; differences between steps are reliable, absolute dBm is not measured.</p>'''
    (out / 'report.html').write_text(page)
    return summary
