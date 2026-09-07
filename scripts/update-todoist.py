#!/usr/bin/env python3
"""Verify Doist's stable ARM64 AppImage and update only its Flatpak source.

Requires PyYAML. --check-only fetches metadata only. Normal mode downloads a
new release before editing; --download also downloads an unchanged release.
No AppImage code is executed here. CI must additionally install/smoke-test the
Flatpak before publishing. Output is JSON; --github-output emits scalar fields.
"""

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import yaml

ORIGIN = "https://electron-dl.todoist.net"
FEED_URL = ORIGIN + "/linux/latest-linux-arm64.yml"
VERSION = r"(?:0|[1-9][0-9]{0,4})\.(?:0|[1-9][0-9]{0,4})\.(?:0|[1-9][0-9]{0,4})"
FILENAME = re.compile(r"Todoist-linux-(" + VERSION + r")-arm64-latest\.AppImage")
MAX_SIZE = 1024 * 1024 * 1024
TIMEOUT = 20
DOWNLOAD_SECONDS = 600


class InvalidRelease(ValueError):
    pass


class UniqueLoader(yaml.SafeLoader):
    """Do not silently accept conflicting integrity metadata."""


def unique_mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise InvalidRelease(f"duplicate YAML key: {key}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def require(condition, message):
    if not condition:
        raise InvalidRelease(message)


def stable_version(value):
    require(isinstance(value, str) and re.fullmatch(VERSION, value), "expected a stable x.y.z version")
    return tuple(int(part) for part in value.split("."))


def trusted_origin(url):
    parsed = urllib.parse.urlsplit(url)
    require(parsed.scheme == "https" and parsed.netloc == "electron-dl.todoist.net"
            and not parsed.query and not parsed.fragment,
            "download URL must use the exact official HTTPS origin without query/fragment")
    return parsed


def appimage_url(value, version):
    require(isinstance(value, str), "AppImage URL must be a string")
    filename = f"Todoist-linux-{version}-arm64-latest.AppImage"
    require(value in (filename, ORIGIN + "/linux/" + filename), "unexpected ARM64 AppImage URL")
    return ORIGIN + "/linux/" + filename


def sha512_digest(value):
    require(isinstance(value, str), "missing SHA512")
    try:
        digest = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as error:
        raise InvalidRelease("invalid base64 SHA512") from error
    require(len(digest) == 64, "SHA512 must be 64 bytes")
    return digest


@dataclass(frozen=True)
class Release:
    version: str
    url: str
    size: int
    sha512: bytes
    date: str


def parse_feed(text):
    feed = yaml.load(text, Loader=UniqueLoader)
    require(isinstance(feed, dict), "release metadata must be a mapping")
    version = feed.get("version")
    stable_version(version)
    files = feed.get("files")
    require(isinstance(files, list) and files, "missing release files")
    expected = f"Todoist-linux-{version}-arm64-latest.AppImage"
    candidates = [item for item in files if isinstance(item, dict)
                  and item.get("url") in (expected, ORIGIN + "/linux/" + expected)]
    require(len(candidates) == 1, "expected exactly one official ARM64 AppImage")
    item = candidates[0]
    url = appimage_url(item["url"], version)
    require(appimage_url(feed.get("path"), version) == url, "feed path differs from selected AppImage")
    digest = sha512_digest(item.get("sha512"))
    require(sha512_digest(feed.get("sha512")) == digest, "feed and file SHA512 disagree")
    size = item.get("size")
    require(type(size) is int and 64 <= size <= MAX_SIZE, "invalid or excessive AppImage size")
    timestamp = feed.get("releaseDate")
    require(isinstance(timestamp, str), "missing releaseDate")
    try:
        released = dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as error:
        raise InvalidRelease("invalid releaseDate") from error
    require(released.tzinfo is not None, "releaseDate must have a timezone")
    require(released <= dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1), "releaseDate is in the future")
    return Release(version, url, size, digest, released.date().isoformat())


@dataclass
class Pin:
    version: str
    url: str
    size: int
    sha256: str
    fields: dict


def mapping(node):
    require(isinstance(node, yaml.MappingNode), "unexpected manifest structure")
    return {key.value: value for key, value in node.value}


