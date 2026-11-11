"""Slow scalar attenuation trajectories; these are not RF fading emulators."""
import csv
import math
import random
import subprocess
import json
import time
from pathlib import Path
from .config import require, finite, validate
from .runtime import append


def trajectory(kind, duration=60., step=1., low=10., high=40., seed=1, sigma=2., correlation_s=5.):
    for name, value in [('duration',duration),('step',step),('low',low),('high',high),('sigma',sigma),('correlation_s',correlation_s)]:
        finite(value,name)
    require(kind in ('ramp','step','triangle','shadow'), 'unknown trace kind')
    require(duration>0 and step>0 and duration/step <= 100000 and 0<=low<high<=60 and sigma>=0 and correlation_s>0, 'invalid trace bounds')
    rng = random.Random(seed)
    shadow = [0., 0.]
    rho = math.exp(-step/correlation_s)
    for i in range(math.floor(duration/step)+1):
        t = i*step
        x = t/duration
        if kind == 'step':
            x = float(x >= 0.5)
        if kind == 'triangle':
            x = 1-abs(2*x-1)
        vals = [low+(high-low)*x, high-(high-low)*x]
        if kind == 'shadow':
            for j in range(2):
                shadow[j] = rho*shadow[j]+math.sqrt(1-rho*rho)*rng.gauss(0,sigma)
                vals[j] = max(low, min(high, vals[j]+shadow[j]))
        yield {'t_s':round(t,6),'du0_db':round(vals[0]*4)/4, 'du1_db':round(vals[1]*4)/4}


def write_trace(path, **kw):
    rows = list(trajectory(**kw))
    with open(path,'w',newline='') as f:
        w = csv.DictWriter(f,fieldnames=['t_s','du0_db','du1_db']);w.writeheader();w.writerows(rows)
    Path(str(path)+'.json').write_text(json.dumps({'synthetic_control_trajectory':True, 'not_measured_channel':True, **kw},indent=2)+'\n')


def replay(c, trace, journal, execute=False):
    with open(trace) as f:
        rows = list(csv.DictReader(f))
    a = c['attenuator']
    require(c['mode']=='hardware', 'attenuator replay requires a hardware experiment')
    require(len(c['cells']) == 2, 'built-in attenuation trace maps exactly two cells; extend adapter/schema for more')
    require(rows, 'empty trace')
    previous = -1.
    for row in rows:
        t = finite(float(row['t_s']),'t_s')
        require(t >= 0 and t > previous, 'trace times must increase')
        if previous >= 0:
            require(t-previous >= a['minimum_interval_s']-1e-6, 'trace exceeds qualified attenuator update rate')
        previous = t
        for k in ('du0_db','du1_db'):
            v = finite(float(row[k]), k)
            require(a['min_db'] <= v <= a['max_db'] and abs(v*4-round(v*4))<1e-6, 'attenuation outside range or 0.25 dB grid')
    if not execute:
        return {'execute':False,'steps':len(rows),'duration_s':previous,'adapter_argv':a['adapter_argv']}
    validate(c,live=True)
    require(c['safety']['single_cell_attach_verified'] is True, 'qualify independent attachment on both cells first')
    require(a['adapter_argv'] and all(isinstance(x,str) for x in a['adapter_argv']), 'configure adapter_argv')
    require(a['readback_qualified'] is True, 'qualify attenuator adapter readback before replay')
    require(a['min_db'] <= a['safe_db'] <= a['max_db'], 'invalid recovery attenuation')
    def apply(values, phase, scheduled=None):
        requested = dict(zip([x['name'] for x in c['cells']], values))
        start = time.monotonic()
        r = subprocess.run(a['adapter_argv'], input=json.dumps({'attenuation_db':requested}),
                           text=True, capture_output=True, timeout=a['timeout_s'])
        require(r.returncode==0, 'attenuator adapter failed: '+r.stderr[-500:])
        actual = json.loads(r.stdout)['confirmed_db']
        require(set(actual)==set(requested), 'attenuator readback channel mapping mismatch')
        for k,v in requested.items():
            require(abs(finite(actual[k],k)-v)<=0.125, 'attenuator readback mismatch')
        append(journal, {'kind':'attenuation','epoch_ns':time.time_ns(),'phase':phase,'scheduled_s':scheduled,
                         'requested_db':requested,'confirmed_db':actual,'adapter_elapsed_s':time.monotonic()-start})
    start = time.monotonic()
    try:
        for row in rows:
            target = float(row['t_s'])
            remaining = start+target-time.monotonic()
            if remaining>0:
                time.sleep(remaining)
            lag = time.monotonic()-start-target
            require(lag <= a['maximum_lateness_s'], 'attenuator fell behind schedule; aborting rather than compressing trace')
            apply([float(row['du0_db']),float(row['du1_db'])], 'trace',target)
    finally:
        # Best-effort RF isolation, not a safety interlock. Fixed pads protect at 0 dB.
        apply([a['safe_db']]*2, 'recovery')
    return {'journal':journal,'steps':len(rows)}
