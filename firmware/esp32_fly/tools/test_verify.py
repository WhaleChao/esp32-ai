"""Protocol and negative-control tests, without serial hardware or model assets."""
import io
import json
import struct
import unittest
from unittest.mock import patch

import verify


class Port:
    def __init__(self, lines=(), reads=(), short_write=False):
        self.lines = list(lines); self.reads = list(reads)
        self.writes = []; self.short_write = short_write

    def write(self, data):
        self.writes.append(data)
        return len(data)-1 if self.short_write else len(data)

    def readline(self):
        return self.lines.pop(0) if self.lines else b''

    def read(self, size):
        if not self.reads: return b''
        data = self.reads.pop(0)
        if len(data) > size: self.reads.insert(0, data[size:])
        return data[:size]


def line(event):
    return (json.dumps(event)+'\n').encode()


class ProtocolTests(unittest.TestCase):
    def test_device_identity_circuit_and_per_core_checks(self):
        config = dict(CONNECTOME_N=48311, CONNECTOME_E=9462135, CONNECTOME_BYTES=14174411,
                      CONNECTOME_HASH=0xd66d1348)
        circuit = b'{"schema_version": 1}'
        manifest = {'circuit': {'inputs': {'L': 165, 'R': 146}, 'outputs': {'L': 4, 'R': 4}}}
        data = {'circuit': circuit}
        info = dict(event='info', ready=True, simd_selftest=True, neurons=48311,
                    edges=9462135, model_bytes=14174411, model_fnv1a='d66d1348',
                    verification_protocol=2, contract=1, simd_core0=True, simd_core1=True,
                    circuit_bytes=len(circuit), circuit_fnv1a=f'{verify.assets.fingerprint(circuit):08x}',
                    inputs=[165, 146], outputs=[4, 4])
        device = verify.Device(Port(lines=[line(info)]), io.StringIO())
        self.assertEqual(verify.device_info(device, config, manifest, data), info)
        for key, value in (('simd_core0', False), ('simd_core1', False), ('model_fnv1a', 'bad'),
                           ('contract', 2), ('verification_protocol', 1), ('neurons', 12),
                           ('circuit_fnv1a', '00000000'), ('circuit_bytes', 1), ('inputs', [165, 145]),
                           ('outputs', [4, 3])):
            bad = dict(info, **{key: value})
            with self.subTest(key=key), self.assertRaises(ValueError):
                verify.device_info(verify.Device(Port(lines=[line(bad)]), io.StringIO()), config, manifest, data)

    def test_fragmented_json_and_fatal(self):
        port = Port(lines=[b'boot text\n', b'{"event":', b'"info"}\n'])
        device = verify.Device(port, io.StringIO())
        self.assertEqual(device.event('info'), {'event':'info'})
        port.lines = [line({'event':'fatal', 'reason':'checksum'})]
        with self.assertRaisesRegex(ValueError, 'fatal'): device.event()

    def test_input_chunking_and_ack_rejection(self):
        data = bytes(2050)
        port = Port(lines=[line({'event':'input_ready','bytes':2050})], reads=[b'\6']*3)
        verify.Device(port, io.StringIO()).send_vector('INIT', data)
        self.assertEqual([len(x) for x in port.writes], [5,4,1024,1024,2])
        self.assertEqual(port.writes[1], struct.pack('<I', verify.assets.fingerprint(data)))
        port = Port(lines=[line({'event':'input_ready'})], reads=[b'x'])
        with self.assertRaisesRegex(ValueError, 'acknowledgement'):
            verify.Device(port, io.StringIO()).send_vector('INIT', bytes(4))

    def test_short_writes_and_payloads(self):
        with self.assertRaisesRegex(ValueError, 'Short serial write'):
            verify.Device(Port(short_write=True), io.StringIO()).command('INFO')
        device = verify.Device(Port(reads=[b'ab']), io.StringIO())
        with patch('verify.time.monotonic', side_effect=[0,0,121]):
            with self.assertRaisesRegex(ValueError, 'Short serial payload'): device.exact(4)
        with patch('verify.time.monotonic', side_effect=[0,121]):
            with self.assertRaises(TimeoutError): device.event('info')

    def test_graph_output_checksum(self):
        raw = struct.pack('<2f', .1, .2)
        info = {'event':'output', 'bytes':8, 'fnv1a':f'{verify.assets.fingerprint(raw):08x}'}
        port = Port(lines=[line({'event':'input_ready'}), line(info)], reads=[b'\6', raw[:3], raw[3:]])
        actual, _ = verify.Device(port, io.StringIO()).graph_step(bytes(8), 2)
        self.assertEqual(actual, raw)
        self.assertEqual(port.writes[-2:], [b'\6',b'\6'])
        info['fnv1a'] = '00000000'
        port = Port(lines=[line({'event':'input_ready'}), line(info)], reads=[b'\6', raw])
        with self.assertRaisesRegex(ValueError, 'checksum'):
            verify.Device(port, io.StringIO()).graph_step(bytes(8), 2)

    def test_numerical_negative_controls(self):
        self.assertEqual(verify.compare([.1], [.1], 3e-5)['failed'], 0)
        self.assertEqual(verify.compare([.1], [.2], 3e-5)['failed'], 1)
        for values in ([float('nan')], [float('inf')], []):
            with self.assertRaises(ValueError): verify.compare(values, [.1], 3e-5)

    def test_restart_waits_for_a_fresh_session(self):
        class Device:
            def event(self, *args, **kwargs): return events.pop(0)
        events = [dict(event='escape', active=True, neural_steps=4), dict(event='escape', active=False, neural_steps=0),
                  dict(event='escape', active=True, neural_steps=0)]
        self.assertEqual(verify.escape_restart(Device()), dict(event='escape', active=True, neural_steps=0))
        events = [dict(event='escape', active=True, neural_steps=7)]
        with patch('verify.time.monotonic', side_effect=[0, 0, 0, 121]):
            with self.assertRaisesRegex(TimeoutError, 'did not restart'):
                verify.escape_restart(Device())


