"""Market maker Arcus : un ordre post-only par côté et par marché."""

import asyncio
import json
import logging
import os
import re
import time
import urllib.request
from contextlib import suppress
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN

import websockets
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

D = Decimal
BASE_URL = "https://api.arcus.xyz"
ACCOUNT_INDEX = int(os.getenv("ARCUS_ACCOUNT_INDEX", "0"))
# Montants NOTIONNELS en USD, indépendants de la marge et du levier.
PAIRS = {"SLV-USD": {"order_usd": D("10"), "max_position_usd": D("40")}}
BOOK_LEVELS = 5
MIN_SIDE_SHARE = D("0.20")
MAX_BOOK_AGE = 2.0
REQUEST_TIMEOUT = 10
log = logging.getLogger("arcus")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def ticks(value, unit):
    result = D(value) / D(unit)
    if result != result.to_integral_value():
        raise ValueError(f"{value} n'est pas un multiple de {unit}")
    return int(result)


@dataclass
class Quote:
    client_id: str
    side: str
    price: Decimal
    remaining: Decimal
    status: str = "PENDING"
    sequence: int = -1
    created: float = field(default_factory=time.monotonic)
    terminal: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class Market:
    info: dict
    config: dict
    book: dict = field(default_factory=dict)
    book_time: float = 0
    position: Decimal = D("0")
    position_sequence: int = -1
    fill_sequence: int = -1
    orders_ready: bool = False
    positions_ready: bool = False
    quotes: dict = field(default_factory=dict)
    leverage: Decimal | None = None
    leverage_changed: asyncio.Event = field(default_factory=asyncio.Event)


def desired_quote(market, side):
    """Prix et quantité autorisés, ou None si le côté ne doit pas être coté."""
    bids, asks = market.book.get("bids", []), market.book.get("asks", [])
    if not bids or not asks or D(bids[0][0]) >= D(asks[0][0]):
        return None
    totals = {}
    for name, levels, order_side in (("bid", bids, "BUY"), ("ask", asks, "SELL")):
        quote = market.quotes.get(order_side)
        total = D("0")
        for price, size in levels[:BOOK_LEVELS]:
            price, size = D(price), D(size)
            # Nos propres ordres ne doivent pas masquer un côté sans liquidité.
            if quote and quote.status == "OPEN" and quote.price == price:
                size = max(D("0"), size - quote.remaining)
            total += price * size
        totals[name] = total
    total = totals["bid"] + totals["ask"]
    own = totals["bid" if side == "BUY" else "ask"]
    if total == 0 or own == 0 or own / total < MIN_SIDE_SHARE:
        return None
    price = D((bids if side == "BUY" else asks)[0][0])
    # On réserve aussi la place pour le remplissage intégral de l'ordre.
    # Le plus élevé des deux prix évite de sous-estimer l'exposition au spread.
    risk_price = max(D(bids[0][0]), D(asks[0][0]))
    signed_position = market.position if side == "BUY" else -market.position
    room = market.config["max_position_usd"] / risk_price - signed_position
    step = D(market.info["stepSize"])
    quantity = min(market.config["order_usd"] / price, room,
                   D(market.info["maxOrderSize"]))
    quantity = (quantity / step).to_integral_value(rounding=ROUND_DOWN) * step
    if quantity < D(market.info["minOrderSize"]):
        return None
    if quantity * price < D(market.info["minOrderNotional"]):
        return None
    return price, quantity


