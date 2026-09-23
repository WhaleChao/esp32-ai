#!/usr/bin/env bash
# Download a released model's inference assets and verify them against the
# hashes recorded here, then install them under artifacts/<model>/.
#
# This script never compiles, flashes, or touches the board. Fetching and device
# mutation are kept apart so that deploy.sh never reaches the network and a
# download can be repeated or audited without risking what is installed.
# pipefail: the download is piped, and `set -e` alone does not see a failure on
# the left of a pipe.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

usage() {
  cat >&2 <<'EOF'
usage: scripts/fetch_model.sh <tinystories|barista|fly>

Downloads that model's released files. The inference assets are checked against
a SHA-256 and byte size pinned in this script; metadata.json carries no pinned
hash, so it is parsed and cross-checked against those same pins instead. Only
then is anything installed into artifacts/<model>/.

Nothing is installed unless every check passes, so a failed or partial download
leaves whatever is already in artifacts/ untouched. The install itself is all or
nothing: if it fails or is interrupted part way, the previous files are restored.

Then flash with:
  scripts/deploy.sh <tinystories|barista|fly>
EOF
  exit 2
}

# Argument first, before any download, verification, or install.
[ $# -eq 1 ] || usage
MODEL_KIND=$1

# --- released files, pinned ---------------------------------------------------
# One line per file: name sha256 bytes. These are the values published with the
# release; a mismatch means the download is not the release, so it is refused
# rather than reported.
case "$MODEL_KIND" in
  tinystories)
    REPO=slvDev/esp32-ai-tinystories
    PINNED=(
      "model.bin 1d8326c05c383ccfa615f5455575802817cb453dbc7ab28875d41a9dbb45477e 14912348"
      "tokenizer.json 4e28163669f2249af31a528a54fc25064dcbd0a34edbfa7bedb16d2d600ec7ae 1788896"
    )
    ;;
  barista)
    REPO=slvDev/esp32-ai-barista
    PINNED=(
      "model.bin 1359a1cb74de4143d630c2c192990de814cd47255bcdfa9cc135f07ef0a39fc4 4600186"
      "tokenizer.json 0ad085811c949f35c5f5f15b555f2ff2d46ec1ec94a1650552416d06aaa19ee2 491735"
      "vocab.json 5a16d6224abf03265d69ebcccf121c8f8d2c222bfedd8274acbc0bbbe13e4eb7 50494"
      "layout.json 15036c5ee2b23b9b35404ef6422cb788bbede8cf2c269bae35d4dbf9a48a0b90 5142"
    )
    ;;
  fly)
    # The graph and the escape circuit run on the board. Both gold files come
    # along because deploy.sh checks the host runtime against them before
    # compiling, and neurons.csv names every neuron the circuit refers to.
    # model_bundle.json binds the five of them together; deploy.sh refuses to
    # build without it.
    REPO=slvDev/esp32-ai-fly
    PINNED=(
      "model_bundle.json 3b55f468f48e67ebf0866a52432a8981f1f782fcf4e69dbbdc98fe8c736eab3a 2003"
      "connectome.fcl 1e35e6ba658986de5e3163ea49b600378a2e3daa64700b8b619c4f21cc0a6e1f 14174411"
      "gold-device-order.bin 9e98ce8294f1a03ce3665f7f651be487f320c66ddf80f6067cf8b37388b9a9bb 75365188"
      "escape-circuit.json 566df28669c4b6ae5fff4a2330558837bce0c2a4fb08eb9edec4193285f3a91d 38724"
      "escape-gold.bin 0990325f0d90035b469e13218edf56631884809199276490ca3516b61837d056 4832"
      "neurons.csv 348e54b2a1c94b8387263c7af82ff41e8518be5fd662aed62bc28ebf281c3703 2132569"
    )
    ;;
  *)
    usage
    ;;
esac

# metadata.json travels with the assets but carries no pinned hash: it records
# the release commit, which is not known when these values are written. It is
# fetched, parsed, and cross-checked against the pins below.
NAMES=()
for entry in "${PINNED[@]}"; do NAMES+=("${entry%% *}"); done
NAMES+=("metadata.json")

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing: $1" >&2; exit 1; }; }
need hf
need shasum
need python3   # the metadata cross-check below is a python snippet