def read_pin(text):
    # Validate duplicate keys before using node positions to preserve formatting.
    parsed = yaml.load(text, Loader=UniqueLoader)
    require(isinstance(parsed, dict) and parsed.get("app-id") == "com.todoist.Todoist", "unexpected app ID")
    root = mapping(yaml.compose(text, Loader=UniqueLoader))
    require("modules" in root and isinstance(root["modules"], yaml.SequenceNode), "missing modules")
    sources = []
    for module in root["modules"].value:
        if not isinstance(module, yaml.MappingNode):
            continue
        module_fields = mapping(module)
        if "sources" not in module_fields:
            continue
        require(isinstance(module_fields["sources"], yaml.SequenceNode), "unexpected sources structure")
        for source in module_fields["sources"].value:
            if not isinstance(source, yaml.MappingNode):
                continue
            fields = mapping(source)
            if fields.get("type") is None or fields["type"].value != "extra-data":
                continue
            arches = fields.get("only-arches")
            if isinstance(arches, yaml.SequenceNode) and [arch.value for arch in arches.value] == ["aarch64"]:
                sources.append(fields)
    require(len(sources) == 1, "expected exactly one aarch64-only extra-data source")
    fields = sources[0]
    for key in ("url", "size", "sha256", "filename"):
        require(key in fields and isinstance(fields[key], yaml.ScalarNode), f"missing scalar {key}")
    require(fields["filename"].value == "todoist.AppImage", "unexpected ARM64 extra-data filename")
    url = fields["url"].value
    parsed_url = trusted_origin(url)
    match = FILENAME.fullmatch(parsed_url.path.removeprefix("/linux/"))
    require(match is not None and parsed_url.path.startswith("/linux/"), "unrecognized current ARM64 version")
    version = match.group(1)
    require(appimage_url(url, version) == url, "noncanonical current URL")
    require(re.fullmatch(r"[0-9a-f]{64}", fields["sha256"].value), "invalid pinned SHA256")
    require(re.fullmatch(r"[0-9]+", fields["size"].value), "invalid pinned size")
    size = int(fields["size"].value)
    require(64 <= size <= MAX_SIZE, "invalid pinned size")
    return Pin(version, url, size, fields["sha256"].value, fields)


def release_changed(pin, release):
    require(stable_version(release.version) >= stable_version(pin.version), "refusing release downgrade")
    if release.version == pin.version:
        require(release.url == pin.url and release.size == pin.size, "existing release was republished with different URL/size")
        return False
    return True


class OfficialRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        trusted_origin(newurl)
        return super().redirect_request(request, response, code, message, headers, newurl)


def open_official(url):
    trusted_origin(url)
    request = urllib.request.Request(url, headers={"User-Agent": "todoist-flatpak-release-checker/1"})
    return urllib.request.build_opener(OfficialRedirects()).open(request, timeout=TIMEOUT)


def fetch_feed():
    data = bytearray()
    started = time.monotonic()
    with open_official(FEED_URL) as response:
        while True:
            require(time.monotonic() - started < 60, "release metadata exceeded time limit")
            chunk = response.read1(16 * 1024)
            if not chunk:
                break
            data.extend(chunk)
            require(len(data) <= 1024 * 1024, "release metadata exceeds size limit")
    return parse_feed(data.decode("utf-8"))


