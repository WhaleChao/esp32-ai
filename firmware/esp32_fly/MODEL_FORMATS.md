# Fly model bundle and compatibility

This document specifies the compressed connectome graph, the escape circuit
that connects it to the demo and their numerical references. `model_bundle.json`
binds their identities, dimensions and node ordering. [Port scope and data
attribution](../../docs/fly-connectome/README.md) describe the retained neurons
and upstream source. The model contract is `fly-escape-v1`. Nothing in the
bundle is trained.

## Bundle files and trust

A complete local bundle contains:

| File | Bytes | Role |
|---|---:|---|
| `model_bundle.json` | Variable | File identities, shapes and bindings |
| `connectome.fcl` | 14,174,411 | Fixed graph in execution order |
| `gold-device-order.bin` | 75,365,188 | Graph reference inputs and outputs in execution order |
| `escape-circuit.json` | Variable | Looming inputs, escape outputs, input amplitude and escape threshold |
| `escape-gold.bin` | Variable | Escape output responses to reference spider stimuli |
| `neurons.csv` | Variable | MaleCNS body ID, class, type and side for every execution index |

The checked-in manifest is trusted by default. The copy inside a downloaded
bundle must agree with it. Changing a bundle's file and its self-reported hash
cannot silently approve a replacement. `--manifest PATH` explicitly chooses a
different trusted manifest, for example one reviewed after exporting a new
circuit. There is no signature verification or online trust service in this tool.

Manifest schema 2 records each role's basename, byte count and SHA-256, the graph
shape and node-order SHA-256, the number of circuit inputs and outputs per side,
and both gold records. The circuit and both gold records are bound to the graph
SHA-256; the escape gold is also bound to the circuit SHA-256. Unknown schemas,
contracts and unsupported shapes are rejected.

`prepare_assets.py --bundle DIR` checks all five files: hashes, graph framing and
order, the circuit map, that `neurons.csv` follows the graph's node order and
agrees with every circuit neuron's body ID, type and side, and both gold layouts
with finite values. It does not decompress every edge or execute the reference
cases. Boot's `fast_prepare` validates the decoded graph; separate host/device
numerical checks establish agreement with gold. No files are downloaded or flashed.

## FCL1 graph

All integers are unsigned, little-endian. There is no outer compression frame.

| Offset | Type | Meaning |
|---:|---|---|
| 0 | 4 bytes | ASCII `FCL1` |
| 4 | uint32 | Version, 1 |
| 8 | uint32 | Node count N, 48,311 |
| 12 | uint32 | Directed edge count E, 9,462,135 |
| 16 | uint32 | Rows per block, 32 |
| 20 | uint32[N] | Execution-index to canonical-index permutation |
| 20 + 4N | Block sequence | Rows in execution order |

The permutation contains every integer in `[0, N)` once. These are local indices
within the selected subgraph, not the connectome's original neuron/body IDs.
The bundle's canonical ordering is the selected source indices sorted
ascending. `canonical_state[order[i]]` corresponds to `execution_state[i]`.
Circuit indices, `neurons.csv` rows and both gold files already use execution
indices. Do not apply the permutation a second time to them.

Each block begins with four uint32 values: row count, edge count, decoded byte
count and compressed byte count. Its payload is a raw LZ4 block, with no stored
size prefix. Blocks cover 32 rows each except the final block. The current file
has 1,510 blocks, with maxima of 105,700 decoded and 55,583 compressed bytes.
The preparation tool derives these maxima from the verified file.

After decompression a block contains three unsigned base-128 varint streams:

1. Incoming degree for each destination row.
2. Sorted source-node indices, delta encoded from zero separately for each row.
3. Positive integer contact counts, in the same edge order.

The first index in a row may be zero; subsequent deltas must be positive.
Indices must remain below N, row degrees sum to the block edge count, and the
three streams must consume the exact decoded length. The optimized executor
requires degree <= 65,535, contact count <= 32,767 and at most three bytes per
index delta or contact count. Each row's total contact count must be <= 2^24.

An edge is one aggregated directed source/destination connection. Its stored
contact count is an integer weight; the number of edges is not the total number
of individual synaptic contacts. Counts in this format have no transmitter sign.

The scalar reference computes, in float32 and in stored source order:

```
scale[row] = 0.7 / max(sum(contact_counts[row]), 1)
sum[row] = sum_j((float(count[j]) * scale[row]) * state[source[j]])
next[row] = 0.5 * state[row] + 0.5 * tanh(sum[row] + input[row])
```

Compression preserves the integer graph exactly. The firmware's mode 6, like
host bridge modes 2 and 4, separately quantizes state to Q29 using
round-to-nearest, then uses integer dot products and float32 normalization. Q29 is a numerical execution choice, not a lossless claim about
floating-point state. Gold comparisons use tolerances. Compile with
`-ffp-contract=off` and without fast-math to preserve the tested convention.

The generic host runtime also understands `FCZ1` through a zlib callback. This
bundle generator and ESP32 sketch accept `FCL1` only.

## Escape circuit

`escape-circuit.json` is UTF-8 JSON:

