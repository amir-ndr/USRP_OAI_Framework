"""Conservative offline metrics: never infer HO completion from an acknowledgment."""
import base64
import csv
import html
import io
import json
import math
import statistics
from pathlib import Path
from .config import require, digest
from .oailog import parse, clean, oai_epoch

WINDOW_PRE_S, WINDOW_POST_S = 1.0, 2.0
# Reference palette (dataviz skill): series 1 blue, status critical, ink tokens.
INK, INK_2, MUTED, GRID, BLUE, ORANGE, CRITICAL = '#0b0b0b', '#52514e', '#898781', '#e1e0d9', '#2a78d6', '#eb6834', '#d03b3b'


def quantile(values, q):
    if not values:
        return None
    x = sorted(values); j = (len(x) - 1) * q; a = int(j); b = min(a + 1, len(x) - 1)
    return x[a] + (x[b] - x[a]) * (j - a)


def fmt(v, digits=2):
    if isinstance(v, float):
        return f'{v:.{digits}f}' if abs(v) >= 0.01 or v == 0 else f'{v:.2e}'
    return '' if v is None else str(v)


def wilson(success, total):
    if not total:
        return None
    z = 1.95996398454; p = success / total; d = 1 + z * z / total
    center = (p + z * z / (2 * total)) / d
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / d
    return [max(0, center - half), min(1, center + half)]


def load_packets(packet_file):
    rows = [json.loads(x) for x in Path(packet_file).read_text().splitlines() if x.strip()]
    meta = next(x for x in rows if x.get('kind') == 'metadata')
    packets = sorted([x for x in rows if x.get('kind') == 'packet'], key=lambda x: x['seq'])
    for x in packets:
        x['recv_epoch_s'] = (None if x['recv_monotonic_ns'] is None else
                             (x['send_epoch_ns'] + x['recv_monotonic_ns'] - x['send_monotonic_ns']) / 1e9)
    return meta, packets


def window_metrics(packets, t0, t1, interval_s):
    """Traffic around one handover: [attempt - 1 s, completion + 2 s] on the shared host clock."""
    lo, hi = t0 - WINDOW_PRE_S, t1 + WINDOW_POST_S
    sent = [x for x in packets if lo <= x['send_epoch_ns'] / 1e9 <= hi]
    arrivals = sorted(x['recv_epoch_s'] for x in packets if x['recv_epoch_s'] is not None and lo <= x['recv_epoch_s'] <= hi)
    gaps = [(b - a) * 1000 for a, b in zip(arrivals, arrivals[1:])]
    rtts = [x['rtt_ms'] for x in sent if x['status'] == 'received']
    times = [x['send_epoch_ns'] / 1e9 for x in packets]
    covered = bool(sent) and min(times) <= lo + interval_s and max(times) >= hi - interval_s
    return {'window_s': [round(lo, 6), round(hi, 6)], 'probe_coverage': covered,
            'coverage': 'complete' if covered else 'partial' if sent else 'none', 'sent': len(sent),
            'lost': sum(x['status'] == 'lost' for x in sent) if sent else None,
            'censored': sum(x['status'] == 'censored' for x in sent),
            'max_gap_ms': max(gaps) if gaps else None, 'max_rtt_ms': max(rtts) if rtts else None}


