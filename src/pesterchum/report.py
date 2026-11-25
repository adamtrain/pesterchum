"""Render what happened as text for the calling agent, or as JSON."""

from __future__ import annotations

import json
from dataclasses import asdict

from pesterchum.detect import CLOSED, MERGED, QUEUED, Events
from pesterchum.models import Check, Comment, Snapshot

ACTION_REQUIRED = "ACTION_REQUIRED"
READY = "READY"
TIMEOUT = "TIMEOUT"
IDLE = "IDLE"

MAX_BODY_CHARS = 6000
DIFF_HUNK_LINES = 8

HEADLINES = {
    MERGED: "The pull request has merged. Nothing further to watch.",
    CLOSED: "The pull request was closed without merging. Nothing further to watch.",
    QUEUED: (
        "The pull request is in the merge queue. Watching has stopped; it is no longer waiting on review. "
        "Run again with --through-queue to wait until it merges or is removed from the queue."
    ),
    READY: (
        "The pull request is ready to merge: the required checks have passed, the required reviews "
        "are in, and it does not conflict with its base. It has not been merged."
    ),
    TIMEOUT: "The --timeout elapsed with nothing to report. Run the same command again to keep watching.",
    IDLE: "Nothing to report right now.",
}


THREAD_COMMANDS = """\
Reply in a thread, or resolve it, with the thread id shown on the comment:
```bash
gh api graphql -f t=THREAD_ID -f b='the reply' -f query='mutation($t: ID!, $b: String!) { addPullRequestReviewThreadReply(input: {pullRequestReviewThreadId: $t, body: $b}) { comment { url } } }'
gh api graphql -f t=THREAD_ID -f query='mutation($t: ID!) { resolveReviewThread(input: {threadId: $t}) { thread { isResolved } } }'
```"""


def status_of(events: Events) -> str:
    if events.terminal:
        return events.terminal
    if events.needs_action:
        return ACTION_REQUIRED
    return READY if events.ready else IDLE


def render_text(snapshot: Snapshot, events: Events, status: str, contexts: dict[str, str], rerun: str) -> str:
    out: list[str] = [f"PESTERCHUM: {status}", ""]
    out.append(f"PR: {snapshot.repo}#{snapshot.number} — {snapshot.title}")
    out.append(snapshot.url)
    flags = [f"head {snapshot.head_oid[:10]}", f"base {snapshot.base_ref}"]
    if snapshot.is_draft:
        flags.append("draft")
    if snapshot.review_decision:
        flags.append("review " + snapshot.review_decision.lower().replace("_", " "))
    if snapshot.in_merge_queue and snapshot.merge_queue_position is not None:
        flags.append(f"merge queue position {snapshot.merge_queue_position}")
    out.append(" · ".join(flags))
    out.append("")

    if status in HEADLINES:
        out.append(HEADLINES[status])
    else:
        out.append("Needs attention: " + ", ".join(_reasons(events)) + ".")
        if events.ready:
            out.append("Apart from that, the pull request is ready to merge.")
    out.append("")

    if events.dequeued:
        out.append("## Removed from the merge queue")
        reason = f" GitHub's reason: {snapshot.dequeue_reason}." if snapshot.dequeue_reason else ""
        out.append(
            f"The pull request was in the merge queue and left it without merging.{reason} "
            "It has to be queued again once whatever removed it is fixed."
        )
        out.append("")

    if events.head_moved_from:
        author = snapshot.head_author_login or snapshot.head_author_name or "an unknown author"
        out.append("## Branch head changed")
        out.append(
            f"The branch moved from `{events.head_moved_from[:10]}` to `{snapshot.head_oid[:10]}` "
            f"(“{snapshot.head_headline}”, by {author}), which was not pushed from here. Fetch it and "
            "bring it into the local branch before changing or pushing anything, or the next push may discard it."
        )
        out.append("")

    if events.conflict:
        out.append("## Merge conflict")
        out.append(
            f"The branch conflicts with `{snapshot.base_ref}` and cannot merge until it is "
            "rebased or merged with the base branch and the conflicts are resolved."
        )
        out.append("")

    if events.failed_checks:
        out.append(f"## Failed checks ({len(events.failed_checks)})")
        out.append("")
        for check in events.failed_checks:
            out.extend(_render_check(check, contexts.get(check.failure_key(snapshot.head_oid), "")))

    if events.new_comments:
        out.append(f"## New comments ({len(events.new_comments)})")
        out.append("")
        for comment in events.new_comments:
            out.extend(_render_comment(comment))

    if events.edited_comments:
        out.append(f"## Edited comments ({len(events.edited_comments)})")
        out.append("")
        for comment in events.edited_comments:
            out.extend(_render_comment(comment))

    if any(comment.thread_id for comment in events.new_comments + events.edited_comments):
        out.extend(["## Replying to inline comments", THREAD_COMMANDS, ""])

    if events.passed or events.failed or events.pending:
        out.append("## Check status")
        out.append(f"{events.passed} passed, {events.failed} failed, {events.pending} still running.")
        if events.failed and not events.failed_checks:
            reason = "other checks are still running" if events.failures_held else "already reported for this commit"
            out.append(f"Failures are not detailed above because {reason}.")
        out.append("")

    if status == ACTION_REQUIRED:
        out.append("## Next")
        out.append(f"After dealing with the above, run `{rerun}` again to keep watching.")
        out.append("")
    elif status == READY:
        out.append("## Next")
        out.append(f"Run `{rerun}` again to keep watching until it merges.")
        out.append("")

    return "\n".join(out).rstrip() + "\n"