| Field | Meaning |
|---|---|
| `schema_version` | 1 |
| `contract` | `fly-escape-v1` |
| `graph_sha256`, `order_sha256` | The graph and node order the indices refer to |
| `amplitude` | Graph input for a fully looming eye, in (0, 1] |
| `threshold` | Mean output activity on one side that triggers an escape, in (0, 1) |
| `inputs.L`, `inputs.R` | Looming-detector neurons per eye, as `{index, body_id, type}` |
| `outputs.L`, `outputs.R` | Escape descending neurons per side, as `{index, body_id, type}` |
| `inputs.selector`, `outputs.selector` | The probe selectors that produced them |
| `evidence` | Probe responses against random visual neurons and the gold's threshold crossings; informational |

The released circuit uses LC4 and LPLC2 neurons as inputs and DNp01 (the giant
fiber), DNp02, DNp04 and DNp11 as outputs, split by MaleCNS side. Indices are
unique across all four groups and below N; at most 1,024 inputs and 64 outputs
per side. Each entry must match the `neurons.csv` row at its index, including
the side of the group it is listed in.

## Escape contract

The graph starts from zero state. For each neural step the demo supplies a loom
level per eye in [0, 1] and sets, in float32:

```
input[i] = amplitude * loom_left    for i in inputs.L
input[i] = amplitude * loom_right   for i in inputs.R
input[i] = 0                        for every other neuron
```

After the graph update, each drive is a float32 mean, summed in listed order:

```
drive_left  = sum(next[i] for i in outputs.L) / count(outputs.L)
drive_right = sum(next[i] for i in outputs.R) / count(outputs.R)
```

An escape triggers when either drive exceeds `threshold`, directed away from the
side with the larger drive. Equal drives count as danger on the right. How the demo turns spider geometry into loom levels
and an escape into movement is firmware behavior, not part of the bundle.

## Gold formats

`FGF1` starts with a 28-byte header: magic, uint32 version (1), node count,
case count, step count, float32 absolute tolerance and relative tolerance.
Each case contains an initial float32[N] state, then `steps` pairs of input and
expected next-state float32[N] vectors. The pinned reference has six cases and
32 steps, with atol 3e-5 and rtol 3e-4. Every vector is already in execution order.

`FEG1` starts with a 32-byte header: magic, uint32 version (1), node count, case
count, step count, output count O, float32 absolute tolerance and relative
tolerance. Each case starts from zero state and contains float32[steps, 2] loom
levels (left, right) followed by float32[steps, O] expected output-neuron states
after each step, outputs in `outputs.L` then `outputs.R` order. The released gold
has five cases of 24 steps: a left spider, a right spider, both eyes at half, a
left approach ramping from 0 to 1 and no spider. It is computed with the scalar
float32 reference; the exporter checks that the Q29 path agrees within tolerance.

Gold files are for verification, not firmware inputs. Their hashes protect their
association with this bundle. Preparation checks integrity and format; a
numerical verifier must still run the model on them.

## Generated firmware inputs

The generator writes `generated/connectome_build.h`, `generated/escape_circuit.h`
and `generated/assets.json`. The directory is ignored by Git.

`CONNECTOME_N`, `CONNECTOME_E`, `CONNECTOME_BLOCK_ROWS`, `CONNECTOME_BYTES`,
`CONNECTOME_RAW` and `CONNECTOME_COMPRESSED` come from the verified graph.
`CONNECTOME_HASH` is its computed FNV-1a diagnostic checksum.

`escape_circuit.h` defines `ESCAPE_CONTRACT`, `ESCAPE_AMPLITUDE`,
`ESCAPE_THRESHOLD` as float32 literals, `ESCAPE_CIRCUIT_BYTES`,
`ESCAPE_CIRCUIT_HASH` (FNV-1a of the circuit file) and the four index arrays
`escape_inputs_left`, `escape_inputs_right`, `escape_outputs_left` and
`escape_outputs_right` with their `ESCAPE_INPUTS_L/R` and `ESCAPE_OUTPUTS_L/R`
lengths. FNV-1a detects accidental mismatch; SHA-256 against the trusted
manifest is the host identity check.

`assets.json` records verified identities, paths, generated constants and the
circuit summary. Its manifest fingerprint is SHA-256 of UTF-8 JSON with sorted
keys and compact separators, not a hash of the manifest's whitespace.

Run preparation before compilation, and re-run it after moving the checkout.
All validation precedes output changes; individual files are atomically
replaced. Do not run preparation concurrently with another preparation or build.
The graph remains an external partition image and is not copied into the app.

## Changing the circuit

Find candidate neurons with the [stimulation probe](../../research/fly/README.md),
then export a new circuit, gold and candidate manifest with
`python3 -m research.fly.circuit`. Review the candidate, copy the files into the
bundle and select the manifest explicitly with `--manifest`, or replace the
checked-in one. Merely changing a hash does not establish that a circuit behaves
as intended; run the host and device checks in [VERIFICATION.md](VERIFICATION.md).

The current firmware fixes the node count, block rows and the escape contract.
Changes to those or to the graph dynamics require code changes, a new contract
and new numerical checks.
