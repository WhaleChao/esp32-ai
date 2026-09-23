"""scripts/fetch_model.sh: which downloads are accepted, which are refused, and
what is left on disk afterwards.

Fixtures are synthetic and offline. Each test builds a throwaway repository
holding only the script, rewrites its pinned hashes to describe files it
generates, and puts a fake `hf` on PATH that copies a prepared directory into
--local-dir. TMPDIR is redirected per test so staging can be inspected.

  uv run python -m unittest discover -s tests
"""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "fetch_model.sh"

FAKE_HF = """#!/usr/bin/env bash
# Stands in for the huggingface CLI: copies a prepared directory into whatever
# --local-dir was asked for, so the script under test does real file handling.
dest=""; prev=""
for a in "$@"; do
  [ "$prev" = "--local-dir" ] && dest=$a
  prev=$a
done
mkdir -p "$dest"
cp -R "$FAKE_HF_SRC"/. "$dest"/ 2>/dev/null || true
echo "fake hf: copied into $dest"
"""


def pin_line(name, blob):
    return f'      "{name} {hashlib.sha256(blob).hexdigest()} {len(blob)}"'


def rewrite_pins(script, model, files):
    """Replace one model's PINNED block so it describes the synthetic files."""
    lines = script.splitlines()
    start = next(i for i, l in enumerate(lines) if l.strip() == f"{model})")
    open_at = next(i for i in range(start, len(lines)) if lines[i].strip() == "PINNED=(")
    close_at = next(i for i in range(open_at, len(lines)) if lines[i].strip() == ")")
    body = [pin_line(n, b) for n, b in files.items() if n != "metadata.json"]
    return "\n".join(lines[:open_at + 1] + body + lines[close_at:]) + "\n"


def metadata_for(files):
    return {
        "files": {
            n: {"sha256": hashlib.sha256(b).hexdigest(), "bytes": len(b)}
            for n, b in files.items() if n != "metadata.json"
        }
    }


class FetchCase(unittest.TestCase):
    MODEL = "barista"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.root = self.dir / "repo"
        (self.root / "scripts").mkdir(parents=True)
        self.remote = self.dir / "remote"
        self.remote.mkdir()
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        self.tmpdir = self.dir / "tmp"
        self.tmpdir.mkdir()
        hf = self.bin / "hf"
        hf.write_text(FAKE_HF)
        hf.chmod(0o755)

    def publish(self, files, pins=None, metadata=None):
        """Write the fake remote, and pin the script to `pins` (default: files)."""
        full = dict(files)
        full["metadata.json"] = json.dumps(
            metadata if metadata is not None else metadata_for(files)).encode()
        for name, blob in full.items():
            (self.remote / name).write_bytes(blob)
        script = rewrite_pins(SCRIPT.read_text(), self.MODEL, pins or files)
        target = self.root / "scripts" / "fetch_model.sh"
        target.write_text(script)
        target.chmod(0o755)

    def run_fetch(self, *args, extra_env=None):
        env = dict(os.environ, **(extra_env or {}))
        env["PATH"] = f"{self.bin}:{env['PATH']}"
        env["FAKE_HF_SRC"] = str(self.remote)
        env["TMPDIR"] = str(self.tmpdir)
        return subprocess.run(
            ["bash", str(self.root / "scripts" / "fetch_model.sh"), *args],
            capture_output=True, text=True, env=env)

    def dest(self):
        return self.root / "artifacts" / self.MODEL

    def staging_dirs(self):
        return sorted(self.tmpdir.glob("esp32ai-fetch-*"))


class TestArguments(FetchCase):
    def setUp(self):
        super().setUp()
        self.publish({"model.bin": b"weights"})

    def test_missing_argument_exits_2(self):
        r = self.run_fetch()
        self.assertEqual(r.returncode, 2)
        self.assertIn("usage:", r.stderr)

    def test_unknown_model_exits_2(self):
        self.assertEqual(self.run_fetch("bogus").returncode, 2)

    def test_extra_arguments_exit_2(self):
        self.assertEqual(self.run_fetch("barista", "tinystories").returncode, 2)

    def test_a_rejected_argument_downloads_nothing(self):
        self.run_fetch("bogus")
        self.assertFalse(self.dest().exists())


class TestVerifiedDownload(FetchCase):
    def test_matching_files_are_installed(self):
        files = {"model.bin": b"weights", "tokenizer.json": b"{}"}
        self.publish(files)
        r = self.run_fetch(self.MODEL)
        self.assertEqual(r.returncode, 0, r.stderr)
        for name in (*files, "metadata.json"):
            with self.subTest(name=name):
                self.assertTrue((self.dest() / name).is_file())
        self.assertEqual((self.dest() / "model.bin").read_bytes(), b"weights")

    def test_it_points_at_the_deploy_step_rather_than_flashing(self):
        self.publish({"model.bin": b"weights"})
        r = self.run_fetch(self.MODEL)
        self.assertIn("scripts/deploy.sh barista", r.stdout)


