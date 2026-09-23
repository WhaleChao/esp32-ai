# Fly connectome verification

Run every command from the repository root. The tools use the five files of a
verified bundle; see [MODEL_FORMATS.md](MODEL_FORMATS.md). They do not import a
research checkout or a private dataset. No command here flashes firmware. Device
commands test the firmware already on the connected board, which may differ from
the local source or compiled binary.

## Setup

Host checks need Python 3, a C11 compiler named `cc`, libm, zlib and compiler
support for AddressSanitizer and UndefinedBehaviorSanitizer. macOS with Xcode
command line tools and Linux with a GCC/Clang development toolchain are the
intended hosts. The C runtime requires little-endian IEEE-754 float32.

Serial commands additionally need pyserial:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r firmware/esp32_fly/tools/requirements-verify.txt
```

Choose a new `--report` path for each run. An existing report is never overwritten.
The directory can contain previous reports, but the selected JSON and device
JSONL log names must be unused. Keep reports under ignored `artifacts/`.
The bundle's manifest must match the checked-in trusted manifest, or another
explicitly selected `--manifest PATH`.

## Full host suite

```sh
python3 firmware/esp32_fly/tools/verify.py host \
  --bundle artifacts/fly \
  --report artifacts/fly/verification/host-001.json
```

This compiles the checked-in C runtime bridge in a temporary directory, runs
self-contained decoder, stream, cache, SIMD-fixture and world checks under
ASan/UBSan, and then two numerical suites.

Graph gold runs in five execution modes:

| Host mode | Execution |
|---|---|
| 0 | Scalar streaming reference |
| 1 | FP32, decoded varint cache |
| 2 | Q29, decoded varint cache |
| 3 | FP32, packed index/contact cache |
| 4 | Q29, packed index/contact cache |

These host mode numbers are not the firmware's `MODE` numbers. Optimized host
checks execute both graph ranges sequentially with the scalar dot hook. They
do not test real Xtensa instructions or concurrent scheduling. Each mode
compares 9,275,712 values for the pinned graph, using all six cases and 32 steps.
State propagates from actual output, not from the expected gold output.

Escape gold then replays the circuit in the same five modes: each case starts
from zero state, drives the looming inputs with the recorded loom levels and
compares every escape output neuron against the reference.

`connectome_simd_test.h` supplies 1,088 aligned dot fixtures covering lengths 0..128
in multiples of eight, signed extremes and deterministic random inputs. The host
test checks those fixtures and deliberately broken hooks. Firmware boot runs the
same test separately on each core against the real assembly function. A scalar
host PASS does not prove SIMD execution on a board.

### Escape simulation

```sh
.venv/bin/python -m research.fly.escape_sim --bundle artifacts/fly
```

Runs the demo's loop on the host with the firmware's world and the real graph in
host mode 2: 20 minutes of world time, seed 61000 and a 1.7-second neural cycle
by default. As on the board, each decision is applied at the end of its cycle
against the world sampled at its start: the jump is aimed from the sampled
heading, and only the sampled spider counts as escaped. It prints escape, catch
and approach counts. They are observations, not a gate.

## Connected board

Pass the actual serial port explicitly. Only one process may use the port;
close Arduino Serial Monitor or other serial tools before running a command.
The examples use `/dev/cu.usbmodem1101`; Linux ports commonly differ.
Opening USB serial can reset some boards. The verifier waits for firmware
readiness and checks the graph identity, the circuit identity and shape, the
verification protocol and a passing SIMD boot result before each test.

Device tests stop the demo. A successful test issues `PLAY` and waits for
confirmation of a fresh session. On transfer failure they leave an ERROR report
and do not send text into an unfinished binary transfer. Reset the board before
retrying after a binary-protocol error.

### SIMD boot result

```sh
.venv/bin/python firmware/esp32_fly/tools/verify.py device-simd \
  --bundle artifacts/fly --port /dev/cu.usbmodem1101 \
  --report artifacts/fly/verification/device-simd-001.json
