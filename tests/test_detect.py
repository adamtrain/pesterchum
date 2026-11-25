from helpers import HEAD, check_run, issue_comment, review, snapshot, status_context, thread

from pesterchum.detect import CLOSED, MERGED, QUEUED, Options, detect, mark_reported
from pesterchum.models import FAIL, PASS, PENDING
from pesterchum.state import State


def run(snap, state=None, **options):
    return detect(snap, state or State(), Options(**options))


def test_parse_keeps_only_newest_run_of_a_check():
    snap = snapshot(checks=[
        check_run("label", conclusion="CANCELLED", job_id=1, started="2026-01-01T00:00:00Z"),
        check_run("label", conclusion="SUCCESS", job_id=2, started="2026-01-01T00:05:00Z"),
    ])
    assert [(c.name, c.state, c.job_id) for c in snap.checks] == [("label", PASS, 2)]


def test_parse_check_states():
    snap = snapshot(checks=[
        check_run("running", status="IN_PROGRESS", conclusion=None),
        check_run("timed out", conclusion="TIMED_OUT"),
        check_run("skipped", conclusion="SKIPPED"),
        status_context("bk", state="ERROR"),
        status_context("waiting", state="EXPECTED"),
    ])
    assert {c.name: c.state for c in snap.checks} == {
        "running": PENDING, "timed out": FAIL, "skipped": PASS, "bk": FAIL, "waiting": PENDING,
    }


def test_parse_reviews_and_threads():
    snap = snapshot(
        reviews=[
            review("r-empty-approval", state="APPROVED"),
            review("r-pending", state="PENDING", body="draft"),
            review("r-changes", state="CHANGES_REQUESTED"),
            review("r-body", state="APPROVED", body="nice"),
        ],
        threads=[thread("src/a.py", 12, [issue_comment("t1", body="first " * 100), issue_comment("t2", body="second")])],
    )
    by_id = {c.id: c for c in snap.comments}
    assert set(by_id) == {"r-changes", "r-body", "t1", "t2"}
    assert by_id["t1"].kind == "inline" and by_id["t1"].path == "src/a.py" and by_id["t1"].line == 12
    assert by_id["t2"].reply_to_login == "reviewer"
    assert by_id["t2"].reply_to_excerpt.endswith("…")


def test_new_comments_wake_and_are_not_repeated():
    state = State()
    snap = snapshot(comments=[issue_comment("c1"), issue_comment("c2", login="greptile", bot=True)])
    events = run(snap, state)
    assert [c.id for c in events.new_comments] == ["c1", "c2"]
    assert events.new_comments[1].author == "greptile [bot]"

    mark_reported(snap, events, state)
    assert not run(snap, state).wake
    later = snapshot(comments=[issue_comment("c1"), issue_comment("c2"), issue_comment("c3")])
    assert [c.id for c in run(later, state).new_comments] == ["c3"]


def test_own_and_ignored_authors_do_not_wake():
    snap = snapshot(viewer="Agent", comments=[issue_comment("mine", login="agent"), issue_comment("noise", login="coverage", bot=True)])
    assert not run(snap, ignore_authors=frozenset({"coverage"})).wake
    assert [c.id for c in run(snap, include_self=True).new_comments] == ["mine", "noise"]


def test_edits_wake_only_when_asked():
    state = State()
    original = snapshot(comments=[issue_comment("c1")])
    mark_reported(original, run(original, state), state)
    edited = snapshot(comments=[issue_comment("c1", edited="2026-01-02T00:00:00Z")])

    assert not run(edited, state).wake
    events = run(edited, state, wake_on_edits=True)
    assert [c.id for c in events.edited_comments] == ["c1"]
    mark_reported(edited, events, state)
    assert not run(edited, state, wake_on_edits=True).wake


def test_failures_wait_for_running_checks_to_finish():
    failing = check_run("unit", conclusion="FAILURE", job_id=1)
    running = snapshot(checks=[failing, status_context("bk", state="PENDING")])
    events = run(running)
    assert not events.wake and (events.failed, events.pending) == (1, 1)

    settled = snapshot(checks=[failing, status_context("bk", state="FAILURE")])
    assert sorted(c.name for c in run(settled).failed_checks) == ["bk", "unit"]


def test_fail_fast_reports_a_failure_while_other_checks_run():
    state = State()
    failing = check_run("unit", conclusion="FAILURE", job_id=1)
    running = snapshot(checks=[failing, status_context("bk", state="PENDING")])
    events = run(running, state, fail_fast=True)
    assert [c.name for c in events.failed_checks] == ["unit"] and events.pending == 1
    mark_reported(running, events, state)
    assert not run(running, state, fail_fast=True).wake

    settled = snapshot(checks=[failing, status_context("bk", state="FAILURE")])
    assert [c.name for c in run(settled, state, fail_fast=True).failed_checks] == ["bk"]


def test_only_required_checks_are_watched_by_default():
    snap = snapshot(checks=[check_run("optional", conclusion="FAILURE", required=False)])
    assert not run(snap).wake
    assert [c.name for c in run(snap, all_checks=True).failed_checks] == ["optional"]


def test_failure_reported_once_per_attempt():
    state = State()
    snap = snapshot(checks=[check_run("unit", conclusion="FAILURE", job_id=1)])
    events = run(snap, state)
    mark_reported(snap, events, state)
    assert not run(snap, state).wake

    rerun = snapshot(checks=[check_run("unit", conclusion="FAILURE", job_id=2, started="2026-01-01T00:09:00Z")])
    assert run(rerun, state).failed_checks
    new_commit = snapshot(head="b" * 40, checks=[check_run("unit", conclusion="FAILURE", job_id=1)])
    assert run(new_commit, state).failed_checks


def test_conflict_reported_once_per_head_commit():
    state = State()
    snap = snapshot(mergeable="CONFLICTING")
    events = run(snap, state)
    assert events.conflict and events.needs_action
    mark_reported(snap, events, state)
    assert state.conflict_head == HEAD
    assert not run(snap, state).wake
    assert run(snapshot(mergeable="CONFLICTING", head="b" * 40), state).conflict
    assert not run(snapshot(mergeable="UNKNOWN")).wake


def test_terminal_states():
    assert run(snapshot(state="MERGED")).terminal == MERGED
    assert run(snapshot(state="CLOSED")).terminal == CLOSED
    queued = run(snapshot(in_queue=True))
    assert queued.terminal == QUEUED and queued.wake and not queued.needs_action
