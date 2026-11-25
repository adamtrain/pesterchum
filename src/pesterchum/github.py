"""Fetch one pull request snapshot with a single `gh api graphql` call."""

from __future__ import annotations

import json
import subprocess

from pesterchum.models import FAIL, PASS, PENDING, Check, Comment, Snapshot

QUERY = """
query($owner: String!, $name: String!, $number: Int!, $checksCursor: String) {
  viewer { login }
  rateLimit { remaining resetAt }
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      url title state isDraft mergeable isInMergeQueue
      mergeQueueEntry { position }
      headRefOid baseRefName
      timelineItems(last: 1, itemTypes: [REMOVED_FROM_MERGE_QUEUE_EVENT]) {
        nodes { ... on RemovedFromMergeQueueEvent { reason } }
      }
      comments(last: 100) {
        nodes {
          id url body createdAt lastEditedAt isMinimized
          author { login __typename }
        }
      }
      reviews(last: 100) {
        nodes {
          id url body state submittedAt lastEditedAt
          author { login __typename }
        }
      }
      reviewThreads(last: 100) {
        nodes {
          isResolved isOutdated path line originalLine
          comments(last: 50) {
            nodes {
              id url body createdAt lastEditedAt isMinimized diffHunk
              author { login __typename }
            }
          }
        }
      }
      commits(last: 1) {
        nodes {
          commit {
            statusCheckRollup {
              contexts(first: 100, after: $checksCursor) {
                pageInfo { hasNextPage endCursor }
                nodes {
                  __typename
                  ... on CheckRun {
                    name status conclusion detailsUrl databaseId title startedAt completedAt
                    isRequired(pullRequestNumber: $number)
                    checkSuite {
                      app { slug }
                      workflowRun { workflow { name } }
                    }
                  }
                  ... on StatusContext {
                    context state targetUrl description createdAt
                    isRequired(pullRequestNumber: $number)
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""

# Errors that will not fix themselves by polling again.
FATAL_MARKERS = (
    "Could not resolve to a",
    "gh auth login",
    "HTTP 401",
    "Bad credentials",
    "SAML enforcement",
)

FAILED_CONCLUSIONS = {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"}
GH_TIMEOUT_SECONDS = 60
EXCERPT_CHARS = 300


class GhError(Exception):
    def __init__(self, message: str, fatal: bool = False):
        super().__init__(message)
        self.fatal = fatal


def _graphql(owner: str, name: str, number: int, cursor: str | None) -> dict:
    cmd = [
        "gh", "api", "graphql",
        "-f", f"query={QUERY}",
        "-f", f"owner={owner}",
        "-f", f"name={name}",
        "-F", f"number={number}",
    ]
    if cursor:
        cmd += ["-f", f"checksCursor={cursor}"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=GH_TIMEOUT_SECONDS)
    except FileNotFoundError:
        raise GhError("the `gh` CLI is not installed or not on PATH", fatal=True) from None
    except subprocess.TimeoutExpired:
        raise GhError(f"`gh api graphql` timed out after {GH_TIMEOUT_SECONDS}s") from None
    if proc.returncode != 0:
        message = (proc.stderr or proc.stdout).strip() or f"gh exited with status {proc.returncode}"
        raise GhError(message, fatal=any(marker in message for marker in FATAL_MARKERS))
    try:
        return json.loads(proc.stdout)["data"]
    except (json.JSONDecodeError, KeyError, TypeError):
        raise GhError("could not parse the response from `gh api graphql`") from None


def fetch_snapshot(repo: str, number: int) -> Snapshot:
    owner, name = repo.split("/", 1)
    data = _graphql(owner, name, number, None)
    pr = (data.get("repository") or {}).get("pullRequest")
    if pr is None:
        raise GhError(f"pull request {repo}#{number} not found", fatal=True)

    # Monorepos can have more than 100 checks on a commit; page through the rest.
    nodes = _contexts(pr)["nodes"]
    page = _contexts(pr)["pageInfo"]
    while page.get("hasNextPage"):
        more = _graphql(owner, name, number, page["endCursor"])
        contexts = _contexts(more["repository"]["pullRequest"])
        nodes.extend(contexts["nodes"])
        page = contexts["pageInfo"]

    return parse_snapshot(data, repo, number)


def _contexts(pr: dict) -> dict:
    commits = pr["commits"]["nodes"]
    rollup = commits[0]["commit"]["statusCheckRollup"] if commits else None
    if rollup is None:
        # No checks have reported on this commit yet. Give callers a stable shape to extend.
        empty = {"nodes": [], "pageInfo": {"hasNextPage": False}}
        if commits:
            commits[0]["commit"]["statusCheckRollup"] = {"contexts": empty}
        return empty
    return rollup["contexts"]


def parse_snapshot(data: dict, repo: str, number: int) -> Snapshot:
    pr = data["repository"]["pullRequest"]
    rate = data.get("rateLimit") or {}
    queue_entry = pr.get("mergeQueueEntry") or {}
    removals = (pr.get("timelineItems") or {}).get("nodes") or []
    return Snapshot(
        repo=repo,
        number=number,
        url=pr["url"],
        title=pr["title"],
        state=pr["state"],
        is_draft=pr["isDraft"],
        mergeable=pr["mergeable"],
        in_merge_queue=bool(pr.get("isInMergeQueue")),
        merge_queue_position=queue_entry.get("position"),
        dequeue_reason=(removals[-1] or {}).get("reason") if removals else None,
        head_oid=pr["headRefOid"],
        base_ref=pr["baseRefName"],
        viewer=data["viewer"]["login"],
        comments=_parse_comments(pr),
        checks=_parse_checks(_contexts(pr)["nodes"]),
        rate_remaining=rate.get("remaining"),
        rate_reset_at=rate.get("resetAt"),
    )


def _author(node: dict) -> tuple[str, bool]:
    author = node.get("author") or {}
    return author.get("login") or "ghost", author.get("__typename") == "Bot"


def _parse_comments(pr: dict) -> list[Comment]:
    comments: list[Comment] = []

    for node in pr["comments"]["nodes"]:
        if node.get("isMinimized"):
            continue
        login, is_bot = _author(node)
        comments.append(Comment(
            id=node["id"], kind="issue", login=login, is_bot=is_bot, body=node["body"] or "",
            url=node["url"], created_at=node["createdAt"], edited_at=node.get("lastEditedAt"),
        ))

    for node in pr["reviews"]["nodes"]:
        body = (node["body"] or "").strip()
        # A pending review is an unsubmitted draft. A review with no body is only worth
        # reporting when it requests changes; its inline comments are reported separately.
        if node["state"] == "PENDING" or (not body and node["state"] != "CHANGES_REQUESTED"):
            continue
        login, is_bot = _author(node)
        comments.append(Comment(
            id=node["id"], kind="review", login=login, is_bot=is_bot, body=body, url=node["url"],
            created_at=node.get("submittedAt") or "", edited_at=node.get("lastEditedAt"),
            review_state=node["state"],
        ))

    for thread in pr["reviewThreads"]["nodes"]:
        previous: Comment | None = None
        for node in thread["comments"]["nodes"]:
            login, is_bot = _author(node)
            comment = Comment(
                id=node["id"], kind="inline", login=login, is_bot=is_bot, body=node["body"] or "",
                url=node["url"], created_at=node["createdAt"], edited_at=node.get("lastEditedAt"),
                path=thread.get("path"), line=thread.get("line") or thread.get("originalLine"),
                diff_hunk=node.get("diffHunk"),
                thread_resolved=bool(thread.get("isResolved")),
                thread_outdated=bool(thread.get("isOutdated")),
            )
            if previous is not None:
                comment.reply_to_login = previous.author
                comment.reply_to_excerpt = _excerpt(previous.body)
            previous = comment
            if not node.get("isMinimized"):
                comments.append(comment)

    comments.sort(key=lambda c: c.created_at)
    return comments


def _excerpt(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= EXCERPT_CHARS else flat[:EXCERPT_CHARS] + "…"


def _parse_checks(nodes: list[dict]) -> list[Check]:
    # The rollup lists every run on the commit, so one check can appear several times
    # (a cancelled run and its replacement, say). Only the newest run of each counts.
    latest: dict[tuple, Check] = {}
    for node in nodes:
        check = _parse_check(node)
        key = (check.kind, check.workflow, check.name)
        current = latest.get(key)
        if current is None or check.timestamp >= current.timestamp:
            latest[key] = check
    return list(latest.values())


def _parse_check(node: dict) -> Check:
    if node["__typename"] == "CheckRun":
        suite = node.get("checkSuite") or {}
        workflow_run = suite.get("workflowRun") or {}
        if node["status"] != "COMPLETED":
            state, raw = PENDING, node["status"]
        else:
            raw = node.get("conclusion") or "UNKNOWN"
            state = FAIL if raw in FAILED_CONCLUSIONS else PASS
        return Check(
            name=node["name"], kind="check_run", state=state, raw_state=raw,
            required=bool(node.get("isRequired")), url=node.get("detailsUrl"),
            description=node.get("title"),
            workflow=(workflow_run.get("workflow") or {}).get("name"),
            app=(suite.get("app") or {}).get("slug"),
            job_id=node.get("databaseId"),
            timestamp=node.get("startedAt") or node.get("completedAt") or "",
        )

    raw = node["state"]
    state = PASS if raw == "SUCCESS" else FAIL if raw in ("FAILURE", "ERROR") else PENDING
    return Check(
        name=node["context"], kind="status", state=state, raw_state=raw,
        required=bool(node.get("isRequired")), url=node.get("targetUrl"),
        description=node.get("description"), timestamp=node.get("createdAt") or "",
    )
