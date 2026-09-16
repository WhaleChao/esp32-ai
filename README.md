# Running a 28.9M parameter LLM on a microcontroller

<p align="center">
  <a href="https://x.com/slvDev">𝕏 slvDev</a> &nbsp;·&nbsp;
  <a href="https://www.linkedin.com/in/slvdev/">LinkedIn</a>
</p>

![28.9M-parameter LLM running on an ESP32-S3](media/esp32-ple-demo.gif)

This is a 28.9 million parameter language model that generates text on an ESP32-S3
microcontroller. It runs on the chip itself, with nothing sent to a server, and it
displays generated text at 9.88 tokens per second on a small screen wired to the
chip. It fits because most of the model lives in flash instead of RAM, using
Per-Layer Embeddings, an idea from Google's Gemma 3n.

The same chip also runs a port of part of the fruit fly connectome: 48,311 neurons
and 9.46 million connections from a real fly brain, escaping a spider on screen.
See [Porting part of the fruit fly connectome to ESP32](#porting-part-of-the-fruit-fly-connectome-to-esp32).

## The numbers

|              |                                                    |
| ------------ | -------------------------------------------------- |
| Parameters   | 28.9M stored (25M of them in a flash lookup table) |
| Chip         | ESP32-S3, 512KB SRAM, 8MB PSRAM and 16MB flash     |
| Speed        | 9.88 tok/s end to end, 94.9 ms/token of compute    |
| Connectivity | none, everything runs on the device                |
| Model size   | 14.9MB at 4-bit                                    |

## Why it is hard, and how it fits anyway

A microcontroller has very little fast memory. The ESP32-S3 gives you 512KB of
SRAM, and only the values touched many times per token can live there:
activations and norm weights. The dense core and output head, scanned once per
position, sit in PSRAM. What is left is the embedding tables, and their size is
what normally decides how big a model can be.

In this model, most parameters sit in an embedding table, which the model reads
from rather than computes on. So that 25-million-parameter table stays in slow
flash, and only the few rows each token needs are pulled from it, about 450
bytes. Most of the model is therefore never loaded to run it: it sits in flash
and is sampled a little at a time.

That idea is Google's Per-Layer Embeddings, from
[Gemma 3n](https://ai.google.dev/gemma/docs/gemma-3n). Here it runs
on the memory layout of a microcontroller instead of a phone or a GPU.

Each tier holds whatever is read at its own frequency:

```
  SRAM  (fast, tiny)   activations and norm weights, touched many times a token
  PSRAM (medium)       the core and output head, read once per position
  FLASH (huge, slow)   the 25M-param table, about 6 rows read per token (~450 B)
```

## What it does, and what it does not

The model was trained on TinyStories, so it writes short, simple stories and mostly
keeps them coherent. It will not answer questions, follow instructions, write code,
or know facts. That limit comes from the small part of the model that does the
reasoning, and the memory trick does not change it. What is interesting here is the
architecture, fitting a large model onto a tiny chip, rather than what a 28.9 million
parameter model can say.

# Porting part of the fruit fly connectome to ESP32

This is a port of part of a real brain to the same board. It holds 48,311 neurons and
9,462,135 connections from the male fruit fly connectome, MaleCNS v1.0, with the
synaptic contact count of every connection kept exactly. The graph is 13.5MiB
compressed in flash, and every simulation step goes through all of its
connections, on both cores with the chip's SIMD instructions.

It is only part of the brain because the whole nervous system does not fit: the
smallest lossless encoding we found is 24.49MiB, against about 14MiB of flash left
for it. [Why only part of it](docs/fly-connectome/README.md#why-only-part-of-the-nervous-system).

## The numbers

|             |                                                            |
| ----------- | ---------------------------------------------------------- |
| Neurons     | 48,311: central brain, visual projection and descending   |
| Connections | 9,462,135, carrying 49,481,754 synaptic contacts          |
| Graph size  | 13.5MiB compressed in flash, blocks cached in PSRAM       |
| Decisions   | a new decision about every 1.7 s                          |
| Trained     | nothing: the demo reads the fly's own escape neurons      |

## Escaping a spider, with nothing trained

To show that it computes something, it escapes a spider on the OLED. The
spider's growing size drives the fly's own looming detectors, LC4 and LPLC2, on
the side it comes from. When the escape neurons on that side get active enough,
including DNp01, the giant fiber that makes a real fly jump, the fly leaps away
from that side.

No model was trained for this. The port ships a small text file naming those
input and output neurons and one threshold; the behavior comes from the fly's
own wiring. I checked it first: stimulating the looming detectors activates
those escape neurons 30 to 100 times more than random visual neurons do, and
ranking all 1,316 descending neurons by specificity puts the known escape
neurons on top.

It is a selected part of the nervous system, not the whole fly: what is ported,
what is left out and how it runs is in
[`docs/fly-connectome/`](docs/fly-connectome/README.md).

# Run it yourself

## Models

- [Barista](https://huggingface.co/slvDev/esp32-ai-barista) - espresso question answering
- [TinyStories](https://huggingface.co/slvDev/esp32-ai-tinystories) - story generation
- [Fly](https://huggingface.co/slvDev/esp32-ai-fly) - fruit fly connectome escaping a spider

## Commands

Download and deployment are separate operations: one reaches the network, the
other touches the board.

```bash
scripts/fetch_model.sh barista   # download, verify, install into artifacts/
scripts/deploy.sh barista        # generate headers, run gates, compile, flash
```

`tinystories` and `fly` take the same two commands. Each requires the model to
be named, because the board holds one at a time and deploying replaces it.

`fetch_model.sh` checks the inference assets against a SHA-256 and byte size
pinned in the script, and cross-checks the release's own `metadata.json` against
those same pins. It installs nothing unless every check passes, so a failed
download leaves what you already have untouched. `deploy.sh` downloads no model:
it works from whatever is already in `artifacts/<model>/`. It does run two of its
header tools through `uv`, which fetches one pinned wheel the first time on a
machine that has never cached it.

The firmware details and the boot output to expect live in
[`firmware/esp32_barista/README.md`](firmware/esp32_barista/README.md),
[`firmware/esp32_tinystories/README.md`](firmware/esp32_tinystories/README.md) and
[`firmware/esp32_fly/README.md`](firmware/esp32_fly/README.md). The reusable
architecture is in `src/`; the training, ablation and quantization code that
reproduces the published numbers is in `research/tinystories/`, and the tools
that find a circuit in the fly graph and export it are in `research/fly/`. The
language model's full method, its ablations and its on-chip measurements are
written up in [`RESULTS.md`](RESULTS.md); the fly's measurements are in
[`firmware/esp32_fly/README.md`](firmware/esp32_fly/README.md#measured).

## Credit

TinyStories is the dataset this trains on: short synthetic stories simple enough
that a small model can still learn to write coherently (Ronen Eldan and Yuanzhi Li,
Microsoft Research, [arXiv:2305.07759](https://arxiv.org/abs/2305.07759)). The other
half is Per-Layer Embeddings, Google's design from Gemma 3n, which is what
lets a big model fit on a small chip.

Andrej Karpathy's [llama2.c](https://github.com/karpathy/llama2.c) is the
reference for training a small language model and running it in plain C.

The fly graph comes from MaleCNS v1.0, the male Drosophila central nervous system
connectome by the FlyEM Project at HHMI Janelia, the University of Cambridge, the
MRC Laboratory of Molecular Biology and Google Research, used under CC BY 4.0.
What was selected and changed is in
[`docs/fly-connectome/ATTRIBUTION.md`](docs/fly-connectome/ATTRIBUTION.md).

## Measurements

Detailed measurements and ablations are documented in `RESULTS.md`.
