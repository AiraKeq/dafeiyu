from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
import math
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from memory_store import DAY, MemoryStore, SHANGHAI


NOW = datetime(2026, 11, 30, 12, tzinfo=timezone.utc).timestamp()


class MemoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "memory.sqlite3"
        self.store = MemoryStore(self.path)
        self.serial = 0

    def tearDown(self):
        self.temp.cleanup()

    def row(self, scope="group-a", ts=NOW, text="朋友说起了考试", role="user", message_id=None):
        self.serial += 1
        message_id = message_id or str(self.serial)
        self.store.add_message(scope, message_id, role, "101", "阿青", text, ts)
        with closing(sqlite3.connect(self.path)) as connection:
            return connection.execute(
                "SELECT id FROM messages WHERE scope=? AND message_id=? AND role=?", (scope, message_id, role),
            ).fetchone()[0]

    def event(self, importance=8, **changes):
        result = {"title": "朋友考试", "detail": "一起复习了考试，朋友希望大家相互鼓励。",
                  "core": "朋友希望考试前相互鼓励。", "importance": importance,
                  "participants": ["101"], "keywords": ["考试", "鼓励"]}
        result.update(changes)
        return result

    def commit(self, scope="group-a", ts=NOW, event=None, style=""):
        row_id = self.row(scope, ts)
        day = datetime.fromtimestamp(ts, SHANGHAI).date().isoformat()
        result = self.store.commit_events(scope, day, [row_id], [event or self.event()], style, now=NOW)
        self.assertEqual(result, {"inserted": 1, "processed": 1})
        return row_id

    def events(self):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute("SELECT * FROM events ORDER BY id")]

    def test_message_dedup_is_scoped_and_allows_generated_reply(self):
        self.assertTrue(self.store.add_message("a", "same-id", "user", "1", "甲", "早上好", NOW))
        self.assertFalse(self.store.add_message("a", "same-id", "user", "1", "甲", "重复投递", NOW))
        self.assertTrue(self.store.add_message("b", "same-id", "user", "1", "甲", "另一个群", NOW))
        self.assertTrue(self.store.add_message("a", "same-id", "assistant", "bot", "鲸鱼", "早上好呀", NOW))
        self.assertEqual(self.store.statistics()["messages"], 3)

    def test_pending_batches_never_mix_scopes_or_local_days_and_before_is_exclusive(self):
        midnight = datetime(2026, 11, 14, tzinfo=SHANGHAI).timestamp()
        self.row("a", midnight - 1, "昨天")
        self.row("a", midnight, "今天")
        self.row("b", midnight - 2, "别的群")
        self.row("b", midnight - 3, "别的群更早")
        batches = self.store.pending_batches(midnight, batch_size=1, max_batches=10)
        self.assertEqual(len(batches), 3)
        self.assertTrue(all(batch["day"] == "2026-11-13" for batch in batches))
        self.assertTrue(all(len(batch["rows"]) == 1 for batch in batches))
        self.assertEqual({batch["scope"] for batch in batches}, {"a", "b"})
        self.assertTrue(all(row["ts"] < midnight for batch in batches for row in batch["rows"]))

    def test_cross_scope_sources_are_rejected_atomically_and_do_not_leak(self):
        a = self.row("a", NOW - DAY)
        b = self.row("b", NOW - DAY)
        day = datetime.fromtimestamp(NOW - DAY, SHANGHAI).date().isoformat()
        with self.assertRaises(ValueError):
            self.store.commit_events("a", day, [a, b], [self.event()], now=NOW)
        self.assertEqual(self.store.statistics()["pending_messages"], 2)
        self.assertEqual(self.store.statistics()["events"], 0)
        self.store.commit_events("b", day, [b], [self.event(detail="乙群的秘密考试计划")], now=NOW)
        self.assertEqual(self.store.recall("a", "考试", now=NOW), "")
        self.assertNotIn("乙群", self.store.recent_messages("a"))
        event_id = self.events()[0]["id"]
        self.assertFalse(self.store.forget_event("a", event_id))
        self.assertTrue(self.store.forget_event("b", event_id))

    def test_invalid_score_never_acknowledges_source_or_partially_writes(self):
        for bad in (None, "8", True, math.nan, math.inf, -1, 10.1):
            with self.subTest(score=bad):
                row_id = self.row(ts=NOW - DAY)
                day = datetime.fromtimestamp(NOW - DAY, SHANGHAI).date().isoformat()
                with self.assertRaises(ValueError):
                    self.store.commit_events("group-a", day, [row_id],
                                             [self.event(), self.event(bad)], now=NOW)
                self.assertEqual(self.store.statistics()["events"], 0)
        self.assertEqual(self.store.statistics()["pending_messages"], 7)

    def test_commit_is_idempotent_and_partial_overlap_does_not_duplicate_events(self):
        ts = NOW - DAY
        first = self.row(ts=ts)
        second = self.row(ts=ts)
        day = datetime.fromtimestamp(ts, SHANGHAI).date().isoformat()
        self.store.commit_events("group-a", day, [first], [self.event()], now=NOW)
        self.assertEqual(self.store.commit_events("group-a", day, [first], [self.event()], now=NOW),
                         {"inserted": 0, "processed": 0})
        with self.assertRaises(ValueError):
            self.store.commit_events("group-a", day, [first, second], [self.event()], now=NOW)
        self.assertEqual(self.store.statistics()["events"], 1)
        self.assertEqual(self.store.statistics()["pending_messages"], 1)

    def test_model_occurrence_date_cannot_extend_source_lifetime(self):
        ts = NOW - 15 * DAY
        self.commit(ts=ts, event=self.event(occurred_at="2099-01-01", detail="旧事件"))
        self.assertEqual(self.events()[0]["ts"], ts)
        self.store.apply_lifecycle(NOW)
        self.assertEqual(self.events()[0]["tier"], "archive")

    def test_fourteen_day_boundary_preserves_only_important_core(self):
        self.commit(ts=NOW - 14 * DAY, event=self.event(8, detail="旧" * 600, core="核心" * 100))
        self.commit(ts=NOW - 14 * DAY, event=self.event(7.99, title="普通事件"))
        self.commit(ts=NOW - 14 * DAY + 1, event=self.event(2, title="还差一秒"))
        result = self.store.apply_lifecycle(NOW)
        rows = self.events()
        self.assertEqual(result["archived"], 1)
        self.assertEqual(result["deleted_events"], 1)
        self.assertEqual(len(rows), 2)
        old = next(row for row in rows if row["tier"] == "archive")
        self.assertEqual(old["importance"], 8)
        self.assertEqual(old["detail"], old["core"])
        self.assertLessEqual(len(old["detail"]), 160)
        self.assertEqual(next(row for row in rows if row["title"] == "还差一秒")["tier"], "active")

    def test_active_detail_compresses_with_age_while_importance_does_not_decay(self):
        for days in (0, 3, 7):
            self.commit(ts=NOW - days * DAY,
                        event=self.event(9, title=str(days), detail="详细" * 400, core="关键部分"))
        self.store.apply_lifecycle(NOW)
        rows = {row["title"]: row for row in self.events()}
        self.assertEqual([len(rows[str(days)]["detail"]) for days in (0, 3, 7)], [640, 320, 160])
        self.assertTrue(all(row["importance"] == 9 for row in rows.values()))

    def test_failed_extraction_keeps_old_raw_messages_and_empty_success_allows_cleanup(self):
        ts = NOW - 20 * DAY
        failed = self.row(ts=ts, text="还没有成功整理的原文")
        done = self.row(ts=ts, text="确认没有值得记的事件")
        day = datetime.fromtimestamp(ts, SHANGHAI).date().isoformat()
        self.store.commit_events("group-a", day, [done], [], now=NOW)
        counts = self.store.apply_lifecycle(NOW)
        self.assertEqual(counts["deleted_messages"], 1)
        batches = self.store.pending_batches(NOW)
        self.assertEqual([row["id"] for batch in batches for row in batch["rows"]], [failed])

    def test_recall_has_independent_scores_scope_isolation_and_strict_character_budget(self):
        self.commit(ts=NOW, event=self.event(8, detail="新的考试约定" * 80))
        self.commit(ts=NOW - 4 * DAY, event=self.event(8, detail="四天前考试约定" * 80))
        self.commit(scope="other", event=self.event(10, detail="其他群独有暗号"))
        recall = self.store.recall("group-a", "考试", now=NOW, max_chars=1800)
        self.assertIn("I=8.0/10", recall)
        self.assertIn("R=10.00/10", recall)
        self.assertIn("R=5.00/10", recall)
        self.assertNotIn("其他群独有暗号", recall)
        for budget in (0, 1, 30, 100, 250):
            self.assertLessEqual(len(self.store.recall("group-a", "考试", now=NOW, max_chars=budget)), budget)

    def test_archived_memory_requires_relevance(self):
        self.commit(ts=NOW - 30 * DAY, event=self.event(10, title="重要考试", core="考试前要鼓励朋友"))
        self.store.apply_lifecycle(NOW)
        self.assertEqual(self.store.recall("group-a", "午饭吃什么", now=NOW), "")
        self.assertIn("考试前要鼓励朋友", self.store.recall("group-a", "考试", now=NOW))

    def test_recent_messages_are_chronological_bounded_and_can_exclude_current_event(self):
        self.row(ts=NOW - 2, text="第一句", message_id="old")
        self.row(ts=NOW - 1, text="第二句", message_id="middle")
        self.row(ts=NOW, text="当前句", message_id="current")
        recent = self.store.recent_messages("group-a", exclude_message_id="current")
        self.assertLess(recent.index("第一句"), recent.index("第二句"))
        self.assertNotIn("当前句", recent)
        self.assertLessEqual(len(self.store.recent_messages("group-a", max_chars=30)), 30)

    def test_old_daily_backlog_cannot_replace_newer_style(self):
        self.commit(ts=NOW - DAY, style="温柔地接住朋友的关心")
        self.commit(ts=NOW - 3 * DAY, style="旧一天整理的风格")
        self.assertEqual(self.store.get_style("group-a"), "温柔地接住朋友的关心")
        self.assertEqual(self.store.get_style("other"), "")

    def test_reply_gap_is_independent_of_unlimited_counts_and_exactly_sixty_seconds(self):
        self.assertTrue(self.store.reserve_proactive("a", NOW, 60, 0, 0))
        self.assertFalse(self.store.reserve_proactive("a", NOW + 59.999, 60, 0, 0))
        self.assertTrue(self.store.reserve_proactive("a", NOW + 60, 60, 0, 0))
        self.store.record_actual_reply("a", NOW + 90)
        self.assertFalse(self.store.reserve_proactive("a", NOW + 149.9, 60, 0, 0))
        self.assertTrue(self.store.reserve_proactive("a", NOW + 150, 60, 0, 0))
        self.assertTrue(all(self.store.reserve_proactive("unlimited", NOW + index, 0, 0, 0)
                            for index in range(40)))

    def test_thirty_second_gap_exact_boundary_with_no_hour_or_day_count_limit(self):
        self.assertTrue(self.store.reserve_proactive("thirty", NOW, 30, 0, 0))
        self.assertFalse(self.store.reserve_proactive("thirty", NOW + 29.999, 30, 0, 0))
        self.assertTrue(self.store.reserve_proactive("thirty", NOW + 30, 30, 0, 0))
        # A normal reply also postpones spontaneous speech by exactly 30 seconds.
        self.store.record_actual_reply("thirty", NOW + 45)
        self.assertFalse(self.store.reserve_proactive("thirty", NOW + 74.999, 30, 0, 0))
        self.assertTrue(self.store.reserve_proactive("thirty", NOW + 75, 30, 0, 0))
        # More than an hour of valid reservations must not encounter a count cap.
        self.assertTrue(all(self.store.reserve_proactive("thirty", NOW + 75 + step * 30, 30, 0, 0)
                            for step in range(1, 125)))

    def test_actual_reply_does_not_spend_proactive_allowance_and_clock_cannot_rewind_gap(self):
        self.store.record_actual_reply("a", NOW)
        self.store.record_actual_reply("a", NOW - 500)
        self.assertFalse(self.store.reserve_proactive("a", NOW + 30, 60, 1, 1))
        self.assertTrue(self.store.reserve_proactive("a", NOW + 60, 60, 1, 1))
        self.assertFalse(self.store.reserve_proactive("a", NOW + 120, 60, 1, 1))

    def test_concurrent_reservations_cannot_exceed_hour_limit(self):
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(lambda _: self.store.reserve_proactive("a", NOW, 0, 3, 50), range(30)))
        self.assertEqual(sum(results), 3)
        self.assertTrue(self.store.reserve_proactive("a", NOW + 3600, 0, 3, 50))

    def test_daily_cap_uses_shanghai_midnight_not_utc_midnight(self):
        midnight = datetime(2026, 12, 1, tzinfo=SHANGHAI).timestamp()
        self.assertTrue(self.store.reserve_proactive("a", midnight - 1, 0, 0, 1))
        self.assertFalse(self.store.reserve_proactive("a", midnight - 0.1, 0, 0, 1))
        self.assertTrue(self.store.reserve_proactive("a", midnight, 0, 0, 1))

    def test_reopening_existing_store_is_an_idempotent_migration(self):
        self.commit()
        reopened = MemoryStore(self.path)
        self.assertEqual(reopened.statistics(), self.store.statistics())


if __name__ == "__main__":
    unittest.main()
