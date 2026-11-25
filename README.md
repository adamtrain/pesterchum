# pesterchum

Block until a GitHub pull request needs attention, then print what happened and exit.

It is meant to be run by a coding agent that has just opened a PR. Instead of sleeping
for a fixed time and hoping the reviewers have finished, the agent runs `pesterchum`
and is woken the moment there is something to do.

The name is a homage to the chat client in Homestuck. This project is otherwise
unrelated to it, or to the real chat client of the same name.

## Quick start

Requires Python 3.11+, an authenticated [`gh`](https://cli.github.com), and, for
Buildkite failure details, an authenticated [`bk`](https://github.com/buildkite/cli).
There are no Python dependencies.

```bash
uv tool install /path/to/pesterchum
```

```bash
pesterchum OWNER/REPO 1234
```

`OWNER/REPO#1234` and a full PR URL work too. With no PR named, it watches the pull
request for the branch checked out in the current directory, as `gh pr view` finds it.

It waits indefinitely, so an agent should run it in the background and act when it
exits. For Claude Code that means `run_in_background: true` on the Bash call; the
foreground limit is ten minutes. When it exits, the agent reads the report, deals with
it, and runs the same command again.

`pesterchum --help` says everything an agent needs to use it, including what each
status means and what to do next.

## What wakes it

| Event | Status printed |
| --- | --- |
| A new PR comment, review, or inline review comment | `ACTION_REQUIRED` |
| A required check failed, and no required check is still running (with `--fail-fast`: as soon as it fails) | `ACTION_REQUIRED` |
| The branch conflicts with its base | `ACTION_REQUIRED` |
| Somebody else pushed to the branch | `ACTION_REQUIRED` |
| The PR left the merge queue without merging | `ACTION_REQUIRED` |
| The PR became ready to merge (only with `--wake-on-ready`) | `READY` |
| The PR entered the merge queue (unless `--through-queue`) | `QUEUED` |
| The PR merged | `MERGED` |
| The PR was closed without merging | `CLOSED` |

Nothing else ends the wait. Passing checks, approvals with no text, checks that are not
required, and comments by the authenticated `gh` user are ignored. That last rule
assumes the agent is the only one using that login: if a person reviews under the same
account, pass `--include-self`.

## Statuses, and what to do next

The first line of output is always `PESTERCHUM: <STATUS>`.

| Status | Meaning | Next step |
| --- | --- | --- |
| `ACTION_REQUIRED` | Something needs doing. | Deal with every item in the report, push, and run the same command again. The report ends with that command. |
| `READY` | The PR can be merged, and nothing has merged it. Only with `--wake-on-ready`. | Merge it or tell whoever does; run the same command again to keep watching. |
| `QUEUED` | The PR is in the merge queue. | Stop, or run again with `--through-queue` to wait for it to merge or be removed. |
| `MERGED`, `CLOSED` | Nothing further to watch. | Stop. |
| `TIMEOUT` | `--timeout` elapsed with nothing to report. | Run the same command again. |
| `IDLE` | Only with `--once`: nothing to report right now. | Nothing. |

The exit status is 0 for every status above, 1 for errors such as an unknown PR or a
`gh` that is not signed in, and 2 for bad arguments. The report goes to stdout and
progress lines to stderr (`--quiet` turns those off).

## The report

````text
PESTERCHUM: ACTION_REQUIRED

PR: acme/billing#412 — Round invoice totals after summing
https://github.com/acme/billing/pull/412
head 3f9c2a71d0 · base main · review changes requested

Needs attention: 1 failed check, 2 new comments.

## Failed checks (1)

### CI / unit — FAILURE (required)
https://github.com/acme/billing/actions/runs/9911/job/4411

_(last 3 of 212 failed-step log lines)_

```
FAILED tests/test_invoice.py::test_total_rounding
AssertionError: assert Decimal('10.01') == Decimal('10.00')
##[error]Process completed with exit code 1.
```

## New comments (2)

### Inline comment by mira on src/billing/invoice.py:88
2026-11-25T10:02:11Z · https://github.com/acme/billing/pull/412#discussion_r2201
comment id 2201 · thread id PRRT_kwDOexample

```diff
@@ -85,4 +85,4 @@ def invoice_total(lines):
-    return sum(line.amount for line in lines).quantize(CENT)
+    return sum(line.amount.quantize(CENT) for line in lines)
```

This rounds before summing, so totals can be off by a cent.

### Review by mira — CHANGES_REQUESTED
2026-11-25T10:02:40Z · https://github.com/acme/billing/pull/412#pullrequestreview-7002
review id 7002

One thing to fix before this goes in.

## Replying to inline comments
Reply in a thread, or resolve it, with the thread id shown on the comment:
```bash
gh api graphql -f t=THREAD_ID -f b='the reply' -f query='mutation($t: ID!, $b: String!) { addPullRequestReviewThreadReply(input: {pullRequestReviewThreadId: $t, body: $b}) { comment { url } } }'
gh api graphql -f t=THREAD_ID -f query='mutation($t: ID!) { resolveReviewThread(input: {threadId: $t}) { thread { isResolved } } }'
```

## Check status
2 passed, 1 failed, 0 still running.

## Next
After dealing with the above, run `pesterchum acme/billing 412` again to keep watching.
````

What is in it:

- **Comments.** Each new comment with its author, link and ids. Inline comments also
  carry the file and line, the diff hunk or the comment being replied to, whether the
  thread is resolved or outdated, and the review thread id. The id shown as `comment id`
  or `review id` is the one the REST API uses.
- **Failed checks.** Each with the reason, taken from wherever one can be found:
  the failing jobs and their logs from `bk` for Buildkite; the failed-step log from `gh`
  for GitHub Actions, with any error annotations above it; and for a check run from any
  other app, the summary, text and annotations that app attached to the check. A plain
  commit status has only its description and link.
- **Everything else that woke it**: a merge conflict, a push by somebody else, removal
  from the merge queue.

`--json` prints the same report as one JSON object with the keys `status`, `pr`,
`ready`, `head_moved_from`, `merge_conflict`, `dequeued`, `dequeue_reason`,
`failed_checks` (each with a `context` holding the failure details), `new_comments`,
`edited_comments` and `checks`.

## Running it again

A cursor for each PR is kept under `~/.cache/pesterchum`. After handling a report, run
the same command again: it reports only what is new, including anything that arrived
while the agent was busy. The first run reports comments already on the PR.

A failed check is reported once per attempt, and a merge conflict once per head commit.
Pushing a fix or re-running the check makes it eligible to be reported again. Pass
`--reset` to forget the cursor and report everything afresh.

Each report is also saved beside the cursor. Because the cursor moves on as soon as a
report is printed, a report that was lost (truncated output, a context reset) would
otherwise be gone; `pesterchum OWNER/REPO NUMBER --last` prints it again.

## Behaviour worth knowing

### Failed checks, and `--fail-fast`

By default a failure is held back until no watched check is still running, so that one
report carries every failure. With `--fail-fast` a failed check is reported as soon as
it fails, which matters when the pipeline is long and the first failure is enough to
act on. Checks that fail afterwards are reported by the next run.

### Pushes by somebody else

A reviewer committing a suggestion, or anyone pressing "Update branch", moves the head
to a commit the agent does not have, and the agent's next force-push would discard it.
So a new head commit wakes the watcher unless it is the agent's own. When the working
directory is a checkout of the repository, "own" means the commit is already in it;
otherwise it means the commit's author is the authenticated `gh` user. The agent's own
pushes never wake it, and the head as first seen is taken as the starting point.

### The merge queue

Without `--through-queue`, the watch ends with `QUEUED` when the PR enters the merge
queue. With it, the watch carries on until the PR merges or is removed from the queue.
Removal without merging is reported as `ACTION_REQUIRED`, with GitHub's reason. The
cursor remembers that the PR was queued, so the removal is reported by the next run
even if nothing was watching when it happened and `--through-queue` was never passed.

### Ready to merge

With `--wake-on-ready`, the watch also ends, with `READY`, when GitHub's own verdict is
that the PR can merge (reviews and branch rules satisfied, no conflict, not a draft) and
no watched check is failing or running. It is reported once per head commit. If there
is also something to act on, the status is `ACTION_REQUIRED` and the report says the PR
is otherwise ready.

### Polling rate

The watch polls every `--interval` seconds (60) for its first hour, and every
`--slow-interval` seconds (600) after that, since most of what happens to a PR happens
soon after a push. Running the command again starts at the fast rate again.

## Suggested agent instructions

> After opening the PR, run `pesterchum OWNER/REPO NUMBER --fail-fast --through-queue`
> in the background and wait for it to exit. If the first line is
> `PESTERCHUM: ACTION_REQUIRED`, address every item in the output, push, and run the
> same command again. Stop when the first line is `MERGED` or `CLOSED`. If the output
> was cut off or is no longer to hand, run the command with `--last` to see it again.

## Options

What ends the wait:

| Flag | Effect |
| --- | --- |
| `--fail-fast` | Report a failed check as soon as it fails, instead of waiting for the other checks to finish. |
| `--all-checks` | Watch every check, not only those required to merge. |
| `--wake-on-ready` | Also wake, with status `READY`, when the PR becomes ready to merge. |
| `--through-queue` | Do not stop at `QUEUED`: keep watching until the PR merges or is removed from the queue. |
| `--wake-on-edits` | Also wake when an already-reported comment is edited, for bots that rewrite one comment in place. |
| `--include-self` | Also wake on comments by the authenticated `gh` user. |
| `--ignore-author LOGIN` | Never wake on comments by this login. Repeatable; omit any `[bot]` suffix. |

Timing:

| Flag | Effect |
| --- | --- |
| `--interval SECONDS` | Seconds between polls. Default 60. |
| `--slow-interval SECONDS` | Seconds between polls once the watch has run for `--slow-after`. Default 600. |
| `--slow-after SECONDS` | How long to poll at `--interval` before dropping to `--slow-interval`. Default 3600. |
| `--settle SECONDS` | After first noticing something, wait this long and poll once more, so comments that arrive together land in one report. Default 20; 0 reports immediately. |
| `--timeout SECONDS` | Give up after this long and print `TIMEOUT`. Default: never. |

Instead of waiting:

| Flag | Effect |
| --- | --- |
| `--once` | Poll a single time and report what is new (`IDLE` if nothing is). |
| `--last` | Print the most recent report for this PR again, without polling. Add `--json` for the JSON form. |
| `--reset` | Forget what was already reported for this PR, including the saved report, then watch as usual. |

Output and state:

| Flag | Effect |
| --- | --- |
| `--json` | Print the report as JSON. |
| `--quiet` | No progress lines on stderr. |
| `--state-dir DIR` | Where to keep the cursor. Default `~/.cache/pesterchum`. |
| `--log-tokens N`, `--log-lines N` | Size of the Buildkite and GitHub Actions log excerpts. Defaults 3000 and 120. |

## How it works

Each poll is one `gh api graphql` call that returns the PR state, merge-queue
membership, mergeability, review decision, head commit, comments, reviews, review
threads, and the checks on the head commit with whether each is required. That costs
one point of the 5,000-per-hour GraphQL budget, so a watcher uses about 60 in its first
hour, while it polls every minute, and about 6 an hour after that. If the budget is
nearly spent, polling pauses until it resets. Transient `gh` failures are retried with
backoff. Failure details are fetched only once there is a failure to report.

Limits worth knowing:

- Only the most recent 100 comments, 100 reviews and 100 review threads are examined
  per poll.
- A required check that has not reported any status yet is invisible, so it does not
  hold back a failure report. `READY` is not affected, because it rests on GitHub's own
  verdict.
- A PR URL must be on github.com.

## Development

```bash
uv run pytest
```
