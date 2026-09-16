"""Circuit export pieces on synthetic data; no model assets needed."""
import csv
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from firmware.esp32_fly.tools import prepare_assets as assets
from firmware.esp32_fly.tools import verify
from . import circuit
from .test_probe import FakeModel, escape_like

NODES = 48311


class CaseTests(unittest.TestCase):
    def test_reference_cases(self):
        cases = circuit.gold_cases(5)
        self.assertEqual(list(cases), ['left', 'right', 'both_half', 'left_approach', 'none'])
        for loom in cases.values():
            self.assertEqual(loom.shape, (5, 2))
            self.assertTrue(((loom >= 0) & (loom <= 1)).all())
        np.testing.assert_array_equal(cases['left'][:, 1], 0)
        np.testing.assert_array_equal(cases['left_approach'][[0, -1], 0], [0, 1])
        np.testing.assert_array_equal(cases['none'], 0)

    def test_gold_roundtrips_through_the_verifier(self):
        cases = circuit.gold_cases(3)
        expected = np.arange(5 * 3 * 2, dtype=np.float32).reshape(5, 3, 2) / 100
        gold = circuit.encode_gold(7, cases, expected, 3e-5, 3e-4)
        self.assertEqual(verify.escape_gold(gold)[:4], (7, 5, 3, 2))
        for c, (looms, want) in enumerate(verify.escape_cases(gold)):
            np.testing.assert_allclose(looms, list(cases.values())[c])
            np.testing.assert_allclose(want, expected[c])

    def test_exporter_and_verifier_build_identical_inputs(self):
        inputs = {'L': np.array([1, 3]), 'R': np.array([4])}
        parsed = {'amplitude': 0.8, 'inputs': {side: [{'index': int(i)} for i in group] for side, group in inputs.items()}}
        for left, right in ((1, 0), (0.5, 0.5), (np.float32(7) / np.float32(23), 0)):
            with self.subTest(left=left, right=right):
                exported = circuit.stimulus(6, inputs, 0.8, left, right).tobytes()
                self.assertEqual(exported, verify.escape_stimulus(6, parsed, float(np.float32(left)), float(np.float32(right))))

    def test_outputs_are_ordered_left_then_right(self):
        cells, w = escape_like()
        inputs = {'L': np.array([0, 1]), 'R': np.array([2, 3])}
        outputs = {'L': np.array([4]), 'R': np.array([5])}
        result = circuit.run_cases(FakeModel(w), len(cells), inputs, outputs, 0.8,
                                   {'left': circuit.gold_cases(6)['left']}, mode=0)
        self.assertGreater(result[0, -1, 0], 0.2)
        self.assertLess(result[0, -1, 1], 0.01)

    def test_side_groups_need_both_sides(self):
        cells, _ = escape_like()
        self.assertEqual(circuit.side_groups(cells, 'type=LC4')['R'].tolist(), [2, 3])
        with self.assertRaisesRegex(ValueError, 'side R'):
            circuit.side_groups(cells, 'type=DNsilent')


class ExportValidationTests(unittest.TestCase):
    """A candidate manifest together with its files passes the firmware's bundle checks."""

    def setUp(self):
        self.graph = b'FCL1' + bytes(16) + struct.pack(f'<{NODES}I', *range(NODES))
        self.graph_gold = struct.pack('<4s4I2f', b'FGF1', 1, NODES, 1, 1, 3e-5, 3e-4) + bytes(NODES * 12)
        text = io.StringIO()
        writer = csv.writer(text, lineterminator='\n')
        writer.writerow(assets.NEURON_COLUMNS)
        listed = {0: ('LC4', 'L'), 1: ('LC4', 'R'), 2: ('DNp01', 'L'), 3: ('DNp01', 'R')}
        for i in range(NODES):
            type_, side = listed.get(i, ('CB0001', ''))
            writer.writerow([i, i, 100000 + i, 'cb_intrinsic', '', type_, side])
        self.neurons = text.getvalue().encode()
        blob = lambda name, data: dict(name=name, bytes=len(data), sha256=assets.sha256(data))
        order = assets.sha256(self.graph[20:])
        self.trusted = dict(files=dict(graph=blob('connectome.fcl', self.graph),
                                       graph_gold=blob('gold-device-order.bin', self.graph_gold)),
                            graph=dict(nodes=NODES, edges=9, block_rows=32, order_sha256=order),
                            gold=dict(graph=dict(graph_sha256=assets.sha256(self.graph), order_sha256=order,
                                                 cases=1, steps=1, atol=3e-5, rtol=3e-4)))
        self.inputs = {'L': np.array([0]), 'R': np.array([1])}
        self.outputs = {'L': np.array([2]), 'R': np.array([3])}

    def export(self, **circuit_changes):
        side = lambda group, type_: [dict(index=int(i), body_id=100000 + int(i), type=type_) for i in group]
        value = dict(schema_version=1, contract=assets.CONTRACT, graph_sha256=self.trusted['files']['graph']['sha256'],
                     order_sha256=self.trusted['graph']['order_sha256'], amplitude=0.8, threshold=0.05,
                     inputs=dict(L=side(self.inputs['L'], 'LC4'), R=side(self.inputs['R'], 'LC4')),
                     outputs=dict(L=side(self.outputs['L'], 'DNp01'), R=side(self.outputs['R'], 'DNp01')))
        value.update(circuit_changes)
        circuit_bytes = json.dumps(value).encode()
        cases = circuit.gold_cases(4)
        gold = circuit.encode_gold(NODES, cases, np.zeros((5, 4, 2), np.float32), 3e-5, 3e-4)
        manifest = circuit.candidate_manifest(self.trusted, 'fly-escape-test', circuit_bytes, gold, self.neurons,
                                              self.inputs, self.outputs, 4, 3e-5, 3e-4)
        return manifest, circuit_bytes, gold

    def test_candidate_and_files_validate(self):
        manifest, circuit_bytes, gold = self.export()
        circuit.validate_export(manifest, circuit_bytes, gold, self.neurons, self.graph, self.graph_gold)
        self.assertEqual(manifest['files']['graph'], self.trusted['files']['graph'])
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)/'model_bundle.json'
            path.write_text(json.dumps(manifest))
            self.assertEqual(assets.load_manifest(path), manifest)

    def test_mismatched_files_fail_before_anything_is_written(self):
        manifest, circuit_bytes, gold = self.export(threshold=1.5)
        with self.assertRaises(ValueError):
            circuit.validate_export(manifest, circuit_bytes, gold, self.neurons, self.graph, self.graph_gold)
        manifest, circuit_bytes, gold = self.export()
        with self.assertRaises(ValueError):
            circuit.validate_export(manifest, circuit_bytes, gold, self.neurons.replace(b',LC4,L', b',LC4,R', 1),
                                    self.graph, self.graph_gold)

    def test_a_non_empty_output_directory_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            (Path(temporary)/'keep').write_text('x')
            with patch.object(sys, 'argv', ['circuit', '--out-dir', temporary]), \
                    patch('sys.stderr', io.StringIO()), self.assertRaises(SystemExit):
                circuit.main()
            self.assertEqual([p.name for p in Path(temporary).iterdir()], ['keep'])


if __name__ == '__main__':
    unittest.main()
