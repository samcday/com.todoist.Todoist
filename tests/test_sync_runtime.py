import importlib.util
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sync-runtime.py"
SPEC = importlib.util.spec_from_file_location("sync_runtime", SCRIPT)
sync_runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync_runtime)


def manifest(version="25.08"):
    return (
        "app-id: com.todoist.Todoist\n"
        "base: org.electronjs.Electron2.BaseApp\n"
        f"base-version: '{version}' # preserve this comment\n"
        "runtime: org.freedesktop.Platform\n"
        f'runtime-version: "{version}"\n'
        "sdk: org.freedesktop.Sdk\n"
        "command: todoist\n"
        "modules:\n"
        "  - name: todoist\n"
        "    sources:\n"
        "      - type: extra-data\n"
        "        url: https://example.invalid/Todoist-25.08.AppImage\n"
    )


class RuntimeSyncTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.local = Path(self.directory.name) / "com.todoist.Todoist.yaml"
        self.local.write_text(manifest())

    def test_bump_changes_only_versions_preserving_formatting(self):
        original = manifest().replace("\n", "\r\n").encode()
        self.local.write_bytes(original)
        self.local.chmod(0o640)
        result = sync_runtime.sync_runtime(self.local, manifest("26.08"))
        self.assertEqual(result, {"changed": True, "from": "25.08", "to": "26.08"})
        expected = original.replace(b"base-version: '25.08'", b"base-version: '26.08'")
        expected = expected.replace(b'runtime-version: "25.08"', b'runtime-version: "26.08"')
        self.assertEqual(self.local.read_bytes(), expected)
        self.assertEqual(self.local.stat().st_mode & 0o777, 0o640)

    def test_equal_version_is_idempotent(self):
        before = self.local.stat().st_mtime_ns
        result = sync_runtime.sync_runtime(self.local, manifest())
        self.assertEqual(result, {"changed": False, "from": "25.08", "to": "25.08"})
        self.assertEqual(self.local.stat().st_mtime_ns, before)

    def test_older_upstream_never_downgrades(self):
        result = sync_runtime.sync_runtime(self.local, manifest("24.08"))
        self.assertFalse(result["changed"])
        self.assertEqual(result["to"], "25.08")
        self.assertEqual(self.local.read_text(), manifest())

    def test_rejects_incompatible_upstream_identities_without_writing(self):
        for field, expected in sync_runtime.EXPECTED_IDENTITIES.items():
            with self.subTest(field=field):
                upstream = manifest("26.08").replace(f"{field}: {expected}", f"{field}: unexpected")
                with self.assertRaisesRegex(ValueError, field):
                    sync_runtime.sync_runtime(self.local, upstream)
                self.assertEqual(self.local.read_text(), manifest())

    def test_rejects_invalid_or_mismatched_versions_without_writing(self):
        cases = [
            manifest("26.08").replace("'26.08'", "'latest'"),
            manifest("26.08").replace("'26.08'", "26.08"),
            manifest("26.08").replace("'26.08'", "'26.8'"),
            manifest("26.08").replace("'26.08'", "'25.08'"),
            manifest("26.08") + "runtime-version: '26.08'\n",
        ]
        for upstream in cases:
            with self.subTest(upstream=upstream):
                with self.assertRaises(ValueError):
                    sync_runtime.sync_runtime(self.local, upstream)
                self.assertEqual(self.local.read_text(), manifest())

    def test_rejects_incompatible_local_manifest(self):
        invalid = manifest().replace("app-id: com.todoist.Todoist", "app-id: other.App")
        self.local.write_text(invalid)
        with self.assertRaisesRegex(ValueError, "local app-id"):
            sync_runtime.sync_runtime(self.local, manifest("26.08"))
        self.assertEqual(self.local.read_text(), invalid)

    def test_offline_cli_emits_json(self):
        upstream = Path(self.directory.name) / "upstream.yaml"
        upstream.write_text(manifest("26.08"))
        arguments = [str(SCRIPT), "--manifest", str(self.local), "--upstream-file", str(upstream)]
        output = io.StringIO()
        with patch.object(sys, "argv", arguments), redirect_stdout(output):
            self.assertEqual(sync_runtime.main(), 0)
        self.assertEqual(json.loads(output.getvalue()), {"changed": True, "from": "25.08", "to": "26.08"})


if __name__ == "__main__":
    unittest.main()