def analyze(packet_file, events_file=None, experiment=None):
    meta, packets = load_packets(packet_file)
    require(len({x['seq'] for x in packets}) == len(packets), 'duplicate sequence records')
    received = [x for x in packets if x['status'] == 'received']
    lost = [x for x in packets if x['status'] == 'lost']
    arrived = sorted(x['recv_monotonic_ns'] for x in received)
    gaps = [(b - a) / 1e6 for a, b in zip(arrived, arrived[1:])]
    # Loss-burst proxy uses actual send times and is separate from receive gaps.
    bursts = []; first = None; count = 0; seen_success = False
    for x in packets:
        if x['status'] == 'lost':
            if first is None:
                first = x; count = 0; left_censored = not seen_success
            count += 1
        elif x['status'] == 'received':
            if first is not None:
                bursts.append({'lost_packets': count, 'send_span_to_next_success_ms': (x['send_monotonic_ns'] - first['send_monotonic_ns']) / 1e6,
                               'left_censored': left_censored, 'right_censored': False})
                first = None
            seen_success = True
    if first is not None:
        bursts.append({'lost_packets': count, 'send_span_to_next_success_ms': None, 'left_censored': left_censored, 'right_censored': True})
    event_rows = []
    if events_file:
        with open(events_file) as f:
            event_rows = list(csv.DictReader(f))
    starts = {}; terminal = {}
    for e in event_rows:
        require(e['evidence'].strip(), 'each event requires a source evidence reference')
        key = (e['ue_id'], e['handover_id'])
        if e['event'] == 'attempt':
            require(key not in starts, 'duplicate HO attempt id')
            starts[key] = e
        elif e['event'] in ('complete', 'failure'):
            require(key not in terminal, 'multiple terminal records for one attempt')
            terminal[key] = e
        else:
            raise ValueError('events.csv event must be attempt/complete/failure')
    require(set(terminal) <= set(starts), 'completion/failure has no paired attempt')
    same_clock = meta.get('clock_id') is not None
    durations = []; completed = 0; failed = 0; transitions = []; details = []
    for key, a in starts.items():
        b = terminal.get(key); duration = None
        if b:
            require(a['clock_id'] == b['clock_id'], 'duration requires same clock_id; normalize synchronized clocks explicitly first')
            duration = (float(b['epoch_s']) - float(a['epoch_s'])) * 1000
            require(duration >= 0, 'negative HO duration')
            require(a['source_pci'] == b['source_pci'] and a['target_pci'] == b['target_pci'], 'mismatched source/target events')
            if b['event'] == 'complete':
                completed += 1; durations.append(duration); transitions.append(b)
            else:
                failed += 1
        row = {'ue_id': key[0], 'handover_id': key[1], 'outcome': b['event'] if b else 'unknown',
               'source_pci': a['source_pci'], 'target_pci': a['target_pci'], 'attempt_epoch_s': float(a['epoch_s']),
               'signaling_duration_ms': duration}
        # Per-handover traffic window only when probe and events share one host clock.
        if b and same_clock and meta['clock_id'] == a['clock_id'].split(':')[0]:
            row['traffic_window'] = window_metrics(packets, float(a['epoch_s']), float(b['epoch_s']), meta['interval_s'])
        details.append(row)
    # Count only verified reverse transitions for the same UE within 5 seconds.
    pingpong = 0; last = {}
    for e in sorted(transitions, key=lambda e: float(e['epoch_s'])):
        previous = last.get(e['ue_id'])
        if previous and e['clock_id'] == previous['clock_id'] and 0 <= float(e['epoch_s']) - float(previous['epoch_s']) <= 5:
            if e['source_pci'] == previous['target_pci'] and e['target_pci'] == previous['source_pci']:
                pingpong += 1
        last[e['ue_id']] = e
    known = completed + failed
    all_windows = [d['traffic_window'] for d in details if 'traffic_window' in d]
    windows = [w for w in all_windows if w['probe_coverage'] and not w['censored']]
    win_gaps = [w['max_gap_ms'] for w in windows if w['max_gap_ms'] is not None]
    # Overlapping windows must not count one missing probe more than once.
    window_lost = sum(x['status'] == 'lost' and any(w['window_s'][0] <= x['send_epoch_ns'] / 1e9 <= w['window_s'][1]
                                                  for w in windows) for x in packets)
    provenance = None if experiment is None else {
        'mode': experiment['mode'], 'topology': experiment['topology'], 'profile': experiment['radio']['profile'],
        'trigger': experiment['handover']['trigger'], 'ue_kind': experiment['ue']['kind'],
        'config_sha256': digest(experiment), 'condition': experiment['experiment']}
    return {'schema_version': 2, 'synthetic': bool(meta.get('synthetic', False)), 'probe_method': meta.get('method', 'udp'),
            'experiment': provenance,
            'traffic': {'sent_records': len(packets), 'received': len(received), 'lost': len(lost),
                        'observed_send_span_s': (max(x['send_epoch_ns'] for x in packets) - min(x['send_epoch_ns'] for x in packets)) / 1e9 if packets else None,
                        'censored': len(packets) - len(received) - len(lost),
                        'loss_fraction': len(lost) / (len(received) + len(lost)) if received or lost else None,
                        'rtt_median_ms': quantile([x['rtt_ms'] for x in received], 0.5),
                        'rtt_p95_ms': quantile([x['rtt_ms'] for x in received], 0.95),
                        'max_interarrival_gap_ms': max(gaps) if gaps else None,
                        'loss_bursts': bursts, 'probe_interval_s': meta['interval_s']},
            'handover': {'attempts': len(starts), 'complete': completed, 'failed': failed, 'unknown': len(starts) - known,
                         'success_fraction_known_outcomes': completed / known if known else None,
                         'wilson95_known_outcomes': wilson(completed, known),
                         'duration_median_ms': quantile(durations, 0.5), 'duration_p95_ms': quantile(durations, 0.95),
                         'pingpong_reverse_transitions_5s': pingpong,
                         'window_max_gap_median_ms': quantile(win_gaps, 0.5),
                         'traffic_windows_complete': len(windows),
                         'traffic_windows_partial_or_censored': sum(w['coverage'] == 'partial' or bool(w['censored']) for w in all_windows),
                         'traffic_windows_uncovered': sum(w['coverage'] == 'none' for w in all_windows),
                         'window_lost_total': window_lost if windows else None,
                         'details': details},
            'limits': ['Traffic metrics cover the complete probe interval; they are not automatically attributed to handover.',
                       'Interarrival gaps include normal packet spacing, scheduling and network delay; not exact radio interruption.',
                       'Signaling duration depends on your explicitly selected event boundaries and timestamp accuracy.',
                       'Unknown HO outcomes are excluded from the known-outcome success fraction and must be reported.',
                       f'Per-handover windows span attempt-{WINDOW_PRE_S:g} s to completion+{WINDOW_POST_S:g} s and need probe and '
                       'events on the same host clock; aggregate window metrics exclude partial/censored windows.',
                       'Experiment mode is supplied by the operator/config, not inferred from packets; synthetic=false does not prove real RF.']}


