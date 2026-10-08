import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from main import Bot, D, Market, Quote, canonical, desired_quote, ticks


def market():
    return Market({"marketId": 28, "marketDisplayName": "NVDA-USD",
                   "tickSize": "0.01", "stepSize": "0.001", "minOrderSize": "0.001",
                   "maxOrderSize": "1000", "minOrderNotional": "5"},
                  {"order_usd": D("10"), "max_position_usd": D("40")},
                  book={"bids": [["100", "10"]], "asks": [["101", "10"]]})


class StrategyTests(unittest.TestCase):
    def test_two_sides_and_notional(self):
        m = market()
        for side in ("BUY", "SELL"):
            price, quantity = desired_quote(m, side)
            self.assertLessEqual(price * quantity, D("10"))
            self.assertGreaterEqual(price * quantity, D("5"))

    def test_limits_include_whole_order_and_short_positions(self):
        m = market()
        for side, position in (("BUY", D("0.32")), ("SELL", D("-0.32"))):
            m.position = position
            _, quantity = desired_quote(m, side)
            self.assertLessEqual((abs(position) + quantity) * D("101"), D("40"))
        m.position = D("0.4")
        self.assertIsNone(desired_quote(m, "BUY"))
        self.assertIsNotNone(desired_quote(m, "SELL"))
        m.position = D("-0.4")
        self.assertIsNone(desired_quote(m, "SELL"))
        self.assertIsNotNone(desired_quote(m, "BUY"))

    def test_imbalance_and_own_liquidity(self):
        m = market()
        m.book["bids"] = [["100", "1"]]
        self.assertIsNone(desired_quote(m, "BUY"))
        self.assertIsNotNone(desired_quote(m, "SELL"))
        m.book["bids"] = [["100", "10"]]
        m.quotes["BUY"] = Quote("own", "BUY", D("100"), D("10"), status="OPEN")
        self.assertIsNone(desired_quote(m, "BUY"))

    def test_empty_crossed_book_and_exact_ticks(self):
        m = market()
        m.book["asks"] = []
        self.assertIsNone(desired_quote(m, "BUY"))
        m.book["asks"] = [["99", "1"]]
        self.assertIsNone(desired_quote(m, "SELL"))
        self.assertEqual(ticks("100.01", "0.01"), 10001)
        with self.assertRaises(ValueError):
            ticks("100.001", "0.01")


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.market = market()
        self.bot = Bot("0x" + "1" * 40, Ed25519PrivateKey.generate(), {"NVDA-USD": self.market})

    def event(self, channel, contents, snapshot=False):
        self.bot.update({"channel": channel, "contents": contents, "accountIndex": 0,
                         "type": "subscribed" if snapshot else "channel_data"})

    async def test_leverage_ack_waits_for_effective_leverage(self):
        self.bot.post = AsyncMock(return_value={"status": "ACK", "leverage": 20})
        task = asyncio.create_task(self.bot.set_leverage(self.market, 20))
        await asyncio.sleep(0.01)
        self.assertFalse(task.done())
        self.event("accountAttributeUpdates", {"entries": [
            {"type": "leverage", "marketId": 28, "leverage": 10}]})
        await asyncio.sleep(0.01)
        self.assertFalse(task.done())
        self.event("accountAttributeUpdates", {"entries": [
            {"type": "leverage", "marketId": 28, "leverage": 20}]})
        await task
        self.assertFalse(self.market.quotes)

    async def test_leverage_confirmation_before_ack(self):
        async def post(*args):
            self.event("accountAttributeUpdates", {"entries": [
                {"type": "leverage", "marketId": 28, "leverage": "20"}]})
            return {"status": "ACK", "leverage": 20}
        self.bot.post = post
        await self.bot.set_leverage(self.market, 20)

    async def test_leverage_applied_and_rejected(self):
        self.bot.post = AsyncMock(return_value={"status": "APPLIED", "leverage": 20})
        await self.bot.set_leverage(self.market, 20)
        self.bot.post = AsyncMock(return_value={"status": "REJECTED", "leverage": 10})
        with self.assertRaises(RuntimeError):
            await self.bot.set_leverage(self.market, 20)
        with self.assertRaisesRegex(RuntimeError, "UNDERCOLLATERALIZED"):
            self.event("accountAttributeUpdates", {"entries": [
                {"type": "leverageReject", "marketId": 28, "rejectReason": "UNDERCOLLATERALIZED"}]})

    async def test_leverage_ack_timeout(self):
        self.bot.post = AsyncMock(return_value={"status": "ACK", "leverage": 20})
        with patch("main.REQUEST_TIMEOUT", 0.01):
            with self.assertRaisesRegex(TimeoutError, "non confirmé"):
                await self.bot.set_leverage(self.market, 20)
        self.assertFalse(self.market.quotes)

    async def test_snapshots_ignore_closed_orders(self):
        self.event("orders", {"openOrders": [], "recentClosedOrders": [{"marketId": 28}]}, True)
        self.assertTrue(self.market.orders_ready)
        self.event("positions", {"positions": {"28": {"size": "-0.1"}}, "lastSequenceId": 7}, True)
        self.assertEqual(self.market.position, D("-0.1"))
        self.event("positions", {"positions": {"28": {"size": "-0.2"}}, "lastSequenceId": 6})
        self.assertEqual(self.market.position, D("-0.1"))

    async def test_open_orders_prevent_startup(self):
        with self.assertRaises(RuntimeError):
            self.event("orders", {"openOrders": [{"marketId": 28, "status": "OPEN"}]}, True)

    async def test_positions_list_after_fill(self):
        q = Quote("own", "BUY", D("100"), D("0.1"))
        self.market.quotes["BUY"] = q
        self.event("orders", {"marketId": 28, "side": "BUY", "clientId": "own",
                              "remainingSize": "0", "status": "FILLED", "sequenceNumber": 8})
        self.event("positions", {"isSnapshot": False, "lastSequenceId": 8,
                                 "positions": [{"marketId": 28, "size": "0.1"}]})
        self.assertEqual(self.market.position, D("0.1"))
        self.assertGreaterEqual(self.market.position_sequence, self.market.fill_sequence)
        self.event("positions", {"lastSequenceId": 7,
                                 "positions": [{"marketId": 28, "size": "0.2"}]})
        self.assertEqual(self.market.position, D("0.1"))
        self.event("positions", [{"marketId": 28, "size": "0", "sequenceNumber": 9}])
        self.assertEqual(self.market.position, D("0"))
        self.assertEqual(self.market.position_sequence, 9)

    async def test_positions_list_snapshot_and_unrelated_delta(self):
        self.event("positions", {"lastSequenceId": 4, "positions": []}, True)
        self.assertTrue(self.market.positions_ready)
        self.event("positions", {"lastSequenceId": 5,
                                 "positions": [{"marketId": 28, "size": "-0.1"}]}, True)
        self.event("positions", {"lastSequenceId": 6,
                                 "positions": [{"marketId": 1, "size": "1"}]})
        self.assertEqual(self.market.position, D("-0.1"))
        self.assertEqual(self.market.position_sequence, 5)

    async def test_positions_without_sequence_fail_explicitly(self):
        with self.assertRaisesRegex(ValueError, "Séquence manquante"):
            self.event("positions", {"positions": [{"marketId": 28, "size": "0.1"}]})
        self.assertFalse(self.market.positions_ready)

    async def test_fill_and_position_sequence_gate(self):
        q = Quote("own", "BUY", D("100"), D("0.1"))
        self.market.quotes["BUY"] = q
        self.event("orders", {"marketId": 28, "side": "BUY", "clientId": "own",
                              "remainingSize": "0", "status": "FILLED", "sequenceNumber": 8})
        self.assertTrue(q.terminal.is_set())
        self.assertLess(self.market.position_sequence, self.market.fill_sequence)
        self.event("positions", {"positions": {"28": {"size": "0.1"}}, "lastSequenceId": 8})
        self.assertGreaterEqual(self.market.position_sequence, self.market.fill_sequence)

    async def test_cancel_ack_does_not_allow_replacement(self):
        q = Quote("own", "BUY", D("100"), D("0.1"), status="OPEN")
        self.bot.post = AsyncMock(return_value={"status": "CANCEL_ACKNOWLEDGED"})
        task = asyncio.create_task(self.bot.cancel(self.market, q))
        await asyncio.sleep(0.01)
        self.assertFalse(task.done())
        q.terminal.set()
        await task

    async def test_signed_order_and_stream_before_ack(self):
        async def post(method, body, typed, ts):
            self.assertEqual(method, "placeOrder")
            self.assertEqual(body["timeInForce"], "ALO")
            self.assertEqual(typed["ct"], ts)
            self.assertEqual(typed["p"], 10000)
            self.assertEqual(typed["q"], 100)
            self.assertEqual(typed["g"], int(body["goodTilTime"]) * 1000)
            self.assertEqual(canonical(typed), json.dumps(typed, sort_keys=True, separators=(",", ":")))
            self.event("orders", {"marketId": 28, "side": "BUY", "clientId": body["clientId"],
                                  "remainingSize": "0", "status": "REJECTED", "sequenceNumber": 9})
            return {"status": "ACK"}
        self.bot.post = post
        await self.bot.place(self.market, "BUY", D("100"), D("0.1"))
        self.assertTrue(self.market.quotes["BUY"].terminal.is_set())
        self.assertEqual(self.market.fill_sequence, -1)


if __name__ == "__main__":
    unittest.main()
