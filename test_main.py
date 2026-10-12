import asyncio
import json
import tempfile
import unittest
from copy import deepcopy
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import AsyncMock, patch

import main
from main import Farmer, common_step, direction, entry_plan, funding_score, match_markets, relative_gap, risk_reason, update_history
from exchanges import Arcus, Lighter, UncertainOrder, canonical, wait_position


def market_info():
    a = {"marketId": 28, "marketDisplayName": "NVDA-USD", "category": "EQUITIES", "status": "ONLINE",
         "stepSize": "0.001", "tickSize": "0.01", "minOrderSize": "0.01", "maxOrderSize": "1000",
         "minOrderNotional": "5", "initialMarginFraction": "0.05", "maintenanceMarginFraction": "0.03",
         "nextFundingRate": "0.00001", "markPrice": "100"}
    a["oraclePrice"] = "101"
    e = {"name": "NVDA-USD", "symbol": "NVDA", "market_id": 14,
         "market_type": "perp", "status": "active", "multiplier": "1", "quote_multiplier": 1,
         "stepSize": "0.01", "tickSize": "0.01", "minOrderSize": "0.1",
         "minOrderNotional": "10", "maxOrderNotional": "100000",
         "supported_size_decimals": 2, "supported_price_decimals": 2,
         "min_initial_margin_fraction": 500,
         "fundingRate": "0.00002", "markPrice": "100", "oraclePrice": "99"}
    return a, e


def book():
    return {"bids": [(D("99.99"), D("10"))], "asks": [(D("100.01"), D("10"))]}


def position(size, mark="100", entry="100", liquidation=None):
    size = D(size)
    return {"size": size, "mark": D(mark), "entry": D(entry),
            "liquidation": D(liquidation) if liquidation else D("95" if size > 0 else "105")}


class StrategyTests(unittest.TestCase):
    def test_mark_oracle_gate(self):
        main.check_oracle("99", "100", 1)
        main.check_oracle("101", "100", -1)
        for mark, oracle, side in (("101", "100", 1), ("99", "100", -1),
                                   ("100", "100", 1), ("NaN", "100", 1),
                                   ("100", "0", -1)):
            with self.assertRaises(ValueError):
                main.check_oracle(mark, oracle, side)

    def test_funding_signs_and_relative_gap(self):
        self.assertEqual(direction(D("-.01"), D(".01")), 1)
        self.assertEqual(direction(D(".01"), D("-.01")), -1)
        self.assertEqual(direction(D("-.01"), D("-.02")), -1)
        self.assertEqual(relative_gap(D(".008"), D(".01")), D(".2"))
        self.assertEqual(relative_gap(D(0), D(0)), 0)

    def test_first_eligible_sample_is_immediately_ranked(self):
        samples = []
        update_history(samples, 0, D(".00001"), D(".00002"))
        self.assertIsNotNone(funding_score(samples, 0))
        update_history(samples, 30, D(".00002"), D(".00001"))
        self.assertEqual(len(samples), 1)
        self.assertIsNotNone(funding_score(samples, 30))
        self.assertIsNone(funding_score([], 0))

    def test_spread_limit_on_each_exchange_and_boundary(self):
        a, e = market_info()
        boundary = {"bids": [(D("99.985"), D(10))], "asks": [(D("100.015"), D(10))]}
        self.assertGreater(entry_plan(a, e, (boundary, boundary), 1), 0)
        wide = {"bids": [(D("99.984"), D(10))], "asks": [(D("100.016"), D(10))]}
        for books in ((wide, book()), (book(), wide)):
            with self.assertRaisesRegex(ValueError, "Spread"):
                entry_plan(a, e, books, 1)

    def test_history_resets_on_inversion_low_gap_and_missing_data(self):
        for now, a, b in ((30, ".02", ".01"), (30, ".01", ".011"), (100, ".01", ".02")):
            samples = [[0, ".01", ".02"]]
            update_history(samples, now, D(a), D(b))
            self.assertLessEqual(len(samples), 1)

    def test_stable_gap_scores_better(self):
        stable = [[0, "0", ".02"], [14400, "0", ".02"]]
        volatile = [[0, "0", ".001"], [14400, "0", ".039"]]
        self.assertGreater(funding_score(stable, 14400), funding_score(volatile, 14400))

    def test_common_lot_and_equal_quantities(self):
        self.assertEqual(common_step(D(".006"), D(".004")), D(".012"))
        a, e = market_info()
        quantity = entry_plan(a, e, (book(), book()), 1)
        self.assertEqual(quantity, D(".99"))
        self.assertLessEqual(quantity * D("100.01"), D(100))
        self.assertEqual(quantity % D(a["stepSize"]), 0)
        self.assertEqual(quantity % D(e["stepSize"]), 0)

    def test_spread_basis_and_depth_limits(self):
        a, e = market_info()
        bad = book(); bad["asks"] = [(D("101"), D(10))]
        with self.assertRaisesRegex(ValueError, "Spread"):
            entry_plan(a, e, (bad, book()), 1)
        bad = {"bids": [(D("101"), D(10))], "asks": [(D("101.01"), D(10))]}
        with self.assertRaisesRegex(ValueError, "Écart"):
            entry_plan(a, e, (book(), bad), 1)
        bad = book(); bad["asks"] = [(D("100.01"), D(".01"))]
        with self.assertRaisesRegex(ValueError, "Profondeur"):
            entry_plan(a, e, (bad, book()), 1)

    def test_crypto_and_different_underlyings_excluded(self):
        a, e = market_info()
        self.assertEqual(match_markets([a], [e])["NVDA-USD"][1]["name"], "NVDA-USD")
        a["category"] = "CRYPTO"
        self.assertFalse(match_markets([a], [e]))
        a["category"] = "COMMODITIES"; a["marketDisplayName"] = "GLD-USD"
        e["name"] = "XAU-USD"; e["symbol"] = "XAU"
        self.assertFalse(match_markets([a], [e]))

    def test_liquidation_before_ten_percent_move(self):
        self.assertIsNone(risk_reason(position("1")))
        self.assertIsNone(risk_reason(position("-1")))
        self.assertEqual(risk_reason(position("1", mark="95.5")), "proximité de liquidation")
        self.assertEqual(risk_reason(position("-1", mark="104.5")), "proximité de liquidation")
        self.assertEqual(risk_reason(position("1", mark="110")), "mouvement de prix de 10 %")
        self.assertIsNotNone(risk_reason(None))


