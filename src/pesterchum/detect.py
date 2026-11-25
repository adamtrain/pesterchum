"""Decide, from a snapshot and the saved cursor, whether the PR needs attention."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from pesterchum.models import FAIL, PASS, PENDING, Check, Comment, Snapshot
from pesterchum.state import State

MERGED = "MERGED"
CLOSED = "CLOSED"
QUEUED = "QUEUED"

# Values of GitHub's mergeStateStatus that mean nothing stands in the way of merging.
# UNSTABLE is mergeable with a check outside the required set failing or still running.
MERGEABLE_STATES = {"CLEAN", "HAS_HOOKS", "UNSTABLE"}


@dataclass
class Options:
    include_self: bool = False
    ignore_authors: frozenset[str] = frozenset()
    wake_on_edits: bool = False
    all_checks: bool = False
    # Lower-cased names of checks to watch whether or not they are required.
    watch_checks: frozenset[str] = frozenset()
    fail_fast: bool = False
    through_queue: bool = False
    wake_on_ready: bool = False
    # Whether a commit is in the local checkout; None when there is no checkout to ask.
    commit_is_local: Callable[[str], bool | None] = lambda oid: None


@dataclass
class Events:
    terminal: str | None = None  # MERGED / CLOSED / QUEUED
    new_comments: list[Comment] = field(default_factory=list)
    edited_comments: list[Comment] = field(default_factory=list)
    conflict: bool = False
    dequeued: bool = False  # left the merge queue without merging
    # The previous head, when the branch has moved to a commit that somebody else pushed.
    head_moved_from: str | None = None
    failed_checks: list[Check] = field(default_factory=list)
    ready: bool = False  # became ready to merge, and the caller asked to hear about it
    # Counts over the checks being watched, for the report and progress lines.
    passed: int = 0
    failed: int = 0
    pending: int = 0
    # Failures exist but are being kept back until the checks still running have finished.
    failures_held: bool = False

    @property
    def needs_action(self) -> bool:
        return bool(
            self.new_comments or self.edited_comments or self.conflict or self.dequeued
            or self.head_moved_from or self.failed_checks
        )

    @property
    def wake(self) -> bool:
        return self.terminal is not None or self.needs_action or self.ready


def watched_checks(snapshot: Snapshot, options: Options) -> list[Check]:
    for check in snapshot.checks:
        check.watched_by_name = bool({check.name.lower(), check.label.lower()} & options.watch_checks)
    return [check for check in snapshot.checks if check.required or check.watched_by_name or options.all_checks]


def unmatched_names(snapshot: Snapshot, options: Options) -> list[str]:
    """The --watch-check names that no check on the head commit answers to."""
    present = {name for check in snapshot.checks for name in (check.name.lower(), check.label.lower())}
    return sorted(options.watch_checks - present)


def _ignored(comment: Comment, snapshot: Snapshot, options: Options) -> bool:
    login = comment.login.lower()
    if not options.include_self and login == snapshot.viewer.lower():
        return True
    return login in options.ignore_authors


def _pushed_by_someone_else(snapshot: Snapshot, options: Options) -> bool:
    local = options.commit_is_local(snapshot.head_oid)
    if local is not None:
        return not local
    # Without a checkout to look in, fall back on who wrote the commit.
    author = snapshot.head_author_login
    return author is not None and author.lower() != snapshot.viewer.lower()


def detect(snapshot: Snapshot, state: State, options: Options) -> Events:
    events = Events()

    if snapshot.state == "MERGED":
        events.terminal = MERGED
    elif snapshot.state == "CLOSED":
        events.terminal = CLOSED
    elif snapshot.in_merge_queue and not options.through_queue:
        events.terminal = QUEUED
    elif not snapshot.in_merge_queue:
        events.dequeued = state.queued

    for comment in snapshot.comments:
        if _ignored(comment, snapshot, options):
            continue
        if comment.id not in state.seen:
            events.new_comments.append(comment)
        elif options.wake_on_edits and state.seen[comment.id] != (comment.edited_at or ""):
            events.edited_comments.append(comment)

    if snapshot.state == "OPEN" and state.head and state.head != snapshot.head_oid:
        if _pushed_by_someone_else(snapshot, options):
            events.head_moved_from = state.head

    # Reported once per head commit: pushing a new commit that still conflicts reports again.
    events.conflict = snapshot.mergeable == "CONFLICTING" and state.conflict_head != snapshot.head_oid

    watched = watched_checks(snapshot, options)
    events.passed = sum(check.state == PASS for check in watched)
    events.failed = sum(check.state == FAIL for check in watched)
    events.pending = sum(check.state == PENDING for check in watched)
    # Failures are held back until nothing is still running, so one report carries them
    # all, unless the caller would rather hear about the first one straight away.
    events.failures_held = events.pending > 0 and not options.fail_fast
    if not events.failures_held:
        events.failed_checks = [
            check for check in watched
            if check.state == FAIL and check.failure_key(snapshot.head_oid) not in state.reported_failures
        ]

    # GitHub decides whether reviews and branch rules are satisfied; the watched checks are
    # looked at as well, so that --all-checks and --watch-check also hold readiness back.
    events.ready = (
        options.wake_on_ready
        and events.terminal is None
        and not snapshot.is_draft
        and snapshot.merge_state in MERGEABLE_STATES
        and events.failed == 0
        and events.pending == 0
        and state.ready_head != snapshot.head_oid
    )

    return events


def mark_reported(snapshot: Snapshot, events: Events, state: State) -> None:
    """Advance the cursor past everything in a report that has just been printed."""
    for comment in snapshot.comments:
        state.seen[comment.id] = comment.edited_at or ""
    if events.conflict:
        state.conflict_head = snapshot.head_oid
    state.queued = snapshot.in_merge_queue
    state.head = snapshot.head_oid
    if events.ready:
        state.ready_head = snapshot.head_oid
    keys = {check.failure_key(snapshot.head_oid) for check in events.failed_checks}
    # Keys for older commits can never match again, so drop them.
    kept = [key for key in state.reported_failures if key.startswith(snapshot.head_oid + "|")]
    state.reported_failures = sorted(set(kept) | keys)
