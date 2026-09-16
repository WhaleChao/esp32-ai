#!/usr/bin/env python3
"""Verify a fly model bundle and generate local firmware inputs. Never flash.

The checked-in manifest is authoritative by default. --manifest explicitly
selects another trusted manifest, for example after exporting a new circuit.
A bundle's own manifest must agree with it; it cannot approve its own hashes.
"""

import argparse
import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import struct
import tempfile

SKETCH = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = SKETCH / "model_bundle.json"
SCHEMA = 2
CONTRACT = "fly-escape-v1"
ROLES = {"graph", "graph_gold", "circuit", "escape_gold", "neurons"}
SIDES = ("L", "R")
KINDS = ("inputs", "outputs")
MAX_PER_SIDE = {"inputs": 1024, "outputs": 64}
NEURON_COLUMNS = ("execution_index", "canonical_index", "body_id", "superclass", "class", "type", "side")
ESCAPE_HEADER = "<4s5I2f"
# Files earlier firmware versions generated. A stale copy must never be compiled.
OBSOLETE_GENERATED = ("pong-readout.bin", "pong_readout_path.h")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def fingerprint(data):
    value = 2166136261
    for byte in data:
        value = ((value ^ byte) * 16777619) & 0xFFFFFFFF
    return value


def is_int(value):
    return type(value) is int


def is_number(value):
    return type(value) in (int, float) and math.isfinite(value)


def is_hash(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def float32(value):
    return struct.unpack("<f", struct.pack("<f", value))[0]


def mapping(value, what):
    require(isinstance(value, dict), f"{what} must be a JSON object")
    return value


def check_tolerance(record, what):
    for key in ("atol", "rtol"):
        require(is_number(record[key]) and 0 <= record[key] <= 0.001, f"Invalid {what} gold tolerance")


def check_manifest(manifest):
    """Validate a parsed manifest's structure and bindings; returns it unchanged."""
    mapping(manifest, "Manifest")
    require(is_int(manifest["schema_version"]) and manifest["schema_version"] == SCHEMA, "Unsupported manifest schema")
    require(manifest["contract"] == CONTRACT, "Unsupported model contract")
    require(isinstance(manifest["bundle_id"], str) and manifest["bundle_id"], "Missing bundle ID")
    files = mapping(manifest["files"], "files")
    require(set(files) == ROLES, "Manifest must describe graph, graph_gold, circuit, escape_gold and neurons")
    names = set()
    for role, entry in files.items():
        mapping(entry, f"files.{role}")
        name = entry["name"]
        require(isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name),
                f"{role}: file name must be a plain basename")
        require(name not in names and name != "model_bundle.json", "Duplicate or reserved file name")
        names.add(name)
        require(is_int(entry["bytes"]) and 0 < entry["bytes"] <= 256 * 1024 * 1024, f"{role}: invalid byte size")
        require(is_hash(entry["sha256"]), f"{role}: invalid SHA-256")
    graph, circuit, gold = (mapping(manifest[key], key) for key in ("graph", "circuit", "gold"))
    for key in ("graph", "escape"):
        mapping(gold[key], f"gold.{key}")
    require(is_int(graph["nodes"]) and graph["nodes"] == 48311 and is_int(graph["block_rows"]) and graph["block_rows"] == 32,
            "This firmware requires 48311 nodes and 32-row blocks")
    require(is_int(graph["edges"]) and 0 < graph["edges"] <= 0xFFFFFFFF, "Invalid edge count")
    require(is_hash(graph["order_sha256"]), "Invalid node-order SHA-256")
    graph_sha = files["graph"]["sha256"]
    require(circuit["graph_sha256"] == gold["graph"]["graph_sha256"] == gold["escape"]["graph_sha256"] == graph_sha,
            "Graph/circuit/gold binding mismatch")
    require(circuit["order_sha256"] == gold["graph"]["order_sha256"] == graph["order_sha256"],
            "Node-order binding mismatch")
    require(gold["escape"]["circuit_sha256"] == files["circuit"]["sha256"], "Circuit/escape gold binding mismatch")
    for kind in KINDS:
        counts = mapping(circuit[kind], f"circuit.{kind}")
        for side in SIDES:
            require(is_int(counts[side]) and 0 < counts[side] <= MAX_PER_SIDE[kind],
                    f"Invalid circuit {kind} count for side {side}")
    for record, keys in ((gold["graph"], ("cases", "steps")), (gold["escape"], ("cases", "steps", "outputs"))):
        for key in keys:
            require(is_int(record[key]) and 0 < record[key] <= 100000, "Invalid gold dimensions")
    require(gold["escape"]["outputs"] == circuit["outputs"]["L"] + circuit["outputs"]["R"],
            "Escape gold outputs disagree with the circuit")
    check_tolerance(gold["graph"], "graph")
    check_tolerance(gold["escape"], "escape")
    return manifest