DEST="artifacts/$MODEL_KIND"

# Download into a scratch directory so a failure cannot leave artifacts/ holding
# a half-written or unverified file.
STAGING_PARENT=${TMPDIR:-/tmp}
STAGING_PARENT=${STAGING_PARENT%/}
STAGING=$(mktemp -d "$STAGING_PARENT/esp32ai-fetch-XXXXXX")
# Removes only the directory this invocation created. The name and parent are
# checked first so the path can only be the one mktemp returned, and a failed
# removal is reported rather than swallowed.
cleanup() {
  case "$STAGING" in
    "$STAGING_PARENT"/esp32ai-fetch-??????) ;;
    *) echo "not removing unexpected staging path: $STAGING" >&2; return 0 ;;
  esac
  [ -d "$STAGING" ] || return 0
  rm -rf -- "$STAGING" || echo "could not remove staging directory: $STAGING" >&2
}

# The install below records each step before taking it, so an interrupted or
# failed install can be undone: "aside NAME" moves the previous file into
# $WORK/old, "new NAME" puts the new file in place. Undoing checks which renames
# really happened. WORK is created inside $DEST so every step is a rename.
# Rollback has one entry point, on_exit, which ignores further signals first.
WORK=""
WORK_PATTERN=""
INSTALLING=0
STEPS=()

# Undoes the recorded steps, newest first, and runs at most once: each undone
# step is marked "done", so no second pass could delete a file it put back. It
# reports success only when every step was undone and $WORK/old is empty again;
# otherwise $WORK is kept, because it may hold the only copies.
restore() {
  local i step name ok=1
  [ "$INSTALLING" -eq 1 ] || return 0
  INSTALLING=0
  i=${#STEPS[@]}
  while [ "$i" -gt 0 ]; do
    i=$((i - 1))
    step=${STEPS[$i]}
    name=${step#* }
    case "$step" in
      "new "*)
        if [ ! -e "$WORK/new/$name" ]; then
          rm -f -- "$DEST/$name" || { ok=0; continue; }
        fi ;;
      "aside "*)
        if [ -e "$WORK/old/$name" ] || [ -L "$WORK/old/$name" ]; then
          mv -f -- "$WORK/old/$name" "$DEST/$name" || { ok=0; continue; }
        fi ;;
      *) continue ;;
    esac
    STEPS[$i]=done
  done
  if [ "$ok" -eq 1 ] && [ -z "$(ls -A -- "$WORK/old" 2>/dev/null)" ]; then
    echo "install failed; $DEST/ was restored to its previous files" >&2
    return 0
  fi
  echo "install failed and could not be fully undone: $DEST/ is not a consistent install." >&2
  echo "The previous files that could not be put back are in $WORK/old/." >&2
  WORK=""
  return 1
}

remove_work() {
  [ -n "$WORK" ] || return 0
  case "$WORK" in
    $WORK_PATTERN) ;;
    *) echo "not removing unexpected install path: $WORK" >&2; return 0 ;;
  esac
  [ -d "$WORK" ] || return 0
  rm -rf -- "$WORK" || echo "could not remove install directory: $WORK" >&2
}

on_exit() {
  # A second interrupt must not cut a rollback short.
  trap '' HUP INT TERM
  if [ "$INSTALLING" -eq 1 ]; then
    restore || true
  fi
  remove_work
  cleanup
}
trap on_exit EXIT
# Interrupts exit through the EXIT trap, so a half-done install is undone.
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

# A work directory left by an install whose rollback failed may hold the only
# copies of earlier files. It is never removed automatically; say so every run.
for stale in "$DEST"/.esp32ai-install-*; do
  [ -d "$stale" ] || continue
  echo "warning: $stale is left over from an install that could not be undone;" >&2
  echo "  it may hold the only copies of earlier files. Check it, then remove it by hand." >&2
done

echo "=== download $REPO ==="
hf download "$REPO" "${NAMES[@]}" --local-dir "$STAGING" 2>&1 | tail -3

