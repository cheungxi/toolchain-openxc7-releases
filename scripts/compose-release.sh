#!/usr/bin/env bash
#
# compose-release.sh -- check a release's assets and write what goes with them.
#
#   scripts/compose-release.sh <tag> [release-body.py options]
#
# Run in the directory that holds the platform tarballs and
# XILINX-PARTS-INVENTORY.json (the pre-release job of build-pre-release and of
# build-upstream-nightly, after downloading their artifacts). It checks
# that every tarball carries the date of the tag, the same index bytes in
# its chipdb directory and the bins that index names, and writes the other
# three things the release publishes or shows:
#   BUILD-INFO.json   the release-level build info (scripts/release-build-info.py)
#   SHA256SUMS        of the other assets
#   RELEASE-BODY.md   the release text (scripts/release-body.py; the options
#                     after the tag go to it: --main-revisions and --drift
#                     for an upstream tag)
#
# The tag is a dated one (2026-10-01) or an upstream one
# (upstream-2026-10-01); the assets and the index carry its date either
# way (pack/release_tags.py).
# Set OPENXC7_RELEASE_LOONGARCH64=true when the dated build enables that job.

set -euo pipefail

[ $# -ge 1 ] || { echo "usage: $0 <tag> [release-body.py options]" >&2; exit 2; }
TAG=$1; shift
REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
export PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}"
export LC_ALL=C          # one collation for every listing below

DATE_TAG=$(python3 -c 'import sys; from pack.release_tags import split_tag; print(split_tag(sys.argv[1])[1])' "$TAG")
DATEID=${DATE_TAG//-/}
# The name of the parts document, from its one owner; and the name it
# had before, which a release composed here must not carry. Inside each
# package it lives at PACKAGE_PATH, checked below.
INDEX=$(python3 -c 'from pack.parts_index import PACKAGE_FILE; print(PACKAGE_FILE)')
LEGACY_INDEX=$(python3 -c 'from pack.parts_index import LEGACY_PACKAGE_FILE; print(LEGACY_PACKAGE_FILE)')

ls -la openxc7-toolchain-*.tgz "$INDEX"
# every package a consumer resolves by name must carry the tag's
# date, or it 404s at install time
for f in openxc7-toolchain-*.tgz; do
    case "$f" in
        *"-$DATEID.tgz") ;;
        *) echo "::error::asset $f is not dated $DATEID"; exit 1 ;;
    esac
done
# The index is NOT dated: it names its own release inside
# (release-tag), and travels under the same name it has in the
# chipdb directory of every package ($INDEX).
test -f "$INDEX" || { echo "::error::$INDEX missing"; exit 1; }
if [ -e "$LEGACY_INDEX" ]; then
    echo "::error::$LEGACY_INDEX is the name before $INDEX: it must not ride along"
    exit 1
fi
# No per-die chipdb asset is published. A leftover from the
# previous contract must not ride along.
if compgen -G 'apio-xilinx-chipdb-*.bin.tgz' > /dev/null; then
    echo "::error::chipdb assets are not part of a release: the bins travel in the packages"
    exit 1
fi

# Each tarball carries the bins the index names, the same index
# bytes next to them (and nowhere else), and the identity stamp.
# The document is read only after the packer's own validator
# (pack/parts_index.py, the one owner of the format) accepted it
# for the date of this tag.
python3 - "$DATE_TAG" <<'PYEOF'
import json, os, sys, tarfile, tempfile
from pathlib import Path
from pack.parts_index import (CHIPDB_SUBDIR, PACKAGE_FILE, PACKAGE_PATH,
                              index_members, index_note, validate_document,
                              validate_package_info)
from pack.platform import LOONGARCH64, RELEASE_PLATFORMS
tag = sys.argv[1]
published = Path(PACKAGE_FILE)
raw = published.read_bytes()
tarballs = sorted(Path(".").glob("openxc7-toolchain-*.tgz"))
loongarch = os.environ.get("OPENXC7_RELEASE_LOONGARCH64") == "true"
expected = {f"openxc7-toolchain-{platform}-{tag.replace('-', '')}.tgz"
            for platform in RELEASE_PLATFORMS if platform != LOONGARCH64 or loongarch}
actual = {p.name for p in tarballs}
if actual != expected:
    sys.exit(f"::error::platform tarballs: missing {sorted(expected - actual)}, "
             f"unexpected {sorted(actual - expected)}")
