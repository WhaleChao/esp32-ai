"""Probe logic on a small synthetic graph; optional real-bundle check with FLY_BUNDLE."""
import csv
import os
from pathlib import Path
import struct
import tempfile
import unittest

import numpy as np

from . import probe


class FakeModel:
    """Same update rule as the port on a small dense graph."""

    def __init__(self, weights):
        self.w = np.asarray(weights, np.float32)
        self.state = np.zeros(len(self.w), np.float32)

    def reset(self):
        self.state[:] = 0

    def step(self, raw, mode):
        stimulus = np.frombuffer(raw, '<f4')
        self.state = (0.5 * self.state + 0.5 * np.tanh(self.w @ self.state + stimulus)).astype(np.float32)
        return self.state.tobytes()


def cell(index, superclass, type_, side, cls=''):
    return {'execution_index': str(index), 'canonical_index': str(index), 'body_id': str(1000 + index),
            'superclass': superclass, 'class': cls, 'type': type_, 'side': side}


def escape_like():
    """Two looming inputs per side wired to an escape neuron on the same side, a
    silent descending neuron, and a pool of visual neurons with weak, unsided input
    to both escape neurons."""
    cells = [cell(0, 'visual_projection', 'LC4', 'L'), cell(1, 'visual_projection', 'LC4', 'L'),
             cell(2, 'visual_projection', 'LC4', 'R'), cell(3, 'visual_projection', 'LC4', 'R'),
             cell(4, 'descending_neuron', 'DNp01', 'L'), cell(5, 'descending_neuron', 'DNp01', 'R'),
             cell(6, 'descending_neuron', 'DNsilent', 'L')]
    cells += [cell(7 + k, 'visual_projection', f'LC{10 + k}', 'LR'[k % 2]) for k in range(12)]
    n = len(cells)
    w = np.zeros((n, n))
    w[4, [0, 1]] = 0.6
    w[5, [2, 3]] = 0.6
    rng = np.random.default_rng(3)
    w[4, 7:] = rng.uniform(0, 0.02, 12)
    w[5, 7:] = rng.uniform(0, 0.02, 12)
    return cells, w


