#!/usr/bin/env python3
"""Compose the release-level BUILD-INFO.json from the packages' own.

Every package carries a BUILD-INFO.json at its root (scripts/build-info.sh,
FPGAwars ecosystem convention). Since apio#1009 the same information also
travels as an asset of the release, so a crawler can answer "what exactly
is this build" without downloading a 100 MB tarball -- apio's own releases
publish theirs the same way.

The package documents are NOT interchangeable: each names its own
platform and tarball, and each is stamped when that platform finished
building. Publishing one of them as THE document of the release would
describe a multi-platform release as a linux build. The release-level
document is composed from the fields they agree on at the top
level, and a row per package for the fields that are properly per-package.

Composing it is also a gate. The packages are built by separate jobs of
one run, from one revision and one chipdb; if they disagree on any shared
field -- toolchain revision, chipdb identity, commit, release tag -- the
release is incoherent and this exits non-zero instead of papering over it
with one platform's answer.

    scripts/release-build-info.py <package BUILD-INFO.json>... > BUILD-INFO.json
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pack.platform import LOONGARCH64  # noqa: E402

# Properly per-package: the platform, the file that carries it, and when
# that platform's job stamped its document. LoongArch also records the
# Debian Yosys version in its package row.
PER_PACKAGE = ("target-platform", "file-name", "build-time")


def compose(documents):
    """The release-level document, or ValueError naming the disagreement.

    *documents* maps a source name (used in error messages) to the parsed
    BUILD-INFO.json of one package.
    """
    if not documents:
        raise ValueError("no package BUILD-INFO.json given")

    first_name, first = next(
        ((name, info) for name, info in documents.items()
         if info.get("target-platform") != LOONGARCH64),
        next(iter(documents.items())),
    )
    shared = {key: value for key, value in first.items()
              if key not in PER_PACKAGE}

    packages = {}
    for name, info in documents.items():
        for key in PER_PACKAGE:
            if not info.get(key):
                raise ValueError(f"{name}: missing field {key!r}")
        platform = info["target-platform"]
        if platform in packages:
            raise ValueError(f"two documents for platform {platform!r}")
        row = {"file-name": info["file-name"], "build-time": info["build-time"]}
        keys = set(info) - set(PER_PACKAGE)
        expected = set(shared)
        if platform == LOONGARCH64:
            if not info.get("yosys-version") or info.get("yosys-release-tag") != "unknown":
                raise ValueError(f"{name}: missing Debian Yosys version or unexpected suite tag")
            row.update({"yosys-release-tag": info["yosys-release-tag"],
                        "yosys-version": info["yosys-version"]})
            keys -= {"yosys-release-tag", "yosys-version"}
            expected -= {"yosys-release-tag", "yosys-version"}
        if keys != expected:
            missing = sorted(expected - keys)
            extra = sorted(keys - expected)
            raise ValueError(
                f"{name} does not carry the same fields as {first_name}: "
                f"missing {missing}, extra {extra}")
        for key, value in shared.items():
            if key in expected and info[key] != value:
                raise ValueError(
                    f"the packages disagree on {key!r}: {first_name} says "
                    f"{value!r}, {name} says {info[key]!r} -- they were not "
                    "built from the same sources")

        packages[platform] = row
    # The shared keys keep the order they have in the package document, so
    # the two files read the same; the per-package rows go last.
    return {**shared, "packages": dict(sorted(packages.items()))}


def main(argv):
    if not argv:
        print(__doc__.rstrip().rsplit("\n\n", 1)[1].strip(), file=sys.stderr)
        return 2
    documents = {}
    for path in argv:
        with open(path, encoding="utf-8") as source:
            documents[path] = json.load(source)
    try:
        print(json.dumps(compose(documents), indent=2))
    except ValueError as error:
        print(f"release-build-info: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