class FakeExchange:
    def __init__(self, name, info):
        self.name = self.identity = name
        self.rows = {}
        self.info = info
        self.orders = []
        self.partial = False
        self.fail = False

    async def positions(self): return deepcopy(self.rows)
    async def markets(self): return [deepcopy(self.info)]
    async def book(self, name): return book()
    async def check_orders(self): pass
    async def cancel_order(self, name, client_id):
        return await self.order_status(name, client_id)
    async def order_status(self, name, client_id):
        return {"status": "FILLED", "filledSize": ".99"}
    async def accounting(self, name, start, end):
        return {"price_pnl": "0", "funding": "0", "fees": "0", "net_pnl": "0", "filled_size": "1.98"}
    async def set_leverage(self, market): pass

    async def order(self, name, side, quantity, reduce_only=False, client_id=None):
        self.orders.append((side, quantity, reduce_only))
        if self.fail: raise RuntimeError("Exchange unavailable")
        if reduce_only:
            self.rows.pop(name, None)
        else:
            self.rows[name] = position(quantity * side * (D(".5") if self.partial else D(1)))
        return {"status": "FILLED", "filledSize": str(quantity * (D(".5") if self.partial and not reduce_only else D(1)))}


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        a, e = market_info()
        self.a, self.e = FakeExchange("Arcus", a), FakeExchange("Lighter", e)
        self.farmer = Farmer(self.a, self.e, Path(self.temp.name) / "state.json")
        self.a_info, self.e_info = a, e

    def tearDown(self): self.temp.cleanup()

    async def open(self):
        await self.farmer.open_pair("NVDA-USD", self.a_info, self.e_info, 1, D(".99"))

    async def test_open_and_restart_reconcile_neutral_positions(self):
        await self.open()
        self.assertEqual(self.a.rows["NVDA-USD"]["size"], -self.e.rows["NVDA-USD"]["size"])
        restarted = Farmer(self.a, self.e, self.farmer.path)
        await restarted.monitor()
        self.assertEqual(restarted.state["active"]["NVDA-USD"]["phase"], "open")

    async def test_order_audit_records_reasons_and_unknown_costs(self):
        await self.open()
        with patch("main.asyncio.sleep", new=AsyncMock()):
            await self.farmer.close_pair("NVDA-USD", "funding inversé")
        records = [json.loads(line) for line in self.farmer.path.with_suffix(".orders.jsonl").read_text(encoding="utf8").splitlines()]
        requests = [row for row in records if row["event"] == "order_requested"]
        self.assertEqual(len(requests), 4)
        self.assertTrue(all(row["reason"] for row in requests))
        snapshots = [row for row in records if row["event"] == "position_snapshot"]
        self.assertEqual(len(snapshots), 2)
        self.assertTrue(all(row["funding"] is None and row["fees"] is None for row in snapshots))

    async def test_partial_fill_unwinds_both_legs(self):
        self.e.partial = True
        with patch("main.asyncio.sleep", new=AsyncMock()):
            with self.assertRaisesRegex(RuntimeError, "non neutres"):
                await self.open()
        self.assertFalse(self.a.rows)
        self.assertFalse(self.e.rows)
        self.assertFalse(self.farmer.state["active"])
        self.assertFalse(self.a.orders)
        self.assertTrue(self.e.orders[-1][2])

    async def test_failed_leg_unwinds_successful_leg(self):
        self.e.fail = True
        with patch("main.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(RuntimeError): await self.open()
        self.assertFalse(self.a.rows)
        self.assertFalse(self.farmer.state["active"])
        self.assertFalse(self.a.orders)

    async def test_liquidation_risk_closes_both(self):
        await self.open()
        self.a.rows["NVDA-USD"]["mark"] = D("95.4")
        with patch("main.asyncio.sleep", new=AsyncMock()): await self.farmer.monitor()
        self.assertFalse(self.a.rows)
        self.assertFalse(self.e.rows)

    async def test_missing_leg_closes_remaining_leg(self):
        await self.open()
        self.e.rows.clear()
        with patch("main.asyncio.sleep", new=AsyncMock()): await self.farmer.monitor()
        self.assertFalse(self.a.rows)

    async def test_funding_inversion_closes_pair(self):
        await self.open()
        self.e.info["fundingRate"] = "0"
        with patch("main.asyncio.sleep", new=AsyncMock()): await self.farmer.scan()
        self.assertFalse(self.farmer.state["active"])
        self.assertFalse(self.a.rows)
        self.assertFalse(self.e.rows)

    async def test_max_pairs_prevents_new_entries(self):
        await self.open()
        now = 14400
        self.farmer.state["history"]["NVDA-USD"] = [[0, ".00001", ".00002"], [14370, ".00001", ".00002"]]
        with patch("main.MAX_PAIRS", 1), patch("main.time.time", return_value=now):
            await self.farmer.scan()
        self.assertEqual(len(self.a.orders), 1)

    async def test_interrupted_opening_is_closed_on_restart(self):
        await self.open()
        self.farmer.state["active"]["NVDA-USD"]["phase"] = "opening"
        self.farmer.save()
        restarted = Farmer(self.a, self.e, self.farmer.path)
        with patch("main.asyncio.sleep", new=AsyncMock()): await restarted.monitor()
        self.assertFalse(restarted.state["active"])

    async def test_failed_close_keeps_journal_and_closes_other_leg(self):
        await self.open()
        self.a.fail = True
        with patch("main.asyncio.sleep", new=AsyncMock()):
            with self.assertRaisesRegex(RuntimeError, "incomplete"):
                await self.farmer.close_pair("NVDA-USD", "test")
        self.assertFalse(self.e.rows)
        self.assertEqual(self.farmer.state["active"]["NVDA-USD"]["phase"], "closing")

    async def test_external_positions_and_wrong_owner_rejected(self):
        self.a.rows["BTC-USD"] = position("1")
        with self.assertRaisesRegex(RuntimeError, "externes"): await self.farmer.monitor()
        self.farmer.save()
        self.e.identity = "another-account"
        with self.assertRaises(ValueError): Farmer(self.a, self.e, self.farmer.path)

    async def test_uncertain_placement_keeps_journal_until_terminal_status(self):
        self.e.order = AsyncMock(side_effect=UncertainOrder("NVDA-USD", "late-id"))
        self.e.order_status = AsyncMock(return_value=None)
        with patch("main.asyncio.sleep", new=AsyncMock()):
            with self.assertRaisesRegex(RuntimeError, "incertain"):
                await self.open()
        self.assertFalse(self.farmer.state["active"]["NVDA-USD"]["orders"][0]["terminal"])
        self.assertFalse(self.a.rows)
        # Le fill arrive après la première tentative de fermeture.
        self.e.rows["NVDA-USD"] = position("-.99")
        self.e.order_status.return_value = {"status": "FILLED", "filledSize": ".99"}
        self.e.order = FakeExchange.order.__get__(self.e)
        with patch("main.asyncio.sleep", new=AsyncMock()):
            await self.farmer.monitor()
        self.assertFalse(self.e.rows)
        self.assertFalse(self.farmer.state["active"])

    async def test_best_stable_gap_selected_with_one_slot(self):
        a2, e2 = deepcopy(self.a_info), deepcopy(self.e_info)
        a2["marketDisplayName"], a2["marketId"] = "AMD-USD", 29
        e2["name"], e2["symbol"] = "AMD-USD", "AMD"
        e2["fundingRate"] = "0.00005"
        self.a.markets = AsyncMock(return_value=[self.a_info, a2])
        self.e.markets = AsyncMock(return_value=[self.e_info, e2])
        self.farmer.state["history"] = {
            "NVDA-USD": [[0, ".00001", ".00002"], [14370, ".00001", ".00002"]],
            "AMD-USD": [[0, ".00001", ".00005"], [14370, ".00001", ".00005"]]}
        with patch("main.MAX_PAIRS", 1), patch("main.time.time", return_value=14400):
            await self.farmer.scan()
        self.assertEqual(set(self.farmer.state["active"]), {"AMD-USD"})


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_arcus_fok_signature_and_terminal_reconciliation(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        adapter = Arcus.__new__(Arcus)
        adapter.address, adapter.account = "0x" + "1" * 40, 0
        adapter.base, adapter.key = "https://example.invalid", Ed25519PrivateKey.generate()
        adapter.api_key = adapter.key.public_key().public_bytes_raw().hex()
        adapter.info = {"NVDA-USD": market_info()[0]}
        adapter.markets = AsyncMock(return_value=[market_info()[0]])
        adapter.positions = AsyncMock(side_effect=[{}, {"NVDA-USD": position(".99")}])
        adapter.book = AsyncMock(return_value=book())
        posted = {}
        class MockHttp:
            async def call(self, method, url, body, headers):
                posted.update(body)
                ts = int(headers["X-Timestamp"])
                typed = {"ad": adapter.address, "ai": 0, "c": body["clientId"], "ct": ts,
                         "g": int(body["goodTilTime"]) * 1000, "m": 28, "op": 1,
                         "p": int(D(body["price"]) / D(".01")), "q": 990, "r": 0, "s": 0, "t": 1, "v": 1}
                adapter.key.public_key().verify(bytes.fromhex(headers["X-Signature"]), canonical(typed).encode())
                return {"status": "ACK"}
        adapter.http = MockHttp()
        async def status(*args): return {"clientId": posted["clientId"], "status": "FILLED", "filledSize": ".99"}
        adapter.order_status = status
        result = await adapter.order("NVDA-USD", 1, D(".99"))
        self.assertEqual(result["status"], "FILLED")
        self.assertEqual(posted["timeInForce"], "FOK")
        self.assertFalse(posted["reduceOnly"])
        self.assertEqual(adapter.positions.call_count, 2)

    async def test_arcus_ack_leverage_and_isolated_mode(self):
        adapter = Arcus.__new__(Arcus)
        adapter.address, adapter.account = "0x" + "1" * 40, 0
        adapter.post = AsyncMock(return_value={"status": "ACK"})
        adapter.get = AsyncMock(return_value={"leverages": [{"marketId": 28, "leverage": 20, "marginMode": "ISOLATED"}]})
        await adapter.set_leverage(market_info()[0])
        self.assertTrue(adapter.post.call_args.args[1]["isolated"])

    async def test_arcus_liquidation_calculation_and_list_positions(self):
        adapter = Arcus.__new__(Arcus)
        adapter.address, adapter.account = "0x" + "1" * 40, 0
        adapter.info = {"NVDA-USD": market_info()[0]}
        adapter.markets = AsyncMock(return_value=[market_info()[0]])
        rows = [{"marketDisplayName": "NVDA-USD", "size": "1", "marginMode": "ISOLATED",
                 "marginUsed": "5", "borrowedCapital": "-100", "averageEntryPrice": "100", "markPx": "100"}]
        adapter.get = AsyncMock(return_value={"positions": rows})
        result = (await adapter.positions())["NVDA-USD"]
        self.assertEqual(result["liquidation"], D(95) / D(".97"))
        rows[0]["size"], rows[0]["borrowedCapital"] = "-1", "100"
        result = (await adapter.positions())["NVDA-USD"]
        self.assertEqual(result["liquidation"], D(105) / D("1.03"))

    async def test_wait_for_position_read_model_after_fill(self):
        exchange = FakeExchange("Arcus", {})
        exchange.positions = AsyncMock(side_effect=[{}, {"NVDA-USD": position(".99")}])
        with patch("exchanges.asyncio.sleep", new=AsyncMock()):
            await wait_position(exchange, "NVDA-USD", D(".99"))
        self.assertEqual(exchange.positions.call_count, 2)





class LighterAdapterTests(unittest.IsolatedAsyncioTestCase):
    def adapter(self):
        from types import SimpleNamespace
        adapter = Lighter.__new__(Lighter)
        adapter.info = {"NVDA-USD": market_info()[1]}
        adapter.account, adapter.key_index = 1, 4
        adapter.stats = AsyncMock(return_value={"14": {"mark_price": "100", "index_price": "99"}})
        adapter.lock = asyncio.Lock()
        adapter.signer = SimpleNamespace(ORDER_TYPE_MARKET=1, ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL=0,
                                        DEFAULT_IOC_EXPIRY=0, ISOLATED_MARGIN_MODE=1,
                                        create_order=AsyncMock(return_value=(None, SimpleNamespace(code=200), None)),
                                        cancel_order=AsyncMock(return_value=(None, SimpleNamespace(code=200), None)),
                                        update_leverage=AsyncMock(return_value=(None, SimpleNamespace(code=200), None)))
        return adapter

    async def test_mainnet_url_and_reserved_keys_rejected(self):
        env = {"LIGHTER_BASE_URL": "https://api.rh-testnet.lighter.xyz", "LIGHTER_ACCOUNT_INDEX": "1",
               "LIGHTER_API_KEY_INDEX": "4", "LIGHTER_API_PRIVATE_KEY": "unused"}
        with patch.dict("os.environ", env):
            with self.assertRaises(ValueError): Lighter(None)
        env["LIGHTER_BASE_URL"] = Lighter.MAINNET
        for key in ("0", "3", "157", "255"):
            env["LIGHTER_API_KEY_INDEX"] = key
            with patch.dict("os.environ", env):
                with self.assertRaises(ValueError): Lighter(None)

    async def test_market_normalization_and_hourly_percentage(self):
        adapter = self.adapter()
        fixture = json.loads((Path(__file__).parent / "tests/fixtures/lighter-nvda.json").read_text())
        adapter.get = AsyncMock(return_value={"order_book_details": [fixture]})
        adapter.stats = AsyncMock(return_value={str(fixture["market_id"]): {
            "current_funding_rate": "0.0012", "mark_price": "100", "index_price": "99"}})
        rows = await adapter.markets()
        self.assertEqual(rows[0]["fundingRate"], "0.000012")
        self.assertEqual(rows[0]["name"], "NVDA-USD")
        self.assertEqual(D(rows[0]["stepSize"]), D(10) ** -fixture["supported_size_decimals"])
        self.assertEqual(rows[0]["oraclePrice"], "99")

    async def test_market_ioc_quantization_and_position_confirmation(self):
        adapter = self.adapter()
        adapter.positions = AsyncMock(side_effect=[{}, {"NVDA-USD": position("-.99")}])
        adapter.book = AsyncMock(return_value=book())
        adapter.confirm = AsyncMock(return_value={"status": "CANCELED", "filledSize": ".99"})
        result = await adapter.order("NVDA-USD", -1, D(".99"), client_id="123")
        sent = adapter.signer.create_order.call_args.kwargs
        self.assertEqual(sent["client_order_index"], 123)
        self.assertNotIn("api_key_index", sent)  # Sinon le SDK attend un nonce explicite.
        self.assertNotIn("nonce", sent)
        self.assertEqual(sent["base_amount"], 99)
        self.assertEqual(sent["price"], 9994)
        self.assertTrue(sent["is_ask"])
        self.assertEqual(sent["time_in_force"], 0)
        self.assertEqual(sent["order_type"], 1)
        self.assertEqual(result["status"], "CANCELED")
        self.assertEqual(adapter.positions.call_count, 2)

    async def test_wide_spread_rejects_entry_on_both_adapters(self):
        wide = {"bids": [(D("99"), D(10))], "asks": [(D("101"), D(10))]}
        for adapter in (self.adapter(), Arcus.__new__(Arcus)):
            adapter.info = {"NVDA-USD": market_info()[0 if isinstance(adapter, Arcus) else 1]}
            adapter.markets = AsyncMock(return_value=[market_info()[0]])
            adapter.positions = AsyncMock(return_value={})
            adapter.book = AsyncMock(return_value=wide)
            with self.assertRaisesRegex(ValueError, "Spread"):
                await adapter.order("NVDA-USD", 1 if isinstance(adapter, Arcus) else -1, D(".99"))
            if isinstance(adapter, Lighter):
                adapter.signer.create_order.assert_not_called()

    async def test_reduce_only_ignores_wide_spread(self):
        adapter = self.adapter()
        adapter.positions = AsyncMock(side_effect=[{"NVDA-USD": position("-.99")}, {}])
        adapter.book = AsyncMock(return_value={"bids": [(D("99"), D(10))], "asks": [(D("101"), D(10))]})
        adapter.confirm = AsyncMock(return_value={"status": "FILLED", "filledSize": ".99"})
        await adapter.order("NVDA-USD", 1, D(".99"), reduce_only=True)
        self.assertTrue(adapter.signer.create_order.call_args.kwargs["reduce_only"])
        adapter.stats.assert_not_called()

    async def test_send_timeout_reconciles_without_resubmitting(self):
        adapter = self.adapter()
        adapter.signer.create_order.side_effect = TimeoutError()
        adapter.stats.return_value = {"14": {"mark_price": "100", "index_price": "101"}}
        adapter.positions = AsyncMock(side_effect=[{}, {"NVDA-USD": position(".99")}])
        adapter.book = AsyncMock(return_value=book())
        adapter.confirm = AsyncMock(return_value={"status": "FILLED", "filledSize": ".99"})
        await adapter.order("NVDA-USD", 1, D(".99"), client_id="123")
        self.assertEqual(adapter.signer.create_order.call_count, 1)

    async def test_cancel_by_client_id_when_order_not_yet_visible(self):
        adapter = self.adapter()
        adapter.order_status = AsyncMock(return_value=None)
        adapter.confirm = AsyncMock(return_value={"status": "CANCELED", "filledSize": "0"})
        await adapter.cancel_order("NVDA-USD", "123")
        self.assertEqual(adapter.signer.cancel_order.call_args.kwargs["order_index"], 123)

    async def test_inactive_pagination_and_canceled_variant(self):
        adapter = self.adapter()
        adapter.get = AsyncMock(side_effect=[{"orders": []},
            {"orders": [], "next_cursor": "page2"},
            {"orders": [{"client_order_index": 123, "status": "canceled-too-much-slippage",
                          "filled_base_amount": ".25"}]}])
        row = await adapter.order_status("NVDA-USD", "123")
        self.assertEqual(row["status"], "CANCELED")
        self.assertEqual(row["filledSize"], ".25")
        self.assertEqual(adapter.get.call_args.kwargs["cursor"], "page2")

    async def test_positions_use_live_mark_and_signed_quantity(self):
        adapter = self.adapter()
        adapter.account_rows = AsyncMock(return_value=[{"symbol": "NVDA", "market_id": 14,
            "position": ".99", "sign": -1, "avg_entry_price": "100", "liquidation_price": "105",
            "total_funding_paid_out": "-2"}])
        adapter.get = AsyncMock(return_value={"order_book_details": [{"market_id": 14, "mark_price": "101"}]})
        row = (await adapter.positions())["NVDA-USD"]
        self.assertEqual(row["size"], D("-.99"))
        self.assertEqual(row["mark"], D("101"))
        self.assertEqual(row["funding"], D("2"))

    async def test_isolated_leverage_is_confirmed_from_account(self):
        adapter = self.adapter()
        adapter.account_rows = AsyncMock(return_value=[{"market_id": 14, "margin_mode": 1,
                                                       "initial_margin_fraction": "5.00"}])
        await adapter.set_leverage(market_info()[1])
        self.assertEqual(adapter.signer.update_leverage.call_args.kwargs["leverage"], 20)
        self.assertEqual(adapter.signer.update_leverage.call_args.kwargs["margin_mode"], 1)

    async def test_accounting_fees_funding_and_price_pnl(self):
        adapter = self.adapter()
        async def pages(path, key, **params):
            if path == "trades":
                yield [{"timestamp": 11000, "ask_account_id": 1, "is_maker_ask": False,
                        "ask_account_pnl": "3", "usd_amount": "100", "taker_fee": 315, "size": "1"}]
            else:
                yield [{"market_id": 14, "change": "2"}]
        adapter.pages = pages
        report = await adapter.accounting("NVDA-USD", 10, 12)
        self.assertEqual(D(report["fees"]), D(".0315"))
        self.assertEqual(D(report["net_pnl"]), D("4.9685"))

    async def test_sdk_manages_nonce_when_sending_adapter_order(self):
        from types import SimpleNamespace
        import lighter
        adapter = self.adapter()
        client = lighter.SignerClient(url=Lighter.MAINNET, account_index=1,
                                      api_private_keys={4: "1" * 80}, chain_id=Lighter.CHAIN_ID)
        adapter.signer = client
        client.nonce_manager._fetch_nonce = AsyncMock(return_value=7)
        client.send_tx = AsyncMock(return_value=SimpleNamespace(code=200))
        adapter.positions = AsyncMock(side_effect=[{}, {"NVDA-USD": position("-.99")}])
        adapter.book = AsyncMock(return_value=book())
        adapter.confirm = AsyncMock(return_value={"status": "FILLED", "filledSize": ".99"})
        try:
            await adapter.order("NVDA-USD", -1, D(".99"), client_id="123")
            signed = json.loads(client.send_tx.call_args.kwargs["tx_info"])
            self.assertEqual(signed["Nonce"], 7)
            self.assertEqual(signed["ApiKeyIndex"], 4)
        finally:
            await client.close()

    async def test_sdk_signs_rh_market_ioc_offline(self):
        import lighter
        client = lighter.SignerClient(url=Lighter.MAINNET, account_index=1,
                                      api_private_keys={4: "1" * 80}, chain_id=Lighter.CHAIN_ID)
        try:
            tx_type, body, _, error = client.sign_create_order(market_index=14, client_order_index=123,
                base_amount=99, price=10005, is_ask=0, order_type=client.ORDER_TYPE_MARKET,
                time_in_force=client.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL,
                reduce_only=True, order_expiry=client.DEFAULT_IOC_EXPIRY, nonce=1, api_key_index=4)
            self.assertIsNone(error)
            signed = json.loads(body)
            self.assertEqual(signed["MarketIndex"], 14)
            self.assertEqual(signed["BaseAmount"], 99)
            self.assertEqual(signed["TimeInForce"], 0)
            self.assertEqual(signed["ReduceOnly"], 1)
            self.assertTrue(signed["Sig"])
            self.assertEqual(client.chain_id, 466324)
        finally:
            await client.close()


class EntrySafetyTests(unittest.IsolatedAsyncioTestCase):
    setUp = LifecycleTests.setUp
    tearDown = LifecycleTests.tearDown
    open = LifecycleTests.open

    async def test_scan_can_open_on_first_observation(self):
        with patch("main.time.time", return_value=100):
            await self.farmer.scan()
        self.assertEqual(self.farmer.state["active"]["NVDA-USD"]["phase"], "open")
        self.assertEqual(len(self.a.orders), 1)
        self.assertEqual(len(self.e.orders), 1)

    async def test_fresh_oracle_rejects_before_any_order(self):
        self.a.info["oraclePrice"] = "99"
        with self.assertRaisesRegex(ValueError, "oracle"):
            await self.open()
        self.assertFalse(self.a.orders)
        self.assertFalse(self.e.orders)
        self.assertFalse(self.farmer.state["active"])

    async def test_arcus_failure_unwinds_lighter(self):
        self.a.fail = True
        with patch("main.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(RuntimeError): await self.open()
        self.assertFalse(self.e.rows)
        self.assertFalse(self.farmer.state["active"])
        self.assertTrue(self.e.orders[-1][2])

    async def test_lighter_preflight_down_does_not_send_arcus(self):
        self.e.set_leverage = AsyncMock(side_effect=RuntimeError("unavailable"))
        with self.assertRaises(RuntimeError): await self.open()
        self.assertFalse(self.a.orders)
        self.assertFalse(self.e.orders)

    async def test_unknown_close_order_keeps_journal(self):
        await self.open()
        self.a.order = AsyncMock(side_effect=UncertainOrder("NVDA-USD", "close-id"))
        with patch("main.asyncio.sleep", new=AsyncMock()):
            with self.assertRaisesRegex(RuntimeError, "incertain"):
                await self.farmer.close_pair("NVDA-USD", "test")
        self.assertFalse(self.e.rows)
        refs = self.farmer.state["active"]["NVDA-USD"]["orders"]
        self.assertTrue(any(r["client_id"] == "close-id" and not r["terminal"] for r in refs))

    async def test_accounting_failure_does_not_block_flattening(self):
        await self.open()
        self.e.accounting = AsyncMock(side_effect=RuntimeError("unavailable"))
        with patch("main.asyncio.sleep", new=AsyncMock()):
            await self.farmer.close_pair("NVDA-USD", "test")
        self.assertFalse(self.farmer.state["active"])
        report = self.farmer.state["closed"][-1]
        self.assertIsNone(report["net_pnl"])
        self.assertIsNone(report["pnl"]["Lighter"]["fees"])



class ArcusAccountingTests(unittest.IsolatedAsyncioTestCase):
    async def test_closed_pnl_fees_are_not_deducted_twice(self):
        adapter = Arcus.__new__(Arcus)
        adapter.address, adapter.account = "0x" + "1" * 40, 0
        adapter.get = AsyncMock(side_effect=[
            {"fills": [{"closedPnl": "-.1", "fee": ".1", "size": "1"},
                       {"closedPnl": "2.9", "fee": ".1", "size": "1"}]},
            {"fundingPayments": [{"payment": "1"}]}])
        report = await adapter.accounting("NVDA-USD", 10, 20)
        self.assertEqual(D(report["price_pnl"]), D("3"))
        self.assertEqual(D(report["fees"]), D(".2"))
        self.assertEqual(D(report["net_pnl"]), D("3.8"))
        self.assertEqual(adapter.get.call_args_list[0].kwargs["from"], 10000000)

    async def test_cancel_uses_typed_signature_and_terminal_confirmation(self):
        adapter = Arcus.__new__(Arcus)
        adapter.address, adapter.account = "0x" + "1" * 40, 0
        adapter.info = {"NVDA-USD": market_info()[0]}
        adapter.post = AsyncMock(return_value={"status": "CANCEL_ACKNOWLEDGED"})
        adapter.order_status = AsyncMock(side_effect=[{"status": "OPEN"},
                                                       {"status": "CANCELED", "filledSize": "0"}])
        row = await adapter.cancel_order("NVDA-USD", "fund123")
        args = adapter.post.call_args.args
        self.assertEqual(args[0], "cancelOrder")
        self.assertEqual(args[1]["clientId"], "fund123")
        self.assertEqual(args[3]["op"], 2)
        self.assertEqual(args[3]["ct"], args[2])
        self.assertEqual(row["status"], "CANCELED")

    async def test_missing_price_pnl_is_not_reported_as_zero(self):
        adapter = Arcus.__new__(Arcus)
        adapter.address, adapter.account = "0x" + "1" * 40, 0
        adapter.get = AsyncMock(side_effect=[{"fills": [{"fee": ".1", "size": "1"}]},
                                            {"fundingPayments": []}])
        with self.assertRaises(RuntimeError):
            await adapter.accounting("NVDA-USD", 10, 20)


if __name__ == "__main__": unittest.main()
