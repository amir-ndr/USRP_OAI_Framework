"""Non-streaming inventory and external 10 MHz lock check of UE X310 34AF675.

Creates neither a TX nor an RX streamer. External PPS is not selected or certified.
"""
import json
import subprocess
import sys
import time

for name in ('nr-softmodem', 'nr-uesoftmodem'):
    check = subprocess.run(['pgrep', '-x', name], capture_output=True, text=True)
    if check.returncode == 0:
        raise SystemExit('Stop OAI before opening another UHD session.')
    if check.returncode != 1:
        raise SystemExit('Could not check whether OAI is running.')

sys.path.insert(0, '/usr/local/lib/python3.12/site-packages')
import uhd

args = 'type=x300,addr=192.168.10.5,serial=34AF675,master_clock_rate=184.32e6'
u = uhd.usrp.MultiUSRP(args)
u.set_rx_subdev_spec(uhd.usrp.SubdevSpec('B:0'))
u.set_tx_subdev_spec(uhd.usrp.SubdevSpec('B:0'))
u.set_clock_source('external')
sensors = list(u.get_mboard_sensor_names(0))
locked = False
for _ in range(50):
    if 'ref_locked' in sensors:
        locked = bool(u.get_mboard_sensor('ref_locked', 0).to_bool())
    if locked:
        break
    time.sleep(0.1)
u.set_rx_rate(7.68e6, 0)


def gain_range(r):
    return {'min_db': r.start(), 'max_db': r.stop(), 'step_db': r.step()}


result = {
    'device_args': args,
    'uhd_module': uhd.__file__,
    'subdev': 'B:0',
    'rx_info': dict(u.get_usrp_rx_info(0)),
    'tx_info': dict(u.get_usrp_tx_info(0)),
    'tx_gain_range': gain_range(u.get_tx_gain_range(0)),
    'rx_gain_range': gain_range(u.get_rx_gain_range(0)),
    'clock_source': u.get_clock_source(0),
    'time_source': u.get_time_source(0),
    'ref_locked': locked,
    'master_clock_hz': u.get_master_clock_rate(0),
    'rx_rate_sps': u.get_rx_rate(0),
    'rx_antenna': u.get_rx_antenna(0),
    'tx_antenna': u.get_tx_antenna(0),
    'external_pps_verified': False,
    'streamers_created': False,
    'transmits': False,
}
print(json.dumps(result, indent=2))
if not locked:
    raise SystemExit(2)
