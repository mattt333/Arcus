"""REST Arcus et Lighter. Aucun renvoi automatique d'un placement incertain."""
import asyncio
import json
import math
import os
import time
import uuid
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

import aiohttp
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

D = Decimal
TIMEOUT = 10
ORDER_TIMEOUT = 20
MAX_SPREAD = min(D(os.getenv("MAX_SPREAD", "0.0003")), D("0.0003"))  # 0,03 % maximum
OPEN_SLIPPAGE = D("0.0005")
CLOSE_SLIPPAGE = D("0.02")
TERMINAL = {"FILLED", "CANCELED", "CANCELLED", "REJECTED", "EXPIRED", "MARGIN_CANCELED"}


class UncertainOrder(TimeoutError):
    def __init__(self, market, client_id):
        self.market, self.client_id = market, client_id
        super().__init__(f"Ordre {market} {client_id} non confirmé ; aucun renvoi automatique")

def check_book_spread(book):
    if not book["bids"] or not book["asks"]:
        raise ValueError("Carnet vide")
    bid, ask = book["bids"][0][0], book["asks"][0][0]
    if (not bid.is_finite() or not ask.is_finite() or bid <= 0 or ask < bid
            or (ask - bid) / ((ask + bid) / 2) > MAX_SPREAD):
        raise ValueError("Spread bid/ask trop large : maximum 0,03 %")


def canonical(body):
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def snap(price, tick, side):
    return (price / tick).to_integral_value(rounding=ROUND_CEILING if side == 1 else ROUND_FLOOR) * tick


def positions_map(rows, key):
    if isinstance(rows, dict):
        rows = rows.values()
    return {row[key]: row for row in rows if D(row["size"]) != 0}


async def wait_position(exchange, name, expected):
    """Attendre que le read-model reflète le fill, avant de continuer."""
    deadline = time.monotonic() + ORDER_TIMEOUT
    while time.monotonic() < deadline:
        row = (await exchange.positions()).get(name)
        actual = row["size"] if row else D(0)
        if actual == expected:
            return
        await asyncio.sleep(0.25)
    raise TimeoutError(f"Position {exchange.name} {name} non réconciliée après fill")


class Http:
    async def __aenter__(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT),
                                             headers={"User-Agent": "arcus-lighter-funding/1.0"})
        return self

    async def __aexit__(self, *args):
        await self.session.close()

    async def call(self, method, url, body=None, headers=None, params=None):
        async with self.session.request(method, url, json=body, headers=headers, params=params) as response:
            data = await response.json(content_type=None)
            if response.status >= 400 or data.get("status") == "ERROR" or data.get("error") or data.get("code", 200) != 200:
                raise RuntimeError(f"{method} {url}: HTTP {response.status}, code {data.get('code')}")
            return data


