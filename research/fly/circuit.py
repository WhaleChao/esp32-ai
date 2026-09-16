"""Export the escape circuit, its gold reference and a candidate model manifest.

The circuit lists the looming-detector inputs and the escape descending-neuron
outputs on each side, selected by MaleCNS cell type from neurons.csv. Its
evidence section is a stimulation probe of those inputs against random visual
neurons. The gold records how every output neuron responds, step by step, to
spider stimuli on the left, the right, both sides and an approach, computed with
the scalar reference path. Firmware and the device verifier must reproduce it
within tolerance. Nothing is trained.

  python3 -m research.fly.circuit --bundle artifacts/fly --out-dir artifacts/fly/escape

The output directory receives escape-circuit.json, escape-gold.bin, the pinned
neurons.csv and a candidate model_bundle.json that keeps the graph and graph
gold entries. Everything is validated with the firmware's bundle checks before
anything is written. The trusted manifest is never edited: review the candidate,
copy the files into the bundle and the manifest to firmware/esp32_fly explicitly.
Give a changed circuit its own --bundle-id.
"""
import argparse
import json
from pathlib import Path
import struct
import tempfile

import numpy as np

from firmware.esp32_fly.tools import prepare_assets as assets
from firmware.esp32_fly.tools.verify import HostModel
from . import probe

CONTRACT = assets.CONTRACT
SIDES = assets.SIDES
INPUTS = 'type=LC4,LPLC2'
OUTPUTS = 'type=DNp01,DNp02,DNp04,DNp11'
CONTROL = 'superclass=visual_projection* type!=LC4,LPLC2,LPLC1,LPLC4,LC16,LC6'


def side_groups(cells, selector):
    groups = {side: probe.select(cells, f'{selector} side={side}') for side in SIDES}
    for side, group in groups.items():
        if len(group) == 0:
            raise ValueError(f'{selector!r} selects no neurons on side {side}')
    return groups


def entries(cells, group):
    return [dict(index=int(i), body_id=int(cells[i]['body_id']), type=cells[i]['type']) for i in group]


def gold_cases(steps):
    """Loom level per step for the left and right eye, in [0, 1]."""
    ramp = np.linspace(0, 1, steps, dtype=np.float32)
    zero, one = np.zeros(steps, np.float32), np.ones(steps, np.float32)
    return {
        'left': np.stack([one, zero], axis=1),
        'right': np.stack([zero, one], axis=1),
        'both_half': np.stack([one * 0.5, one * 0.5], axis=1),
        'left_approach': np.stack([ramp, zero], axis=1),
        'none': np.stack([zero, zero], axis=1),
    }


def stimulus(n, inputs, amplitude, left, right):
    """Graph input for one step: amplitude times each eye's loom level, in float32."""
    vector = np.zeros(n, '<f4')
    vector[inputs['L']] = np.float32(amplitude) * np.float32(left)
    vector[inputs['R']] = np.float32(amplitude) * np.float32(right)
    return vector


def run_cases(model, n, inputs, outputs, amplitude, cases, mode):
    """Output-neuron states after each step, outputs ordered left then right."""
    order = np.concatenate([outputs['L'], outputs['R']])
    first = next(iter(cases.values()))
    expected = np.empty((len(cases), first.shape[0], len(order)), np.float32)
    for c, loom in enumerate(cases.values()):
        model.reset()
        for t, (left, right) in enumerate(loom):
            state = model.step(stimulus(n, inputs, amplitude, left, right).tobytes(), mode)
            expected[c, t] = np.frombuffer(state, '<f4')[order]
    return expected


def encode_gold(nodes, cases, expected, atol, rtol):
    looms = np.stack(list(cases.values()))
    count, steps, outputs = expected.shape
    body = b''.join(looms[c].astype('<f4').tobytes() + expected[c].astype('<f4').tobytes() for c in range(count))
    return struct.pack(assets.ESCAPE_HEADER, b'FEG1', 1, nodes, count, steps, outputs, atol, rtol) + body


