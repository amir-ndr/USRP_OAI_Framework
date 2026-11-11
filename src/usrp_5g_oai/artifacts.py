"""Portable CSV tables and PNG figures, including explicit empty/failed-run outputs."""
import csv
import json
from pathlib import Path


def table(path, fields, rows):
    with Path(path).open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)


def export_analysis(packet_file, out, analysis):
    out = Path(out)
    packets = []
    for line in Path(packet_file).read_text().splitlines():
        row = json.loads(line)
        if row.get('kind') == 'packet':
            packets.append(row)
    table(out / 'packets.csv', ['seq', 'status', 'send_epoch_ns', 'send_relative_s', 'rtt_ms', 'error'], packets)
    handovers = []
    for d in analysis['handover']['details']:
        w = d.get('traffic_window', {})
        handovers.append({**d, 'coverage': w.get('coverage', 'none'), 'lost_probes': w.get('lost'),
                          'censored_probes': w.get('censored'), 'largest_reply_gap_ms': w.get('max_gap_ms')})
    table(out / 'handover_metrics.csv', ['handover_id', 'ue_id', 'source_pci', 'target_pci', 'outcome',
          'attempt_epoch_s', 'signaling_duration_ms', 'coverage', 'lost_probes', 'censored_probes', 'largest_reply_gap_ms'], handovers)
    metrics = [{'section': sec, 'metric': k, 'value': v} for sec in ('traffic', 'handover')
               for k, v in analysis[sec].items() if k not in ('details', 'loss_bursts')]
    table(out / 'metrics.csv', ['section', 'metric', 'value'], metrics)


def radio_rows(run):
    from .oailog import parse, oai_epoch
    metrics = {'mac_ul': {'snr': ('ul_snr_db', 'dB'), 'rssi': ('ul_rssi_dbfs', 'dBFS'), 'bler': ('ul_bler', 'fraction')},
               'mac_dl': {'pucch_snr': ('pucch_snr_db', 'dB'), 'pucch_rssi': ('pucch_rssi_dbfs', 'dBFS'), 'bler': ('dl_bler', 'fraction')},
               'mac_goodput': {'dl': ('dl_goodput_mbps', 'Mbit/s'), 'ul': ('ul_goodput_mbps', 'Mbit/s')},
               'mac_ue': {'rsrp': ('ue_reported_rsrp_dbm', 'dBm'), 'pcmax': ('ue_reported_pcmax_dbm', 'dBm')},
               'neighbour_rsrp': {'rsrp': ('neighbour_reported_rsrp_raw', 'OAI logged value')}}
    rows = []
    for f in sorted((Path(run) / 'logs').glob('*/*.jsonl')):
        for ln in f.read_text(errors='replace').splitlines():
            r = json.loads(ln)
            if r.get('kind') != 'oai_log':
                continue
            ts, basis = oai_epoch(r['line'], r['epoch_ns'] / 1e9)
            for event, data in parse(r['line']):
                for key, (metric, unit) in metrics.get(event, {}).items():
                    if key in data:
                        rows.append({'epoch_s': ts, 'timestamp_basis': basis, 'node': f.parent.name, 'event': event,
                                     'ue_id': data.get('ue_id'), 'rnti': data.get('rnti'), 'pci': data.get('pci'),
                                     'metric': metric, 'value': float(data[key]), 'unit': unit})
    return sorted(rows, key=lambda r: r['epoch_s'])


def export_run(run, summary, analysis):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    run = Path(run)
    rows = radio_rows(run)
    table(run / 'radio_metrics.csv', ['epoch_s', 'timestamp_basis', 'node', 'event', 'ue_id', 'rnti', 'pci', 'metric', 'value', 'unit'], rows)
    table(run / 'checks.csv', ['status', 'title', 'detail'], summary['findings'])
    table(run / 'transport.csv', ['node', 'radio_verified', 'actual_tx_gain_db', 'U', 'L', 'O', 'D', 'S',
                                 'uhd_errors', 'returncode', 'seconds', 'interlock'], summary['nodes'])
    # Empty tables are explicit evidence of absent observations, never invented zero measurements.
    if analysis is None:
        table(run / 'packets.csv', ['seq', 'status', 'send_epoch_ns', 'send_relative_s', 'rtt_ms', 'error'], [])
        table(run / 'handover_metrics.csv', ['handover_id', 'outcome', 'signaling_duration_ms'], [])
        table(run / 'metrics.csv', ['section', 'metric', 'value'], [])
    else:
        export_analysis(run / 'packets.jsonl', run, analysis)
    fig, axes = plt.subplots(1, 2, figsize=(12, 4), layout='constrained')
    detail = (analysis or {}).get('handover', {}).get('details', [])
    complete = [d for d in detail if d['outcome'] == 'complete']
    if complete:
        axes[0].bar([d['handover_id'] for d in complete], [d['signaling_duration_ms'] for d in complete])
        axes[0].set_ylabel('Signaling duration (ms)')
    else:
        axes[0].text(.5, .5, 'No completed handover observations', ha='center', transform=axes[0].transAxes)
    axes[0].set_title(f"{summary['mode'].upper()}: completed handovers")
    axes[1].axis('off')
    axes[1].text(0, 1, '\n'.join(f"{r['status']}: {r['title']}" for r in summary['findings']), va='top', fontsize=9)
    fig.savefig(run / 'handover.png', dpi=150)
    plt.close(fig)
    fig, axes = plt.subplots(2, 1, figsize=(11, 6), layout='constrained')
    for ax, metric, unit in zip(axes, ('ul_snr_db', 'ul_rssi_dbfs'), ('dB', 'dBFS')):
        observed = [r for r in rows if r['metric'] == metric]
        if observed:
            t0 = rows[0]['epoch_s']
            for node in sorted({r['node'] for r in observed}):
                points = [r for r in observed if r['node'] == node]
                ax.plot([r['epoch_s'] - t0 for r in points], [r['value'] for r in points], label=node)
            ax.legend()
        else:
            ax.text(.5, .5, 'No corresponding radio observations', ha='center', transform=ax.transAxes)
        ax.set(xlabel='Seconds since first radio observation', ylabel=unit, title=metric)
    fig.savefig(run / 'radio_metrics.png', dpi=150)
    plt.close(fig)
    (run / 'artifacts.json').write_text(json.dumps({'mode': summary['mode'], 'files': sorted(p.name for p in run.iterdir()
                                                if p.suffix in ('.csv', '.png')), 'analysis_available': analysis is not None}, indent=2) + '\n')
