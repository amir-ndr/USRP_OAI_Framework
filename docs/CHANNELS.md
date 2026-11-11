# Channel conditions: what can actually be changed

## Three different experiment modes

| Mode | What changes the signal | What it can establish |
|---|---|---|
| Real RF with movement/shielded antennas | Physical propagation, interference, position and orientation | Realistic observations with limited repeatability |
| Real RF with programmable attenuation | Scalar attenuation on a protected RF path | Repeatable slow coverage/crossover experiments |
| RFsim | Software modifies the samples used by OAI's simulated RF device | Controlled simulated channel/protocol experiments, without X310 transmission |

Putting `TDL_C` in an RFsim configuration does not make an X310-to-phone path a TDL-C channel. Fast fading, multipath delay taps, Doppler and angle-dependent MIMO need a suitable RF channel emulator or a deliberately engineered waveform-processing setup. They are not implemented by a scalar attenuator or by Linux `netem` on the core network. `netem` changes packet-network conditions, a separate experimental variable.

## Slow hardware attenuation trajectories

`oaiho trace` produces CSV columns `t_s,du0_db,du1_db`. The values are **additional programmable attenuation**, not total path loss, RSRP, SINR or hardware TX gain. Fixed passive protection remains outside the trajectory.

| Trace | Definition | Use |
|---|---|---|
| `ramp` | DU0 attenuation rises while DU1 falls | One controlled cell-edge crossing |
| `step` | Both levels swap at the midpoint | Abrupt obstruction/coverage-change stress |
| `triangle` | Crossing and return within the run | Reverse handover and ping-pong sensitivity |
| `shadow` | Ramp plus independent bounded correlated Gaussian variation in dB | Slow shadowing-like variation; not a standardized full channel model |

The shadow state follows `s[k] = rho*s[k-1] + sqrt(1-rho^2)*N(0,sigma^2)`, with `rho=exp(-dt/correlation_s)`. The output is clipped to the selected attenuation range and quantized to 0.25 dB. Clipping/quantization alter the intended distribution; report them. A seed reproduces the **control trajectory**, not a complete RF realization or OAI RNG state.

```bash
oaiho trace --kind shadow --duration 90 --step 1 \
  --low 10 --high 40 --sigma 2 --correlation-s 5 --seed 42 --out shadow42.csv
```

Choose actual levels after establishing a working link budget. If both cells disappear, a trace has tested an outage instead of a viable handover. Fixed total loss, UL/DL directionality and UE power control affect the result. Preserve an initial stable serving interval and a final stable target interval; prepend/append CSV hold rows with strictly increasing times if needed.

## Optional Mini-Circuits adapter

The included `adapters/minicircuits_usb.py` uses the USB command protocol found in OAI's inspected attenuator controller [S16]. It is **not physically tested** here. It selects one device by exact serial and checks its model, channel assignment and readback. It intentionally does not detach kernel drivers or change system USB permissions automatically.

Install PyUSB in the adapter's Python environment and establish appropriate lab USB access. With the transmitters off and a safe RF network:

```bash
python -m pip install pyusb
python adapters/minicircuits_usb.py --serial YOUR_SERIAL --info
```

An example YAML adapter command is:

```yaml
attenuator:
  adapter_argv:
    - /absolute/path/USRP_5g_OAI/.venv/bin/python
    - /absolute/path/USRP_5g_OAI/adapters/minicircuits_usb.py
    - --serial
    - YOUR_SERIAL
    - --du0-channels
    - '1,2'
    - --du1-channels
    - '3,4'
  readback_qualified: false
  min_db: 0
  max_db: 60
  safe_db: 60
  minimum_interval_s: 1.0
  maximum_lateness_s: 0.5
  timeout_s: 3.0
```

Channels 1/2 and 3/4 are only **logical grouping examples**. They do not prescribe the RF wiring. A pair might correspond to two separate physical paths; a bidirectional reciprocal single-port path might need only one channel. Verify the correct group for your exact port arrangement and ensure no uncontrolled branch bypasses the intended attenuation. Never duplicate or overlap attenuator channels across cell groups.

Bench-test set/readback at several values, timing, disconnect behaviour and recovery before marking `readback_qualified: true`. The adapter returns JSON to stdout; diagnostics go to stderr. Replay uses scheduled monotonic times, records command latency and readback, and aborts rather than silently compressing a late schedule. It attempts to set `safe_db` on normal completion or failure; a disconnected device or power failure can defeat that attempt. Fixed pads remain essential.

## Other attenuators or channel emulators

Supply an executable argument list in `adapter_argv`. The runner invokes it without a shell, sends one JSON object on stdin:

```json
{"attenuation_db":{"du0":10.0,"du1":40.0}}
```

and requires an independently queried readback on stdout:

```json
{"confirmed_db":{"du0":10.0,"du1":40.0}}
```

Do not merely echo the request as readback. The generic interface is a scalar attenuation contract. A channel emulator with delay/Doppler/taps should have a separate explicit schema, capability checks and metadata, not pretend to be this adapter.

## RFsim channel models

Use `mode: rfsim`, `handover.trigger: manual`, and set `rfsim_channel.enabled: true`. This generator exposes `model`, `ploss_db`, `noise_power_db`, `forgetfact`, `offset_samples` and `ds_tdl`. It restricts model names to a small set found in the pinned OAI enum. Their generation is tested, but actual RFsim model behaviour and handover interaction still require a run of OAI.

The channel names follow **RFsim server/client roles**, not physical UL/DL labels. Because the handover UE is the server, its receive models are `rfsimu_channel_ue0`, `rfsimu_channel_ue1`, etc.; each DU client uses `rfsimu_channel_enB0`. The server's model index follows client connection order. Start DU0, establish the UE connection, then DU1 consistently and verify the mapping [S17].

The current generator assigns the same channel parameter set to the model entries. Extend `channel_config` to support per-link parameters if needed; do not claim asymmetric models from the existing shared block. The hardware attenuation CSV does not control RFsim. Noise values use OAI's model convention, not a measured receiver dBm scale; consult the pinned implementation before converting them to SNR or comparing to hardware measurements. TDL delay-spread units likewise belong to that API; confirm them against the pinned source and an impulse-response test before using them quantitatively.

Suggested sequence: establish a noise-free/default RFsim baseline, enable AWGN at a mild setting, verify the actual noise/SNR effect, then change one model parameter at a time. Do not compare software simulated timing directly with X310 wall-clock timing: RFsim is CPU-bound and need not run in real time [S18].
