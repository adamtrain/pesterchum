"""Command line entry point and the polling loop."""

from __future__ import annotations

import argparse
import re
import shlex
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from pesterchum import __version__
from pesterchum.ci import failure_context
from pesterchum.detect import Events, Options, detect, mark_reported
from pesterchum.github import GhError, current_branch_pr, fetch_snapshot
from pesterchum.local import commit_is_local
from pesterchum.models import Snapshot
from pesterchum.report import IDLE, TIMEOUT, render_json, render_text, status_of
from pesterchum.state import State, default_state_dir, last_report_path, state_path, write_atomic

PR_URL = re.compile(r"^https?://github\.com/([^/\s]+/[^/\s]+)/pull/(\d+)")
REPO_AND_NUMBER = re.compile(r"^([^/\s#]+/[^/\s#]+)#(\d+)$")
REPO = re.compile(r"^[^/\s#]+/[^/\s#]+$")

LOW_RATE_LIMIT = 100
MAX_BACKOFF_SECONDS = 900

DESCRIPTION = """\
Block until a GitHub pull request needs attention, then print what happened and exit.

Wakes on: a new comment or review, a failed required check (once every required check
has finished, or at once with --fail-fast), a merge conflict, somebody else pushing to
the branch, or the PR being merged, closed, or added to the merge queue (with
--through-queue: removed from the queue instead). Otherwise it waits indefinitely, so
run it in the background.

The first line of output is `PESTERCHUM: <STATUS>` where STATUS is one of
ACTION_REQUIRED, READY (only with --wake-on-ready), MERGED, QUEUED, CLOSED, or TIMEOUT.
The exit status is 0 for all of these and 1 for errors.

A cursor is kept per PR under ~/.cache/pesterchum, so running the same command again
reports only what is new since the last report, including anything that arrived while
you were not watching. If a report was lost, --last prints the most recent one again.
"""


def parse_target(target: list[str]) -> tuple[str, int]:
    if len(target) == 1:
        match = PR_URL.match(target[0]) or REPO_AND_NUMBER.match(target[0])
        if match:
            return match.group(1), int(match.group(2))
    elif len(target) == 2 and REPO.match(target[0]) and target[1].lstrip("#").isdigit():
        return target[0], int(target[1].lstrip("#"))
    raise ValueError("expected OWNER/REPO NUMBER, OWNER/REPO#NUMBER, or a pull request URL")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pesterchum", description=DESCRIPTION, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "target", nargs="*", metavar="PR",
        help="OWNER/REPO NUMBER, OWNER/REPO#NUMBER, or a PR URL (default: the pull request for the current branch)",
    )
    parser.add_argument("--interval", type=float, default=60, metavar="SECONDS", help="seconds between polls (default: 60)")
    parser.add_argument(
        "--slow-interval", type=float, default=600, metavar="SECONDS",
        help="seconds between polls once the watch has run for --slow-after (default: 600)",
    )
    parser.add_argument(
        "--slow-after", type=float, default=3600, metavar="SECONDS",
        help="how long to poll at --interval before dropping to --slow-interval (default: 3600)",
    )
    parser.add_argument(
        "--settle", type=float, default=20, metavar="SECONDS",
        help="after first noticing something, wait this long and poll once more so that "
        "comments arriving together land in one report (default: 20)",
    )
    parser.add_argument("--timeout", type=float, default=0, metavar="SECONDS", help="give up after this long (default: never)")
    parser.add_argument("--wake-on-edits", action="store_true", help="also wake when an already-reported comment is edited")
    parser.add_argument("--include-self", action="store_true", help="also wake on comments by the authenticated gh user")
    parser.add_argument(
        "--ignore-author", action="append", default=[], metavar="LOGIN",
        help="never wake on comments by this login (repeatable; omit any [bot] suffix)",
    )
    parser.add_argument("--all-checks", action="store_true", help="watch every check, not only those required to merge")
    parser.add_argument(
        "--fail-fast", action="store_true",
        help="report a failed check as soon as it fails, instead of waiting for the other checks to finish",
    )
    parser.add_argument(
        "--through-queue", action="store_true",
        help="keep watching while the PR is in the merge queue, until it merges or is removed from the queue",
    )
    parser.add_argument(
        "--wake-on-ready", action="store_true",
        help="also wake, with status READY, when the PR becomes ready to merge",
    )
    parser.add_argument("--once", action="store_true", help="poll a single time and report, instead of waiting")
    parser.add_argument(
        "--last", action="store_true",
        help="print the most recent report for this PR again, without polling, and exit",
    )
    parser.add_argument("--reset", action="store_true", help="forget what was already reported for this PR")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument("--quiet", action="store_true", help="no progress lines on stderr")
    parser.add_argument("--state-dir", metavar="DIR", help="where to keep the per-PR cursor (default: ~/.cache/pesterchum)")
    parser.add_argument("--log-tokens", type=int, default=3000, metavar="N", help="token budget per Buildkite job log (default: 3000)")
    parser.add_argument("--log-lines", type=int, default=120, metavar="N", help="lines kept per GitHub Actions job log (default: 120)")
    parser.add_argument("--version", action="version", version=f"pesterchum {__version__}")
    return parser