class SelectorTests(unittest.TestCase):
    def setUp(self):
        self.cells, _ = escape_like()

    def test_values_are_alternatives_and_terms_must_all_match(self):
        self.assertEqual(probe.select(self.cells, 'type=LC4,DNp01 side=L').tolist(), [0, 1, 4])

    def test_wildcards_and_exclusion(self):
        picked = probe.select(self.cells, 'superclass=visual_* type!=LC4')
        self.assertEqual(picked.tolist(), list(range(7, 19)))

    def test_question_mark_matches_a_comma_inside_a_type(self):
        cells = [cell(0, 'cb_intrinsic', 'SLP283,SLP284', 'L'), cell(1, 'cb_intrinsic', 'SLP283', 'L')]
        self.assertEqual(probe.select(cells, 'type=SLP283?SLP284').tolist(), [0])

    def test_bad_selectors_are_rejected(self):
        for text in ('', 'colour=red', 'type', 'type=', 'type=,', 'type==LC4'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                probe.parse_selector(text)

    def test_stimulus_needs_a_name(self):
        self.assertEqual(probe.parse_stimulus('loom_L: type=LC4'), ('loom_L', 'type=LC4'))
        for text in ('type=LC4', 'bad name: type=LC4', ': type=LC4'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                probe.parse_stimulus(text)


class ProbeTests(unittest.TestCase):
    def run_probe(self, model=None, **kwargs):
        cells, w = escape_like()
        stimuli = {'loom_L': probe.select(cells, 'type=LC4 side=L'), 'loom_R': probe.select(cells, 'type=LC4 side=R')}
        pool = probe.select(cells, 'superclass=visual_projection type!=LC4')
        watch = probe.select(cells, 'type=DNp01')
        options = dict(steps=12, controls=6, top=3)
        options.update(kwargs)
        return probe.probe(model or FakeModel(w), cells, stimuli, pool, watch, **options)

    def test_wired_side_beats_random_groups(self):
        result = self.run_probe()
        left = result['stimuli']['loom_L']['watched']
        self.assertGreater(left['DNp01_L']['final'], 0.2)
        self.assertEqual(left['DNp01_L']['controls_at_or_above'], 0)
        self.assertGreater(left['DNp01_L']['z'], 10)
        self.assertLess(left['DNp01_R']['final'], 0.01)
        self.assertEqual(left['DNp01_L']['latency_steps'], 2)
        self.assertEqual(result['stimuli']['loom_L']['top_descending'][0]['neuron'], 'DNp01_L')

    def test_ranking_skips_neurons_that_never_respond(self):
        ranked = [r['neuron'] for s in self.run_probe()['stimuli'].values() for r in s['top_descending']]
        self.assertNotIn('DNsilent_L', ranked)

    def test_laterality_uses_paired_control_differences(self):
        lateral = self.run_probe()['laterality']['loom']['DNp01']
        self.assertGreater(lateral['separation'], 0.4)
        self.assertGreater(lateral['null_sd'], 0)
        self.assertAlmostEqual(lateral['z'], (lateral['separation'] - lateral['null_mean']) / lateral['null_sd'])

    def test_laterality_is_zero_when_controls_separate_as_much(self):
        # Controls that separate the sides exactly as much as the stimulus give no evidence.
        pairs = {'x_L': np.array([[0.3, 0.1]] * 4), 'x_R': np.array([[0.1, 0.3], [0.2, 0.2], [0.1, 0.3], [0.2, 0.2]])}
        members = {'t_L': [0], 't_R': [1]}
        results = {'stimuli': {'x_L': {'watched': {'t_L': {'final': 0.35}, 't_R': {'final': 0.1}}},
                               'x_R': {'watched': {'t_L': {'final': 0.15}, 't_R': {'final': 0.2}}}}}
        row = probe.laterality(pairs, members, results, pairs)['x']['t']
        self.assertAlmostEqual(row['null_mean'], row['separation'])
        self.assertAlmostEqual(row['z'], 0)

    def test_results_are_deterministic_and_follow_the_seed(self):
        self.assertEqual(self.run_probe(), self.run_probe())
        mean = lambda r: r['stimuli']['loom_L']['watched']['DNp01_L']['control_mean']
        self.assertNotEqual(mean(self.run_probe(seed=7)), mean(self.run_probe(seed=8)))

    def test_controls_are_same_size_and_exclude_the_stimulus(self):
        cells, w = escape_like()
        seen = []

        class Recording(FakeModel):
            def step(self, raw, mode):
                seen.append(tuple(np.nonzero(np.frombuffer(raw, '<f4'))[0]))
                return super().step(raw, mode)
        group = probe.select(cells, 'type=LC4 side=L')
        probe.probe(Recording(w), cells, {'loom_L': group}, probe.select(cells, 'superclass=visual_projection'),
                    np.array([], dtype=np.int64), steps=1, controls=5)
        self.assertEqual(len(seen), 6)
        for stimulated in seen[:-1]:
            self.assertEqual(len(stimulated), len(group))
            self.assertFalse(set(stimulated) & set(group.tolist()))
        self.assertEqual(seen[-1], tuple(group.tolist()))

    def test_impossible_requests_are_rejected(self):
        cells, w = escape_like()
        model = FakeModel(w)
        watch = np.array([], dtype=np.int64)
        pool = np.arange(7, 19)
        with self.assertRaisesRegex(ValueError, 'selects no neurons'):
            probe.probe(model, cells, {'empty': np.array([], dtype=np.int64)}, pool, watch)
        with self.assertRaisesRegex(ValueError, 'Control pool'):
            probe.probe(model, cells, {'big': np.arange(0, 4)}, np.array([7, 8]), watch)
        for options in (dict(amplitude=1.5), dict(top=0), dict(controls=1), dict(steps=0)):
            with self.subTest(options=options), self.assertRaises(ValueError):
                probe.probe(model, cells, {'x': np.array([0])}, pool, watch, **options)

    def test_markdown_shows_missing_values_as_dashes(self):
        text = probe.markdown(self.run_probe())
        self.assertNotIn('None', text)
        self.assertIn('| DNp01_L |', text)


class NeuronsTableTests(unittest.TestCase):
    def write(self, rows, fields=probe.COLUMNS):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name)/'neurons.csv'
        with open(path, 'w', newline='') as target:
            writer = csv.DictWriter(target, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        return path

    def test_rows_are_ordered_by_execution_index(self):
        cells, _ = escape_like()
        loaded = probe.load_neurons(self.write(list(reversed(cells))))
        self.assertEqual([c['execution_index'] for c in loaded], [str(i) for i in range(len(cells))])

    def test_missing_columns_and_gaps_are_rejected(self):
        cells, _ = escape_like()
        with self.assertRaisesRegex(ValueError, 'lacks columns'):
            probe.load_neurons(self.write([{k: v for k, v in c.items() if k != 'type'} for c in cells],
                                          [c for c in probe.COLUMNS if c != 'type']))
        with self.assertRaisesRegex(ValueError, 'every execution index'):
            probe.load_neurons(self.write(cells[1:]))

    def test_table_must_follow_the_graph_node_order(self):
        cells, _ = escape_like()
        n = len(cells)
        graph = b'FCL1' + bytes(16) + struct.pack(f'<{n}I', *range(n))
        probe.check_order(cells, graph)
        swapped = [dict(c) for c in cells]
        swapped[0]['canonical_index'], swapped[1]['canonical_index'] = '1', '0'
        with self.assertRaisesRegex(ValueError, 'node order'):
            probe.check_order(swapped, graph)


@unittest.skipUnless(os.environ.get('FLY_BUNDLE'), 'Set FLY_BUNDLE to a verified bundle with a typed neurons.csv')
class RealGraphTests(unittest.TestCase):
    def test_looming_drives_the_same_side_giant_fiber(self):
        from firmware.esp32_fly.tools import prepare_assets as assets
        from firmware.esp32_fly.tools.verify import HostModel, load_bundle
        bundle = Path(os.environ['FLY_BUNDLE'])
        cells = probe.load_neurons(bundle/'neurons.csv')
        _, data, _ = load_bundle(bundle, Path(os.environ.get('FLY_MANIFEST', str(assets.DEFAULT_MANIFEST))))
        probe.check_order(cells, data['graph'])
        with tempfile.TemporaryDirectory() as temporary:
            model = HostModel(dict(graph=data['graph']), Path(temporary))
            try:
                result = probe.probe(model, cells, {'loom_L': probe.select(cells, 'type=LC4,LPLC2 side=L')},
                                     probe.select(cells, 'superclass=visual_projection* type!=LC4,LPLC2,LPLC1,LPLC4,LC16,LC6'),
                                     probe.select(cells, 'type=DNp01'), controls=5)
            finally:
                model.close()
        giant_fiber = result['stimuli']['loom_L']['watched']
        self.assertGreater(giant_fiber['DNp01_L']['final'], 0.1)
        self.assertEqual(giant_fiber['DNp01_L']['controls_at_or_above'], 0)
        self.assertLess(giant_fiber['DNp01_R']['final'], 0.02)


if __name__ == '__main__':
    unittest.main()
