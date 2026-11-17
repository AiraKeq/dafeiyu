"""Offline PageStore regression tests: stdlib only, all data in TemporaryDirectory.

Load the helper directly, never AstrBot/the plugin package.  No deployment cache,
network endpoint, real bot, installation, sleep, or persistent test artifact is
used.  Run with ``python -B -m unittest discover -s tests -p test_page_store.py -v``.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import copy
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.dont_write_bytecode = True
_STORE_FILE = Path(__file__).resolve().parents[1] / "page_store.py"
_SPEC = importlib.util.spec_from_file_location("_whale_page_store_unittest", _STORE_FILE)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError("Cannot load isolated PageStore tests")
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
PageStore = _MODULE.PageStore
PageStoreError = _MODULE.PageStoreError

SCOPE_A = "aiocqhttp:GroupMessage:10001"
SCOPE_B = "aiocqhttp:GroupMessage:10002"
PRIVATE_SCOPE = "aiocqhttp:FriendMessage:20001"
USER_A = "20001"
USER_B = "20002"
SOURCE = "same-source-id"
BAD_NUMBERS = (True, False, None, "10", complex(1, 0), float("nan"),
               float("inf"), float("-inf"), 10 ** 1000)


class Clock:
    def __init__(self, value=1000.0):
        self.value = value

    def __call__(self):
        return self.value

    def set(self, value):
        self.value = value


def plan(chunks=("第一页，全文保留。\n", "第二页，不跳过。\n", "最后一页。"), limit=20):
    full_text = "".join(chunks)
    pages, cursor = [], 0
    for index, content in enumerate(chunks):
        continuation = index < len(chunks) - 1
        display = content + ("回复继续查看后文" if continuation else "")
        segments = [display[start:start + limit] for start in range(0, len(display), limit)]
        pages.append({
            "content": content, "display_text": display, "segments": segments,
            "char_limit": limit, "continuation": continuation, "index": index,
            "source_start": cursor, "source_end": cursor + len(content),
            "score_plan": {"chosen_count": len(segments), "scores": [0.6, 0.3, 0.1],
                           "nested": {"values": [True, None, {"label": "score-private-sentinel"}]}},
        })
        cursor += len(content)
    return full_text, pages


class PageStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="whale-page-store-unittest-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.path = self.root / "离线 data # with spaces" / "分页 cache.sqlite3"
        self.clock = Clock()
        self.store = self.make_store()

    def make_store(self, path=None, **kwargs):
        store = PageStore(path or self.path, now=self.clock, **kwargs)
        self.addCleanup(store.close)
        return store

    def prepare(self, store=None, scope=SCOPE_A, sender=USER_A, chunks=None, source=SOURCE, limit=20):
        full_text, pages = plan() if chunks is None else plan(chunks, limit)
        lease = (store or self.store).prepare(scope, sender, full_text, pages, source)
        return lease, full_text, pages

    @contextmanager
    def database(self, path=None):
        connection = sqlite3.connect(str(path or self.path))
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def snapshot(self):
        with self.database() as connection:
            responses = connection.execute(
                "SELECT * FROM page_responses ORDER BY scope,sender_id",
            ).fetchall()
            pages = connection.execute(
                "SELECT * FROM page_items ORDER BY generation,page_index",
            ).fetchall()
            return responses, pages

    def meta(self, scope=SCOPE_A, sender=USER_A, store=None):
        return (store or self.store).peek_metadata(scope, sender)

    def race(self, actions):
        barrier = threading.Barrier(len(actions))

        def worker(action):
            barrier.wait(timeout=10)
            return action()

        with ThreadPoolExecutor(max_workers=len(actions)) as executor:
            futures = [executor.submit(worker, action) for action in actions]
            return [future.result(timeout=15) for future in futures]

    def test_prepare_ack_all_pages_and_exhaustion(self):
        lease, full_text, pages = self.prepare()
        self.assertEqual(set(lease), {"token", "generation", "scope", "sender_id", "index", "page"})
        self.assertEqual(lease["page"], pages[0])
        generation = lease["generation"]
        confirmed = []
        for index, page in enumerate(pages):
            if index:
                lease = self.store.reserve_next(SCOPE_A, USER_A)
            self.assertEqual(lease["index"], index)
            self.assertEqual(lease["generation"], generation)
            self.assertEqual(lease["page"], page)
            self.assertIsNone(self.store.reserve_next(SCOPE_A, USER_A))
            self.assertEqual(self.meta()["index"], index)
            self.assertTrue(self.meta()["busy"])
            self.assertTrue(self.store.ack(lease))
            confirmed.append(lease["page"]["content"])
            self.assertFalse(self.store.ack(lease))
            self.assertFalse(self.store.release(lease))
            self.assertFalse(self.store.renew(lease))
            self.assertEqual(self.meta()["index"], index + 1)
            self.assertFalse(self.meta()["busy"])
        self.assertEqual("".join(confirmed), full_text)
        self.assertIsNone(self.store.reserve_next(SCOPE_A, USER_A))
        self.assertFalse(self.meta()["active"])
        self.assertEqual(self.meta()["remaining"], 0)
        self.assertEqual(self.meta()["expiry"], 1600.0)

    def test_prepare_and_input_pages_are_deep_copies(self):
        full_text, pages = plan()
        expected = copy.deepcopy(pages[0])
        lease = self.store.prepare(SCOPE_A, USER_A, full_text, pages, SOURCE)
        pages[0]["content"] = "input changed"
        pages[0]["segments"][0] = "input changed"
        pages[0]["score_plan"]["nested"]["values"][2]["label"] = "input changed"
        lease["page"]["content"] = "returned changed"
        lease["page"]["segments"].clear()
        lease["page"]["score_plan"]["nested"]["values"][2]["label"] = "returned changed"
        self.assertTrue(self.store.release(lease))
        retry = self.store.reserve_next(SCOPE_A, USER_A)
        self.assertEqual(retry["index"], 0)
        self.assertEqual(retry["page"], expected)
        self.assertEqual(retry["page"]["content"], full_text[:expected["source_end"]])

    def test_reservation_page_is_also_a_deep_copy(self):
        first, _, pages = self.prepare()
        self.assertTrue(self.store.ack(first))
        second = self.store.reserve_next(SCOPE_A, USER_A)
        second["page"]["segments"][0] = "changed"
        second["page"]["score_plan"]["scores"].append(99)
        self.assertTrue(self.store.release(second))
        retry = self.store.reserve_next(SCOPE_A, USER_A)
        self.assertEqual(retry["page"], pages[1])
        self.assertNotEqual(retry["token"], second["token"])

    def test_metadata_has_only_whitelisted_fields_and_is_detached(self):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            lease, full_text, pages = self.prepare(chunks=("PRIVATE_REPLY_SENTINEL_Alpha", "PRIVATE_REPLY_SENTINEL_Beta"))
            metadata = self.store.metadata()
            encoded = json.dumps(metadata, ensure_ascii=False)
            self.assertEqual(metadata["count"], 1)
            self.assertEqual(metadata["schema_version"], 1)
            expected_keys = {"scope", "sender_id", "user", "index", "pages", "remaining",
                             "active", "busy", "expiry", "lease_expiry", "generation"}
            self.assertEqual(set(metadata["entries"][0]), expected_keys)
            for secret in (full_text, *(page["content"] for page in pages),
                           "score-private-sentinel", lease["token"]):
                self.assertNotIn(secret, encoded)
            metadata["entries"][0]["index"] = 999
            metadata["entries"].append({"scope": "fake"})
            self.assertEqual(self.meta()["index"], 0)
            self.assertEqual(self.store.metadata()["count"], 1)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(errors.getvalue(), "")

    def test_release_keeps_unsent_index_and_fences_failed_token(self):
        lease, _, pages = self.prepare()
        self.assertTrue(self.store.release(lease))
        self.assertEqual(self.meta()["index"], 0)
        self.assertFalse(self.meta()["busy"])
        self.assertFalse(self.store.release(lease))
        retry = self.store.reserve_next(SCOPE_A, USER_A)
        self.assertEqual(retry["index"], 0)
        self.assertEqual(retry["page"], pages[0])
        self.assertEqual(retry["generation"], lease["generation"])
        self.assertNotEqual(retry["token"], lease["token"])
        for action in (self.store.ack, self.store.release, self.store.renew):
            self.assertFalse(action(lease))
        self.assertTrue(self.meta()["busy"])
        self.assertTrue(self.store.ack(retry))

    def test_duplicates_cannot_ack_or_release_a_later_reservation(self):
        first, _, _ = self.prepare()
        self.assertTrue(self.store.ack(first))
        second = self.store.reserve_next(SCOPE_A, USER_A)
        for action in (self.store.ack, self.store.release, self.store.renew):
            self.assertFalse(action(first))
        self.assertEqual(self.meta()["index"], 1)
        self.assertTrue(self.meta()["busy"])
        self.assertTrue(self.store.release(second))
        third = self.store.reserve_next(SCOPE_A, USER_A)
        self.assertEqual(third["index"], 1)
        self.assertFalse(self.store.ack(second))
        self.assertFalse(self.store.release(second))
        self.assertFalse(self.store.renew(second))
        self.assertTrue(self.store.ack(third))

    def test_unconfirmed_first_page_never_becomes_sent_implicitly(self):
        lease, _, pages = self.prepare()
        future = copy.deepcopy(lease)
        future["index"] = 1
        future["page"] = pages[1]
        self.assertFalse(self.store.ack(future))
        self.assertIsNone(self.store.reserve_next(SCOPE_A, USER_A))
        self.clock.set(1090.0)
        retry = self.store.reserve_next(SCOPE_A, USER_A)
        self.assertEqual(retry["index"], 0)
        self.assertEqual(retry["page"], pages[0])
        self.assertEqual(self.meta()["index"], 0)
        self.assertFalse(self.store.ack(lease))

    def test_prepare_replacement_is_scoped_and_old_generation_is_fenced(self):
        old, _, _ = self.prepare()
        other, _, _ = self.prepare(sender=USER_B)
        self.clock.set(1050.0)
        fresh, _, fresh_pages = self.prepare(chunks=("新问题回复。", "新后文。"))
        self.assertNotEqual(fresh["generation"], old["generation"])
        self.assertNotEqual(fresh["token"], old["token"])
        self.assertEqual(fresh["page"], fresh_pages[0])
        self.assertEqual(self.meta()["expiry"], 1650.0)
        for action in (self.store.ack, self.store.release, self.store.renew):
            self.assertFalse(action(old))
        self.assertEqual(self.meta()["index"], 0)
        self.assertTrue(self.meta()["busy"])
        self.assertEqual(self.meta(sender=USER_B)["generation"], other["generation"])
        self.assertTrue(self.store.ack(other))
        self.assertTrue(self.store.ack(fresh))
        with self.database() as connection:
            count = connection.execute("SELECT COUNT(*) FROM page_items WHERE generation=?",
                                       (old["generation"],)).fetchone()[0]
            self.assertEqual(count, 0)

    def test_discard_removes_only_own_cache_not_chats_or_other_users(self):
        old, _, _ = self.prepare()
        other, _, _ = self.prepare(sender=USER_B)
        with self.database() as connection:
            connection.execute("CREATE TABLE ordinary_messages(scope TEXT, text TEXT)")
            connection.execute("INSERT INTO ordinary_messages VALUES (?,?)", (SCOPE_A, "ordinary chat memory"))
        self.assertFalse(self.store.discard(SCOPE_B, USER_A))
        self.assertTrue(self.store.discard(SCOPE_A, USER_A))
        self.assertFalse(self.store.discard(SCOPE_A, USER_A))
        self.assertIsNone(self.store.reserve_next(SCOPE_A, USER_A))
        self.assertIsNone(self.meta())
        self.assertEqual(self.meta(sender=USER_B)["generation"], other["generation"])
        fresh, _, _ = self.prepare(chunks=("新的独立回答。",))
        for action in (self.store.ack, self.store.release, self.store.renew):
            self.assertFalse(action(old))
        self.assertTrue(self.meta()["busy"])
        with self.database() as connection:
            self.assertEqual(connection.execute("SELECT * FROM ordinary_messages").fetchall(),
                             [(SCOPE_A, "ordinary chat memory")])
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM page_items WHERE generation=?",
                                                (old["generation"],)).fetchone()[0], 0)
        self.assertTrue(self.store.ack(fresh))

    def test_same_source_id_is_isolated_by_group_private_scope_and_user(self):
        owners = [(SCOPE_A, USER_A), (SCOPE_A, USER_B), (SCOPE_B, USER_A), (PRIVATE_SCOPE, USER_A)]
        leases = {}
        for number, (scope, sender) in enumerate(owners):
            leases[(scope, sender)], _, _ = self.prepare(scope=scope, sender=sender,
                                                        chunks=(f"owner-{number}-first", f"owner-{number}-next"))
        self.assertEqual(len({lease["generation"] for lease in leases.values()}), len(owners))
        self.assertIsNone(self.store.reserve_next(SCOPE_B, USER_B))
        self.assertIsNone(self.store.reserve_next(PRIVATE_SCOPE, USER_B))
        self.assertIsNone(self.store.reserve_next("another-platform:" + SCOPE_A, USER_A))
        self.assertEqual(self.store.metadata(SCOPE_A)["count"], 2)
        self.assertEqual(self.store.metadata(sender_id=USER_A)["count"], 3)
        for number, (scope, sender) in enumerate(owners):
            self.assertTrue(self.store.ack(leases[(scope, sender)]))
            lease = self.store.reserve_next(scope, sender)
            self.assertEqual((lease["scope"], lease["sender_id"]), (scope, sender))
            self.assertEqual(lease["page"]["content"], f"owner-{number}-next")
            self.assertTrue(self.store.ack(lease))

    def test_identifiers_are_not_coerced_trimmed_or_truncated(self):
        first, _, _ = self.prepare(sender="user")
        second, _, _ = self.prepare(sender=" user ")
        self.assertNotEqual(first["generation"], second["generation"])
        self.assertEqual(self.store.metadata(SCOPE_A)["count"], 2)
        self.assertIsNone(self.store.reserve_next(SCOPE_A, "USER"))
        for invalid in (1, True, None, b"20001", "", "  ", "user\x00other"):
            with self.subTest(value_type=type(invalid).__name__):
                with self.assertRaises((TypeError, ValueError)):
                    self.store.reserve_next(SCOPE_A, invalid)
        self.assertTrue(self.store.ack(first))
        self.assertTrue(self.store.ack(second))

    def test_sql_metacharacters_remain_parameterized_literal_data(self):
        scope = "group'; DROP TABLE page_responses; --"
        sender = "user' OR 1=1 --"
        source = "source'); DELETE FROM page_items; --"
        text = "literal '; SQL ? % _ \x00 内容"
        full_text, pages = plan((text,), limit=60)
        lease = self.store.prepare(scope, sender, full_text, pages, source)
        self.assertEqual(lease["page"]["content"], text)
        self.assertEqual(self.store.metadata(scope, sender)["count"], 1)
        self.assertEqual(self.store.metadata(SCOPE_A, USER_A)["count"], 0)
        self.assertTrue(self.store.ack(lease))
        restarted = self.make_store()
        self.assertEqual(restarted.metadata()["count"], 1)
        with self.database() as connection:
            self.assertEqual(connection.execute("SELECT source_id,full_text FROM page_responses").fetchone(),
                             (source, text))

    def test_fake_and_malformed_leases_cannot_cross_owners_or_advance(self):
        lease, _, _ = self.prepare()
        victim, _, _ = self.prepare(scope=SCOPE_B, sender=USER_B)
        before = self.snapshot()
        fakes = [None, [], "lease", {}, {"token": lease["token"]}]
        changes = {"scope": SCOPE_B, "sender_id": USER_B, "index": 1,
                   "generation": victim["generation"], "token": victim["token"]}
        for key, value in changes.items():
            fake = copy.deepcopy(lease)
            fake[key] = value
            fakes.append(fake)
        for key, value in (("index", True), ("index", 0.0), ("index", -1),
                           ("token", "0" * 64), ("generation", "0" * 32),
                           ("scope", 1), ("sender_id", None)):
            fake = copy.deepcopy(lease)
            fake[key] = value
            fakes.append(fake)
        crossed = copy.deepcopy(lease)
        crossed.update(scope=SCOPE_B, sender_id=USER_B)
        fakes.append(crossed)
        for number, fake in enumerate(fakes):
            with self.subTest(fake=number):
                for action in (self.store.ack, self.store.release, self.store.renew):
                    self.assertFalse(action(fake))
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(self.store.ack(lease))
        self.assertTrue(self.store.ack(victim))

    def test_restart_preserves_live_lease_and_it_can_be_acknowledged(self):
        lease, _, _ = self.prepare()
        expected = self.meta()
        self.store.close()
        reopened = self.make_store()
        self.assertEqual(reopened.peek_metadata(SCOPE_A, USER_A), expected)
        self.assertIsNone(reopened.reserve_next(SCOPE_A, USER_A))
        self.assertTrue(reopened.ack(lease))
        self.assertEqual(reopened.peek_metadata(SCOPE_A, USER_A)["index"], 1)

    def test_restart_preserves_unconfirmed_index_and_reservation_identity(self):
        first, _, pages = self.prepare()
        self.assertTrue(self.store.ack(first))
        second = self.store.reserve_next(SCOPE_A, USER_A)
        self.assertTrue(self.store.release(second))
        self.store.close()
        reopened = self.make_store()
        retry = reopened.reserve_next(SCOPE_A, USER_A)
        self.assertEqual(retry["index"], 1)
        self.assertEqual(retry["generation"], first["generation"])
        self.assertEqual(retry["page"], pages[1])
        self.assertFalse(reopened.ack(second))
        reopened.close()
        another = self.make_store()
        self.assertIsNone(another.reserve_next(SCOPE_A, USER_A))
        self.assertTrue(another.ack(retry))
        self.assertEqual(another.reserve_next(SCOPE_A, USER_A)["index"], 2)

    def test_restart_recovers_expired_lease_without_skipping_unconfirmed_page(self):
        first, _, pages = self.prepare()
        self.assertTrue(self.store.ack(first))
        second = self.store.reserve_next(SCOPE_A, USER_A)
        self.store.close()
        self.clock.set(1090.0)
        reopened = self.make_store()
        self.assertFalse(reopened.peek_metadata(SCOPE_A, USER_A)["busy"])
        retry = reopened.reserve_next(SCOPE_A, USER_A)
        self.assertEqual(retry["index"], 1)
        self.assertEqual(retry["page"], pages[1])
        self.assertNotEqual(retry["token"], second["token"])
        self.assertFalse(reopened.ack(second))
        self.assertFalse(reopened.release(second))
        self.assertFalse(reopened.renew(second))
        self.assertTrue(reopened.ack(retry))

    def test_restart_at_ttl_boundary_reaps_durably_and_cannot_revive(self):
        lease, _, _ = self.prepare()
        self.store.close()
        self.clock.set(1600.0)
        reopened = self.make_store()
        self.assertEqual(reopened.metadata()["count"], 0)
        self.assertIsNone(reopened.reserve_next(SCOPE_A, USER_A))
        self.assertFalse(reopened.ack(lease))
        self.clock.set(1000.0)
        reopened.close()
        another = self.make_store()
        self.assertIsNone(another.reserve_next(SCOPE_A, USER_A))
        self.assertFalse(another.renew(lease))
        with self.database() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM page_responses").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM page_items").fetchone()[0], 0)

    def test_restart_different_configuration_does_not_rewrite_existing_expiries(self):
        lease, _, _ = self.prepare()
        self.store.close()
        reopened = self.make_store(ttl_seconds=9000, lease_seconds=1000)
        metadata = reopened.peek_metadata(SCOPE_A, USER_A)
        self.assertEqual(metadata["expiry"], 1600.0)
        self.assertEqual(metadata["lease_expiry"], 1090.0)
        self.clock.set(1080.0)
        self.assertTrue(reopened.renew(lease))
        self.assertEqual(reopened.peek_metadata(SCOPE_A, USER_A)["lease_expiry"], 1600.0)

    def test_concurrent_reserve_same_instance_grants_exactly_one_lease(self):
        first, _, pages = self.prepare()
        self.assertTrue(self.store.release(first))
        results = self.race([lambda: self.store.reserve_next(SCOPE_A, USER_A) for _ in range(8)])
        successes = [result for result in results if result is not None]
        self.assertEqual(len(successes), 1)
        self.assertEqual(successes[0]["index"], 0)
        self.assertEqual(successes[0]["page"], pages[0])
        self.assertTrue(self.store.ack(successes[0]))
        self.assertEqual(self.meta()["index"], 1)

    def test_concurrent_reserve_across_instances_is_atomic(self):
        first, _, _ = self.prepare()
        other = self.make_store()
        self.assertTrue(self.store.release(first))
        for _ in range(4):
            results = self.race([lambda owner=owner: owner.reserve_next(SCOPE_A, USER_A)
                                 for owner in (self.store, other, self.store, other)])
            successes = [result for result in results if result is not None]
            self.assertEqual(len(successes), 1)
            self.assertEqual(successes[0]["index"], 0)
            self.assertTrue(other.release(successes[0]))
        retry = self.store.reserve_next(SCOPE_A, USER_A)
        self.assertTrue(other.ack(retry))

    def test_concurrent_expired_lease_reclaim_grants_one_fenced_token(self):
        old, _, _ = self.prepare()
        other = self.make_store()
        self.clock.set(1090.0)
        results = self.race([lambda owner=owner: owner.reserve_next(SCOPE_A, USER_A)
                             for owner in (self.store, other, self.store, other)])
        successes = [result for result in results if result is not None]
        self.assertEqual(len(successes), 1)
        self.assertNotEqual(successes[0]["token"], old["token"])
        self.assertEqual(successes[0]["index"], 0)
        for action in (self.store.ack, other.release, self.store.renew):
            self.assertFalse(action(old))
        self.assertTrue(other.ack(successes[0]))

    def test_concurrent_duplicate_ack_advances_only_once(self):
        first, _, _ = self.prepare()
        other = self.make_store()
        results = self.race([lambda owner=owner: owner.ack(first)
                             for owner in (self.store, other, self.store, other, self.store, other)])
        self.assertEqual(results.count(True), 1)
        self.assertEqual(results.count(False), 5)
        self.assertEqual(self.meta()["index"], 1)
        self.assertEqual(other.reserve_next(SCOPE_A, USER_A)["index"], 1)

    def test_concurrent_different_owners_each_receive_only_their_page(self):
        owners = [(SCOPE_A, USER_A), (SCOPE_A, USER_B), (SCOPE_B, USER_A), (PRIVATE_SCOPE, USER_A)]
        for number, (scope, sender) in enumerate(owners):
            lease, _, _ = self.prepare(scope=scope, sender=sender, chunks=(f"private-owner-{number}",))
            self.assertTrue(self.store.release(lease))
        results = self.race([lambda scope=scope, sender=sender: self.store.reserve_next(scope, sender)
                             for scope, sender in owners])
        for number, (result, owner) in enumerate(zip(results, owners)):
            self.assertEqual((result["scope"], result["sender_id"]), owner)
            self.assertEqual(result["page"]["content"], f"private-owner-{number}")

    def test_lease_boundary_before_and_exact_expiry(self):
        leases = {}
        for sender in (USER_A, USER_B, "20003", "20004"):
            leases[sender], _, _ = self.prepare(sender=sender)
        self.clock.set(1089.999)
        self.assertTrue(self.store.ack(leases[USER_A]))
        self.assertIsNone(self.store.reserve_next(SCOPE_A, USER_B))
        self.clock.set(1090.0)
        self.assertFalse(self.store.ack(leases[USER_B]))
        self.assertFalse(self.store.release(leases["20003"]))
        self.assertFalse(self.store.renew(leases["20004"]))
        retry = self.store.reserve_next(SCOPE_A, USER_B)
        self.assertEqual(retry["index"], 0)
        self.assertNotEqual(retry["token"], leases[USER_B]["token"])
        self.assertEqual(self.meta(sender=USER_B)["lease_expiry"], 1180.0)
        self.assertFalse(self.store.ack(leases[USER_B]))
        self.assertTrue(self.store.ack(retry))

    def test_observed_expired_token_cannot_revive_when_clock_moves_back(self):
        lease, _, _ = self.prepare()
        self.clock.set(1090.0)
        self.assertFalse(self.store.renew(lease))
        self.clock.set(1089.0)
        for action in (self.store.ack, self.store.release, self.store.renew):
            self.assertFalse(action(lease))
        retry = self.store.reserve_next(SCOPE_A, USER_A)
        self.assertNotEqual(retry["token"], lease["token"])
        self.assertEqual(retry["index"], 0)
        self.assertTrue(self.store.ack(retry))

    def test_ttl_is_exclusive_and_caps_long_leases(self):
        store = self.make_store(path=self.root / "long-lease.sqlite", lease_seconds=900)
        lease, _, _ = self.prepare(store=store, chunks=("初页。", "末页。"))
        self.assertEqual(self.meta(store=store)["lease_expiry"], 1600.0)
        self.clock.set(1599.999)
        self.assertTrue(store.ack(lease))
        final = store.reserve_next(SCOPE_A, USER_A)
        self.assertEqual(final["index"], 1)
        self.assertEqual(self.meta(store=store)["lease_expiry"], 1600.0)
        self.clock.set(1600.0)
        self.assertIsNone(store.reserve_next(SCOPE_A, USER_A))
        for action in (store.ack, store.release, store.renew):
            self.assertFalse(action(final))
        self.assertIsNone(self.meta(store=store))
        self.clock.set(1599.0)
        self.assertIsNone(store.reserve_next(SCOPE_A, USER_A))

    def test_ttl_never_slides_on_ack_reserve_or_renew(self):
        lease, _, _ = self.prepare()
        self.clock.set(1080.0)
        self.assertTrue(self.store.renew(lease))
        self.assertEqual(self.meta()["lease_expiry"], 1170.0)
        self.clock.set(1160.0)
        self.assertTrue(self.store.ack(lease))
        self.clock.set(1400.0)
        next_lease = self.store.reserve_next(SCOPE_A, USER_A)
        self.clock.set(1480.0)
        self.assertTrue(self.store.renew(next_lease))
        self.clock.set(1560.0)
        self.assertTrue(self.store.renew(next_lease))
        self.assertEqual(self.meta()["expiry"], 1600.0)
        self.assertEqual(self.meta()["lease_expiry"], 1600.0)
        self.clock.set(1600.0)
        self.assertFalse(self.store.ack(next_lease))
        self.assertIsNone(self.store.reserve_next(SCOPE_A, USER_A))

    def test_renew_preserves_identity_and_progress_while_extending_live_lease(self):
        lease, _, _ = self.prepare()
        expected = copy.deepcopy(lease)
        self.clock.set(1089.0)
        self.assertTrue(self.store.renew(lease))
        self.assertEqual(lease, expected)
        self.assertEqual(self.meta()["index"], 0)
        self.assertEqual(self.meta()["lease_expiry"], 1179.0)
        self.clock.set(1090.0)
        self.assertIsNone(self.store.reserve_next(SCOPE_A, USER_A))
        self.assertTrue(self.store.ack(lease))
        self.assertFalse(self.store.renew(lease))

    def test_renew_does_not_shrink_a_lease_after_backward_clock_move(self):
        lease, _, _ = self.prepare()
        self.clock.set(1080.0)
        self.assertTrue(self.store.renew(lease))
        self.assertEqual(self.meta()["lease_expiry"], 1170.0)
        self.clock.set(1070.0)
        self.assertTrue(self.store.renew(lease))
        self.assertEqual(self.meta()["lease_expiry"], 1170.0)
        self.assertTrue(self.store.ack(lease))

    def test_renew_cannot_extend_past_ttl_or_revive_expired_token(self):
        store = self.make_store(path=self.root / "short-ttl.sqlite", ttl_seconds=100)
        lease, _, _ = self.prepare(store=store)
        self.clock.set(1080.0)
        self.assertTrue(store.renew(lease))
        self.assertEqual(self.meta(store=store)["lease_expiry"], 1100.0)
        self.clock.set(1095.0)
        self.assertTrue(store.renew(lease))
        self.assertEqual(self.meta(store=store)["lease_expiry"], 1100.0)
        self.clock.set(1100.0)
        self.assertFalse(store.renew(lease))
        self.clock.set(1099.0)
        self.assertFalse(store.renew(lease))
        self.assertIsNone(store.reserve_next(SCOPE_A, USER_A))

    def test_valid_limits_20_40_60_and_three_segments_up_to_180(self):
        for limit in (1, 20, 40, 60):
            with self.subTest(limit=limit):
                content = "字" * (3 * limit)
                full_text, pages = plan((content,), limit=limit)
                lease = self.store.prepare(SCOPE_A, USER_A, full_text, pages, SOURCE)
                self.assertEqual(len(lease["page"]["segments"]), 3)
                self.assertEqual(len(lease["page"]["display_text"]), 3 * limit)
                self.assertTrue(self.store.ack(lease))

    def test_whitespace_punctuation_newlines_and_unicode_are_not_dropped(self):
        chunks = (" \t开头，\r\n", "e\u0301\u200d汉字\x00中间  ", "\n结尾!? \t")
        lease, full_text, pages = self.prepare(chunks=chunks)
        confirmed = []
        for index, page in enumerate(pages):
            if index:
                lease = self.store.reserve_next(SCOPE_A, USER_A)
            self.assertEqual(lease["page"]["content"], page["content"])
            confirmed.append(lease["page"]["content"])
            self.assertTrue(self.store.ack(lease))
        self.assertEqual("".join(confirmed), full_text)

    def test_constructor_rejects_wrong_types_nonfinite_and_nonpositive_durations(self):
        for field in ("ttl_seconds", "lease_seconds"):
            for index, value in enumerate((*BAD_NUMBERS, 0, -1, -0.1)):
                with self.subTest(field=field, case=index):
                    path = self.root / f"invalid-{field}-{index}.sqlite"
                    with self.assertRaises((TypeError, ValueError)):
                        PageStore(path, now=self.clock, **{field: value})
                    self.assertFalse(path.exists())
        for index, value in enumerate(BAD_NUMBERS):
            with self.subTest(clock=index):
                path = self.root / f"invalid-clock-{index}.sqlite"
                with self.assertRaises((TypeError, ValueError)):
                    PageStore(path, now=lambda value=value: value)
                self.assertFalse(path.exists())
        for value in (None, 1000.0, "clock"):
            with self.assertRaises(TypeError):
                PageStore(self.root / "not-callable.sqlite", now=value)
        for value in (None, 1, b"cache.sqlite"):
            with self.assertRaises(TypeError):
                PageStore(value, now=self.clock)
        for value in ("", "  ", ":memory:", "file:cache.sqlite?mode=memory"):
            with self.assertRaises(ValueError):
                PageStore(value, now=self.clock)

    def test_nonfinite_runtime_clock_rejects_all_operations_without_state_change(self):
        lease, _, _ = self.prepare()
        before = self.snapshot()
        full_text, pages = plan(("不会覆盖原回复。",))
        actions = [lambda: self.store.prepare(SCOPE_A, USER_A, full_text, pages, "new-source"),
                   lambda: self.store.reserve_next(SCOPE_A, USER_A), lambda: self.store.ack(lease),
                   lambda: self.store.release(lease), lambda: self.store.renew(lease),
                   lambda: self.store.discard(SCOPE_A, USER_A), lambda: self.store.metadata(),
                   lambda: self.store.peek_metadata(SCOPE_A, USER_A)]
        for value in BAD_NUMBERS:
            self.clock.set(value)
            for index, action in enumerate(actions):
                with self.subTest(value_type=type(value).__name__, action=index):
                    with self.assertRaises((TypeError, ValueError)):
                        action()
        self.clock.set(1000.0)
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(self.store.ack(lease))

    def test_overflow_or_float_rounding_of_computed_expiry_rolls_back(self):
        lease, _, _ = self.prepare()
        before = self.snapshot()
        self.clock.set(1e308)
        self.store.ttl_seconds = 1e308
        full_text, pages = plan(("不能替换。",))
        with self.assertRaises(ValueError):
            self.store.prepare(SCOPE_A, USER_A, full_text, pages, "new-source")
        self.clock.set(1000.0)
        self.store.ttl_seconds = 600.0
        self.store.lease_seconds = 1e-300
        with self.assertRaises(ValueError):
            self.store.prepare(SCOPE_A, USER_A, full_text, pages, "new-source")
        self.store.lease_seconds = 90.0
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(self.store.ack(lease))

    def test_invalid_page_plans_never_replace_existing_cache(self):
        lease, full_text, valid = self.prepare()
        before = self.snapshot()
        cases = [None, [], (), tuple(valid), iter(valid), "pages", {}, [None], [[]]]

        def changed(key, value, page_index=0):
            pages = copy.deepcopy(valid)
            pages[page_index][key] = value
            cases.append(pages)

        for key in _MODULE._REQUIRED_PAGE_KEYS:
            pages = copy.deepcopy(valid)
            del pages[0][key]
            cases.append(pages)
        changed("unexpected_text", "unvalidated")
        for value in (True, 0.0, -1, 1, "0"):
            changed("index", value)
        for value in (True, 0, -1, 61, 20.0, "20", float("nan"), float("inf")):
            changed("char_limit", value)
        for value in (0, 1, None, "true", False):
            changed("continuation", value)
        changed("continuation", True, page_index=2)
        for value in ("text", (), [], ["a"] * 4, [""], [None], [b"text"], ["字" * 21]):
            changed("segments", value)
        for value in (None, b"text", "", "rewritten reply"):
            changed("content", value)
            changed("display_text", value)
        for value in (True, 0.0, -1, 1, "0"):
            changed("source_start", value)
        for value in (True, 1.0, -1, 0, 99999, "10"):
            changed("source_end", value)
        for value in ({"score": float("nan")}, {"score": float("inf")}, {1: "bad key"},
                      {"bad": {1, 2}}, {"bad": object()}, {"bad": (1, 2)}):
            changed("score_plan", value)
        reversed_pages = copy.deepcopy(valid)
        reversed_pages.reverse()
        cases.append(reversed_pages)
        dropped = copy.deepcopy(valid[:-1])
        dropped[-1]["continuation"] = False
        dropped[-1]["display_text"] = dropped[-1]["content"]
        dropped[-1]["segments"] = [dropped[-1]["content"]]
        cases.append(dropped)
        for number, pages in enumerate(cases):
            with self.subTest(case=number):
                with self.assertRaises((TypeError, ValueError)):
                    self.store.prepare(SCOPE_A, USER_A, full_text, pages, "invalid-source")
                self.assertEqual(self.snapshot(), before)
        self.assertTrue(self.store.ack(lease))

    def test_full_text_exact_coverage_and_offsets_are_required(self):
        lease, _, _ = self.prepare()
        before = self.snapshot()
        full_text, pages = plan(("aa", "bb"))
        for wrong_full in (None, b"aabb", "", "aabb-extra", "aabB", " aabb", "aabb "):
            with self.subTest(wrong_type=type(wrong_full).__name__):
                with self.assertRaises((TypeError, ValueError)):
                    self.store.prepare(SCOPE_A, USER_A, wrong_full, pages, SOURCE)
        gap_text, gap_pages = plan(("aa", "bb"))
        gap_pages[1]["source_start"] = 3
        gap_pages[1]["source_end"] = 5
        with self.assertRaises(ValueError):
            self.store.prepare(SCOPE_A, USER_A, "aaXbb", gap_pages, SOURCE)
        overlap_text, overlap_pages = plan(("aa", "aa"))
        overlap_pages[1]["source_start"] = 1
        overlap_pages[1]["source_end"] = 3
        with self.assertRaises(ValueError):
            self.store.prepare(SCOPE_A, USER_A, "aaa", overlap_pages, SOURCE)
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(self.store.ack(lease))

    def test_final_display_cannot_invent_text_or_hide_unsent_content(self):
        full_text, pages = plan(("原文不能丢",))
        for display in ("原文", "改写后的内容", "原文不能丢附加未授权文字"):
            corrupted = copy.deepcopy(pages)
            corrupted[0]["display_text"] = display
            corrupted[0]["segments"] = [display]
            with self.assertRaises(ValueError):
                self.store.prepare(SCOPE_A, USER_A, full_text, corrupted, SOURCE)
        self.assertEqual(self.store.metadata()["count"], 0)

    def test_score_plan_is_optional_and_deep_or_cyclic_data_is_rejected(self):
        full_text, pages = plan(("允许没有评分计划。",))
        del pages[0]["score_plan"]
        lease = self.store.prepare(SCOPE_A, USER_A, full_text, pages, SOURCE)
        self.assertNotIn("score_plan", lease["page"])
        before = self.snapshot()
        recursive = {}
        recursive["cycle"] = recursive
        pages[0]["score_plan"] = recursive
        with self.assertRaises(ValueError):
            self.store.prepare(SCOPE_A, USER_A, full_text, pages, SOURCE)
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(self.store.ack(lease))

    def test_sqlite_insert_failure_rolls_back_replacement_and_all_new_pages(self):
        old, _, _ = self.prepare()
        self.prepare(sender=USER_B)
        before = self.snapshot()
        with self.database() as connection:
            connection.execute("""CREATE TRIGGER inject_insert_failure BEFORE INSERT ON page_items
                WHEN NEW.page_index = 1 BEGIN SELECT RAISE(ABORT, 'injected page insert failure'); END""")
        new_text, new_pages = plan(("new first", "new second", "new last"))
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.prepare(SCOPE_A, USER_A, new_text, new_pages, "new-source")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.meta()["generation"], old["generation"])
        self.assertEqual(self.meta()["index"], 0)
        with self.database() as connection:
            connection.execute("DROP TRIGGER inject_insert_failure")
        self.assertTrue(self.store.ack(old))

    def test_sqlite_ack_failure_rolls_back_progress_and_keeps_original_token(self):
        lease, _, _ = self.prepare()
        before = self.snapshot()
        with self.database() as connection:
            connection.execute("""CREATE TRIGGER inject_ack_failure BEFORE UPDATE OF next_index
                ON page_responses WHEN NEW.next_index > OLD.next_index
                BEGIN SELECT RAISE(ABORT, 'injected ack failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.ack(lease)
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(self.meta()["busy"])
        self.assertIsNone(self.store.reserve_next(SCOPE_A, USER_A))
        with self.database() as connection:
            connection.execute("DROP TRIGGER inject_ack_failure")
        self.assertTrue(self.store.ack(lease))

    def test_sqlite_commit_failure_rolls_back_ack_and_prepare(self):
        lease, _, _ = self.prepare()
        before = self.snapshot()
        original_connect = sqlite3.connect

        class FailingCommit(sqlite3.Connection):
            def commit(self):
                raise sqlite3.OperationalError("injected commit failure")

        def failing_connection(*args, **kwargs):
            kwargs["factory"] = FailingCommit
            return original_connect(*args, **kwargs)

        new_text, new_pages = plan(("replacement first", "replacement next"))
        for action in (lambda: self.store.ack(lease),
                       lambda: self.store.prepare(SCOPE_A, USER_A, new_text, new_pages, "new-source")):
            with mock.patch.object(_MODULE.sqlite3, "connect", side_effect=failing_connection):
                with self.assertRaises(sqlite3.OperationalError):
                    action()
            self.assertEqual(self.snapshot(), before)
        self.assertTrue(self.store.ack(lease))

    def test_incompatible_ordinary_sqlite_database_is_not_migrated_or_modified(self):
        path = self.root / "ordinary-memory.sqlite"
        with self.database(path) as connection:
            connection.execute("CREATE TABLE messages(scope TEXT,text TEXT)")
            connection.execute("INSERT INTO messages VALUES (?,?)", (SCOPE_A, "ordinary memory preserved"))
            before_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        before_bytes = path.read_bytes()
        with self.assertRaises(PageStoreError):
            self.make_store(path=path)
        self.assertEqual(path.read_bytes(), before_bytes)
        with self.database(path) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], before_mode)
            self.assertEqual(connection.execute("SELECT * FROM messages").fetchall(),
                             [(SCOPE_A, "ordinary memory preserved")])
            self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name=?",
                                                 ("page_responses",)).fetchone())

    def test_damaged_schema_fails_closed_on_reopen_and_live_operations(self):
        lease, _, _ = self.prepare()
        with self.database() as connection:
            connection.execute("ALTER TABLE page_items ADD COLUMN unsafe_extra TEXT")
        before = self.snapshot()
        for action in (lambda: self.store.metadata(), lambda: self.store.ack(lease),
                       lambda: self.store.reserve_next(SCOPE_A, USER_A), lambda: self.make_store()):
            with self.assertRaises(PageStoreError):
                action()
        self.assertEqual(self.snapshot(), before)
        with self.database() as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
            columns = [row[1] for row in connection.execute("PRAGMA table_info(page_items)")]
            self.assertIn("unsafe_extra", columns)

    def test_missing_schema_index_is_not_silently_recreated(self):
        self.prepare()
        with self.database() as connection:
            connection.execute("DROP INDEX page_responses_expiry")
        with self.assertRaises(PageStoreError):
            self.make_store()
        with self.database() as connection:
            self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name=?",
                                                 ("page_responses_expiry",)).fetchone())

    def test_newer_schema_fails_closed_even_when_expiry_cleanup_would_run(self):
        lease, _, _ = self.prepare()
        with self.database() as connection:
            connection.execute("PRAGMA user_version=2")
        before = self.snapshot()
        self.clock.set(2000.0)
        for action in (lambda: self.make_store(), lambda: self.store.metadata(),
                       lambda: self.store.reserve_next(SCOPE_A, USER_A),
                       lambda: self.store.ack(lease), lambda: self.store.release(lease),
                       lambda: self.store.renew(lease), lambda: self.store.discard(SCOPE_A, USER_A)):
            with self.assertRaises(PageStoreError):
                action()
        self.assertEqual(self.snapshot(), before)
        with self.database() as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)

    def test_wrong_application_id_fails_closed_without_repair(self):
        self.prepare()
        with self.database() as connection:
            connection.execute("PRAGMA application_id=1234")
        before = self.snapshot()
        with self.assertRaises(PageStoreError):
            self.make_store()
        self.assertEqual(self.snapshot(), before)
        with self.database() as connection:
            self.assertEqual(connection.execute("PRAGMA application_id").fetchone()[0], 1234)

    def test_non_sqlite_file_fails_closed_and_bytes_are_preserved(self):
        path = self.root / "damaged.sqlite"
        raw = b"this is not a SQLite database\x00" * 10
        path.write_bytes(raw)
        with self.assertRaises(sqlite3.DatabaseError):
            self.make_store(path=path)
        self.assertEqual(path.read_bytes(), raw)

    def test_corrupt_page_json_fails_closed_without_reserving_or_advancing(self):
        lease, full_text, pages = self.prepare()
        self.assertTrue(self.store.release(lease))
        original = json.dumps(pages[0])
        invalid_content = copy.deepcopy(pages[0])
        invalid_content["content"] = "not the original content"
        invalid_score = copy.deepcopy(pages[0])
        invalid_score["score_plan"] = {"number": float("nan")}
        bad_values = ["{broken JSON", json.dumps(invalid_content), json.dumps(invalid_score),
                      original[:-1] + ',"index":0}', '["not-a-page"]']
        for number, bad in enumerate(bad_values):
            with self.subTest(corruption=number):
                with self.database() as connection:
                    connection.execute("UPDATE page_items SET page_json=? WHERE generation=? AND page_index=?",
                                       (bad, lease["generation"], 0))
                before = self.snapshot()
                with self.assertRaises(PageStoreError):
                    self.store.reserve_next(SCOPE_A, USER_A)
                with self.assertRaises(PageStoreError):
                    self.make_store()
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(self.meta()["index"], 0)
                self.assertFalse(self.meta()["busy"])
        with self.database() as connection:
            connection.execute("UPDATE page_items SET page_json=? WHERE generation=? AND page_index=?",
                               (original, lease["generation"], 0))
        retry = self.store.reserve_next(SCOPE_A, USER_A)
        self.assertEqual(retry["page"], pages[0])
        self.assertTrue(self.store.ack(retry))

    def test_missing_stored_page_fails_closed_before_ack_or_new_reservation(self):
        lease, _, _ = self.prepare()
        with self.database() as connection:
            connection.execute("DELETE FROM page_items WHERE generation=? AND page_index=?",
                               (lease["generation"], 1))
        before = self.snapshot()
        with self.assertRaises(PageStoreError):
            self.store.ack(lease)
        with self.assertRaises(PageStoreError):
            self.make_store()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.meta()["index"], 0)

    def test_wal_busy_timeout_and_foreign_keys_are_configured(self):
        with self.store._connection() as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(connection.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(connection.execute("PRAGMA application_id").fetchone()[0], 0x57504731)
        self.assertEqual(self.path.parent, self.root / "离线 data # with spaces")
        self.assertTrue(self.path.is_file())

    def test_close_is_idempotent_and_future_operations_fail(self):
        lease, full_text, pages = self.prepare()
        self.store.close()
        self.store.close()
        for action in (lambda: self.store.prepare(SCOPE_A, USER_A, full_text, pages, SOURCE),
                       lambda: self.store.reserve_next(SCOPE_A, USER_A), lambda: self.store.ack(lease),
                       lambda: self.store.release(lease), lambda: self.store.renew(lease),
                       lambda: self.store.discard(SCOPE_A, USER_A), lambda: self.store.metadata()):
            with self.assertRaises(RuntimeError):
                action()
        reopened = self.make_store()
        self.assertTrue(reopened.ack(lease))


if __name__ == "__main__":
    unittest.main()
