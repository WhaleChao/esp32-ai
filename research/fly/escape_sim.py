"""Simulate the escape demo on the host with the real graph and the firmware's world.

Runs the board's loop: at the start of each neural cycle the world is sampled,
the spider's loom levels drive the circuit's looming inputs, the graph takes one
step and the escape neurons' mean activity per side decides an escape. The
decision is applied when the cycle ends, while the world keeps moving at 120 Hz
in between, as on the board. Nothing is trained.

  python3 -m research.fly.escape_sim --bundle artifacts/fly --minutes 20 --seeds 61000 61001
"""
import argparse
from contextlib import contextmanager
import ctypes as ct
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tempfile

import numpy as np

from firmware.esp32_fly.tools import prepare_assets as assets
from firmware.esp32_fly.tools.verify import HostModel, check, escape_stimulus, load_bundle

ROOT = Path(__file__).resolve().parents[2]
WORLD = ROOT/'firmware/esp32_fly/escape_world.h'
TUNABLE = ('FLY_WALK_SPEED', 'FLY_JUMP_SPEED', 'FLY_JUMP_SECONDS', 'SPIDER_SPEED', 'SPIDER_RETREAT_SPEED',
           'LOOM_FAR_DISTANCE', 'LOOM_NEAR_DISTANCE', 'ESCAPE_REFRACTORY_SECONDS')


def world_defaults():
    found = dict(re.findall(r'^#define (\w+) ([0-9.]+)', WORLD.read_text(), re.MULTILINE))
    return {name: float(found[name]) for name in TUNABLE}


def parse_define(text):
    name, _, value = text.partition('=')
    check(name in TUNABLE, f'--define name must be one of {", ".join(TUNABLE)}')
    try:
        number = float(value)
    except ValueError:
        number = float('nan')
    check(number > 0 and number != float('inf'), f'--define {name} needs a positive number')
    return name, number


@contextmanager
def world_library(directory, defines=()):
    compiler = shutil.which('cc')
    check(compiler, 'A C compiler (cc) is required')
    library = directory/'world_bridge.so'
    flags = [f'-D{name}={value!r}' for name, value in defines]
    subprocess.run([compiler, '-std=c11', '-O3', '-ffp-contract=off', '-Wall', '-Wextra', '-Werror', '-shared',
                    '-fPIC', *flags, str(ROOT/'research/fly/world_bridge.c'), '-lm', '-o', str(library)], check=True)
    lib = ct.CDLL(str(library))
    lib.escape_world_create.argtypes = [ct.c_uint32]
    lib.escape_world_create.restype = ct.c_void_p
    lib.escape_world_destroy.argtypes = [ct.c_void_p]
    lib.escape_world_advance.argtypes = [ct.c_void_p, ct.c_uint32]
    lib.escape_world_looms.argtypes = [ct.c_void_p, ct.POINTER(ct.c_float)]
    lib.escape_world_decide.argtypes = [ct.c_void_p, ct.c_float, ct.c_float, ct.c_float]
    lib.escape_world_decide.restype = ct.c_int
    lib.escape_world_stats.argtypes = [ct.c_void_p, ct.POINTER(ct.c_uint32)]
    lib.escape_world_positions.argtypes = [ct.c_void_p, ct.POINTER(ct.c_double)]
    yield lib


def drives(state, circuit):
    """float32 mean per side, summed in listed order like the firmware."""
    result = []
    for side in assets.SIDES:
        total = np.float32(0)
        for entry in circuit['outputs'][side]:
            total = np.float32(total + np.float32(struct.unpack_from('<f', state, entry['index']*4)[0]))
        result.append(float(total / np.float32(len(circuit['outputs'][side]))))
    return result