def candidate_manifest(trusted, bundle_id, circuit_bytes, gold, neurons_bytes, inputs, outputs, steps, atol, rtol):
    """A complete manifest for the new bundle; graph and graph gold entries are copied unchanged."""
    graph_sha = trusted['files']['graph']['sha256']
    entry = lambda name, blob: dict(name=name, bytes=len(blob), sha256=assets.sha256(blob))
    return dict(
        schema_version=assets.SCHEMA, bundle_id=bundle_id, contract=CONTRACT,
        files=dict(graph=trusted['files']['graph'], graph_gold=trusted['files']['graph_gold'],
                   circuit=entry('escape-circuit.json', circuit_bytes), escape_gold=entry('escape-gold.bin', gold),
                   neurons=entry('neurons.csv', neurons_bytes)),
        graph=trusted['graph'],
        circuit=dict(graph_sha256=graph_sha, order_sha256=trusted['graph']['order_sha256'],
                     inputs={side: int(len(inputs[side])) for side in SIDES},
                     outputs={side: int(len(outputs[side])) for side in SIDES}),
        gold=dict(graph=trusted['gold']['graph'],
                  escape=dict(circuit_sha256=assets.sha256(circuit_bytes), graph_sha256=graph_sha,
                              cases=len(gold_cases(steps)), steps=steps,
                              outputs=int(len(outputs['L']) + len(outputs['R'])), atol=atol, rtol=rtol)))


