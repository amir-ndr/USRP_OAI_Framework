# X310 receive transport qualification

One 60-second UE-only baseline followed by one 60-second concurrent three-radio test. All receivers used 7.68 MS/s, sc16 on the wire and CPU, slot B:0, a 184.32 MHz master clock, and external frequency reference. No transmit rate was specified; every native summary reports zero transmitted samples.

| Condition | Radio | Dropped samples | RX sequence-error events | Dropped sample fraction |
|---|---|---:|---:|---:|
| ue_alone | ue | 0 | 0 | 0.00000% |
| three_concurrent | du0 | 67,340 | 46 | 0.01461% |
| three_concurrent | du1 | 71,344 | 46 | 0.01548% |
| three_concurrent | ue | 64,792 | 46 | 0.01406% |

All four runs reported zero overruns and RX timeouts. Process exit code 0 alone is not a clean-transport criterion: the native benchmark permits nonzero error counters unless thresholds are explicitly supplied.

The adapter driver reported zero additional `rx_missed` in the isolated phase and 559 in the concurrent phase. Its RX ring is already at its reported maximum of 4096; host RX/TX maximum socket buffers are 33,554,432 bytes. The driver identifies as r8152 v2.22.1 on the USB adapter. These observations locate evidence of loss in the current receive path, but do not isolate USB hardware, host/VM scheduling or driver behavior as the sole cause.

The isolated UE was clean in this observation; concurrent operation of all three was not. Keep the conservative transport gate. A separate physical Ethernet path and/or a native host should be qualified before treating traffic gaps as handover effects. Requalify every concurrent path after any change; a second adapter is not an automatic guarantee that the remaining paths become clean.

Limits: receive-only, no full-duplex test, no OAI real-time workload, no cellular attachment or handover, no RSRP calibration. There is one observation per condition, and no isolated DU baselines in this capture. This is infrastructure screening, not a statistical handover comparison.

Raw evidence: [summary.json](summary.json), [UE alone](ue_alone/ue.log), [DU0 concurrent](three_concurrent/du0.log), [DU1 concurrent](three_concurrent/du1.log), [UE concurrent](three_concurrent/ue.log).