def _reasons(events: Events) -> list[str]:
    reasons = []
    if events.dequeued:
        reasons.append("removed from the merge queue")
    if events.head_moved_from:
        reasons.append("somebody else pushed to the branch")
    if events.conflict:
        reasons.append("merge conflict")
    if events.failed_checks:
        reasons.append(_count(len(events.failed_checks), "failed check"))
    if events.new_comments:
        reasons.append(_count(len(events.new_comments), "new comment"))
    if events.edited_comments:
        reasons.append(_count(len(events.edited_comments), "edited comment"))
    return reasons


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" + ("" if n == 1 else "s")


def _render_check(check: Check, context: str) -> list[str]:
    if check.required:
        required = "required"
    elif check.watched_by_name:
        # Said outright, so that "not required" is not read as "safe to ignore".
        required = "not marked required, but watched with --watch-check: treat it as blocking"
    else:
        required = "not required"
    out = [f"### {check.label} — {check.raw_state} ({required})"]
    if check.url:
        out.append(check.url)
    if check.description:
        out.append(check.description)
    out.append("")
    if context:
        out.append(context)
        out.append("")
    return out


def _render_comment(comment: Comment) -> list[str]:
    if comment.kind == "review":
        title = f"Review by {comment.author} — {comment.review_state}"
    elif comment.kind == "inline":
        where = comment.path or "(unknown file)"
        if comment.line:
            where += f":{comment.line}"
        title = f"Inline comment by {comment.author} on {where}"
        notes = [n for n, on in (("thread resolved", comment.thread_resolved), ("outdated", comment.thread_outdated)) if on]
        if notes:
            title += f" ({', '.join(notes)})"
    else:
        title = f"Comment by {comment.author}"

    ids = [f"{'review' if comment.kind == 'review' else 'comment'} id {comment.database_id or comment.id}"]
    if comment.thread_id:
        ids.append(f"thread id {comment.thread_id}")
    out = [f"### {title}", f"{comment.created_at} · {comment.url}", " · ".join(ids), ""]
    if comment.kind == "inline":
        if comment.reply_to_login:
            out.append(f"In reply to {comment.reply_to_login}: “{comment.reply_to_excerpt}”")
            out.append("")
        elif comment.diff_hunk:
            hunk = comment.diff_hunk.splitlines()[-DIFF_HUNK_LINES:]
            out.extend(["```diff", *hunk, "```", ""])
    body = comment.body.strip() or "(no text)"
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + f"\n…(truncated; full text at {comment.url})"
    out.extend([body, ""])
    return out


def render_json(snapshot: Snapshot, events: Events, status: str, contexts: dict[str, str]) -> str:
    payload = {
        "status": status,
        "pr": {
            "repo": snapshot.repo,
            "number": snapshot.number,
            "url": snapshot.url,
            "title": snapshot.title,
            "state": snapshot.state,
            "is_draft": snapshot.is_draft,
            "mergeable": snapshot.mergeable,
            "merge_state": snapshot.merge_state,
            "review_decision": snapshot.review_decision,
            "in_merge_queue": snapshot.in_merge_queue,
            "head_oid": snapshot.head_oid,
            "head_headline": snapshot.head_headline,
            "head_author": snapshot.head_author_login or snapshot.head_author_name,
            "base_ref": snapshot.base_ref,
        },
        "ready": events.ready,
        "head_moved_from": events.head_moved_from,
        "merge_conflict": events.conflict,
        "dequeued": events.dequeued,
        "dequeue_reason": snapshot.dequeue_reason if events.dequeued else None,
        "failed_checks": [
            {**asdict(check), "context": contexts.get(check.failure_key(snapshot.head_oid), "")}
            for check in events.failed_checks
        ],
        "new_comments": [asdict(comment) for comment in events.new_comments],
        "edited_comments": [asdict(comment) for comment in events.edited_comments],
        "checks": {"passed": events.passed, "failed": events.failed, "pending": events.pending},
    }
    return json.dumps(payload, indent=2) + "\n"