# --------------------------------------------------------------------------- event extraction
def extract_events(c, run_dir, output):
    """Build F1 events.csv from CU log lines, with file:line evidence.

    Automatic N2 pairing is refused until a validated cross-gNB identity/clock mapping exists.

    attempt  = "Handover triggered for UE x/RNTI y towards cell .../PCI p" (CU decision; F1) or
               "UE x: triggered N2 HO, source PCI=a to target PCI=b" (source gNB; N2)
    complete = "handover for UE x/RNTI y complete!" (RRCReconfigurationComplete received on the target)
    failure  = an explicit abort message for an open attempt. Anything else stays unknown.
    """
    run = Path(run_dir)
    require(c['topology'] == 'f1',
            'automatic N2 event pairing is disabled: gNB-local UE IDs and clocks need explicit correlation. '
            'Use candidates and a reviewed event CSV with normalized identities/clocks (docs/PROJECT_REVIEW.md).')
    nodes = ['cu'] if c['topology'] == 'f1' else [x['name'] for x in c['cells']]
    pci_by_nci = {x['nr_cellid']: x['pci'] for x in c['cells']}
    journal = run / 'trigger.jsonl'
    manual_times = [json.loads(x)['epoch_ns'] / 1e9 for x in journal.read_text().splitlines()
                    if x.strip() and json.loads(x).get('kind') in ('trigger_sent', 'trigger_request')] if journal.exists() else []
    lines = []
    for node in nodes:
        for f in sorted((run / 'logs' / node).glob('*.jsonl')):
            for n, raw in enumerate(f.read_text().splitlines(), 1):
                row = json.loads(raw)
                if row.get('kind') == 'oai_log':
                    t, src = oai_epoch(clean(row['line']), row['epoch_ns'] / 1e9)
                    lines.append((t, node, f'{f}:{n}', row['line'], row.get('clock_id', 'host') + ':' + src, src))
    lines.sort(key=lambda x: x[0])
    serving, open_ho, last_a3, counter, events, details = {}, {}, {}, {}, [], []
    for t, node, evidence, line, clock, src in lines:
        for ev, d in parse(line):
            ue = d.get('ue_id')
            if ev == 'rrc_connected' and c['topology'] == 'f1':
                serving[ue] = pci_by_nci.get(int(d['cellid'], 16), '')
            elif ev == 'a3_report':
                last_a3[ue] = t
            elif ev in ('ho_trigger', 'n2_trigger'):
                key = ue if c['topology'] == 'f1' else 'n2'
                if key in open_ho:
                    open_ho[key]['note'] = 'superseded by a new attempt before completion'
                counter[ue] = counter.get(ue, 0) + 1
                hid = f'ho{counter[ue]}'
                src_pci = d.get('source_pci', serving.get(ue, ''))
                kind = 'a3' if t - last_a3.get(ue, -1e18) <= 1.0 else (
                    'manual' if any(0 <= t - m <= 3.0 for m in manual_times) else 'unattributed')
                rec = {'ue_id': ue, 'handover_id': hid, 'source_pci': src_pci, 'target_pci': d['pci'], 'trigger_kind': kind,
                       'attempt_epoch_s': t, 'command_epoch_s': None, 'complete_epoch_s': None, 'clock_id': clock,
                       'time_source': src, 'outcome': 'unknown', 'evidence': evidence, 'note': ''}
                open_ho[key] = rec; details.append(rec)
                events.append({'epoch_s': f'{t:.6f}', 'clock_id': clock, 'event': 'attempt', 'ue_id': ue, 'handover_id': hid,
                               'source_pci': src_pci, 'target_pci': d['pci'], 'evidence': evidence})
            elif ev in ('ho_command', 'n2_command'):
                rec = open_ho.get(ue if c['topology'] == 'f1' else 'n2')
                if rec and rec['command_epoch_s'] is None:
                    rec['command_epoch_s'] = t
                    rec['command_clock_id'] = clock
            elif ev == 'ho_complete':
                key = ue if c['topology'] == 'f1' else 'n2'
                rec = open_ho.pop(key, None)
                if rec:
                    rec.update(complete_epoch_s=t, outcome='complete', terminal_clock_id=clock)
                    serving[rec['ue_id']] = rec['target_pci']
                    events.append({'epoch_s': f'{t:.6f}', 'clock_id': clock, 'event': 'complete', 'ue_id': rec['ue_id'],
                                   'handover_id': rec['handover_id'], 'source_pci': rec['source_pci'],
                                   'target_pci': rec['target_pci'], 'evidence': evidence})
            elif ev in ('ho_abort', 'ho_failure'):
                rec = open_ho.pop(ue, None)
                if rec:
                    rec.update(outcome='failure', failure_epoch_s=t, terminal_clock_id=clock)
                    events.append({'epoch_s': f'{t:.6f}', 'clock_id': clock, 'event': 'failure', 'ue_id': ue,
                                   'handover_id': rec['handover_id'], 'source_pci': rec['source_pci'],
                                   'target_pci': rec['target_pci'], 'evidence': evidence})
                else:
                    details.append({'ue_id': ue, 'handover_id': '', 'outcome': 'rejected_before_start', 'attempt_epoch_s': t,
                                    'evidence': evidence, 'note': 'CU refused the trigger; no handover was started'})
            elif ev == 'reestablishment':
                for rec in open_ho.values():
                    rec['note'] = (rec['note'] + '; ' if rec['note'] else '') + f're-establishment seen at {t:.3f}'
    for rec in details:
        if rec.get('complete_epoch_s'):
            valid = rec['terminal_clock_id'] == rec['clock_id']
            command_valid = valid and rec.get('command_clock_id') == rec['clock_id']
            rec['timing_valid'] = valid
            rec['decision_to_command_ms'] = None if not command_valid else round((rec['command_epoch_s'] - rec['attempt_epoch_s']) * 1e3, 3)
            rec['command_to_complete_ms'] = None if not command_valid else round((rec['complete_epoch_s'] - rec['command_epoch_s']) * 1e3, 3)
            rec['total_ms'] = round((rec['complete_epoch_s'] - rec['attempt_epoch_s']) * 1e3, 3) if valid else None
    out = Path(output)
    with open(out, 'x', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['epoch_s', 'clock_id', 'event', 'ue_id', 'handover_id', 'source_pci', 'target_pci', 'evidence'])
        w.writeheader(); w.writerows(events)
    side = out.with_name(out.stem + '_details.json')
    side.write_text(json.dumps(details, indent=2) + '\n')
    return {'events': str(out), 'details': str(side), 'attempts': sum(e['event'] == 'attempt' for e in events),
            'complete': sum(e['event'] == 'complete' for e in events), 'failure': sum(e['event'] == 'failure' for e in events),
            'automatic_success_inference': False}