def download_verified(release, directory, expected_sha256=None):
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / Path(urllib.parse.urlsplit(release.url).path).name
    sha256, sha512 = hashlib.sha256(), hashlib.sha512()
    count, header = 0, b""
    started = time.monotonic()
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".todoist-", delete=False) as output:
            temporary = Path(output.name)
            with open_official(release.url) as response:
                while True:
                    require(time.monotonic() - started < DOWNLOAD_SECONDS, "AppImage download exceeded time limit")
                    # read1 returns after one underlying read, allowing the
                    # deadline to stop a server that continually drips bytes.
                    data = response.read1(min(1024 * 1024, release.size - count + 1))
                    if not data:
                        break
                    count += len(data)
                    require(count <= release.size, "AppImage exceeds advertised size")
                    header = (header + data)[:64]
                    sha256.update(data)
                    sha512.update(data)
                    output.write(data)
        require(count == release.size, "AppImage size mismatch")
        require(sha512.digest() == release.sha512, "AppImage SHA512 mismatch")
        require(header[:4] == b"\x7fELF" and header[4:6] == b"\x02\x01"
                and int.from_bytes(header[18:20], "little") == 183,
                "AppImage must be a little-endian ELF64 aarch64 executable")
        digest = sha256.hexdigest()
        require(expected_sha256 is None or digest == expected_sha256, "AppImage differs from pinned SHA256")
        os.replace(temporary, destination)
        return destination, digest
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def updated_manifest(text, pin, release, digest):
    replacements = []
    for key, value in {"url": release.url, "size": str(release.size), "sha256": digest}.items():
        node = pin.fields[key]
        if node.style in ("'", '"'):
            value = node.style + value + node.style
        require(node.style in (None, "'", '"'), "unsupported manifest scalar formatting")
        replacements.append((node.start_mark.index, node.end_mark.index, value))
    for start, end, value in sorted(replacements, reverse=True):
        text = text[:start] + value + text[end:]
    candidate = read_pin(text)
    require(candidate.url == release.url and candidate.sha256 == digest and candidate.size == release.size,
            "updated manifest did not preserve verified payload")
    return text


def updated_metainfo(text, release):
    root = ET.fromstring(text)
    require(root.findtext("id") == "com.todoist.Todoist", "unexpected metainfo app ID")
    entries = root.findall("./releases/release")
    if any(entry.get("version") == release.version for entry in entries):
        return text
    require(entries, "missing metainfo releases")
    require(stable_version(release.version) > stable_version(entries[0].get("version")),
            "refusing to insert release older than metainfo")
    pattern = r"(?m)^(\s*)<releases>\s*\n"
    require(len(re.findall(pattern, text)) == 1, "unexpected metainfo release formatting")
    return re.sub(pattern, lambda match: match.group(0) + match.group(1) + "    "
                  + f'<release version="{release.version}" date="{release.date}"/>\n', text, count=1)


def atomic_write(path, text):
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix="." + path.name, delete=False) as output:
        temporary = Path(output.name)
        try:
            output.write(text)
            output.flush()
            os.chmod(temporary, path.stat().st_mode & 0o777)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def run(args):
    original = args.manifest.read_text(encoding="utf-8")
    pin = read_pin(original)
    release = fetch_feed()
    changed = release_changed(pin, release)
    result = dict(changed=changed, version=release.version, current_version=pin.version,
                  url=release.url, size=release.size, release_date=release.date)
    if args.check_only or (not changed and not args.download):
        return result
    require(args.download_dir is not None, "--download-dir is required when downloading")
    path, digest = download_verified(release, args.download_dir,
                                     expected_sha256=None if changed else pin.sha256)
    result.update(downloaded_path=str(path.resolve()), sha256=digest)
    if changed:
        manifest = updated_manifest(original, pin, release, digest)
        metadata = None
        if args.metainfo:
            metadata = updated_metainfo(args.metainfo.read_text(encoding="utf-8"), release)
        # All validation completes before either tracked file changes.
        require(args.manifest.read_text(encoding="utf-8") == original, "manifest changed during download")
        atomic_write(args.manifest, manifest)
        if metadata is not None:
            atomic_write(args.metainfo, metadata)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("com.todoist.Todoist.yaml"))
    parser.add_argument("--metainfo", type=Path, help="also prepend new release metadata to this file")
    parser.add_argument("--download-dir", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check-only", action="store_true")
    mode.add_argument("--download", action="store_true", help="download/verify even when version is unchanged")
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    try:
        result = run(args)
        if args.github_output:
            with args.github_output.open("a", encoding="utf-8") as output:
                for key, value in result.items():
                    scalar = str(value).lower() if isinstance(value, bool) else str(value)
                    require("\n" not in scalar and "\r" not in scalar, "output contains newline")
                    output.write(f"{key}={scalar}\n")
        print(json.dumps(result, sort_keys=True))
    except (InvalidRelease, OSError, ValueError, yaml.YAMLError, ET.ParseError) as error:
        print(f"Todoist release check failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