class Arcus:
    name = "Arcus"

    def __init__(self, http):
        self.http = http
        self.base = os.getenv("ARCUS_BASE_URL", "https://api.arcus.xyz").rstrip("/")
        if self.base != "https://api.arcus.xyz":
            raise ValueError("Seul le mainnet Arcus https://api.arcus.xyz est autorisé")
        self.address = os.environ["ARCUS_ADDRESS"].lower()
        self.account = int(os.getenv("ARCUS_ACCOUNT_INDEX", "0"))
        self.key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(os.environ["ARCUS_SIGNING_KEY"].removeprefix("0x")))
        self.api_key = self.key.public_key().public_bytes_raw().hex()
        self.info = {}
        self.identity = f"{self.base}/{self.address}/{self.account}"

    def params(self, **extra):
        return {"address": self.address, "accountIndex": self.account, **extra}

    async def get(self, path, **params):
        return await self.http.call("GET", self.base + "/v1/" + path, params=params)

    async def post(self, method, body, ts=None, typed=None):
        ts = ts or time.time_ns()
        message = canonical(typed) if typed is not None else str(ts) + method + canonical(body)
        headers = {"X-API-Key": self.api_key, "X-Timestamp": str(ts),
                   "X-Signature": self.key.sign(message.encode()).hex()}
        return await self.http.call("POST", self.base + "/v1/" + method, body, headers)

    async def markets(self):
        rows = (await self.get("markets"))["markets"]
        self.info = {row["marketDisplayName"]: row for row in rows}
        return rows

    async def book(self, name):
        row = await self.get(f"l2OrderBook/{name}", nLevels=100)
        return {side: [(D(p), D(q)) for p, q in row[side]] for side in ("bids", "asks")}

    async def funding_history(self, name, start, end):
        data = await self.get("fundingRates", market=name, limit=1000,
                              **{"from": int(start * 1_000_000), "to": int(end * 1_000_000)})
        rows = data["fundingRates"]
        if len(rows) >= 1000:
            raise ValueError("Historique de funding Arcus tronque")
        return [(D(str(row["time"])) / 1_000_000, D(row["fundingRate"])) for row in rows]

    async def set_leverage(self, market):
        leverage = int(D(1) / D(market["initialMarginFraction"]))
        await self.post("setLeverage", self.params(marketId=market["marketId"], leverage=leverage, isolated=True))
        deadline = time.monotonic() + ORDER_TIMEOUT
        while time.monotonic() < deadline:
            rows = (await self.get("leverages", **self.params()))["leverages"]
            if any(row["marketId"] == market["marketId"] and D(str(row["leverage"])) == leverage
                   and row["marginMode"] == "ISOLATED" for row in rows):
                return
            await asyncio.sleep(0.25)
        raise TimeoutError("Levier et marge isolée Arcus non confirmés")

    async def positions(self):
        rows = positions_map((await self.get("positions", **self.params()))["positions"], "marketDisplayName")
        result = {}
        for name, row in rows.items():
            quantity = D(row["size"])
            liquidation = None
            if row["marginMode"] == "ISOLATED" and name in self.info:
                # Arcus n'expose pas liquidationPrice : equity(price) =
                # (marginUsed + borrowedCapital) + quantité_signée * price.
                cash = D(row["marginUsed"]) + D(row["borrowedCapital"])
                maintenance = D(self.info[name]["maintenanceMarginFraction"])
                liquidation = -cash / (quantity - abs(quantity) * maintenance)
                if liquidation <= 0:
                    liquidation = D(0) if quantity > 0 else None
            result[name] = {"size": quantity, "entry": D(row["averageEntryPrice"]),
                            "mark": D(row["markPx"]), "liquidation": liquidation}
        return result

    async def check_orders(self):
        if (await self.get("openOrders", **self.params()))["orders"]:
            raise RuntimeError("Ordres Arcus existants : sous-compte dédié sans ordres ouverts requis")

    async def order_status(self, name, client_id):
        rows = (await self.get("orders", **self.params(market=name, limit=100)))["orders"]
        return next((row for row in rows if row.get("clientId") == client_id), None)

    async def cancel_order(self, name, client_id):
        row = await self.order_status(name, client_id)
        if row and row["status"] in TERMINAL:
            return row
        ts = time.time_ns()
        market_id = self.info[name]["marketId"]
        typed = {"ad": self.address, "ai": self.account, "c": client_id,
                 "ct": ts, "m": market_id, "op": 2, "v": 1}
        await self.post("cancelOrder", self.params(marketId=market_id, clientId=client_id, timestamp=ts), ts, typed)
        deadline = time.monotonic() + ORDER_TIMEOUT
        while time.monotonic() < deadline:
            row = await self.order_status(name, client_id)
            if row and row["status"] in TERMINAL:
                return row
            await asyncio.sleep(0.25)
        raise UncertainOrder(name, client_id)

    async def accounting(self, name, start, end):
        async def history(path, key):
            rows = (await self.get(path, **self.params(market=name, limit=1000),
                                   **{"from": int(start * 1_000_000), "to": int(end * 1_000_000)}))[key]
            if len(rows) >= 1000:
                raise RuntimeError("Historique Arcus tronque ; PnL non confirme")
            return rows
        fills, fundings = await asyncio.gather(history("fills", "fills"), history("fundingPayments", "fundingPayments"))
        fees = sum((D(row["fee"]) for row in fills), D(0))
        if any(row.get("liquidation") or row.get("closedPnl") is None for row in fills):
            raise RuntimeError("PnL Arcus non confirme pour les fills forces/anciens")
        net_trading = sum((D(row["closedPnl"]) for row in fills), D(0))
        funding = sum((D(row["payment"]) for row in fundings), D(0))
        return {"price_pnl": str(net_trading + fees), "funding": str(funding), "fees": str(fees),
                "net_pnl": str(net_trading + funding), "fills": len(fills),
                "filled_size": str(sum((D(r["size"]) for r in fills), D(0)))}

    async def order(self, name, side, quantity, reduce_only=False, client_id=None):
        if not reduce_only:
            fresh = next((r for r in await self.markets() if r["marketDisplayName"] == name), None)
            if fresh is None or fresh["status"] != "ONLINE":
                raise ValueError("Marche Arcus indisponible")
        market = self.info[name]
        before = (await self.positions()).get(name)
        before_size = before["size"] if before else D(0)
        book = await self.book(name)
        if not reduce_only:
            check_book_spread(book)
        levels = book["asks" if side == 1 else "bids"]
        if not levels:
            raise ValueError("Carnet Arcus vide")
        slippage = CLOSE_SLIPPAGE if reduce_only else OPEN_SLIPPAGE
        price = levels[0][0] * (1 + slippage * side)
        tick = D(market["tickSize"])
        for tier in market.get("tickTiers", []):
            if "upToPrice" not in tier or price < D(tier["upToPrice"]):
                tick = D(tier["tick"])
                break
        price = snap(price, tick, side)
        ts = time.time_ns()
        expiry = ts // 1000 + 40 * 86400 * 1_000_000
        client_id = client_id or "fund" + uuid.uuid4().hex
        quantum = quantity / D(market["stepSize"])
        if quantum != quantum.to_integral_value():
            raise ValueError("Quantité Arcus non alignée")
        typed = {"ad": self.address, "ai": self.account, "c": client_id, "ct": ts,
                 "g": expiry * 1000, "m": market["marketId"], "op": 1,
                 "p": int(price / D(market["tickSize"])), "q": int(quantum),
                 "r": int(reduce_only), "s": 0 if side == 1 else 1,
                 "t": 2 if reduce_only else 1, "v": 1}
        body = self.params(marketId=market["marketId"], clientId=client_id,
                           orderSide="BUY" if side == 1 else "SELL", orderType="LIMIT",
                           quantity=str(quantity), price=str(price), reduceOnly=reduce_only,
                           timeInForce="IOC" if reduce_only else "FOK", goodTilTime=str(expiry), timestamp=ts)
        try:
            await self.post("placeOrder", body, ts, typed)
        except Exception:
            pass  # Rechercher le statut même après un timeout de transmission.
        deadline = time.monotonic() + ORDER_TIMEOUT
        while time.monotonic() < deadline:
            try:
                row = await self.order_status(name, client_id)
            except Exception as exc:
                raise UncertainOrder(name, client_id) from exc
            if row and row["status"] in TERMINAL:
                filled = D(row["filledSize"]) if "filledSize" in row else D(row["originalSize"]) - D(row["remainingSize"])
                if filled:
                    try:
                        await wait_position(self, name, before_size + side * filled)
                    except Exception as exc:
                        raise UncertainOrder(name, client_id) from exc
                return row
            await asyncio.sleep(0.25)
        raise UncertainOrder(name, client_id)


