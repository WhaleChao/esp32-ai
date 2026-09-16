#!/usr/bin/env python3
"""Host gold and serial verification for the fly firmware. Never builds/flashes firmware.

See ../VERIFICATION.md for command scopes, report interpretation and recovery.
Host mode requires a C compiler; device modes additionally require pyserial.
"""
import argparse
import ctypes as ct
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import time

try:
    from . import prepare_assets as assets
except ImportError:
    import prepare_assets as assets

ROOT = Path(__file__).resolve().parents[3]
ACK = b'\x06'


def check(condition, message):
    if not condition:
        raise ValueError(message)


def load_bundle(path, trusted):
    manifest, data, config, _, _ = assets.read_bundle(path, trusted)
    return manifest, data, config


def compare(actual, expected, atol, rtol=0):
    check(len(actual) == len(expected) and len(actual) > 0, 'Comparison shape mismatch')
    failed = 0
    maximum = 0.0
    for a, e in zip(actual, expected):
        check(math.isfinite(a) and math.isfinite(e), 'Nonfinite comparison value')
        error = abs(a-e)
        maximum = max(maximum, error)
        failed += error > atol + rtol*abs(e)
    return dict(values=len(actual), failed=failed, max_abs=maximum)


class HostModel:
    def __init__(self, data, directory):
        compiler = shutil.which('cc')
        check(compiler, 'A C compiler (cc) is required')
        library = directory/'connectome_bridge.so'
        subprocess.run([compiler, '-std=c11', '-O3', '-ffp-contract=off', '-Wall', '-Wextra', '-Werror',
                        '-shared', '-fPIC', str(ROOT/'runtime/host_verify/connectome_bridge.c'), '-lm', '-o', str(library)], check=True)
        self.lib = ct.CDLL(str(library))
        pointer = ct.POINTER(ct.c_float)
        self.lib.fly_create.argtypes = [ct.c_void_p, ct.c_size_t]
        self.lib.fly_create.restype = ct.c_void_p
        self.lib.fly_destroy.argtypes = [ct.c_void_p]
        self.lib.fly_reset.argtypes = [ct.c_void_p, pointer]
        self.lib.fly_step.argtypes = [ct.c_void_p, pointer, pointer, ct.c_int]
        self.graph = ct.create_string_buffer(data['graph'])
        self.handle = self.lib.fly_create(self.graph, len(data['graph']))
        check(self.handle, 'Runtime rejected the graph')

    def close(self):
        if self.handle:
            self.lib.fly_destroy(self.handle)
            self.handle = None

    def reset(self, raw=None):
        value = (ct.c_float*48311).from_buffer_copy(raw) if raw is not None else None
        check(self.lib.fly_reset(self.handle, value) == 0, 'State reset failed')

    def step(self, raw, mode):
        stimulus = (ct.c_float*48311).from_buffer_copy(raw)
        output = (ct.c_float*48311)()
        check(self.lib.fly_step(self.handle, stimulus, output, mode) == 0, 'Graph execution failed')
        return bytes(output)


def graph_gold(data):
    _, _, n, cases, steps, atol, rtol = struct.unpack_from('<4s4I2f', data)
    return n, cases, steps, atol, rtol


def escape_gold(data):
    _, _, n, cases, steps, outputs, atol, rtol = struct.unpack_from(assets.ESCAPE_HEADER, data)
    return n, cases, steps, outputs, atol, rtol


def escape_cases(data):
    """Per case: loom levels (left, right) per step and expected output states per step."""
    _, cases, steps, outputs, _, _ = escape_gold(data)
    header, size = struct.calcsize(assets.ESCAPE_HEADER), steps*(2+outputs)*4
    for case in range(cases):
        offset = header + case*size
        looms = struct.unpack_from(f'<{2*steps}f', data, offset)
        expected = struct.unpack_from(f'<{steps*outputs}f', data, offset + 2*steps*4)
        yield ([looms[2*t:2*t+2] for t in range(steps)],
               [expected[outputs*t:outputs*(t+1)] for t in range(steps)])