def session(model, lib, circuit, seed, minutes, cycle_seconds, mode):
    """One continuous session; returns counters and one record per neural cycle."""
    nodes = 48311
    world = lib.escape_world_create(seed)
    check(world, 'World allocation failed')
    try:
        model.reset()
        ticks_per_cycle = round(cycle_seconds * 120)
        cycles = round(minutes * 60 / cycle_seconds)
        records = []
        stats, positions = (ct.c_uint32 * 5)(), (ct.c_double * 5)()
        for step in range(cycles):
            looms = (ct.c_float * 2)()
            lib.escape_world_looms(world, looms)
            lib.escape_world_stats(world, stats)
            lib.escape_world_positions(world, positions)
            sample = dict(spider_state=stats[3], fly_state=stats[4], caught=stats[1],
                          distance=((positions[3] - positions[0])**2 + (positions[4] - positions[1])**2) ** 0.5)
            left, right = float(looms[0]), float(looms[1])
            state = model.step(escape_stimulus(nodes, circuit, left, right), mode)
            drive = drives(state, circuit)
            lib.escape_world_advance(world, ticks_per_cycle)
            side = lib.escape_world_decide(world, drive[0], drive[1], circuit['threshold'])
            records.append(dict(step=step + 1, looms=[left, right], drives=drive, escape_side=side, sample=sample))
        lib.escape_world_stats(world, stats)
        return dict(seed=seed, escapes=stats[0], caught=stats[1], encounters=stats[2], cycles=cycles,
                    escape_decisions=sum(r['escape_side'] >= 0 for r in records)), records
    finally:
        lib.escape_world_destroy(world)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--bundle', type=Path, default=Path('artifacts/fly'))
    parser.add_argument('--manifest', type=Path, default=assets.DEFAULT_MANIFEST)
    parser.add_argument('--minutes', type=float, default=20)
    parser.add_argument('--seeds', type=int, nargs='+', default=[61000])
    parser.add_argument('--cycle-seconds', type=float, default=1.7, help='Neural cycle duration measured on the board')
    parser.add_argument('--mode', type=int, default=2, choices=range(5))
    parser.add_argument('--define', action='append', default=[], metavar='NAME=VALUE',
                        help=f'Override a world constant for this run: {", ".join(TUNABLE)}')
    parser.add_argument('--out', type=Path, help='Optional new JSON path with per-cycle records')
    args = parser.parse_args()
    try:
        if args.minutes <= 0 or args.cycle_seconds <= 0:
            raise ValueError('--minutes and --cycle-seconds must be positive')
        if any(not 0 <= seed <= 0xFFFFFFFF for seed in args.seeds):
            raise ValueError('Seeds must be unsigned 32-bit integers')
        if args.out is not None and args.out.exists():
            raise ValueError(f'{args.out} exists; choose a new path')
        defines = [parse_define(text) for text in args.define]
        constants = dict(world_defaults(), **dict(defines))
        if constants['LOOM_NEAR_DISTANCE'] >= constants['LOOM_FAR_DISTANCE']:
            raise ValueError('LOOM_NEAR_DISTANCE must be smaller than LOOM_FAR_DISTANCE')
        manifest, data, _ = load_bundle(args.bundle, args.manifest)
        circuit = assets.validate_circuit(data['circuit'], manifest)
    except (OSError, KeyError, ValueError) as error:
        parser.error(str(error))
    results = []
    with tempfile.TemporaryDirectory(prefix='fly-escape-sim-') as temporary, \
            world_library(Path(temporary), defines) as lib:
        model = HostModel(dict(graph=data['graph']), Path(temporary))
        try:
            for seed in args.seeds:
                summary, records = session(model, lib, circuit, seed, args.minutes, args.cycle_seconds, args.mode)
                results.append(dict(summary, records=records))
                print(json.dumps(summary), flush=True)
        finally:
            model.close()
    total = {key: sum(r[key] for r in results) for key in ('escapes', 'caught', 'encounters')}
    settings = dict(minutes_per_seed=args.minutes, cycle_seconds=args.cycle_seconds, mode=args.mode,
                    world_constants=constants, threshold=circuit['threshold'], bundle_id=manifest['bundle_id'])
    print(json.dumps(dict(total=total, **settings)))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with open(args.out, 'x') as output:
            json.dump(dict(total=total, settings=settings, sessions=results), output)
            output.write('\n')


if __name__ == '__main__':
    main()