# --------------------------------------------------------------------------- reports
def _style(plt):
    plt.rcParams.update({'font.family': 'sans-serif', 'font.size': 9, 'axes.edgecolor': '#c3c2b7', 'axes.labelcolor': INK_2,
                         'xtick.color': MUTED, 'ytick.color': MUTED, 'axes.grid': True, 'grid.color': GRID,
                         'grid.linewidth': 0.8, 'axes.spines.top': False, 'axes.spines.right': False,
                         'axes.titlesize': 10, 'axes.titlecolor': INK})


def _png(fig):
    buf = io.BytesIO(); fig.savefig(buf, format='png', dpi=150); return base64.b64encode(buf.getvalue()).decode()


def traffic_figure(packet_file, summary, label):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    _style(plt)
    meta, packets = load_packets(packet_file)
    t0 = packets[0]['send_epoch_ns'] / 1e9 if packets else 0
    fig, ax = plt.subplots(2, 1, figsize=(10, 5.6), sharex=True, layout='constrained', height_ratios=(4, 1))
    good = [x for x in packets if x['status'] == 'received']
    bad = [x for x in packets if x['status'] == 'lost']
    ax[0].plot([x['send_epoch_ns'] / 1e9 - t0 for x in good], [x['rtt_ms'] for x in good], '.', ms=3, color=BLUE)
    ax[0].set_ylabel('RTT (ms)')
    ax[0].set_title(f'Probe round-trip time ({meta.get("method", "udp")}, every {meta["interval_s"]*1e3:g} ms)', loc='left')
    ax[1].vlines([x['send_epoch_ns'] / 1e9 - t0 for x in bad], 0, 1, color=CRITICAL, linewidth=1)
    ax[1].set(ylabel='lost', xlabel='time since probe start (s)', ylim=(0, 1.2), yticks=[])
    t_end = packets[-1]['send_epoch_ns'] / 1e9 if packets else t0
    for d in summary['handover']['details']:
        if not t0 - 2 <= d['attempt_epoch_s'] <= t_end + 2:
            continue                                  # outside this capture; still listed in the table
        for a in ax:
            a.axvline(d['attempt_epoch_s'] - t0, color=INK_2, linewidth=1)
        ax[0].annotate(f"{d['handover_id']} {d['source_pci']}→{d['target_pci']} ({d['outcome']})",
                       (d['attempt_epoch_s'] - t0, 1), xycoords=('data', 'axes fraction'), xytext=(3, -12),
                       textcoords='offset points', fontsize=8, color=INK)
    fig.suptitle(label, fontsize=9, color=INK_2)
    return fig


