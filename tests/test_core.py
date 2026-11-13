"""Behavior checks for deterministic task policy and durable queue state."""

import concurrent.futures
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from dispatcher.core import (
    Store,
    classify,
    is_completed,
    normalize_task,
    task_fingerprint,
    task_key,
    task_tags,
    task_text,
)


def task(identifier="one", **changes):
    value = {"id": identifier, "projectId": "p1", "title": "整理会议记录", "tags": ["AI"], "status": 0}
    value.update(changes)
    return value


class PolicyTests(unittest.TestCase):
    def test_identity_uses_project_and_task_id(self):
        self.assertEqual(task_key(task()), "p1:one")
        self.assertNotEqual(task_key(task()), task_key(task(projectId="p2")))
        with self.assertRaises(ValueError):
            task_key({"id": "one"})

    def test_normalization_is_non_mutating_and_supports_tag_records(self):
        original = task(tags=[" #AI ", {"name": "difficulty:easy"}], priority="5")
        normalized = normalize_task(original)
        self.assertEqual(normalized["priority"], 5)
        self.assertEqual(task_tags(normalized), ["AI", "difficulty:easy"])
        self.assertEqual(original["priority"], "5")
        self.assertIn("说明", task_text(task(desc="说明")))
        self.assertIn("子项", task_text(task(items=[{"title": "子项"}])))

    def test_fingerprint_ignores_transport_metadata(self):
        first = task(etag="a", modifiedTime="yesterday")
        second = task(etag="b", modifiedTime="today")
        self.assertEqual(task_fingerprint(first), task_fingerprint(second))
        self.assertEqual(task_fingerprint(first), task_fingerprint(dict(reversed(list(first.items())))))
        self.assertNotEqual(task_fingerprint(first), task_fingerprint(task(content="新需求")))
        self.assertNotEqual(task_fingerprint(first), task_fingerprint(task(dueDate="2026-11-07")))
        self.assertNotEqual(task_fingerprint(first), task_fingerprint(task(status=2)))

    def test_nested_etag_does_not_change_version(self):
        self.assertEqual(
            task_fingerprint(task(items=[{"id": "i", "title": "项", "etag": "a"}])),
            task_fingerprint(task(items=[{"id": "i", "title": "项", "etag": "b"}])),
        )

    def test_scope_is_tag_or_project_and_completed_tasks_are_ineligible(self):
        config = {"eligibility_tag": "AI", "project_ids": ["included"]}
        self.assertTrue(classify(task(), config)["eligible"])
        self.assertTrue(classify(task(tags=[], projectId="included"), config)["eligible"])
        self.assertFalse(classify(task(tags=[]), config)["eligible"])
        self.assertFalse(classify(task(status=2), config)["eligible"])
        self.assertTrue(is_completed(task(status="2")))

    def test_explicit_difficulty_overrides_complex_text(self):
        for label, expected in [("difficulty:easy", "easy"), ("中等", "medium"), ("复杂", "hard")]:
            decision = classify(task(title="重构整个系统架构", tags=["AI", label]), {})
            self.assertEqual(decision["difficulty"], expected)
            self.assertIn("标签", decision["reason"])

    def test_only_active_status_is_eligible(self):
        for status in (-1, 1, 2, 99, "-1", "unknown", None, False, 0.1):
            with self.subTest(status=status):
                self.assertFalse(classify(task(status=status), {})["eligible"])
        self.assertTrue(classify(task(status="0"), {})["eligible"])
        missing = task()
        del missing["status"]
        self.assertFalse(classify(missing, {})["eligible"])

    def test_priority_does_not_change_difficulty(self):
        decisions = [classify(task(priority=p), {})["difficulty"] for p in (0, 1, 3, 5)]
        self.assertEqual(len(set(decisions)), 1)
        self.assertEqual(classify(task(title="重构认证架构"), {})["difficulty"], "hard")
        self.assertEqual(classify(task(content="说明" * 900), {})["difficulty"], "hard")
        self.assertEqual(classify(task(items=[{"title": str(n)} for n in range(4)]), {})["difficulty"], "medium")

    def test_repository_tag_selects_configured_path(self):
        config = {"repositories": {"demo": "D:/repos/demo"}}
        chosen = classify(task(title="修复登录 bug", tags=["AI", "repo:demo"]), config)
        self.assertEqual(chosen["kind"], "code")
        self.assertEqual(chosen["repo"], "D:/repos/demo")
        missing = classify(task(title="修改 Python 源码"), config)
        self.assertEqual(missing["kind"], "code")
        self.assertIsNone(missing["repo"])
        self.assertEqual(classify(task(), config)["kind"], "document")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "queue.sqlite3"
        self.store = Store(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_sync_and_claim_return_complete_decoded_rows(self):
        counts = self.store.sync([task()], {})
        self.assertEqual(counts["added"], 1)
        row = self.store.claim()
        expected = {"id", "payload", "fingerprint", "difficulty", "kind", "repo", "reason", "state", "attempts", "run_dir", "result", "error", "first_seen", "updated_at", "priority", "due_date"}
        self.assertTrue(expected.issubset(row))
        self.assertEqual(row["id"], "p1:one")
        self.assertEqual(row["payload"]["title"], "整理会议记录")
        self.assertEqual(row["state"], "running")
        self.assertEqual(row["attempts"], 1)
        self.assertTrue(row["started_at"])
        self.assertIsNone(self.store.claim())

    def test_claim_orders_priority_then_due_date(self):
        self.store.sync([
            task("none", priority=0), task("low", priority=1),
            task("medium", priority=3), task("high-late", priority=5, dueDate="2026-11-09T00:00:00Z"),
            task("high-early", priority=5, dueDate="2026-11-08T00:00:00Z"), task("high-undated", priority=5),
        ], {})
        order = [self.store.claim()["payload"]["id"] for _ in range(6)]
        self.assertEqual(order, ["high-early", "high-late", "high-undated", "medium", "low", "none"])

    def test_date_offsets_are_compared_as_instants(self):
        self.store.sync([
            task("later", dueDate="2026-11-06T01:00:00+0000"),
            task("earlier", dueDate="2026-11-06T08:00:00+0800"),
        ], {})
        self.assertEqual(self.store.claim()["id"], "p1:earlier")

    def test_equal_priority_and_due_date_claim_in_fifo_order(self):
        identifiers = ("z-first", "a-second", "m-third")
        timestamps = iter(f"2026-11-06T00:00:00.{number:06d}+00:00" for number in range(20))
        with patch("dispatcher.core._now", side_effect=lambda: next(timestamps)):
            self.store.sync([task(identifier, priority=3, dueDate="2026-11-08") for identifier in identifiers], {})
        # A later unchanged sync must not move the first task to the queue tail.
        self.store.sync([task(identifiers[0], priority=3, dueDate="2026-11-08")], {})
        self.assertEqual([self.store.claim()["payload"]["id"] for _ in identifiers], list(identifiers))

    def test_same_version_terminal_is_not_requeued(self):
        self.store.sync([task()], {})
        self.store.claim()
        self.store.mark("p1:one", "done", result={"artifacts": ["answer.md"]})
        counts = self.store.sync([task(etag="new")], {})
        self.assertEqual(counts["unchanged"], 1)
        self.assertEqual(self.store.get("p1:one")["state"], "done")
        self.assertIsNone(self.store.claim())

    def test_edited_terminal_version_requeues_and_resets_attempts(self):
        self.store.sync([task()], {})
        self.store.claim()
        self.store.mark("p1:one", "done", result={"artifacts": ["answer.md"]})
        self.store.sync([task(content="新增范围")], {})
        row = self.store.get("p1:one")
        self.assertEqual(row["state"], "queued")
        self.assertEqual(row["attempts"], 0)
        self.assertIsNone(row["result"])
        self.assertIsNone(row["run_dir"])

    def test_completion_acknowledgement_retains_done_record(self):
        self.store.sync([task()], {})
        claimed = self.store.claim()
        result = {"artifacts": ["answer.md"], "verified": True}
        self.store.mark("p1:one", "done", result=result, run_dir="runs/original")
        self.store.sync([task(status=2, etag="acknowledged")], {})
        self.store.sync([task(status=2, etag="later")], {})
        row = self.store.get("p1:one")
        self.assertEqual(row["state"], "done")
        self.assertEqual(row["result"], result)
        self.assertEqual(row["run_dir"], "runs/original")
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(row["started_at"], claimed["started_at"])
        self.assertEqual(self.store.count_started_today(), 1)
        self.assertEqual(row["payload"]["status"], 2)
        self.assertIsNone(self.store.claim())
        self.store.sync([task(status=0)], {})
        self.assertEqual(self.store.get("p1:one")["state"], "queued")

    def test_inactive_status_cannot_be_normalized_into_active_work(self):
        self.store.sync([task(str(status), status=status) for status in (-1, 1, "unknown", None)], {})
        self.assertIsNone(self.store.claim())

    def test_completed_source_edit_keeps_original_execution_record(self):
        self.store.sync([task()], {})
        claimed = self.store.claim()
        result = {"artifacts": ["answer.md"]}
        self.store.mark("p1:one", "done", result=result, run_dir="runs/original")
        self.store.sync([task(status=2, content="later edited while complete")], {})
        row = self.store.get("p1:one")
        self.assertEqual(row["state"], "done")
        self.assertEqual(row["fingerprint"], claimed["fingerprint"])
        self.assertEqual(row["result"], result)
        self.assertEqual(row["run_dir"], "runs/original")
        self.assertEqual(row["attempts"], 1)

    def test_active_version_edit_preserves_execution_snapshot(self):
        for state in ("running", "verifying", "writeback_pending"):
            with self.subTest(state=state):
                identifier = state
                self.store.sync([task(identifier)], {})
                self.store.claim()
                self.store.mark("p1:" + identifier, state, run_dir="run/original")
                counts = self.store.sync([task(identifier, content="new")], {})
                row = self.store.get("p1:" + identifier)
                self.assertEqual(row["payload"].get("content", ""), "")
                self.assertEqual(row["state"], state)
                self.assertEqual(row["run_dir"], "run/original")
                self.assertEqual(counts["deferred"], 1)

    def test_scope_withdrawal_cancels_queued_work(self):
        self.store.sync([task()], {})
        self.store.sync([task(tags=[])], {})
        self.assertEqual(self.store.get("p1:one")["state"], "cancelled")
        self.assertIsNone(self.store.claim())

    def test_missing_tasks_are_not_assumed_complete(self):
        self.store.sync([task()], {})
        self.store.sync([], {})
        self.assertEqual(self.store.get("p1:one")["state"], "queued")

    def test_completed_source_is_never_claimed(self):
        self.store.sync([task(status=2)], {})
        self.assertIsNone(self.store.claim())
        self.store.sync([task("other")], {})
        self.store.sync([task("other", status=2)], {})
        self.assertEqual(self.store.get("p1:other")["state"], "cancelled")

    def test_ignored_task_can_enter_configured_scope(self):
        self.store.sync([task(tags=[])], {})
        self.assertEqual(self.store.get("p1:one")["state"], "ignored")
        self.store.sync([task(tags=[])], {"project_ids": ["p1"]})
        self.assertEqual(self.store.get("p1:one")["state"], "queued")

    def test_concurrent_claims_cannot_duplicate_a_task(self):
        self.store.sync([task(str(n)) for n in range(24)], {})
        def claim_all(_):
            local = Store(self.path)
            results = []
            while True:
                row = local.claim()
                if row is None:
                    return results
                results.append(row["id"])
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            groups = list(executor.map(claim_all, range(4)))
        ids = [identifier for group in groups for identifier in group]
        self.assertEqual(len(ids), 24)
        self.assertEqual(len(set(ids)), 24)
        self.assertEqual(self.store.count_started_today(), 24)

    def test_memory_store_supports_concurrent_claims_and_reads(self):
        store = Store(":memory:")
        store.sync([task(str(n)) for n in range(48)], {})
        barrier = threading.Barrier(4)

        def claim_all(_):
            results, errors = [], []
            barrier.wait(timeout=5)
            try:
                while True:
                    row = store.claim()
                    if row is None:
                        break
                    results.append(row["id"])
                    store.get(row["id"])
                    store.list_tasks()
                    store.events()
            except sqlite3.Error as error:
                errors.append(str(error))
            return results, errors

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            groups = list(executor.map(claim_all, range(4)))
        errors = [error for _, errors in groups for error in errors]
        ids = [identifier for ids, _ in groups for identifier in ids]
        self.assertEqual(errors, [])
        self.assertEqual(len(ids), 48)
        self.assertEqual(len(set(ids)), 48)
        self.assertEqual(store.count_started_today(), 48)

    def test_recovery_preserves_artifacts_and_does_not_retry_writeback(self):
        for name, state in [("run", "running"), ("verify", "verifying"), ("writeback", "writeback_pending")]:
            self.store.sync([task(name)], {})
            self.store.claim()
            self.store.mark("p1:" + name, state, run_dir="runs/" + name, result={"artifact": name})
        self.assertEqual(Store(self.path).recover(), 2)
        for name in ("run", "verify"):
            row = self.store.get("p1:" + name)
            self.assertEqual(row["state"], "needs_review")
            self.assertEqual(row["result"], {"artifact": name})
            self.assertEqual(row["run_dir"], "runs/" + name)
        self.assertEqual(self.store.get("p1:writeback")["state"], "writeback_pending")
        self.assertIsNone(self.store.claim())

    def test_retry_preserves_attempt_count_and_daily_started_count(self):
        self.store.sync([task()], {})
        self.store.claim()
        self.store.mark("p1:one", "failed", error="temporary")
        self.store.retry("p1:one")
        self.assertEqual(self.store.get("p1:one")["attempts"], 1)
        self.assertEqual(self.store.claim()["attempts"], 2)
        self.assertEqual(self.store.count_started_today(), 2)
        self.store.mark("p1:one", "done")
        with self.assertRaises(ValueError):
            self.store.retry("p1:one")

    def test_mark_restricts_state_and_fields(self):
        self.store.sync([task()], {})
        with self.assertRaises(ValueError):
            self.store.mark("p1:one", "surprise")
        with self.assertRaises(ValueError):
            self.store.mark("p1:one", "done", attempts=0)
        with self.assertRaises(KeyError):
            self.store.mark("p1:missing", "done")

    def test_events_and_unknown_get(self):
        self.assertIsNone(self.store.get("missing"))
        self.store.event("p1:one", "第一条")
        self.store.event("p1:one", "第二条")
        events = self.store.events(limit=1)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["message"], "第二条")
        self.assertEqual(events[0]["task_id"], "p1:one")

    def test_malformed_source_is_skipped_without_losing_valid_work(self):
        counts = self.store.sync([{"title": "bad"}, task()], {})
        self.assertEqual(counts["invalid"], 1)
        self.assertEqual(len(self.store.list_tasks()), 1)


if __name__ == "__main__":
    unittest.main()
