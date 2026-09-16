# Fruit fly connectome on ESP32-S3

This port runs a selected MaleCNS v1.0 neural connectivity graph on an ESP32-S3
with 16 MiB Flash and 8 MiB PSRAM. It preserves the selected neurons' directed
connections and integer synaptic contact counts. Every simulation step processes
all retained edges, using compressed Flash storage, a PSRAM cache and two cores.

The [escape firmware](../../firmware/esp32_fly/README.md) demonstrates how to
connect a stimulus and read a decision out of that graph, without training
anything. It is one application; the graph decoder and numerical executor do not
depend on its world or display.

## What is on the device?

| Quantity | Value |
|---|---:|
| Selected neurons | 48,311 |
| Directed neuron-to-neuron edges | 9,462,135 |
| Sum of synaptic contact counts | 49,481,754 |
| Compressed graph in Flash | 14,174,411 bytes (13.518 MiB) |
| Escape circuit compiled into the firmware | 311 input and 8 output neuron indices |

An edge groups the contacts from one source neuron to one destination neuron.
For example, a stored weight of 20 means 20 contacts on that directed pair.
The graph has 9.46 million such pairs, not 9.46 million individual contacts.
Compression preserves counts from 1 to 1,878, including 633,813 weights above 15.
It does not quantize them to four bits.

The device stores connectivity and counts, not electron microscopy images,
neuron meshes or the locations of individual synapses. The graph is fixed, and
an application supplies only which neurons it stimulates and which it reads.

## Which neurons are included?

The source population consists of rows with a nonempty `superclass` and
`status != Glia`, sorted by body ID. It contains 166,700 annotated cells.
The port retains classes beginning with `cb_`, `visual_` or `descending_neuron`,
then keeps every connection whose two endpoints are both retained.

| Retained superclass | Neurons |
|---|---:|
| `cb_intrinsic` | 32,164 |
| `cb_sensory` | 4,868 |
| `cb_sensory_tbc` | 14 |
| `cb_motor` | 107 |
| `cb_endocrine` | 72 |
| `cb_efferent` | 4 |
| `visual_projection` | 9,201 |
| `visual_projection_tbc` | 2 |
| `visual_centrifugal` | 563 |
| `descending_neuron` | 1,314 |
| `descending_neuron_tbc` | 2 |
| **Total** | **48,311** |

This includes central-brain classes, visual projection and centrifugal cells,
and descending neurons. It excludes all `vnc_*` classes, `ol_intrinsic`,
`ol_sensory`, ascending neurons and the remaining classes enumerated in
[subgraph.json](subgraph.json). Relative to the filtered source population,
118,389 cells and 16,120,803 edges are excluded.

**This is not the whole brain with only the nerve cord removed.** Most optic-lobe
intrinsic neurons are excluded too. Selection uses cell annotations, not spatial
coordinates: retained descending cells can extend into the nerve cord, and
contacts on those cells are not spatially cropped. No extra strength threshold
is applied inside the selected subgraph.

The model package's `neurons.csv` links execution index, canonical index, source
body ID, superclass, class, cell type and side. Canonical order is ascending body ID;
the compressed file supplies its permutation into execution order. The
[superclass connection table](connections-by-superclass.csv) records retained
connectivity totals; [source-data.json](source-data.json) pins the source files.

## Why only part of the nervous system

The whole MaleCNS nervous system is 166,700 neurons and 25.6 million connections.
The smallest lossless encoding we measured for it is 24.49 MiB, after trying graph
codecs such as Zuckerli with neuron reordering, OpenZL, MM-RePair and k²-trees.
Removing only the nerve cord still leaves 20.99 MiB. The board has about 14 MiB
of flash for the graph, and PSRAM cannot hold it because it is cleared at
power-off. 4-bit weights did not fit either, and they changed the results.

| Graph | Neurons | Connections | Smallest lossless size |
|---|---:|---:|---:|
| Whole nervous system | 166,700 | 25,582,938 | 24.49 MiB |
| Without nerve cord classes | 146,271 | ~21,880,000 | 20.99 MiB |
| This port | 48,311 | 9,462,135 | 8.61 MiB |

So the port keeps the central brain, visual projection and descending neurons,
stored exactly. On the board it takes 13.5 MiB rather than 8.61 MiB, because the
smallest encoding cannot be decoded piece by piece; the runtime format stores
independent LZ4 blocks that are decoded as the graph runs.

## How it fits and runs

The FCL1 format stores incoming adjacency lists in 32-row blocks. Source indices
use sorted deltas, contact counts use unsigned varints, and each block is
compressed independently with raw LZ4. The graph has 1,510 blocks. The executor
can decode blocks as needed without expanding the whole graph in RAM.

| Memory | Contents |
|---|---|
| Flash | Compressed graph; firmware and escape circuit in a separate application partition |
| PSRAM | Graph state/input arrays, decoding workspaces and a cache of decoded blocks |
| Internal SRAM | Frequently used Q29 state and computation buffers |

In the default device mode, two workers split destination rows. Each reads the
same previous state, computes its next rows, then synchronizes before the state
vectors are swapped. Signed integer dot products use ESP32-S3 SIMD; float32
normalization and tanh complete the update. The cache reserves room for subsequent
allocations instead of consuming all available PSRAM. See `INFO` telemetry for
actual allocation and cache occupancy on a particular build.