def escape_stimulus(n, circuit, left, right):
    """float32 graph input for one step, as the firmware computes it."""
    amplitude = struct.unpack('<f', struct.pack('<f', circuit['amplitude']))[0]
    vector = bytearray(n*4)
    for side, level in (('L', left), ('R', right)):
        value = struct.pack('<f', amplitude*level)
        for entry in circuit['inputs'][side]:
            vector[entry['index']*4:entry['index']*4+4] = value
    return bytes(vector)


def host_escape(model, manifest, data):
    circuit = assets.validate_circuit(data['circuit'], manifest)
    order = [entry['index'] for side in assets.SIDES for entry in circuit['outputs'][side]]
    n, cases, steps, outputs, atol, rtol = escape_gold(data['escape_gold'])
    results = []
    for mode in range(5):
        values = 0; maximum = 0.0
        for case, (looms, expected) in enumerate(escape_cases(data['escape_gold'])):
            model.reset()
            for step, ((left, right), want) in enumerate(zip(looms, expected)):
                state = model.step(escape_stimulus(n, circuit, left, right), mode)
                actual = [struct.unpack_from('<f', state, index*4)[0] for index in order]
                result = compare(actual, want, atol, rtol)
                check(not result['failed'], f'Escape gold mismatch: mode={mode}, case={case}, step={step}, {result}')
                values += result['values']; maximum = max(maximum, result['max_abs'])
        print(f'Host escape mode {mode}, {cases} cases: PASS', flush=True)
        results.append(dict(mode=mode, values=values, failed=0, max_abs=maximum))
    return results


def host_checks(model, manifest, data, directory):
    fixtures = []
    for name in ('lz4_block', 'lz4_block_fast', 'connectome_stream', 'connectome_fast', 'connectome_simd', 'escape_world'):
        exe = directory/name
        command = ['cc', '-std=c11', '-O1', '-ffp-contract=off', '-Wall', '-Wextra', '-Werror',
                   '-fsanitize=address,undefined', str(ROOT/f'runtime/host_verify/{name}_test.c'), '-lm', '-o', str(exe)]
        if name == 'connectome_stream': command.append('-lz')
        subprocess.run(command, check=True)
        result = subprocess.run([str(exe)], capture_output=True, text=True, check=True)
        fixtures.append(dict(name=name, result=(result.stdout+result.stderr).strip()))
    n, cases, steps, atol, rtol = graph_gold(data['graph_gold'])
    results = []
    for mode in range(5):
        offset = 28
        values = failed = 0
        maximum = 0.0
        for case in range(cases):
            model.reset(data['graph_gold'][offset:offset+n*4]); offset += n*4
            for step in range(steps):
                stimulus = data['graph_gold'][offset:offset+n*4]; offset += n*4
                expected = struct.unpack_from(f'<{n}f', data['graph_gold'], offset); offset += n*4
                actual = struct.unpack(f'<{n}f', model.step(stimulus, mode))
                result = compare(actual, expected, atol, rtol)
                values += result['values']; failed += result['failed']; maximum = max(maximum, result['max_abs'])
                check(not result['failed'], f'Graph gold mismatch: mode={mode}, case={case}, step={step}, {result}')
            print(f'Host graph mode {mode}, case {case+1}/{cases}: PASS', flush=True)
        results.append(dict(mode=mode, values=values, failed=failed, max_abs=maximum, full_suite=True))
    return dict(target='host', fixtures=fixtures, graph=results, escape=host_escape(model, manifest, data),
                xtensa_instructions_executed=False)


