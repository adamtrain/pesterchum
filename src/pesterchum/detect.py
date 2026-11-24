"""Decide, from a snapshot and the saved cursor, whether the PR needs attention."""

from __future__ import annotations

from dataclasses import dataclass, field

from pesterchum.models import FAIL, PASS, PENDING, Check, Comment, Snapshot
from pesterchum.state import State

MERGED = "MERGED"
CLOSED = "CLOSED"
QUEUED = "QUEUED"


@dataclass
class Options:
    include_self: bool = False
    ignore_authors: frozenset[str] = frozenset()
    wake_on_edits: bool = False
    all_checks: bool = False


@dataclass
class Events:
    terminal: str | None = None  # MERGED / CLOSED / QUEUED
    new_comments: list[Comment] = field(default_factory=list)
    edited_comments: list[Comment] = field(default_factory=list)
    conflict: bool = False
    failed_checks: list[Check] = field(default_factory=list)
    # Counts over the checks being watched, for the report and progress lines.
    passed: int = 0
    failed: int = 0
    pending: int = 0

    @property
    def needs_action(self) -> bool:
        return bool(self.new_comments or self.edited_comments or self.conflict or self.failed_checks)

    @property
    def wake(self) -> bool:
        return self.terminal is not None or self.needs_action


def watched_checks(snapshot: Snapshot, options: Options) -> list[Check]:
    return [check for check in snapshot.checks if check.required or options.all_checks]


def _ignored(comment: Comment, snapshot: Snapshot, options: Options) -> bool:
    login = comment.login.lower()
    if not options.include_self and login == snapshot.viewer.lower():
        return True
    return login in options.ignore_authors


def detect(snapshot: Snapshot, state: State, options: Options) -> Events:
    events = Events()

    if snapshot.state == "MERGED":
        events.terminal = MERGED
    elif snapshot.state == "CLOSED":
        events.terminal = CLOSED
    elif snapshot.in_merge_queue:
        events.terminal = QUEUED

    for comment in snapshot.comments:
        if _ignored(comment, snapshot, options):
            continue
        if comment.id not in state.seen:
            events.new_comments.append(comment)
        elif options.wake_on_edits and state.seen[comment.id] != (comment.edited_at or ""):
            events.edited_comments.append(comment)

    # Reported once per head commit: pushing a new commit that still conflicts reports again.
    events.conflict = snapshot.mergeable == "CONFLICTING" and state.conflict_head != snapshot.head_oid

    watched = watched_checks(snapshot, options)
    events.passed = sum(check.state == PASS for check in watched)
    events.failed = sum(check.state == FAIL for check in watched)
    events.pending = sum(check.state == PENDING for check in watched)
    # Failures are held back until nothing is still running, so one report carries them all.
    if events.pending == 0:
        events.failed_checks = [
            check for check in watched
            if check.state == FAIL and check.failure_key(snapshot.head_oid) not in state.reported_failures
        ]

    return events


def mark_reported(snapshot: Snapshot, events: Events, state: State) -> None:
    """Advance the cursor past everything in a report that has just been printed."""
    for comment in snapshot.comments:
        state.seen[comment.id] = comment.edited_at or ""
    if events.conflict:
        state.conflict_head = snapshot.head_oid
    keys = {check.failure_key(snapshot.head_oid) for check in events.failed_checks}
    # Keys for older commits can never match again, so drop them.
    kept = [key for key in state.reported_failures if key.startswith(snapshot.head_oid + "|")]
    state.reported_failures = sorted(set(kept) | keys)