def report(packet_file, output, events_file=None, plots=False, experiment=None):
    out = Path(output); out.mkdir(parents=True, exist_ok=False)
    r = analyze(packet_file, events_file, experiment)
    (out / 'summary.json').write_text(json.dumps(r, indent=2) + '\n')
    from .artifacts import export_analysis
    export_analysis(packet_file, out, r)
    label = 'SYNTHETIC SOFTWARE DEMO — NOT AN RF MEASUREMENT' if r['synthetic'] else 'MEASURED INPUT — VALIDITY DEPENDS ON CAPTURE AND EVENT EVIDENCE'
    if not r['synthetic'] and r['experiment']:
        label = r['experiment']['mode'].upper() + ' — ' + label
    t = r['traffic']; h = r['handover']
    lines = [f'# Experiment report\n\n**{label}**\n', '| Metric | Value |', '|---|---|']
    for key, value in t.items():
        if key != 'loss_bursts':
            lines.append(f'| {key} | {value} |')
    for key, value in h.items():
        if key != 'details':
            lines.append(f'| {key} | {value} |')
    if h['details']:
        lines += ['', '## Handovers', '', '| id | source→target | outcome | signaling ms | window lost | window max gap ms |', '|---|---|---|---:|---:|---:|']
        for d in h['details']:
            w = d.get('traffic_window', {})
            lines.append(f"| {d['handover_id']} | {d['source_pci']}→{d['target_pci']} | {d['outcome']} | {d['signaling_duration_ms']} | "
                         f"{w.get('lost', '')} | {w.get('max_gap_ms', '')} |")
    lines += ['', '## Interpretation boundaries', ''] + ['- ' + x for x in r['limits']]
    (out / 'report.md').write_text('\n'.join(lines) + '\n')
    if plots:
        import matplotlib.pyplot as plt
        fig = traffic_figure(packet_file, r, label)
        fig.savefig(out / 'traffic.png', dpi=160)
        img = _png(fig); plt.close(fig)
        rows = ''.join(f'<tr><th scope="row">{html.escape(str(k))}</th><td>{html.escape(fmt(v) if isinstance(v, float) else str(v))}</td></tr>'
                       for sec in (t, h) for k, v in sec.items() if k not in ('loss_bursts', 'details'))
        hos = ''.join(f"<tr><td>{html.escape(str(d['handover_id']))}</td><td>{d['source_pci']}→{d['target_pci']}</td>"
                      f"<td>{html.escape(d['outcome'])}</td><td>{fmt(d['signaling_duration_ms'], 1)}</td>"
                      f"<td>{d.get('traffic_window', {}).get('lost', '')}</td><td>{fmt(d.get('traffic_window', {}).get('max_gap_ms'), 1)}</td></tr>"
                      for d in h['details'])
        (out / 'report.html').write_text(HTML_PAGE.format(title='Handover run report', label=html.escape(label),
            body=f'<img alt="RTT over time with handover markers and lost probes" src="data:image/png;base64,{img}">'
                 f'<h2>Handovers</h2><table><tr><th>id</th><th>source→target</th><th>outcome</th><th>signaling ms</th>'
                 f'<th>lost in window</th><th>max gap in window (ms)</th></tr>{hos}</table>'
                 f'<h2>All metrics</h2><table>{rows}</table>'
                 '<h2>Interpretation boundaries</h2><ul>' + ''.join(f'<li>{html.escape(x)}</li>' for x in r['limits']) + '</ul>'))
    return r