```

This reads the boot result; it does not trigger a fresh self-test. The firmware
reports `simd_core0` and `simd_core1` separately. To execute the boot tests
again, reset the board before invoking the verifier.

### Full device graph gold

```sh
.venv/bin/python firmware/esp32_fly/tools/verify.py device-graph \
  --bundle artifacts/fly --port /dev/cu.usbmodem1101 \
  --report artifacts/fly/verification/device-graph-001.json
```

This selects firmware mode 6, streams each case's initial state and stimuli,
and receives complete updated state vectors. Graph decoding, recurrence and
SIMD computation happen on the ESP32. The host checks every returned value
and transfer checksum. All six cases and 32 steps run by default; allow several
minutes for computation and serial transfer.

For a short protocol check, append `--steps 1`. It still visits every case but
reports `full_suite: false`. A partial PASS is not a full graph gold PASS.

### Escape gold on the device

```sh
.venv/bin/python firmware/esp32_fly/tools/verify.py device-escape \
  --bundle artifacts/fly --port /dev/cu.usbmodem1101 \
  --report artifacts/fly/verification/device-escape-001.json
```

Each gold case starts with `ZERO`, which clears graph state, and then sends one
`LOOM` command per step carrying the exact float32 bits of both loom levels. The
firmware encodes them through the circuit's own input indices, runs one graph
step and returns every escape output neuron with the drive per side. The host
compares the outputs against the gold within its tolerances and recomputes each
drive from the returned outputs, so the firmware's own averaging is checked too.
Five cases of 24 steps are 120 graph steps, about three and a half minutes.

This exercises the deployed circuit indices, the input encoding and the drive
averaging, which the graph gold does not: graph gold supplies whole state
vectors from the host instead.

### Autonomous escape replay

```sh
.venv/bin/python firmware/esp32_fly/tools/verify.py device-live \
  --bundle artifacts/fly --port /dev/cu.usbmodem1101 \
  --report artifacts/fly/verification/device-live-001.json
```

Restarts the demo with `PLAY` and follows 60 neural decisions by default;
`--steps` selects another count. The host sends no stimuli and no commands
during the session. It decodes the exact float32 loom levels the firmware
reported, replays each step on the portable runtime and compares every escape
output neuron at an absolute tolerance of 3e-4. It rejects missing, duplicated
and out-of-order steps.

For every escape the firmware reports, the host checks that a drive was above
the threshold and that the side matches the larger drive. When an escape may
happen at all is the world's decision: the fly must be walking and out of its
refractory period, so a step above threshold without an escape is expected.

Escape and catch counts are observations. There is no automatic success-rate
threshold in this parity test: the demo's behavior depends on the world's random
walk, and a slow neural cycle means the fly sometimes reacts late.

## Protocol tests and reports

```sh
python3 -m unittest discover -s firmware/esp32_fly/tools -p 'test_*.py'
```

These tests exercise malformed bundles, partial serial lines, acknowledged
chunks, short transfers, bad checksums, numerical mismatch rejection, dropped
steps, escape-side consistency and device identity checks without hardware.
Fake serial fixtures test the verifier only and are never used as evidence
about a board.

A result records the asset identities, local source hashes, comparison scope
and device INFO when applicable. Local source hashes describe verification
inputs; they are not proof of which firmware is installed. Device firmware
SHA-256 remains null because the serial protocol does not report an attested
binary identity. FNV-1a is an accidental-mismatch check, not a cryptographic
identity for the board. A local `.bin` hash must never be substituted for it.

Device logs preserve received JSON telemetry; binary state payloads are checked
as they arrive rather than retained as large trace files. Reports distinguish
host fixtures from hardware results, full gold from partial gold, and numerical
parity from demo behavior. ERROR exits with status 1; PASS exits with 0.
