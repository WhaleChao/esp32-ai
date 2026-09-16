# Fruit fly connectome on ESP32-S3

This sketch runs a 48,311-neuron subgraph of the male fruit fly connectome on an
ESP32-S3 N16R8 and uses it to escape a spider on a small OLED. The connectome is
the part that is ported: every retained connection and its synaptic contact
count, compressed into the `flymodel` flash partition at `0x210000`.

Nothing is trained. A spider creeps toward a walking fly; its growing size on
screen drives the fly's own looming-detector neurons (LC4 and LPLC2) on the side
it approaches from. When the escape descending neurons on that side (DNp01, the
giant fiber, plus DNp02, DNp04 and DNp11) become active enough, the fly jumps
away from that side and the spider gives up. The wiring decides, not a learned
model: [why we know that](../../docs/fly-connectome/README.md#what-the-wiring-does-by-itself).

The whole nervous system does not fit in 16 MB of flash, even compressed
losslessly, so this is the part that does; see
[why only part of it](../../docs/fly-connectome/README.md#why-only-part-of-the-nervous-system).

## Build and flash

```bash
scripts/fetch_model.sh fly   # download and verify the graph, circuit and gold
scripts/deploy.sh fly        # bundle check, host gold, compile, flash graph, flash firmware
```

`deploy.sh` is the authoritative procedure. It checks the bundle against
`model_bundle.json`, runs the full graph and escape gold on the host, and
compiles before either flash, so a failed check cannot leave a new graph under
old firmware.

The sketch needs the `esp32:esp32` core 3.3.10 and three display libraries:

```bash
arduino-cli core install esp32:esp32@3.3.10 --additional-urls https://espressif.github.io/arduino-esp32/package_esp32_index.json
arduino-cli lib install 'Adafruit SH110X@2.1.14' 'Adafruit GFX Library@1.12.6' 'Adafruit BusIO@1.17.4'
```

The host gold builds C with `cc`, and needs zlib and the address and
undefined-behaviour sanitizers. Xcode command line tools on macOS or a GCC/Clang
toolchain on Linux provide them.

## Wiring

An SH1106 I2C OLED, 128 x 64 pixels:

| OLED pin | ESP32-S3 |
|---|---|
| VCC | 3V3 |
| GND | GND |
| SDA | GPIO18 |
| SCL | GPIO46 |

The firmware probes `0x3C`, then `0x3D`. Without a display the demo still runs
and reports over serial.

## The artifact set

| file | why the device needs it |
|---|---|
| `model_bundle.json` | names the other five files with their sizes and hashes; `deploy.sh` refuses to build without it |
| `connectome.fcl` | the graph, written to the `flymodel` partition |
| `escape-circuit.json` | which neurons are the eyes and which are the escape outputs, with the input strength and escape threshold |
| `gold-device-order.bin` | graph reference: six cases of 32 steps |
| `escape-gold.bin` | escape reference: how the outputs respond to five spider cases |
| `neurons.csv` | MaleCNS body ID, class, type and side for every neuron |

Only the graph and the compiled circuit reach the board; the gold files and the
neuron table are checked on the host. Binary layouts and the escape rule are in
[MODEL_FORMATS.md](MODEL_FORMATS.md).

## What you see

```text
+--------------------------------+
| [####   ]      3/1     [#     ]|
|--------------------------------|
|            \o/                 |
|             |        >o<       |
|            / \                 |
|                                |
+--------------------------------+
```

The bar in each top corner is the activity of that side's escape neurons, and
the notch in the middle of a bar is the escape threshold: when the fill passes
the notch, the fly jumps. So you can watch a decision build up before the fly
moves. The two sides are the fly's own left and right eye, not the screen's, so
which bar fills depends on where the spider is relative to the way the fly is
facing, not on which half of the screen it is in. Between the bars is the count
of escapes and catches.

Below the line the fly walks around, wings fluttering, and the spider creeps in
from an edge. When the fly escapes it jumps sideways with a short trail, and the
spider walks off screen. If the spider reaches the fly first, the catch counter
goes up and the fly reappears elsewhere.

Physics runs at 120 Hz, the display at about 19 frames per second, and a neural
decision arrives about every 1.7 seconds. Reactions are therefore slow on
purpose: the spider needs roughly 5 seconds of looming before the fly jumps, and
the fly sometimes jumps once more after the spider has left, because the escape
neurons take a few seconds to calm down.

## Measured

On this board with the released bundle, checked from the host over serial:

| | |
|---|---:|
| graph gold on the device | 9,275,712 values, 6 cases x 32 steps, max error 3.9e-7 |
| escape gold on the device | 960 values, 5 cases x 24 steps, max error 1.8e-7 |
| autonomous escapes replayed on the host | 60 decisions, max error 3.0e-8 |
| SIMD self-test | passed on both cores |
| one neural decision | 1.70 s, of which 1.69 s is the graph |
| decoded-block cache | 530 of 1,510 blocks, 6.1 MB of PSRAM |
| application | 426,274 of 2,097,152 bytes |

In a 20-minute simulation of the same loop on a computer, the fly escaped 48
times and was caught 17 times across 65 spider approaches. Escape counts depend
on the world's random walk and are not a quality gate. The commands that produce
these numbers are in [VERIFICATION.md](VERIFICATION.md).

## Reuse it

The graph executor knows nothing about spiders. `runtime/connectome_*.h`,
`runtime/lz4_block*.h` and `connectome_simd.S` decode and run the graph; the
demo-specific parts are separate:

| to change | edit |
|---|---|
| which neurons are the inputs and outputs | export a new circuit with [`research/fly/circuit.py`](../../research/fly/README.md) |
| how the world becomes a stimulus, and what an escape does | `escape_app.h` and `escape_world.h` |
| what the screen shows | `escape_ui.h` |

To find your own circuit, stimulate any group of neurons with
[`research/fly/probe.py`](../../research/fly/README.md) and see which descending
neurons answer. Export it, review the candidate manifest, then deploy it with
its own manifest:

```bash
ARTIFACTS=artifacts/fly/mine MANIFEST=artifacts/fly/mine/model_bundle.json scripts/deploy.sh fly
```

## Firmware layout

| File | Responsibility |
|---|---|
| `esp32_fly.ino` | Allocation, graph validation, two-core execution, startup and serial commands |
| `connectome_simd.S` | ESP32-S3 signed 16-bit dot product in IRAM |
| `escape_app.h` | Loom encoding, one neural cycle, escape decision and telemetry |
| `escape_world.h` | Fly, spider, loom levels and the escape rule |
| `escape_ui.h` | OLED rendering and the world's 120 Hz task |

`partitions.csv` gives the application 2 MiB at `0x10000` and `flymodel`
13.875 MiB at `0x210000`, leaving 374,581 bytes after the graph. The rest is NVS
and core dumps; there is no OTA slot.

At startup the firmware checks the graph fingerprint, validates every compressed
block and runs the SIMD self-test on both cores. Graph state stays in internal
RAM as Q29 values; the graph is read from mapped Flash, and decoded blocks are
cached in PSRAM up to 6 MiB, keeping 512 KiB free. Two workers split the rows of
each step and synchronize before the state is swapped. A separate task runs the
world and draws the OLED.

## Serial commands

USB CDC at 921600 baud. Commands end with a newline and are handled between
neural decisions, so one may wait for the current decision to finish.

| Command | Effect |
|---|---|
| `INFO` | Graph and circuit identity, execution mode, cache and memory counters |
| `STATUS` | Counters, fly and spider positions, last loom levels and drives |
| `STOP` | Pause the demo |
| `PLAY [seed]` | Restart the world from a seed, clearing neural state |
| `ZERO` | Clear neural state for a verification run |
| `LOOM l r` | One neural step with exact loom levels, given as float32 hex |
| `MODE 0` to `MODE 8` | Pause and select a graph execution mode; `PLAY` returns to mode 6 |
| `INIT` | Pause and receive an initial float32 graph state |
| `STEP` | Pause, receive graph input and return one updated state |

`INIT`, `STEP`, `ZERO` and `LOOM` serve [VERIFICATION.md](VERIFICATION.md).
`INIT` and `STEP` use a binary protocol with FNV-1a checksums and acknowledged
chunks; a transfer failure stops inference, so reset the board before retrying.

## Running a step by hand

What `deploy.sh fly` does, one command at a time:

```bash
python3 firmware/esp32_fly/tools/prepare_assets.py --bundle artifacts/fly
python3 firmware/esp32_fly/tools/verify.py host --bundle artifacts/fly \
  --report artifacts/fly/verification/host-001.json

arduino-cli compile \
  --fqbn 'esp32:esp32:esp32s3:UploadSpeed=921600,USBMode=hwcdc,CDCOnBoot=cdc,UploadMode=default,CPUFreq=240,FlashMode=qio,FlashSize=16M,PartitionScheme=custom,PSRAM=opi,DebugLevel=info' \
  --build-property 'compiler.optimization_flags=-O3 -ffp-contract=off' \
  --build-property 'upload.maximum_size=2097152' \
  --build-path "$PWD/artifacts/fly/build" \
  firmware/esp32_fly

esptool --chip esp32s3 --port /dev/cu.usbmodem1101 --baud 921600 \
  write_flash 0x210000 artifacts/fly/connectome.fcl
arduino-cli upload -p /dev/cu.usbmodem1101 \
  --fqbn 'esp32:esp32:esp32s3:UploadSpeed=921600,USBMode=hwcdc,CDCOnBoot=cdc,UploadMode=default,CPUFreq=240,FlashMode=qio,FlashSize=16M,PartitionScheme=custom,PSRAM=opi,DebugLevel=info' \
  --input-dir artifacts/fly/build firmware/esp32_fly
```

The circuit is compiled into the application; the graph is flashed on its own.
The merged image Arduino produces does not contain the graph, so it is not a
complete installation.
