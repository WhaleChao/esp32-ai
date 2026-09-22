"""Stimulate neuron groups in the ported graph and rank descending-neuron responses.

The graph runs through the host C bridge with the board's arithmetic, starting
from a zero state with a constant input on the stimulated group. Each stimulus
is compared with random groups of the same size drawn from a control pool, so a
response only counts when similar groups of neurons do not produce it. Nothing
is trained.

Selectors are space-separated key=value terms. Values within a term are
alternatives (comma-separated, shell-style wildcards allowed); every term must
match. key!=value excludes. Keys: type, side, superclass, class, body_id. A few
MaleCNS types contain a comma, such as SLP283,SLP284; match them with ? in place
of the comma.

  python3 -m research.fly.probe --bundle artifacts/fly \\
    --stimulus 'loom_L: type=LC4,LPLC2 side=L' \\
    --stimulus 'loom_R: type=LC4,LPLC2 side=R' \\
    --control 'superclass=visual_projection* type!=LC4,LPLC2,LPLC1,LPLC4,LC16,LC6' \\
    --watch 'type=DNp01,DNp02,DNp04,DNp11' \\
    --out artifacts/fly/probe/loom.json
"""
import argparse
import csv
import fnmatch
import json
from pathlib import Path
import re
import tempfile

import numpy as np

from firmware.esp32_fly.tools import prepare_assets as assets
from firmware.esp32_fly.tools.verify import HostModel, load_bundle

KEYS = ('type', 'side', 'superclass', 'class', 'body_id')
COLUMNS = ('execution_index', 'canonical_index', 'body_id', 'superclass', 'class', 'type', 'side')
ACTIVE = 1e-3   # |state| counted as a response for latency and the descending ranking


def load_neurons(path):
    with open(path, newline='') as source:
        reader = csv.DictReader(source)
        missing = [c for c in COLUMNS if c not in (reader.fieldnames or ())]
        if missing:
            raise ValueError(f'{path} lacks columns {missing}; add them with research.fly.neurons_csv')
        cells = [{key: row[key] or '' for key in COLUMNS} for row in reader]
    cells.sort(key=lambda cell: int(cell['execution_index']))
    if [int(cell['execution_index']) for cell in cells] != list(range(len(cells))):
        raise ValueError(f'{path} must list every execution index exactly once')
    return cells


def check_order(cells, graph):
    """The table must follow the graph's own execution-to-canonical node order."""
    order = np.frombuffer(graph, '<u4', count=len(cells), offset=20)
    canonical = np.array([int(cell['canonical_index']) for cell in cells])
    wrong = np.nonzero(order != canonical)[0]
    if len(wrong):
        raise ValueError(f'neurons.csv does not follow the graph node order (first mismatch at execution index {wrong[0]})')


def parse_selector(text):
    terms = []
    for term in text.split():
        match = re.fullmatch(r'([a-z_]+)(!?=)([^=\s]\S*)', term)
        if not match or match[1] not in KEYS:
            raise ValueError(f'Bad selector term {term!r}; use key=value or key!=value with keys {", ".join(KEYS)}')
        values = [v for v in match[3].split(',') if v]
        if not values:
            raise ValueError(f'Selector term {term!r} has no values')
        terms.append((match[1], match[2] == '!=', values))
    if not terms:
        raise ValueError('Empty selector')
    return terms


def matches(cell, terms):
    for key, negate, values in terms:
        hit = any(fnmatch.fnmatchcase(cell[key], value) for value in values)
        if hit == negate:
            return False
    return True


def select(cells, text):
    terms = parse_selector(text)
    return np.array([i for i, cell in enumerate(cells) if matches(cell, terms)], dtype=np.int64)


def parse_stimulus(text):
    name, separator, selector = text.partition(':')
    name = name.strip()
    if not separator or not re.fullmatch(r'[A-Za-z0-9_.-]+', name):
        raise ValueError(f'Stimulus {text!r} must look like "name: selector"')
    return name, selector.strip()


def label(cell):
    return f"{cell['type'] or cell['body_id']}_{cell['side']}" if cell['side'] else (cell['type'] or cell['body_id'])


def validate(stimuli, pool, *, steps, amplitude, controls, top):
    if not 0 < amplitude <= 1 or steps < 1 or controls < 2 or top < 1:
        raise ValueError('Need 0 < amplitude <= 1, steps >= 1, at least 2 controls and top >= 1')
    for name, group in stimuli.items():
        if len(group) == 0:
            raise ValueError(f'Stimulus {name} selects no neurons')
        candidates = len(np.setdiff1d(pool, group))
        if candidates < len(group):
            raise ValueError(f'Control pool has {candidates} neurons outside {name}; it needs at least {len(group)}')


