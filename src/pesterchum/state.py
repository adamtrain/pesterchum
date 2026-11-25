"""Per-PR cursor persisted between invocations, so nothing is reported twice or missed."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


def default_state_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "pesterchum"


def state_path(state_dir: Path, repo: str, number: int) -> Path:
    return state_dir / f"{repo.replace('/', '__')}__{number}.json"


def last_report_path(cursor: Path, as_json: bool) -> Path:
    """Where the most recent report for the PR with this cursor is kept, for --last."""
    return cursor.with_suffix(".last.json" if as_json else ".last.txt")


def write_atomic(path: Path, text: str) -> None:
    """Write then rename, so a kill mid-write cannot leave a truncated file behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    with os.fdopen(fd, "w") as handle:
        handle.write(text)
    os.replace(tmp, path)


@dataclass
class State:
    path: Path | None = None
    # comment id -> its lastEditedAt when we last saw it ("" if never edited)
    seen: dict[str, str] = field(default_factory=dict)
    reported_failures: list[str] = field(default_factory=list)
    # head commit for which a merge conflict has already been reported
    conflict_head: str | None = None
    # the PR was last seen in the merge queue, so leaving it without merging is news
    queued: bool = False
    # head commit for which the PR has already been reported as ready to merge
    ready_head: str | None = None

    @classmethod
    def load(cls, path: Path) -> State:
        try:
            raw = json.loads(path.read_text())
        except FileNotFoundError:
            return cls(path=path)
        except (json.JSONDecodeError, OSError) as error:
            raise ValueError(f"state file {path} is unreadable ({error}); delete it or pass --reset") from None
        return cls(
            path=path,
            seen=dict(raw.get("seen", {})),
            reported_failures=list(raw.get("reported_failures", [])),
            conflict_head=raw.get("conflict_head"),
            queued=bool(raw.get("queued", False)),
            ready_head=raw.get("ready_head"),
        )

    def save(self) -> None:
        if self.path is None:
            return
        payload = {
            "version": 1,
            "seen": self.seen,
            "reported_failures": self.reported_failures,
            "conflict_head": self.conflict_head,
            "queued": self.queued,
            "ready_head": self.ready_head,
        }
        write_atomic(self.path, json.dumps(payload, indent=2))