echo "=== verify ==="
failed=0
for entry in "${PINNED[@]}"; do
  read -r name want_sha want_bytes <<<"$entry"
  path="$STAGING/$name"
  if [ ! -f "$path" ]; then
    echo "  $name MISSING from the download" >&2
    failed=1
    continue
  fi
  got_bytes=$(wc -c < "$path" | tr -d ' ')
  got_sha=$(shasum -a 256 "$path" | cut -d' ' -f1)
  if [ "$got_bytes" != "$want_bytes" ]; then
    echo "  $name size $got_bytes, expected $want_bytes" >&2
    failed=1
  elif [ "$got_sha" != "$want_sha" ]; then
    echo "  $name sha256 $got_sha, expected $want_sha" >&2
    failed=1
  else
    printf "  %-21s ok  %s B\n" "$name" "$got_bytes"
  fi
done

# The release's own metadata must agree with the pins above. The pinned bytes
# stay authoritative either way; a disagreement means the release is internally
# inconsistent, which is reported rather than worked around.
if [ -f "$STAGING/metadata.json" ]; then
  if ! python3 - "$STAGING/metadata.json" "${PINNED[@]}" <<'PY'
import json, sys
with open(sys.argv[1]) as fh:
    meta = json.load(fh)
files = meta.get("files", {})
bad = 0
for entry in sys.argv[2:]:
    name, sha, size = entry.split()
    rec = files.get(name)
    if rec is None:
        print(f"  metadata.json does not describe {name}", file=sys.stderr); bad = 1
    elif rec.get("sha256") != sha or str(rec.get("bytes")) != size:
        print(f"  metadata.json disagrees with the pinned {name}", file=sys.stderr); bad = 1
sys.exit(bad)
PY
  then
    failed=1
  else
    echo "  metadata.json          ok  agrees with the pinned values"
  fi
else
  echo "  metadata.json MISSING from the download" >&2
  failed=1
fi

if [ "$failed" -ne 0 ]; then
  echo "verification failed; $DEST/ was not modified" >&2
  exit 1
fi

# Everything verified: install, all or nothing. Existing files are replaced only
# at this point. mv onto a directory would move the file inside it instead.
mkdir -p "$DEST"
for name in "${NAMES[@]}"; do
  if [ -d "$DEST/$name" ]; then
    echo "$DEST/$name is a directory; remove or rename it and fetch again. $DEST/ was not modified" >&2
    exit 1
  fi
done

# Copy everything next to its destination first: staging may be on another
# filesystem, where a copy can fail part way. Until the renames below, $DEST/
# only gains the hidden work directory.
WORK_PATTERN="$DEST/.esp32ai-install-??????"
WORK=$(mktemp -d "$DEST/.esp32ai-install-XXXXXX") || {
  echo "could not create a work directory in $DEST/; $DEST/ was not modified" >&2
  WORK=""
  exit 1
}
case "$WORK" in
  $WORK_PATTERN) ;;
  *) echo "unexpected install path: $WORK; $DEST/ was not modified" >&2; WORK=""; exit 1 ;;
esac
mkdir "$WORK/new" "$WORK/old"
for name in "${NAMES[@]}"; do
  if ! cp -- "$STAGING/$name" "$WORK/new/$name"; then
    echo "could not copy $name next to $DEST/; $DEST/ was not modified" >&2
    exit 1
  fi
done

# Only renames within $DEST from here: move each previous file aside, then put
# the new one in its place. A failure or signal exits, and on_exit undoes every
# step taken so far.
INSTALLING=1
for name in "${NAMES[@]}"; do
  if [ -e "$DEST/$name" ] || [ -L "$DEST/$name" ]; then
    STEPS+=("aside $name")
    mv -f -- "$DEST/$name" "$WORK/old/$name" || exit 1
  fi
  STEPS+=("new $name")
  mv -f -- "$WORK/new/$name" "$DEST/$name" || exit 1
done
INSTALLING=0

echo
echo "installed into $DEST/:"
for name in "${NAMES[@]}"; do printf "  %s\n" "$name"; done
echo
echo "flash it with:"
echo "  scripts/deploy.sh $MODEL_KIND"
