"""Deterministic standard-library regression specifications for ReplyQueue.

Load the queue directly, without importing AstrBot or the plugin package.  All
queue data lives in automatically cleaned TemporaryDirectory instances; no
configured deployment state is opened.  The clock is advanced explicitly and
pump() only yields to the event loop, so these tests never sleep for a cooldown.

The force concurrency contract is deliberate: force tokens remain independent,
but claimed snapshots and other force tokens' reserved triggering packets must
not overlap.  Storage/validation failures must roll back in-memory state too.
Assertions for those guarantees should expose unfinished production fixes, not
be weakened to preserve the old behavior.
"""
from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


# Discovery and direct execution must not create plugin/package bytecode files.
sys.dont_write_bytecode = True
_QUEUE_FILE = Path(__file__).resolve().parents[1] / "reply_queue.py"
_SPEC = importlib.util.spec_from_file_location("_whale_reply_queue_unittest", _QUEUE_FILE)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError("Cannot load reply_queue.py for its isolated unittest suite")
_QUEUE_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _QUEUE_MODULE
_SPEC.loader.exec_module(_QUEUE_MODULE)
ReplyQueue = _QUEUE_MODULE.ReplyQueue

SCOPE_A = "aiocqhttp:GroupMessage:10001"
SCOPE_B = "aiocqhttp:GroupMessage:10002"
BAD_NUMBERS = (float("nan"), float("inf"), float("-inf"), True, False, "10", None)


def ids(batch):
    return [packet["message_id"] for packet in batch]


class Clock:
    def __init__(self, value=1000.0):
        self.value = value

    def __call__(self):
        return self.value

    def set(self, value):
        self.value = value

    def advance(self, seconds):
        self.value += seconds


class AsyncDispatch:
    """A local native-event stand-in; generation/finish are controlled by tests."""

    def __init__(self):
        self.queue = None
        self.calls = []
        self.sent = None
        self.error = None
        self.finish_results = []

    async def __call__(self, scope, payload, token):
        batch = self.queue.claim(scope, token)
        self.calls.append({"scope": scope, "payload": copy.deepcopy(payload),
                           "token": token, "batch": batch})
        if self.error is not None:
            raise self.error
        if self.sent is not None:
            self.finish_results.append(self.queue.finish(scope, token, sent=self.sent))


class ReplyQueueTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="whale-reply-queue-unittest-")
        self.root = Path(self.temporary.name).resolve()
        self.addCleanup(self.temporary.cleanup)
        self.clock = Clock()
        self.gap = 10.0
        self.quiet_seconds = 3600.0
        self.allowed = {}
        self.errors = []
        self.queues = []
        self.queue, self.dispatch = self.make_queue("queue.json")

    def make_queue(self, filename=None, dispatch=None, on_cancel=None, can_force_reply=None):
        filename = filename or "queue-{}.json".format(len(self.queues))
        probe = AsyncDispatch() if dispatch is None else dispatch
        queue = ReplyQueue(
            self.root / filename, probe, now=self.clock,
            gap_seconds=lambda: self.gap,
            can_reply=lambda scope: self.allowed.get(scope, True),
            quiet_delay=lambda: self.quiet_seconds,
            on_error=self.errors.append,
            on_cancel=on_cancel, can_force_reply=can_force_reply,
        )
        if isinstance(probe, AsyncDispatch):
            probe.queue = queue
        self.queues.append(queue)
        return queue, probe

    async def asyncTearDown(self):
        self.clock.set(1000.0)
        self.gap = 10.0
        self.quiet_seconds = 3600.0
        for queue in reversed(self.queues):
            # Do not resave an old, already closed owner over its restarted owner.
            if not queue._closed:
                await queue.close()

    def packet(self, message_id, text=None, **updates):
        packet = {
            "message_id": message_id,
            "text": "text " + message_id if text is None else text,
            "ts": self.clock(),
            "sender_id": "20001", "sender_name": "测试伙伴",
            "group_id": "10001", "platform_id": "aiocqhttp",
            "self_id": "30001", "session_id": "10001",
        }
        packet.update(updates)
        return packet

    def document(self, queue=None):
        queue = queue or self.queue
        if not queue.path.exists():
            return None
        return json.loads(queue.path.read_text(encoding="utf-8"))

    def state(self, queue=None, scope=SCOPE_A):
        document = self.document(queue)
        return None if document is None else document["scopes"].get(scope)

    def pending(self, queue=None, scope=SCOPE_A):
        state = self.state(queue, scope)
        return [] if state is None else ids(state["pending"])

    async def restart(self, queue=None):
        queue = queue or self.queue
        filename = queue.path.name
        await queue.close()
        return self.make_queue(filename)

    async def test_cooldown_9999_and_10_pump_without_new_message(self):
        self.queue.external_reply(SCOPE_A)
        self.assertIsNone(self.queue.offer(SCOPE_A, self.packet("waiting")))
        self.clock.set(1009.999)
        self.assertEqual(await self.queue.pump(), 0)
        self.assertEqual(self.dispatch.calls, [])
        self.assertEqual(self.pending(), ["waiting"])
        self.clock.set(1010.0)
        self.assertEqual(await self.queue.pump(), 1)
        self.assertEqual(len(self.dispatch.calls), 1)
        self.assertEqual(ids(self.dispatch.calls[0]["batch"]), ["waiting"])
        self.assertNotIn("seq", self.dispatch.calls[0]["payload"])

    async def test_offer_at_deadline_returns_deterministic_native_token(self):
        self.queue.external_reply(SCOPE_A)
        self.clock.set(1009.999)
        self.assertIsNone(self.queue.offer(SCOPE_A, self.packet("before")))
        self.clock.set(1010.0)
        token = self.queue.offer(SCOPE_A, self.packet("at-deadline"))
        self.assertIsNotNone(token)
        self.assertEqual(ids(self.queue.claim(SCOPE_A, token)), ["before", "at-deadline"])
        self.assertTrue(self.queue.finish(SCOPE_A, token))
        # Ordinary messages at every next deadline get a token, never a random draw.
        for index in range(12):
            self.clock.advance(10.0)
            message_id = "ordinary-{}".format(index)
            token = self.queue.offer(SCOPE_A, self.packet(message_id))
            self.assertIsNotNone(token)
            self.assertEqual(ids(self.queue.claim(SCOPE_A, token)), [message_id])
            self.assertTrue(self.queue.finish(SCOPE_A, token))
        self.assertEqual(self.dispatch.calls, [])

    async def test_no_pending_means_no_dispatch_and_no_self_chat(self):
        self.assertEqual(await self.queue.pump(), 0)
        self.queue.external_reply(SCOPE_A)
        self.clock.advance(10000.0)
        self.assertEqual(await self.queue.pump(), 0)
        token = self.queue.offer(SCOPE_A, self.packet("last-user-message"))
        self.assertIsNotNone(token)
        self.queue.claim(SCOPE_A, token)
        self.assertTrue(self.queue.finish(SCOPE_A, token))
        for jump in (9.999, 0.001, 100000.0):
            self.clock.advance(jump)
            self.assertEqual(await self.queue.pump(), 0)
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.dispatch.calls, [])

    async def test_cooldown_aggregates_multiple_messages_in_order(self):
        self.queue.external_reply(SCOPE_A)
        expected = []
        for index in range(6):
            message_id = "batch-{}".format(index)
            expected.append(message_id)
            self.assertIsNone(self.queue.offer(
                SCOPE_A, self.packet(message_id, sender_id=str(index), sender_name="朋友" + str(index))))
            self.clock.advance(1.0)
        self.assertEqual(await self.queue.pump(), 0)
        self.clock.set(1010.0)
        self.assertEqual(await self.queue.pump(), 1)
        batch = self.dispatch.calls[0]["batch"]
        self.assertEqual(ids(batch), expected)
        self.assertEqual([p["sender_id"] for p in batch], [str(i) for i in range(6)])
        self.assertEqual([p["ts"] for p in batch], [1000.0 + i for i in range(6)])

    async def test_new_arrivals_during_reply_are_next_batch_and_gap_starts_at_finish(self):
        token = self.queue.offer(SCOPE_A, self.packet("first"))
        self.assertIsNone(self.queue.offer(SCOPE_A, self.packet("second")))
        self.assertEqual(ids(self.queue.claim(SCOPE_A, token)), ["first", "second"])
        self.clock.advance(30.0)
        self.assertIsNone(self.queue.offer(SCOPE_A, self.packet("during-generation")))
        self.assertEqual(await self.queue.pump(), 0)
        self.clock.advance(20.0)
        self.assertTrue(self.queue.finish(SCOPE_A, token))
        self.assertEqual(self.state()["last_reply"], 1050.0)
        self.assertEqual(self.pending(), ["during-generation"])
        self.clock.set(1059.999)
        self.assertEqual(await self.queue.pump(), 0)
        self.clock.set(1060.0)
        self.assertEqual(await self.queue.pump(), 1)
        self.assertEqual(ids(self.dispatch.calls[0]["batch"]), ["during-generation"])

    async def test_claim_is_frozen_and_returns_deep_copies(self):
        payload = self.packet("original", "original text")
        token = self.queue.offer(SCOPE_A, payload)
        payload["text"] = "changed outside queue"
        payload["sender_name"] = "changed outside queue"
        first = self.queue.claim(SCOPE_A, token)
        first[0]["text"] = "changed returned copy"
        first[0]["seq"] = 999
        self.assertIsNone(self.queue.offer(SCOPE_A, self.packet("arrived-after-claim")))
        second = self.queue.claim(SCOPE_A, token)
        self.assertEqual(ids(second), ["original"])
        self.assertEqual(second[0]["text"], "original text")
        self.assertEqual(second[0]["sender_name"], "测试伙伴")
        self.assertEqual(second[0]["seq"], 1)
        self.assertTrue(self.queue.finish(SCOPE_A, token))
        self.assertEqual(self.pending(), ["arrived-after-claim"])

    async def test_live_job_is_not_dispatched_twice(self):
        self.queue.external_reply(SCOPE_A)
        self.queue.offer(SCOPE_A, self.packet("waiting"))
        self.clock.advance(10.0)
        self.assertEqual(await self.queue.pump(), 1)
        token = self.dispatch.calls[0]["token"]
        self.assertTrue(self.queue.is_valid(SCOPE_A, token))
        for jump in (0.0, 10.0, 10.0):
            self.clock.advance(jump)
            self.assertEqual(await self.queue.pump(), 0)
        self.assertEqual(len(self.dispatch.calls), 1)

    async def test_force_preempts_unclaimed_normal_token_and_restarts_gap(self):
        ordinary = self.queue.offer(SCOPE_A, self.packet("ordinary"))
        self.clock.advance(1.0)
        forced = self.queue.offer(SCOPE_A, self.packet("addressed", "大肥鱼，回答呀"), force=True)
        self.assertIsNotNone(forced)
        self.assertNotEqual(ordinary, forced)
        self.assertFalse(self.queue.is_valid(SCOPE_A, ordinary))
        self.assertIsNone(self.queue.claim(SCOPE_A, ordinary))
        before = self.document()
        self.assertFalse(self.queue.finish(SCOPE_A, ordinary))
        self.assertEqual(self.document(), before)
        self.assertEqual(ids(self.queue.claim(SCOPE_A, forced)), ["ordinary", "addressed"])
        self.clock.advance(1.0)
        self.assertTrue(self.queue.finish(SCOPE_A, forced))
        self.assertIsNone(self.queue.offer(SCOPE_A, self.packet("cooldown")))
        self.clock.set(1011.999)
        self.assertEqual(await self.queue.pump(), 0)
        self.clock.set(1012.0)
        self.assertEqual(await self.queue.pump(), 1)
        self.assertEqual(ids(self.dispatch.calls[0]["batch"]), ["cooldown"])

    async def test_force_preempts_claimed_normal_without_losing_original_messages(self):
        ordinary = self.queue.offer(SCOPE_A, self.packet("claimed-normal"))
        self.assertEqual(ids(self.queue.claim(SCOPE_A, ordinary)), ["claimed-normal"])
        self.queue.offer(SCOPE_A, self.packet("arrived-in-generation"))
        forced = self.queue.offer(SCOPE_A, self.packet("at-request", "@bot 请回答"), force=True)
        self.assertFalse(self.queue.is_valid(SCOPE_A, ordinary))
        before = self.document()
        self.assertFalse(self.queue.finish(SCOPE_A, ordinary, sent=True))
        self.assertFalse(self.queue.finish(SCOPE_A, ordinary, sent=False))
        self.assertEqual(self.document(), before)
        self.assertEqual(ids(self.queue.claim(SCOPE_A, forced)),
                         ["claimed-normal", "arrived-in-generation", "at-request"])
        self.assertTrue(self.queue.finish(SCOPE_A, forced))
        self.assertEqual(self.pending(), [])

    async def test_force_cancels_inflight_deferred_normal_dispatch(self):
        started, cancelled, hold = asyncio.Event(), asyncio.Event(), asyncio.Event()
        claimed = []

        async def held_dispatch(scope, payload, token):
            claimed.extend(ids(self.queue.claim(scope, token)))
            started.set()
            try:
                await hold.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        self.queue.dispatch = held_dispatch
        self.queue.external_reply(SCOPE_A)
        self.queue.offer(SCOPE_A, self.packet("deferred-normal"))
        self.clock.advance(10.0)
        self.assertEqual(await self.queue.pump(), 1)
        self.assertTrue(started.is_set(), "pump must start, not await, generation")
        self.assertEqual(claimed, ["deferred-normal"])
        normal = next(iter(self.queue._jobs))
        forced = self.queue.offer(SCOPE_A, self.packet("interrupt", "鲸鲸"), force=True)
        self.assertIsNotNone(forced)
        self.assertEqual(await self.queue.pump(), 0)  # Also lets cancellation run; no real sleep.
        self.assertTrue(cancelled.is_set())
        self.assertFalse(self.queue.finish(SCOPE_A, normal))
        self.assertEqual(ids(self.queue.claim(SCOPE_A, forced)), ["deferred-normal", "interrupt"])
        self.assertTrue(self.queue.finish(SCOPE_A, forced))

    async def test_consecutive_forces_reserve_trigger_ids_in_both_claim_orders(self):
        for first_name, second_name in (("force-a", "force-b"), ("force-b", "force-a")):
            with self.subTest(first_claim=first_name):
                queue, _ = self.make_queue()
                normal = queue.offer(SCOPE_A, self.packet("backlog-a"))
                queue.offer(SCOPE_A, self.packet("backlog-b"))
                tokens = {
                    "force-a": queue.offer(SCOPE_A, self.packet("force-a"), force=True),
                    "force-b": queue.offer(SCOPE_A, self.packet("force-b"), force=True),
                }
                self.assertIsNotNone(tokens["force-a"])
                self.assertIsNotNone(tokens["force-b"])
                self.assertNotEqual(tokens["force-a"], tokens["force-b"])
                self.assertFalse(queue.is_valid(SCOPE_A, normal))
                first = queue.claim(SCOPE_A, tokens[first_name])
                second = queue.claim(SCOPE_A, tokens[second_name])
                self.assertEqual(ids(first), ["backlog-a", "backlog-b", first_name])
                self.assertEqual(ids(second), [second_name])
                self.assertTrue(set(ids(first)).isdisjoint(ids(second)))
                self.assertTrue(queue.is_valid(SCOPE_A, tokens[first_name]))
                self.assertTrue(queue.is_valid(SCOPE_A, tokens[second_name]))
                self.assertTrue(queue.finish(SCOPE_A, tokens[first_name]))
                self.assertEqual(self.pending(queue), [second_name])
                self.assertTrue(queue.finish(SCOPE_A, tokens[second_name]))
                self.assertEqual(self.pending(queue), [])

    async def test_force_after_another_claim_does_not_overlap_snapshot(self):
        normal = self.queue.offer(SCOPE_A, self.packet("backlog"))
        first = self.queue.offer(SCOPE_A, self.packet("force-a"), force=True)
        batch_a = self.queue.claim(SCOPE_A, first)
        second = self.queue.offer(SCOPE_A, self.packet("force-b"), force=True)
        batch_b = self.queue.claim(SCOPE_A, second)
        self.assertFalse(self.queue.is_valid(SCOPE_A, normal))
        self.assertEqual(ids(batch_a), ["backlog", "force-a"])
        self.assertEqual(ids(batch_b), ["force-b"])
        self.assertEqual(self.queue.claim(SCOPE_A, first), batch_a)
        self.assertTrue(set(ids(batch_a)).isdisjoint(ids(batch_b)))
        self.queue.offer(SCOPE_A, self.packet("next-batch"))
        self.clock.advance(1.0)
        self.assertTrue(self.queue.finish(SCOPE_A, second))  # Out-of-order completion.
        self.assertEqual(self.pending(), ["backlog", "force-a", "next-batch"])
        self.clock.advance(1.0)
        self.assertTrue(self.queue.finish(SCOPE_A, first))
        self.assertEqual(self.pending(), ["next-batch"])
        self.assertEqual(self.state()["covered"], 3)
        self.clock.set(1011.999)
        self.assertEqual(await self.queue.pump(), 0)
        self.clock.set(1012.0)
        self.assertEqual(await self.queue.pump(), 1)
        self.assertEqual(ids(self.dispatch.calls[0]["batch"]), ["next-batch"])

    async def test_failed_force_releases_its_batch_without_eating_other_force(self):
        self.queue.offer(SCOPE_A, self.packet("backlog"))
        first = self.queue.offer(SCOPE_A, self.packet("force-a"), force=True)
        self.assertEqual(ids(self.queue.claim(SCOPE_A, first)), ["backlog", "force-a"])
        second = self.queue.offer(SCOPE_A, self.packet("force-b"), force=True)
        self.assertEqual(ids(self.queue.claim(SCOPE_A, second)), ["force-b"])
        self.assertTrue(self.queue.finish(SCOPE_A, first, sent=False))
        self.assertEqual(self.pending(), ["backlog", "force-a", "force-b"])
        self.assertTrue(self.queue.is_valid(SCOPE_A, second))
        self.assertEqual(await self.queue.pump(), 0)
        self.assertTrue(self.queue.finish(SCOPE_A, second))
        self.assertEqual(self.pending(), ["backlog", "force-a"])
        self.clock.advance(10.0)
        self.assertEqual(await self.queue.pump(), 1)
        self.assertEqual(ids(self.dispatch.calls[0]["batch"]), ["backlog", "force-a"])

    async def test_force_bypasses_retry_backoff(self):
        failed = self.queue.offer(SCOPE_A, self.packet("failed-normal"))
        self.queue.claim(SCOPE_A, failed)
        self.assertTrue(self.queue.finish(SCOPE_A, failed, sent=False))
        self.assertEqual(self.state()["retry_at"], 1010.0)
        self.clock.advance(1.0)
        forced = self.queue.offer(SCOPE_A, self.packet("force-retry"), force=True)
        self.assertIsNotNone(forced)
        self.assertEqual(ids(self.queue.claim(SCOPE_A, forced)), ["failed-normal", "force-retry"])
        self.assertTrue(self.queue.finish(SCOPE_A, forced))
        self.assertEqual(self.state()["failures"], 0)
        self.assertEqual(self.state()["retry_at"], 0.0)

    async def test_quiet_gate_blocks_ordinary_and_force_then_releases_without_new_message(self):
        self.allowed[SCOPE_A] = False
        self.assertIsNone(self.queue.offer(SCOPE_A, self.packet("quiet-ordinary")))
        self.assertIsNone(self.queue.offer(SCOPE_A, self.packet("quiet-force"), force=True))
        self.assertEqual(self.queue._next_delay(), 3600.0)
        self.clock.advance(3600.0)
        self.assertEqual(await self.queue.pump(), 0)
        self.assertIsNone(self.state()["last_reply"])
        self.assertEqual(self.pending(), ["quiet-ordinary", "quiet-force"])
        self.allowed[SCOPE_A] = True
        self.assertEqual(await self.queue.pump(), 1)
        self.assertEqual(ids(self.dispatch.calls[0]["batch"]), ["quiet-ordinary", "quiet-force"])

    async def test_finish_without_claim_only_covers_trigger_message(self):
        normal = self.queue.offer(SCOPE_A, self.packet("unanswered-earlier"))
        forced = self.queue.offer(SCOPE_A, self.packet("command-answer"), force=True)
        self.assertFalse(self.queue.finish(SCOPE_A, normal))
        self.assertTrue(self.queue.finish(SCOPE_A, forced))
        self.assertEqual(self.pending(), ["unanswered-earlier"])
        self.assertEqual(self.state()["covered"], 0)
        queue, probe = await self.restart()
        self.assertEqual(self.pending(queue), ["unanswered-earlier"])
        self.clock.advance(10.0)
        self.assertEqual(await queue.pump(), 1)
        self.assertEqual(ids(probe.calls[0]["batch"]), ["unanswered-earlier"])

    async def test_failed_finish_keeps_messages_and_retries_at_boundary(self):
        token = self.queue.offer(SCOPE_A, self.packet("retry-a"))
        self.queue.offer(SCOPE_A, self.packet("retry-b"))
        self.assertEqual(ids(self.queue.claim(SCOPE_A, token)), ["retry-a", "retry-b"])
        self.assertTrue(self.queue.finish(SCOPE_A, token, sent=False))
        self.assertFalse(self.queue.is_valid(SCOPE_A, token))
        self.assertIsNone(self.state()["last_reply"])
        self.assertEqual(self.pending(), ["retry-a", "retry-b"])
        self.clock.set(1009.999)
        self.assertEqual(await self.queue.pump(), 0)
        self.clock.set(1010.0)
        self.assertEqual(await self.queue.pump(), 1)
        retry = self.dispatch.calls[0]
        self.assertNotEqual(retry["token"], token)
        self.assertEqual(ids(retry["batch"]), ["retry-a", "retry-b"])
        self.assertTrue(self.queue.finish(SCOPE_A, retry["token"]))
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.state()["failures"], 0)
        self.assertEqual(self.state()["retry_at"], 0.0)

    async def test_dispatch_exception_reports_and_retries_without_loss(self):
        self.queue.external_reply(SCOPE_A)
        self.queue.offer(SCOPE_A, self.packet("local-dispatch-error"))
        failure = RuntimeError("simulated local event dispatch failure")
        self.dispatch.error = failure
        self.clock.advance(10.0)
        self.assertEqual(await self.queue.pump(), 1)
        self.assertEqual(self.errors, [failure])
        self.assertEqual(self.pending(), ["local-dispatch-error"])
        self.assertEqual(self.state()["last_reply"], 1000.0)
        self.assertEqual(self.state()["retry_at"], 1020.0)
        self.dispatch.error = None
        self.dispatch.sent = True
        self.clock.set(1019.999)
        self.assertEqual(await self.queue.pump(), 0)
        self.clock.set(1020.0)
        self.assertEqual(await self.queue.pump(), 1)
        self.assertEqual(ids(self.dispatch.calls[-1]["batch"]), ["local-dispatch-error"])
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.dispatch.finish_results, [True])
        self.clock.advance(10.0)
        self.assertEqual(await self.queue.pump(), 0)

    async def test_exponential_retry_is_capped_and_success_resets_failures(self):
        token = self.queue.offer(SCOPE_A, self.packet("persistent-failure"))
        self.queue.claim(SCOPE_A, token)
        self.assertTrue(self.queue.finish(SCOPE_A, token, sent=False))
        self.dispatch.sent = False
        for count, expected_delay in enumerate((10.0, 20.0, 40.0, 60.0, 60.0, 60.0), 1):
            self.assertEqual(self.state()["failures"], count)
            deadline = self.state()["retry_at"]
            self.assertEqual(deadline - self.clock(), expected_delay)
            self.clock.set(deadline - 0.001)
            self.assertEqual(await self.queue.pump(), 0)
            self.clock.set(deadline)
            self.assertEqual(await self.queue.pump(), 1)
            self.assertEqual(self.pending(), ["persistent-failure"])
        self.dispatch.sent = True
        self.clock.set(self.state()["retry_at"])
        self.assertEqual(await self.queue.pump(), 1)
        self.assertEqual(self.state()["failures"], 0)
        self.assertEqual(self.state()["retry_at"], 0.0)
        self.assertEqual(self.pending(), [])

    async def test_unclaimed_lease_expiration_retains_messages_and_retries(self):
        token = self.queue.offer(SCOPE_A, self.packet("leased"))
        deadline = 1000.0 + self.queue.LEASE_SECONDS
        self.clock.set(deadline - 0.001)
        self.assertTrue(self.queue.is_valid(SCOPE_A, token))
        self.clock.set(deadline)
        self.assertFalse(self.queue.is_valid(SCOPE_A, token))
        self.assertIsNone(self.queue.claim(SCOPE_A, token))
        self.assertFalse(self.queue.finish(SCOPE_A, token))
        self.assertEqual(self.pending(), ["leased"])
        self.assertIsNone(self.state()["last_reply"])
        self.assertEqual(self.state()["retry_at"], deadline + 10.0)
        self.clock.set(deadline + 9.999)
        self.assertEqual(await self.queue.pump(), 0)
        self.clock.set(deadline + 10.0)
        self.assertEqual(await self.queue.pump(), 1)
        self.assertEqual(ids(self.dispatch.calls[0]["batch"]), ["leased"])

    async def test_first_claim_refreshes_lease_but_repeat_claim_does_not(self):
        token = self.queue.offer(SCOPE_A, self.packet("claim-lease"))
        self.clock.advance(self.queue.LEASE_SECONDS - 10.0)
        first_claim_time = self.clock()
        snapshot = self.queue.claim(SCOPE_A, token)
        self.clock.set(1000.0 + self.queue.LEASE_SECONDS)
        self.assertTrue(self.queue.is_valid(SCOPE_A, token))
        renewed_deadline = first_claim_time + self.queue.LEASE_SECONDS
        self.clock.set(renewed_deadline - 0.001)
        self.assertEqual(self.queue.claim(SCOPE_A, token), snapshot)
        self.clock.set(renewed_deadline)
        self.assertFalse(self.queue.is_valid(SCOPE_A, token))
        self.assertFalse(self.queue.finish(SCOPE_A, token))
        self.assertEqual(self.pending(), ["claim-lease"])

    async def test_restart_restores_claimed_unsent_batch_without_waiting_for_lease(self):
        token = self.queue.offer(SCOPE_A, self.packet("restart-a"))
        self.queue.offer(SCOPE_A, self.packet("restart-b"))
        self.assertEqual(ids(self.queue.claim(SCOPE_A, token)), ["restart-a", "restart-b"])
        self.clock.advance(5.0)
        queue, probe = await self.restart()
        self.assertFalse(self.queue.is_valid(SCOPE_A, token))
        self.assertFalse(self.queue.finish(SCOPE_A, token))
        self.assertEqual(self.pending(queue), ["restart-a", "restart-b"])
        self.assertEqual(await queue.pump(), 1)
        self.assertNotEqual(probe.calls[0]["token"], token)
        self.assertEqual(ids(probe.calls[0]["batch"]), ["restart-a", "restart-b"])

    async def test_restart_preserves_cooldown_and_retry_deadlines(self):
        token = self.queue.offer(SCOPE_A, self.packet("already-sent"))
        self.queue.claim(SCOPE_A, token)
        self.queue.finish(SCOPE_A, token)
        self.queue.offer(SCOPE_A, self.packet("after-reply"))
        queue, probe = await self.restart()
        self.clock.set(1009.999)
        self.assertEqual(await queue.pump(), 0)
        self.clock.set(1010.0)
        self.assertEqual(await queue.pump(), 1)
        self.assertTrue(queue.finish(SCOPE_A, probe.calls[0]["token"], sent=False))
        queue, probe = await self.restart(queue)
        self.assertEqual(self.pending(queue), ["after-reply"])
        self.assertEqual(self.state(queue)["last_reply"], 1000.0)
        self.assertEqual(self.state(queue)["retry_at"], 1020.0)
        self.clock.set(1019.999)
        self.assertEqual(await queue.pump(), 0)
        self.clock.set(1020.0)
        self.assertEqual(await queue.pump(), 1)
        self.assertEqual(ids(probe.calls[0]["batch"]), ["after-reply"])

    async def test_restart_accepts_noncontiguous_covered_snapshot(self):
        self.assertEqual((self.queue.MAX_PACKET_CHARS, self.queue.MAX_TEXT_CHARS), (4000, 6000))
        normal = self.queue.offer(SCOPE_A, self.packet("seq-1", "a" * 4000))
        self.queue.offer(SCOPE_A, self.packet("seq-2", "b" * 3000))
        forced = self.queue.offer(SCOPE_A, self.packet("seq-3", "c" * 1500), force=True)
        self.assertFalse(self.queue.is_valid(SCOPE_A, normal))
        batch = self.queue.claim(SCOPE_A, forced)
        self.assertEqual(ids(batch), ["seq-1", "seq-3"])
        self.assertEqual(sum(len(p["text"]) for p in batch), 5500)
        self.assertTrue(self.queue.finish(SCOPE_A, forced))
        self.assertEqual(self.pending(), ["seq-2"])
        self.assertEqual(self.state()["covered"], 1)
        self.assertEqual(self.state()["seq"], 3)
        queue, probe = await self.restart()
        self.assertEqual(self.pending(queue), ["seq-2"])
        self.assertEqual(self.state(queue)["covered"], 1)
        self.assertIsNone(queue.offer(SCOPE_A, self.packet("seq-1"), force=True))
        self.assertIsNone(queue.offer(SCOPE_A, self.packet("seq-3"), force=True))
        self.clock.advance(10.0)
        self.assertEqual(await queue.pump(), 1)
        self.assertEqual(ids(probe.calls[0]["batch"]), ["seq-2"])
        self.assertTrue(queue.finish(SCOPE_A, probe.calls[0]["token"]))
        self.assertEqual(self.state(queue)["covered"], 3)
        self.assertEqual(self.pending(queue), [])

    async def test_real_atomic_save_is_utf8_and_reopens_same_state(self):
        real_replace = _QUEUE_MODULE.os.replace
        real_fsync = _QUEUE_MODULE.os.fsync
        with mock.patch.object(_QUEUE_MODULE.os, "replace", wraps=real_replace) as replace, \
                mock.patch.object(_QUEUE_MODULE.os, "fsync", wraps=real_fsync) as fsync:
            token = self.queue.offer(SCOPE_A, self.packet("真实ID-鲸鲸", "你好，伙伴"))
        replace.assert_called_once_with(str(self.queue.path) + ".pending", str(self.queue.path))
        fsync.assert_called_once()
        self.assertFalse(self.queue.path.with_name(self.queue.path.name + ".pending").exists())
        text = self.queue.path.read_text(encoding="utf-8")
        self.assertIn("真实ID-鲸鲸", text)
        self.assertIn("你好，伙伴", text)
        self.assertEqual(json.loads(text)["version"], 1)
        self.queue.claim(SCOPE_A, token)
        before = self.document()
        queue, _ = await self.restart()
        self.assertEqual(self.document(queue), before)
        self.assertEqual(queue._scopes, before["scopes"])

    async def test_packet_limit_retains_full_ids_and_all_remaining_packets(self):
        self.assertEqual(self.queue.MAX_PACKETS, 32)
        all_ids = [("id-" + "x" * 233 + "{:04d}".format(i)) for i in range(35)]
        self.assertTrue(all(len(message_id) == 240 for message_id in all_ids))
        token = self.queue.offer(SCOPE_A, self.packet(all_ids[0], "x"))
        for message_id in all_ids[1:]:
            self.assertIsNone(self.queue.offer(SCOPE_A, self.packet(message_id, "x")))
        first = self.queue.claim(SCOPE_A, token)
        self.assertEqual(ids(first), all_ids[:32])
        self.assertEqual(len(first), self.queue.MAX_PACKETS)
        self.assertTrue(self.queue.finish(SCOPE_A, token))
        self.assertEqual(self.pending(), all_ids[32:])
        self.clock.advance(10.0)
        self.assertEqual(await self.queue.pump(), 1)
        second = self.dispatch.calls[0]["batch"]
        self.assertEqual(ids(first) + ids(second), all_ids)
        self.assertTrue(self.queue.finish(SCOPE_A, self.dispatch.calls[0]["token"]))
        self.assertEqual(self.pending(), [])
        self.assertEqual(set(self.state()["seen"]), set(all_ids))

    async def test_force_packet_limit_includes_trigger_and_retains_unselected_ids(self):
        all_ids = ["force-budget-{:03d}".format(i) for i in range(35)]
        normal = self.queue.offer(SCOPE_A, self.packet(all_ids[0], "x"))
        for message_id in all_ids[1:-1]:
            self.queue.offer(SCOPE_A, self.packet(message_id, "x"))
        forced = self.queue.offer(SCOPE_A, self.packet(all_ids[-1], "x"), force=True)
        self.assertFalse(self.queue.is_valid(SCOPE_A, normal))
        first = self.queue.claim(SCOPE_A, forced)
        self.assertEqual(ids(first), all_ids[:31] + [all_ids[-1]])
        self.assertEqual(len(first), self.queue.MAX_PACKETS)
        self.assertTrue(self.queue.finish(SCOPE_A, forced))
        self.assertEqual(self.pending(), all_ids[31:-1])
        self.assertEqual(self.state()["covered"], 31)
        queue, probe = await self.restart()
        self.clock.advance(10.0)
        self.assertEqual(await queue.pump(), 1)
        second = probe.calls[0]["batch"]
        self.assertEqual(ids(second), all_ids[31:-1])
        self.assertEqual(set(ids(first) + ids(second)), set(all_ids))
        self.assertEqual(len(ids(first) + ids(second)), len(all_ids))
        self.assertTrue(queue.finish(SCOPE_A, probe.calls[0]["token"]))
        self.assertEqual(self.state(queue)["covered"], 35)

    async def test_text_budget_exact_boundary_and_per_packet_truncation(self):
        first_id, second_id, third_id = "z" * 239 + "1", "z" * 239 + "2", "z" * 239 + "3"
        token = self.queue.offer(SCOPE_A, self.packet(first_id, "甲" * 4001))
        self.queue.offer(SCOPE_A, self.packet(second_id, "乙" * 2000))
        self.queue.offer(SCOPE_A, self.packet(third_id, "丙"))
        batch = self.queue.claim(SCOPE_A, token)
        self.assertEqual(ids(batch), [first_id, second_id])
        self.assertEqual([len(p["text"]) for p in batch], [4000, 2000])
        self.assertEqual(sum(len(p["text"]) for p in batch), self.queue.MAX_TEXT_CHARS)
        self.assertTrue(self.queue.finish(SCOPE_A, token))
        self.assertEqual(self.pending(), [third_id])
        self.clock.advance(10.0)
        self.assertEqual(await self.queue.pump(), 1)
        self.assertEqual(ids(self.dispatch.calls[0]["batch"]), [third_id])

    async def test_force_text_budget_excludes_oversized_backlog_but_keeps_complete_ids(self):
        original = self.queue.offer(SCOPE_A, self.packet("old-4000", "a" * 4000))
        self.queue.offer(SCOPE_A, self.packet("old-1000", "b" * 1000))
        trigger_id = "t" * 240
        forced = self.queue.offer(SCOPE_A, self.packet(trigger_id, "c" * 4000), force=True)
        self.assertFalse(self.queue.is_valid(SCOPE_A, original))
        batch = self.queue.claim(SCOPE_A, forced)
        self.assertEqual(ids(batch), [trigger_id])
        self.assertLessEqual(sum(len(p["text"]) for p in batch), self.queue.MAX_TEXT_CHARS)
        self.assertTrue(self.queue.finish(SCOPE_A, forced))
        self.assertEqual(self.pending(), ["old-4000", "old-1000"])
        self.assertEqual(self.state()["covered"], 0)
        self.clock.advance(10.0)
        self.assertEqual(await self.queue.pump(), 1)
        self.assertEqual(ids(self.dispatch.calls[0]["batch"]), ["old-4000", "old-1000"])

    async def test_scope_isolation_identical_ids_preemption_and_deadlines(self):
        token_a = self.queue.offer(SCOPE_A, self.packet("same-id", "group A"))
        token_b = self.queue.offer(SCOPE_B, self.packet("same-id", "group B", group_id="10002"))
        self.assertIsNotNone(token_a)
        self.assertIsNotNone(token_b)
        self.assertEqual(self.queue.claim(SCOPE_A, token_a)[0]["text"], "group A")
        self.assertEqual(self.queue.claim(SCOPE_B, token_b)[0]["text"], "group B")
        before = self.document()
        self.assertIsNone(self.queue.claim(SCOPE_A, token_b))
        self.assertFalse(self.queue.is_valid(SCOPE_A, token_b))
        self.assertFalse(self.queue.finish(SCOPE_A, token_b))
        self.assertEqual(self.document(), before)
        force_a = self.queue.offer(SCOPE_A, self.packet("force-only-A"), force=True)
        self.assertFalse(self.queue.is_valid(SCOPE_A, token_a))
        self.assertTrue(self.queue.is_valid(SCOPE_B, token_b))
        self.assertEqual(ids(self.queue.claim(SCOPE_A, force_a)), ["same-id", "force-only-A"])
        self.queue.finish(SCOPE_A, force_a)
        self.clock.advance(1.0)
        self.queue.finish(SCOPE_B, token_b)
        self.assertIsNone(self.queue.offer(SCOPE_A, self.packet("pending-A")))
        self.assertIsNone(self.queue.offer(SCOPE_B, self.packet("pending-B", group_id="10002")))
        self.clock.set(1010.0)
        self.assertEqual(await self.queue.pump(), 1)
        self.assertEqual(self.dispatch.calls[0]["scope"], SCOPE_A)
        self.assertEqual(ids(self.dispatch.calls[0]["batch"]), ["pending-A"])
        self.clock.set(1011.0)
        self.assertEqual(await self.queue.pump(), 1)
        self.assertEqual(self.dispatch.calls[1]["scope"], SCOPE_B)
        self.assertEqual(ids(self.dispatch.calls[1]["batch"]), ["pending-B"])

    async def test_recent_id_dedup_pending_finished_and_after_restart(self):
        token = self.queue.offer(SCOPE_A, self.packet("duplicate", "original"))
        self.assertIsNone(self.queue.offer(SCOPE_A, self.packet("duplicate", "replacement"), force=True))
        self.assertTrue(self.queue.is_valid(SCOPE_A, token))
        self.assertEqual(self.pending(), ["duplicate"])
        self.assertEqual(self.state()["seq"], 1)
        self.assertEqual(self.queue.claim(SCOPE_A, token)[0]["text"], "original")
        self.queue.finish(SCOPE_A, token)
        self.clock.advance(1.0)
        self.assertIsNone(self.queue.offer(SCOPE_A, self.packet("duplicate"), force=True))
        self.assertEqual(self.state()["last_reply"], 1000.0)
        queue, _ = await self.restart()
        self.assertIsNone(queue.offer(SCOPE_A, self.packet("duplicate"), force=True))
        self.assertEqual(self.state(queue)["seq"], 1)
        self.assertEqual(self.pending(queue), [])
        self.assertIsNotNone(queue.offer(SCOPE_B, self.packet("duplicate")))

    async def test_recent_seen_pruning_never_evicts_pending_ids(self):
        with mock.patch.object(self.queue, "RECENT_SEEN_LIMIT", 3):
            pending_token = self.queue.offer(SCOPE_A, self.packet("old-pending"))
            for index in range(6):
                token = self.queue.offer(SCOPE_A, self.packet("finished-{}".format(index)), force=True)
                self.assertTrue(self.queue.finish(SCOPE_A, token))  # Only its unclaimed trigger.
            self.assertFalse(self.queue.is_valid(SCOPE_A, pending_token))
            self.assertEqual(self.pending(), ["old-pending"])
            self.assertEqual(set(self.state()["seen"]),
                             {"old-pending", "finished-3", "finished-4", "finished-5"})
            self.assertIsNone(self.queue.offer(SCOPE_A, self.packet("old-pending"), force=True))
            self.assertIsNone(self.queue.offer(SCOPE_A, self.packet("finished-5"), force=True))
            self.assertIsNotNone(self.queue.offer(SCOPE_A, self.packet("finished-0"), force=True))

    async def test_nonfinite_packet_timestamp_rejected_without_mutation(self):
        for bad in BAD_NUMBERS:
            with self.subTest(ts=repr(bad)):
                before = copy.deepcopy(self.queue._scopes)
                with self.assertRaises(ValueError):
                    self.queue.offer(SCOPE_A, self.packet("bad-ts", ts=bad))
                self.assertEqual(self.queue._scopes, before)
                self.assertFalse(self.queue.path.exists())
        token = self.queue.offer(SCOPE_A, self.packet("bad-ts"))
        self.assertIsNotNone(token)
        self.assertEqual(ids(self.queue.claim(SCOPE_A, token)), ["bad-ts"])

    async def test_nonfinite_clock_offer_rejected_without_mutation(self):
        for bad in BAD_NUMBERS:
            with self.subTest(now=repr(bad)):
                self.clock.set(bad)
                try:
                    with self.assertRaises(ValueError):
                        self.queue.offer(SCOPE_A, {"message_id": "bad-now", "text": "x", "ts": 1000.0})
                    self.assertEqual(self.queue._scopes, {})
                    self.assertFalse(self.queue.path.exists())
                finally:
                    self.clock.set(1000.0)
        self.assertIsNotNone(self.queue.offer(SCOPE_A, self.packet("bad-now")))

    async def test_nonfinite_gap_offer_rejection_does_not_mutate_pending_or_seen(self):
        for index, bad in enumerate(BAD_NUMBERS):
            with self.subTest(gap=repr(bad)):
                queue, _ = self.make_queue("bad-gap-{}.json".format(index))
                queue.external_reply(SCOPE_A)
                before_live, before_disk = copy.deepcopy(queue._scopes), self.document(queue)
                self.gap = bad
                try:
                    with self.assertRaises(ValueError):
                        queue.offer(SCOPE_A, self.packet("rejected-gap"))
                    self.assertEqual(queue._scopes, before_live,
                                     "rejected input must not increment seq or enter pending/seen")
                    self.assertEqual(self.document(queue), before_disk)
                finally:
                    self.gap = 10.0
                self.clock.advance(10.0)
                token = queue.offer(SCOPE_A, self.packet("rejected-gap"))
                self.assertIsNotNone(token, "the rejected ID must remain admissible")

    async def test_nonfinite_clock_cannot_claim_validate_finish_or_pump(self):
        token = self.queue.offer(SCOPE_A, self.packet("clock-guard"))
        self.queue.claim(SCOPE_A, token)
        before_live, before_disk = copy.deepcopy(self.queue._scopes), self.document()
        for bad in BAD_NUMBERS:
            with self.subTest(now=repr(bad)):
                self.clock.set(bad)
                try:
                    for operation in (lambda: self.queue.claim(SCOPE_A, token),
                                      lambda: self.queue.is_valid(SCOPE_A, token),
                                      lambda: self.queue.keepalive(SCOPE_A, token),
                                      lambda: self.queue.finish(SCOPE_A, token)):
                        with self.assertRaises(ValueError):
                            operation()
                    with self.assertRaises(ValueError):
                        await self.queue.pump()
                    self.assertEqual(self.queue._scopes, before_live)
                    self.assertEqual(self.document(), before_disk)
                finally:
                    self.clock.set(1000.0)
                self.assertTrue(self.queue.is_valid(SCOPE_A, token))

    async def test_nonfinite_gap_pump_reports_and_defers_without_losing_pending(self):
        for index, bad in enumerate(BAD_NUMBERS):
            with self.subTest(gap=repr(bad)):
                queue, probe = self.make_queue("pump-gap-{}.json".format(index))
                queue.external_reply(SCOPE_A)
                queue.offer(SCOPE_A, self.packet("keep-on-invalid-gap"))
                self.clock.advance(10.0)
                self.gap = bad
                errors_before = len(self.errors)
                try:
                    self.assertEqual(await queue.pump(), 0,
                                     "an invalid policy callback must be deferred, not abort pump")
                    self.assertEqual(len(self.errors), errors_before + 1)
                    self.assertIsInstance(self.errors[-1], ValueError)
                    self.assertEqual(self.pending(queue), ["keep-on-invalid-gap"])
                    self.assertEqual(probe.calls, [])
                finally:
                    self.gap = 10.0
                self.clock.advance(1000.0)
                self.assertEqual(await queue.pump(), 1)
                self.assertEqual(ids(probe.calls[0]["batch"]), ["keep-on-invalid-gap"])

    async def test_nonfinite_quiet_delay_is_reported_and_safely_deferred(self):
        self.allowed[SCOPE_A] = False
        self.queue.offer(SCOPE_A, self.packet("quiet-pending"))
        for bad in BAD_NUMBERS:
            with self.subTest(quiet_delay=repr(bad)):
                self.quiet_seconds = bad
                errors_before = len(self.errors)
                self.assertEqual(self.queue._next_delay(), 30.0)
                self.assertEqual(len(self.errors), errors_before + 1)
                self.assertIsInstance(self.errors[-1], ValueError)
                self.assertEqual(self.pending(), ["quiet-pending"])
        self.quiet_seconds = 3600.0

    async def test_nonfinite_durable_times_fail_closed_preserving_file(self):
        self.queue.offer(SCOPE_A, self.packet("durable"))
        original = self.document()
        for field in ("last_reply", "retry_at", "packet_ts"):
            for index, bad in enumerate(BAD_NUMBERS):
                with self.subTest(field=field, value=repr(bad)):
                    # A null last_reply is a valid never-replied state, not bad data.
                    if field == "last_reply" and bad is None:
                        continue
                    document = copy.deepcopy(original)
                    state = document["scopes"][SCOPE_A]
                    if field == "packet_ts":
                        state["pending"][0]["ts"] = bad
                    else:
                        state[field] = bad
                    path = self.root / "invalid-{}-{}.json".format(field, index)
                    text = json.dumps(document, ensure_ascii=False, allow_nan=True)
                    path.write_text(text, encoding="utf-8")
                    errors_before = len(self.errors)
                    with self.assertRaises(ValueError):
                        self.make_queue(path.name)
                    self.assertEqual(len(self.errors), errors_before + 1)
                    self.assertEqual(path.read_text(encoding="utf-8"), text)
                    self.assertFalse(path.with_name(path.name + ".pending").exists())

    async def test_out_of_range_integer_timestamp_is_value_error(self):
        with self.assertRaises(ValueError):
            self.queue.offer(SCOPE_A, self.packet("overflow-ts", ts=10 ** 1000))
        self.assertEqual(self.queue._scopes, {})
        self.assertFalse(self.queue.path.exists())

    async def test_unknown_wrong_scope_finished_and_closed_tokens_cannot_finish(self):
        token = self.queue.offer(SCOPE_A, self.packet("protected"))
        before = self.document()
        for scope, candidate in ((SCOPE_A, "unknown-token"), (SCOPE_B, token)):
            self.assertFalse(self.queue.is_valid(scope, candidate))
            self.assertIsNone(self.queue.claim(scope, candidate))
            self.assertFalse(self.queue.finish(scope, candidate, sent=True))
            self.assertFalse(self.queue.finish(scope, candidate, sent=False))
            self.assertEqual(self.document(), before)
        self.queue.claim(SCOPE_A, token)
        self.assertTrue(self.queue.finish(SCOPE_A, token))
        self.clock.advance(1.0)
        before = self.document()
        self.assertFalse(self.queue.finish(SCOPE_A, token))
        self.assertEqual(self.document(), before)
        live = self.queue.offer(SCOPE_A, self.packet("closed-unsent"), force=True)
        await self.queue.close()
        before = self.document()
        self.assertFalse(self.queue.is_valid(SCOPE_A, live))
        self.assertIsNone(self.queue.claim(SCOPE_A, live))
        self.assertFalse(self.queue.finish(SCOPE_A, live))
        self.assertEqual(self.document(), before)
        with self.assertRaises(RuntimeError):
            self.queue.offer(SCOPE_A, self.packet("after-close"))

    async def test_offer_save_failure_rolls_back_state_and_allows_same_id_retry(self):
        before_live = copy.deepcopy(self.queue._scopes)
        with mock.patch.object(self.queue, "_save", side_effect=OSError("simulated save failure")):
            with self.assertRaises(OSError):
                self.queue.offer(SCOPE_A, self.packet("save-failed-offer"))
        self.assertEqual(self.queue._scopes, before_live)
        self.assertEqual(self.queue._jobs, {})
        self.assertFalse(self.queue.path.exists())
        token = self.queue.offer(SCOPE_A, self.packet("save-failed-offer"))
        self.assertIsNotNone(token)
        batch = self.queue.claim(SCOPE_A, token)
        self.assertEqual(ids(batch), ["save-failed-offer"])
        self.assertEqual(batch[0]["seq"], 1)

    async def test_force_offer_save_failure_keeps_previous_normal_valid(self):
        ordinary = self.queue.offer(SCOPE_A, self.packet("normal-before-storage-failure"))
        snapshot = self.queue.claim(SCOPE_A, ordinary)
        before_live, before_disk = copy.deepcopy(self.queue._scopes), self.document()
        with mock.patch.object(self.queue, "_save", side_effect=OSError("forced offer save failure")):
            with self.assertRaises(OSError):
                self.queue.offer(SCOPE_A, self.packet("uncommitted-force"), force=True)
        self.assertEqual(self.queue._scopes, before_live)
        self.assertEqual(self.document(), before_disk)
        self.assertTrue(self.queue.is_valid(SCOPE_A, ordinary))
        self.assertEqual(self.queue.claim(SCOPE_A, ordinary), snapshot)
        self.assertEqual(set(self.queue._jobs), {ordinary})
        forced = self.queue.offer(SCOPE_A, self.packet("uncommitted-force"), force=True)
        self.assertIsNotNone(forced)
        self.assertFalse(self.queue.is_valid(SCOPE_A, ordinary))
        self.assertEqual(ids(self.queue.claim(SCOPE_A, forced)),
                         ["normal-before-storage-failure", "uncommitted-force"])

    async def test_successful_finish_save_failure_rolls_back_ack_and_keeps_token(self):
        token = self.queue.offer(SCOPE_A, self.packet("claimed-before-save-failure"))
        snapshot = self.queue.claim(SCOPE_A, token)
        self.queue.offer(SCOPE_A, self.packet("next-before-save-failure"))
        self.clock.advance(2.0)
        before_live, before_disk = copy.deepcopy(self.queue._scopes), self.document()
        with mock.patch.object(self.queue, "_save", side_effect=OSError("successful finish save failure")):
            with self.assertRaises(OSError):
                self.queue.finish(SCOPE_A, token, sent=True)
        self.assertEqual(self.queue._scopes, before_live)
        self.assertEqual(self.document(), before_disk)
        self.assertTrue(self.queue.is_valid(SCOPE_A, token))
        self.assertEqual(self.queue.claim(SCOPE_A, token), snapshot)
        self.assertTrue(self.queue.finish(SCOPE_A, token))
        self.assertEqual(self.pending(), ["next-before-save-failure"])
        self.assertEqual(self.state()["last_reply"], 1002.0)

    async def test_failed_finish_save_failure_rolls_back_backoff_and_keeps_token(self):
        token = self.queue.offer(SCOPE_A, self.packet("unsent-storage-failure"))
        snapshot = self.queue.claim(SCOPE_A, token)
        before_live, before_disk = copy.deepcopy(self.queue._scopes), self.document()
        with mock.patch.object(self.queue, "_save", side_effect=OSError("failed finish save failure")):
            with self.assertRaises(OSError):
                self.queue.finish(SCOPE_A, token, sent=False)
        self.assertEqual(self.queue._scopes, before_live)
        self.assertEqual(self.document(), before_disk)
        self.assertTrue(self.queue.is_valid(SCOPE_A, token))
        self.assertEqual(self.queue.claim(SCOPE_A, token), snapshot)
        self.assertTrue(self.queue.finish(SCOPE_A, token, sent=False))
        self.assertEqual(self.pending(), ["unsent-storage-failure"])
        self.assertEqual(self.state()["failures"], 1)
        self.assertEqual(self.state()["retry_at"], 1010.0)
        self.clock.advance(10.0)
        self.assertEqual(await self.queue.pump(), 1)
        self.assertEqual(ids(self.dispatch.calls[0]["batch"]), ["unsent-storage-failure"])

    async def test_atomic_replace_failure_keeps_previous_disk_and_memory_state(self):
        self.queue.external_reply(SCOPE_A)
        self.clock.advance(10.0)
        before_live = copy.deepcopy(self.queue._scopes)
        before_text = self.queue.path.read_text(encoding="utf-8")
        with mock.patch.object(_QUEUE_MODULE.os, "replace", side_effect=OSError("atomic replace failed")):
            with self.assertRaises(OSError):
                self.queue.offer(SCOPE_A, self.packet("replace-failed"))
        self.assertEqual(self.queue.path.read_text(encoding="utf-8"), before_text)
        self.assertEqual(self.queue._scopes, before_live)
        self.assertEqual(self.queue._jobs, {})
        token = self.queue.offer(SCOPE_A, self.packet("replace-failed"))
        self.assertIsNotNone(token)
        self.assertEqual(ids(self.queue.claim(SCOPE_A, token)), ["replace-failed"])
        self.assertFalse(self.queue.path.with_name(self.queue.path.name + ".pending").exists())

    async def test_external_reply_save_failure_does_not_start_phantom_cooldown(self):
        with mock.patch.object(self.queue, "_save", side_effect=OSError("external reply save failure")):
            with self.assertRaises(OSError):
                self.queue.external_reply(SCOPE_A)
        self.assertEqual(self.queue._scopes, {})
        self.assertFalse(self.queue.path.exists())
        self.assertIsNotNone(self.queue.offer(SCOPE_A, self.packet("not-cooled-by-failure")))

    async def test_negative_finite_gap_is_clamped_to_zero(self):
        self.gap = -10.0
        self.queue.external_reply(SCOPE_A)
        token = self.queue.offer(SCOPE_A, self.packet("zero-gap-a"))
        self.assertIsNotNone(token)
        self.queue.claim(SCOPE_A, token)
        self.assertTrue(self.queue.finish(SCOPE_A, token))
        self.assertIsNotNone(self.queue.offer(SCOPE_A, self.packet("zero-gap-b")))

    async def test_invalid_scope_identity_and_payload_do_not_create_jobs(self):
        for scope in (None, "", 3, "x" * 513):
            with self.subTest(scope=repr(scope)):
                with self.assertRaises(ValueError):
                    self.queue.offer(scope, self.packet("invalid-scope"))
        for payload in (None, [], {"text": "missing id"}, {"message_id": "x", "text": 3},
                        {"message_id": "x" * 241, "text": "too long id"},
                        self.packet("invalid-identity", sender_id=True),
                        self.packet("invalid-identity", sender_name={})):
            with self.subTest(payload=repr(payload)):
                with self.assertRaises(ValueError):
                    self.queue.offer(SCOPE_A, payload)
        self.assertEqual(self.queue._scopes, {})
        self.assertEqual(self.queue._jobs, {})
        self.assertFalse(self.queue.path.exists())

    async def test_failed_force_save_does_not_cancel_real_inflight_dispatch(self):
        started, cancelled, release, completed = (asyncio.Event() for _ in range(4))
        cancellations, finishes = [], []

        async def held_dispatch(scope, payload, token):
            queue.claim(scope, token)
            started.set()
            try:
                await release.wait()
                finishes.append(queue.finish(scope, token))
                completed.set()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        queue, _ = self.make_queue(dispatch=held_dispatch, on_cancel=cancellations.append)
        queue.external_reply(SCOPE_A)
        queue.offer(SCOPE_A, self.packet("running-before-failed-force"))
        self.clock.advance(10.0)
        self.assertEqual(await queue.pump(), 1)
        self.assertTrue(started.is_set())
        ordinary = next(iter(queue._jobs))
        before_live, before_disk = copy.deepcopy(queue._scopes), self.document(queue)
        with mock.patch.object(queue, "_save", side_effect=OSError("inflight preemption save failure")):
            with self.assertRaises(OSError):
                queue.offer(SCOPE_A, self.packet("failed-inflight-force"), force=True)
        self.assertEqual(queue._scopes, before_live)
        self.assertEqual(self.document(queue), before_disk)
        self.assertTrue(queue.is_valid(SCOPE_A, ordinary))
        self.assertEqual(await queue.pump(), 0)
        self.assertFalse(cancelled.is_set(), "an uncommitted force must not cancel generation")
        self.assertEqual(cancellations, [], "on_cancel must run only after a durable commit")
        release.set()
        self.assertEqual(await queue.pump(), 0)
        self.assertTrue(completed.is_set())
        self.assertEqual(finishes, [True])
        self.assertEqual(self.pending(queue), [])

    async def test_independent_force_gate_ignores_proactive_disable_but_not_quiet(self):
        force_allowed = {SCOPE_A: True}
        self.allowed[SCOPE_A] = False  # Proactive replies disabled; direct requests remain allowed.
        queue, probe = self.make_queue(can_force_reply=lambda scope: force_allowed.get(scope, False))
        queue.external_reply(SCOPE_A)
        self.assertIsNone(queue.offer(SCOPE_A, self.packet("disabled-proactive")))
        direct = queue.offer(SCOPE_A, self.packet("still-addressed", "大肥鱼"), force=True)
        self.assertIsNotNone(direct)
        self.assertEqual(ids(queue.claim(SCOPE_A, direct)), ["disabled-proactive", "still-addressed"])
        self.assertTrue(queue.finish(SCOPE_A, direct))
        force_allowed[SCOPE_A] = False  # Both injected policies now deny night replies.
        self.assertIsNone(queue.offer(SCOPE_A, self.packet("night-addressed"), force=True))
        self.clock.advance(10.0)
        self.assertEqual(await queue.pump(), 0)
        self.assertEqual(self.pending(queue), ["night-addressed"])
        force_allowed[SCOPE_A] = True
        self.allowed[SCOPE_A] = True
        self.assertEqual(await queue.pump(), 1)
        self.assertEqual(ids(probe.calls[0]["batch"]), ["night-addressed"])

    async def test_keepalive_renews_lease_without_reviving_expired_or_preempted_token(self):
        ordinary = self.queue.offer(SCOPE_A, self.packet("long-generation"))
        self.queue.claim(SCOPE_A, ordinary)
        self.clock.set(1000.0 + self.queue.LEASE_SECONDS - 0.001)
        renewed_deadline = self.clock() + self.queue.LEASE_SECONDS
        self.assertTrue(self.queue.keepalive(SCOPE_A, ordinary))
        self.clock.set(1000.0 + self.queue.LEASE_SECONDS)
        self.assertTrue(self.queue.is_valid(SCOPE_A, ordinary))
        self.clock.set(renewed_deadline - 0.001)
        self.assertTrue(self.queue.is_valid(SCOPE_A, ordinary))
        self.clock.set(renewed_deadline)
        self.assertFalse(self.queue.keepalive(SCOPE_A, ordinary))
        self.assertFalse(self.queue.finish(SCOPE_A, ordinary))
        self.assertEqual(self.pending(), ["long-generation"])
        normal = self.queue.offer(SCOPE_B, self.packet("preempt-keepalive", group_id="10002"))
        forced = self.queue.offer(SCOPE_B, self.packet("force-keepalive", group_id="10002"), force=True)
        self.assertFalse(self.queue.keepalive(SCOPE_B, normal))
        self.assertFalse(self.queue.keepalive(SCOPE_A, forced))
        self.assertTrue(self.queue.keepalive(SCOPE_B, forced))
        self.assertEqual(ids(self.queue.claim(SCOPE_B, forced)), ["preempt-keepalive", "force-keepalive"])
        await self.queue.close()
        self.assertFalse(self.queue.keepalive(SCOPE_B, forced))

    async def test_external_reply_fences_enqueued_normal_and_restarts_its_cooldown(self):
        self.queue.external_reply(SCOPE_A)
        self.queue.offer(SCOPE_A, self.packet("already-enqueued-normal"))
        self.clock.advance(10.0)
        self.assertEqual(await self.queue.pump(), 1)
        normal = self.dispatch.calls[0]["token"]
        self.clock.advance(1.0)
        self.queue.external_reply(SCOPE_A)
        self.assertFalse(self.queue.is_valid(SCOPE_A, normal))
        self.assertFalse(self.queue.finish(SCOPE_A, normal))
        self.assertEqual(self.pending(), ["already-enqueued-normal"])
        self.assertEqual(self.state()["last_reply"], 1011.0)
        self.clock.set(1020.999)
        self.assertEqual(await self.queue.pump(), 0)
        self.clock.set(1021.0)
        self.assertEqual(await self.queue.pump(), 1)
        self.assertNotEqual(self.dispatch.calls[-1]["token"], normal)
        self.assertEqual(ids(self.dispatch.calls[-1]["batch"]), ["already-enqueued-normal"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