class Lighter:
    name = "Lighter"
    MAINNET = "https://api.rh.lighter.xyz"
    CHAIN_ID = 466324

    def __init__(self, http):
        self.http = http
        self.base = os.getenv("LIGHTER_BASE_URL", self.MAINNET).rstrip("/")
        if self.base != self.MAINNET:
            raise ValueError("Seul le mainnet Lighter sur Robinhood Chain est autorisé")
        self.account = int(os.environ["LIGHTER_ACCOUNT_INDEX"])
        self.key_index = int(os.environ["LIGHTER_API_KEY_INDEX"])
        if self.account < 0 or not (4 <= self.key_index <= 254 and self.key_index != 157):
            raise ValueError("Index de compte ou de clé API Lighter invalide/réservé")
        self.private_key = os.environ["LIGHTER_API_PRIVATE_KEY"]
        self.signer = None
        self.lock = asyncio.Lock()  # Les nonces sont propres à la clé API.
        self.info = {}
        self.identity = f"{self.base}/{self.CHAIN_ID}/{self.account}"

    def client(self):
        if self.signer is None:
            import lighter
            self.signer = lighter.SignerClient(url=self.base, account_index=self.account,
                                               api_private_keys={self.key_index: self.private_key},
                                               chain_id=self.CHAIN_ID)
        return self.signer

    async def close(self):
        if self.signer is not None:
            await self.signer.close()

    async def get(self, path, private=False, **params):
        headers = None
        if private:
            token, error = self.client().create_auth_token_with_expiry(api_key_index=self.key_index)
            if error:
                raise RuntimeError("Authentification Lighter impossible")
            headers = {"Authorization": token}
        return await self.http.call("GET", self.base + "/api/v1/" + path,
                                    headers=headers, params=params)

    async def stats(self):
        # Un snapshot neuf à chaque scan : aucun funding périmé après reconnexion.
        async with asyncio.timeout(TIMEOUT):
            async with self.http.session.ws_connect(self.base.replace("https://", "wss://") + "/stream") as ws:
                await ws.send_json({"type": "subscribe", "channel": "market_stats/all"})
                while True:
                    message = await ws.receive_json()
                    if message.get("type") == "ping":
                        await ws.send_json({"type": "pong"})
                    if "market_stats" in message:
                        return message["market_stats"]
                    if message.get("type") == "error":
                        raise RuntimeError("Flux market_stats Lighter indisponible")

    async def markets(self):
        details, stats = await asyncio.gather(self.get("orderBookDetails", filter="perp"), self.stats())
        result = []
        for row in details["order_book_details"]:
            stat = stats.get(str(row["market_id"]))
            config = row.get("market_config", {})
            if (not stat or row["market_type"] != "perp" or row["status"] != "active"
                    or row.get("is_frozen") or config.get("force_reduce_only")):
                continue
            # current_funding_rate est un pourcentage horaire, pas une fraction.
            market = {**row, "name": row["symbol"] + "-USD",
                      "stepSize": str(D(10) ** -row["supported_size_decimals"]),
                      "tickSize": str(D(10) ** -row["supported_price_decimals"]),
                      "minOrderSize": row["min_base_amount"],
                      "minOrderNotional": row["min_quote_amount"],
                      "maxOrderNotional": row["order_quote_limit"],
                      "fundingRate": str(D(stat["current_funding_rate"]) / 100),
                      "markPrice": stat["mark_price"], "oraclePrice": stat["index_price"]}
            result.append(market)
        self.info.update({row["name"]: row for row in result})
        return result

    async def book(self, name):
        row = await self.get("orderBookOrders", market_id=self.info[name]["market_id"], limit=250)
        return {side: sorted([(D(r["price"]), D(r["remaining_base_amount"])) for r in row[side]],
                             reverse=side == "bids") for side in ("bids", "asks")}

    async def funding_history(self, name, start, end):
        data = await self.get("fundings", market_id=self.info[name]["market_id"], resolution="1h",
                              start_timestamp=int(start), end_timestamp=int(end),
                              count_back=math.ceil((end - start) / 3600) + 2)
        result = []
        for row in data["fundings"]:
            if row["direction"] not in ("long", "short"):
                raise ValueError("Sens du funding Lighter inconnu")
            rate = abs(D(row["rate"])) / 100
            result.append((D(str(row["timestamp"])), rate if row["direction"] == "long" else -rate))
        return result

    async def account_rows(self):
        result, cursor = [], None
        while True:
            data = await self.get("account", by="index", value=str(self.account),
                                  **({"cursor": cursor} if cursor else {}))
            account = next((r for r in data["accounts"] if int(r["index"]) == self.account), None)
            if account is None:
                raise RuntimeError("Compte Lighter introuvable")
            result.extend(account["positions"])
            next_cursor = data.get("next_cursor")
            if not next_cursor:
                return result
            if next_cursor == cursor:
                raise RuntimeError("Pagination du compte Lighter bloquée")
            cursor = next_cursor

    async def set_leverage(self, market):
        client = self.client()
        leverage = int(D(10000) / D(market["min_initial_margin_fraction"]))
        async with self.lock, asyncio.timeout(TIMEOUT):
            _, response, error = await client.update_leverage(market_index=market["market_id"],
                margin_mode=client.ISOLATED_MARGIN_MODE, leverage=leverage)
        if error or not response or response.code != 200:
            raise RuntimeError("Configuration de marge Lighter refusée")
        deadline = time.monotonic() + ORDER_TIMEOUT
        expected = int(10000 / leverage)
        while time.monotonic() < deadline:
            for row in await self.account_rows():
                # AccountPosition.initial_margin_fraction est exprimé en %.
                if (row["market_id"] == market["market_id"] and row["margin_mode"] == client.ISOLATED_MARGIN_MODE
                        and D(row["initial_margin_fraction"]) * 100 == expected):
                    return
            await asyncio.sleep(0.25)
        raise TimeoutError("Levier et marge isolée Lighter non confirmés")

    async def positions(self):
        # Rafraîchir le mark de risque, indépendamment de la fréquence des scans.
        rows, details = await asyncio.gather(self.account_rows(), self.get("orderBookDetails", filter="perp"))
        marks = {r["market_id"]: D(r["mark_price"]) for r in details["order_book_details"]}
        result = {}
        for row in rows:
            size = D(row["position"]) * int(row["sign"])
            if size:
                result[row["symbol"] + "-USD"] = {
                    "size": size, "entry": D(row["avg_entry_price"]),
                    "mark": marks.get(row["market_id"], D(0)),
                    "liquidation": D(row["liquidation_price"]) if row.get("liquidation_price") else None,
                    "funding": -D(row["total_funding_paid_out"]) if row.get("total_funding_paid_out") is not None else None}
        return result

    async def check_orders(self):
        if (await self.get("accountActiveOrders", private=True, account_index=self.account))["orders"]:
            raise RuntimeError("Ordres Lighter existants : compte dédié requis")

    async def pages(self, path, key, **params):
        cursor = None
        seen = set()
        while True:
            data = await self.get(path, private=True, account_index=self.account, limit=100,
                                  **params, **({"cursor": cursor} if cursor else {}))
            yield data[key]
            cursor = data.get("next_cursor")
            if not cursor:
                return
            if cursor in seen:
                raise RuntimeError("Pagination Lighter bloquée")
            seen.add(cursor)

    async def order_status(self, name, client_id):
        market_id = self.info[name]["market_id"]
        active = (await self.get("accountActiveOrders", private=True, account_index=self.account,
                                 market_id=market_id))["orders"]
        def normalize(row):
            status = row["status"]
            return {**row, "clientId": str(row["client_order_index"]),
                    "status": "CANCELED" if status.startswith("canceled") else status.upper(),
                    "filledSize": row["filled_base_amount"]}
        for row in active:
            if str(row["client_order_index"]) == str(client_id):
                return normalize(row)
        async for rows in self.pages("accountInactiveOrders", "orders", market_id=market_id):
            for row in rows:
                if str(row["client_order_index"]) == str(client_id):
                    return normalize(row)
        return None

    async def cancel_order(self, name, client_id):
        row = await self.order_status(name, client_id)
        if row and row["status"] in TERMINAL:
            return row
        client = self.client()
        # client_order_index peut être fourni avant que le server order_index existe.
        order_index = int(row["order_index"]) if row else int(client_id)
        async with self.lock, asyncio.timeout(TIMEOUT):
            _, response, error = await client.cancel_order(market_index=self.info[name]["market_id"],
                                      order_index=order_index)
        if error or not response or response.code != 200:
            raise UncertainOrder(name, client_id)
        return await self.confirm(name, client_id)

    async def confirm(self, name, client_id):
        deadline = time.monotonic() + ORDER_TIMEOUT
        while time.monotonic() < deadline:
            row = await self.order_status(name, client_id)
            if row and row["status"] in TERMINAL:
                return row
            await asyncio.sleep(0.25)
        raise UncertainOrder(name, client_id)

    async def order(self, name, side, quantity, reduce_only=False, client_id=None):
        market = self.info[name]
        before = (await self.positions()).get(name)
        before_size = before["size"] if before else D(0)
        book = await self.book(name)
        if not reduce_only:
            check_book_spread(book)
        levels = book["asks" if side == 1 else "bids"]
        if not levels:
            raise ValueError("Carnet Lighter vide")
        price = snap(levels[0][0] * (1 + side * (CLOSE_SLIPPAGE if reduce_only else OPEN_SLIPPAGE)),
                     D(market["tickSize"]), side)
        amount = quantity * (D(10) ** market["supported_size_decimals"])
        if amount != amount.to_integral_value() or quantity <= 0:
            raise ValueError("Quantité Lighter non alignée")
        client_id = client_id or str(uuid.uuid4().int % (2 ** 48 - 1) + 1)
        client = self.client()
        try:
            async with self.lock, asyncio.timeout(TIMEOUT):
                _, response, error = await client.create_order(market_index=market["market_id"],
                    client_order_index=int(client_id), base_amount=int(amount),
                    price=int(price * (D(10) ** market["supported_price_decimals"])), is_ask=side == -1,
                    order_type=client.ORDER_TYPE_MARKET, time_in_force=client.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL,
                    reduce_only=reduce_only, order_expiry=client.DEFAULT_IOC_EXPIRY)
            if error or not response or response.code != 200:
                raise RuntimeError("Ordre Lighter refusé")
        except Exception:
            pass  # Ne jamais renvoyer un placement ; retrouver le statut.
        try:
            row = await self.confirm(name, client_id)
            filled = D(row["filledSize"])
            if filled:
                await wait_position(self, name, before_size + side * filled)
            return row
        except Exception as exc:
            raise UncertainOrder(name, client_id) from exc

    async def accounting(self, name, start, end):
        market_id = self.info[name]["market_id"]
        fills = []
        async for rows in self.pages("trades", "trades", market_id=market_id, sort_by="timestamp"):
            fills.extend(r for r in rows if int(start * 1000) <= r["timestamp"] <= int(end * 1000))
            if rows and min(r["timestamp"] for r in rows) < int(start * 1000):
                break
        funding = D(0)
        async for rows in self.pages("positionFunding", "position_fundings",
                                    start_timestamp=int(start * 1000), end_timestamp=int(end * 1000),
                                    market_ids=str(market_id)):
            funding += sum((D(r["change"]) for r in rows if r["market_id"] == market_id), D(0))
        price_pnl, fees = D(0), D(0)
        for row in fills:
            ask = row["ask_account_id"] == self.account
            maker = row["is_maker_ask"] == ask
            price_pnl += D(row["ask_account_pnl" if ask else "bid_account_pnl"])
            # Frais de Trade : taux en millionièmes du notionnel.
            fees += D(row["usd_amount"]) * D(row.get("maker_fee" if maker else "taker_fee", 0)) / 1_000_000
        return {"price_pnl": str(price_pnl), "funding": str(funding), "fees": str(fees),
                "net_pnl": str(price_pnl + funding - fees), "fills": len(fills),
                "filled_size": str(sum((D(r["size"]) for r in fills), D(0)))}