def load_manifest(path):
    return check_manifest(json.loads(path.read_text()))


def verified_bytes(path, entry):
    require(path.stat().st_size == entry["bytes"], f"{path}: byte size mismatch")
    data = path.read_bytes()
    require(len(data) == entry["bytes"] and sha256(data) == entry["sha256"], f"{path}: SHA-256 mismatch")
    return data


def graph_config(data, expected):
    require(20 <= len(data) <= 0xDE0000, "Graph does not fit flymodel partition")
    magic, version, nodes, edges, block_rows = struct.unpack_from("<4s4I", data)
    require((magic, version) == (b"FCL1", 1), "Unsupported graph format")
    require((nodes, edges, block_rows) == (expected["nodes"], expected["edges"], expected["block_rows"]),
            "Graph header disagrees with manifest")
    offset = 20 + nodes * 4
    require(offset <= len(data), "Truncated node order")
    order_bytes = data[20:offset]
    require(sha256(order_bytes) == expected["order_sha256"], "Node-order SHA-256 mismatch")
    order = struct.unpack(f"<{nodes}I", order_bytes)
    require(set(order) == set(range(nodes)), "Node order is not a permutation")
    rows_seen = edges_seen = max_raw = max_compressed = blocks = 0
    while rows_seen < nodes:
        require(offset + 16 <= len(data), "Truncated block header")
        rows, block_edges, raw, compressed = struct.unpack_from("<4I", data, offset)
        offset += 16
        require(rows == min(block_rows, nodes - rows_seen), "Unexpected block row count")
        require(block_edges <= rows * 65535, "Block exceeds runtime degree limit")
        require(rows + 2 * block_edges <= raw <= 5 * rows + 6 * block_edges,
                "Decoded block size is inconsistent with varint limits")
        require(compressed > 0 and offset + compressed <= len(data), "Truncated block payload")
        offset += compressed
        rows_seen += rows
        edges_seen += block_edges
        max_raw = max(max_raw, raw)
        max_compressed = max(max_compressed, compressed)
        blocks += 1
    require((offset, edges_seen) == (len(data), edges), "Graph totals or trailing data mismatch")
    return {
        "CONNECTOME_N": nodes, "CONNECTOME_E": edges, "CONNECTOME_BLOCK_ROWS": block_rows,
        "CONNECTOME_BYTES": len(data), "CONNECTOME_RAW": max_raw,
        "CONNECTOME_COMPRESSED": max_compressed, "CONNECTOME_HASH": fingerprint(data),
    }, blocks


def validate_circuit(data, manifest):
    """Parse the circuit map and check it against the manifest; returns the parsed map."""
    try:
        circuit = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"Circuit is not valid JSON: {error}") from None
    require(isinstance(circuit, dict) and circuit.get("schema_version") == 1 and is_int(circuit.get("schema_version"))
            and circuit.get("contract") == CONTRACT, "Unsupported circuit schema or contract")
    require(circuit.get("graph_sha256") == manifest["files"]["graph"]["sha256"]
            and circuit.get("order_sha256") == manifest["graph"]["order_sha256"], "Circuit/graph binding mismatch")
    amplitude, threshold = circuit.get("amplitude"), circuit.get("threshold")
    # The firmware uses float32 values; the rounded value must stay in range too.
    require(is_number(amplitude) and 0 < amplitude <= 1 and 0 < float32(amplitude) <= 1,
            "Circuit amplitude must be in (0, 1] as float32")
    require(is_number(threshold) and 0 < threshold < 1 and 0 < float32(threshold) < 1,
            "Circuit threshold must be in (0, 1) as float32")
    nodes, seen = manifest["graph"]["nodes"], set()
    for kind in KINDS:
        group = circuit.get(kind)
        require(isinstance(group, dict), f"Circuit lacks {kind}")
        for side in SIDES:
            entries = group.get(side)
            require(isinstance(entries, list) and len(entries) == manifest["circuit"][kind][side],
                    f"Circuit {kind} {side} count disagrees with the manifest")
            for entry in entries:
                require(isinstance(entry, dict) and is_int(entry.get("index")) and 0 <= entry["index"] < nodes
                        and is_int(entry.get("body_id")) and entry["body_id"] > 0
                        and isinstance(entry.get("type"), str) and entry["type"],
                        f"Invalid circuit {kind} {side} entry")
                require(entry["index"] not in seen, f"Circuit neuron {entry['index']} is listed twice")
                seen.add(entry["index"])
    return circuit