def trajectory(model, n, group, columns, steps, amplitude, mode):
    """Run from zero state with constant input; return the selected columns per step."""
    model.reset()
    stimulus = np.zeros(n, '<f4')
    stimulus[group] = amplitude
    raw = stimulus.tobytes()
    trace = np.empty((steps, len(columns)), np.float32)
    for t in range(steps):
        trace[t] = np.frombuffer(model.step(raw, mode), '<f4')[columns]
    return trace


def probe(model, cells, stimuli, pool, watch, *, steps=24, amplitude=0.8, controls=20, seed=7, mode=2, top=15):
    """Stimulus responses of watched and descending neurons against random same-size groups."""
    validate(stimuli, pool, steps=steps, amplitude=amplitude, controls=controls, top=top)
    n = len(cells)
    dn = np.array([i for i, c in enumerate(cells) if c['superclass'].startswith('descending_neuron')], dtype=np.int64)
    columns = np.union1d(dn, watch).astype(np.int64)
    at = {int(index): k for k, index in enumerate(columns)}
    labels = sorted({label(cells[i]) for i in watch})
    members = {name: [at[int(i)] for i in watch if label(cells[i]) == name] for name in labels}
    dn_columns = [at[int(i)] for i in dn]
    results = dict(steps=steps, amplitude=amplitude, controls=controls, seed=seed, mode=mode, stimuli={})
    control_finals = {}
    for number, (name, group) in enumerate(stimuli.items()):
        candidates = np.setdiff1d(pool, group)
        rng = np.random.default_rng([seed, number])
        finals = np.stack([trajectory(model, n, rng.choice(candidates, len(group), replace=False),
                                      columns, steps, amplitude, mode)[-1] for _ in range(controls)])
        control_finals[name] = finals
        trace = trajectory(model, n, group, columns, steps, amplitude, mode)
        watched = {}
        for name_out, cols in members.items():
            curve = trace[:, cols].mean(axis=1)
            base = finals[:, cols].mean(axis=1)
            sd = float(base.std())
            active = np.nonzero(np.abs(curve) > ACTIVE)[0]
            watched[name_out] = dict(
                final=float(curve[-1]), control_mean=float(base.mean()), control_sd=sd,
                z=float((curve[-1] - base.mean()) / sd) if sd > 0 else None,
                controls_at_or_above=int((base >= curve[-1]).sum()),
                latency_steps=int(active[0]) + 1 if len(active) else None,
                curve=[float(v) for v in curve])
        final, mean, sd = trace[-1, dn_columns], finals[:, dn_columns].mean(axis=0), finals[:, dn_columns].std(axis=0)
        # Rank only neurons that respond and whose controls vary; otherwise z is meaningless.
        valid = (sd > 0) & (np.abs(final) > ACTIVE)
        z = np.full(len(final), -np.inf)
        z[valid] = (final[valid] - mean[valid]) / sd[valid]
        ranked = [k for k in np.argsort(-z, kind='stable')[:top] if valid[k]]
        ranking = [dict(neuron=label(cells[dn[k]]), body_id=cells[dn[k]]['body_id'], final=float(final[k]),
                        control_mean=float(mean[k]), z=float(z[k])) for k in ranked]
        results['stimuli'][name] = dict(size=int(len(group)), watched=watched, top_descending=ranking)
    results['laterality'] = laterality(stimuli, members, results, control_finals)
    return results


def laterality(stimuli, members, results, control_finals):
    """For stimulus pairs X_L/X_R: (left-minus-right of a watched type under X_L) minus (under X_R).
    The null is the same difference of differences across paired random control groups."""
    output = {}
    types = sorted({name[:-2] for name in members if name.endswith(('_L', '_R'))
                    and f'{name[:-2]}_L' in members and f'{name[:-2]}_R' in members})
    for name in stimuli:
        if not name.endswith('_L') or f'{name[:-2]}_R' not in stimuli:
            continue
        pair = name[:-2]
        left, right = results['stimuli'][name]['watched'], results['stimuli'][f'{pair}_R']['watched']
        output[pair] = {}
        for t in types:
            lc, rc = members[f'{t}_L'], members[f'{t}_R']
            difference = lambda finals: finals[:, lc].mean(axis=1) - finals[:, rc].mean(axis=1)
            null = difference(control_finals[name]) - difference(control_finals[f'{pair}_R'])
            separation = (left[f'{t}_L']['final'] - left[f'{t}_R']['final']) - (right[f'{t}_L']['final'] - right[f'{t}_R']['final'])
            centre, spread = float(null.mean()), float(null.std())
            output[pair][t] = dict(separation=separation, null_mean=centre, null_sd=spread,
                                   z=(separation - centre) / spread if spread > 0 else None)
    return output


