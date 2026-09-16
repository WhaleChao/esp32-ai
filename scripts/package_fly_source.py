#!/usr/bin/env python3
"""Refresh the local Fly model package's source archive and file checksums.

No download, authentication, commit or publication. Large model assets must
already be present in --bundle. The snapshot records uncommitted source changes.
"""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]


def source_paths():
    paths = {'.gitignore', 'LICENSE', 'research/__init__.py',
             'scripts/fetch_model.sh', 'scripts/deploy.sh', 'scripts/package_fly_source.py',
             'tests/test_fetch_model.py', 'tests/test_deploy.py'}
    for directory in ('firmware/esp32_fly', 'research/fly', 'docs/fly-connectome'):
        for path in (ROOT/directory).rglob('*'):
            if path.is_file() and not {'generated','__pycache__'} & set(path.relative_to(ROOT/directory).parts):
                if path.suffix in ('.py','.h','.c','.S','.ino','.csv','.json','.txt','.md') or path.name == 'LICENSE-DATA':
                    paths.add(str(path.relative_to(ROOT)))
    for pattern in ('connectome*.h', 'lz4_block*.h', 'host_verify/connectome*', 'host_verify/lz4_block*',
                    'host_verify/escape_world_test.c'):
        for path in (ROOT/'runtime').glob(pattern):
            if path.is_file() and path.suffix in ('.c','.h'):
                paths.add(str(path.relative_to(ROOT)))
    return sorted(paths)


def package(bundle):
    revision = subprocess.check_output(['git','rev-parse','HEAD'], cwd=ROOT, text=True).strip()
    records, changes = {}, []
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w') as archive:
        for path in source_paths():
            file = ROOT/path
            if file.is_symlink():
                raise ValueError(f'Source symlink is not allowed: {path}')
            data = file.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            records[path] = digest
            base = subprocess.run(['git','show',revision+':'+path], cwd=ROOT, capture_output=True)
            if base.returncode or base.stdout != data:
                changes.append(dict(path=path, base_sha256=hashlib.sha256(base.stdout).hexdigest() if base.returncode==0 else None,
                                    sha256=digest))
            info = tarfile.TarInfo('esp32-ai-fly/'+path)
            info.size = len(data)
            info.mode = 0o755 if path.endswith('.sh') else 0o644
            archive.addfile(info, io.BytesIO(data))
    archive_name = 'esp32-ai-fly-source.tar.gz'
    (bundle/archive_name).write_bytes(gzip.compress(stream.getvalue(), mtime=0))
    source = dict(repository='https://github.com/slvDev/esp32-ai', revision=revision,
                  archive=archive_name, license='MIT', data_notices_license='CC-BY-4.0',
                  scope='Fly graph runtime, escape firmware, probe and circuit tools, deployment and verification tools, data notices; no model binaries',
                  snapshot_kind='working-tree snapshot' if changes else 'Git revision',
                  changes_from_revision=changes, paths=list(records), file_sha256=records)
    (bundle/'SOURCE_CODE.json').write_text(json.dumps(source, indent=2)+'\n')
    if (bundle/'metadata.json').is_file():
        meta = json.loads((bundle/'metadata.json').read_text())
        meta.update(source_revision=revision, source_snapshot_kind=source['snapshot_kind'],
                    source_archive_sha256=hashlib.sha256((bundle/archive_name).read_bytes()).hexdigest())
        (bundle/'metadata.json').write_text(json.dumps(meta, indent=2)+'\n')
    files = sorted(p for p in bundle.rglob('*') if p.is_file() and '.cache' not in p.relative_to(bundle).parts and p.name != 'SHA256SUMS')
    if any(p.is_symlink() for p in files):
        raise ValueError('Model package must contain regular files, not symlinks')
    (bundle/'SHA256SUMS').write_text(''.join(f'{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(bundle)}\n' for p in files))
    print(f'Local source snapshot: {len(records)} files; {len(changes)} changes relative to {revision}')
    print(f'Checksums: {len(files)} files. Nothing published.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', required=True, type=Path)
    args = parser.parse_args()
    if not args.bundle.is_dir():
        parser.error('--bundle must be an existing local model package')
    package(args.bundle.resolve())