def validate_neurons(data, manifest, circuit, graph):
    """neurons.csv must follow the graph's node order and agree with every circuit neuron."""
    try:
        text = data.decode("utf-8")
        require("\0" not in text, "neurons.csv contains NUL bytes")
        reader = csv.DictReader(io.StringIO(text, newline=""))
        require(tuple(reader.fieldnames or ()) == NEURON_COLUMNS, f"neurons.csv columns must be {', '.join(NEURON_COLUMNS)}")
        nodes = manifest["graph"]["nodes"]
        order = struct.unpack_from(f"<{nodes}I", graph, 20)
        rows = []
        for row in reader:
            index = len(rows)
            require(None not in row and None not in row.values(), f"neurons.csv row {index + 1} must have exactly 7 fields")
            require(index < nodes, "neurons.csv lists more neurons than the graph")
            require(row["execution_index"] == str(index), "neurons.csv must list execution indices in order")
            require(row["canonical_index"] == str(order[index]), "neurons.csv does not follow the graph node order")
            rows.append(row)
    except (UnicodeDecodeError, csv.Error) as error:
        raise ValueError(f"neurons.csv is not valid UTF-8 CSV: {error}") from None
    require(len(rows) == nodes, "neurons.csv must list every graph neuron")
    for kind in KINDS:
        for side in SIDES:
            for entry in circuit[kind][side]:
                row = rows[entry["index"]]
                require((row["body_id"], row["type"], row["side"]) == (str(entry["body_id"]), entry["type"], side),
                        f"Circuit {kind} {side} neuron {entry['index']} disagrees with neurons.csv")


def require_finite(data, offset, message):
    # Iterate without unpacking millions of floats into a Python tuple.
    require(all(math.isfinite(x[0]) for x in struct.iter_unpack("<f", memoryview(data)[offset:])), message)


def validate_graph_gold(data, manifest):
    require(len(data) >= 28, "Truncated graph gold")
    magic, version, nodes, cases, steps, atol, rtol = struct.unpack_from("<4s4I2f", data)
    expected = manifest["gold"]["graph"]
    require((magic, version, nodes, cases, steps) ==
            (b"FGF1", 1, manifest["graph"]["nodes"], expected["cases"], expected["steps"]),
            "Graph gold header mismatch")
    require(struct.pack("<2f", atol, rtol) == struct.pack("<2f", expected["atol"], expected["rtol"]),
            "Graph gold tolerance mismatch")
    require(len(data) == 28 + cases * (1 + 2*steps) * nodes * 4, "Graph gold size mismatch")
    require_finite(data, 28, "Nonfinite graph gold values")


def validate_escape_gold(data, manifest):
    header = struct.calcsize(ESCAPE_HEADER)
    require(len(data) >= header, "Truncated escape gold")
    magic, version, nodes, cases, steps, outputs, atol, rtol = struct.unpack_from(ESCAPE_HEADER, data)
    expected = manifest["gold"]["escape"]
    require((magic, version, nodes, cases, steps, outputs) ==
            (b"FEG1", 1, manifest["graph"]["nodes"], expected["cases"], expected["steps"], expected["outputs"]),
            "Escape gold header mismatch")
    require(struct.pack("<2f", atol, rtol) == struct.pack("<2f", expected["atol"], expected["rtol"]),
            "Escape gold tolerance mismatch")
    size = steps * (2 + outputs) * 4
    require(len(data) == header + cases * size, "Escape gold size mismatch")
    require_finite(data, header, "Nonfinite escape gold values")
    for case in range(cases):
        looms = struct.unpack_from(f"<{2*steps}f", data, header + case*size)
        require(all(0 <= value <= 1 for value in looms), "Escape gold loom levels must be in [0, 1]")