def markdown(results):
    dash = lambda value, fmt: '-' if value is None else format(value, fmt)
    lines = ['# Stimulation probe', '',
             f"Constant input {results['amplitude']} for {results['steps']} steps from zero state, host mode "
             f"{results['mode']}; {results['controls']} random same-size control groups (seed {results['seed']}).", '']
    for name, entry in results['stimuli'].items():
        lines += [f'## {name} ({entry["size"]} neurons)', '']
        if entry['watched']:
            lines += ['| neuron | final | control mean | z | controls >= | latency |', '|---|---:|---:|---:|---:|---:|']
            for out, o in entry['watched'].items():
                lines.append(f"| {out} | {o['final']:.4f} | {o['control_mean']:.4f} | {dash(o['z'], '.1f')} | "
                             f"{o['controls_at_or_above']}/{results['controls']} | {dash(o['latency_steps'], 'd')} |")
            lines.append('')
        ranked = ', '.join(f"{r['neuron']} (z {r['z']:.1f})" for r in entry['top_descending']) or 'none responded'
        lines += [f'Most specifically activated descending neurons: {ranked}', '']
    for pair, rows in results['laterality'].items():
        lines += [f'## Laterality: {pair}_L vs {pair}_R', '', '| neuron type | separation | z |', '|---|---:|---:|']
        lines += [f"| {t} | {r['separation']:.4f} | {dash(r['z'], '.1f')} |" for t, r in rows.items()]
        lines.append('')
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--bundle', type=Path, default=Path('artifacts/fly'))
    parser.add_argument('--manifest', type=Path, default=assets.DEFAULT_MANIFEST)
    parser.add_argument('--neurons', type=Path, help='neurons.csv with type and side (default: BUNDLE/neurons.csv)')
    parser.add_argument('--stimulus', action='append', required=True, help='"name: selector"; repeat for more')
    parser.add_argument('--control', required=True, help='Selector for the random control pool')
    parser.add_argument('--watch', help='Selector for neurons to report individually')
    parser.add_argument('--steps', type=int, default=24)
    parser.add_argument('--amplitude', type=float, default=0.8)
    parser.add_argument('--controls', type=int, default=20)
    parser.add_argument('--seed', type=int, default=7)
    parser.add_argument('--mode', type=int, default=2, choices=range(5), help='Host bridge mode; 2 is the Q29 path the board runs')
    parser.add_argument('--top', type=int, default=15)
    parser.add_argument('--out', type=Path, required=True, help='New JSON path; never overwritten')
    parser.add_argument('--report', type=Path, help='Optional new Markdown path')
    args = parser.parse_args()
    try:
        if args.report is not None and args.report.resolve() == args.out.resolve():
            raise ValueError('--out and --report must be different files')
        for path in (args.out, args.report):
            if path is not None and path.exists():
                raise ValueError(f'{path} exists; choose a new path')
        cells = load_neurons(args.neurons or args.bundle/'neurons.csv')
        _, data, config = load_bundle(args.bundle, args.manifest)
        if len(cells) != config['CONNECTOME_N']:
            raise ValueError(f"neurons.csv lists {len(cells)} neurons; the graph has {config['CONNECTOME_N']}")
        check_order(cells, data['graph'])
        stimuli = {}
        for text in args.stimulus:
            name, selector = parse_stimulus(text)
            if name in stimuli:
                raise ValueError(f'Duplicate stimulus name {name}')
            stimuli[name] = select(cells, selector)
        pool = select(cells, args.control)
        watch = select(cells, args.watch) if args.watch else np.array([], dtype=np.int64)
        validate(stimuli, pool, steps=args.steps, amplitude=args.amplitude, controls=args.controls, top=args.top)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    with tempfile.TemporaryDirectory(prefix='fly-probe-') as temporary:
        model = HostModel(dict(graph=data['graph']), Path(temporary))
        try:
            results = probe(model, cells, stimuli, pool, watch, steps=args.steps, amplitude=args.amplitude,
                            controls=args.controls, seed=args.seed, mode=args.mode, top=args.top)
        finally:
            model.close()
    results['selectors'] = dict(stimuli=dict(parse_stimulus(t) for t in args.stimulus), control=args.control, watch=args.watch)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, 'x') as output:
        output.write(json.dumps(results, indent=2) + '\n')
    text = markdown(results)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        with open(args.report, 'x') as output:
            output.write(text + '\n')
    print(text)


if __name__ == '__main__':
    main()