class Bot:
    def __init__(self, address, private_key, markets):
        self.address = address.lower()
        self.key = private_key
        self.api_key = private_key.public_key().public_bytes_raw().hex()
        self.markets = markets
        self.by_id = {int(m.info["marketId"]): m for m in markets.values()}
        self.pending = {}
        self.request_id = 0
        self.quote_id = 0
        self.client_prefix = f"mm-{time.time_ns()}-"
        self.ws = None

    def body(self, market, **values):
        return {"address": self.address, "accountIndex": ACCOUNT_INDEX,
                "marketId": market.info["marketId"], **values}

    async def post(self, method, body, typed=None, timestamp=None):
        ts = timestamp or time.time_ns()
        message = canonical(typed) if typed is not None else str(ts) + method + canonical(body)
        self.request_id += 1
        request_id = self.request_id
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self.ws.send(canonical({"type": "post", "id": request_id, "request": {
                "type": method, "payload": body, "apiKey": self.api_key,
                "timestamp": str(ts), "signature": self.key.sign(message.encode()).hex()}}))
            response = await asyncio.wait_for(future, REQUEST_TIMEOUT)
            if response.get("error") or int(response["status"]) >= 400:
                raise RuntimeError(f"{method}: {response.get('error', response.get('result'))}")
            return response["result"]
        finally:
            self.pending.pop(request_id, None)

    async def receive(self):
        async for raw in self.ws:
            msg = json.loads(raw)
            if isinstance(msg.get("id"), int) and msg["id"] in self.pending:
                future = self.pending[msg["id"]]
                if not future.done():
                    future.set_result(msg)
                continue
            if msg.get("type") in ("error", "degraded"):
                raise RuntimeError(f"Flux Arcus indisponible : {msg}")
            if msg.get("type") not in ("subscribed", "channel_data"):
                continue
            try:
                self.update(msg)
            except Exception:
                log.exception("Erreur de traitement du canal %s (contents=%s)",
                              msg.get("channel"), type(msg.get("contents")).__name__)
                raise
        raise ConnectionError("WebSocket fermé")

    def update(self, msg):
        channel, contents = msg["channel"], msg["contents"]
        if isinstance(contents, list):
            if channel == "positions":
                contents = {"positions": contents}
            elif channel == "accountAttributeUpdates":
                contents = {"entries": contents}
            elif channel == "orders" and msg["type"] == "channel_data":
                for row in contents:
                    self.update({**msg, "contents": row})
                return
        if not isinstance(contents, dict):
            raise ValueError(f"Format inattendu pour {channel}: {type(contents).__name__}")
        if channel == "l2Orderbook":
            market = self.markets[msg["id"]]
            market.book = contents
            market.book_time = time.monotonic()
            return
        if msg["accountIndex"] != ACCOUNT_INDEX:
            return
        if channel == "accountAttributeUpdates":
            for entry in contents["entries"]:
                market = self.by_id.get(int(entry.get("marketId", -1)))
                if market is None:
                    continue
                if entry["type"] == "leverageReject":
                    raise RuntimeError(f"Changement de levier refusé : {entry}")
                if entry["type"] == "leverage":
                    market.leverage = D(str(entry["leverage"]))
                    market.leverage_changed.set()
        elif channel == "positions":
            snapshot = msg["type"] == "subscribed" or contents.get("isSnapshot")
            rows = contents.get("positions")
            if rows is None:
                # Certaines versions de l'API émettent la ligne directement.
                rows = {str(contents["marketId"]): contents}
            elif isinstance(rows, list):
                rows = {str(row["marketId"]): row for row in rows}
            if not isinstance(rows, dict):
                raise ValueError("positions doit être une liste ou un dictionnaire")
            for market_id, market in self.by_id.items():
                row = rows.get(str(market_id))
                if not snapshot and row is None:
                    continue
                sequence = contents.get("lastSequenceId")
                if sequence is None and row is not None:
                    sequence = row.get("sequenceNumber")
                if sequence is None:
                    if snapshot and row is None and not market.positions_ready:
                        sequence = -1  # Snapshot initial vide, aucune position.
                    else:
                        raise ValueError(f"Séquence manquante sur positions pour le marché {market_id}")
                sequence = int(sequence)
                if (snapshot or row is not None) and sequence >= market.position_sequence:
                    market.position = D(row["size"]) if row else D("0")
                    market.position_sequence = sequence
                    market.positions_ready = True
        elif channel == "orders":
            if msg["type"] == "subscribed" or contents.get("isSnapshot"):
                for row in contents["openOrders"]:
                    if int(row["marketId"]) in self.by_id and row["status"] in ("OPEN", "PARTIALLY_FILLED"):
                        raise RuntimeError("Des ordres existent déjà sur une paire configurée. "
                                           "Utiliser un sous-compte dédié et annuler ces ordres avant de lancer le bot.")
                for market in self.markets.values():
                    market.orders_ready = True
                return
            market = self.by_id.get(int(contents["marketId"]))
            if market is None:
                return
            quote = market.quotes.get(contents["side"])
            if quote is None or contents.get("clientId") != quote.client_id:
                if contents.get("clientId", "").startswith(self.client_prefix):
                    return  # Événement retardé d'un ordre déjà terminé.
                raise RuntimeError("Ordre externe détecté sur une paire du bot : sous-compte dédié requis")
            sequence = int(contents["sequenceNumber"])
            if sequence <= quote.sequence:
                return
            remaining = D(contents["remainingSize"])
            # Une annulation peut mettre remainingSize à zéro sans aucun fill.
            if contents.get("positionEffect") or (remaining < quote.remaining and
                                                   contents["status"] in ("OPEN", "FILLED")):
                market.fill_sequence = max(market.fill_sequence, sequence)
            quote.sequence = sequence
            quote.remaining = remaining
            quote.status = contents.get("state", contents["status"])
            if quote.status in ("OPEN", "PARTIALLY_FILLED"):
                quote.status = "OPEN"
            else:
                quote.terminal.set()
            log.info("%s %s %s reste=%s %s", market.info["marketDisplayName"],
                     quote.side, quote.status, remaining, contents.get("rejectionReason", ""))

    async def place(self, market, side, price, quantity):
        ts = time.time_ns()
        expiry = ts // 1000 + 40 * 86400 * 1_000_000
        self.quote_id += 1
        client_id = f"{self.client_prefix}{self.quote_id}"
        # Réserver avant l'envoi : le flux peut arriver avant la réponse RPC.
        market.quotes[side] = Quote(client_id, side, price, quantity)
        typed = {"ad": self.address, "ai": ACCOUNT_INDEX, "c": client_id,
                 "ct": ts, "g": expiry * 1000, "m": market.info["marketId"],
                 "op": 1, "p": ticks(price, market.info["tickSize"]),
                 "q": ticks(quantity, market.info["stepSize"]), "r": 0,
                 "s": 0 if side == "BUY" else 1, "t": 3, "v": 1}
        await self.post("placeOrder", self.body(market, clientId=client_id,
                        orderSide=side, orderType="LIMIT", price=str(price),
                        quantity=str(quantity), timeInForce="ALO", goodTilTime=str(expiry),
                        timestamp=ts), typed, ts)

    async def cancel(self, market, quote):
        if quote.terminal.is_set():
            return
        ts = time.time_ns()
        typed = {"ad": self.address, "ai": ACCOUNT_INDEX, "c": quote.client_id,
                 "ct": ts, "m": market.info["marketId"], "op": 2, "v": 1}
        await self.post("cancelOrder", self.body(market, kind="clientId",
                        clientId=quote.client_id, timestamp=ts), typed, ts)
        # Un ACK ne suffit pas : attendre la fin réelle avant de replacer.
        await asyncio.wait_for(quote.terminal.wait(), REQUEST_TIMEOUT)

    async def set_leverage(self, market, leverage):
        result = await self.post("setLeverage", self.body(market, leverage=leverage))
        status = result.get("status")
        if status not in ("APPLIED", "ACK") or D(str(result["leverage"])) != leverage:
            raise RuntimeError(f"Levier non confirmé : {result}")
        if status == "ACK":
            # ACK confirme la réception ; le flux indique le levier effectif.
            # La valeur peut déjà être confirmée (snapshot ou événement avant ACK).
            async def wait_for_leverage():
                while market.leverage != leverage:
                    market.leverage_changed.clear()
                    await market.leverage_changed.wait()
            try:
                await asyncio.wait_for(wait_for_leverage(), REQUEST_TIMEOUT)
            except TimeoutError as exc:
                raise TimeoutError(f"Levier x{leverage} non confirmé sur "
                                   f"{market.info['marketDisplayName']} après {REQUEST_TIMEOUT}s") from exc
        log.info("%s levier x%s confirmé", market.info["marketDisplayName"], leverage)

    async def run_strategy(self):
        deadline = time.monotonic() + REQUEST_TIMEOUT
        while not all(m.orders_ready and m.positions_ready and m.book for m in self.markets.values()):
            if time.monotonic() > deadline:
                raise TimeoutError("Snapshots initiaux manquants")
            await asyncio.sleep(0.05)
        for market in self.markets.values():
            leverage = int(D("1") / D(market.info["initialMarginFraction"]))
            await self.set_leverage(market, leverage)
        refresh_at = 0
        while True:
            now = time.monotonic()
            if any(now - m.book_time > MAX_BOOK_AGE for m in self.markets.values()):
                raise TimeoutError("Carnet périmé : arrêt et annulation des ordres")
            if now >= refresh_at:
                for market in self.markets.values():
                    await self.post("scheduleCancel", self.body(market, time=time.time_ns() // 1000 + 30_000_000))
                refresh_at = time.monotonic() + 10
            for market in self.markets.values():
                for side in ("BUY", "SELL"):
                    quote = market.quotes.get(side)
                    if quote and quote.terminal.is_set():
                        del market.quotes[side]
                        quote = None
                    target = desired_quote(market, side)
                    stale = time.monotonic() - market.book_time > MAX_BOOK_AGE
                    if quote:
                        if quote.status == "PENDING" and time.monotonic() - quote.created > REQUEST_TIMEOUT:
                            raise TimeoutError("Placement sans confirmation sur le flux orders")
                        if stale or target is None or quote.price != target[0] or quote.remaining > target[1]:
                            await self.cancel(market, quote)
                    elif not stale and target and market.position_sequence >= market.fill_sequence:
                        await self.place(market, side, *target)
            await asyncio.sleep(0.05)

    async def run(self):
        url = BASE_URL.replace("https://", "wss://").replace("http://", "ws://") + "/v1/ws"
        async with websockets.connect(url, max_queue=1024) as self.ws:
            receiver = asyncio.create_task(self.receive())
            strategy = None
            try:
                for channel in ("orders", "positions", "accountAttributeUpdates"):
                    await self.ws.send(canonical({"type": "subscribe", "channel": channel,
                                                "id": self.address, "accountIndex": ACCOUNT_INDEX}))
                for symbol in self.markets:
                    await self.ws.send(canonical({"type": "subscribe", "channel": "l2Orderbook",
                                                "id": symbol, "nLevels": BOOK_LEVELS}))
                strategy = asyncio.create_task(self.run_strategy())
                done, _ = await asyncio.wait((receiver, strategy), return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                if strategy:
                    strategy.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await strategy
                for market in self.markets.values():
                    for quote in list(market.quotes.values()):
                        if quote.terminal.is_set():
                            continue
                        try:
                            if receiver.done():
                                raise ConnectionError("Flux arrêté")
                            await self.cancel(market, quote)
                        except Exception as exc:
                            log.error("Annulation non confirmée : %s ; switch serveur laissé armé", exc)
                receiver.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await receiver


async def main():
    address = os.getenv("ARCUS_ADDRESS", "")
    signing_key = os.getenv("ARCUS_SIGNING_KEY", "").removeprefix("0x")
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", address) or not re.fullmatch(r"[0-9a-fA-F]{64}", signing_key):
        raise ValueError("Définir ARCUS_ADDRESS et ARCUS_SIGNING_KEY (clé Ed25519 privée de 32 octets en hex)")
    if not 0 <= ACCOUNT_INDEX <= 9:
        raise ValueError("ARCUS_ACCOUNT_INDEX doit être entre 0 et 9")
    if not 1 <= BOOK_LEVELS <= 100 or not D("0") < MIN_SIDE_SHARE < D("0.5"):
        raise ValueError("Paramètres de carnet invalides")
    def fetch_markets():
        with urllib.request.urlopen(BASE_URL + "/v1/markets", timeout=10) as response:
            return json.load(response)["markets"]
    available = {m["marketDisplayName"]: m for m in await asyncio.to_thread(fetch_markets)}
    markets = {}
    for symbol, config in PAIRS.items():
        info = available[symbol]
        if info["status"] != "ONLINE" or config["order_usd"] <= 0 or config["max_position_usd"] <= 0:
            raise ValueError(f"Marché ou configuration invalide : {symbol}")
        markets[symbol] = Market(info, config)
    await Bot(address, Ed25519PrivateKey.from_private_bytes(bytes.fromhex(signing_key)), markets).run()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