HTML_PAGE = '''<!doctype html><meta charset="utf-8"><title>{title}</title>
<style>body{{font:14px system-ui,-apple-system,"Segoe UI",sans-serif;max-width:1100px;margin:24px auto;padding:0 16px;color:#0b0b0b;background:#fcfcfb}}
h1{{font-size:20px}}h2{{font-size:15px;margin-top:28px}}.label{{color:#52514e;font-weight:600}}img{{max-width:100%}}
table{{border-collapse:collapse;font-variant-numeric:tabular-nums}}td,th{{border-bottom:1px solid #e1e0d9;padding:4px 10px;text-align:left}}
@media (prefers-color-scheme:dark){{body{{background:#1a1a19;color:#f0efec}}.label{{color:#c3c2b7}}td,th{{border-color:#2c2c2a}}img{{background:#fcfcfb}}}}</style>
<h1>{title}</h1><p class="label">{label}</p>{body}'''


def demo(output):
    out = Path(output); out.mkdir(parents=True, exist_ok=False)
    path = out / 'packets.jsonl'
    rows = [{'kind': 'metadata', 'synthetic': True, 'interval_s': 0.02, 'timeout_s': 1.0, 'method': 'udp', 'clock_id': 'demo'}]
    for i in range(250):
        t = i * 0.02; lost = 100 <= i < 106; rtt = 12 + (i % 7) * 0.5; sent = round(t * 1e9)
        rows.append({'kind': 'packet', 'seq': i, 'send_relative_s': t, 'send_epoch_ns': 1700000000000000000 + sent,
                     'send_monotonic_ns': sent, 'recv_monotonic_ns': None if lost else sent + round(rtt * 1e6),
                     'rtt_ms': None if lost else rtt, 'status': 'lost' if lost else 'received'})
    path.write_text('\n'.join(json.dumps(x) for x in rows) + '\n')
    (out / 'events.csv').write_text('epoch_s,clock_id,event,ue_id,handover_id,source_pci,target_pci,evidence\n'
                                    '1700000002,demo,attempt,1,ho1,0,1,synthetic_fixture\n'
                                    '1700000002.12,demo,complete,1,ho1,0,1,synthetic_fixture\n')
    return report(path, out / 'report', out / 'events.csv', plots=True)