for tarball in tarballs:
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)
        with tarfile.open(tarball) as archive:
            members = []
            names = []
            for member in archive.getmembers():
                name = member.name[2:] if member.name.startswith("./") else member.name
                if member.isfile():
                    names.append(name)
                if name.startswith(f"{CHIPDB_SUBDIR}/"):
                    member.name = name
                    members.append(member)
            # The places a reader looks for the document: a package composed
            # here carries it in the chipdb directory and nowhere else.
            for place in index_members(names):
                if place != PACKAGE_PATH:
                    sys.exit(f"::error::{tarball.name} carries {place} "
                             f"({index_note(place)}): a package built now "
                             f"carries {PACKAGE_PATH} only")
            archive.extractall(root, members=members)
        index = root / PACKAGE_PATH
        if not index.is_file():
            sys.exit(f"::error::{tarball.name} carries no {PACKAGE_PATH}")
        if index.read_bytes() != raw:
            sys.exit(f"::error::{tarball.name} carries a different {PACKAGE_PATH}")
        try:
            info = json.loads(raw)
            validate_document(info, expect_tag=tag, written_now=True)
            counts = validate_package_info(index, root / CHIPDB_SUBDIR,
                                           written_now=True)
        except ValueError as error:
            sys.exit(f"::error::{tarball.name}: {error}")
    print(f"{tarball.name}: {counts['chipdb-files']} chipdb files, "
          f"{counts['generated-count']} of {counts['part-count']} parts "
          f"(schema {info['schema']})")
PYEOF

# The BUILD-INFO.json of each package, which goes into the
# release text (ecosystem convention, apio#927) and, composed
# into one release-level document, travels as an asset of its
# own (apio#1009, asked by the apio maintainer so a crawler can
# read the build identity without downloading a package).
# Member naming differs per platform (the linux/darwin packer tars
# "-C dist ." -> "./BUILD-INFO.json"; the windows job appends the
# bare "BUILD-INFO.json"), so resolve the actual member name from
# the listing instead of hardcoding one form (2026-08-13 body
# showed '{ "missing": true }' for two of the three packages).
rm -rf build-infos
mkdir -p build-infos
: > BUILD-INFOS.md
for f in openxc7-toolchain-*.tgz; do
    M=$(tar tzf "$f" | grep -Ex '(\./)?BUILD-INFO\.json' | head -1 || true)
    [ -n "$M" ] \
        || { echo "::error::$f carries no BUILD-INFO.json"; exit 1; }
    tar xzf "$f" -O "$M" > "build-infos/$f.json"
    {
        echo "**\`$f\`**"
        echo '```json'
        cat "build-infos/$f.json"
        echo '```'
        echo
    } >> BUILD-INFOS.md
done
# The release-level document: the fields the packages agree
# on, plus a row per package for the ones that are properly
# per-package. Composing it IS the agreement check —
# packages built from different sources stop the release here.
python3 "$REPO_ROOT/scripts/release-build-info.py" build-infos/*.json > BUILD-INFO.json
cat BUILD-INFO.json
# It describes THIS tag: the packages were told the tag they are
# published under (an upstream build that forgot would claim a dated tag).
python3 - "$TAG" openxc7-toolchain-*.tgz <<'PYEOF'
import json, sys
info = json.load(open("BUILD-INFO.json"))
recorded = info["release-tag"]
if recorded != sys.argv[1]:
    sys.exit(f"::error::the packages record release-tag {recorded!r}, "
             f"the release is {sys.argv[1]!r}")
if sorted(entry["file-name"] for entry in info["packages"].values()) != sorted(sys.argv[2:]):
    sys.exit("::error::BUILD-INFO.json does not name the release tarballs")
PYEOF

# SHA256SUMS covers the packages, the index and the build info. Written
# HERE, in the job that uploads them, from the very bytes that
# the upload sends — the manifest and the release cannot
# describe different bytes.
sha256sum openxc7-toolchain-*.tgz "$INDEX" BUILD-INFO.json > SHA256SUMS
cat SHA256SUMS
# The release text: what the packages are, the yosys they need,
# the assets, the revisions and a manual install -- a release
# usable without apio. Composed from the document just checked.
python3 "$REPO_ROOT/scripts/release-body.py" BUILD-INFO.json BUILD-INFOS.md "$@" > RELEASE-BODY.md
cat RELEASE-BODY.md
