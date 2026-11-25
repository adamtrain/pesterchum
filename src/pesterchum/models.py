"""Plain data types shared by the fetch, detect and report layers."""

from __future__ import annotations

from dataclasses import dataclass, field

PASS = "pass"
FAIL = "fail"
PENDING = "pending"


@dataclass
class Comment:
    id: str  # GraphQL node id
    kind: str  # "issue" (PR conversation), "review" (review summary) or "inline" (on a diff line)
    login: str  # raw login, without any [bot] suffix
    is_bot: bool
    body: str
    url: str
    created_at: str
    edited_at: str | None = None
    database_id: int | None = None  # the id the REST API uses for this comment or review
    thread_id: str | None = None  # node id of the review thread an inline comment belongs to
    review_state: str | None = None  # APPROVED / CHANGES_REQUESTED / COMMENTED / DISMISSED
    path: str | None = None
    line: int | None = None
    diff_hunk: str | None = None
    thread_resolved: bool = False
    thread_outdated: bool = False
    reply_to_login: str | None = None
    reply_to_excerpt: str | None = None

    @property
    def author(self) -> str:
        return f"{self.login} [bot]" if self.is_bot else self.login


@dataclass
class Check:
    name: str
    kind: str  # "check_run" or "status"
    state: str  # PASS / FAIL / PENDING
    raw_state: str  # e.g. FAILURE, TIMED_OUT, IN_PROGRESS
    required: bool
    url: str | None = None
    description: str | None = None
    workflow: str | None = None
    app: str | None = None
    job_id: int | None = None  # check run database id (the Actions job id)
    timestamp: str = ""
    watched_by_name: bool = False  # named with --watch-check

    @property
    def label(self) -> str:
        return f"{self.workflow} / {self.name}" if self.workflow else self.name

    def failure_key(self, head_oid: str) -> str:
        """Identity of one failed attempt: a rerun or a new commit gets a new key."""
        return "|".join([head_oid, self.kind, self.workflow or "", self.name, str(self.job_id or ""), self.timestamp])


@dataclass
class Snapshot:
    repo: str
    number: int
    url: str
    title: str
    state: str  # OPEN / CLOSED / MERGED
    is_draft: bool
    mergeable: str  # MERGEABLE / CONFLICTING / UNKNOWN
    in_merge_queue: bool
    merge_queue_position: int | None
    head_oid: str
    base_ref: str
    viewer: str
    # Why the PR last left the merge queue without merging, if it ever did.
    dequeue_reason: str | None = None
    # The head commit: its subject line, and who wrote it (the login is None when
    # GitHub cannot match the commit's email address to an account).
    head_headline: str = ""
    head_author_login: str | None = None
    head_author_name: str | None = None
    # GitHub's own verdict on whether the PR can merge: CLEAN, BLOCKED, BEHIND, DIRTY, ...
    merge_state: str = "UNKNOWN"
    review_decision: str | None = None  # APPROVED / CHANGES_REQUESTED / REVIEW_REQUIRED
    comments: list[Comment] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    rate_remaining: int | None = None
    rate_reset_at: str | None = None
