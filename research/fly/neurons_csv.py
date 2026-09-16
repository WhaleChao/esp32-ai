"""Add MaleCNS cell type, class and side to the model package's neurons.csv.

The probe selects neurons by these annotations. Needs Python 3.11+, pyarrow and
the MaleCNS v1.0 body annotation table pinned in docs/fly-connectome/source-data.json;
its SHA-256 is checked before use. Writes a new file and never overwrites one.

  python3 -m research.fly.neurons_csv --neurons neurons.csv \\
    --annotations body-annotations-male-cns-v1.0-minconf-0.5.feather \\
    --out artifacts/fly/neurons.csv
"""
import argparse
import csv
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INPUT = ('execution_index', 'canonical_index', 'body_id', 'superclass')
OUTPUT = ('execution_index', 'canonical_index', 'body_id', 'superclass', 'class', 'type', 'side')


def pinned_sha256(name):
    source = json.loads((ROOT/'docs/fly-connectome/source-data.json').read_text())
    for entry in source['source_files']:
        if entry['name'] == name:
            return entry['sha256']
    raise ValueError(f'{name} is not a pinned source file; keep the original MaleCNS file name')


def annotate(neurons, annotations, feather):
    with open(annotations, 'rb') as source:
        digest = hashlib.file_digest(source, 'sha256').hexdigest()
    if digest != pinned_sha256(annotations.name):
        raise ValueError(f'{annotations} does not match the pinned MaleCNS annotation table')
    table = feather.read_table(annotations, columns=['bodyId', 'type', 'class', 'somaSide', 'rootSide']).to_pydict()
    by_body = {body: i for i, body in enumerate(table['bodyId'])}
    with open(neurons, newline='') as source:
        reader = csv.DictReader(source)
        missing = [c for c in INPUT if c not in (reader.fieldnames or ())]
        if missing:
            raise ValueError(f'{neurons} lacks columns {missing}')
        rows = list(reader)
    output = []
    for row in rows:
        i = by_body.get(int(row['body_id']))
        if i is None:
            raise ValueError(f"body {row['body_id']} is missing from the annotation table")
        side = next((s for s in (table['somaSide'][i], table['rootSide'][i]) if s in ('L', 'R')), '')
        output.append({'execution_index': row['execution_index'], 'canonical_index': row['canonical_index'],
                       'body_id': row['body_id'], 'superclass': row['superclass'], 'class': table['class'][i] or '',
                       'type': table['type'][i] or '', 'side': side})
    output.sort(key=lambda r: int(r['execution_index']))
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--neurons', type=Path, required=True, help='neurons.csv with execution_index, canonical_index, body_id, superclass')
    parser.add_argument('--annotations', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    try:
        import pyarrow.feather as feather
    except ImportError:
        parser.error('pyarrow is required: python3 -m pip install pyarrow')
    if args.out.exists():
        parser.error(f'{args.out} exists; choose a new path')
    try:
        output = annotate(args.neurons, args.annotations, feather)
    except (OSError, KeyError, ValueError) as error:
        parser.error(str(error))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, 'x', newline='') as target:
        writer = csv.DictWriter(target, fieldnames=OUTPUT, lineterminator='\n')
        writer.writeheader()
        writer.writerows(output)
    print(f'Wrote {len(output)} neurons to {args.out}')


if __name__ == '__main__':
    main()
