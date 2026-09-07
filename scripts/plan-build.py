#!/usr/bin/env python3
"""Skip unchanged Flatpak builds; record state only after a successful publish/check.

The fingerprint covers tracked working-tree files and checked-out submodule
commits, excluding .automation/. --record is intentionally a separate operation:
the publishing workflow calls it only after deployment succeeds or no build is
needed. Monthly state refreshes also keep the scheduled workflow active.
"""

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], timeout=30)


def valid_commit(value):
    return isinstance(value, str) and re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value)


def fingerprint(repo):
    digest = hashlib.sha256()

    def field(data):
        digest.update(str(len(data)).encode() + b":" + data)

    for entry in sorted(git(repo, "ls-files", "--stage", "-z").split(b"\0")):
        if not entry:
            continue
        metadata, name = entry.split(b"\t", 1)
        mode, indexed_commit, stage = metadata.split()
        if name.startswith(b".automation/"):
            continue
        if stage != b"0":
            raise ValueError("cannot plan a build with unresolved merge conflicts")
        field(name)
        path = repo / os.fsdecode(name)
        if mode == b"160000":
            # Check that git did not walk up into the parent repository when
            # the submodule is uninitialized, and do not traverse its contents.
            top = Path(os.fsdecode(git(path, "rev-parse", "--show-toplevel").rstrip(b"\n")))
            if top.resolve() != path.resolve():
                raise ValueError(f"submodule must be initialized: {os.fsdecode(name)}")
            commit = git(path, "rev-parse", "HEAD").strip()
            if not valid_commit(commit.decode("ascii")):
                raise ValueError("invalid checked-out submodule commit")
            field(mode)
            field(indexed_commit)
            field(commit)
        elif mode == b"120000":
            if not path.is_symlink():
                raise ValueError(f"tracked symlink changed type: {os.fsdecode(name)}")
            field(mode)
            field(os.fsencode(os.readlink(path)))
        elif mode in (b"100644", b"100755"):
            details = path.lstat()
            if not stat.S_ISREG(details.st_mode):
                raise ValueError(f"tracked regular file changed type: {os.fsdecode(name)}")
            field(b"100755" if details.st_mode & 0o111 else b"100644")
            content = hashlib.sha256()
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    content.update(chunk)
            field(content.digest())
        else:
            raise ValueError(f"unsupported tracked file mode: {mode.decode()}")
    return digest.hexdigest()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate state field: {key}")
        result[key] = value
    return result


def read_state(path):
    if not path.exists():
        return None
    state = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
    if not isinstance(state, dict) or set(state) != {"input_digest", "base_commit", "checked_month"}:
        raise ValueError("build state must contain exactly input_digest, base_commit and checked_month")
    if not isinstance(state["input_digest"], str) or not re.fullmatch(r"[0-9a-f]{64}", state["input_digest"]):
        raise ValueError("invalid build state input_digest")
    if not valid_commit(state["base_commit"]):
        raise ValueError("invalid build state base_commit")
    if not isinstance(state["checked_month"], str) or not re.fullmatch(r"[0-9]{4}-(?:0[1-9]|1[0-2])", state["checked_month"]):
        raise ValueError("invalid build state checked_month")
    return state


def plan(repo, state_path, base_commit, force=False):
    if not valid_commit(base_commit):
        raise ValueError("--base-commit must be a lowercase 40- or 64-digit hex commit")
    previous = read_state(state_path)
    input_digest = fingerprint(repo)
    build = force or previous is None or previous["input_digest"] != input_digest or previous["base_commit"] != base_commit
    return dict(build=build, input_digest=input_digest, base_commit=base_commit,
                checked_month=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m"))


def record(path, result):
    state = {key: result[key] for key in ("input_digest", "base_commit", "checked_month")}
    if read_state(path) == state:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".state-", delete=False) as output:
            temporary = Path(output.name)
            json.dump(state, output, indent=2, sort_keys=True)
            output.write("\n")
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
        return True
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-commit", required=True)
    parser.add_argument("--state", type=Path, default=Path(".automation/state.json"))
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--record", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    try:
        repo = Path(os.fsdecode(git(Path.cwd(), "rev-parse", "--show-toplevel").rstrip(b"\n")))
        result = plan(repo, args.state, args.base_commit, args.force)
        if args.record:
            result["state_changed"] = record(args.state, result)
        if args.github_output:
            with args.github_output.open("a", encoding="utf-8") as output:
                for key, value in result.items():
                    value = str(value).lower() if isinstance(value, bool) else value
                    output.write(f"{key}={value}\n")
        print(json.dumps(result, sort_keys=True))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Build planning failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
