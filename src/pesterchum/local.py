"""Look in the git checkout in the working directory, to tell our own pushes from others'."""

from __future__ import annotations

import subprocess

GIT_TIMEOUT_SECONDS = 20


def commit_is_local(repo: str, oid: str, cwd: str | None = None) -> bool | None:
    """Whether the checkout in the working directory has the commit.

    A commit the agent pushed is necessarily there; one that somebody else pushed is not
    until it is fetched. None means the working directory is not a checkout of `repo`,
    so there is nothing to go by.
    """
    remotes = _git(["config", "--get-regexp", r"^remote\..*\.url$"], cwd)
    if remotes is None or not any(_names_repo(line.split()[-1], repo) for line in remotes.splitlines() if line.strip()):
        return None
    return _git(["cat-file", "-e", f"{oid}^{{commit}}"], cwd) is not None


def _names_repo(url: str, repo: str) -> bool:
    url = url.lower().rstrip("/").removesuffix(".git")
    return url.endswith(("/" + repo.lower(), ":" + repo.lower()))


def _git(args: list[str], cwd: str | None) -> str | None:
    """The command's output, or None if it failed or git is not available."""
    try:
        proc = subprocess.run(["git", *args], capture_output=True, text=True, timeout=GIT_TIMEOUT_SECONDS, cwd=cwd)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    return proc.stdout if proc.returncode == 0 else None