class HostEscapeTests(unittest.TestCase):
    def test_replays_every_mode_and_rejects_a_wrong_gold_value(self):
        n = 6
        entry = lambda index, type_: {'index': index, 'body_id': index + 1, 'type': type_}
        circuit = {'schema_version': 1, 'contract': verify.assets.CONTRACT, 'graph_sha256': 'a'*64,
                   'order_sha256': 'b'*64, 'amplitude': 0.8, 'threshold': 0.05,
                   'inputs': {'L': [entry(0, 'LC4')], 'R': [entry(1, 'LC4')]},
                   'outputs': {'L': [entry(2, 'DNp01')], 'R': [entry(3, 'DNp01')]}}
        manifest = {'files': {'graph': {'sha256': 'a'*64}}, 'graph': {'nodes': n, 'order_sha256': 'b'*64},
                    'circuit': {'inputs': {'L': 1, 'R': 1}, 'outputs': {'L': 1, 'R': 1}}}
        modes = []

        class Model:
            def reset(self): pass
            def step(self, raw, mode):
                modes.append(mode)
                x = struct.unpack(f'<{n}f', raw)
                return struct.pack(f'<{n}f', 0, 0, x[0] * .5, x[1] * .5, 0, 0)
        gold = (struct.pack(verify.assets.ESCAPE_HEADER, b'FEG1', 1, n, 1, 2, 2, 3e-5, 3e-4)
                + struct.pack('<4f', 1, 0, 0, 1) + struct.pack('<4f', .4, 0, 0, .4))
        data = {'circuit': json.dumps(circuit).encode(), 'escape_gold': gold}
        results = verify.host_escape(Model(), manifest, data)
        self.assertEqual([r['mode'] for r in results], [0, 1, 2, 3, 4])
        self.assertEqual(sorted(set(modes)), [0, 1, 2, 3, 4])
        bad = bytearray(gold)
        struct.pack_into('<f', bad, len(bad) - 4, .41)
        with self.assertRaisesRegex(ValueError, 'Escape gold mismatch'):
            verify.host_escape(Model(), manifest, dict(data, escape_gold=bytes(bad)))
        swapped = dict(circuit, outputs={'L': circuit['outputs']['R'], 'R': circuit['outputs']['L']})
        with self.assertRaisesRegex(ValueError, 'Escape gold mismatch'):
            verify.host_escape(Model(), manifest, dict(data, circuit=json.dumps(swapped).encode()))


