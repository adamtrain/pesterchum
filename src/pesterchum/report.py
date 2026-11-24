"""Render what happened as text for the calling agent, or as JSON."""

from __future__ import annotations

import json
from dataclasses import asdict

from pesterchum.detect import CLOSED, MERGED, QUEUED, Events
from pesterchum.models import Check, Comment, Snapshot

ACTION_REQUIRED = "ACTION_REQUIRED"
TIMEOUT = "TIMEOUT"
IDLE = "IDLE"

MAX_BODY_CHARS = 6000
DIFF_HUNK_LINES = 8

HEADLINES = {
    MERGED: "The pull request has merged. Nothing further to watch.",
    CLOSED: "The pull request was closed without merging. Nothing further to watch.",
    QUEUED: "The pull request is in the merge queue. Watching has stopped; it is no longer waiting on review.",
    TIMEOUT: "The --timeout elapsed with nothing to report. Run the same command again to keep watching.",
    IDLE: "Nothing to report right now.",
}


def status_of(events: Events) -> str:
    return events.terminal or (ACTION_REQUIRED if events.needs_action else IDLE)


def render_text(snapshot: Snapshot, events: Events, status: str, contexts: dict[str, str], rerun: str) -> str:
    out: list[str] = [f"PESTERCHUM: {status}", ""]
    out.append(f"PR: {snapshot.repo}#{snapshot.number} — {snapshot.title}")
    out.append(snapshot.url)
    flags = [f"head {snapshot.head_oid[:10]}", f"base {snapshot.base_ref}"]
    if snapshot.is_draft:
        flags.append("draft")
    if snapshot.in_merge_queue and snapshot.merge_queue_position is not None:
        flags.append(f"merge queue position {snapshot.merge_queue_position}")
    out.append(" · ".join(flags))
    out.append("")

    if status in HEADLINES:
        out.append(HEADLINES[status])
    else:
        out.append("Needs attention: " + ", ".join(_reasons(events)) + ".")
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

    if events.passed or events.failed or events.pending:
        out.append("## Check status")
        out.append(f"{events.passed} passed, {events.failed} failed, {events.pending} still running.")
        if events.failed and not events.failed_checks:
            reason = "other checks are still running" if events.pending else "already reported for this commit"
            out.append(f"Failures are not detailed above because {reason}.")
        out.append("")

    if status == ACTION_REQUIRED:
        out.append("## Next")
        out.append(f"After dealing with the above, run `{rerun}` again to keep watching.")
        out.append("")

    return "\n".join(out).rstrip() + "\n"


def _reasons(events: Events) -> list[str]:
    reasons = []
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
    required = "required" if check.required else "not required"
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

    out = [f"### {title}", f"{comment.created_at} · {comment.url}", ""]
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
            "in_merge_queue": snapshot.in_merge_queue,
            "head_oid": snapshot.head_oid,
            "base_ref": snapshot.base_ref,
        },
        "merge_conflict": events.conflict,
        "failed_checks": [
            {**asdict(check), "context": contexts.get(check.failure_key(snapshot.head_oid), "")}
            for check in events.failed_checks
        ],
        "new_comments": [asdict(comment) for comment in events.new_comments],
        "edited_comments": [asdict(comment) for comment in events.edited_comments],
        "checks": {"passed": events.passed, "failed": events.failed, "pending": events.pending},
    }
    return json.dumps(payload, indent=2) + "\n"
