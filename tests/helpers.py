"""Builders for the GraphQL response shape and for snapshots."""

from __future__ import annotations

from pesterchum.github import parse_snapshot
from pesterchum.models import Snapshot

HEAD = "a" * 40


def author(login: str, bot: bool = False) -> dict:
    return {"login": login, "__typename": "Bot" if bot else "User"}


def issue_comment(id: str, login: str = "reviewer", body: str = "hello", bot: bool = False, edited: str | None = None) -> dict:
    return {
        "id": id, "url": f"https://example.test/{id}", "body": body, "createdAt": f"2026-01-01T00:00:{len(id):02d}Z",
        "lastEditedAt": edited, "isMinimized": False, "author": author(login, bot),
    }


def review(id: str, state: str = "COMMENTED", body: str = "", login: str = "reviewer") -> dict:
    return {
        "id": id, "url": f"https://example.test/{id}", "body": body, "state": state,
        "submittedAt": "2026-01-01T00:01:00Z", "lastEditedAt": None, "author": author(login),
    }


def thread(path: str, line: int, comments: list[dict], resolved: bool = False) -> dict:
    return {
        "isResolved": resolved, "isOutdated": False, "path": path, "line": line, "originalLine": line,
        "comments": {"nodes": [{**c, "diffHunk": "@@ -1 +1 @@\n+new line"} for c in comments]},
    }


def check_run(name: str, status: str = "COMPLETED", conclusion: str | None = "SUCCESS", required: bool = True,
              workflow: str | None = "CI", job_id: int = 1, started: str = "2026-01-01T00:00:00Z") -> dict:
    return {
        "__typename": "CheckRun", "name": name, "status": status, "conclusion": conclusion,
        "detailsUrl": f"https://github.com/o/r/actions/runs/1/job/{job_id}", "databaseId": job_id, "title": None,
        "startedAt": started, "completedAt": None, "isRequired": required,
        "checkSuite": {"app": {"slug": "github-actions"}, "workflowRun": {"workflow": {"name": workflow}} if workflow else None},
    }


def status_context(context: str, state: str = "SUCCESS", required: bool = True, url: str | None = None,
                   created: str = "2026-01-01T00:00:00Z") -> dict:
    return {
        "__typename": "StatusContext", "context": context, "state": state, "targetUrl": url,
        "description": f"{context} {state.lower()}", "createdAt": created, "isRequired": required,
    }


def response(*, comments=(), reviews=(), threads=(), checks=(), state="OPEN", mergeable="MERGEABLE",
             in_queue=False, head=HEAD, viewer="agent", remaining=5000, dequeue_reason=None,
             draft=True, merge_state="BLOCKED", review_decision=None, head_author=None) -> dict:
    return {
        "viewer": {"login": viewer},
        "rateLimit": {"remaining": remaining, "resetAt": "2026-01-01T01:00:00Z"},
        "repository": {"pullRequest": {
            "url": "https://github.com/o/r/pull/7", "title": "A change", "state": state, "isDraft": draft,
            "mergeable": mergeable, "mergeStateStatus": merge_state, "reviewDecision": review_decision, "isInMergeQueue": in_queue,
            "mergeQueueEntry": {"position": 2} if in_queue else None,
            "headRefOid": head, "baseRefName": "main",
            "timelineItems": {"nodes": [{"reason": dequeue_reason}] if dequeue_reason else []},
            "comments": {"nodes": list(comments)},
            "reviews": {"nodes": list(reviews)},
            "reviewThreads": {"nodes": list(threads)},
            "commits": {"nodes": [{"commit": {
                "messageHeadline": "Apply suggestion",
                "author": {"name": "Some Name", "user": {"login": head_author} if head_author else None},
                "statusCheckRollup": {
                "contexts": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": list(checks)},
            }}}]},
        }},
    }


def snapshot(**kwargs) -> Snapshot:
    return parse_snapshot(response(**kwargs), "o/r", 7)