def watch(
    fetch: Callable[[], Snapshot],
    state: State,
    options: Options,
    *,
    interval: float,
    settle: float,
    slow_interval: float = 600,
    slow_after: float = 3600,
    timeout: float = 0,
    once: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    log: Callable[[str], None] = lambda message: None,
) -> tuple[Snapshot, Events, str]:
    """Poll until there is something to report. Returns the snapshot, events and status."""
    started = clock()
    deadline = started + timeout if timeout > 0 else None
    fast = interval
    latest: tuple[Snapshot, Events] | None = None
    consecutive_errors = 0
    confirming = False
    progress = None

    while True:
        # Most of what happens to a PR happens soon after a push, so a watch that has
        # been quiet for a long time polls less often. Running it again starts afresh.
        if interval == fast and slow_interval > fast and clock() - started >= slow_after:
            interval = slow_interval
            log(f"watching for {slow_after:.0f}s; polling every {interval:.0f}s from now on")
        try:
            snapshot = fetch()
        except GhError as error:
            # A failure on the very first poll is most likely a bad argument or broken
            # setup, so surface it. Later ones are treated as blips and retried.
            if error.fatal or latest is None:
                raise
            consecutive_errors += 1
            delay = min(interval * 2 ** min(consecutive_errors, 4), MAX_BACKOFF_SECONDS)
            log(f"poll failed ({error}); retrying in {delay:.0f}s")
            if deadline is not None and clock() >= deadline:
                return (*latest, TIMEOUT)
            sleep(delay)
            continue

        consecutive_errors = 0
        events = detect(snapshot, state, options)
        if not events.head_moved_from:
            # Either the first look at the PR or a commit of our own: the new baseline.
            state.head = snapshot.head_oid
        if snapshot.in_merge_queue and not state.queued:
            # Remembered straight away, so that a watcher started later still notices
            # the PR being removed from the queue.
            state.queued = True
            state.save()
        latest = (snapshot, events)

        if events.terminal or once:
            return snapshot, events, status_of(events)

        if events.needs_action or events.ready:
            if confirming or settle <= 0:
                return snapshot, events, status_of(events)
            confirming = True
            log(f"something changed; checking again in {settle:.0f}s before reporting")
            sleep(settle)
            continue
        confirming = False

        summary = f"checks: {events.passed} passed, {events.failed} failed, {events.pending} running"
        if snapshot.in_merge_queue:
            summary = f"in the merge queue at position {snapshot.merge_queue_position}; {summary}"
        if summary != progress:
            progress = summary
            log(summary)

        if deadline is not None and clock() >= deadline:
            return snapshot, events, TIMEOUT
        sleep(_next_delay(snapshot, interval, log))


