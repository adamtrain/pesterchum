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


@dataclass
class State:
    path: Path | None = None
    # comment id -> its lastEditedAt when we last saw it ("" if never edited)
    seen: dict[str, str] = field(default_factory=dict)
    reported_failures: list[str] = field(default_factory=list)
    # head commit for which a merge conflict has already been reported
    conflict_head: str | None = None

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
        )

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "seen": self.seen,
            "reported_failures": self.reported_failures,
            "conflict_head": self.conflict_head,
        }
        # Write then rename, so a kill mid-write cannot leave a truncated cursor behind.
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name, suffix=".tmp")
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, indent=2)
        os.replace(tmp, self.path)