def compare(manifest, output, mixed_modes=False):
    """Aggregate independent run summaries. Group assignments are explicit.

    mixed_modes=True allows conditions of different modes (e.g. an RFsim baseline next to the over-the-air
    runs); every condition must still hold one mode only, and each row and the report name its mode."""
    source = Path(manifest)
    with source.open() as f:
        items = list(csv.DictReader(f))
    require(bool(items), 'empty comparison manifest')
    groups = {}; kinds = set(); modes = set(); seen = set(); cond_modes = {}
    for item in items:
        path = (source.parent / item['summary']).resolve()
        require(path not in seen, 'duplicate run summary would double-count observations')
        seen.add(path)
        r = json.loads(path.read_text()); kinds.add(r['synthetic'])
        mode = (r.get('experiment') or {}).get('mode', 'unspecified')
        modes.add(mode); cond_modes.setdefault(item['condition'], set()).add(mode)
        groups.setdefault(item['condition'], []).append(r)
    require(len(kinds) == 1, 'do not mix synthetic and measured runs in one comparison')
    if mixed_modes:
        require(all(len(m) == 1 for m in cond_modes.values()), 'each condition must contain runs of one mode only')
        require('unspecified' not in modes, 'regenerate summaries with analyze --config (mode unspecified)')
    else:
        require(len(modes) == 1, 'do not mix hardware, RFsim or unspecified experiment modes (or pass --mixed-modes '
                                 'to compare conditions of different modes side by side)')
    out = Path(output); out.mkdir(parents=True, exist_ok=False)
    metrics = [('loss_fraction', 'traffic', 'loss_fraction'), ('rtt_p95_ms', 'traffic', 'rtt_p95_ms'),
               ('max_gap_ms', 'traffic', 'max_interarrival_gap_ms'), ('ho_duration_median_ms', 'handover', 'duration_median_ms'),
               ('ho_window_max_gap_ms', 'handover', 'window_max_gap_median_ms')]
    rows = []
    for name, runs in groups.items():
        row = {'condition': name, 'runs': len(runs), 'synthetic': next(iter(kinds)), 'mode': next(iter(cond_modes[name]))}
        for label, section, key in metrics:
            values = [r[section].get(key) for r in runs if r[section].get(key) is not None]
            row[label + '_n'] = len(values)
            row[label + '_mean'] = statistics.mean(values) if values else None
            row[label + '_sd'] = statistics.stdev(values) if len(values) > 1 else None
        for key in ('attempts', 'complete', 'failed', 'unknown'):
            row['ho_' + key] = sum(r['handover'][key] for r in runs)
        rows.append(row)
    with (out / 'comparison.csv').open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    (out / 'comparison.json').write_text(json.dumps(rows, indent=2) + '\n')
    lines = ['# Across-run comparison', '', '**SYNTHETIC**' if next(iter(kinds)) else f"**{' + '.join(sorted(m.upper() for m in modes))} input summaries**", '',
             '| Condition | Mode | Runs | Loss mean | RTT p95 mean (ms) | Max-gap mean (ms) | HO signalling median mean (ms) | HO window max-gap mean (ms) | HO complete / failed / unknown |',
             '|---|---|---:|---:|---:|---:|---:|---:|---|']
    for r in rows:
        lines.append(f"| {r['condition']} | {r['mode']} | {r['runs']} | {fmt(r['loss_fraction_mean'], 4)} | {fmt(r['rtt_p95_ms_mean'])} | {fmt(r['max_gap_ms_mean'])} | "
                     f"{fmt(r['ho_duration_median_ms_mean'])} | {fmt(r['ho_window_max_gap_ms_mean'])} | {r['ho_complete']} / {r['ho_failed']} / {r['ho_unknown']} |")
    lines += ['', 'CSV includes sample standard deviations and available-run counts. These are summaries of per-run metrics, not pooled '
              'packet percentiles. Match traffic windows, endpoints, event definitions and load before interpreting a difference.']
    (out / 'comparison.md').write_text('\n'.join(lines) + '\n')
    try:
        _comparison_html(groups, rows, out, next(iter(kinds)))
    except ImportError:
        pass
    return rows