This differs from a Flash lookup table in a PLE language model. PLE reads a few
embedding rows per token; this executor visits every retained edge per neural
step. Stored LLM parameter count is therefore not a direct capacity or throughput
comparison for a connectome.

## What does the simulation mean?

For each neuron, the reference update is:

```text
scale = 0.7 / max(sum(incoming_contact_counts), 1)
weighted_input = sum(contact_count * scale * previous_source_state)
next_state = 0.5 * previous_state + 0.5 * tanh(weighted_input + external_input)
```

These are engineered rate dynamics. Contact counts are positive; the simulation
does not reconstruct excitatory/inhibitory transmitter signs, conduction delays,
biological plasticity or measured firing dynamics. It is a computation over a
real connectome subgraph, not a complete biological fly simulation.

Graph compression is exact. Q29 state arithmetic is a separate numerical
approximation checked against float32 reference cases with explicit tolerances.
[MODEL_FORMATS.md](../../firmware/esp32_fly/MODEL_FORMATS.md) specifies the
byte layout, ordering and update conventions.

## What the wiring does by itself

Nothing has to be trained to see what this graph computes. Stimulate a group of
neurons with [the probe](../../research/fly/README.md) and watch which descending
neurons answer. Driving the looming-detector neurons LC4 and LPLC2 on the left
activates the escape descending neurons on that same side, far beyond what
random visual projection neurons of the same group size produce:

| Left looming stimulus | Response | Random visual groups | Controls at or above |
|---|---:|---:|---:|
| DNp04 | 0.390 | 0.004 | 0 of 20 |
| DNp01, the giant fiber | 0.197 | 0.003 | 0 of 20 |
| DNp02 | 0.176 | 0.002 | 0 of 20 |
| DNp11 | 0.143 | 0.005 | 0 of 20 |
| DNp01 on the opposite side | 0.008 | 0.003 | 0 of 20 |

Ranking all 1,316 descending neurons by how specifically they respond puts
DNp01, DNp04, DNp02, DNg40, DNp71 and DNp11 at the top: known looming-escape
neurons of the fly, found by the wiring alone. Stimulating the right eye puts
the same escape neurons on top, in a slightly different order.
The response appears two steps after the stimulus, because LC4 and
LPLC2 contact these neurons directly, with thousands of synapses.

Smell and taste behave differently. Stimulating olfactory or gustatory sensory
neurons produces no left/right signal in the steering neurons DNa01, DNa02,
DNa03 or MDN, neither in this port nor in the whole nervous system it was
selected from, and neither with nor without transmitter signs. Walking toward
food would need a trained readout; escaping does not.

The looming measurements above ship with the released circuit, in its `evidence`
section. The smell and taste runs do not: they were made while choosing what to
port, on graphs this repository does not contain, and
[the probe](../../research/fly/README.md) is what reproduces that kind of
measurement here.

## The escape demonstration

The [firmware](../../firmware/esp32_fly/README.md) turns that into a demo. A
spider's growing size on screen drives the looming inputs of the eye it
approaches, the graph takes one step about every 1.7 seconds, and when the mean
activity of the escape neurons on one side passes a threshold, the fly jumps
away from that side. No computer takes part while it runs, and nothing is
trained: the circuit file lists the input and output neurons, and the escape
threshold is a single number.

Passing numerical gold does not establish biological realism, and how often the
fly escapes depends on the demo's own random walk. Instructions:
[install and run](../../firmware/esp32_fly/README.md),
[find your own circuit](../../research/fly/README.md),
[check numerical agreement](../../firmware/esp32_fly/VERIFICATION.md).

## Reuse the graph executor

| Source | Role |
|---|---|
| `runtime/connectome_codec.h` | Compressed graph framing and bounded decoding |
| `runtime/lz4_block.h`, `lz4_block_fast.h` | LZ4 decoding |
| `runtime/connectome_stream.h` | Streaming graph execution and reference path |
| `runtime/connectome_fast.h` | Cached execution, Q29 and replaceable dot-product hook |
| `firmware/esp32_fly/connectome_simd.S` | ESP32-S3 signed integer SIMD dot product |
| `runtime/host_verify/connectome_bridge.c` | Host bridge that runs graph steps from Python |

Start with the portable graph tests in `runtime/host_verify/` and the allocation,
Flash mapping and worker setup in the firmware `.ino`. For a new task, choose
input and output neurons with the probe, export a circuit, and write your own
stimulus and action rules, preserving the graph's execution ordering.
`escape_app.h`, `escape_world.h` and `escape_ui.h` are the demonstration-specific
pieces. The generic graph headers do not include them.

The supplied manifest, node maps and gold are bound to this selected graph.
A different graph size, node ordering or controller interface requires updated
configuration, compatible maps and new reference checks. This repository
contains the decoder, the executor and the tools that find and export a circuit;
it does not currently include a pipeline to regenerate the graph from the
upstream raw Feather tables.

## Data and code

Code uses MIT. Connectome-derived data uses CC BY 4.0, with upstream credit and
modifications described in [ATTRIBUTION.md](ATTRIBUTION.md). Small source and
selection records live here in Git. Compressed weights, full per-neuron maps,
gold arrays and validation reports belong in the model package.