class Device:
    def __init__(self, port, log):
        self.port, self.log = port, log
        self.line = bytearray()

    def write(self, data):
        check(self.port.write(data) == len(data), 'Short serial write')

    def command(self, command):
        self.write(command.encode()+b'\n')

    def event(self, name=None, timeout=120):
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            self.line.extend(self.port.readline())
            check(len(self.line) <= 65536, 'Oversized serial event')
            if not self.line.endswith(b'\n'):
                continue
            raw = bytes(self.line)
            self.line.clear()
            try: value = json.loads(raw)
            except (ValueError, UnicodeError): continue
            if not isinstance(value, dict): continue
            self.log.write(json.dumps(value)+'\n'); self.log.flush()
            check(value.get('event') != 'fatal', f'Device fatal: {value}')
            if name is None or value.get('event') == name: return value
        raise TimeoutError(f'Device event timeout: {name}')

    def exact(self, count):
        data = bytearray(); deadline = time.monotonic()+120
        while len(data) < count and time.monotonic() < deadline:
            data.extend(self.port.read(count-len(data)))
        check(len(data) == count, f'Short serial payload: {len(data)}/{count}')
        return bytes(data)

    def send_vector(self, command, data):
        self.command(command)
        ready = self.event('input_ready')
        if 'bytes' in ready: check(ready['bytes'] == len(data), 'Input size mismatch')
        self.write(struct.pack('<I', assets.fingerprint(data)))
        for offset in range(0, len(data), 1024):
            self.write(data[offset:offset+1024])
            check(self.exact(1) == ACK, 'Missing input acknowledgement')

    def graph_step(self, stimulus, n):
        self.send_vector('STEP', stimulus)
        event = self.event('output', timeout=600)
        check(event.get('bytes') == n*4, 'Output size mismatch')
        self.write(ACK)
        data = bytearray()
        for offset in range(0, n*4, 4096):
            data.extend(self.exact(min(4096, n*4-offset))); self.write(ACK)
        check(event.get('fnv1a') == f'{assets.fingerprint(data):08x}', 'Output checksum mismatch')
        return data, event


def device_info(device, config, manifest, data):
    device.command('STOP'); device.command('INFO')
    info = device.event('info')
    check(info.get('ready') is True and info.get('simd_selftest') is True, 'Device not ready or SIMD boot self-test failed')
    for field, key in (('neurons','CONNECTOME_N'), ('edges','CONNECTOME_E'), ('model_bytes','CONNECTOME_BYTES')):
        check(info.get(field) == config[key], f'Device {field} mismatch')
    check(info.get('model_fnv1a') == f'{config["CONNECTOME_HASH"]:08x}', 'Device graph fingerprint mismatch')
    check(info.get('verification_protocol') == 2 and info.get('contract') == 1,
          'Unsupported device verification protocol or contract; flash the escape firmware')
    check(info.get('simd_core0') is True and info.get('simd_core1') is True, 'Per-core SIMD boot self-test failed')
    # FNV-1a detects an accidental mismatch; it is not a cryptographic firmware identity.
    check(info.get('circuit_bytes') == len(data['circuit'])
          and info.get('circuit_fnv1a') == f'{assets.fingerprint(data["circuit"]):08x}', 'Device circuit identity mismatch')
    counts = manifest['circuit']
    check(info.get('inputs') == [counts['inputs']['L'], counts['inputs']['R']]
          and info.get('outputs') == [counts['outputs']['L'], counts['outputs']['R']], 'Device circuit shape mismatch')
    return info


def device_graph(device, data, steps):
    n, cases, available, atol, rtol = graph_gold(data['graph_gold'])
    check(1 <= steps <= available, 'Invalid graph step count')
    device.command('MODE 6'); info = device.event('info')
    check(info.get('mode') == 6 and info.get('ready'), 'Device mode switch failed')
    results = []
    for case in range(cases):
        offset = 28 + case*(1+2*available)*n*4
        device.send_vector('INIT', data['graph_gold'][offset:offset+n*4]); offset += n*4
        device.event('initialized'); maximum = 0.0
        for step in range(steps):
            stimulus = data['graph_gold'][offset:offset+n*4]; offset += n*4
            expected = struct.unpack_from(f'<{n}f', data['graph_gold'], offset); offset += n*4
            raw, timing = device.graph_step(stimulus, n)
            result = compare(struct.unpack(f'<{n}f', raw), expected, atol, rtol)
            check(not result['failed'], f'Device graph mismatch: case={case}, step={step}, {result}')
            maximum = max(maximum, result['max_abs'])
            print(f'Device graph case {case+1}/{cases}, step {step+1}/{steps}: PASS ({timing["compute_us"]/1e6:.3f}s)', flush=True)
        results.append(dict(case=case, values=n*steps, max_abs=maximum, failed=0))
    return dict(cases=results, values=n*cases*steps, full_suite=steps == available, mode=6)


def side_means(circuit, outputs):
    """The float32 drive per side the firmware reports, recomputed from its own outputs."""
    means, offset = [], 0
    for side in assets.SIDES:
        count = len(circuit['outputs'][side])
        total = 0.0
        for value in outputs[offset:offset+count]:
            total = assets.float32(total + assets.float32(value))
        means.append(assets.float32(total / count))
        offset += count
    return means