def escape_fixture(nodes=6):
    entry = lambda index, type_: {'index': index, 'body_id': index + 1, 'type': type_}
    circuit = {'schema_version': 1, 'contract': verify.assets.CONTRACT, 'graph_sha256': 'a'*64,
               'order_sha256': 'b'*64, 'amplitude': 0.8, 'threshold': 0.05,
               'inputs': {'L': [entry(0, 'LC4')], 'R': [entry(1, 'LC4')]},
               'outputs': {'L': [entry(2, 'DNp01')], 'R': [entry(3, 'DNp01')]}}
    manifest = {'files': {'graph': {'sha256': 'a'*64}}, 'graph': {'nodes': nodes, 'order_sha256': 'b'*64},
                'circuit': {'inputs': {'L': 1, 'R': 1}, 'outputs': {'L': 1, 'R': 1}}}
    # One case, two steps: a spider on the left, then on the right.
    gold = (struct.pack(verify.assets.ESCAPE_HEADER, b'FEG1', 1, nodes, 1, 2, 2, 3e-5, 3e-4)
            + struct.pack('<4f', 1, 0, 0, 1) + struct.pack('<4f', .4, 0, 0, .4))
    return manifest, {'circuit': json.dumps(circuit).encode(), 'escape_gold': gold}


class ReplayDevice:
    """Answers MODE, ZERO and LOOM with the events the firmware would send."""

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.commands = []
        self.pending = []

    def command(self, command):
        self.commands.append(command)
        if command.startswith('MODE'):
            self.pending.append(dict(event='info', mode=6, ready=True))
        elif command == 'ZERO':
            self.pending.append(dict(event='zeroed'))
        elif command.startswith('LOOM'):
            values = self.outputs.pop(0)
            self.pending.append(dict(event='loom', mode=6, compute_us=1690000, outputs=values,
                                     drives=verify.side_means(self.circuit, values)))

    def event(self, name=None, timeout=120):
        return self.pending.pop(0)


class DeviceEscapeTests(unittest.TestCase):
    def replay(self, outputs):
        manifest, data = escape_fixture()
        device = ReplayDevice(outputs)
        device.circuit = verify.assets.validate_circuit(data['circuit'], manifest)
        return device, verify.device_escape(device, manifest, data)

    def test_matching_replay_sends_zero_then_one_loom_per_step(self):
        device, result = self.replay([[.4, 0], [0, .4]])
        self.assertEqual(device.commands, ['MODE 6', 'ZERO', 'LOOM 3f800000 00000000', 'LOOM 00000000 3f800000'])
        self.assertEqual((result['values'], result['failed'], result['mode']), (4, 0, 6))
        self.assertTrue(result['full_suite'])

    def test_wrong_output_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'escape gold mismatch'):
            self.replay([[.4, 0], [0, .5]])

    def test_drives_must_match_the_reported_outputs(self):
        manifest, data = escape_fixture()
        device = ReplayDevice([[.4, 0], [0, .4]])
        device.circuit = verify.assets.validate_circuit(data['circuit'], manifest)
        original = device.command

        def command(text):
            original(text)
            if text.startswith('LOOM'):
                device.pending[-1]['drives'] = [0.0, 0.0]
        device.command = command
        with self.assertRaisesRegex(ValueError, 'drives disagree'):
            verify.device_escape(device, manifest, data)


class DeviceLiveTests(unittest.TestCase):
    def live(self, events, steps=2, outputs=(.4, .01)):
        manifest, data = escape_fixture()

        class Model:
            def reset(self): pass
            def step(self, raw, mode):
                return struct.pack('<6f', 0, 0, outputs[0], outputs[1], 0, 0)

        class Device:
            def command(self, command): pass
            def event(self, name=None, timeout=120): return queue.pop(0)
        queue = [dict(event='escape', active=True, neural_steps=0)] + events
        return verify.device_live(Device(), Model(), manifest, data, steps)

    def step(self, number, outputs, side=-1):
        # The firmware reports float32 values; one output per side means drive == output.
        drives = [verify.assets.float32(value) for value in outputs]
        return dict(event='escape_step', step=number, loom_bits=[0x3f800000, 0],
                    outputs=list(outputs), drives=drives, escape_side=side)

    def test_matching_steps_pass_and_count_escapes(self):
        result = self.live([self.step(1, [.4, .01], side=0), self.step(2, [.4, .01])])
        self.assertEqual((result['steps'], result['escapes'], result['failed']), (2, 1, 0))
        self.assertFalse(result['escape_quality_gate'])

    def test_dropped_step_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Dropped'):
            self.live([self.step(2, [.4, .01])])

    def test_output_mismatch_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Live output mismatch'):
            self.live([self.step(1, [.5, .01])])

    def test_escape_to_the_quiet_side_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Escape side disagrees'):
            self.live([self.step(1, [.4, .01], side=1)])

    def test_escape_below_threshold_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Escape side disagrees'):
            self.live([self.step(1, [.01, .001], side=0)], outputs=(.01, .001))


if __name__ == '__main__': unittest.main()
