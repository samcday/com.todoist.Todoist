#!/usr/bin/env python3
"""Follow only the supported runtime versions in Todoist's upstream manifest."""

import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

import yaml


UPSTREAM_URL = (
    "https://raw.githubusercontent.com/flathub/com.todoist.Todoist/"
    "master/com.todoist.Todoist.yaml"
)
MAX_MANIFEST_BYTES = 1024 * 1024
EXPECTED_IDENTITIES = {
    "app-id": "com.todoist.Todoist",
    "base": "org.electronjs.Electron2.BaseApp",
    "runtime": "org.freedesktop.Platform",
    "sdk": "org.freedesktop.Sdk",
}
VERSION_PATTERN = re.compile(r"[0-9]{2}\.[0-9]{2}\Z")
RUNTIME_LINES = re.compile(
    r"^(?P<key>base-version|runtime-version)[ \t]*:[ \t]*"
    r"(?P<quote>['\"]?)(?P<version>[0-9]{2}\.[0-9]{2})(?P=quote)"
    r"[ \t]*(?:\#[^\r\n]*)?(?:\r?\n|$)",
    re.MULTILINE,
)


class UniqueKeyLoader(yaml.SafeLoader):
    """Reject ambiguous metadata instead of accepting the last duplicate key."""


def unique_mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            if key in result:
                raise ValueError(f"duplicate YAML key: {key!r}")
            result[key] = loader.construct_object(value_node, deep=deep)
        except TypeError as error:
            raise ValueError("manifest has an invalid mapping key") from error
    return result


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping
)


def runtime_version(contents, label):
    """Require compatible app/runtime identities and one matched version pair."""
    manifest = yaml.load(contents, Loader=UniqueKeyLoader)
    if not isinstance(manifest, dict):
        raise ValueError(f"{label} manifest must be a YAML mapping")
    for field, expected in EXPECTED_IDENTITIES.items():
        if manifest.get(field) != expected:
            raise ValueError(f"{label} {field} must remain {expected!r}")
    versions = []
    for field in ("base-version", "runtime-version"):
        version = manifest.get(field)
        if not isinstance(version, str) or not VERSION_PATTERN.fullmatch(version):
            raise ValueError(f"{label} {field} must be a quoted NN.NN version")
        versions.append(version)
    if versions[0] != versions[1]:
        raise ValueError(f"{label} base-version and runtime-version must match")
    return versions[0]


def fetch_upstream():
    request = urllib.request.Request(
        UPSTREAM_URL, headers={"User-Agent": "todoist-flatpak-runtime-checker"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        destination = urllib.parse.urlsplit(response.url)
        if destination.scheme != "https" or destination.hostname != "raw.githubusercontent.com":
            raise ValueError("upstream manifest redirected outside its trusted HTTPS host")
        contents = response.read(MAX_MANIFEST_BYTES + 1)
    if len(contents) > MAX_MANIFEST_BYTES:
        raise ValueError("upstream manifest exceeds 1 MiB")
    return contents.decode("utf-8")


def sync_runtime(manifest_path, upstream_contents):
    manifest_path = Path(manifest_path)
    # Read bytes so CRLF line endings are preserved alongside quoting/comments.
    original = manifest_path.read_bytes().decode("utf-8")
    previous = runtime_version(original, "local")
    upstream = runtime_version(upstream_contents, "upstream")
    version_tuple = lambda value: tuple(int(part) for part in value.split("."))
    if version_tuple(upstream) <= version_tuple(previous):
        return {"changed": False, "from": previous, "to": previous}

    matches = list(RUNTIME_LINES.finditer(original))
    if sorted(match.group("key") for match in matches) != ["base-version", "runtime-version"]:
        raise ValueError("local runtime versions must be simple top-level YAML scalars")
    updated = original
    for match in reversed(matches):
        updated = updated[:match.start("version")] + upstream + updated[match.end("version"):]
    if runtime_version(updated, "updated") != upstream:
        raise ValueError("updated manifest failed runtime validation")

    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=manifest_path.parent, delete=False) as output:
            temporary_path = Path(output.name)
            output.write(updated.encode("utf-8"))
        temporary_path.chmod(stat.S_IMODE(manifest_path.stat().st_mode))
        os.replace(temporary_path, manifest_path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return {"changed": True, "from": previous, "to": upstream}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("com.todoist.Todoist.yaml"))
    parser.add_argument("--upstream-file", type=Path, help="read upstream YAML locally for offline checks")
    args = parser.parse_args()
    try:
        upstream = args.upstream_file.read_text() if args.upstream_file else fetch_upstream()
        print(json.dumps(sync_runtime(args.manifest, upstream), sort_keys=True))
    except (OSError, ValueError, yaml.YAMLError, urllib.error.URLError) as error:
        print(f"Runtime check failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
