#!/usr/bin/env python3
"""Optional RC4DAT-6G-60 adapter. Requires pyusb and independent bench qualification.

Protocol grounded in the pinned OAI ci-scripts/attenuatorctl.py. The adapter is
not hardware-tested here. Never count its attenuation as receiver protection.
Input: {"attenuation_db":{"du0":10,"du1":40}}
Output: {"confirmed_db":{"du0":10.0,"du1":40.0}}
"""
import argparse
import json
import math
import sys


def main():
    import usb.core
    p=argparse.ArgumentParser()
    p.add_argument('--serial',required=True)
    p.add_argument('--model',default='RC4DAT-6G-60')
    p.add_argument('--du0-channels',default='1,2')
    p.add_argument('--du1-channels',default='3,4')
    p.add_argument('--info',action='store_true')
    a=p.parse_args()
    def query(dev,cmd):
        dev.write(1,cmd.encode(),timeout=1000)
        reply=dev.read(0x81,64,timeout=1000)
        out=[]
        for x in reply:
            if x in (0,255):break
            out.append(x)
        return bytes(out).decode('ascii').strip()
    found=[]
    for dev in usb.core.find(idVendor=0x20ce,idProduct=0x0023,find_all=True):
        if query(dev,'*:SN?')==a.serial:found.append(dev)
    if len(found)!=1:raise ValueError('Expected exactly one device with the requested serial')
    dev=found[0];model=query(dev,'*:MN?')
    if model!=a.model:raise ValueError(f'Model mismatch: {model}')
    if a.info:
        print(json.dumps({'serial':a.serial,'model':model,'readback':query(dev,'*:ATT?')}));return
    mapping={k:[int(n) for n in value.split(',')] for k,value in
             [('du0',a.du0_channels),('du1',a.du1_channels)]}
    channels=[ch for group in mapping.values() for ch in group]
    if len(set(channels))!=len(channels) or not all(1<=ch<=4 for ch in channels):raise ValueError('Overlapping/invalid channels')
    requested=json.load(sys.stdin)['attenuation_db']
    if set(requested)!=set(mapping):raise ValueError('Cell names must be du0 and du1')
    for k,v in requested.items():
        if not isinstance(v,(float,int)) or not math.isfinite(v) or not 0<=v<=60 or abs(v*4-round(v*4))>1e-6:
            raise ValueError('Attenuation must be 0..60 dB in 0.25 dB steps')
    for k,chs in mapping.items():
        for ch in chs:query(dev,f'*:CHAN:{ch}:SETATT:{requested[k]};')
    actual=[float(x) for x in query(dev,'*:ATT?').lstrip('*').split()]
    if len(actual)!=4:raise ValueError('Unexpected readback')
    confirmed={}
    for k,chs in mapping.items():
        vals=[actual[ch-1] for ch in chs]
        if any(abs(v-requested[k])>0.125 for v in vals):raise ValueError('Readback differs from request')
        confirmed[k]=vals[0]
    print(json.dumps({'confirmed_db':confirmed}))


if __name__=='__main__':
    try:main()
    except Exception as e:
        print(str(e),file=sys.stderr);sys.exit(1)
