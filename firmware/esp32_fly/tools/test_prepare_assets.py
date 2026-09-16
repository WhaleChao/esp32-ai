"""Asset-contract regressions; standard library only, no private/released data.

python3 -m unittest discover -s firmware/esp32_fly/tools -p 'test_*.py'
"""

import copy
import csv
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest

import prepare_assets as assets

NODES = 48311
CIRCUIT = {"inputs": {"L": [(0, "LC4"), (1, "LPLC2")], "R": [(2, "LC4"), (3, "LPLC2")]},
           "outputs": {"L": [(4, "DNp01")], "R": [(5, "DNp01")]}}


def graph_bytes():
    """Supported shape with one edge."""
    order = struct.pack(f"<{NODES}I", *range(NODES))
    graph = bytearray(struct.pack("<4s4I", b"FCL1", 1, NODES, 1, 32) + order)
    for row in range(0, NODES, 32):
        count = min(32, NODES - row)
        raw = bytes([int(row == 0)]) + bytes(count - 1)
        if row == 0:
            raw += b'\x00\x01'  # source 0, contact count 1
        packed = bytes([0xf0, len(raw) - 15]) + raw
        if len(raw) < 15:
            packed = bytes([len(raw) << 4]) + raw
        graph += struct.pack("<4I", count, int(row == 0), len(raw), len(packed)) + packed
    return bytes(graph), order


def neurons_bytes(rows=None):
    listed = {index: (type_, side) for kind in CIRCUIT.values() for side, entries in kind.items()
              for index, type_ in entries}
    text = io.StringIO()
    writer = csv.writer(text, lineterminator="\n")
    writer.writerow(assets.NEURON_COLUMNS)
    for i in range(NODES):
        type_, side = listed.get(i, ("CB0001", ""))
        writer.writerow([i, i, 100000 + i, "cb_intrinsic", "", type_, side])
    return text.getvalue().encode()


def circuit_bytes(graph_sha, order_sha, **changes):
    value = {"schema_version": 1, "contract": assets.CONTRACT, "graph_sha256": graph_sha, "order_sha256": order_sha,
             "amplitude": 0.8, "threshold": 0.05}
    for kind, sides in CIRCUIT.items():
        value[kind] = {side: [{"index": i, "body_id": 100000 + i, "type": t} for i, t in entries]
                       for side, entries in sides.items()}
    value.update(changes)
    return json.dumps(value).encode()


def escape_gold_bytes(looms=(1, 0, 1, 0)):
    return struct.pack(assets.ESCAPE_HEADER, b"FEG1", 1, NODES, 1, 2, 2, 3e-5, 3e-4) + struct.pack("<4f", *looms) + bytes(16)


def fixture():
    graph, order = graph_bytes()
    graph_sha, order_sha = assets.sha256(graph), assets.sha256(order)
    data = dict(graph=graph,
                graph_gold=struct.pack("<4s4I2f", b"FGF1", 1, NODES, 1, 1, 3e-5, 3e-4) + bytes(NODES*12),
                circuit=circuit_bytes(graph_sha, order_sha), escape_gold=escape_gold_bytes(), neurons=neurons_bytes())
    names = dict(graph="connectome.fcl", graph_gold="gold-device-order.bin", circuit="escape-circuit.json",
                 escape_gold="escape-gold.bin", neurons="neurons.csv")
    manifest = {
        "schema_version": assets.SCHEMA, "bundle_id": "test", "contract": assets.CONTRACT,
        "files": {role: {"name": names[role], "bytes": len(blob), "sha256": assets.sha256(blob)} for role, blob in data.items()},
        "graph": {"nodes": NODES, "edges": 1, "block_rows": 32, "order_sha256": order_sha},
        "circuit": {"graph_sha256": graph_sha, "order_sha256": order_sha,
                    "inputs": {"L": 2, "R": 2}, "outputs": {"L": 1, "R": 1}},
        "gold": {"graph": {"graph_sha256": graph_sha, "order_sha256": order_sha, "cases": 1, "steps": 1,
                           "atol": 3e-5, "rtol": 3e-4},
                 "escape": {"circuit_sha256": assets.sha256(data["circuit"]), "graph_sha256": graph_sha,
                            "cases": 1, "steps": 2, "outputs": 2, "atol": 3e-5, "rtol": 3e-4}},
    }
    return manifest, data


class BundleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest, cls.data = fixture()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='fly bundle test ')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bundle = self.root / 'bundle'
        self.bundle.mkdir()
        self.output = self.root / 'generated inputs'
        self.trusted = self.root / 'trusted.json'
        text = json.dumps(self.manifest)
        self.trusted.write_text(text)
        (self.bundle / 'model_bundle.json').write_text(text)
        for role, blob in self.data.items():
            (self.bundle / self.manifest['files'][role]['name']).write_bytes(blob)

    def prepare(self):
        return assets.prepare(self.bundle, manifest_path=self.trusted, output=self.output)

    def snapshot(self):
        return {p.name: p.read_bytes() for p in self.output.iterdir()}

    def test_complete_bundle_and_repeat(self):
        report = self.prepare()
        self.assertFalse(report['numerical_gold_executed'])
        self.assertEqual(report['graph_blocks'], 1510)
        self.assertEqual(report['config']['CONNECTOME_RAW'], 34)
        self.assertEqual(report['config']['CONNECTOME_COMPRESSED'], 36)
        self.assertEqual(report['config']['CONNECTOME_BYTES'], len(self.data['graph']))
        self.assertEqual(report['escape']['inputs'], {'L': 2, 'R': 2})
        header = (self.output/'escape_circuit.h').read_text()
        self.assertIn('#define ESCAPE_AMPLITUDE 0.800000011920929f', header)
        self.assertIn('#define ESCAPE_THRESHOLD 0.05000000074505806f', header)
        self.assertIn('static const uint32_t escape_inputs_left[ESCAPE_INPUTS_L] = {0, 1};', header)
        self.assertIn('static const uint32_t escape_outputs_right[ESCAPE_OUTPUTS_R] = {5};', header)
        self.assertIn(f'#define ESCAPE_CIRCUIT_HASH 0x{assets.fingerprint(self.data["circuit"]):08x}u', header)
        before = self.snapshot()
        self.prepare()
        self.assertEqual(before, self.snapshot())
        self.assertEqual(assets.fingerprint(b'hello'), 0x4f9f2cab)

    def test_untrusted_bundle_cannot_approve_itself(self):
        with self.assertRaisesRegex(ValueError, 'trusted manifest'):
            assets.prepare(self.bundle, output=self.output)
        self.assertFalse(self.output.exists())

    def test_missing_or_corrupt_assets_preserve_generated_files(self):
        self.prepare()
        before = self.snapshot()
        for role, original in self.data.items():
            path = self.bundle/self.manifest['files'][role]['name']
            for damage in ('missing', 'truncated', 'corrupt'):
                with self.subTest(role=role, damage=damage):
                    if damage == 'missing':
                        path.unlink()
                    else:
                        value = original[:-1] if damage == 'truncated' else original[:-1] + bytes([original[-1] ^ 1])
                        path.write_bytes(value)
                    with self.assertRaises((OSError, ValueError)):
                        self.prepare()
                    self.assertEqual(before, self.snapshot())
                    path.write_bytes(original)

    def test_manifest_mismatches(self):
        changes = [
            (('schema_version',), 1), (('contract',), 'fly-scalar-intercept-v1'),
            (('graph', 'nodes'), 123), (('circuit', 'graph_sha256'), '0'*64),
            (('circuit', 'order_sha256'), '0'*64), (('gold', 'escape', 'circuit_sha256'), '0'*64),
            (('gold', 'escape', 'graph_sha256'), '0'*64), (('gold', 'escape', 'outputs'), 3),
            (('circuit', 'inputs', 'L'), 0), (('circuit', 'outputs', 'R'), 65), (('circuit', 'inputs', 'R'), True),
            (('files', 'graph', 'name'), '../escape'), (('files', 'graph', 'name'), 'neurons.csv'),
            (('files', 'circuit', 'name'), 'model_bundle.json'), (('files', 'graph', 'bytes'), -1),
            (('gold', 'graph', 'atol'), float('nan')), (('gold', 'escape', 'rtol'), 0.5),
        ]
        for keys, value in changes:
            with self.subTest(keys=keys, value=value):
                modified = copy.deepcopy(self.manifest)
                parent = modified
                for key in keys[:-1]:
                    parent = parent[key]
                parent[keys[-1]] = value
                self.trusted.write_text(json.dumps(modified))
                with self.assertRaises(ValueError):
                    assets.load_manifest(self.trusted)
        modified = copy.deepcopy(self.manifest)
        modified['files']['extra'] = modified['files']['graph']
        self.trusted.write_text(json.dumps(modified))
        with self.assertRaisesRegex(ValueError, 'must describe'):
            assets.load_manifest(self.trusted)
        self.assertFalse(self.output.exists())

    def test_graph_container_errors(self):
        original = self.data['graph']
        block = 20 + NODES*4
        variants = [original[:19], original[:-1], original+b'\0']
        for position, value in ((block, 0), (block+8, 0), (block+12, 0xffffffff)):
            data = bytearray(original)
            struct.pack_into('<I', data, position, value)
            variants.append(data)
        for data in variants:
            with self.subTest(size=len(data)):
                with self.assertRaises(ValueError):
                    assets.graph_config(data, self.manifest['graph'])
        data = bytearray(original)
        struct.pack_into('<I', data, 20, 1)  # Duplicate canonical index.
        expected = dict(self.manifest['graph'], order_sha256=assets.sha256(data[20:block]))
        with self.assertRaisesRegex(ValueError, 'permutation'):
            assets.graph_config(data, expected)

    def test_circuit_errors(self):
        graph_sha = self.manifest['files']['graph']['sha256']
        order_sha = self.manifest['graph']['order_sha256']
        valid = json.loads(self.data['circuit'])
        entry = lambda i: {'index': i, 'body_id': 100000 + i, 'type': 'LC4'}
        cases = {
            'not json': b'{',
            'contract': circuit_bytes(graph_sha, order_sha, contract='other'),
            'graph binding': circuit_bytes('0'*64, order_sha),
            'amplitude zero': circuit_bytes(graph_sha, order_sha, amplitude=0),
            'amplitude above one': circuit_bytes(graph_sha, order_sha, amplitude=1.5),
            'amplitude nan': circuit_bytes(graph_sha, order_sha, amplitude=float('nan')),
            'threshold one': circuit_bytes(graph_sha, order_sha, threshold=1),
            'missing outputs': circuit_bytes(graph_sha, order_sha, outputs=None),
            'count': circuit_bytes(graph_sha, order_sha, inputs=dict(valid['inputs'], L=[entry(0)])),
            'duplicate': circuit_bytes(graph_sha, order_sha, inputs=dict(valid['inputs'], L=[entry(0), entry(4)])),
            'range': circuit_bytes(graph_sha, order_sha, inputs=dict(valid['inputs'], L=[entry(0), entry(NODES)])),
            'bool index': circuit_bytes(graph_sha, order_sha, outputs=dict(valid['outputs'], L=[dict(entry(4), index=True)])),
            'empty type': circuit_bytes(graph_sha, order_sha, outputs=dict(valid['outputs'], L=[dict(entry(4), type='')])),
        }
        for name, blob in cases.items():
            with self.subTest(name=name), self.assertRaises(ValueError):
                assets.validate_circuit(blob, self.manifest)

    def test_neurons_must_follow_graph_order_and_match_the_circuit(self):
        circuit = assets.validate_circuit(self.data['circuit'], self.manifest)
        lines = self.data['neurons'].decode().splitlines()
        swapped = lines[:]
        swapped[1], swapped[2] = swapped[2], swapped[1]
        wrong_canonical = lines[:]
        wrong_canonical[1] = '0,1,100000,cb_intrinsic,,LC4,L'
        wrong_side = lines[:]
        wrong_side[1] = '0,0,100000,cb_intrinsic,,LC4,R'
        wrong_type = lines[:]
        wrong_type[5] = '4,4,100004,cb_intrinsic,,DNp02,L'
        cases = {
            'columns': ['execution_index,body_id'] + lines[1:],
            'row order': swapped, 'canonical order': wrong_canonical,
            'missing row': lines[:-1], 'side': wrong_side, 'type': wrong_type,
        }
        graph = self.data['graph']
        for name, rows in cases.items():
            with self.subTest(name=name), self.assertRaises(ValueError):
                assets.validate_neurons(('\n'.join(rows) + '\n').encode(), self.manifest, circuit, graph)
        with self.assertRaises(ValueError):
            assets.validate_neurons(b'\xff\xfe', self.manifest, circuit, graph)

    def test_manifest_structure_errors(self):
        for change in (lambda m: m.update(files=list(m['files'])), lambda m: m['graph'].update(nodes=48311.0),
                       lambda m: m['graph'].update(block_rows=32.0), lambda m: m.update(schema_version=2.0),
                       lambda m: m['gold'].update(escape=[]), lambda m: m['circuit'].update(inputs=[2, 2]),
                       lambda m: m['files'].update(graph='connectome.fcl')):
            modified = copy.deepcopy(self.manifest)
            change(modified)
            with self.subTest(change=change), self.assertRaises(ValueError):
                assets.check_manifest(modified)
        with self.assertRaises(ValueError):
            assets.check_manifest([])

    def test_circuit_values_are_checked_as_float32(self):
        graph_sha = self.manifest['files']['graph']['sha256']
        order_sha = self.manifest['graph']['order_sha256']
        for changes in (dict(threshold=0.99999999999), dict(threshold=1e-50), dict(amplitude=1e-50),
                        dict(order_sha256='0'*64), dict(schema_version=2), dict(schema_version=True)):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                assets.validate_circuit(circuit_bytes(graph_sha, order_sha, **changes), self.manifest)

    def test_neurons_rows_must_be_exact(self):
        circuit = assets.validate_circuit(self.data['circuit'], self.manifest)
        lines = self.data['neurons'].decode().splitlines()
        body = lines[:]
        body[1] = '0,0,999,cb_intrinsic,,LC4,L'
        extra_field = lines[:]
        extra_field[10] = extra_field[10] + ',surplus'
        short = lines[:]
        short[10] = '9,9,100009,cb_intrinsic,,CB0001'
        cases = {'body id': body, 'extra row': lines + [f'{NODES},{NODES},1,cb_intrinsic,,CB0001,'],
                 'extra field': extra_field, 'short row': short, 'nul': lines[:10] + ['9,9,100009,cb\0,,CB0001,'] + lines[11:]}
        for name, rows in cases.items():
            with self.subTest(name=name), self.assertRaises(ValueError):
                assets.validate_neurons(('\n'.join(rows) + '\n').encode(), self.manifest, circuit, self.data['graph'])
        crlf = ('\r\n'.join(lines) + '\r\n').encode()
        assets.validate_neurons(crlf, self.manifest, circuit, self.data['graph'])

    def test_obsolete_generated_files_are_removed(self):
        self.output.mkdir()
        for name in assets.OBSOLETE_GENERATED:
            (self.output/name).write_text('stale')
        self.prepare()
        for name in assets.OBSOLETE_GENERATED:
            self.assertFalse((self.output/name).exists())

    def test_gold_header_bindings(self):
        wrong_nodes = bytearray(self.data['escape_gold'])
        struct.pack_into('<I', wrong_nodes, 8, NODES - 1)
        with self.assertRaisesRegex(ValueError, 'header'):
            assets.validate_escape_gold(bytes(wrong_nodes), self.manifest)
        wrong_tolerance = bytearray(self.data['graph_gold'])
        struct.pack_into('<f', wrong_tolerance, 20, 1e-4)
        with self.assertRaisesRegex(ValueError, 'tolerance'):
            assets.validate_graph_gold(bytes(wrong_tolerance), self.manifest)

    def test_gold_errors(self):
        data = bytearray(self.data['graph_gold'])
        struct.pack_into('<f', data, 28, float('nan'))
        with self.assertRaisesRegex(ValueError, 'Nonfinite'):
            assets.validate_graph_gold(data, self.manifest)
        with self.assertRaises(ValueError):
            assets.validate_graph_gold(self.data['graph_gold'][:-1], self.manifest)
        header = struct.calcsize(assets.ESCAPE_HEADER)
        data = bytearray(self.data['escape_gold'])
        struct.pack_into('<f', data, header + 8, float('inf'))
        with self.assertRaisesRegex(ValueError, 'Nonfinite'):
            assets.validate_escape_gold(data, self.manifest)
        with self.assertRaisesRegex(ValueError, r'\[0, 1\]'):
            assets.validate_escape_gold(escape_gold_bytes(looms=(1.5, 0, 1, 0)), self.manifest)
        with self.assertRaisesRegex(ValueError, 'size'):
            assets.validate_escape_gold(self.data['escape_gold'][:-1], self.manifest)
        wrong_outputs = dict(self.manifest, gold=dict(self.manifest['gold'], escape=dict(self.manifest['gold']['escape'], outputs=3)))
        with self.assertRaisesRegex(ValueError, 'header'):
            assets.validate_escape_gold(self.data['escape_gold'], wrong_outputs)


if __name__ == '__main__':
    unittest.main()
