"""Fingerprint and publication-state tests using small, real Git repositories."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/plan-build.py"
spec = importlib.util.spec_from_file_location("plan_build", SCRIPT)
planner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(planner)
BASE = "a" * 64


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "package"
        self.repo.mkdir()
        self.git(self.repo, "init", "-q", "-b", "main")
        self.source = self.repo / "manifest.yaml"
        self.source.write_text("version: 1\n")
        self.git(self.repo, "add", "manifest.yaml")
        self.state = self.repo / ".automation/state.json"

    @staticmethod
    def git(repo, *args):
        return subprocess.check_output(["git", "-C", str(repo), "-c", "user.name=Fixture",
                                        "-c", "user.email=fixture@example.invalid", *args], stderr=subprocess.STDOUT)

    def plan(self, base=BASE, force=False):
        return planner.plan(self.repo, self.state, base, force)

    def test_new_state_builds_then_unchanged_check_skips(self):
        first = self.plan()
        self.assertTrue(first["build"])
        self.assertTrue(planner.record(self.state, first))
        before = self.state.read_bytes(), self.state.stat().st_mtime_ns
        self.assertFalse(self.plan()["build"])
        self.assertFalse(planner.record(self.state, self.plan()))
        self.assertEqual((self.state.read_bytes(), self.state.stat().st_mtime_ns), before)

    def test_tracked_working_tree_edits_and_modes_trigger_build(self):
        original = self.plan()
        planner.record(self.state, original)
        self.source.write_text("version: 2\n")
        self.assertTrue(self.plan()["build"])
        self.assertNotEqual(original["input_digest"], self.plan()["input_digest"])
        self.source.write_text("version: 1\n")
        self.assertFalse(self.plan()["build"])
        self.source.chmod(0o755)
        self.assertTrue(self.plan()["build"])

    def test_recorded_state_and_untracked_files_are_excluded(self):
        original = self.plan()["input_digest"]
        planner.record(self.state, self.plan())
        self.git(self.repo, "add", ".automation/state.json")
        (self.repo / ".automation/other.json").write_text("not a build input")
        self.git(self.repo, "add", ".automation/other.json")
        (self.repo / "untracked.AppImage").write_bytes(b"downloaded runtime")
        self.assertEqual(original, self.plan()["input_digest"])
        (self.repo / ".automation/other.json").write_text("new bookkeeping")
        self.assertEqual(original, self.plan()["input_digest"])

    def test_base_change_and_force_trigger_build(self):
        planner.record(self.state, self.plan())
        self.assertTrue(self.plan("b" * 64)["build"])
        self.assertTrue(self.plan(force=True)["build"])
        self.assertFalse(self.plan()["build"])

    def test_monthly_refresh_does_not_trigger_build(self):
        result = self.plan()
        result["checked_month"] = "2020-01"
        planner.record(self.state, result)
        current = self.plan()
        self.assertFalse(current["build"])
        self.assertTrue(planner.record(self.state, current))
        self.assertEqual(planner.read_state(self.state)["checked_month"], current["checked_month"])

    def test_malformed_state_is_never_silently_ignored_or_overwritten(self):
        valid = {key: self.plan()[key] for key in ("input_digest", "base_commit", "checked_month")}
        invalid = ["not JSON", "[]", "{}", json.dumps({**valid, "unknown": 1}),
                   json.dumps({**valid, "input_digest": "x" * 64}),
                   json.dumps({**valid, "base_commit": "short"}),
                   json.dumps({**valid, "checked_month": "2026-13"}),
                   json.dumps({**valid, "checked_month": True}),
                   json.dumps(valid)[:-1] + ', "base_commit": "' + BASE + '"}']
        self.state.parent.mkdir()
        for text in invalid:
            with self.subTest(state=text):
                self.state.write_text(text)
                with self.assertRaises(ValueError):
                    self.plan()
                with self.assertRaises(ValueError):
                    planner.record(self.state, valid)
                self.assertEqual(self.state.read_text(), text)

    def test_submodule_commit_is_hashed_without_walking_submodule_files(self):
        child = self.root / "source-module"
        child.mkdir()
        self.git(child, "init", "-q", "-b", "main")
        (child / "source.txt").write_text("one\n")
        self.git(child, "add", ".")
        self.git(child, "commit", "-qm", "Initial fixture")
        self.git(self.repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(child), "shared-modules")
        module = self.repo / "shared-modules"
        before = self.plan()["input_digest"]
        (module / "source.txt").write_text("two\n")
        self.assertEqual(before, self.plan()["input_digest"])
        self.git(module, "add", ".")
        self.git(module, "commit", "-qm", "Updated fixture")
        self.assertNotEqual(before, self.plan()["input_digest"])
        unstaged = self.plan()["input_digest"]
        self.git(self.repo, "add", "shared-modules")
        self.assertNotEqual(unstaged, self.plan()["input_digest"])

    def test_symlink_target_is_hashed(self):
        link = self.repo / "linked-manifest"
        link.symlink_to("manifest.yaml")
        self.git(self.repo, "add", "linked-manifest")
        before = self.plan()["input_digest"]
        link.unlink()
        link.symlink_to("different.yaml")
        self.assertNotEqual(before, self.plan()["input_digest"])

    def test_missing_tracked_file_and_invalid_base_fail(self):
        with self.assertRaises(ValueError):
            self.plan("invalid")
        self.source.unlink()
        with self.assertRaises(OSError):
            self.plan()

    def test_cli_record_and_github_outputs(self):
        output = self.root / "github-output"
        result = subprocess.check_output([sys.executable, str(SCRIPT), "--base-commit", BASE,
                                          "--record", "--github-output", str(output)], cwd=self.repo)
        parsed = json.loads(result)
        self.assertTrue(parsed["build"])
        self.assertTrue(parsed["state_changed"])
        self.assertIn("build=true\n", output.read_text())
        self.assertFalse(self.plan()["build"])


if __name__ == "__main__":
    unittest.main()