def check_drives(event, circuit, where=''):
    """The device prints nine significant digits of a float32; compare as float32."""
    reported = [assets.float32(value) for value in event['drives']]
    check(not compare(reported, side_means(circuit, event['outputs']), 0)['failed'],
          f'Device drives disagree with its own outputs{where}')


def device_escape(device, manifest, data):
    """Replay the escape gold on the board: ZERO, then one LOOM command per step."""
    circuit = assets.validate_circuit(data['circuit'], manifest)
    _, cases, steps, _, atol, rtol = escape_gold(data['escape_gold'])
    device.command('MODE 6')
    info = device.event('info')
    check(info.get('mode') == 6 and info.get('ready'), 'Device mode switch failed')
    results = []
    values = 0
    maximum = 0.0
    for case, (looms, expected) in enumerate(escape_cases(data['escape_gold'])):
        device.command('ZERO')
        device.event('zeroed')
        case_max = 0.0
        for step, ((left, right), want) in enumerate(zip(looms, expected)):
            bits = struct.unpack('<2I', struct.pack('<2f', left, right))
            device.command(f'LOOM {bits[0]:08x} {bits[1]:08x}')
            event = device.event('loom', timeout=600)
            result = compare(event['outputs'], want, atol, rtol)
            check(not result['failed'], f'Device escape gold mismatch: case={case}, step={step}, {result}')
            check_drives(event, circuit, f': case={case}, step={step}')
            values += result['values']
            case_max = max(case_max, result['max_abs'])
        print(f'Device escape case {case+1}/{cases}, {steps} steps: PASS (max error {case_max:.3g})', flush=True)
        results.append(dict(case=case, steps=steps, values=steps*len(want), max_abs=case_max, failed=0))
        maximum = max(maximum, case_max)
    return dict(cases=results, values=values, max_abs=maximum, failed=0, full_suite=True, mode=6,
                atol=atol, rtol=rtol)


def device_live(device, model, manifest, data, steps):
    """Follow the autonomous demo: replay each step's looming on the host and compare."""
    check(steps >= 2, 'Live replay needs at least two steps')
    circuit = assets.validate_circuit(data['circuit'], manifest)
    threshold = assets.float32(circuit['threshold'])
    nodes = manifest['graph']['nodes']
    model.reset()
    device.command('PLAY')
    escape_restart(device)
    count = escapes = 0
    maximum = 0.0
    while count < steps:
        event = device.event('escape_step', timeout=600)
        check(event['step'] == count+1, 'Dropped, duplicated or out-of-order neural step')
        left, right = (struct.unpack('<f', struct.pack('<I', bits))[0] for bits in event['loom_bits'])
        check(0 <= left <= 1 and 0 <= right <= 1, 'Loom levels outside [0, 1]')
        state = model.step(escape_stimulus(nodes, circuit, left, right), 2)
        expected = [struct.unpack_from('<f', state, entry['index']*4)[0]
                    for side in assets.SIDES for entry in circuit['outputs'][side]]
        result = compare(event['outputs'], expected, 3e-4)
        check(not result['failed'], f'Live output mismatch: step={event["step"]}, {result}')
        drives = side_means(circuit, event['outputs'])
        check_drives(event, circuit, f': step={event["step"]}')
        side = event['escape_side']
        if side >= 0:
            # The world decides when an escape may happen; the circuit decides which side.
            check(max(drives) > threshold and side == (0 if drives[0] > drives[1] else 1),
                  f'Escape side disagrees with the drives: step={event["step"]}, {drives}, side={side}')
            escapes += 1
        maximum = max(maximum, result['max_abs'])
        count += 1
        if count % 5 == 0:
            print(f'Device live replay: {count}/{steps} steps, max error {maximum:.3g}', flush=True)
    return dict(values=count*len(expected), failed=0, max_abs=maximum, steps=count, escapes=escapes,
                action_tolerance=3e-4, host_supplied_stimuli=False, host_supplied_actions=False,
                escape_quality_gate=False)


