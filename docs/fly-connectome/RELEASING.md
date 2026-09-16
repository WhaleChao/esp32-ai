# Preparing a connectome release

Keep the code checkout and model package separate. Git contains source,
configuration, small source/selection records and data notices. The model
package contains the compressed graph, the escape circuit, both gold files, the
annotated neuron table and scoped validation reports. `HF_models/`, `artifacts/`,
`data/`, `runs/` and generated firmware inputs are ignored by Git.

## Code and model checks

1. Inspect `git status --short` and the intended diff. Include new tools, tests
   and documentation; exclude personal notes, private corpora, generated headers
   and model binaries. `git ls-files` describes the committed file inventory;
   new untracked source needs to be added when making the release commits.
2. Use a clean checkout with no local generated files. Put the release files in
   `artifacts/fly/`: `scripts/fetch_model.sh fly` once published, or a copy of
   the prepared package before that. The pins in `fetch_model.sh` and the
   checked-in manifest must describe exactly those files.
3. Run `scripts/deploy.sh fly` on the target N16R8 board with the display wiring
   in the firmware guide. It checks the bundle, runs full host gold, compiles and
   flashes. A passing host gate does not execute ESP32 SIMD.
4. Run all device checks in [VERIFICATION.md](../../firmware/esp32_fly/VERIFICATION.md):
   `device-simd`, `device-graph`, `device-escape` and `device-live`. Watch the
   display too: the fly walks, the spider approaches and the fly jumps away from
   it. Keep the device reports with the build and model identities.
5. Re-export the circuit from the released bundle with
   [the research tools](../../research/fly/README.md) and check that the files
   come out byte-identical to the published ones, so anyone can reproduce them.
6. Review source-data attribution, retained/excluded classes, contact/edge counts,
   license notices and limits of the rate dynamics. Keep host and device results
   distinguishable in the model package.

## Offline tests

The bundle tools and the fetch/deploy tests use Python's standard library. The
research tests need `research/fly/requirements.txt` installed in `.venv`.
Commands run from the repo root:

```sh
python3 -m unittest discover -s firmware/esp32_fly/tools -p 'test_*.py'
python3 -m unittest discover -s tests -p 'test_fetch_model.py'
python3 -m unittest discover -s tests -p 'test_deploy.py'
.venv/bin/python -m unittest research.fly.test_circuit
FLY_BUNDLE=artifacts/fly \
  .venv/bin/python -m unittest research.fly.test_probe
```

`test_circuit` runs on synthetic data. `test_probe` adds a check against the
real graph when `FLY_BUNDLE` points at a bundle, and skips it otherwise.

The C runtime tests in `runtime/host_verify/` carry their own build commands.
For the repository's LLM tests, install its Python project with `uv sync` and use
`uv run python -m unittest discover -s tests`; fly-only work does not require
that Torch environment.

## Prepare the Hugging Face directory

After collecting the intended assets, model card and validation reports locally:

```sh
python3 scripts/package_fly_source.py --bundle HF_models/esp32-ai-fly
```

This refreshes the source archive, records every archived file hash, identifies
changes relative to Git HEAD and rebuilds `SHA256SUMS`. It performs no network
operations. Uncommitted changes are labelled as a working-tree snapshot; they
must not be described as the exact contents of a commit.

Commit the reviewed source separately, then rerun packaging so its recorded
revision matches that commit. `metadata.json` must list every pinned file with
its `sha256` and `bytes`, because `fetch_model.sh` cross-checks it.

## Publication checks

Publishing is a separate operation. After publication, run
`scripts/fetch_model.sh fly` against the public release without credentials,
then `scripts/deploy.sh fly`. Do not change the pinned hashes just to make a
failed download pass.
