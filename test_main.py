import asyncio
import json
import tempfile
import time
import unittest
from copy import deepcopy
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from exchanges import ApiError, Http, Lighter, OrderRejected, RateLimited, TERMINAL, UncertainOrder
from main import (MarketMaker, QuoteRetryPending, QuoteUnavailable, ReadModelLag,
                  quote_quantity, reduce_only_quantity, state_lock, stop_hit)


def market():
    return {"symbol": "XAU", "market_id": 40, "market_type": "perp", "status": "active",
            "multiplier": "1", "quote_multiplier": 1, "min_base_amount": "0.0020",
            "min_quote_amount": "10", "order_quote_limit": "5000000",
            "supported_size_decimals": 4, "supported_price_decimals": 2,
            "min_initial_margin_fraction": 400, "step": D(".0001"), "tick": D(".01")}


def book(bid="3000.45", ask="3000.55"):
    bid, ask = D(bid), D(ask)
    return {"bids": [(bid, D("1"))], "asks": [(ask, D("1"))],
            "external_bids": {bid: D("1")}, "external_asks": {ask: D("1")}}


class FakeLighter:
    identity = "fake/lighter/466324/1"
    retry_after = 0

    def __init__(self):
        self.market = market()
        self.depth = book()
        self.rows = {}
        self.current = {"size": D(0), "entry": D(0)}
        self.sent = []
        self.canceled = []
        self.close_fraction = D(1)

    async def load_market(self): return self.market
    async def set_leverage(self): pass
    async def book(self): return deepcopy(self.depth)
    async def position(self): return deepcopy(self.current)
    async def active_orders(self):
        return [{**deepcopy(o), "client_id": o["id"]}
                for o in self.rows.values() if o["status"] not in TERMINAL]

    async def order_status(self, client_id):
        return deepcopy(self.rows.get(client_id))

    async def order_statuses(self, client_ids):
        return {key: await self.order_status(key) for key in client_ids}

    async def place(self, order):
        self.sent.append(deepcopy(order))
        if (D(order["quantity"]) < D(self.market["min_base_amount"])
                or D(order["quantity"]) * D(order["price"]) < D(self.market["min_quote_amount"])):
            raise OrderRejected("code=21706 invalid order base or quote amount")
        self.rows[order["id"]] = {**deepcopy(order), "status": "OPEN"}
        if order["kind"] == "close":
            executable = min(D(order["quantity"]), abs(self.current["size"]))
            self.fill(order["id"], executable * self.close_fraction)
            if self.rows[order["id"]]["status"] != "FILLED":
                self.rows[order["id"]]["status"] = "CANCELED"
        return await self.order_status(order["id"])

    async def cancel(self, order):
        self.canceled.append(order["id"])
        row = self.rows[order["id"]]
        if row["status"] not in TERMINAL:
            row["status"] = "CANCELED"
        return deepcopy(row)

    def fill(self, client_id, quantity=None):
        row = self.rows[client_id]
        remaining = D(row["quantity"]) - D(row["filled"])
        quantity = remaining if quantity is None else quantity
        previous = self.current["size"]
        if row["reduce_only"]:
            quantity = min(quantity, abs(previous)) if previous * row["side"] < 0 else D(0)
        updated = previous + row["side"] * quantity
        entry = self.current["entry"]
        if not previous or previous * updated < 0:
            entry = D(row["price"])
        elif previous * row["side"] > 0:
            entry = (entry * abs(previous) + D(row["price"]) * quantity) / abs(updated)
        self.current = {"size": updated, "entry": entry if updated else D(0)}
        row["filled"] = str(D(row["filled"]) + quantity)
        if D(row["filled"]) == D(row["quantity"]):
            row["status"] = "FILLED"


class SizingTests(unittest.TestCase):
    def test_second_process_cannot_use_same_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            with state_lock(path):
                with self.assertRaises(RuntimeError):
                    with state_lock(path):
                        self.fail("Le deuxième verrou aurait dû échouer")
            with state_lock(path):
                pass

    def test_strict_ten_dollars_never_rounds_above_cap(self):
        m = market()
        m["min_quote_amount"] = "0"
        quantity = quote_quantity(m, D("3000.45"), D("3000.55"), round_up=False)
        self.assertLessEqual(quantity * D("3000.55"), D(10))
        self.assertEqual(quantity % m["step"], 0)

    def test_strict_cap_blocks_when_exchange_minimum_is_ten(self):
        with self.assertRaises(QuoteUnavailable):
            quote_quantity(market(), D("3000.45"), D("3000.55"), round_up=False)

    def test_default_sizing_accepts_ten_dollar_target_with_xau_minimum(self):
        bid, ask = D("4022.04"), D("4022.45")
        quantity = quote_quantity(market(), bid, ask)
        self.assertEqual(quantity, D(".0025"))
        self.assertGreaterEqual(quantity * bid, D(10))
        self.assertLess((quantity - market()["step"]) * bid, D(10))

    def test_authorized_rounding_uses_first_step_above_ten(self):
        bid, ask = D("3000.45"), D("3000.55")
        quantity = quote_quantity(market(), bid, ask, round_up=True)
        self.assertEqual(quantity, D(".0034"))
        self.assertGreaterEqual(quantity * bid, D(10))
        self.assertLess((quantity - market()["step"]) * bid, D(10))

    def test_min_base_and_max_quote_are_checked(self):
        for key, value in (("min_base_amount", "1"), ("order_quote_limit", "9")):
            m = market()
            m[key] = value
            with self.assertRaises(QuoteUnavailable):
                quote_quantity(m, D(3000), D(3001), round_up=True)

    def test_small_reduce_only_exit_meets_notional_minimum(self):
        self.assertEqual(reduce_only_quantity(market(), D(".0001"), D(4020)), D(".0025"))
        self.assertEqual(reduce_only_quantity(market(), D(".0024"), D(4020)), D(".0025"))

    def test_reduce_only_exit_meets_base_minimum_and_rounds_to_step(self):
        self.assertEqual(reduce_only_quantity(market(), D(".0001"), D(10000)), D(".0020"))
        self.assertEqual(reduce_only_quantity(market(), D(".0020"), D(4000)), D(".0025"))
        self.assertEqual(reduce_only_quantity(market(), D(".0040"), D(4000)), D(".0040"))

    def test_reduce_only_exit_rejects_invalid_size_and_quote_limit(self):
        for size in (D(0), D(-1)):
            with self.assertRaises(ValueError):
                reduce_only_quantity(market(), size, D(4000))
        with self.assertRaises(QuoteUnavailable):
            reduce_only_quantity({**market(), "order_quote_limit": "9"}, D(".0001"), D(4000))

    def test_stop_is_one_price_unit_in_both_directions(self):
        long = {"size": D(".0034"), "entry": D("3000.45")}
        short = {"size": D("-.0034"), "entry": D("3000.55")}
        self.assertFalse(stop_hit(long, book("2999.46", "2999.56")))
        self.assertTrue(stop_hit(long, book("2999.45", "2999.55")))
        self.assertFalse(stop_hit(short, book("3001.44", "3001.54")))
        self.assertTrue(stop_hit(short, book("3001.45", "3001.55")))


class HttpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = 100.0
        clock = patch("exchanges.time", SimpleNamespace(monotonic=lambda: self.now, time=time.time))
        clock.start()
        self.addCleanup(clock.stop)

        async def sleep(delay):
            self.now += delay

        sleeper = patch("exchanges.asyncio.sleep", side_effect=sleep)
        sleeper.start()
        self.addCleanup(sleeper.stop)
        self.http = Http()
        self.http.session = SimpleNamespace(get=Mock())

    def respond(self, status=200, data=None, headers=None):
        response = SimpleNamespace(status=status, headers=headers or {},
                                   json=AsyncMock(return_value=data or {"code": 200}))
        context = MagicMock()
        context.__aenter__.return_value = response
        self.http.session.get.return_value = context
        return response

    async def test_429_html_respects_retry_after_and_blocks_all_rest_reads(self):
        response = self.respond(429, headers={"Retry-After": "30"})
        response.json.side_effect = ValueError("HTML rate limit page")
        with self.assertRaises(RateLimited):
            await self.http.get("active", {})
        response.json.assert_not_awaited()
        self.assertEqual(self.http.retry_after, 30)
        with self.assertRaises(RateLimited):
            await self.http.get("book", {})
        self.http.session.get.assert_called_once()
        self.now += 30
        self.respond()
        self.assertEqual(await self.http.get("active", {}), {"code": 200})

    async def test_repeated_429_back_off_and_success_resets_delay(self):
        for delay in (5, 10, 20, 40, 60, 60):
            self.respond(429)
            with self.assertRaises(RateLimited):
                await self.http.get("active", {})
            self.assertEqual(self.http.retry_after, delay)
            self.now += delay
        self.respond()
        await self.http.get("active", {})
        self.respond(429)
        with self.assertRaises(RateLimited):
            await self.http.get("active", {})
        self.assertEqual(self.http.retry_after, 5)

    async def test_http_date_retry_after_is_respected(self):
        self.respond(429, headers={"Retry-After": "Mon, 26 Oct 2026 12:00:30 GMT"})
        with patch("exchanges.time.time", return_value=1793016000):
            with self.assertRaises(RateLimited):
                await self.http.get("active", {})
        self.assertEqual(self.http.retry_after, 30)

    async def test_invalid_retry_after_uses_fallback(self):
        for header in ("invalid", "nan", "inf", "-1"):
            http = Http()
            http.session = self.http.session
            self.respond(429, headers={"Retry-After": header})
            with self.assertRaises(RateLimited):
                await http.get("active", {})
            self.assertEqual(http.retry_after, 5)

    async def test_json_rate_limit_code_also_suspends_reads(self):
        self.respond(data={"code": 429})
        with self.assertRaises(RateLimited):
            await self.http.get("active", {})
        self.assertEqual(self.http.retry_after, 5)

    async def test_successful_volume_quota_message_is_accepted(self):
        data = {"code": 200, "ratelimit": "didn't use volume quota"}
        self.respond(data=data)
        self.assertEqual(await self.http.get("active", {}), data)

    async def test_rest_requests_are_spaced_across_endpoints(self):
        self.respond()
        sent = []
        context = self.http.session.get.return_value
        self.http.session.get.side_effect = lambda *a, **k: (sent.append(self.now) or context)
        await asyncio.gather(*(self.http.get(url, {}) for url in ("active", "book", "account")))
        self.assertEqual(sent, [100, 100.5, 101])

    async def test_other_http_error_is_still_an_api_failure(self):
        self.respond(503)
        with self.assertRaises(ApiError):
            await self.http.get("active", {})

    async def test_invalid_json_response_is_api_failure_then_next_read_can_recover(self):
        response = self.respond()
        response.json.side_effect = json.JSONDecodeError("Expecting value", "", 0)
        with self.assertRaises(ApiError):
            await self.http.get("history", {})
        self.respond()
        self.assertEqual(await self.http.get("history", {}), {"code": 200})


class MarketMakerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.exchange = FakeLighter()
        self.path = Path(self.temp.name) / "state.json"
        self.maker = MarketMaker(self.exchange, self.path, round_up=True)

    def tearDown(self):
        self.temp.cleanup()

    async def start(self):
        await self.maker.initialize()
        await self.maker.step()

    def quote(self, side):
        return next(o for o in self.maker.live("quote") if o["side"] == side)

    def seed_missing_quote(self, **changes):
        order = {"id": "249670502102335", "kind": "quote", "side": 1,
                 "quantity": ".0034", "price": "3000.45", "reduce_only": False,
                 "filled": "0", "status": "PENDING", **changes}
        self.maker.state["orders"].append(order)
        self.maker.save()
        return order

    async def test_places_two_best_quotes_with_equal_quantities(self):
        await self.start()
        bid, ask = self.quote(1), self.quote(-1)
        self.assertEqual(D(bid["price"]), D("3000.45"))
        self.assertEqual(D(ask["price"]), D("3000.55"))
        self.assertEqual(bid["quantity"], ask["quantity"])
        self.assertFalse(bid["reduce_only"])
        self.assertEqual(len(self.exchange.sent), 2)

    async def test_default_configuration_places_quotes_without_rounding_override(self):
        self.maker = MarketMaker(self.exchange, self.path)
        await self.start()
        self.assertEqual(len(self.exchange.sent), 2)
        self.assertEqual(self.quote(1)["quantity"], self.quote(-1)["quantity"])
        for order in self.exchange.sent:
            self.assertGreaterEqual(D(order["quantity"]) * D(order["price"]), D(10))

    async def test_no_duplicate_quotes_on_repeated_cycles(self):
        await self.start()
        for _ in range(3):
            await self.maker.step()
        self.assertEqual(len(self.exchange.sent), 2)

    async def test_refresh_reads_both_orders_in_one_batch(self):
        await self.start()
        batch = AsyncMock(wraps=self.exchange.order_statuses)
        self.exchange.order_statuses = batch
        await self.maker.refresh()
        batch.assert_awaited_once_with([order["id"] for order in self.maker.live()])

    async def test_rate_limited_refresh_preserves_live_orders_and_inventory(self):
        await self.start()
        before = deepcopy(self.maker.state)
        self.exchange.order_statuses = AsyncMock(side_effect=RateLimited("429"))
        with self.assertRaises(RateLimited):
            await self.maker.step()
        self.assertEqual(self.maker.state, before)
        self.assertEqual(len(self.exchange.sent), 2)

    async def test_run_waits_for_quota_before_next_cycle(self):
        self.maker.initialize = AsyncMock()
        self.maker.initialized = True
        self.maker.shutdown = AsyncMock()
        self.maker.step = AsyncMock(side_effect=RateLimited("429"))
        self.maker.cancel_quotes = AsyncMock(side_effect=UncertainOrder("confirmation 429"))
        self.exchange.retry_after = 30

        async def wait_for_stop(event, delay):
            self.assertEqual(delay, 60)
            self.maker.stop.set()
            await event

        with patch("main.asyncio.wait_for", side_effect=wait_for_stop), self.assertLogs("xau-mm") as logs:
            await self.maker.run()
        self.maker.step.assert_awaited_once()
        self.maker.cancel_quotes.assert_awaited_once()
        self.assertTrue(any("URGENT" in record.getMessage() for record in logs.records))
        self.assertTrue(all(record.exc_info is None for record in logs.records))

    async def test_startup_429_waits_for_quota_then_initializes_in_same_process(self):
        self.exchange.load_market = AsyncMock(side_effect=[RateLimited("429"), market()])
        self.exchange.retry_after = 35
        self.maker.step = AsyncMock(side_effect=self.maker.stop.set)
        self.maker.shutdown = AsyncMock()

        async def retry(delay):
            if not self.maker.initialized:
                self.assertEqual(delay, 60)
                self.assertFalse(self.exchange.sent)
                self.assertFalse(self.exchange.canceled)
                self.exchange.retry_after = 0

        with patch.object(self.maker, "wait_for_retry", side_effect=retry), self.assertLogs("xau-mm", level="WARNING") as logs:
            await self.maker.run()
        self.assertTrue(self.maker.initialized)
        self.assertEqual(self.exchange.load_market.await_count, 2)
        self.assertIn("Démarrage suspendu", logs.output[0])
        self.maker.step.assert_awaited_once()

    async def test_startup_errors_back_off_without_entering_trading_loop(self):
        self.maker.initialize = AsyncMock(side_effect=[
            RateLimited("429"), ApiError("offline"), ReadModelLag("lag"),
            UncertainOrder("invisible"), RateLimited("429"), RateLimited("429")])
        self.maker.step = AsyncMock()
        self.maker.shutdown = AsyncMock()
        delays = []

        async def retry(delay):
            delays.append(delay)
            if len(delays) == 6:
                self.maker.stop.set()

        with patch.object(self.maker, "wait_for_retry", side_effect=retry), self.assertLogs("xau-mm"):
            await self.maker.run()
        self.assertEqual(delays, [60] * 6)
        self.assertFalse(self.maker.initialized)
        self.maker.step.assert_not_awaited()
        self.maker.shutdown.assert_not_awaited()
        self.assertFalse(self.exchange.sent)
        self.assertFalse(self.exchange.canceled)

    async def test_startup_persistent_position_mismatch_preserves_journal(self):
        order = self.seed_missing_quote(expires_at_ms=1)
        self.exchange.current = {"size": D(".0034"), "entry": D("3000.45")}
        before = deepcopy(self.maker.state)
        self.maker.step = AsyncMock()
        self.maker.shutdown = AsyncMock()

        async def stop_retry(delay):
            self.assertEqual(delay, 60)
            self.maker.stop.set()

        with patch.object(self.maker, "wait_for_retry", side_effect=stop_retry), self.assertLogs("xau-mm", level="WARNING") as logs:
            await self.maker.run()
        self.assertIn("Lighter=0.0034 XAU, journal=0 XAU", logs.output[0])
        self.assertEqual(self.maker.state, {**before, "recovery_pending": True})
        self.assertEqual(json.loads(self.path.read_text(encoding="utf8")), self.maker.state)
        self.assertEqual(order["status"], "PENDING")
        self.maker.step.assert_not_awaited()
        self.maker.shutdown.assert_not_awaited()
        self.assertFalse(self.exchange.sent)
        self.assertFalse(self.exchange.canceled)

    async def test_startup_position_lag_recovers_without_counting_fill_twice(self):
        await self.start()
        self.exchange.fill(self.quote(1)["id"])
        current = deepcopy(self.exchange.current)
        restarted = MarketMaker(self.exchange, self.path)
        self.exchange.position = AsyncMock(side_effect=[
            {"size": D(0), "entry": D(0)}, current, current, current])
        restarted.step = AsyncMock(side_effect=restarted.stop.set)
        restarted.shutdown = AsyncMock()
        with patch.object(restarted, "wait_for_retry", new_callable=AsyncMock), self.assertLogs("xau-mm"):
            await restarted.run()
        self.assertTrue(restarted.initialized)
        self.assertEqual(D(restarted.state["expected_size"]), current["size"])
        self.assertFalse(self.exchange.canceled)
        self.assertEqual(len(self.exchange.sent), 2)
        restarted.step.assert_awaited_once()

    async def test_stop_interrupts_startup_backoff_without_shutdown_orders(self):
        self.exchange.load_market = AsyncMock(side_effect=RateLimited("429"))
        self.maker.shutdown = AsyncMock()

        async def stop_wait(event, delay):
            self.assertEqual(delay, 60)
            self.maker.stop.set()
            await event

        with patch("main.asyncio.wait_for", side_effect=stop_wait), self.assertLogs("xau-mm"):
            await self.maker.run()
        self.exchange.load_market.assert_awaited_once()
        self.maker.shutdown.assert_not_awaited()
        self.assertFalse(self.exchange.sent)
        self.assertFalse(self.exchange.canceled)

    async def test_startup_configuration_errors_are_not_retried(self):
        self.exchange.load_market = AsyncMock(side_effect=ValueError("Contrat incompatible"))
        with patch.object(self.maker, "wait_for_retry", new_callable=AsyncMock) as wait:
            with self.assertRaisesRegex(ValueError, "Contrat incompatible"):
                await self.maker.run()
        wait.assert_not_awaited()
        self.assertFalse(self.maker.initialized)

    async def test_stop_before_startup_does_not_read_or_send(self):
        self.exchange.load_market = AsyncMock()
        self.maker.stop.set()
        await self.maker.run()
        self.exchange.load_market.assert_not_awaited()
        self.assertFalse(self.exchange.sent)
        self.assertFalse(self.exchange.canceled)

    async def test_server_order_index_is_persisted_for_cancellation(self):
        await self.start()
        order = self.quote(1)
        self.maker.update(order, {"status": "OPEN", "filled": "0", "order_index": 987})
        saved = json.loads(self.path.read_text(encoding="utf8"))
        self.assertEqual(saved["orders"][0]["order_index"], 987)

    async def test_full_long_fill_keeps_original_ask_and_no_new_bid(self):
        await self.start()
        bid, ask = self.quote(1), deepcopy(self.quote(-1))
        self.exchange.fill(bid["id"])
        for _ in range(3):
            await self.maker.step()
        self.assertEqual(self.quote(-1)["id"], ask["id"])
        self.assertFalse(any(o["side"] == 1 for o in self.maker.live()))
        self.assertEqual(len(self.exchange.sent), 2)

    async def test_full_short_fill_keeps_original_bid(self):
        await self.start()
        ask, bid = self.quote(-1), deepcopy(self.quote(1))
        self.exchange.fill(ask["id"])
        await self.maker.step()
        self.assertEqual(self.quote(1)["id"], bid["id"])
        self.assertEqual(len(self.exchange.sent), 2)

    async def test_keeps_exit_price_even_when_best_ask_changes(self):
        await self.start()
        ask = deepcopy(self.quote(-1))
        self.exchange.fill(self.quote(1)["id"])
        self.exchange.depth["asks"].insert(0, (D("3000.50"), D(1)))
        self.exchange.depth["external_asks"][D("3000.50")] = D(1)
        await self.maker.step()
        self.assertEqual(self.quote(-1)["id"], ask["id"])
        self.assertEqual(self.quote(-1)["price"], ask["price"])

    async def test_expired_exit_is_recreated_at_original_price(self):
        await self.start()
        ask = deepcopy(self.quote(-1))
        self.exchange.fill(self.quote(1)["id"])
        self.exchange.rows[ask["id"]]["status"] = "CANCELED"
        self.exchange.depth["asks"].insert(0, (D("3000.50"), D(1)))
        self.exchange.depth["external_asks"][D("3000.50")] = D(1)
        await self.maker.step()
        self.assertEqual(self.quote(-1)["price"], ask["price"])
        self.assertTrue(self.quote(-1)["reduce_only"])

    async def test_partial_fill_cancels_entry_and_resizes_exit_at_same_price(self):
        await self.start()
        bid, ask = self.quote(1), deepcopy(self.quote(-1))
        self.exchange.fill(bid["id"], D(".0010"))
        for _ in range(3):
            await self.maker.step()
        self.assertIn(bid["id"], self.exchange.canceled)
        self.assertIn(ask["id"], self.exchange.canceled)
        exit_order = self.quote(-1)
        self.assertEqual(D(exit_order["quantity"]), reduce_only_quantity(market(), D(".0010"), D(ask["price"])))
        self.assertEqual(exit_order["price"], ask["price"])
        self.assertTrue(exit_order["reduce_only"])
        self.assertFalse(any(o["side"] == 1 for o in self.maker.live()))

    async def test_small_reduce_only_exit_is_kept_and_cannot_reverse_position(self):
        await self.start()
        self.exchange.fill(self.quote(1)["id"], D(".0010"))
        await self.maker.step()
        await self.maker.step()
        exit_order = deepcopy(self.quote(-1))
        self.assertEqual(D(exit_order["quantity"]), D(".0034"))
        before = len(self.exchange.sent)
        for _ in range(4):
            await self.maker.step()
        self.assertEqual(self.quote(-1)["id"], exit_order["id"])
        self.assertEqual(len(self.exchange.sent), before)
        self.assertNotIn(exit_order["id"], self.exchange.canceled)
        self.exchange.fill(exit_order["id"])
        self.assertEqual(self.exchange.current["size"], 0)
        await self.maker.step()
        self.assertEqual(D(self.maker.state["expected_size"]), 0)
        self.assertFalse(self.maker.live())

    async def test_small_short_exit_respects_minimum_without_increasing_inventory(self):
        await self.start()
        self.exchange.fill(self.quote(-1)["id"], D(".0010"))
        await self.maker.step()
        await self.maker.step()
        exit_order = self.quote(1)
        self.assertEqual(D(exit_order["quantity"]), D(".0034"))
        self.assertTrue(exit_order["reduce_only"])
        self.exchange.fill(exit_order["id"])
        await self.maker.refresh()
        self.assertEqual(self.exchange.current["size"], 0)
        self.assertEqual(D(self.maker.state["expected_size"]), 0)

    async def test_rejected_exit_waits_between_retries_while_stop_remains_active(self):
        await self.start()
        self.exchange.fill(self.quote(1)["id"], D(".0010"))
        await self.maker.step()
        original = self.exchange.place
        with patch.object(self.exchange, "place", side_effect=OrderRejected("code=21706")) as send:
            with self.assertRaises(OrderRejected):
                await self.maker.step()
            for _ in range(3):
                with self.assertRaises(QuoteRetryPending):
                    await self.maker.step()
            send.assert_awaited_once()
            self.assertEqual(D(self.maker.state["expected_size"]), D(".0010"))
        self.exchange.place = original
        self.exchange.depth = book("2999.45", "2999.55")
        await self.maker.step()
        close = self.exchange.sent[-1]
        self.assertEqual(close["kind"], "close")
        self.assertTrue(close["reduce_only"])
        self.assertGreaterEqual(D(close["quantity"]) * D(close["price"]), 10)
        self.assertEqual(self.exchange.current["size"], 0)

    async def test_repeated_quote_rejections_back_off_and_success_clears_delay(self):
        await self.maker.initialize()
        now = 1000
        with patch("main.time.time", side_effect=lambda: now):
            for delay in (5, 10, 20, 40, 60, 60):
                with patch.object(self.exchange, "place", side_effect=OrderRejected("21706")):
                    with self.assertRaises(OrderRejected):
                        await self.maker.place("quote", -1, D(".0034"), D("3000.55"), reduce_only=True)
                self.assertEqual(self.maker.state["quote_retry"]["-1"]["retry_at"], now + delay)
                with self.assertRaises(QuoteRetryPending):
                    await self.maker.place("quote", -1, D(".0034"), D("3000.55"), reduce_only=True)
                now += delay
            await self.maker.place("quote", -1, D(".0034"), D("3000.55"), reduce_only=True)
        self.assertNotIn("-1", self.maker.state["quote_retry"])

    async def test_deferred_quote_does_not_trigger_cycle_error_cancellations(self):
        self.maker.initialized = True
        self.maker.initialize = AsyncMock()
        self.maker.step = AsyncMock(side_effect=QuoteRetryPending("attente"))
        self.maker.cancel_quotes = AsyncMock()
        self.maker.shutdown = AsyncMock()

        async def stop_retry(delay):
            self.maker.stop.set()

        with patch.object(self.maker, "wait_for_retry", side_effect=stop_retry):
            await self.maker.run()
        self.maker.step.assert_awaited_once()
        self.maker.cancel_quotes.assert_not_awaited()

    async def test_liquidity_below_100_cancels_quote(self):
        await self.start()
        bid = self.quote(1)
        price = D(bid["price"])
        self.exchange.depth["external_bids"][price] = D("99.99") / price
        await self.maker.step()
        self.assertIn(bid["id"], self.exchange.canceled)
        for _ in range(2):
            await self.maker.step()
        self.assertFalse(any(o["side"] == 1 for o in self.maker.live("quote")))

    async def test_liquidity_exactly_100_keeps_quote(self):
        self.exchange.depth = book("3125", "3125.10")
        await self.start()
        bid = self.quote(1)
        self.exchange.depth["external_bids"][D(3125)] = D(".032")
        await self.maker.step()
        self.assertNotIn(bid["id"], self.exchange.canceled)

    async def test_flat_quotes_follow_best_prices(self):
        await self.start()
        previous = self.quote(1)["id"]
        self.exchange.depth = book("3000.46", "3000.56")
        for _ in range(4):
            await self.maker.step()
        self.assertIn(previous, self.exchange.canceled)
        self.assertEqual(D(self.quote(1)["price"]), D("3000.46"))
        self.assertEqual(D(self.quote(-1)["price"]), D("3000.56"))

    async def test_exit_is_kept_when_liquidity_disappears_at_its_tick(self):
        await self.start()
        ask = deepcopy(self.quote(-1))
        self.exchange.fill(self.quote(1)["id"])
        self.exchange.depth = book("3000.45", "3000.60")
        await self.maker.step()
        await self.maker.step()
        self.assertNotIn(ask["id"], self.exchange.canceled)
        self.assertEqual(self.quote(-1)["id"], ask["id"])
        self.assertEqual(self.quote(-1)["price"], ask["price"])
        self.assertEqual(len(self.exchange.sent), 2)

    async def test_long_keeps_spread_exit_when_market_moves_below_entry_until_stop(self):
        self.exchange.depth = book("4010.61", "4010.81")
        await self.start()
        ask = deepcopy(self.quote(-1))
        self.exchange.fill(self.quote(1)["id"])
        for bid, offer in (("4010.31", "4010.41"), ("4009.62", "4009.72")):
            self.exchange.depth = book(bid, offer)
            await self.maker.step()
            self.assertEqual(self.quote(-1)["id"], ask["id"])
            self.assertEqual(self.quote(-1)["price"], "4010.81")
            self.assertEqual(len(self.exchange.sent), 2)
        self.exchange.depth = book("4009.61", "4009.71")
        await self.maker.step()
        self.assertEqual(self.exchange.sent[-1]["kind"], "close")
        self.assertEqual(self.exchange.current["size"], 0)

    async def test_short_keeps_original_bid_when_current_bid_would_realize_loss(self):
        self.exchange.depth = book("4009.80", "4009.94")
        await self.start()
        bid = deepcopy(self.quote(1))
        self.exchange.fill(self.quote(-1)["id"])
        for _ in range(3):
            self.exchange.depth = book("4010.08", "4010.20")
            await self.maker.step()
        self.assertEqual(self.quote(1)["id"], bid["id"])
        self.assertEqual(self.quote(1)["price"], "4009.80")
        self.assertFalse(self.exchange.canceled)
        self.assertEqual(len(self.exchange.sent), 2)
        self.exchange.depth = book("4010.84", "4010.94")
        await self.maker.step()
        self.assertEqual(self.exchange.sent[-1]["kind"], "close")
        self.assertEqual(self.exchange.current["size"], 0)

    async def test_expired_exit_returns_to_frozen_price_with_no_liquidity_there(self):
        await self.start()
        ask = deepcopy(self.quote(-1))
        self.exchange.fill(self.quote(1)["id"])
        await self.maker.step()
        self.exchange.rows[ask["id"]]["status"] = "EXPIRED"
        self.exchange.depth = book("3000.20", "3000.30")
        await self.maker.step()
        self.assertNotEqual(self.quote(-1)["id"], ask["id"])
        self.assertEqual(self.quote(-1)["price"], ask["price"])
        self.assertTrue(self.quote(-1)["reduce_only"])
        self.assertEqual(self.maker.state["exit_price"], ask["price"])

    async def test_partial_fill_recreates_exit_at_original_price_after_market_falls(self):
        await self.start()
        ask = deepcopy(self.quote(-1))
        self.exchange.fill(self.quote(1)["id"], D(".0010"))
        self.exchange.depth = book("3000.20", "3000.30")
        await self.maker.step()
        await self.maker.step()
        self.assertIn(ask["id"], self.exchange.canceled)
        self.assertEqual(self.quote(-1)["price"], ask["price"])
        self.assertTrue(self.quote(-1)["reduce_only"])
        self.assertEqual(self.exchange.current["size"], D(".0010"))

    async def test_immediate_first_fill_uses_opposite_price_saved_before_submission(self):
        await self.maker.initialize()
        original = self.exchange.place

        async def immediate_fill(order):
            saved = json.loads(self.path.read_text(encoding="utf8"))
            if order["side"] == 1:
                self.assertEqual(saved["orders"][-1]["opposite_price"], "3000.55")
            row = await original(order)
            if order["side"] == 1:
                self.exchange.fill(order["id"])
                row = await self.exchange.order_status(order["id"])
            return row

        with patch.object(self.exchange, "place", side_effect=immediate_fill):
            await self.maker.step()
        self.assertEqual(len(self.exchange.sent), 1)
        self.exchange.depth = book("3000.20", "3000.30")
        await self.maker.step()
        self.assertEqual(self.quote(-1)["price"], "3000.55")
        self.assertEqual(self.maker.state["exit_price"], "3000.55")

    async def test_frozen_exit_survives_read_failure_cancellation_and_restart(self):
        await self.start()
        ask = deepcopy(self.quote(-1))
        self.exchange.fill(self.quote(1)["id"])
        with patch.object(self.exchange, "position", side_effect=ApiError("offline")):
            with self.assertRaises(ApiError):
                await self.maker.step()
        await self.maker.cancel_quotes("erreur de lecture")
        restarted = MarketMaker(self.exchange, self.path)
        await restarted.initialize()
        self.exchange.depth = book("3000.20", "3000.30")
        await restarted.step()
        exit_order = next(o for o in restarted.live("quote") if o["side"] == -1)
        self.assertEqual(exit_order["price"], ask["price"])
        self.assertEqual(restarted.state["exit_price"], ask["price"])

    async def test_legacy_journal_recovers_original_exit_instead_of_repriced_exit(self):
        await self.start()
        ask = deepcopy(self.quote(-1))
        self.exchange.fill(self.quote(1)["id"])
        await self.maker.refresh()
        await self.maker.cancel(self.quote(-1), "ancienne règle de liquidité")
        await self.maker.place("quote", -1, D(".0034"), D("3000.30"), reduce_only=True)
        self.maker.state.pop("exit_side")
        self.maker.state["exit_price"] = "3000.30"
        for order in self.maker.state["orders"]:
            order.pop("opposite_price", None)
        self.maker.save()
        restarted = MarketMaker(self.exchange, self.path)
        await restarted.initialize()
        self.exchange.depth = book("3000.20", "3000.30")
        await restarted.step()
        await restarted.step()
        self.assertEqual(restarted.live("quote")[0]["price"], ask["price"])
        self.assertEqual(restarted.state["exit_price"], ask["price"])

    async def test_missing_original_exit_does_not_follow_market_and_stop_still_works(self):
        self.seed_missing_quote(status="FILLED", filled=".0034")
        self.maker.state.update(expected_size=".0034", cycle_filled=True)
        self.maker.save()
        self.exchange.current = {"size": D(".0034"), "entry": D("3000.45")}
        await self.maker.initialize()
        self.exchange.depth = book("3000.20", "3000.30")
        with self.assertRaisesRegex(UncertainOrder, "Prix opposé d'origine introuvable"):
            await self.maker.step()
        self.assertFalse(self.exchange.sent)
        self.exchange.depth = book("2999.45", "2999.55")
        await self.maker.step()
        self.assertEqual(self.exchange.sent[-1]["kind"], "close")
        self.assertEqual(self.exchange.current["size"], 0)

    async def test_long_stop_cancels_ask_then_closes_reduce_only(self):
        await self.start()
        ask = self.quote(-1)
        self.exchange.fill(self.quote(1)["id"])
        self.exchange.depth = book("2999.45", "2999.55")
        await self.maker.step()
        self.assertIn(ask["id"], self.exchange.canceled)
        close = self.exchange.sent[-1]
        self.assertEqual(close["kind"], "close")
        self.assertEqual(close["side"], -1)
        self.assertTrue(close["reduce_only"])
        self.assertEqual(self.exchange.current["size"], 0)

    async def test_short_stop_closes_with_buy(self):
        await self.start()
        self.exchange.fill(self.quote(-1)["id"])
        self.exchange.depth = book("3001.45", "3001.55")
        await self.maker.step()
        self.assertEqual(self.exchange.sent[-1]["side"], 1)
        self.assertTrue(self.exchange.sent[-1]["reduce_only"])
        self.assertEqual(self.exchange.current["size"], 0)

    async def test_partial_stop_fill_retries_only_remaining_inventory(self):
        await self.start()
        quantity = D(self.quote(1)["quantity"])
        self.exchange.fill(self.quote(1)["id"])
        self.exchange.depth = book("2999.45", "2999.55")
        self.exchange.close_fraction = D(".5")
        await self.maker.step()
        self.exchange.close_fraction = D(1)
        await self.maker.step()
        self.assertEqual(D(self.exchange.sent[-1]["quantity"]),
                         reduce_only_quantity(market(), quantity / 2, D(self.exchange.sent[-1]["price"])))
        self.assertEqual(self.exchange.current["size"], 0)

    async def test_both_quotes_resume_only_after_flat(self):
        await self.start()
        self.exchange.fill(self.quote(1)["id"])
        await self.maker.step()
        self.exchange.fill(self.quote(-1)["id"])
        await self.maker.step()
        self.assertFalse(self.maker.live())
        await self.maker.step()
        self.assertEqual(len(self.maker.live("quote")), 2)
        self.assertEqual(len(self.exchange.sent), 4)

    async def test_no_entry_during_position_read_model_lag(self):
        await self.start()
        self.exchange.fill(self.quote(1)["id"])
        expected = self.exchange.current["size"]
        with patch.object(self.exchange, "position", AsyncMock(return_value={"size": D(0), "entry": D(0)})) as read, \
                patch.object(self.maker, "wait_for_retry", AsyncMock()) as wait:
            with self.assertRaises(ReadModelLag):
                await self.maker.step()
        self.assertEqual(read.await_count, 3)
        self.assertEqual(wait.await_count, 2)
        self.assertEqual(D(self.maker.state["expected_size"]), expected)
        self.assertEqual(len(self.exchange.sent), 2)

    async def test_position_ahead_of_order_snapshot_keeps_original_exit(self):
        await self.start()
        bid, ask = self.quote(1), self.quote(-1)
        snapshot = {o["id"]: deepcopy(self.exchange.rows[o["id"]]) for o in (bid, ask)}
        self.exchange.fill(bid["id"])
        original = self.exchange.order_statuses
        reads = 0

        async def statuses(ids):
            nonlocal reads
            reads += 1
            return snapshot if reads == 1 else await original(ids)

        with patch.object(self.exchange, "order_statuses", side_effect=statuses), \
                patch.object(self.maker, "wait_for_retry", AsyncMock()) as wait:
            await self.maker.step()
        wait.assert_awaited_once()
        self.assertEqual(self.quote(-1)["id"], ask["id"])
        self.assertEqual(D(self.maker.state["exit_price"]), D(ask["price"]))
        self.assertFalse(self.exchange.canceled)
        self.assertEqual(len(self.exchange.sent), 2)

    async def test_order_fill_ahead_of_position_keeps_original_exit(self):
        await self.start()
        ask = self.quote(-1)
        self.exchange.fill(self.quote(1)["id"])
        with patch.object(self.exchange, "position", side_effect=[
                {"size": D(0), "entry": D(0)}, deepcopy(self.exchange.current)]), \
                patch.object(self.maker, "wait_for_retry", AsyncMock()) as wait:
            await self.maker.step()
        wait.assert_awaited_once()
        self.assertEqual(self.quote(-1)["id"], ask["id"])
        self.assertFalse(self.exchange.canceled)
        self.assertEqual(len(self.exchange.sent), 2)

    async def test_exit_fill_between_order_and_position_reads_finishes_cycle(self):
        await self.start()
        self.exchange.fill(self.quote(1)["id"])
        await self.maker.step()
        ask = self.quote(-1)
        original = self.exchange.position

        async def position():
            if self.exchange.current["size"]:
                self.exchange.fill(ask["id"])
            return await original()

        with patch.object(self.exchange, "position", side_effect=position), \
                patch.object(self.maker, "wait_for_retry", AsyncMock()):
            await self.maker.step()
        self.assertEqual(D(self.maker.state["expected_size"]), 0)
        self.assertFalse(self.maker.state["cycle_filled"])
        self.assertFalse(self.maker.live())
        self.assertFalse(self.exchange.canceled)
        self.assertEqual(len(self.exchange.sent), 2)

    async def test_api_failure_during_reconciliation_preserves_inventory(self):
        await self.start()
        self.exchange.fill(self.quote(1)["id"])
        with patch.object(self.exchange, "position", side_effect=[
                {"size": D(0), "entry": D(0)}, ApiError("lecture indisponible")]), \
                patch.object(self.maker, "wait_for_retry", AsyncMock()):
            with self.assertRaises(ApiError):
                await self.maker.step()
        self.assertEqual(D(self.maker.state["expected_size"]), self.exchange.current["size"])
        self.assertEqual(len(self.exchange.sent), 2)

    async def test_stop_interrupts_position_reconciliation_without_new_orders(self):
        await self.start()
        self.exchange.fill(self.quote(1)["id"])
        with patch.object(self.exchange, "position", return_value={"size": D(0), "entry": D(0)}) as read, \
                patch.object(self.maker, "wait_for_retry", side_effect=lambda _: self.maker.stop.set()) as wait:
            with self.assertRaises(ReadModelLag):
                await self.maker.step()
        wait.assert_awaited_once()
        read.assert_awaited_once()
        self.assertEqual(len(self.exchange.sent), 2)

    async def test_matching_position_needs_no_reconciliation(self):
        await self.start()
        with patch.object(self.maker, "refresh", AsyncMock()) as refresh, \
                patch.object(self.maker, "wait_for_retry", AsyncMock()) as wait:
            await self.maker.position()
        refresh.assert_not_awaited()
        wait.assert_not_awaited()

    async def test_expected_cycle_errors_log_without_traceback_and_cancel_quotes(self):
        for error in (ReadModelLag("positions divergentes", observed=D(0)),
                      ApiError("lecture indisponible"), UncertainOrder("ordre invisible"),
                      RuntimeError("erreur inattendue")):
            with self.subTest(error=type(error).__name__):
                self.maker.stop.clear()
                self.maker.initialized = True
                with patch.object(self.maker, "initialize", AsyncMock()), \
                        patch.object(self.maker, "step", AsyncMock(side_effect=error)), \
                        patch.object(self.maker, "cancel_quotes", AsyncMock()) as cancel, \
                        patch.object(self.maker, "shutdown", AsyncMock()), \
                        patch.object(self.maker, "wait_for_retry", side_effect=lambda _: self.maker.stop.set()), \
                        self.assertLogs("xau-mm", level="WARNING") as logs:
                    await self.maker.run()
                cancel.assert_awaited_once()
                self.assertEqual(bool(logs.records[0].exc_info), type(error) is RuntimeError)

    async def test_outage_retries_once_a_minute_without_repeated_cancellations(self):
        await self.start()
        original_step = self.maker.step
        attempts, delays = [], []
        self.exchange.retry_after = 90

        async def step():
            attempts.append("step")
            if len(attempts) == 1:
                raise ApiError("réponse HTML")
            await original_step()
            self.maker.stop.set()

        async def wait(delay):
            delays.append(delay)
            if len(delays) == 1:
                self.assertTrue(json.loads(self.path.read_text())["recovery_pending"])
                self.exchange.retry_after = 0

        with patch.object(self.maker, "initialize", AsyncMock()), \
                patch.object(self.maker, "shutdown", AsyncMock()), \
                patch.object(self.maker, "step", side_effect=step), \
                patch.object(self.exchange, "active_orders", AsyncMock(side_effect=[
                    ApiError("toujours indisponible"), await self.exchange.active_orders()])) as reads, \
                patch.object(self.maker, "cancel_quotes", AsyncMock()) as cancel, \
                patch.object(self.maker, "wait_for_retry", side_effect=wait), self.assertLogs("xau-mm"):
            await self.maker.run()
        self.assertEqual(delays, [90, 60, .5])
        self.assertEqual(reads.await_count, 2)
        cancel.assert_awaited_once()
        self.assertEqual(len(attempts), 2)
        self.assertNotIn("recovery_pending", self.maker.state)
        self.assertEqual(len(self.exchange.sent), 2)

    async def check_returning_api_stop(self, side):
        await self.start()
        entry = self.quote(side)
        self.exchange.fill(entry["id"])
        # Le fill a eu lieu pendant la panne ; seul le compte le connaît.
        self.maker.state["recovery_pending"] = True
        for order in self.maker.live():
            order["expires_at_ms"] = 1
        self.exchange.rows.clear()
        self.exchange.depth = (book("2999.45", "2999.55") if side == 1
                               else book("3001.45", "3001.55"))
        original_step = self.maker.step

        async def step():
            await original_step()
            self.maker.stop.set()

        with patch.object(self.maker, "initialize", AsyncMock()), \
                patch.object(self.maker, "shutdown", AsyncMock()), \
                patch.object(self.maker, "step", side_effect=step), \
                patch.object(self.maker, "wait_for_retry", AsyncMock()):
            await self.maker.run()
        self.assertEqual(self.exchange.current["size"], 0)
        self.assertEqual(len(self.exchange.sent), 3)
        close = self.exchange.sent[-1]
        self.assertEqual(close["kind"], "close")
        self.assertTrue(close["reduce_only"])
        self.assertEqual(close["side"], -side)
        self.assertIn("api_orders_reconciled", self.path.with_suffix(".orders.jsonl").read_text())

    async def test_returning_api_checks_long_stop_before_recreating_limit_exit(self):
        await self.check_returning_api_stop(1)

    async def test_returning_api_checks_short_stop_before_recreating_limit_exit(self):
        await self.check_returning_api_stop(-1)

    async def test_recovery_recreates_expired_limit_at_original_price(self):
        await self.start()
        bid, ask = self.quote(1), self.quote(-1)
        self.exchange.fill(bid["id"], D(".0010"))
        for order in self.maker.live():
            order["expires_at_ms"] = 1
        self.exchange.rows.clear()
        self.exchange.depth = book("3000.47", "3000.57")
        with patch.object(self.maker, "wait_for_retry", AsyncMock()):
            await self.maker.recover_after_outage()
        self.assertEqual(D(self.maker.state["expected_size"]), D(".0010"))
        self.assertEqual(bid["filled"], "0")  # Aucun fill individuel inventé.
        self.assertEqual(bid["status"], "ABSENT")
        await self.maker.step()
        exit_order = self.exchange.sent[-1]
        self.assertEqual(exit_order["kind"], "quote")
        self.assertTrue(exit_order["reduce_only"])
        self.assertEqual(exit_order["price"], ask["price"])
        self.assertEqual(len(self.exchange.sent), 3)

    async def test_recovery_preserves_known_active_exit_and_counts_visible_fill(self):
        await self.start()
        bid, ask = self.quote(1), self.quote(-1)
        self.exchange.fill(bid["id"])
        bid["expires_at_ms"] = 1
        del self.exchange.rows[bid["id"]]
        with patch.object(self.maker, "wait_for_retry", AsyncMock()):
            await self.maker.recover_after_outage()
        await self.maker.step()
        self.assertEqual(ask["status"], "OPEN")
        self.assertEqual(len(self.exchange.sent), 2)
        self.assertFalse(self.exchange.canceled)

    async def test_recovery_retains_missing_quotes_on_failed_or_changing_snapshot(self):
        order = self.seed_missing_quote(expires_at_ms=1)
        flat = {"size": D(0), "entry": D(0)}
        for second in (ApiError("HTML"), {"size": D(".0010"), "entry": D("3000.45")}):
            with self.subTest(second=second):
                before = deepcopy(self.maker.state)
                with patch.object(self.exchange, "position", AsyncMock(side_effect=[flat, second])), \
                        patch.object(self.maker, "wait_for_retry", AsyncMock()):
                    with self.assertRaises((ApiError, UncertainOrder)):
                        await self.maker.recover_after_outage()
                self.assertEqual(self.maker.state, before)
                self.assertEqual(order["status"], "PENDING")
        self.assertFalse(self.exchange.sent)

    async def test_recovery_requires_expiry_and_never_discards_unknown_market_close(self):
        order = self.seed_missing_quote(expires_at_ms=int(time.time() * 1000) + 60000)
        for changes in ({}, {"expires_at_ms": 1, "kind": "close"}):
            order.update(changes)
            with self.assertRaises(UncertainOrder):
                await self.maker.recover_after_outage()
            self.assertEqual(order["status"], "PENDING")
        self.assertFalse(self.exchange.sent)
        self.assertFalse(self.exchange.canceled)

    async def test_recovery_refuses_inventory_outside_possible_missing_fills(self):
        self.seed_missing_quote(expires_at_ms=1)
        self.exchange.current = {"size": D(".1"), "entry": D("3000.45")}
        with patch.object(self.maker, "wait_for_retry", AsyncMock()):
            with self.assertRaises(ReadModelLag):
                await self.maker.recover_after_outage()
        self.assertEqual(self.maker.state["expected_size"], "0")

    async def test_recovery_from_persisted_outage_after_restart(self):
        self.seed_missing_quote(expires_at_ms=1)
        self.maker.state["recovery_pending"] = True
        self.maker.save()
        restarted = MarketMaker(self.exchange, self.path)
        with patch.object(restarted, "wait_for_retry", AsyncMock()):
            await restarted.initialize()
        self.assertTrue(restarted.initialized)
        self.assertNotIn("recovery_pending", restarted.state)
        await restarted.step()
        self.assertEqual(len(self.exchange.sent), 2)

    async def test_stop_during_api_recovery_preserves_missing_orders(self):
        order = self.seed_missing_quote(expires_at_ms=1)
        before = deepcopy(self.maker.state)
        with patch.object(self.maker, "wait_for_retry", side_effect=lambda _: self.maker.stop.set()):
            await self.maker.recover_after_outage()
        self.assertEqual(self.maker.state, before)
        self.assertEqual(order["status"], "PENDING")
        self.assertFalse(self.exchange.sent)

    async def test_legacy_outage_quotes_wait_six_minutes_and_recover_original_exit(self):
        await self.start()
        bid, ask = self.quote(1), self.quote(-1)
        self.exchange.fill(bid["id"])
        for order in self.maker.live():
            order.pop("expires_at_ms")
            order.pop("opposite_price")
        self.exchange.rows.clear()
        with self.assertRaises(UncertainOrder):
            await self.maker.recover_after_outage()
        self.assertGreater(self.maker.outage_legacy_deadline - time.monotonic(), 360)
        self.maker.outage_legacy_deadline = 0
        with patch.object(self.maker, "wait_for_retry", AsyncMock()):
            await self.maker.recover_after_outage()
        await self.maker.step()
        self.assertEqual(self.exchange.sent[-1]["price"], ask["price"])
        self.assertTrue(self.exchange.sent[-1]["reduce_only"])

    async def test_late_visible_quote_is_not_archived_during_outage_recovery(self):
        order = self.seed_missing_quote(expires_at_ms=1)

        async def reappear(_):
            self.exchange.rows[order["id"]] = {**deepcopy(order), "status": "OPEN"}

        with patch.object(self.maker, "wait_for_retry", side_effect=reappear):
            with self.assertRaises(UncertainOrder):
                await self.maker.recover_after_outage()
        self.assertEqual(order["status"], "OPEN")
        self.assertFalse(self.exchange.sent)

    async def test_refresh_records_visible_fills_even_when_first_quote_is_missing(self):
        await self.start()
        bid, ask = self.quote(1), self.quote(-1)
        del self.exchange.rows[bid["id"]]
        self.exchange.fill(ask["id"])
        with self.assertRaises(UncertainOrder):
            await self.maker.refresh()
        self.assertEqual(D(self.maker.state["expected_size"]), self.exchange.current["size"])

    async def test_fill_during_cancellation_does_not_requote(self):
        await self.start()
        bid = self.quote(1)
        original_cancel = self.exchange.cancel

        async def racing_cancel(order):
            if order["id"] == bid["id"]:
                self.exchange.fill(order["id"])
            return await original_cancel(order)

        self.exchange.depth = book("3000.46", "3000.56")
        with patch.object(self.exchange, "cancel", side_effect=racing_cancel):
            await self.maker.step()
        self.assertEqual(len(self.exchange.sent), 2)
        self.assertTrue(self.maker.state["cycle_filled"])

    async def test_journal_is_saved_before_order_submission(self):
        await self.maker.initialize()
        original = self.exchange.place

        async def inspect_journal(order):
            state = json.loads(self.path.read_text(encoding="utf8"))
            self.assertTrue(any(o["id"] == order["id"] for o in state["orders"]))
            return await original(order)

        with patch.object(self.exchange, "place", side_effect=inspect_journal):
            await self.maker.step()

    async def test_unknown_send_is_reconciled_without_duplicate(self):
        await self.maker.initialize()
        original = self.exchange.place

        async def lost_response(order):
            await original(order)
            raise UncertainOrder("réponse perdue")

        with patch.object(self.exchange, "place", side_effect=lost_response):
            with self.assertRaises(UncertainOrder):
                await self.maker.step()
        await self.maker.step()
        self.assertEqual(len([o for o in self.exchange.sent if o["side"] == 1]), 1)

    async def test_explicit_rejection_is_terminal_and_not_canceled_on_restart(self):
        await self.maker.initialize()
        with patch.object(self.exchange, "place", side_effect=OrderRejected("invalid nonce")):
            with self.assertRaisesRegex(OrderRejected, "invalid nonce"):
                await self.maker.step()
        state = json.loads(self.path.read_text(encoding="utf8"))
        self.assertEqual(state["orders"][0]["status"], "REJECTED")
        self.assertEqual(D(state["expected_size"]), 0)
        self.assertFalse(self.maker.live())
        restarted = MarketMaker(self.exchange, self.path)
        await restarted.initialize()
        with self.assertRaises(QuoteRetryPending):
            await restarted.step()
        with patch("main.time.time", return_value=time.time() + 61):
            await restarted.step()
        self.assertEqual(len(restarted.live("quote")), 2)
        self.assertFalse(self.exchange.canceled)

    async def test_submission_details_survive_confirmation_timeout(self):
        await self.maker.initialize()

        async def accepted_but_not_visible(order):
            order["submission"] = {"state": "accepted", "tx_hash": "fake-hash"}
            raise UncertainOrder("non confirmé")

        with patch.object(self.exchange, "place", side_effect=accepted_but_not_visible):
            with self.assertRaises(UncertainOrder):
                await self.maker.step()
        state = json.loads(self.path.read_text(encoding="utf8"))
        self.assertEqual(state["orders"][0]["submission"]["tx_hash"], "fake-hash")
        self.assertEqual(state["orders"][0]["status"], "PENDING")

    async def test_restart_keeps_existing_exit_and_recovers_inventory(self):
        await self.start()
        bid = self.quote(1)
        ask_id = self.quote(-1)["id"]
        self.exchange.fill(bid["id"])
        restarted = MarketMaker(self.exchange, self.path, round_up=True)
        await restarted.initialize()
        self.assertEqual(D(restarted.state["expected_size"]), self.exchange.current["size"])
        await restarted.step()
        self.assertEqual(restarted.live("quote")[0]["id"], ask_id)
        self.assertEqual(restarted.live("quote")[0]["side"], -1)
        self.assertFalse(self.exchange.canceled)
        self.assertEqual(len(self.exchange.sent), 2)

    async def test_startup_preserves_both_existing_quotes_without_canceling(self):
        await self.start()
        before = [o["id"] for o in self.maker.live("quote")]
        restarted = MarketMaker(self.exchange, self.path)
        with patch.object(self.exchange, "set_leverage", new_callable=AsyncMock) as leverage:
            await restarted.initialize()
        self.assertEqual([o["id"] for o in restarted.live("quote")], before)
        self.assertFalse(self.exchange.canceled)
        self.assertEqual(len(self.exchange.sent), 2)
        leverage.assert_not_called()

    def seed_manually_closed_cycle(self, expected="-.0025"):
        order = self.seed_missing_quote(status="FILLED", filled=".0025", quantity=".0025",
                                        side=-1 if D(expected) < 0 else 1)
        self.maker.state.update(expected_size=expected, cycle_filled=True,
                                cycle_quantity=".0025", phase="stopping", exit_price="3000.45")
        self.maker.save()
        return order

    async def test_manual_short_close_resets_inventory_and_resumes_quotes(self):
        old_order = deepcopy(self.seed_manually_closed_cycle())
        with patch.object(self.maker, "wait_for_retry", new_callable=AsyncMock) as wait:
            await self.maker.initialize()
        wait.assert_awaited_once_with(5)
        self.assertTrue(self.maker.initialized)
        self.assertEqual(self.maker.state["expected_size"], "0")
        self.assertEqual(self.maker.state["orders"], [])
        self.assertEqual(self.maker.state["phase"], "quoting")
        self.assertIsNone(self.maker.state["cycle_quantity"])
        self.assertFalse(self.maker.state["cycle_filled"])
        self.assertNotIn("exit_price", self.maker.state)
        saved = json.loads(self.path.read_text(encoding="utf8"))
        self.assertEqual(saved["last_flat_recovery"]["previous_expected_size"], "-.0025")
        self.assertEqual(saved["last_flat_recovery"]["orders"], [old_order])
        audit = self.path.with_suffix(".orders.jsonl").read_text(encoding="utf8")
        self.assertIn("external_flat_reconciled", audit)
        self.assertIn(old_order["id"], audit)
        self.assertFalse(self.exchange.sent)
        self.assertFalse(self.exchange.canceled)
        await self.maker.step()
        self.assertEqual(len(self.maker.live("quote")), 2)

    async def test_manual_long_close_is_recovered_once_across_restarts(self):
        self.seed_manually_closed_cycle(expected=".0025")
        with patch.object(self.maker, "wait_for_retry", new_callable=AsyncMock):
            await self.maker.initialize()
        restarted = MarketMaker(self.exchange, self.path)
        with patch.object(restarted, "wait_for_retry", new_callable=AsyncMock) as wait:
            await restarted.initialize()
        wait.assert_not_awaited()
        self.assertTrue(restarted.initialized)
        self.assertEqual(restarted.state["last_flat_recovery"]["previous_expected_size"], ".0025")
        audit = self.path.with_suffix(".orders.jsonl").read_text(encoding="utf8")
        self.assertEqual(audit.count('"event": "external_flat_reconciled"'), 1)

    async def test_flat_recovery_is_blocked_by_active_quote(self):
        self.seed_manually_closed_cycle()
        active = self.seed_missing_quote(id="exit", side=1, status="OPEN")
        self.exchange.rows["exit"] = deepcopy(active)
        with patch.object(self.maker, "wait_for_retry", new_callable=AsyncMock) as wait:
            with self.assertRaises(ReadModelLag):
                await self.maker.initialize()
        wait.assert_not_awaited()
        self.assertEqual(self.maker.state["expected_size"], "-.0025")
        self.assertNotIn("last_flat_recovery", self.maker.state)
        self.assertFalse(self.exchange.sent)
        self.assertFalse(self.exchange.canceled)

    async def test_flat_recovery_waits_for_invisible_quote_expiry(self):
        self.seed_manually_closed_cycle()
        pending = self.seed_missing_quote(id="pending", expires_at_ms=int(time.time() * 1000) + 60000)
        with patch.object(self.maker, "wait_for_retry", new_callable=AsyncMock) as wait:
            await self.maker.initialize()
        self.assertGreater(wait.call_args.args[0], 60)
        recovery = self.maker.state["last_flat_recovery"]
        archived = next(o for o in recovery["orders"] if o["id"] == pending["id"])
        self.assertEqual(archived["status"], "ABSENT")
        self.assertEqual(self.maker.state["expected_size"], "0")

    async def test_flat_recovery_never_discards_uncertain_market_close(self):
        self.seed_manually_closed_cycle()
        self.seed_missing_quote(id="close", kind="close", reduce_only=True)
        with patch.object(self.maker, "wait_for_retry", new_callable=AsyncMock) as wait:
            with self.assertRaises(ReadModelLag):
                await self.maker.initialize()
        wait.assert_not_awaited()
        self.assertEqual(self.maker.state["expected_size"], "-.0025")
        self.assertNotIn("last_flat_recovery", self.maker.state)

    async def test_flat_recovery_second_read_must_confirm_empty_account(self):
        for method, second in (("position", {"size": D("-.0025"), "entry": D(3000)}),
                               ("active_orders", [{"client_id": "249670502102335", "status": "OPEN"}])):
            with self.subTest(method=method):
                self.seed_manually_closed_cycle()
                original = getattr(self.exchange, method)
                calls = 0

                async def changing_account():
                    nonlocal calls
                    calls += 1
                    return second if calls == 3 else await original()

                with patch.object(self.exchange, method, side_effect=changing_account), \
                        patch.object(self.maker, "wait_for_retry", new_callable=AsyncMock):
                    with self.assertRaises(ReadModelLag):
                        await self.maker.initialize()
                self.assertEqual(self.maker.state["expected_size"], "-.0025")
                self.assertNotIn("last_flat_recovery", self.maker.state)

    async def test_flat_recovery_api_failure_keeps_inventory(self):
        for method in ("active_orders", "position"):
            with self.subTest(method=method):
                self.seed_manually_closed_cycle()
                original = getattr(self.exchange, method)
                calls = 0

                async def failed_read():
                    nonlocal calls
                    calls += 1
                    if calls == 3:
                        raise RateLimited("429")
                    return await original()

                with patch.object(self.exchange, method, side_effect=failed_read), \
                        patch.object(self.maker, "wait_for_retry", new_callable=AsyncMock):
                    with self.assertRaises(RateLimited):
                        await self.maker.initialize()
                self.assertEqual(self.maker.state["expected_size"], "-.0025")
                self.assertNotIn("last_flat_recovery", self.maker.state)

    async def test_stop_during_flat_recovery_keeps_inventory(self):
        self.seed_manually_closed_cycle()
        before = deepcopy(self.maker.state)

        async def stop_retry(delay):
            self.maker.stop.set()

        with patch.object(self.maker, "wait_for_retry", side_effect=stop_retry):
            await self.maker.initialize()
        self.assertFalse(self.maker.initialized)
        self.assertEqual(self.maker.state, before)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf8")), before)
        self.assertFalse(self.exchange.sent)
        self.assertFalse(self.exchange.canceled)

    async def test_legacy_phantom_quote_is_archived_when_grace_elapsed_and_api_flat(self):
        order = self.seed_missing_quote()
        self.maker.state["phase"] = "stopping"
        self.maker.legacy_recovery_deadline = 0
        await self.maker.initialize()
        self.assertEqual(order["status"], "ABSENT")
        self.assertFalse(self.exchange.canceled)
        self.assertFalse(self.exchange.sent)
        saved = json.loads(self.path.read_text(encoding="utf8"))
        self.assertEqual(saved["orders"][0]["status"], "ABSENT")
        audit = self.path.with_suffix(".orders.jsonl").read_text(encoding="utf8")
        self.assertIn("order_reconciled_absent", audit)
        self.assertIn("249670502102335", audit)
        await self.maker.step()
        await self.maker.step()
        self.assertEqual(len(self.maker.live("quote")), 2)

    async def test_expired_missing_quote_with_stored_expiry_needs_no_legacy_wait(self):
        order = self.seed_missing_quote(expires_at_ms=1)
        with patch("main.asyncio.wait_for", new_callable=AsyncMock) as wait:
            await self.maker.initialize()
        wait.assert_not_called()
        self.assertEqual(order["status"], "ABSENT")

    async def test_fresh_missing_quote_waits_and_stop_does_not_discard_it(self):
        import time
        order = self.seed_missing_quote(expires_at_ms=int(time.time() * 1000) + 60000)

        async def interrupt_wait(coroutine, timeout):
            self.assertGreater(timeout, 60)
            self.maker.stop.set()
            return await coroutine

        with patch("main.asyncio.wait_for", side_effect=interrupt_wait):
            await self.maker.run()
        self.assertEqual(order["status"], "PENDING")
        self.assertFalse(self.exchange.canceled)
        self.assertFalse(self.exchange.sent)
        self.assertFalse(self.maker.initialized)

    async def test_legacy_missing_quote_gets_one_minute_grace(self):
        order = self.seed_missing_quote()

        async def interrupt_wait(coroutine, timeout):
            self.assertGreater(timeout, 60)
            self.maker.stop.set()
            return await coroutine

        with patch("main.asyncio.wait_for", side_effect=interrupt_wait):
            await self.maker.initialize()
        self.assertEqual(order["status"], "PENDING")
        self.assertFalse(self.exchange.canceled)

    async def test_missing_reference_with_position_is_not_discarded(self):
        order = self.seed_missing_quote(expires_at_ms=1)
        self.exchange.current = {"size": D(".0034"), "entry": D("3000.45")}
        with self.assertRaises(ReadModelLag):
            await self.maker.initialize()
        self.assertEqual(order["status"], "PENDING")
        self.assertFalse(self.exchange.canceled)

    async def test_missing_reference_with_reconciled_position_is_not_discarded(self):
        order = self.seed_missing_quote(expires_at_ms=1)
        self.maker.state["expected_size"] = ".0034"
        self.exchange.current = {"size": D(".0034"), "entry": D("3000.45")}
        with self.assertRaises(UncertainOrder):
            await self.maker.initialize()
        self.assertEqual(order["status"], "PENDING")
        self.assertFalse(self.exchange.canceled)

    async def test_missing_reference_with_real_open_order_is_not_discarded(self):
        await self.start()
        order = self.seed_missing_quote(expires_at_ms=1)
        with self.assertRaises(UncertainOrder):
            await self.maker.initialize()
        self.assertEqual(order["status"], "PENDING")
        self.assertFalse(self.exchange.canceled)

    async def test_api_failure_never_means_missing_order_can_be_discarded(self):
        order = self.seed_missing_quote(expires_at_ms=1)
        for method in ("active_orders", "order_status", "position"):
            with self.subTest(method=method):
                with patch.object(self.exchange, method, side_effect=RuntimeError("offline")):
                    with self.assertRaisesRegex(RuntimeError, "offline"):
                        await self.maker.initialize()
                self.assertEqual(order["status"], "PENDING")
        self.assertFalse(self.exchange.canceled)

    async def test_late_appearing_order_is_kept_after_grace(self):
        order = self.seed_missing_quote()

        async def make_order_visible(coroutine, timeout):
            coroutine.close()
            self.exchange.rows[order["id"]] = {**deepcopy(order), "status": "OPEN"}
            raise TimeoutError()

        with patch("main.asyncio.wait_for", side_effect=make_order_visible):
            await self.maker.initialize()
        self.assertEqual(order["status"], "OPEN")
        self.assertFalse(self.exchange.canceled)
        self.assertFalse(self.exchange.sent)

    async def test_second_snapshot_failure_keeps_missing_reference(self):
        order = self.seed_missing_quote(expires_at_ms=1)
        with patch.object(self.exchange, "order_status", side_effect=[None, RuntimeError("offline")]):
            with self.assertRaisesRegex(RuntimeError, "offline"):
                await self.maker.initialize()
        self.assertEqual(order["status"], "PENDING")
        self.assertFalse(self.exchange.canceled)

    async def test_position_appearing_in_second_snapshot_prevents_retirement(self):
        order = self.seed_missing_quote(expires_at_ms=1)
        with patch.object(self.exchange, "position", side_effect=[
                {"size": D(0), "entry": D(0)},
                {"size": D(".0034"), "entry": D("3000.45")} ]):
            with self.assertRaises(ReadModelLag):
                await self.maker.initialize()
        self.assertEqual(order["status"], "PENDING")
        self.assertFalse(self.exchange.canceled)

    async def test_missing_market_close_is_never_retired_as_expired_quote(self):
        order = self.seed_missing_quote(kind="close", expires_at_ms=1, reduce_only=True)
        with self.assertRaises(UncertainOrder):
            await self.maker.initialize()
        self.assertEqual(order["status"], "PENDING")
        self.assertFalse(self.exchange.canceled)

    async def test_quote_expiry_is_persisted_before_submission(self):
        await self.maker.initialize()
        original = self.exchange.place

        async def verify_expiry(order):
            saved = json.loads(self.path.read_text(encoding="utf8"))
            recorded = next(o for o in saved["orders"] if o["id"] == order["id"])
            self.assertEqual(recorded["expires_at_ms"], order["expires_at_ms"])
            # Lighter's minimum validity period is 300,000 milliseconds.
            self.assertGreater(recorded["expires_at_ms"] - int(time.time() * 1000), 300000)
            return await original(order)

        with patch.object(self.exchange, "place", side_effect=verify_expiry):
            await self.maker.step()

    async def test_rejects_other_strategy_journal(self):
        self.path.write_text(json.dumps({"strategy": "funding", "identity": self.exchange.identity}))
        with self.assertRaises(ValueError):
            MarketMaker(self.exchange, self.path)

    async def test_rejects_other_account_journal(self):
        await self.start()
        self.exchange.identity = "other-account"
        with self.assertRaises(ValueError):
            MarketMaker(self.exchange, self.path)

    async def test_external_orders_block_startup(self):
        self.exchange.rows["external"] = {"id": "external", "status": "OPEN"}
        with self.assertRaises(ValueError):
            await self.maker.initialize()
        self.assertFalse(self.exchange.sent)

    async def test_external_position_blocks_startup(self):
        self.exchange.current = {"size": D(".01"), "entry": D(3000)}
        with self.assertRaises(ReadModelLag):
            await self.maker.initialize()

    async def test_cancel_failure_blocks_stop_submission(self):
        await self.start()
        self.exchange.fill(self.quote(1)["id"])
        self.exchange.depth = book("2999.45", "2999.55")
        with patch.object(self.exchange, "cancel", side_effect=UncertainOrder("incertain")):
            with self.assertRaises(UncertainOrder):
                await self.maker.step()
        self.assertEqual(len(self.exchange.sent), 2)
        self.assertEqual(self.maker.state["phase"], "stopping")

    async def test_shutdown_cancels_quotes_and_flattens(self):
        await self.start()
        self.exchange.fill(self.quote(1)["id"])
        await self.maker.shutdown()
        self.assertEqual(self.exchange.current["size"], 0)
        self.assertFalse(self.maker.live())

    async def test_strict_mode_does_not_send_undersized_or_oversized_orders(self):
        self.maker.round_up = False
        await self.start()
        self.assertFalse(self.exchange.sent)

    async def test_exit_fill_during_stop_cancellation_does_not_open_inverse_position(self):
        await self.start()
        self.exchange.fill(self.quote(1)["id"])
        self.exchange.depth = book("2999.45", "2999.55")
        original = self.exchange.cancel

        async def racing_exit(order):
            self.exchange.fill(order["id"])
            return await original(order)

        with patch.object(self.exchange, "cancel", side_effect=racing_exit):
            await self.maker.step()
        self.assertEqual(self.exchange.current["size"], 0)
        self.assertEqual(len(self.exchange.sent), 2)

    async def test_inventory_does_not_grow_after_repeated_entry_cycles(self):
        await self.start()
        quantity = D(self.quote(1)["quantity"])
        self.exchange.fill(self.quote(1)["id"], D(".001"))
        for _ in range(8):
            await self.maker.step()
        self.assertEqual(self.exchange.current["size"], D(".001"))
        self.assertLessEqual(abs(self.exchange.current["size"]), quantity)
        self.assertEqual(len([o for o in self.exchange.sent if o["side"] == 1]), 1)


