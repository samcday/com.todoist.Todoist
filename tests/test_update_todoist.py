"""Release integrity and source isolation tests; no network or real binaries."""

import argparse
import base64
import copy
import hashlib
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

spec = importlib.util.spec_from_file_location(
    "update_todoist", Path(__file__).resolve().parents[1] / "scripts/update-todoist.py")
updater = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = updater
spec.loader.exec_module(updater)


def payload(machine=183):
    header = bytearray(64)
    header[:6] = b"\x7fELF\x02\x01"
    header[18:20] = machine.to_bytes(2, "little")
    return bytes(header) + b"fixture AppImage contents" * 10


def feed(version="9.31.0", binary=None):
    binary = payload() if binary is None else binary
    checksum = base64.b64encode(hashlib.sha512(binary).digest()).decode()
    filename = f"Todoist-linux-{version}-arm64-latest.AppImage"
    return {"version": version, "files": [{"url": filename, "sha512": checksum, "size": len(binary)}],
            "path": filename, "sha512": checksum, "releaseDate": "2026-08-04T16:54:04.119Z"}


def manifest(version="9.30.0"):
    return f"""app-id: com.todoist.Todoist
modules:
  - shared-modules/squashfs-tools/squashfs-tools.json
  - name: todoist
    sources:
      - type: extra-data
        only-arches: [x86_64]
        filename: todoist.snap
        url: https://api.snapcraft.io/original.snap
        sha256: {'a' * 64}
        size: 112746496
        x-checker-data:
          type: snapcraft
      - type: extra-data
        only-arches: [aarch64]
        filename: todoist.AppImage
        url: 'https://electron-dl.todoist.net/linux/Todoist-linux-{version}-arm64-latest.AppImage' # retain me
        sha256: {hashlib.sha256(payload()).hexdigest()}
        size: {len(payload())}
        x-checker-data:
          type: electron-updater
          url: {updater.FEED_URL}
      - type: script
        only-arches: [aarch64]
        commands: [true]
"""


METAINFO = """<?xml version="1.0"?>
<component type="desktop-application">
    <id>com.todoist.Todoist</id>
    <releases>
        <release version="9.30.0" date="2026-08-04"/>
    </releases>
</component>
"""