def validate_export(manifest, circuit_bytes, gold, neurons_bytes, graph, graph_gold):
    """The same checks firmware preparation applies, on the in-memory files."""
    assets.check_manifest(manifest)
    parsed = assets.validate_circuit(circuit_bytes, manifest)
    assets.validate_neurons(neurons_bytes, manifest, parsed, graph)
    assets.validate_graph_gold(graph_gold, manifest)
    assets.validate_escape_gold(gold, manifest)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--bundle', type=Path, default=Path('artifacts/fly'))
    parser.add_argument('--manifest', type=Path, default=assets.DEFAULT_MANIFEST,
                        help='Manifest whose graph and graph gold entries identify the files to keep')
    parser.add_argument('--neurons', type=Path, help='Typed neurons.csv (default: BUNDLE/neurons.csv)')
    parser.add_argument('--bundle-id', default='fly-escape-v1', help='Use a new ID for any changed circuit')
    parser.add_argument('--inputs', default=INPUTS, help='Selector for looming inputs; split by side')
    parser.add_argument('--outputs', default=OUTPUTS, help='Selector for escape outputs; split by side')
    parser.add_argument('--control', default=CONTROL, help='Control pool for the evidence probe')
    parser.add_argument('--amplitude', type=float, default=0.8)
    parser.add_argument('--threshold', type=float, default=0.05,
                        help='Mean output activity on one side that triggers an escape')
    parser.add_argument('--steps', type=int, default=24)
    parser.add_argument('--controls', type=int, default=20)
    parser.add_argument('--seed', type=int, default=7)
    parser.add_argument('--atol', type=float, default=3e-5)
    parser.add_argument('--rtol', type=float, default=3e-4)
    parser.add_argument('--out-dir', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.out_dir.exists() and any(args.out_dir.iterdir()):
            raise ValueError(f'{args.out_dir} is not empty; choose a new directory')
        if not 0 < args.amplitude <= 1 or not 0 < args.threshold < 1 or args.steps < 2:
            raise ValueError('Need 0 < amplitude <= 1, 0 < threshold < 1 and at least 2 steps')
        # Only the graph, graph gold and node-order records are reused, so a manifest
        # from before the escape contract works as a starting point.
        trusted = json.loads(args.manifest.read_text())
        graph = assets.verified_bytes(args.bundle/trusted['files']['graph']['name'], trusted['files']['graph'])
        graph_gold = assets.verified_bytes(args.bundle/trusted['files']['graph_gold']['name'],
                                           trusted['files']['graph_gold'])
        config, _ = assets.graph_config(graph, trusted['graph'])
        neurons_path = args.neurons or args.bundle/'neurons.csv'
        neurons_bytes = neurons_path.read_bytes()
        cells = probe.load_neurons(neurons_path)
        nodes = config['CONNECTOME_N']
        if len(cells) != nodes:
            raise ValueError(f'neurons.csv lists {len(cells)} neurons; the graph has {nodes}')
        probe.check_order(cells, graph)
        inputs, outputs = side_groups(cells, args.inputs), side_groups(cells, args.outputs)
        pool = probe.select(cells, args.control)
    except (OSError, KeyError, TypeError, ValueError) as error:
        parser.error(str(error))

    cases = gold_cases(args.steps)
    with tempfile.TemporaryDirectory(prefix='fly-circuit-') as temporary:
        model = HostModel(dict(graph=graph), Path(temporary))
        try:
            watch = np.concatenate([outputs['L'], outputs['R']])
            evidence = probe.probe(model, cells, {'loom_L': inputs['L'], 'loom_R': inputs['R']}, pool, watch,
                                   steps=args.steps, amplitude=args.amplitude, controls=args.controls,
                                   seed=args.seed, mode=2, top=10)
            expected = run_cases(model, nodes, inputs, outputs, args.amplitude, cases, mode=0)
            # verify.py host replays the gold in every execution mode; check them all here.
            differences = {mode: np.abs(run_cases(model, nodes, inputs, outputs, args.amplitude, cases, mode) - expected)
                           for mode in (1, 2, 3, 4)}
        finally:
            model.close()
    for mode, difference in differences.items():
        if np.any(difference > args.atol + args.rtol * np.abs(expected)):
            parser.exit(1, f'Host mode {mode} differs from the float32 reference by up to {difference.max():.3g}, '
                           'beyond the gold tolerance\n')

    left_outputs = len(outputs['L'])
    drive = {'L': expected[:, :, :left_outputs].mean(axis=2), 'R': expected[:, :, left_outputs:].mean(axis=2)}
    crossing = {name: {side: next((t + 1 for t in range(args.steps) if drive[side][c, t] > args.threshold), None)
                       for side in SIDES} for c, name in enumerate(cases)}
    responses = {name: {out: {k: v for k, v in response.items() if k != 'curve'}
                        for out, response in entry['watched'].items()}
                 for name, entry in evidence['stimuli'].items()}
    circuit = dict(
        schema_version=1, contract=CONTRACT,
        graph_sha256=trusted['files']['graph']['sha256'], order_sha256=trusted['graph']['order_sha256'],
        amplitude=args.amplitude, threshold=args.threshold,
        inputs=dict(selector=args.inputs, **{side: entries(cells, inputs[side]) for side in SIDES}),
        outputs=dict(selector=args.outputs, **{side: entries(cells, outputs[side]) for side in SIDES}),
        evidence=dict(control=args.control, steps=args.steps, controls=args.controls, seed=args.seed, host_mode=2,
                      responses=responses,
                      top_descending={name: entry['top_descending'] for name, entry in evidence['stimuli'].items()},
                      laterality=evidence['laterality'], gold_threshold_crossing_step=crossing))
    circuit_bytes = (json.dumps(circuit, indent=2) + '\n').encode()
    gold = encode_gold(nodes, cases, expected, args.atol, args.rtol)
    manifest = candidate_manifest(trusted, args.bundle_id, circuit_bytes, gold, neurons_bytes,
                                  inputs, outputs, args.steps, args.atol, args.rtol)
    try:
        validate_export(manifest, circuit_bytes, gold, neurons_bytes, graph, graph_gold)
    except (KeyError, TypeError, ValueError) as error:
        parser.exit(1, f'Export fails bundle validation, nothing written: {error}\n')

    args.out_dir.mkdir(parents=True, exist_ok=True)
    for name, content in (('escape-circuit.json', circuit_bytes), ('escape-gold.bin', gold),
                          ('neurons.csv', neurons_bytes),
                          ('model_bundle.json', (json.dumps(manifest, indent=2) + '\n').encode())):
        with open(args.out_dir/name, 'xb') as target:
            target.write(content)
    print(json.dumps(dict(cases=list(cases), threshold_crossing_step=crossing, laterality=evidence['laterality'],
                          max_abs_difference_by_mode={mode: float(d.max()) for mode, d in differences.items()}),
                     indent=2))
    print(f'Wrote escape-circuit.json, escape-gold.bin, neurons.csv and model_bundle.json to {args.out_dir}')


if __name__ == '__main__':
    main()
