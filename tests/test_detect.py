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


def test_a_named_check_is_watched_without_being_required():
    stale = snapshot(checks=[
        status_context("branch-freshness", state="FAILURE", required=False),
        check_run("AI Review Nudge", conclusion="FAILURE", required=False),
        check_run("freshness", conclusion="FAILURE", required=False, workflow="Nightly"),
    ])
    assert not run(stale).wake
    # The whole name, in any case; a workflow's check also answers to "workflow / name".
    assert not run(stale, watch_checks=frozenset({"fresh"})).wake
    events = run(stale, watch_checks=frozenset({"branch-freshness", "nightly / freshness"}))
    assert [(c.name, c.watched_by_name) for c in events.failed_checks] == [("branch-freshness", True), ("freshness", True)]
    assert events.failed == 2

    # It holds back the other failures while it runs, and readiness while it fails.
    running = snapshot(draft=False, merge_state="CLEAN", checks=[
        check_run("unit", conclusion="FAILURE"), status_context("branch-freshness", state="PENDING", required=False),
    ])
    assert not run(running, watch_checks=frozenset({"branch-freshness"})).wake
    assert run(running).failed_checks
    failed = snapshot(draft=False, merge_state="CLEAN", checks=[status_context("branch-freshness", state="FAILURE", required=False)])
    assert run(failed, wake_on_ready=True).ready
    assert not run(failed, wake_on_ready=True, watch_checks=frozenset({"branch-freshness"})).ready


def test_a_status_that_fails_later_on_the_same_commit_is_reported():
    state = State()
    watching = {"watch_checks": frozenset({"branch-freshness"})}
    fresh = snapshot(checks=[status_context("branch-freshness", required=False)])
    assert not run(fresh, state, **watching).wake

    stale = snapshot(checks=[status_context("branch-freshness", state="FAILURE", required=False, created="2026-01-08T00:00:00Z")])
    events = run(stale, state, **watching)
    assert [c.name for c in events.failed_checks] == ["branch-freshness"]
    mark_reported(stale, events, state)
    assert not run(stale, state, **watching).wake


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


def test_through_queue_keeps_watching_and_reports_removal():
    state = State()
    queued = snapshot(in_queue=True)
    assert not run(queued, state, through_queue=True).wake

    # Without the flag QUEUED is reported, and the cursor remembers the PR was queued.
    events = run(queued, state)
    mark_reported(queued, events, state)
    assert state.queued

    removed = snapshot(dequeue_reason="CI failed")
    events = run(removed, state, through_queue=True)
    assert events.dequeued and events.needs_action and events.terminal is None
    mark_reported(removed, events, state)
    assert not run(removed, state).wake
    assert run(snapshot(state="MERGED"), State(queued=True)).terminal == MERGED
    assert not run(snapshot(state="MERGED"), State(queued=True)).dequeued


def test_ready_wakes_once_per_head_commit_and_only_when_asked():
    state = State()
    passing = [check_run("unit")]
    clean = snapshot(draft=False, merge_state="CLEAN", review_decision="APPROVED", checks=passing)
    assert not run(clean, state).wake
    events = run(clean, state, wake_on_ready=True)
    assert events.ready and events.wake and not events.needs_action
    mark_reported(clean, events, state)
    assert not run(clean, state, wake_on_ready=True).wake
    assert run(snapshot(draft=False, merge_state="CLEAN", head="b" * 40), state, wake_on_ready=True).ready

    for not_ready in (
        snapshot(draft=False, merge_state="BLOCKED", checks=passing),
        snapshot(draft=True, merge_state="CLEAN", checks=passing),
        snapshot(draft=False, merge_state="CLEAN", checks=[check_run("unit", status="IN_PROGRESS", conclusion=None)]),
        snapshot(draft=False, merge_state="CLEAN", state="MERGED"),
    ):
        assert not run(not_ready, wake_on_ready=True).ready
    # A check outside the required set holds readiness back only with --all-checks.
    unstable = snapshot(draft=False, merge_state="UNSTABLE", checks=[check_run("lint", conclusion="FAILURE", required=False)])
    assert run(unstable, wake_on_ready=True).ready
    assert not run(unstable, wake_on_ready=True, all_checks=True).ready


def test_head_moved_by_someone_else_wakes_once():
    other = "b" * 40
    state = State(head=HEAD)
    moved = snapshot(head=other, head_author="reviewer")

    # In a checkout, what counts is whether the commit is there, whoever wrote it.
    assert not run(moved, state, commit_is_local=lambda oid: True).wake
    assert run(snapshot(head=other, head_author="agent"), state, commit_is_local=lambda oid: False).head_moved_from == HEAD
    # With no checkout to ask, the commit's author decides, and an unknown author does not wake.
    assert not run(snapshot(head=other, head_author="Agent"), state).wake
    assert not run(snapshot(head=other), state).wake

    events = run(moved, state)
    assert events.head_moved_from == HEAD and events.needs_action
    mark_reported(moved, events, state)
    assert state.head == other and not run(moved, state).wake

    # The first look at a PR has nothing to compare with, and a merged PR needs no warning.
    assert not run(moved, State()).wake
    assert not run(snapshot(head=other, head_author="reviewer", state="MERGED"), State(head=HEAD)).head_moved_from
