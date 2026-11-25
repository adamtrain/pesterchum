"""Gather an explanation for a failed check: Buildkite (`bk`), GitHub Actions, or the check run itself (`gh`)."""

from __future__ import annotations

import html
import json
import re
import subprocess

from pesterchum.models import Check

BUILDKITE_URL = re.compile(r"https://buildkite\.com/([^/\s]+)/([^/\s]+)/builds/(\d+)")
ACTIONS_LOG_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:.]+Z ")
COMMAND_TIMEOUT_SECONDS = 120
MAX_FAILED_JOBS = 5
MAX_ANNOTATION_CHARS = 3000
MAX_OUTPUT_CHARS = 3000
MAX_CHECK_ANNOTATIONS = 20
MAX_MESSAGE_CHARS = 400
ANNOTATION_LEVELS = {"failure": 0, "warning": 1, "notice": 2}
# Actions attaches this to every failed job; the log excerpt already says as much.
ACTIONS_NOISE = re.compile(r"^Process completed with exit code \d+\.?$")


def failure_context(check: Check, repo: str, log_tokens: int = 3000, log_lines: int = 120) -> str:
    """Markdown describing why `check` failed. Never raises: problems become a note."""
    try:
        match = BUILDKITE_URL.search(check.url or "")
        if match:
            return _buildkite(match.group(1), match.group(2), match.group(3), log_tokens)
        if check.kind == "check_run" and check.app == "github-actions" and check.job_id:
            return _actions(repo, check.job_id, log_lines)
        if check.kind == "check_run" and check.job_id:
            # Any other app: all there is to go on is what it wrote on the check run.
            output = _check_run_output(repo, check.job_id)
            if output:
                return output
    except _CommandError as error:
        return f"_Could not fetch failure details: {error}_"
    if check.kind == "status" and check.description:
        # A plain commit status: its description, printed with the check, is all there is.
        return ""
    return "_No log source is known for this check; open its link for details._"


class _CommandError(Exception):
    pass


def _run(cmd: list[str], allow_failure: bool = False) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=COMMAND_TIMEOUT_SECONDS)
    except FileNotFoundError:
        raise _CommandError(f"the `{cmd[0]}` CLI is not installed or not on PATH") from None
    except subprocess.TimeoutExpired:
        raise _CommandError(f"`{' '.join(cmd[:3])}` timed out") from None
    if proc.returncode != 0 and not allow_failure:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        raise _CommandError(f"`{' '.join(cmd[:3])}` failed: {detail[-1] if detail else proc.returncode}")
    return proc.stdout


def _buildkite(org: str, pipeline: str, number: str, log_tokens: int) -> str:
    build = f"{org}/{pipeline}/{number}"
    # --recursive follows triggered builds and returns only jobs that hard-failed or timed
    # out. It exits non-zero when the tree is incomplete but still prints what it found.
    raw = _run(["bk", "build", "view", build, "--recursive", "--json", "--no-input"], allow_failure=True)
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        raise _CommandError(f"`bk build view {build}` returned no usable output") from None

    failures = result.get("failures") or []
    parts: list[str] = []
    if not failures:
        parts.append("_Buildkite reports no hard-failed jobs for this build (it may have been cancelled or blocked)._")
    for job in failures[:MAX_FAILED_JOBS]:
        heading = f"#### Buildkite job: {job.get('name') or job.get('id')} ({job.get('state')})"
        if job.get("build") and job["build"] != build:
            heading += f" in triggered build {job['build']}"
        parts.append(heading)
        if job.get("web_url"):
            parts.append(job["web_url"])
        try:
            log = _run([
                "bk", "job", "log", job["id"], "--agent", "--format", "markdown",
                "--max-tokens", str(log_tokens), "--no-input",
            ])
            parts.append(trim_buildkite_log(log) or "_(empty log)_")
        except _CommandError as error:
            parts.append(f"_Could not fetch this job's log: {error}_")
    if len(failures) > MAX_FAILED_JOBS:
        parts.append(f"_…and {len(failures) - MAX_FAILED_JOBS} more failed jobs not shown._")
    if result.get("complete") is False:
        parts.append("_Buildkite could not walk the whole trigger tree; this list may be incomplete._")

    annotations = _buildkite_annotations(org, pipeline, number)
    if annotations:
        parts.append("#### Buildkite error annotations")
        parts.append(annotations)
    return "\n\n".join(parts)


BOILERPLATE_SECTION = re.compile(r"^(Preparing |Setting up |Running (agent|plugin|global|local|repository) .*hook)")
ERROR_MARKER = re.compile(r"\berror\b|\bfatal\b|🚨|exit(ed with)? status [1-9]", re.IGNORECASE)


def trim_buildkite_log(log: str) -> str:
    """Drop agent setup and hook sections that show no error, and flatten the headings.

    `bk job log --agent --format markdown` emits one `## section` per log group. Most are
    hook and checkout boilerplate, and their headings would collide with the report's own.
    """
    sections: list[tuple[str | None, list[str]]] = [(None, [])]
    for line in log.strip().splitlines():
        if line.startswith("## "):
            sections.append((line[3:].strip(), []))
        else:
            sections[-1][1].append(line)

    out: list[str] = []
    dropped = 0
    for heading, body in sections:
        text = "\n".join(body).strip()
        if heading is None:
            if text:
                out.append(text)
        elif BOILERPLATE_SECTION.match(heading) and not ERROR_MARKER.search(text):
            dropped += 1
        else:
            out.append(f"**{heading}**\n{text}" if text else f"**{heading}**")
    if dropped:
        out.append(f"_({dropped} setup and hook sections without errors omitted)_")
    return "\n\n".join(out)


