import pytest
from helpers import HEAD, check_run, issue_comment, snapshot, status_context

from pesterchum import cli
from pesterchum.ci import BUILDKITE_URL, trim_buildkite_log
from pesterchum.cli import parse_target, watch
from pesterchum.detect import Options
from pesterchum.github import GhError
from pesterchum.report import ACTION_REQUIRED, IDLE, TIMEOUT, render_text
from pesterchum.state import State, state_path


class Script:
    """Feeds `watch` a fixed sequence of snapshots (or errors) and records the sleeps."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.sleeps = []
        self.now = 0.0

    def fetch(self):
        step = self.steps.pop(0) if len(self.steps) > 1 else self.steps[0]
        if isinstance(step, Exception):
            raise step
        return step

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def watch(self, state=None, **kwargs):
        kwargs.setdefault("interval", 60)
        kwargs.setdefault("settle", 20)
        options = kwargs.pop("options", Options())
        return watch(self.fetch, state or State(), options, sleep=self.sleep, clock=lambda: self.now, **kwargs)


def test_parse_target_forms():
    assert parse_target(["o/r", "12"]) == ("o/r", 12)
    assert parse_target(["o/r#12"]) == ("o/r", 12)
    assert parse_target(["https://github.com/o/r/pull/12/files"]) == ("o/r", 12)
    for bad in (["o/r"], ["12"], ["o/r", "twelve"], ["a", "b", "c"]):
        with pytest.raises(ValueError):
            parse_target(bad)


def test_watch_waits_then_settles_before_reporting():
    quiet = snapshot()
    one = snapshot(comments=[issue_comment("c1")])
    two = snapshot(comments=[issue_comment("c1"), issue_comment("c2")])
    script = Script(quiet, quiet, one, two)
    _, events, status = script.watch()
    assert status == ACTION_REQUIRED
    assert [c.id for c in events.new_comments] == ["c1", "c2"]
    assert script.sleeps == [60, 60, 20]


def test_watch_returns_immediately_on_merge():
    script = Script(snapshot(state="MERGED"))
    assert script.watch()[2] == "MERGED"
    assert script.sleeps == []


def test_watch_through_queue_waits_for_removal_or_merge():
    queued = snapshot(in_queue=True)
    assert Script(queued).watch()[2] == "QUEUED"

    script = Script(queued, queued, snapshot(dequeue_reason="CI failed"))
    snap, events, status = script.watch(options=Options(through_queue=True), settle=0)
    assert status == ACTION_REQUIRED and events.dequeued
    text = render_text(snap, events, status, {}, "pesterchum o/r 7")
    assert "removed from the merge queue" in text and "GitHub's reason: CI failed." in text

    assert Script(queued, snapshot(state="MERGED")).watch(options=Options(through_queue=True))[2] == "MERGED"


def test_watch_reports_ready_after_settling():
    blocked = snapshot(draft=False)
    clean = snapshot(draft=False, merge_state="CLEAN", review_decision="APPROVED")
    script = Script(blocked, clean)
    snap, events, status = script.watch(options=Options(wake_on_ready=True))
    assert status == "READY" and script.sleeps == [60, 20]
    text = render_text(snap, events, status, {}, "pesterchum o/r 7")
    assert text.startswith("PESTERCHUM: READY\n") and "review approved" in text
    assert "run `pesterchum o/r 7` again to keep watching until it merges" in text.lower()

    with_comment = snapshot(draft=False, merge_state="CLEAN", comments=[issue_comment("c1")])
    snap, events, status = Script(with_comment).watch(options=Options(wake_on_ready=True))
    assert status == ACTION_REQUIRED
    assert "Apart from that, the pull request is ready to merge." in render_text(snap, events, status, {}, "x")


def test_watch_follows_own_pushes_and_reports_someone_elses():
    first, mine, theirs = HEAD, "b" * 40, "c" * 40
    script = Script(snapshot(head=first), snapshot(head=mine, head_author="agent"), snapshot(head=theirs, head_author="reviewer"))
    state = State()
    snap, events, status = script.watch(state, settle=0)
    assert status == ACTION_REQUIRED and events.head_moved_from == mine
    text = render_text(snap, events, status, {}, "x")
    assert "somebody else pushed to the branch" in text
    assert "moved from `bbbbbbbbbb` to `cccccccccc` (“Apply suggestion”, by reviewer)" in text


def test_commit_is_local(tmp_path):
    import subprocess

    from pesterchum.local import commit_is_local

    def git(*args):
        return subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True, text=True).stdout.strip()

    assert commit_is_local("o/r", HEAD, cwd=str(tmp_path)) is None  # not a git repository
    git("init", "-q")
    git("-c", "user.name=t", "-c", "user.email=t@example.test", "commit", "-q", "--allow-empty", "-m", "one")
    oid = git("rev-parse", "HEAD")
    assert commit_is_local("o/r", oid, cwd=str(tmp_path)) is None  # no remote for that repository
    git("remote", "add", "origin", "git@github.com:O/R.git")
    assert commit_is_local("o/r", oid, cwd=str(tmp_path)) is True
    assert commit_is_local("o/r", HEAD, cwd=str(tmp_path)) is False
    assert commit_is_local("o/other", oid, cwd=str(tmp_path)) is None


def test_watch_polls_less_often_after_the_first_hour():
    script = Script(snapshot())
    assert script.watch(timeout=3 * 3600)[2] == TIMEOUT
    assert script.sleeps == [60] * 60 + [600] * 12

    # A restart begins at the fast rate again, and an interval already slower is left alone.
    script = Script(snapshot())
    script.watch(interval=900, timeout=2 * 3600)
    assert set(script.sleeps) == {900}
    script = Script(snapshot())
    script.watch(slow_after=120, slow_interval=300, timeout=600)
    assert script.sleeps == [60, 60, 300, 300]


def test_watch_times_out_and_once_reports_idle():
    script = Script(snapshot())
    assert script.watch(timeout=150)[2] == TIMEOUT
    assert script.sleeps == [60, 60, 60]
    assert Script(snapshot()).watch(once=True)[2] == IDLE


def test_watch_retries_transient_errors_but_not_fatal_or_first_poll_ones():
    script = Script(snapshot(), GhError("502"), GhError("502"), snapshot(state="MERGED"))
    assert script.watch()[2] == "MERGED"
    assert script.sleeps == [60, 120, 240]

    with pytest.raises(GhError):
        Script(GhError("network down")).watch()
    with pytest.raises(GhError):
        Script(snapshot(), GhError("Could not resolve to a PullRequest", fatal=True)).watch()


def test_watch_pauses_until_rate_limit_resets(monkeypatch):
    class Frozen(cli.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 1, 1, 0, 50, 0, tzinfo=tz)

    monkeypatch.setattr(cli, "datetime", Frozen)
    script = Script(snapshot(remaining=10), snapshot(state="MERGED"))
    script.watch()
    assert script.sleeps == [605]


def test_state_round_trip(tmp_path):
    path = state_path(tmp_path / "nested", "o/r", 7)
    assert path.name == "o__r__7.json"
    state = State.load(path)
    state.seen["c1"] = ""
    state.reported_failures = ["k"]
    state.conflict_head = "abc"
    state.save()
    loaded = State.load(path)
    assert (loaded.seen, loaded.reported_failures, loaded.conflict_head) == ({"c1": ""}, ["k"], "abc")

    path.write_text("{not json")
    with pytest.raises(ValueError):
        State.load(path)


def test_main_reports_then_stays_quiet_on_rerun(tmp_path, monkeypatch, capsys):
    snap = snapshot(
        comments=[issue_comment("c1", body="please rename this")],
        checks=[check_run("unit", conclusion="FAILURE")],
        mergeable="CONFLICTING",
    )
    monkeypatch.setattr(cli, "fetch_snapshot", lambda repo, number: snap)
    monkeypatch.setattr(cli, "failure_context", lambda check, repo, tokens, lines: "LOG EXCERPT")
    argv = ["o/r", "7", "--once", "--quiet", "--state-dir", str(tmp_path)]

    assert cli.main(argv) == 0
    out = capsys.readouterr().out
    assert out.startswith("PESTERCHUM: ACTION_REQUIRED\n")
    assert "merge conflict, 1 failed check, 1 new comment" in out
    assert "please rename this" in out and "LOG EXCERPT" in out
    assert "run `pesterchum o/r 7 --quiet --state-dir" in out

    assert cli.main(argv) == 0
    assert capsys.readouterr().out.startswith("PESTERCHUM: IDLE\n")

    # The IDLE run above did not replace the saved report.
    assert cli.main([*argv, "--last"]) == 0
    assert capsys.readouterr().out == out
    assert cli.main([*argv, "--last", "--json"]) == 0
    assert '"status": "ACTION_REQUIRED"' in capsys.readouterr().out

    assert cli.main([*argv, "--reset", "--json"]) == 0
    assert '"status": "ACTION_REQUIRED"' in capsys.readouterr().out


def test_main_watches_a_named_check_and_says_why_it_matters(tmp_path, monkeypatch, capsys):
    snap = snapshot(checks=[
        status_context("branch-freshness", state="FAILURE", required=False),
        check_run("AI Review Nudge", conclusion="FAILURE", required=False),
    ])
    monkeypatch.setattr(cli, "fetch_snapshot", lambda repo, number: snap)
    monkeypatch.setattr(cli, "failure_context", lambda check, repo, tokens, lines: "")
    argv = ["o/r", "7", "--once", "--state-dir", str(tmp_path)]

    assert cli.main(argv) == 0
    assert capsys.readouterr().out.startswith("PESTERCHUM: IDLE\n")

    assert cli.main(argv + ["--watch-check", "Branch-Freshness", "--watch-check", "typo"]) == 0
    captured = capsys.readouterr()
    assert captured.out.startswith("PESTERCHUM: ACTION_REQUIRED\n")
    assert "### branch-freshness — FAILURE (not marked required, but watched with --watch-check" in captured.out
    assert "branch-freshness failure" in captured.out and "AI Review Nudge" not in captured.out
    assert "--watch-check Branch-Freshness --watch-check typo` again" in captured.out
    assert "no check named 'typo' on the head commit yet" in captured.err
    assert "no check named 'branch-freshness'" not in captured.err


def test_last_without_a_saved_report_is_an_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "fetch_snapshot", lambda repo, number: pytest.fail("--last must not poll"))
    assert cli.main(["o/r", "7", "--last", "--state-dir", str(tmp_path)]) == 1
    assert "no report has been saved for o/r#7" in capsys.readouterr().err


def test_main_takes_the_pr_from_the_current_branch(tmp_path, monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(cli, "current_branch_pr", lambda: "https://github.com/o/r/pull/7")
    monkeypatch.setattr(cli, "fetch_snapshot", lambda repo, number: seen.append((repo, number)) or snapshot(comments=[issue_comment("c1")]))
    assert cli.main(["--once", "--quiet", "--state-dir", str(tmp_path)]) == 0
    assert seen == [("o/r", 7)]
    assert "run `pesterchum o/r 7 --quiet --state-dir" in capsys.readouterr().out

    def no_pr():
        raise GhError("could not find a pull request for the current branch", fatal=True)

    monkeypatch.setattr(cli, "current_branch_pr", no_pr)
    assert cli.main(["--quiet", "--state-dir", str(tmp_path)]) == 1
    assert "could not find a pull request for the current branch" in capsys.readouterr().err


def test_main_exits_1_on_gh_error(tmp_path, monkeypatch, capsys):
    def boom(repo, number):
        raise GhError("pull request o/r#7 not found", fatal=True)

    monkeypatch.setattr(cli, "fetch_snapshot", boom)
    assert cli.main(["o/r", "7", "--quiet", "--state-dir", str(tmp_path)]) == 1
    assert "not found" in capsys.readouterr().err


def test_render_inline_comment_shows_hunk_or_reply_context():
    from helpers import review, thread

    from pesterchum.detect import detect

    snap = snapshot(threads=[thread("src/a.py", 3, [issue_comment("t1", body="why?"), issue_comment("t2", body="still wrong")])])
    events = detect(snap, State(), Options())
    text = render_text(snap, events, ACTION_REQUIRED, {}, "pesterchum o/r 7")
    assert "Inline comment by reviewer on src/a.py:3" in text
    assert "```diff\n@@ -1 +1 @@\n+new line\n```" in text
    assert "In reply to reviewer: “why?”" in text
    assert text.count("comment id 9002 · thread id PRRT_src/a.py") == 2
    assert "## Replying to inline comments" in text and "resolveReviewThread" in text

    plain = snapshot(comments=[issue_comment("c1")], reviews=[review("r1", body="looks fine")])
    text = render_text(plain, detect(plain, State(), Options()), ACTION_REQUIRED, {}, "pesterchum o/r 7")
    assert "\ncomment id 9002\n" in text and "\nreview id 7002\n" in text
    assert "Replying to inline comments" not in text


def test_failure_context_from_the_check_run_itself(monkeypatch):
    import json

    from pesterchum import ci
    from pesterchum.models import FAIL, Check

    annotations = [
        {"path": "src/a.py", "start_line": 3, "annotation_level": "warning", "title": None, "message": "unused import"},
        {"path": "src/b.py", "start_line": 9, "annotation_level": "failure", "title": "E501", "message": "line too\n  long"},
        {"path": ".github", "start_line": 1, "annotation_level": "failure", "message": "Process completed with exit code 1."},
    ]
    replies = {
        "repos/o/r/check-runs/5": {"output": {"summary": "## 2 problems\nsee below", "text": None, "annotations_count": 3}},
        "repos/o/r/check-runs/5/annotations?per_page=100": annotations,
        "repos/o/r/check-runs/6": {"output": {"summary": None, "text": "", "annotations_count": 0}},
        "repos/o/r/check-runs/7/annotations?per_page=100": annotations,
    }

    def fake_run(cmd, allow_failure=False):
        if cmd[:2] == ["gh", "api"]:
            return json.dumps(replies[cmd[2]])
        assert cmd[:3] == ["gh", "run", "view"]
        return "job\tstep\t2026-01-01T00:00:00.0Z boom\n"

    monkeypatch.setattr(ci, "_run", fake_run)

    def check(job_id, app="some-linter"):
        return Check(name="lint", kind="check_run", state=FAIL, raw_state="FAILURE", required=True, app=app, job_id=job_id)

    text = ci.failure_context(check(5), "o/r")
    assert text.startswith("> ## 2 problems\n> see below\n\n#### Annotations (3)\n")
    # Failures come first, and a title and a message spread over lines are joined up.
    assert text.index("- `src/b.py:9` failure: E501 line too long") < text.index("- `src/a.py:3` warning: unused import")

    assert "No log source is known" in ci.failure_context(check(6), "o/r")
    assert "No log source is known" in ci.failure_context(Check(name="s", kind="status", state=FAIL, raw_state="ERROR", required=True), "o/r")
    described = Check(name="s", kind="status", state=FAIL, raw_state="FAILURE", required=False, description="Branch is 6d behind main")
    assert ci.failure_context(described, "o/r") == ""

    # GitHub Actions: only real errors are worth showing above the log excerpt.
    text = ci.failure_context(check(7, app="github-actions"), "o/r")
    assert text.startswith("#### Annotations (1)\n- `src/b.py:9` failure: E501 line too long\n\n```\nboom\n```")


def test_buildkite_url_and_log_trimming():
    match = BUILDKITE_URL.search("https://buildkite.com/Org/some-pipeline/builds/42#0190-abc")
    assert match.groups() == ("Org", "some-pipeline", "42")
    assert BUILDKITE_URL.search("https://github.com/o/r/actions/runs/1") is None

    log = "\n".join([
        "## Preparing plugins", "ok",
        "## Running agent environment hook", "$ hook",
        "## Running script", "$ make test", "🚨 Error: The command exited with status 1",
        "## Running plugin cache post-command hook", "fatal: could not upload",
    ])
    trimmed = trim_buildkite_log(log)
    assert "**Running script**\n$ make test" in trimmed
    assert "**Running plugin cache post-command hook**" in trimmed
    assert "Preparing plugins" not in trimmed and "environment hook" not in trimmed
    assert "(2 setup and hook sections without errors omitted)" in trimmed
    assert "## " not in trimmed
