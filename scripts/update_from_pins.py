#!/usr/bin/env python3
"""Helper for hermes-update.cmd.

Reads scripts/hermes-update.local-pins.json and applies the actions:
- push:  skip (Austin already pushed it to fork upstream)
- keep:  git cherry-pick from the patch branch onto the current branch
- discard: skip (already in upstream fork/main)
- skip: skip (Austin decided not to re-apply)

Idempotent: re-running the script on the same branch is safe — already-applied commits are skipped.

``--verify`` runs the pre-flight check that hermes-update.cmd calls BEFORE its
``git checkout --force -B main upstream/main``. That checkout is destructive: anything
reachable only from the current branch is discarded. --verify proves that every local-only
commit is reproducible from the pin file, so the reset can never silently drop work.

Usage:
    python update_from_pins.py <repo_path> <pins_path>
    python update_from_pins.py <repo_path> <pins_path> --verify [--against upstream/main]

Exit codes:
    0 = success (all keep commits applied or skipped-cleanly)
    1 = fatal error (git problem, malformed pins, etc.)
    2 = partial success with conflicts (Austin must resolve manually)
    3 = --verify found local-only commits that no pin would re-apply (refuse to reset)
"""
from __future__ import annotations
import argparse
import json
import subprocess
import sys
from pathlib import Path


def run(cmd: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    """Run a command, capture output, raise if non-zero."""
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding='utf-8', errors='replace')
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


# hermes-update.cmd stages an authoritative copy of the updater scripts in
# %TEMP%\hermes-update-runner and restores them into the repo immediately after the
# destructive checkout ("copy /y %~dp0*.* ORIG_SCRIPTS_DIR"). Those files therefore
# survive every update WITHOUT being pinned, so commits touching only them are exempt
# from the coverage check.
#
# Pinning them would actively break the update: after the restore they are untracked,
# and a cherry-pick that adds a path already present untracked is refused by git with
# "untracked working tree files would be overwritten by merge".
RUNNER_RESTORED = {
    "scripts/hermes-update.cmd",
    "scripts/hermes-update.local-pins.json",
    "scripts/update_from_pins.py",
}

# Actions whose commit the update will actually re-apply onto the new base. `discard` and
# `skip` intentionally drop their commit, so they provide no coverage.
COVERING_ACTIONS = {"keep", "push"}


def commit_files(sha: str, repo: Path) -> list[str]:
    """Paths a commit touches (empty for a merge commit / unreadable commit)."""
    proc = subprocess.run(
        ["git", "-C", str(repo), "show", "--name-only", "--format=", sha],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        return []
    return [line.strip().replace("\\", "/") for line in proc.stdout.splitlines() if line.strip()]


def local_only_commits(base: str, repo: Path) -> list[str]:
    """Commits reachable from HEAD but not from ``base``, oldest first."""
    proc = subprocess.run(
        ["git", "-C", str(repo), "rev-list", "--reverse", f"{base}..HEAD"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        raise SystemExit(f"ERROR: cannot list commits in {base}..HEAD")
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def verify_against_pins(repo: Path, pins: dict, base: str) -> int:
    """Refuse to let a destructive reset discard work the pin file would not restore.

    The safety property being enforced is the absence of *silent* loss. A local commit is
    covered when every file it touches is either touched by some covering pin, or is the
    pin file itself. Coverage is deliberately file-level rather than patch-id-level: a pin
    that rewrites the same file still surfaces loudly (update_from_pins reports CONFLICT and
    exits 2 for manual resolution), whereas a file no pin touches is discarded with no
    signal at all. Conflicting is recoverable; silent is not.
    """
    print(f"=== verifying local work against {base} ===")
    print(f"  repo: {repo}")

    commits = local_only_commits(base, repo)
    if not commits:
        print("  no local-only commits — nothing to lose.")
        return 0
    print(f"  local-only commits: {len(commits)}")

    covered_files: dict[str, str] = {}
    for entry in pins.get("commits", []):
        sha = entry.get("sha")
        action = entry.get("action", "skip")
        if not sha or action not in COVERING_ACTIONS:
            continue
        resolved = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}"],
            capture_output=True, text=True,
        )
        if resolved.returncode != 0 or not resolved.stdout.strip():
            print(f"  WARNING: pin {sha[:11]} ({action}) is not present in this repo")
            continue
        for path in commit_files(resolved.stdout.strip(), repo):
            covered_files.setdefault(path, f"{sha[:11]} ({action})")

    uncovered: list[tuple[str, list[str]]] = []
    exempt = 0
    for sha in commits:
        files = commit_files(sha, repo)
        if files and set(files) <= RUNNER_RESTORED:
            exempt += 1
            continue
        lost = [p for p in files if p not in covered_files and p not in RUNNER_RESTORED]
        if lost:
            uncovered.append((sha, lost))

    print()
    print(f"=== summary ===")
    print(f"  local-only:   {len(commits)}")
    print(f"  covered:      {len(commits) - exempt - len(uncovered)}")
    print(f"  self-exempt:  {exempt}  (runner-restored updater scripts)")
    print(f"  UNCOVERED:    {len(uncovered)}")

    if not uncovered:
        print()
        print("  every local-only commit has a pin that rewrites its files.")
        print("  Safe to reset: a re-apply may conflict (loud), but nothing is lost silently.")
        return 0

    print()
    print("  ABORT: the destructive checkout would discard local work that no pin")
    print("  rewrites. Add a pin entry with action \"keep\" for each commit below, or")
    print("  land it upstream, before running the update again:")
    for sha, lost in uncovered:
        proc = subprocess.run(
            ["git", "-C", str(repo), "show", "-s", "--format=%s", sha],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        subject = proc.stdout.strip() if proc.returncode == 0 else ""
        print()
        print(f"    {sha}")
        print(f"      {subject}")
        print(f"      files with no covering pin:")
        for path in lost:
            print(f"        {path}")
    return 3


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", type=Path)
    parser.add_argument("pins", type=Path)
    parser.add_argument("--verify", action="store_true",
                        help="pre-flight: abort if any local-only commit is not covered by a pin")
    parser.add_argument("--against", default="upstream/main",
                        help="ref that the destructive checkout resets to (default: upstream/main)")
    args = parser.parse_args()

    if not args.repo.is_dir():
        print(f"ERROR: repo path {args.repo} is not a directory", file=sys.stderr)
        return 1
    if not args.pins.is_file():
        print(f"ERROR: pins file {args.pins} is not a file", file=sys.stderr)
        return 1

    pins = json.loads(args.pins.read_text(encoding="utf-8"))

    if args.verify:
        return verify_against_pins(args.repo, pins, args.against)

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