def _buildkite_annotations(org: str, pipeline: str, number: str) -> str:
    try:
        raw = _run(["bk", "build", "annotations", number, "-p", f"{org}/{pipeline}", "--json", "--no-input"])
        annotations = json.loads(raw).get("annotations") or []
    except (_CommandError, json.JSONDecodeError):
        return ""
    texts = [_html_to_text(a.get("body_html") or "") for a in annotations if a.get("style") == "error"]
    joined = "\n\n".join(text for text in texts if text)
    if len(joined) > MAX_ANNOTATION_CHARS:
        joined = joined[:MAX_ANNOTATION_CHARS] + "\n…(truncated)"
    return joined


def _html_to_text(body: str) -> str:
    body = re.sub(r"<(br|/p|/li|/h\d|/tr|/div)\s*/?>", "\n", body, flags=re.IGNORECASE)
    body = html.unescape(re.sub(r"<[^>]+>", "", body))
    return "\n".join(line.strip() for line in body.splitlines() if line.strip())


def _actions(repo: str, job_id: int, log_lines: int) -> str:
    raw = _run(["gh", "run", "view", "--repo", repo, "--job", str(job_id), "--log-failed"])
    lines = []
    for line in raw.splitlines():
        # Each line is "job<TAB>step<TAB>timestamp message"; keep only the message.
        message = line.split("\t", 2)[-1].lstrip("﻿")
        lines.append(ACTIONS_LOG_PREFIX.sub("", message))
    if not lines:
        return "_GitHub Actions returned no failed-step log for this job._"
    omitted = len(lines) - log_lines
    tail = lines[-log_lines:]
    note = f"_(last {log_lines} of {len(lines)} failed-step log lines)_\n\n" if omitted > 0 else ""
    log = f"{note}```\n" + "\n".join(tail) + "\n```"
    # Errors that a step raised with `::error file=...` point at the file and line.
    try:
        annotations = _check_annotations(repo, job_id, failures_only=True)
    except _CommandError:
        annotations = ""
    return f"{annotations}\n\n{log}" if annotations else log


def _check_run_output(repo: str, check_run_id: int) -> str:
    """What the app behind a check run wrote about it: its summary, its text and its annotations."""
    try:
        output = json.loads(_run(["gh", "api", f"repos/{repo}/check-runs/{check_run_id}"])).get("output") or {}
    except json.JSONDecodeError:
        raise _CommandError("`gh api` returned no usable check run") from None
    parts = []
    for key in ("summary", "text"):
        body = (output.get(key) or "").strip()
        if body:
            if len(body) > MAX_OUTPUT_CHARS:
                body = body[:MAX_OUTPUT_CHARS] + "\n…(truncated)"
            # Quoted, so that the app's own headings cannot be mistaken for the report's.
            parts.append("\n".join(f"> {line}".rstrip() for line in body.splitlines()))
    if output.get("annotations_count"):
        try:
            parts.append(_check_annotations(repo, check_run_id))
        except _CommandError as error:
            parts.append(f"_Could not fetch this check's annotations: {error}_")
    return "\n\n".join(part for part in parts if part)


def _check_annotations(repo: str, check_run_id: int, failures_only: bool = False) -> str:
    raw = _run(["gh", "api", f"repos/{repo}/check-runs/{check_run_id}/annotations?per_page=100"])
    try:
        annotations = [a for a in json.loads(raw) if isinstance(a, dict)]
    except (json.JSONDecodeError, TypeError):
        raise _CommandError("`gh api` returned no usable annotations") from None
    if failures_only:
        annotations = [
            a for a in annotations
            if a.get("annotation_level") == "failure" and not ACTIONS_NOISE.match((a.get("message") or "").strip())
        ]
    if not annotations:
        return ""
    # The sort is stable, so within a level the annotations stay in the order GitHub gave.
    annotations.sort(key=lambda a: ANNOTATION_LEVELS.get(a.get("annotation_level"), len(ANNOTATION_LEVELS)))
    shown = annotations[:MAX_CHECK_ANNOTATIONS]
    count = f"{len(shown)} of {len(annotations)}" if len(annotations) > len(shown) else str(len(shown))
    lines = [f"#### Annotations ({count})"]
    for annotation in shown:
        where = annotation.get("path") or "(no file)"
        if annotation.get("start_line"):
            where += f":{annotation['start_line']}"
        message = " ".join(" ".join(filter(None, [annotation.get("title"), annotation.get("message")])).split())
        if len(message) > MAX_MESSAGE_CHARS:
            message = message[:MAX_MESSAGE_CHARS] + "…"
        lines.append(f"- `{where}` {annotation.get('annotation_level') or 'note'}: {message or '(no message)'}")
    return "\n".join(lines)