class LighterAdapterTests(unittest.IsolatedAsyncioTestCase):
    def adapter(self):
        adapter = Lighter.__new__(Lighter)
        adapter.account, adapter.key_index = 1, 4
        adapter.market = market()
        adapter.lock = asyncio.Lock()
        adapter.signer = SimpleNamespace(ORDER_TYPE_LIMIT=0, ORDER_TYPE_MARKET=1, ORDER_TYPE_STOP_LOSS=2,
            ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL=0, ORDER_TIME_IN_FORCE_POST_ONLY=2,
            DEFAULT_IOC_EXPIRY=0, DEFAULT_28_DAY_ORDER_EXPIRY=-1, ISOLATED_MARGIN_MODE=1,
            create_order=AsyncMock(return_value=(None, SimpleNamespace(code=200), None)),
            cancel_order=AsyncMock(return_value=(None, SimpleNamespace(code=200), None)),
            update_leverage=AsyncMock(return_value=(None, SimpleNamespace(code=200), None)))
        adapter.confirm = AsyncMock(return_value={"status": "OPEN", "filled": "0"})
        return adapter

    def order(self, **kwargs):
        return {"id": "123", "kind": "quote", "side": 1, "quantity": ".0034",
                "price": "3000.45", "reduce_only": False, **kwargs}

    async def test_quote_is_post_only_at_exact_price(self):
        adapter = self.adapter()
        await adapter.place(self.order())
        sent = adapter.signer.create_order.call_args.kwargs
        self.assertEqual(sent["market_index"], 40)
        self.assertEqual(sent["price"], 300045)
        self.assertEqual(sent["base_amount"], 34)
        self.assertEqual(sent["order_type"], 0)
        self.assertEqual(sent["time_in_force"], 2)
        self.assertFalse(sent["reduce_only"])
        self.assertNotIn("nonce", sent)
        self.assertNotIn("api_key_index", sent)

    async def test_close_is_market_ioc_reduce_only(self):
        adapter = self.adapter()
        await adapter.place(self.order(kind="close", side=-1, reduce_only=True))
        sent = adapter.signer.create_order.call_args.kwargs
        self.assertEqual(sent["order_type"], 1)
        self.assertEqual(sent["time_in_force"], 0)
        self.assertEqual(sent["order_expiry"], 0)
        self.assertTrue(sent["is_ask"])
        self.assertTrue(sent["reduce_only"])
        adapter.confirm.assert_awaited_once_with("123", terminal=True)

    async def test_quote_uses_expiry_already_persisted_in_journal(self):
        adapter = self.adapter()
        order = self.order(expires_at_ms=1790000060000)
        await adapter.place(order)
        self.assertEqual(adapter.signer.create_order.call_args.kwargs["order_expiry"],
                         order["expires_at_ms"])

    async def test_quote_expiry_respects_lighter_minimum_in_milliseconds(self):
        adapter = self.adapter()
        now = 1793000000.125
        with patch("exchanges.time.time", return_value=now):
            await adapter.place(self.order())
        expiry = adapter.signer.create_order.call_args.kwargs["order_expiry"]
        # Official MinOrderExpiryPeriod is 1000 * 60 * 5. A 60-second
        # expiry signs successfully but is rejected by the exchange (21711).
        self.assertGreater(expiry - int(now * 1000), 300000)
        self.assertEqual(expiry, int(now * 1000) + 360000)

    async def test_timeout_does_not_resend_order(self):
        adapter = self.adapter()
        adapter.signer.create_order.side_effect = TimeoutError()
        await adapter.place(self.order())
        self.assertEqual(adapter.signer.create_order.await_count, 1)
        adapter.confirm.assert_awaited_once()

    async def test_sdk_rejection_exposes_reason_without_waiting_for_confirmation(self):
        adapter = self.adapter()
        adapter.signer.create_order.return_value = (None, None, "invalid nonce")
        order = self.order()
        with self.assertRaisesRegex(OrderRejected, "invalid nonce"):
            await adapter.place(order)
        adapter.confirm.assert_not_called()
        self.assertEqual(order["submission"]["state"], "rejected")

    async def test_api_rejection_exposes_code_and_message(self):
        adapter = self.adapter()
        adapter.signer.create_order.return_value = (
            None, SimpleNamespace(code=21111, message="insufficient margin"), None)
        with self.assertRaisesRegex(OrderRejected, "21111.*insufficient margin"):
            await adapter.place(self.order())
        adapter.confirm.assert_not_called()

    async def test_21706_reports_submitted_amount_price_and_market_minimums(self):
        adapter = self.adapter()
        adapter.signer.create_order.return_value = (None, None, "code=21706 invalid order base or quote amount")
        with self.assertRaises(OrderRejected) as failure:
            await adapter.place(self.order(quantity=".0010", reduce_only=True))
        message = str(failure.exception)
        self.assertIn("base_amount=10", message)
        self.assertIn("price=300045", message)
        self.assertIn("reduce_only=True", message)
        self.assertIn("min_base=0.0020", message)
        self.assertIn("min_quote=10", message)
        adapter.confirm.assert_not_awaited()

    async def test_unknown_timeout_stays_uncertain_with_original_context(self):
        adapter = self.adapter()
        adapter.signer.create_order.side_effect = TimeoutError()
        adapter.confirm.side_effect = UncertainOrder("absent")
        order = self.order()
        with self.assertRaisesRegex(UncertainOrder, "TimeoutError"):
            await adapter.place(order)
        self.assertEqual(order["submission"]["state"], "unknown")
        self.assertEqual(adapter.signer.create_order.await_count, 1)

    async def test_accepted_but_invisible_order_reports_transaction_hash(self):
        adapter = self.adapter()
        adapter.signer.create_order.return_value = (
            None, SimpleNamespace(code=200, tx_hash="fake-hash"), None)
        adapter.confirm.side_effect = UncertainOrder("absent")
        with self.assertRaisesRegex(UncertainOrder, "fake-hash"):
            await adapter.place(self.order())

    async def test_sdk_exception_message_survives_failed_confirmation(self):
        adapter = self.adapter()
        adapter.signer.create_order.side_effect = RuntimeError("SDK transport unavailable")
        adapter.confirm.side_effect = UncertainOrder("absent")
        with self.assertRaisesRegex(UncertainOrder, "SDK transport unavailable"):
            await adapter.place(self.order())

    async def test_empty_sdk_response_is_not_treated_as_explicit_rejection(self):
        adapter = self.adapter()
        adapter.signer.create_order.return_value = (None, None, None)
        adapter.confirm.side_effect = UncertainOrder("absent")
        order = self.order()
        with self.assertRaises(UncertainOrder):
            await adapter.place(order)
        self.assertEqual(order["submission"]["state"], "unknown")

    async def test_invalid_quantity_is_rejected_before_sending(self):
        adapter = self.adapter()
        with self.assertRaises(ValueError):
            await adapter.place(self.order(quantity=".00341"))
        adapter.signer.create_order.assert_not_called()

    async def test_aggregates_tick_liquidity_and_excludes_own_orders(self):
        adapter = self.adapter()
        adapter.get = AsyncMock(return_value={
            "bids": [{"price": "3000.45", "remaining_base_amount": ".02", "owner_account_index": 2},
                     {"price": "3000.45", "remaining_base_amount": ".03", "owner_account_index": 3},
                     {"price": "3000.45", "remaining_base_amount": ".0034", "owner_account_index": 1}],
            "asks": [{"price": "3000.55", "remaining_base_amount": ".05", "owner_account_index": 2}]})
        depth = await adapter.book()
        self.assertEqual(depth["bids"], [(D("3000.45"), D(".0534"))])
        self.assertEqual(depth["external_bids"][D("3000.45")], D(".05"))

    async def test_xau_only_no_gold_alias_or_other_symbol(self):
        adapter = self.adapter()
        other = {**market(), "symbol": "GLD"}
        adapter.get = AsyncMock(return_value={"order_book_details": [other]})
        with self.assertRaises(ValueError):
            await adapter.load_market()

    async def test_loads_xau_with_live_precision(self):
        adapter = self.adapter()
        adapter.get = AsyncMock(return_value={"order_book_details": [market()]})
        result = await adapter.load_market()
        self.assertEqual(result["market_id"], 40)
        self.assertEqual(result["step"], D(".0001"))
        self.assertEqual(result["tick"], D(".01"))

    async def test_frozen_or_inactive_xau_is_rejected(self):
        for change in ({"status": "inactive"}, {"is_frozen": True},
                       {"market_config": {"force_reduce_only": True}}, {"multiplier": "1000"}):
            adapter = self.adapter()
            adapter.get = AsyncMock(return_value={"order_book_details": [{**market(), **change}]})
            with self.assertRaises(ValueError):
                await adapter.load_market()

    async def test_cancel_confirms_terminal_and_uses_server_index(self):
        adapter = self.adapter()
        adapter.order_status = AsyncMock(return_value={"status": "OPEN", "order_index": 999})
        adapter.confirm.return_value = {"status": "CANCELED", "filled": "0"}
        await adapter.cancel(self.order())
        self.assertEqual(adapter.signer.cancel_order.call_args.kwargs["order_index"], 999)
        adapter.confirm.assert_awaited_once_with("123", terminal=True)

    async def test_cancel_unknown_order_uses_client_id(self):
        adapter = self.adapter()
        adapter.order_status = AsyncMock(return_value=None)
        adapter.confirm.return_value = {"status": "CANCELED", "filled": "0"}
        await adapter.cancel(self.order())
        self.assertEqual(adapter.signer.cancel_order.call_args.kwargs["order_index"], 123)

    async def test_cancel_sends_despite_rest_quota_and_keeps_confirmation_uncertain(self):
        for fields, expected_index in (({"order_index": 999}, 999), ({}, 123)):
            adapter = self.adapter()
            adapter.order_status = AsyncMock(side_effect=RateLimited("429"))
            adapter.confirm.side_effect = RateLimited("429")
            with self.assertRaisesRegex(UncertainOrder, "429"):
                await adapter.cancel(self.order(**fields))
            adapter.signer.cancel_order.assert_awaited_once_with(market_index=40, order_index=expected_index)
            adapter.confirm.assert_awaited_once_with("123", terminal=True)

    async def test_batch_status_uses_one_active_snapshot_and_shared_history(self):
        adapter = self.adapter()
        adapter.active_orders = AsyncMock(return_value=[{"client_id": "123", "status": "OPEN"}])
        adapter.get = AsyncMock(side_effect=[
            {"orders": [{"client_order_index": 456, "status": "filled", "filled_base_amount": ".0034"}],
             "next_cursor": "page2"},
            {"orders": [{"client_order_index": 789, "status": "canceled", "filled_base_amount": "0"}]}
        ])
        rows = await adapter.order_statuses(["123", "456", "789", "absent"])
        self.assertEqual(rows["123"]["status"], "OPEN")
        self.assertEqual(rows["456"]["status"], "FILLED")
        self.assertEqual(rows["789"]["status"], "CANCELED")
        self.assertIsNone(rows["absent"])
        adapter.active_orders.assert_awaited_once()
        self.assertEqual(adapter.get.await_count, 2)
        self.assertEqual(adapter.get.call_args.kwargs["cursor"], "page2")

    async def test_batch_of_open_orders_does_not_read_history(self):
        adapter = self.adapter()
        adapter.active_orders = AsyncMock(return_value=[{"client_id": "123"}, {"client_id": "456"}])
        adapter.get = AsyncMock()
        rows = await adapter.order_statuses(["123", "456"])
        self.assertEqual(set(rows), {"123", "456"})
        adapter.active_orders.assert_awaited_once()
        adapter.get.assert_not_awaited()

    async def test_empty_batch_sends_no_request(self):
        adapter = self.adapter()
        adapter.active_orders = AsyncMock()
        self.assertEqual(await adapter.order_statuses([]), {})
        adapter.active_orders.assert_not_awaited()

    async def test_batch_api_failure_never_becomes_missing_orders(self):
        adapter = self.adapter()
        adapter.active_orders = AsyncMock(side_effect=RateLimited("429"))
        with self.assertRaises(RateLimited):
            await adapter.order_statuses(["123", "456"])

    async def test_batch_rejects_repeated_history_cursor(self):
        adapter = self.adapter()
        adapter.active_orders = AsyncMock(return_value=[])
        adapter.get = AsyncMock(return_value={"orders": [], "next_cursor": "same"})
        with self.assertRaisesRegex(ApiError, "Pagination"):
            await adapter.order_statuses(["123"])

    async def test_failed_cancel_exposes_reason_without_marking_order_terminal(self):
        adapter = self.adapter()
        adapter.order_status = AsyncMock(return_value=None)
        adapter.signer.cancel_order.return_value = (None, None, "order not found")
        adapter.confirm.side_effect = UncertainOrder("absent")
        with self.assertRaisesRegex(UncertainOrder, "order not found"):
            await adapter.cancel(self.order())

    async def test_canceled_post_only_is_terminal(self):
        row = Lighter.normalize({"status": "canceled-post-only", "client_order_index": 123,
                                 "filled_base_amount": "0"})
        self.assertEqual(row["status"], "CANCELED")

    async def test_more_margin_setting_is_preserved(self):
        adapter = self.adapter()
        adapter.account_rows = AsyncMock(return_value=[{"market_id": 40, "margin_mode": 1,
                                                        "initial_margin_fraction": "6.25"}])
        await adapter.set_leverage()
        self.assertEqual(adapter.signer.update_leverage.call_args.kwargs["leverage"], 16)

    async def test_rejects_other_chain_and_reserved_keys(self):
        env = {"LIGHTER_BASE_URL": "https://api.rh-testnet.lighter.xyz",
               "LIGHTER_ACCOUNT_INDEX": "1", "LIGHTER_API_KEY_INDEX": "4",
               "LIGHTER_API_PRIVATE_KEY": "unused"}
        with patch.dict("os.environ", env):
            with self.assertRaises(ValueError):
                Lighter(None)
        env["LIGHTER_BASE_URL"] = Lighter.MAINNET
        for key in ("0", "3", "157", "255"):
            env["LIGHTER_API_KEY_INDEX"] = key
            with patch.dict("os.environ", env):
                with self.assertRaises(ValueError):
                    Lighter(None)

    async def test_real_sdk_signs_post_only_xau_offline(self):
        import lighter
        client = lighter.SignerClient(url=Lighter.MAINNET, account_index=1,
                                      api_private_keys={4: "1" * 80}, chain_id=Lighter.CHAIN_ID)
        try:
            expiry = int(time.time() * 1000) + 360000
            _, body, _, error = client.sign_create_order(market_index=40, client_order_index=123,
                base_amount=34, price=300045, is_ask=0, order_type=client.ORDER_TYPE_LIMIT,
                time_in_force=client.ORDER_TIME_IN_FORCE_POST_ONLY, reduce_only=False,
                order_expiry=expiry, nonce=1, api_key_index=4)
            self.assertIsNone(error)
            signed = json.loads(body)
            self.assertEqual(signed["MarketIndex"], 40)
            self.assertEqual(signed["TimeInForce"], 2)
            self.assertEqual(signed["BaseAmount"], 34)
            self.assertEqual(signed["OrderExpiry"], expiry)
            self.assertTrue(signed["Sig"])
        finally:
            await client.close()


if __name__ == "__main__":
    unittest.main()
