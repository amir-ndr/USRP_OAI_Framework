# Could another X310 be a channel emulator?

Assessment only, 2026-11-09. No emulator code, FPGA image or RF wiring was implemented.

## What is possible

An SDR can in principle receive an RF waveform, process I/Q samples with gain/delay/filtering,
and retransmit it. The X310 offers FPGA/RFNoC processing, and UBX has independent RX/TX frontends.
That is enabling hardware, not a ready-made bidirectional fading emulator. The following practical
assessment is an engineering inference from those capabilities and this bench's one-UBX-per-radio
inventory. [UHD RFNoC](https://files.ettus.com/manual/page_rfnoc.html),
[X3x0 manual](https://files.ettus.com/manual/page_usrp_x3x0.html), [UBX](https://kb.ettus.com/UBX).

## Why the current three-radio handover roles do not leave a spare

A real two-cell USRP-UE experiment consumes A=cell, B=cell and C=UE. Repurposing C removes the UE;
repurposing B removes the second cell. Either choice prevents that three-role handover experiment.
With B inactive, it could instead support a separate one-cell channel-processing research experiment,
but then there would still be no second target cell. A modem/phone UE or additional radio resources
would change the available roles.

## Work a useful emulator would require

- A controlled RF network with fixed protection and enough isolation to prevent direct-path bypass
  and repeater feedback. OTA RX/retransmission on the same frequency can otherwise oscillate or leave
  the original signal visible at the UE, invalidating the alleged channel effect.
- Explicit UL and DL path handling. One installed UBX supplies one RX and one TX frontend. Simultaneous
  bidirectional processing requires more resources; TDD time-sharing requires verified fast switching,
  state handling and frame alignment. Two independent cell paths add further routing requirements.
- A measured latency budget. Host receive-process-send over the current shared 1 GbE link is not a
  transparent real-time NR relay. Buffering/jitter and inserted delay can break random access/timing.
  FPGA processing may reduce latency, but requires an implemented, tested graph/image and calibration.
- Validation of delay, gain, noise and any Doppler/tap process against a reference waveform, including
  a unity-channel baseline. Record the emulator clock, saturation, loss and processing latency.

## More useful next experiments

First establish single-cell attachment, then manual F1. For reproducible slow source/target level
crossovers, a protected programmable attenuator network is simpler than building an SDR emulator.
It controls scalar loss, not full multipath/Doppler. RFsim is useful for software channel-model
studies, with model units/behavior validated separately; it does not alter a real antenna path.
A dedicated RF channel emulator is the stronger option for calibrated fast fading/delay studies.

The framework intentionally offers no `usrp channel-emulator` command. The implementation and its
qualification would be a separate project; the current workflow must not imply that it already exists.