def _next_delay(snapshot: Snapshot, interval: float, log: Callable[[str], None]) -> float:
    """The poll interval, stretched to the rate-limit reset when the budget is nearly spent."""
    if snapshot.rate_remaining is None or snapshot.rate_remaining >= LOW_RATE_LIMIT or not snapshot.rate_reset_at:
        return interval
    try:
        reset = datetime.fromisoformat(snapshot.rate_reset_at.replace("Z", "+00:00"))
    except ValueError:
        return interval
    wait = (reset - datetime.now(timezone.utc)).total_seconds() + 5
    if wait <= interval:
        return interval
    log(f"GitHub API budget is nearly spent ({snapshot.rate_remaining} left); pausing {wait:.0f}s until it resets")
    return wait


def _replay(report: Path, pr: str, log: Callable[[str], None]) -> int:
    try:
        text = report.read_text()
        saved = datetime.fromtimestamp(report.stat().st_mtime).astimezone()
    except FileNotFoundError:
        print(f"pesterchum: error: no report has been saved for {pr} yet", file=sys.stderr)
        return 1
    log(f"replaying the report for {pr} saved at {saved:%Y-%m-%d %H:%M:%S %Z}")
    sys.stdout.write(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.interval <= 0 or args.slow_interval <= 0:
        parser.error("--interval and --slow-interval must be greater than zero")
    try:
        repo, number = parse_target(args.target or [current_branch_pr()])
    except ValueError as error:
        parser.error(str(error))
    except GhError as error:
        print(f"pesterchum: error: {error}", file=sys.stderr)
        return 1

    def log(message: str) -> None:
        if not args.quiet:
            print(f"pesterchum: {message}", file=sys.stderr, flush=True)

    path = state_path(Path(args.state_dir) if args.state_dir else default_state_dir(), repo, number)
    if args.last:
        return _replay(last_report_path(path, args.json), f"{repo}#{number}", log)
    if args.reset:
        for stale in (path, last_report_path(path, True), last_report_path(path, False)):
            stale.unlink(missing_ok=True)
    try:
        state = State.load(path)
    except ValueError as error:
        print(f"pesterchum: error: {error}", file=sys.stderr)
        return 1

    options = Options(
        include_self=args.include_self,
        ignore_authors=frozenset(login.lower().removesuffix("[bot]").strip() for login in args.ignore_author),
        wake_on_edits=args.wake_on_edits,
        all_checks=args.all_checks,
        fail_fast=args.fail_fast,
        through_queue=args.through_queue,
        wake_on_ready=args.wake_on_ready,
        commit_is_local=lambda oid: commit_is_local(repo, oid),
    )

    log(f"watching {repo}#{number} every {args.interval:.0f}s (cursor: {path})")
    try:
        snapshot, events, status = watch(
            lambda: fetch_snapshot(repo, number), state, options,
            interval=args.interval, settle=args.settle, slow_interval=args.slow_interval, slow_after=args.slow_after,
            timeout=args.timeout, once=args.once, log=log,
        )
    except GhError as error:
        print(f"pesterchum: error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130

    contexts = {}
    for check in events.failed_checks:
        log(f"fetching failure details for {check.label}")
        contexts[check.failure_key(snapshot.head_oid)] = failure_context(check, repo, args.log_tokens, args.log_lines)

    # A PR taken from the current branch is spelled out, so the command still names the
    # same PR after the branch or the directory changes.
    named = [] if args.target else [repo, str(number)]
    rerun = "pesterchum " + shlex.join(named + [arg for arg in argv if arg not in ("--reset", "--once")])
    as_text = render_text(snapshot, events, status, contexts, rerun)
    as_json = render_json(snapshot, events, status, contexts)
    sys.stdout.write(as_json if args.json else as_text)
    sys.stdout.flush()

    # Advance the cursor only once the report is out, so a crash never swallows an event.
    # The report is kept as well, because the cursor will not offer these events again.
    if status not in (TIMEOUT, IDLE):
        write_atomic(last_report_path(path, False), as_text)
        write_atomic(last_report_path(path, True), as_json)
        mark_reported(snapshot, events, state)
        state.save()
    return 0
