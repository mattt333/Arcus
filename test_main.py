import asyncio
import json
import tempfile
import time
import unittest
from copy import deepcopy
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from exchanges import Lighter, OrderRejected, TERMINAL, UncertainOrder
from main import MarketMaker, QuoteUnavailable, ReadModelLag, quote_quantity, state_lock, stop_hit


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

    async def place(self, order):
        self.sent.append(deepcopy(order))
        self.rows[order["id"]] = {**deepcopy(order), "status": "OPEN"}
        if order["kind"] == "close":
            self.fill(order["id"], D(order["quantity"]) * self.close_fraction)
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

    def test_stop_is_one_price_unit_in_both_directions(self):
        long = {"size": D(".0034"), "entry": D("3000.45")}
        short = {"size": D("-.0034"), "entry": D("3000.55")}
        self.assertFalse(stop_hit(long, book("2999.46", "2999.56")))
        self.assertTrue(stop_hit(long, book("2999.45", "2999.55")))
        self.assertFalse(stop_hit(short, book("3001.44", "3001.54")))
        self.assertTrue(stop_hit(short, book("3001.45", "3001.55")))


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
        self.assertEqual(D(exit_order["quantity"]), D(".0010"))
        self.assertEqual(exit_order["price"], ask["price"])
        self.assertTrue(exit_order["reduce_only"])
        self.assertFalse(any(o["side"] == 1 for o in self.maker.live()))

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

    async def test_exit_also_respects_liquidity_rule(self):
        await self.start()
        ask = deepcopy(self.quote(-1))
        self.exchange.fill(self.quote(1)["id"])
        self.exchange.depth = book("3000.45", "3000.60")
        await self.maker.step()
        self.assertIn(ask["id"], self.exchange.canceled)
        await self.maker.step()
        self.assertEqual(D(self.quote(-1)["price"]), D("3000.60"))
        self.assertTrue(self.quote(-1)["reduce_only"])

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
        self.assertEqual(D(self.exchange.sent[-1]["quantity"]), quantity / 2)
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
        with patch.object(self.exchange, "position", AsyncMock(return_value={"size": D(0), "entry": D(0)})):
            with self.assertRaises(ReadModelLag):
                await self.maker.step()
        self.assertEqual(len(self.exchange.sent), 2)

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
