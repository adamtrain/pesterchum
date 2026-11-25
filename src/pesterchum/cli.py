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
from pesterchum.github import GhError, fetch_snapshot
from pesterchum.models import Snapshot
from pesterchum.report import ACTION_REQUIRED, IDLE, TIMEOUT, render_json, render_text, status_of
from pesterchum.state import State, default_state_dir, state_path

PR_URL = re.compile(r"^https?://github\.com/([^/\s]+/[^/\s]+)/pull/(\d+)")
REPO_AND_NUMBER = re.compile(r"^([^/\s#]+/[^/\s#]+)#(\d+)$")
REPO = re.compile(r"^[^/\s#]+/[^/\s#]+$")

LOW_RATE_LIMIT = 100
MAX_BACKOFF_SECONDS = 900

DESCRIPTION = """\
Block until a GitHub pull request needs attention, then print what happened and exit.

Wakes on: a new comment or review, a failed required check (once every required check
has finished, or at once with --fail-fast), a merge conflict, or the PR being merged, closed, or added to the merge
queue. Otherwise it waits indefinitely, so run it in the background.

The first line of output is `PESTERCHUM: <STATUS>` where STATUS is one of
ACTION_REQUIRED, MERGED, QUEUED, CLOSED, or TIMEOUT. The exit status is 0 for all of
these and 1 for errors.

A cursor is kept per PR under ~/.cache/pesterchum, so running the same command again
reports only what is new since the last report, including anything that arrived while
you were not watching.
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
    parser.add_argument("target", nargs="+", metavar="PR", help="OWNER/REPO NUMBER, OWNER/REPO#NUMBER, or a PR URL")
    parser.add_argument("--interval", type=float, default=60, metavar="SECONDS", help="seconds between polls (default: 60)")
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
    parser.add_argument("--once", action="store_true", help="poll a single time and report, instead of waiting")
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
    timeout: float = 0,
    once: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    log: Callable[[str], None] = lambda message: None,
) -> tuple[Snapshot, Events, str]:
    """Poll until there is something to report. Returns the snapshot, events and status."""
    deadline = clock() + timeout if timeout > 0 else None
    latest: tuple[Snapshot, Events] | None = None
    consecutive_errors = 0
    confirming = False
    progress = None

    while True:
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
        latest = (snapshot, events)

        if events.terminal or once:
            return snapshot, events, status_of(events)

        if events.needs_action:
            if confirming or settle <= 0:
                return snapshot, events, ACTION_REQUIRED
            confirming = True
            log(f"something changed; checking again in {settle:.0f}s before reporting")
            sleep(settle)
            continue
        confirming = False

        summary = f"checks: {events.passed} passed, {events.failed} failed, {events.pending} running"
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


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        repo, number = parse_target(args.target)
    except ValueError as error:
        parser.error(str(error))

    def log(message: str) -> None:
        if not args.quiet:
            print(f"pesterchum: {message}", file=sys.stderr, flush=True)

    path = state_path(Path(args.state_dir) if args.state_dir else default_state_dir(), repo, number)
    if args.reset:
        path.unlink(missing_ok=True)
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
    )

    log(f"watching {repo}#{number} every {args.interval:.0f}s (cursor: {path})")
    try:
        snapshot, events, status = watch(
            lambda: fetch_snapshot(repo, number), state, options,
            interval=args.interval, settle=args.settle, timeout=args.timeout, once=args.once, log=log,
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

    if args.json:
        output = render_json(snapshot, events, status, contexts)
    else:
        rerun = "pesterchum " + shlex.join(arg for arg in argv if arg not in ("--reset", "--once"))
        output = render_text(snapshot, events, status, contexts, rerun)
    sys.stdout.write(output)
    sys.stdout.flush()

    # Advance the cursor only once the report is out, so a crash never swallows an event.
    if status not in (TIMEOUT, IDLE):
        mark_reported(snapshot, events, state)
        state.save()
    return 0