def read_bundle(bundle, manifest_path=DEFAULT_MANIFEST):
    """Verify every file of a bundle against the trusted manifest; returns manifest, data, config, circuit, blocks."""
    manifest = load_manifest(manifest_path)
    require(load_manifest(bundle / "model_bundle.json") == manifest,
            "Bundle manifest disagrees with the trusted manifest")
    data = {role: verified_bytes(bundle / entry["name"], entry) for role, entry in manifest["files"].items()}
    config, blocks = graph_config(data["graph"], manifest["graph"])
    circuit = validate_circuit(data["circuit"], manifest)
    validate_neurons(data["neurons"], manifest, circuit, data["graph"])
    validate_graph_gold(data["graph_gold"], manifest)
    validate_escape_gold(data["escape_gold"], manifest)
    return manifest, data, config, circuit, blocks


def c_float(value):
    return f"{float32(value)!r}f"


def escape_header(circuit, circuit_bytes):
    lines = ["// Generated from a verified bundle. Do not commit.", "#pragma once", "#include <stdint.h>", "",
             "#define ESCAPE_CONTRACT 1u",
             f"#define ESCAPE_AMPLITUDE {c_float(circuit['amplitude'])}",
             f"#define ESCAPE_THRESHOLD {c_float(circuit['threshold'])}",
             f"#define ESCAPE_CIRCUIT_BYTES {len(circuit_bytes)}u",
             f"#define ESCAPE_CIRCUIT_HASH 0x{fingerprint(circuit_bytes):08x}u", ""]
    for kind in KINDS:
        for side in SIDES:
            macro = f"ESCAPE_{kind.upper()}_{side}"
            indices = ", ".join(str(entry["index"]) for entry in circuit[kind][side])
            lines += [f"#define {macro} {len(circuit[kind][side])}u",
                      f"static const uint32_t escape_{kind}_{'left' if side == 'L' else 'right'}[{macro}] = {{{indices}}};"]
    return ("\n".join(lines) + "\n").encode()


def prepare(bundle, *, manifest_path=DEFAULT_MANIFEST, output=None):
    manifest, data, config, circuit, blocks = read_bundle(bundle, manifest_path)
    generated = (output if output is not None else SKETCH / "generated").resolve()
    receipt = {
        "schema_version": 1, "bundle_id": manifest["bundle_id"], "contract": CONTRACT,
        "manifest_sha256": sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()),
        "files": {role: {**entry, "path": str((bundle / entry["name"]).resolve())}
                  for role, entry in manifest["files"].items()},
        "numerical_gold_executed": False, "graph_blocks": blocks, "config": config,
        "escape": {"amplitude": circuit["amplitude"], "threshold": circuit["threshold"],
                   "inputs": dict(manifest["circuit"]["inputs"]), "outputs": dict(manifest["circuit"]["outputs"]),
                   "circuit_fnv1a": f"{fingerprint(data['circuit']):08x}"},
    }
    outputs = {
        "connectome_build.h": ("// Generated from a verified bundle. Do not commit.\n#pragma once\n"
                               + "".join(f"#define {key} {value}u\n" for key, value in config.items())).encode(),
        "escape_circuit.h": escape_header(circuit, data["circuit"]),
        "assets.json": (json.dumps(receipt, indent=2) + "\n").encode(),
    }
    # All validation precedes writes. Stage complete files, then replace each
    # destination atomically. This is not a concurrent-build transaction.
    generated.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".prepare-", dir=generated) as temporary:
        staged = Path(temporary)
        for name, content in outputs.items():
            (staged / name).write_bytes(content)
        for name in outputs:
            os.replace(staged / name, generated / name)
    for name in OBSOLETE_GENERATED:
        (generated / name).unlink(missing_ok=True)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True, help="Directory containing model_bundle.json and all five files")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST, help="Explicitly trusted manifest")
    parser.add_argument("--output", type=Path, help="Generated directory (defaults to this sketch's generated/)")
    args = parser.parse_args()
    try:
        receipt = prepare(args.bundle, manifest_path=args.manifest, output=args.output)
    except (OSError, ValueError, KeyError, TypeError, struct.error) as error:
        parser.exit(1, f"Asset preparation failed: {error}\n")
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