class ReleaseTests(unittest.TestCase):
    def test_official_release_metadata(self):
        release = updater.parse_feed(yaml.safe_dump(feed()))
        self.assertEqual(release.version, "9.31.0")
        self.assertEqual(release.date, "2026-08-04")
        self.assertEqual(release.size, len(payload()))

    def test_untrusted_or_ambiguous_metadata_fails_closed(self):
        invalid = []
        for version in ("9.31.0-beta.1", "09.31.0", "v9.31.0", "9.31", 931):
            case = feed()
            case["version"] = version
            invalid.append(case)
        for url in ("https://evil.example/Todoist-linux-9.31.0-arm64-latest.AppImage",
                    "http://electron-dl.todoist.net/linux/Todoist-linux-9.31.0-arm64-latest.AppImage",
                    "../Todoist-linux-9.31.0-arm64-latest.AppImage",
                    "Todoist-linux-9.31.0-x86_64-latest.AppImage"):
            case = feed()
            case["files"][0]["url"] = url
            invalid.append(case)
        for size in (True, "304", -1, 1, updater.MAX_SIZE + 1):
            case = feed()
            case["files"][0]["size"] = size
            invalid.append(case)
        for digest in ("garbage", "YQ==", None):
            case = feed()
            case["files"][0]["sha512"] = digest
            invalid.append(case)
        case = feed()
        case["files"].append(copy.deepcopy(case["files"][0]))
        invalid.append(case)
        case = feed()
        case["sha512"] = base64.b64encode(b"a" * 64).decode()
        invalid.append(case)
        for case in invalid:
            with self.subTest(case=case), self.assertRaises(updater.InvalidRelease):
                updater.parse_feed(yaml.safe_dump(case))

    def test_duplicate_yaml_keys_rejected(self):
        with self.assertRaisesRegex(updater.InvalidRelease, "duplicate YAML key"):
            updater.parse_feed(yaml.safe_dump(feed()) + "version: 9.99.0\n")

    def test_cross_origin_and_insecure_redirects_rejected(self):
        for url in ("https://evil.example/payload", "http://electron-dl.todoist.net/payload",
                    "https://electron-dl.todoist.net:443/payload", "https://electron-dl.todoist.net@evil.example/payload"):
            with self.subTest(url=url), self.assertRaises(updater.InvalidRelease):
                updater.OfficialRedirects().redirect_request(None, None, 302, "Found", {}, url)

    def test_only_arm64_source_changes_and_formatting_survives(self):
        original = manifest()
        release = updater.parse_feed(yaml.safe_dump(feed()))
        digest = "b" * 64
        updated = updater.updated_manifest(original, updater.read_pin(original), release, digest)
        expected = original.replace("Todoist-linux-9.30.0-arm64", "Todoist-linux-9.31.0-arm64")
        expected = expected.replace(hashlib.sha256(payload()).hexdigest(), digest)
        self.assertEqual(updated, expected)
        self.assertIn("' # retain me", updated)

    def test_shared_or_multiple_arm64_sources_rejected(self):
        ambiguous = manifest().replace("only-arches: [x86_64]", "only-arches: [aarch64]")
        shared = manifest().replace("only-arches: [aarch64]", "only-arches: [x86_64, aarch64]")
        for text in (ambiguous, shared):
            with self.assertRaisesRegex(updater.InvalidRelease, "exactly one"):
                updater.read_pin(text)

    def test_downgrade_or_republished_size_rejected(self):
        pin = updater.read_pin(manifest())
        with self.assertRaisesRegex(updater.InvalidRelease, "downgrade"):
            updater.release_changed(pin, updater.parse_feed(yaml.safe_dump(feed("9.29.9"))))
        with self.assertRaisesRegex(updater.InvalidRelease, "republished"):
            updater.release_changed(pin, updater.parse_feed(yaml.safe_dump(feed("9.30.0", payload() + b"x"))))

    def test_verified_download(self):
        binary = payload()
        release = updater.parse_feed(yaml.safe_dump(feed(binary=binary)))
        with tempfile.TemporaryDirectory() as tmp, patch.object(updater, "open_official", return_value=io.BytesIO(binary)):
            path, digest = updater.download_verified(release, Path(tmp))
            self.assertEqual(path.read_bytes(), binary)
            self.assertEqual(digest, hashlib.sha256(binary).hexdigest())

    def test_bad_payload_never_replaces_verified_file(self):
        for label, advertised, received, pinned in (
            ("checksum", payload(), payload()[:-1] + b"x", None),
            ("truncated", payload(), payload()[:-1], None),
            ("oversized", payload(), payload() + b"x", None),
            ("wrong-arch", payload(62), payload(62), None),
            ("changed-current", payload(), payload(), "0" * 64),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                release = updater.parse_feed(yaml.safe_dump(feed(binary=advertised)))
                destination = Path(tmp) / release.url.rsplit("/", 1)[1]
                destination.write_bytes(b"previous verified artifact")
                with patch.object(updater, "open_official", return_value=io.BytesIO(received)):
                    with self.assertRaises(updater.InvalidRelease):
                        updater.download_verified(release, Path(tmp), pinned)
                self.assertEqual(destination.read_bytes(), b"previous verified artifact")
                self.assertEqual(list(Path(tmp).iterdir()), [destination])

    def test_metainfo_insert_is_idempotent_and_preserves_content(self):
        release = updater.parse_feed(yaml.safe_dump(feed()))
        updated = updater.updated_metainfo(METAINFO, release)
        added = '        <release version="9.31.0" date="2026-08-04"/>\n'
        self.assertEqual(updated.replace(added, ""), METAINFO)
        self.assertEqual(updater.updated_metainfo(updated, release), updated)

    def test_run_check_and_unchanged_skip_binary_download(self):
        for check_only, version, changed in ((True, "9.31.0", True), (False, "9.30.0", False)):
            with self.subTest(check_only=check_only), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "manifest.yaml"
                path.write_text(manifest())
                args = argparse.Namespace(manifest=path, metainfo=None, download_dir=None,
                                          download=False, check_only=check_only)
                release = updater.parse_feed(yaml.safe_dump(feed(version)))
                with patch.object(updater, "fetch_feed", return_value=release), patch.object(updater, "download_verified") as download:
                    result = updater.run(args)
                    download.assert_not_called()
                self.assertEqual(result["changed"], changed)
                self.assertEqual(path.read_text(), manifest())

    def test_run_integrity_failure_leaves_manifest_and_metainfo_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, meta = Path(tmp) / "manifest.yaml", Path(tmp) / "metainfo.xml"
            path.write_text(manifest())
            meta.write_text(METAINFO)
            args = argparse.Namespace(manifest=path, metainfo=meta, download_dir=Path(tmp),
                                      download=False, check_only=False)
            release = updater.parse_feed(yaml.safe_dump(feed()))
            with patch.object(updater, "fetch_feed", return_value=release), patch.object(updater, "open_official", return_value=io.BytesIO(b"bad")):
                with self.assertRaises(updater.InvalidRelease):
                    updater.run(args)
            self.assertEqual(path.read_text(), manifest())
            self.assertEqual(meta.read_text(), METAINFO)

    def test_run_updates_after_verification_and_then_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, meta = Path(tmp) / "manifest.yaml", Path(tmp) / "metainfo.xml"
            path.write_text(manifest())
            meta.write_text(METAINFO)
            args = argparse.Namespace(manifest=path, metainfo=meta, download_dir=Path(tmp),
                                      download=False, check_only=False)
            release = updater.parse_feed(yaml.safe_dump(feed()))
            with patch.object(updater, "fetch_feed", return_value=release), patch.object(updater, "open_official", return_value=io.BytesIO(payload())):
                result = updater.run(args)
            self.assertTrue(result["changed"])
            self.assertEqual(updater.read_pin(path.read_text()).version, "9.31.0")
            self.assertIn('version="9.31.0"', meta.read_text())
            updated = path.read_bytes(), meta.read_bytes()
            with patch.object(updater, "fetch_feed", return_value=release), patch.object(updater, "download_verified") as download:
                self.assertFalse(updater.run(args)["changed"])
                download.assert_not_called()
            self.assertEqual((path.read_bytes(), meta.read_bytes()), updated)


if __name__ == "__main__":
    unittest.main()
