import pytest
from helpers import check_run, issue_comment, snapshot

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

    assert cli.main([*argv, "--reset", "--json"]) == 0
    assert '"status": "ACTION_REQUIRED"' in capsys.readouterr().out


def test_main_exits_1_on_gh_error(tmp_path, monkeypatch, capsys):
    def boom(repo, number):
        raise GhError("pull request o/r#7 not found", fatal=True)

    monkeypatch.setattr(cli, "fetch_snapshot", boom)
    assert cli.main(["o/r", "7", "--quiet", "--state-dir", str(tmp_path)]) == 1
    assert "not found" in capsys.readouterr().err


def test_render_inline_comment_shows_hunk_or_reply_context():
    from helpers import thread

    from pesterchum.detect import detect

    snap = snapshot(threads=[thread("src/a.py", 3, [issue_comment("t1", body="why?"), issue_comment("t2", body="still wrong")])])
    events = detect(snap, State(), Options())
    text = render_text(snap, events, ACTION_REQUIRED, {}, "pesterchum o/r 7")
    assert "Inline comment by reviewer on src/a.py:3" in text
    assert "```diff\n@@ -1 +1 @@\n+new line\n```" in text
    assert "In reply to reviewer: “why?”" in text


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
