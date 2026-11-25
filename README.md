# pesterchum

Block until a GitHub pull request needs attention, then print what happened and exit.

It is meant to be run by a coding agent that has just opened a PR. Instead of sleeping
for a fixed time and hoping the reviewers have finished, the agent runs `pesterchum`
and is woken the moment there is something to do.

The name is a homage to the chat client in Homestuck. This project is otherwise
unrelated to it, or to the real chat client of the same name.

## What wakes it

| Event | Status printed |
| --- | --- |
| A new PR comment, review, or inline review comment | `ACTION_REQUIRED` |
| A required check failed, and no required check is still running (or, with `--fail-fast`, as soon as it fails) | `ACTION_REQUIRED` |
| The branch conflicts with its base | `ACTION_REQUIRED` |
| Somebody else pushed to the branch | `ACTION_REQUIRED` |
| The PR became ready to merge, with `--wake-on-ready` | `READY` |
| The PR entered the merge queue | `QUEUED` |
| The PR left the merge queue without merging | `ACTION_REQUIRED` |
| The PR merged | `MERGED` |
| The PR was closed without merging | `CLOSED` |

Nothing else ends the wait. Passing checks, approvals with no text, and comments by the
authenticated `gh` user (the agent itself) are ignored, unless `--wake-on-ready` is
passed and they are what makes the PR mergeable.

## Install

Requires Python 3.11+, an authenticated [`gh`](https://cli.github.com), and, for
Buildkite failure details, an authenticated [`bk`](https://github.com/buildkite/cli).
There are no Python dependencies.

```bash
uv tool install /path/to/pesterchum
```

## Use

```bash
pesterchum OWNER/REPO 1234
```

`OWNER/REPO#1234` and a full PR URL work too. With no PR named, it watches the pull
request for the branch checked out in the current directory, as `gh pr view` finds it.
The first line of output is
`PESTERCHUM: <STATUS>`; the rest explains what happened: each new comment with its
author, location and link, and each failed check with the failing jobs and their logs
(from `bk` for Buildkite, `gh` for GitHub Actions). The exit status is 0 for every
status above and 1 for errors such as an unknown PR.

It waits indefinitely, so an agent should run it in the background and act when it
exits. For Claude Code that means `run_in_background: true` on the Bash call; the
foreground limit is ten minutes.

### Running it again

A cursor for each PR is kept under `~/.cache/pesterchum`. After handling a report, run
the same command again: it reports only what is new, including anything that arrived
while the agent was busy. The first run reports comments already on the PR.

A failed check is reported once per attempt, and a merge conflict once per head commit.
Pushing a fix or re-running the check makes it eligible to be reported again. Pass
`--reset` to forget the cursor and report everything afresh.

Each report is also saved beside the cursor. Because the cursor moves on as soon as a
report is printed, a report that was lost (truncated output, a context reset) would
otherwise be gone; `pesterchum OWNER/REPO NUMBER --last` prints it again.

The cursor also remembers that the PR was in the merge queue. If it is later removed
from the queue without merging, the next run reports that, with GitHub's reason, whether
or not `--through-queue` was passed.

### Suggested agent instructions

> After opening the PR, run `pesterchum OWNER/REPO NUMBER` in the background and wait
> for it to exit. If the first line is `PESTERCHUM: ACTION_REQUIRED`, address every
> item in the output, push, and run the same command again. Stop when the first line
> is `MERGED`, `QUEUED` or `CLOSED`.

### Pushes by somebody else

A reviewer committing a suggestion, or anyone pressing "Update branch", moves the head
to a commit the agent does not have, and the agent's next force-push would discard it.
So a new head commit wakes the watcher unless it is the agent's own. When the working
directory is a checkout of the repository, "own" means the commit is already in it;
otherwise it means the commit's author is the authenticated `gh` user. The agent's own
pushes never wake it, and the head as first seen is taken as the starting point.

## Options

| Flag | Effect |
| --- | --- |
| `--interval SECONDS` | Seconds between polls. Default 60. |
| `--settle SECONDS` | After first noticing something, wait this long and poll once more, so comments that arrive together land in one report. Default 20; 0 reports immediately. |
| `--timeout SECONDS` | Give up after this long and print `TIMEOUT`. Default: never. |
| `--wake-on-edits` | Also wake when an already-reported comment is edited, for bots that rewrite one comment in place. |
| `--include-self` | Also wake on comments by the authenticated `gh` user. |
| `--ignore-author LOGIN` | Never wake on comments by this login. Repeatable. |
| `--all-checks` | Watch every check, not only those required to merge. |
| `--fail-fast` | Report a failed check as soon as it fails, instead of waiting for the other checks to finish. Checks that fail later are reported by the next run. |
| `--wake-on-ready` | Also wake, with status `READY`, when GitHub says the PR can merge (reviews and branch rules satisfied, no conflict, not a draft) and no watched check is failing or running. Reported once per head commit. |
| `--through-queue` | Do not stop at `QUEUED`: keep watching while the PR is in the merge queue, until it merges or is removed from the queue. |
| `--once` | Poll a single time and report (`IDLE` if there is nothing), instead of waiting. |
| `--last` | Print the most recent report for this PR again, without polling, and exit. Add `--json` for the JSON form. |
| `--reset` | Forget what was already reported for this PR, including the saved report. |
| `--json` | Print the report as JSON. |
| `--quiet` | No progress lines on stderr. |
| `--state-dir DIR` | Where to keep the cursor. |
| `--log-tokens N`, `--log-lines N` | Size of the Buildkite and GitHub Actions log excerpts. |

## How it works

Each poll is one `gh api graphql` call that returns the PR state, merge-queue
membership, mergeability, comments, reviews, review threads, and the checks on the head
commit with whether each is required. That costs one point of the 5,000-per-hour
GraphQL budget, so a watcher polling every minute uses about 60 an hour. If the budget
is nearly spent, polling pauses until it resets. Transient `gh` failures are retried
with backoff.

Limits worth knowing: only the most recent 100 comments, 100 reviews and 100 review
threads are examined per poll, and a required check that has not reported any status
yet is invisible, so it does not hold back a failure report.

## Development

```bash
uv run pytest
```
