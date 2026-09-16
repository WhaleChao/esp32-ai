# Probing the fly connectome

Find out what the ported graph does with an input, without training anything.
`probe.py` stimulates a group of neurons, runs the graph with the board's
arithmetic and ranks how every descending neuron responds. Each stimulus is
compared with random groups of the same size from a control pool, so a response
only counts when similar neurons do not produce it.

This is how the escape circuit was found: stimulating the looming-detector
neurons LC4 and LPLC2 on one side activates the giant fiber (DNp01) and other
escape descending neurons on the same side, far above random visual neurons.

## Setup

Use Python 3.11+, NumPy and a C compiler available as `cc`. The probe compiles
the same C runtime bridge as the host gold.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r research/fly/requirements.txt
```

The examples assume the verified model bundle at `artifacts/fly/` and a
`neurons.csv` there with MaleCNS cell `type`, `class` and `side` for each
execution index; see [the annotated neuron table](#annotated-neuron-table).

## Run a probe

```sh
.venv/bin/python -m research.fly.probe --bundle artifacts/fly \
  --stimulus 'loom_L: type=LC4,LPLC2 side=L' \
  --stimulus 'loom_R: type=LC4,LPLC2 side=R' \
  --control 'superclass=visual_projection* type!=LC4,LPLC2,LPLC1,LPLC4,LC16,LC6' \
  --watch 'type=DNp01,DNp02,DNp04,DNp11' \
  --out artifacts/fly/probe/loom.json --report artifacts/fly/probe/loom.md
```

Selectors are space-separated `key=value` terms. Comma-separated values are
alternatives and may use shell wildcards; every term must match; `key!=value`
excludes. Keys are `type`, `side`, `superclass`, `class` and `body_id`. A few
MaleCNS types contain a comma, such as `SLP283,SLP284`; write `?` in place of
that comma.

| Option | Default | Meaning |
|---|---|---|
| `--stimulus 'name: selector'` | required | Group to stimulate; repeat it. Names ending `_L`/`_R` are also compared as a pair |
| `--control selector` | required | Pool the random control groups are drawn from, excluding the stimulus |
| `--watch selector` | none | Neurons reported one by one, averaged per type and side |
| `--steps` | 24 | Graph steps from a zero state |
| `--amplitude` | 0.8 | Constant input on every stimulated neuron |
| `--controls` | 20 | Random groups per stimulus |
| `--mode` | 2 | Host bridge execution mode; 2 is the Q29 path the board runs |
| `--top` | 15 | Descending neurons listed in the ranking |

For each watched neuron type and side, the report gives the final activity, the
control mean, a z score against the controls, how many controls reached it and
the first step with activity above 0.001. It ranks the most specifically
activated descending neurons, skipping any that stay below 0.001. For `_L`/`_R`
pairs it reports how much the left-minus-right activity of each watched type
changes between the two stimuli, against the same change across paired random
control groups.

Choose the control pool from the same kind of neurons as the stimulus, such as
other visual projection neurons for a visual input. A large z against unrelated
neurons says little.

## Export a circuit

`circuit.py` turns selected inputs and outputs into the files the firmware uses:
the circuit map, its escape gold and a candidate manifest.

```sh
.venv/bin/python -m research.fly.circuit --bundle artifacts/fly --out-dir artifacts/fly/escape
```

Defaults reproduce the released circuit: LC4 and LPLC2 inputs, DNp01, DNp02,
DNp04 and DNp11 outputs, both split by side, input amplitude 0.8 and escape
threshold 0.05. `--inputs`, `--outputs`, `--amplitude` and `--threshold` change
them; give a changed circuit its own `--bundle-id`. The circuit file includes
the probe evidence and the step at which each reference case crosses the
threshold. The gold is computed with the float32 reference and checked against
every other host execution mode, including the Q29 path the board runs.

The output directory receives `escape-circuit.json`, `escape-gold.bin`, the
pinned `neurons.csv` and a candidate `model_bundle.json` that keeps the graph and
graph gold entries. The manifest and all files are validated with the firmware's
bundle checks before anything is written. Nothing is copied automatically:
review them, put the three data files in the bundle and select the manifest with
`--manifest` or replace `firmware/esp32_fly/model_bundle.json`. Formats are in
[MODEL_FORMATS.md](../../firmware/esp32_fly/MODEL_FORMATS.md).

## What the probe does not show

The graph uses positive contact counts in a simple rate model. A strong, specific
response means the wiring carries the signal in this model, not that a real fly
behaves this way. A missing response can also come from the model: smell and
taste inputs do not produce a left/right steering signal here, even though
their pathways are in the graph.

## Annotated neuron table

`neurons.csv` maps every execution index to a MaleCNS body ID, superclass, class,
cell type and side. The probe checks that its `canonical_index` column follows
the node order stored in the graph. Build it from the model package's neuron
order and the pinned MaleCNS annotation table:

```sh
.venv/bin/python -m pip install pyarrow
.venv/bin/python -m research.fly.neurons_csv --neurons neurons.csv \
  --annotations body-annotations-male-cns-v1.0-minconf-0.5.feather \
  --out artifacts/fly/neurons.csv
```

The annotation file keeps its original name and is checked against the SHA-256
in [source-data.json](../../docs/fly-connectome/source-data.json).

## Tests

```sh
.venv/bin/python -m unittest research.fly.test_probe -v
FLY_BUNDLE=artifacts/fly .venv/bin/python -m unittest research.fly.test_probe -v
```

The first command uses a small synthetic graph. With `FLY_BUNDLE` set it also
checks that looming input drives the same-side giant fiber in the real graph.