class TestRefusals(FetchCase):
    def test_wrong_hash_is_refused(self):
        # Same length, different content: the size check passes, so only the
        # hash can reject this.
        self.publish({"model.bin": b"weights"}, pins={"model.bin": b"weightZ"})
        r = self.run_fetch(self.MODEL)
        self.assertEqual(r.returncode, 1)
        self.assertIn("sha256", r.stderr)
        self.assertNotIn("size", r.stderr.split("metadata")[0])

    def test_wrong_size_is_refused(self):
        # A size mismatch is reported as a size mismatch, not as a bad hash.
        self.publish({"model.bin": b"weights"}, pins={"model.bin": b"much longer weights"})
        r = self.run_fetch(self.MODEL)
        self.assertEqual(r.returncode, 1)
        self.assertIn("size", r.stderr)

    def test_missing_file_is_refused(self):
        # Pinned for two files, the remote serves one.
        self.publish({"model.bin": b"weights"},
                     pins={"model.bin": b"weights", "tokenizer.json": b"{}"})
        r = self.run_fetch(self.MODEL)
        self.assertEqual(r.returncode, 1)
        self.assertIn("MISSING", r.stderr)

    def test_missing_metadata_is_refused(self):
        files = {"model.bin": b"weights"}
        self.publish(files)
        (self.remote / "metadata.json").unlink()
        r = self.run_fetch(self.MODEL)
        self.assertEqual(r.returncode, 1)
        self.assertIn("metadata.json MISSING", r.stderr)

    def test_metadata_disagreeing_with_the_pins_is_refused(self):
        # The bytes verify against the pins, but the release's own record
        # describes something else.
        files = {"model.bin": b"weights"}
        wrong = {"files": {"model.bin": {"sha256": "0" * 64, "bytes": 7}}}
        self.publish(files, metadata=wrong)
        r = self.run_fetch(self.MODEL)
        self.assertEqual(r.returncode, 1)
        self.assertIn("disagrees", r.stderr)

    def test_metadata_omitting_a_file_is_refused(self):
        self.publish({"model.bin": b"weights"}, metadata={"files": {}})
        r = self.run_fetch(self.MODEL)
        self.assertEqual(r.returncode, 1)
        self.assertIn("does not describe", r.stderr)


class TestExistingArtifactsSurviveAFailure(FetchCase):
    def test_a_refused_download_changes_nothing(self):
        # A good install, then a failing fetch: the install must be unchanged.
        self.publish({"model.bin": b"good weights"})
        self.assertEqual(self.run_fetch(self.MODEL).returncode, 0)
        before = {p.name: p.read_bytes() for p in self.dest().iterdir()}

        self.publish({"model.bin": b"tampered"}, pins={"model.bin": b"good weights"})
        r = self.run_fetch(self.MODEL)
        self.assertEqual(r.returncode, 1)
        self.assertIn("was not modified", r.stderr)

        after = {p.name: p.read_bytes() for p in self.dest().iterdir()}
        self.assertEqual(before, after)
        self.assertEqual(after["model.bin"], b"good weights")


# Stands in for mv: counts calls and, on call $MV_FAIL_AT, either fails or sends
# the script $MV_SIGNAL before moving, the way an interrupt would land. On call
# $MV_INTERRUPT_AT it also sends INT and then moves anyway.
FAKE_MV = """#!/usr/bin/env bash
n=$(( $(cat "$MV_COUNT" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$MV_COUNT"
if [ "$n" = "${MV_INTERRUPT_AT:-0}" ]; then
  kill -INT "$PPID"
fi
if [ "$n" = "${MV_FAIL_AT:-0}" ]; then
  if [ -n "${MV_SIGNAL:-}" ]; then
    kill -"$MV_SIGNAL" "$PPID"
  else
    echo "mv: injected failure" >&2
    exit 1
  fi
fi
exec "$REAL_MV" "$@"
"""


