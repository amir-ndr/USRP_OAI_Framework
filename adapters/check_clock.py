#!/usr/bin/env python3
"""Clock/time checks with OAI STOPPED. No sample streaming; nothing is transmitted.

  --octoclock IP      read the OctoClock-G status sensors (GPS / reference / switch position)
  --args ARGS         one X310: select external 10 MHz + PPS, read ref_locked, watch the PPS counter advance
  --pair IP_A IP_B    both X310s in one session: set time at the same PPS edge, then compare last-PPS times

Use the UHD Python bindings that match the radios' FPGA images (pass --uhd-python, e.g.
/usr/local/lib/python3.12/site-packages for a source-built UHD in /usr/local).
"""
import argparse
import json
import sys
import time


def octoclock(uhd, ip):
    clk = uhd.usrp_clock.MultiUSRPClock(f'addr={ip}')
    names = list(clk.get_sensor_names(0))
    values = {}
    for n in names:
        try:
            s = clk.get_sensor(n, 0)
            values[n] = s.to_pp_string() if hasattr(s, 'to_pp_string') else str(s)
        except Exception as e:                   # one unreadable sensor must not hide the others
            values[n] = f'unreadable: {e}'
    using = values.get('using_ref', '').lower()
    ext = 'true' in values.get('ext_ref_detected', '').lower()
    # A common reference needs a valid source, not GPS lock: the internal oscillator/GPSDO, or a
    # detected external input. GPS lock only adds absolute-time accuracy.
    ok = bool(values) and ('internal' in using or ('external' in using and ext) or not using)
    return {'octoclock': ip, 'sensors': values, 'reference_in_use': using or 'unknown', 'external_input_detected': ext,
            'gps_locked': values.get('gps_locked'),
            'scope': 'OctoClock status only; each X310 must still report ref_locked'}, ok


def one_radio(uhd, args, reference_only=False):
    u = uhd.usrp.MultiUSRP(args)
    u.set_clock_source('external')
    if not reference_only:
        u.set_time_source('external')
    time.sleep(1)
    names = u.get_mboard_sensor_names(0)
    ref = bool(u.get_mboard_sensor('ref_locked', 0).to_bool()) if 'ref_locked' in names else None
    if reference_only:
        return {'args': args, 'clock_source': u.get_clock_source(0), 'ref_locked': ref,
                'scope': 'Frequency reference only; UE frame timing comes from the serving cell'}, bool(ref)
    first = u.get_time_last_pps().get_real_secs(); time.sleep(1.2)
    last = u.get_time_last_pps().get_real_secs()
    ok = bool(ref) and last > first
    return {'args': args, 'clock_source': u.get_clock_source(0), 'time_source': u.get_time_source(0),
            'ref_locked': ref, 'pps_advanced': last > first, 'first_pps_time': first, 'last_pps_time': last,
            'scope': 'Local lock and PPS observation only; see --pair for a cross-radio check'}, ok


def pair(uhd, ip_a, ip_b):
    u = uhd.usrp.MultiUSRP(f'addr0={ip_a},addr1={ip_b},type=x300')
    for mb in range(2):
        u.set_clock_source('external', mb); u.set_time_source('external', mb)
    time.sleep(1)
    u.set_time_unknown_pps(uhd.types.TimeSpec(0.0))     # both latch 0.0 on the same PPS edge
    time.sleep(1.5)
    t = [u.get_time_last_pps(mb).get_real_secs() for mb in range(2)]
    ok = abs(t[0] - t[1]) < 1e-6
    return {'radios': [ip_a, ip_b], 'last_pps_time': t, 'same_pps_epoch': ok,
            'scope': 'Both radios see the same PPS edge; OAI still sets its own epoch from host CLOCK_TAI at start'}, ok


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--uhd-python', help='directory containing the matching uhd package')
    p.add_argument('--reference-only', action='store_true', help='check external 10 MHz without requiring PPS (nrUE)')
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument('--octoclock'); g.add_argument('--args'); g.add_argument('--pair', nargs=2)
    a = p.parse_args()
    if a.uhd_python:
        sys.path.insert(0, a.uhd_python)
    import uhd
    if a.octoclock:
        result, ok = octoclock(uhd, a.octoclock)
    elif a.args:
        result, ok = one_radio(uhd, a.args, a.reference_only)
    else:
        result, ok = pair(uhd, *a.pair)
    result['uhd_module'] = uhd.__file__
    print(json.dumps(result, indent=2))
    if not ok:
        raise SystemExit(2)


if __name__ == '__main__':
    main()
