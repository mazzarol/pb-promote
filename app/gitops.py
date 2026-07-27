"""
Git operations — commit, tag, push for promotion audit trail.
"""

import subprocess
import os
from datetime import datetime, timezone


CUSTOM_ADDONS = "/opt/odoo19dev/custom-addons/priority_blinds"


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _run_git(args: list[str], timeout: int = 30) -> tuple[bool, str, str]:
    """Run a git command in the custom addons repo. Returns (ok, stdout, stderr)."""
    try:
        result = subprocess.run(
            ["git", "-C", CUSTOM_ADDONS] + args,
            capture_output=True, text=True, timeout=timeout,
        )
        return result.returncode == 0, result.stdout.strip(), result.stderr.strip()
    except FileNotFoundError:
        return False, "", "git not found"
    except subprocess.TimeoutExpired:
        return False, "", "git command timed out"
    except Exception as e:
        return False, "", str(e)


def is_git_repo() -> bool:
    """Check if custom addons dir is a git repo."""
    return os.path.isdir(os.path.join(CUSTOM_ADDONS, ".git"))


def get_status() -> dict:
    """Get git status of the custom addons repo."""
    if not is_git_repo():
        return {"is_repo": False, "error": f"Not a git repo: {CUSTOM_ADDONS}"}

    ok, stdout, stderr = _run_git(["status", "--porcelain"])
    files = [l for l in stdout.split("\n") if l] if ok else []

    # Get current branch
    ok2, branch, _ = _run_git(["rev-parse", "--abbrev-ref", "HEAD"])
    if not ok2:
        branch = "unknown"

    # Get last commit
    ok3, last_commit, _ = _run_git(["log", "-1", "--format=%h %s"])
    if not ok3:
        last_commit = "unknown"

    return {
        "is_repo": True,
        "branch": branch,
        "last_commit": last_commit,
        "uncommitted_count": len(files),
        "uncommitted_files": files[:20],
        "clean": len(files) == 0,
    }


def auto_commit_dev() -> tuple[bool, str]:
    """Stage all changes and commit with an auto-generated message."""
    if not is_git_repo():
        return False, "Not a git repo"

    # Stage all
    ok, _, err = _run_git(["add", "-A"])
    if not ok:
        return False, f"git add failed: {err}"

    # Check if there's anything to commit
    ok, stdout, _ = _run_git(["diff", "--cached", "--quiet"])
    if ok:  # exit 0 = no changes
        return True, "Nothing to commit"

    # Commit
    msg = f"promote: pre-promotion snapshot {_ts()}"
    ok, _, err = _run_git(["commit", "-m", msg])
    if ok:
        return True, f"Committed: {msg}"
    return False, f"git commit failed: {err}"


def create_tag(env: str) -> str:
    """Create and push a git tag for a promotion."""
    if not is_git_repo():
        return ""

    tag = f"{env}-{_ts()}"
    ok, _, err = _run_git(["tag", tag])
    if not ok:
        return f"tag-failed:{err[:80]}"

    # Push tag
    ok, _, err = _run_git(["push", "origin", tag], timeout=60)
    if not ok:
        return f"tagged:{tag} (push failed: {err[:80]})"

    return tag


def get_tags(limit: int = 20) -> list[str]:
    """Get recent git tags."""
    if not is_git_repo():
        return []

    ok, stdout, _ = _run_git([
        "tag", "--sort=-creatordate",
    ])
    if not ok:
        return []

    tags = [t for t in stdout.split("\n") if t]
    return tags[:limit]


def rollback_to_tag(tag: str, code_root: str) -> tuple[bool, str]:
    """Rollback a code directory to a specific git tag (if it's a git repo)."""
    if not is_git_repo():
        return False, "Not a git repo"

    # This operates on the custom addons repo, not the full odoo code tree
    ok, _, err = _run_git(["checkout", tag])
    if ok:
        return True, f"Checked out tag: {tag}"
    return False, f"Checkout failed: {err}"