def _comparison_html(groups, rows, out, synthetic):
    """One strip chart per metric: each dot is one run, so spread stays visible next to the mean."""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    _style(plt)
    panels = [('Probe RTT p95 per run (ms)', 'traffic', 'rtt_p95_ms'),
              ('Largest reply gap per run (ms)', 'traffic', 'max_interarrival_gap_ms'),
              ('HO signaling duration, median per run (ms)', 'handover', 'duration_median_ms'),
              ('Max traffic gap around HO, median per run (ms)', 'handover', 'window_max_gap_median_ms'),
              ('Probe loss fraction per run', 'traffic', 'loss_fraction')]
    names = list(groups)
    fig, axes = plt.subplots(len(panels), 1, figsize=(9, 2.4 * len(panels)), layout='constrained')
    for ax, (title, section, key) in zip(axes, panels):
        for i, name in enumerate(names):
            vals = [r[section].get(key) for r in groups[name] if r[section].get(key) is not None]
            ax.plot(vals, [i] * len(vals), 'o', ms=6, color=BLUE, markeredgecolor='#fcfcfb', markeredgewidth=1.5)
            if vals:
                ax.plot([statistics.mean(vals)] * 2, [i - 0.3, i + 0.3], color=INK, linewidth=2)
            else:
                ax.text(.98, i, 'not observed', transform=ax.get_yaxis_transform(), ha='right', va='center', fontsize=8)
        ax.set_yticks(range(len(names)), [f'{n} (n={len(groups[n])})' for n in names]); ax.set_ylim(-0.6, len(names) - 0.4)
        lo, hi = ax.get_xlim()
        ax.set_xlim(0 if lo < 0 or key == 'loss_fraction' else lo, max(hi, 0.01))   # all metrics are >= 0
        ax.set_title(title, loc='left'); ax.grid(axis='y', visible=False)
    modes = ' + '.join(sorted({r['mode'].upper() for r in rows}))
    fig.suptitle('SYNTHETIC' if synthetic else f'{modes} runs — dots are runs, bars are means', fontsize=9, color=INK_2)
    fig.savefig(out / 'comparison.png', dpi=150)
    img = _png(fig); plt.close(fig)
    head = ''.join(f'<th>{html.escape(k)}</th>' for k in ('condition', 'mode', 'runs', 'ho_complete', 'ho_failed', 'ho_unknown',
                                                         'ho_duration_median_ms_mean', 'ho_window_max_gap_ms_mean', 'loss_fraction_mean'))
    body = ''.join('<tr>' + ''.join(f'<td>{html.escape(fmt(r[k]))}</td>' for k in ('condition', 'mode', 'runs', 'ho_complete', 'ho_failed',
                   'ho_unknown', 'ho_duration_median_ms_mean', 'ho_window_max_gap_ms_mean', 'loss_fraction_mean')) + '</tr>' for r in rows)
    (out / 'comparison.html').write_text(HTML_PAGE.format(
        title='Handover comparison', label='SYNTHETIC' if synthetic else modes + ' input summaries',
        body=f'<img alt="Per-run handover metrics by condition" src="data:image/png;base64,{img}"><h2>Table</h2>'
             f'<div style="overflow-x:auto"><table><tr>{head}</tr>{body}</table></div>'))


def rx_check_figure(result, path, freqs):
    """Small multiples: one panel per radio x frequency, one line per antenna at the highest gain tested."""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    _style(plt)
    radios = result['radios']
    names = {round(freqs['ul_center_hz']): 'UL', round(freqs['dl_center_hz']): 'DL'}
    if len(names) == 1:
        names = {round(freqs['dl_center_hz']): 'UL/DL'}
    fig, axes = plt.subplots(len(radios), len(names), figsize=(11, 3.1 * len(radios)), layout='constrained', squeeze=False)
    for i, r in enumerate(radios):
        for j, f in enumerate(sorted(names)):
            ax = axes[i][j]
            caps = [cp for cp in r['captures'] if round(cp['freq_hz']) == f and 'spectrum_db' in cp]
            if not caps:
                continue
            gmax = max(cp['gain_db'] for cp in caps)
            for k, cp in enumerate([cp for cp in caps if cp['gain_db'] == gmax]):
                s = cp['spectrum_db']; span = r.get('sample_rate_sps', 7.68e6) / 2e6
                xs = [-span + 2 * span * (n + 0.5) / len(s) for n in range(len(s))]
                ax.plot(xs, s, color=(BLUE, ORANGE)[k % 2], linewidth=1.5,
                        label=f"{cp['antenna']}: avg {cp['avg_dbfs']} dBFS, peak {cp['peak_dbfs']} dBFS")
            ax.set_title(f"{r['cell']} ({r['args'].split('addr=')[1].split(',')[0]}) {names[f]} {f/1e6:.2f} MHz, RX gain {gmax:g} dB", loc='left')
            ax.set_xlabel('offset from centre (MHz)'); ax.set_ylabel('dB (relative)')
            ax.legend(fontsize=7, frameon=False, loc='upper right')
    fig.suptitle('RECEIVE-ONLY check (nothing transmitted): spectrum at the planned frequencies', fontsize=9, color=INK_2)
    fig.savefig(path, dpi=140); plt.close(fig)