def escape_restart(device):
    deadline = time.monotonic()+120
    while time.monotonic() < deadline:
        event = device.event('escape', timeout=max(.1, deadline-time.monotonic()))
        if event.get('active') and event.get('neural_steps') == 0:
            return event
    raise TimeoutError('Escape demo did not restart')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('host', 'device-graph', 'device-escape', 'device-live', 'device-simd'))
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, default=assets.DEFAULT_MANIFEST)
    parser.add_argument('--report', type=Path, required=True, help='New JSON report path; never overwritten')
    parser.add_argument('--port', help='Required for device commands')
    parser.add_argument('--steps', type=int, help='Device graph: default 32; live: default 60')
    args = parser.parse_args()
    if args.command != 'host' and not args.port: parser.error('--port is required for device commands')
    if args.steps is not None and args.command not in ('device-graph', 'device-live'):
        parser.error('--steps does not apply to this command')
    if args.steps is not None:
        if args.command == 'device-graph' and not 1 <= args.steps <= 32: parser.error('--steps must be 1..32')
        if args.command == 'device-live' and args.steps < 2: parser.error('--steps must be at least 2')
    if args.report.suffix != '.json': parser.error('--report must end in .json')
    if args.command != 'host' and args.report.with_suffix('.jsonl').exists():
        parser.error('Device JSONL log already exists; choose a new report path')
    args.report.parent.mkdir(parents=True, exist_ok=True)
    # Reserve the report before any interaction; a failed run cannot leave an old PASS.
    with args.report.open('x') as report_file:
        started = time.monotonic()
        report = dict(status='ERROR', command=args.command, flashed=False,
                      started_at=datetime.now(timezone.utc).isoformat())
        model = None
        try:
            manifest, data, config = load_bundle(args.bundle, args.manifest)
            report['bundle_id'] = manifest['bundle_id']; report['assets'] = manifest['files']
            report['contract'] = manifest['contract']
            report['manifest_sha256'] = assets.sha256(json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode())
            sources = (list((ROOT/'runtime').glob('connectome*.h')) + list((ROOT/'runtime').glob('lz4_block*.h'))
                       + [ROOT/'runtime/host_verify/connectome_bridge.c', Path(__file__), ROOT/'firmware/esp32_fly/tools/prepare_assets.py',
                          ROOT/'firmware/esp32_fly/escape_world.h', ROOT/'firmware/esp32_fly/escape_app.h',
                          ROOT/'firmware/esp32_fly/escape_ui.h', ROOT/'firmware/esp32_fly/esp32_fly.ino',
                          ROOT/'runtime/host_verify/escape_world_test.c'])
            report['source_sha256'] = {str(p.relative_to(ROOT)): assets.sha256(p.read_bytes()) for p in sorted(sources)}
            with tempfile.TemporaryDirectory(prefix='fly-verify-') as scratch:
                if args.command in ('host', 'device-live'):
                    model = HostModel(data, Path(scratch))
                if args.command == 'host':
                    report['result'] = host_checks(model, manifest, data, Path(scratch))
                else:
                    import serial
                    with serial.Serial(args.port, 921600, timeout=.2, write_timeout=60) as port, args.report.with_suffix('.jsonl').open('x') as log:
                        device = Device(port, log)
                        report['device_info'] = device_info(device, config, manifest, data)
                        report['target'] = 'ESP32-S3'
                        report['firmware_sha256'] = None
                        if args.command == 'device-graph':
                            report['result'] = device_graph(device, data, args.steps if args.steps is not None else 32)
                        elif args.command == 'device-escape':
                            report['result'] = device_escape(device, manifest, data)
                        elif args.command == 'device-live':
                            report['result'] = device_live(device, model, manifest, data,
                                                           args.steps if args.steps is not None else 60)
                        else:
                            report['result'] = dict(boot_selftest_passed=True, fresh_selftest_executed=False,
                                                    per_core_results_reported=True)
                        device.command('PLAY')
                        report['restart_confirmed'] = escape_restart(device)
                report['status'] = 'PASS'
        except Exception as error:
            report['error'] = f'{type(error).__name__}: {error}'
        finally:
            if model: model.close()
            report['elapsed_seconds'] = time.monotonic()-started
            json.dump(report, report_file, indent=2); report_file.write('\n')
        print(json.dumps(report.get('result', report) if report['status'] == 'PASS' else report, indent=2), flush=True)
        if report['status'] != 'PASS': raise SystemExit(1)


if __name__ == '__main__':
    main()