class TestInstallIsAllOrNothing(FetchCase):
    """A failure while replacing an existing install puts every old file back."""
    OLD = {"model.bin": b"old weights", "tokenizer.json": b"old tokenizer"}
    NEW = {"model.bin": b"new weights!", "tokenizer.json": b"new tokenizer"}

    def setUp(self):
        super().setUp()
        self.publish(self.OLD)
        r = self.run_fetch(self.MODEL)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.before = self.snapshot()
        self.publish(self.NEW)

    def snapshot(self):
        return {p.name: p.read_bytes() for p in self.dest().iterdir()}

    def leftovers(self):
        return sorted(self.dest().glob(".esp32ai-install-*")) + self.staging_dirs()

    def run_with_mv(self, fail_at, signal=None, interrupt_at=None):
        mv = self.bin / "mv"
        mv.write_text(FAKE_MV)
        mv.chmod(0o755)
        count = self.dir / f"mv-count-{fail_at}-{signal}-{interrupt_at}"
        env = {"MV_COUNT": str(count), "MV_FAIL_AT": str(fail_at), "REAL_MV": shutil.which("mv")}
        if signal:
            env["MV_SIGNAL"] = signal
        if interrupt_at:
            env["MV_INTERRUPT_AT"] = str(interrupt_at)
        try:
            return self.run_fetch(self.MODEL, extra_env=env)
        finally:
            mv.unlink()

    def test_a_failed_rename_restores_every_old_file(self):
        # Three names (two assets and metadata.json), each moved aside and then
        # replaced: six renames, and a failure at each one must undo the rest.
        for fail_at in range(1, 7):
            with self.subTest(fail_at=fail_at):
                r = self.run_with_mv(fail_at)
                self.assertEqual(r.returncode, 1, r.stderr)
                self.assertIn("injected failure", r.stderr)
                self.assertIn("restored to its previous files", r.stderr)
                self.assertEqual(self.snapshot(), self.before)
                self.assertEqual(self.leftovers(), [])

    def test_an_interrupt_mid_install_restores_every_old_file(self):
        for signal, code in (("INT", 130), ("TERM", 143)):
            with self.subTest(signal=signal):
                # The signal lands during the fourth rename, with files of both
                # releases already in place.
                r = self.run_with_mv(4, signal)
                self.assertEqual(r.returncode, code, r.stderr)
                self.assertIn("restored to its previous files", r.stderr)
                self.assertEqual(self.snapshot(), self.before)
                self.assertEqual(self.leftovers(), [])

    def test_an_interrupt_during_the_rollback_does_not_undo_it(self):
        # Rename 4 (the new tokenizer.json) fails after model.bin was fully
        # replaced. The rollback's own renames are 5 (tokenizer.json back) and
        # 6 (model.bin back); Ctrl-C lands during each in turn. A second rollback
        # would delete the model.bin the first one had just put back.
        for interrupt_at in (5, 6):
            with self.subTest(interrupt_at=interrupt_at):
                r = self.run_with_mv(4, interrupt_at=interrupt_at)
                self.assertNotEqual(r.returncode, 0, r.stderr)
                self.assertEqual(self.snapshot(), self.before)
                self.assertIn("restored to its previous files", r.stderr)
                self.assertEqual(self.leftovers(), [])

    def test_a_second_rollback_is_harmless(self):
        # restore() taken from the script and run twice on a replaced file: once
        # as a repeated call, once with the once-only guard forced open, so the
        # per-step bookkeeping is tested on its own.
        script = (self.root / "scripts" / "fetch_model.sh").read_text()
        start = script.index("restore() {")
        body = script[start:script.index("\n}\n", start) + 3]
        for second in ("restore", "INSTALLING=1; restore"):
            with self.subTest(second=second):
                dest = Path(tempfile.mkdtemp(dir=self.dir))
                work = dest / ".esp32ai-install-AbC123"
                (work / "new").mkdir(parents=True)
                (work / "old").mkdir()
                (dest / "model.bin").write_bytes(b"new")
                (work / "old" / "model.bin").write_bytes(b"old")
                driver = (f"set -euo pipefail\n{body}\n"
                          f'DEST="{dest}"; WORK="{work}"; INSTALLING=1\n'
                          'STEPS=("aside model.bin" "new model.bin")\n'
                          f"restore; {second}\n")
                r = subprocess.run(["bash", "-c", driver], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertTrue((dest / "model.bin").is_file(), "the second rollback deleted the old file")
                self.assertEqual((dest / "model.bin").read_bytes(), b"old")
                self.assertIn("restored to its previous files", r.stderr)
                self.assertNotIn("could not be fully undone", r.stderr)

    def test_a_destination_directory_is_refused_before_anything_changes(self):
        (self.dest() / "tokenizer.json").unlink()
        (self.dest() / "tokenizer.json").mkdir()
        (self.dest() / "tokenizer.json" / "keep").write_bytes(b"x")
        r = self.run_fetch(self.MODEL)
        self.assertEqual(r.returncode, 1)
        self.assertIn("tokenizer.json is a directory", r.stderr)
        self.assertIn("was not modified", r.stderr)
        self.assertEqual((self.dest() / "model.bin").read_bytes(), b"old weights")
        self.assertEqual([p.name for p in (self.dest() / "tokenizer.json").iterdir()], ["keep"])
        self.assertEqual(self.leftovers(), [])

    def test_a_leftover_work_directory_is_reported_and_kept(self):
        # What a rollback that could not finish leaves behind.
        stale = self.dest() / ".esp32ai-install-AbC123"
        (stale / "old").mkdir(parents=True)
        (stale / "old" / "model.bin").write_bytes(b"only copy")
        for expected in (0, 1):
            if expected:
                self.publish(self.NEW, pins={"model.bin": b"other weights", "tokenizer.json": b"new tokenizer"})
            with self.subTest(returncode=expected):
                r = self.run_fetch(self.MODEL)
                self.assertEqual(r.returncode, expected, r.stderr)
                self.assertIn(f"warning: {Path('artifacts') / self.MODEL / stale.name} is left over", r.stderr)
                self.assertEqual((stale / "old" / "model.bin").read_bytes(), b"only copy")

    def test_a_successful_replacement_leaves_nothing_temporary(self):
        r = self.run_fetch(self.MODEL)
        self.assertEqual(r.returncode, 0, r.stderr)
        after = self.snapshot()
        self.assertEqual(sorted(after), ["metadata.json", "model.bin", "tokenizer.json"])
        self.assertEqual(after["model.bin"], b"new weights!")
        self.assertEqual(after["tokenizer.json"], b"new tokenizer")
        self.assertEqual(self.leftovers(), [])


class TestStagingCleanup(FetchCase):
    def test_a_successful_fetch_leaves_no_staging_directory(self):
        self.publish({"model.bin": b"weights"})
        self.assertEqual(self.run_fetch(self.MODEL).returncode, 0)
        self.assertEqual(self.staging_dirs(), [])

    def test_a_refused_fetch_leaves_no_staging_directory(self):
        self.publish({"model.bin": b"weights"}, pins={"model.bin": b"weightZ"})
        self.assertEqual(self.run_fetch(self.MODEL).returncode, 1)
        self.assertEqual(self.staging_dirs(), [])

    def test_a_normal_run_trips_neither_guard(self):
        self.publish({"model.bin": b"weights"})
        r = self.run_fetch(self.MODEL)
        self.assertNotIn("not removing unexpected staging path", r.stderr)
        self.assertNotIn("could not remove", r.stderr)

    def test_an_unexpected_staging_path_is_left_alone(self):
        # Removal is keyed on the parent and the name. A directory that is not
        # the one mktemp was asked for must survive, whatever else happens.
        self.publish({"model.bin": b"weights"})
        unexpected = self.dir / "not-a-staging-dir"
        fake_mktemp = self.bin / "mktemp"
        fake_mktemp.write_text(
            "#!/usr/bin/env bash\n"
            f'mkdir -p "{unexpected}"\n'
            f'echo "{unexpected}"\n')
        fake_mktemp.chmod(0o755)
        r = self.run_fetch(self.MODEL)
        self.assertIn("not removing unexpected staging path", r.stderr)
        self.assertTrue(unexpected.is_dir(), "the guard removed a path it did not create")


class TestPinnedReleaseValues(unittest.TestCase):
    """Every pin must be a name, a 64-character digest, and a positive size."""

    def test_every_pin_is_a_name_a_sha256_and_a_size(self):
        found = 0
        for line in SCRIPT.read_text().splitlines():
            line = line.strip()
            if not (line.startswith('"') and line.endswith('"')):
                continue
            parts = line.strip('"').split()
            if len(parts) != 3:
                continue
            name, sha, size = parts
            with self.subTest(name=name):
                self.assertRegex(sha, r"^[0-9a-f]{64}$")
                self.assertTrue(size.isdigit() and int(size) > 0)
                found += 1
        self.assertGreaterEqual(found, 12)   # 2 tinystories + 4 barista + 6 fly


class TestFly(FetchCase):
    """fly goes through the same pinned download, one argument and all."""
    MODEL = "fly"

    def test_the_bundle_is_installed_under_artifacts_fly(self):
        files = {"connectome.fcl": b"graph", "escape-circuit.json": b"{}",
                 "neurons.csv": b"execution_index\n"}
        self.publish(files)
        r = self.run_fetch("fly")
        self.assertEqual(r.returncode, 0, r.stderr)
        for name in (*files, "metadata.json"):
            with self.subTest(name=name):
                self.assertTrue((self.root / "artifacts" / "fly" / name).is_file())
        self.assertIn("scripts/deploy.sh fly", r.stdout)

    def test_a_wrong_graph_is_refused(self):
        self.publish({"connectome.fcl": b"graph"}, pins={"connectome.fcl": b"grapH"})
        r = self.run_fetch("fly")
        self.assertEqual(r.returncode, 1)
        self.assertFalse(self.dest().exists())

    def test_options_are_not_accepted(self):
        self.publish({"connectome.fcl": b"graph"})
        self.assertEqual(self.run_fetch("fly", "--from", "somewhere").returncode, 2)


if __name__ == "__main__":
    unittest.main()
