#!/usr/bin/env python3
"""Helper for hermes-update.cmd.

Reads scripts/hermes-update.local-pins.json and applies the actions:
- push:  skip (Austin already pushed it to fork upstream)
- keep:  git cherry-pick from the patch branch onto the current branch
- discard: skip (already in upstream fork/main)
- skip: skip (Austin decided not to re-apply)

Idempotent: re-running the script on the same branch is safe — already-applied commits are skipped.

Usage:
    python update_from_pins.py <repo_path> <pins_path>

Exit codes:
    0 = success (all keep commits applied or skipped-cleanly)
    1 = fatal error (git problem, malformed pins, etc.)
    2 = partial success with conflicts (Austin must resolve manually)
"""
from __future__ import annotations
import argparse
import json
import subprocess
import sys
from pathlib import Path


def run(cmd: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    """Run a command, capture output, raise if non-zero."""
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        print(f"FAILED: {' '.join(cmd)}", file=sys.stderr)
        print(proc.stdout, file=sys.stderr)
        print(proc.stderr, file=sys.stderr)
        raise SystemExit(1)
    return proc


def is_ancestor(needle: str, possible_ancestor: str, repo: Path) -> bool:
    """True if needle is reachable from possible_ancestor in git's DAG."""
    proc = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", possible_ancestor, needle],
        capture_output=True, text=True,
    )
    return proc.returncode == 0


def current_branch(repo: Path) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    )
    return proc.stdout.strip()


def tip_already_applied(sha: str, repo: Path) -> tuple[bool, str]:
    """Check if sha is reachable from HEAD or if its patch is already present in HEAD.

    Returns (already_applied, reason).
    """
    # 1. Direct ancestor check (exact commit SHA in history)
    proc = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", sha, "HEAD"],
        capture_output=True, text=True,
    )
    if proc.returncode == 0:
        return True, "ancestor"

    # 2. Patch-id equivalence check via git cherry
    # git cherry HEAD <sha> <sha>^ outputs:
    #   '' or '- <sha>' if equivalent patch/diff is already in HEAD
    #   '+ <sha>' if patch is not in HEAD
    cherry = subprocess.run(
        ["git", "-C", str(repo), "cherry", "HEAD", sha, f"{sha}^"],
        capture_output=True, text=True,
    )
    if cherry.returncode == 0:
        out = cherry.stdout.strip()
        if not out:
            return True, "ancestor"
        if out.startswith("-"):
            return True, "patch-id match (merged upstream)"

    return False, ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", type=Path)
    parser.add_argument("pins", type=Path)
    args = parser.parse_args()

    if not args.repo.is_dir():
        print(f"ERROR: repo path {args.repo} is not a directory", file=sys.stderr)
        return 1
    if not args.pins.is_file():
        print(f"ERROR: pins file {args.pins} is not a file", file=sys.stderr)
        return 1

    pins = json.loads(args.pins.read_text())
    commits = pins.get("commits", [])
    if not commits:
        print("nothing to do (no commits in pins)")
        return 0

    print(f"=== applying pins from {args.pins} ===")
    print(f"  repo: {args.repo}")
    print(f"  branch: {current_branch(args.repo)}")
    print()

    applied = 0
    already = 0
    skipped = 0
    discarded = 0
    pushed = 0
    conflicts = []

    # Apply commits in chronological order (oldest -> newest) so parent dependencies resolve
    for entry in reversed(commits):
        sha = entry.get("sha")
        action = entry.get("action", "skip")
        note = entry.get("note", "")
        if not sha:
            print(f"  skip (no sha): {note}")
            skipped += 1
            continue

        applied_already, reason = tip_already_applied(sha, args.repo)
        if applied_already:
            reason_str = f" [{reason}]" if reason else ""
            print(f"  already applied: {sha[:11]}{reason_str}  ({note})")
            already += 1
            continue

        if action == "push":
            print(f"  push: {sha[:11]}  (already pushed - skip)  ({note})")
            pushed += 1
            continue

        if action == "discard":
            print(f"  discard: {sha[:11]}  ({note})")
            discarded += 1
            continue

        if action == "skip":
            print(f"  skip (manual): {sha[:11]}  ({note})")
            skipped += 1
            continue

        if action != "keep":
            print(f"  unknown action {action!r}: {sha[:11]} - skip")
            skipped += 1
            continue

        # verify commit exists in git
        exists = subprocess.run(
            ["git", "-C", str(args.repo), "rev-parse", "--verify", sha],
            capture_output=True,
        )
        if exists.returncode != 0:
            print(f"  skip: commit {sha[:11]} does not exist locally - cannot cherry-pick")
            skipped += 1
            continue

        print(f"  keep: cherry-picking {sha[:11]}  ({note})")
        proc = subprocess.run(
            ["git", "-C", str(args.repo), "cherry-pick", "--no-commit", sha],
            capture_output=True, text=True,
        )
        if proc.returncode == 0:
            print(f"     applied (will be committed after verification)")
            applied += 1
        else:
            print(f"     CONFLICT - manual resolution needed")
            conflicts.append((sha, proc.stdout, proc.stderr))

    # commit the applied commits as a single batch if any
    if applied > 0:
        # only commit if there are no conflicts pending
        if not conflicts:
            run(["git", "-C", str(args.repo), "add", "-A"], args.repo)
            run(
                [
                    "git", "-C", str(args.repo), "commit", "-m",
                    f"chore: apply {applied} local fix(es) from pins after update",
                ],
                args.repo,
            )
            print(f"committed {applied} applied fix(es)")

    print()
    print(f"=== summary ===")
    print(f"  applied:    {applied}")
    print(f"  already:    {already}")
    print(f"  pushed:     {pushed}")
    print(f"  discarded:  {discarded}")
    print(f"  skipped:    {skipped}")
    print(f"  conflicts:  {len(conflicts)}")

    if conflicts:
        print()
        print(f"  {len(conflicts)} cherry-pick(s) had conflicts. Resolve manually:")
        for sha, stdout, stderr in conflicts:
            print(f"    - {sha[:11]}: see 'git status' for the unmerged path(s)")
        print()
        print(f"  After resolving each conflict:")
        print(f"    git -C {args.repo} add <resolved-files>")
        print(f"    git -C {args.repo} cherry-pick --continue")
        print(f"    git -C {args.repo} cherry-pick --abort  (if you want to skip)")
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
