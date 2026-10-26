"""Market making XAU uniquement, sur Lighter Robinhood Chain."""
import asyncio
import json
import logging
import os
import signal
import time
import uuid
from contextlib import contextmanager
from decimal import Decimal as D, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path

from exchanges import Http, Lighter, TERMINAL, UncertainOrder, decimal

ORDER_USD = D("10")
MIN_LIQUIDITY_USD = D("100")
STOP_DISTANCE = D("1")
POLL_SECONDS = 0.5
CLOSE_SLIPPAGE = D("0.02")
STATE_FILE = Path(os.getenv("STATE_FILE", "xau-mm-state.json"))
# Le plafond strict de 10 $ reste la règle tant que l'arrondi n'est pas autorisé.
ROUND_ORDER_UP = os.getenv("ROUND_ORDER_UP", "0") == "1"
log = logging.getLogger("xau-mm")


@contextmanager
def state_lock(path):
    """Empêcher deux processus d'utiliser le même inventaire et journal."""
    with Path(path).with_suffix(".lock").open("a+b") as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("Une autre instance utilise déjà ce journal") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class QuoteUnavailable(ValueError):
    pass


class ReadModelLag(RuntimeError):
    pass


def quote_quantity(market, bid, ask, round_up=False):
    """Même quantité des deux côtés, pour que l'ordre opposé puisse solder."""
    step = market["step"]
    if round_up:
        quantity = (ORDER_USD / bid / step).to_integral_value(rounding=ROUND_CEILING) * step
    else:
        quantity = (ORDER_USD / ask / step).to_integral_value(rounding=ROUND_FLOOR) * step
    if (quantity <= 0 or quantity < decimal(market["min_base_amount"])
            or quantity * bid < decimal(market["min_quote_amount"])
            or quantity * ask > decimal(market["order_quote_limit"])):
        raise QuoteUnavailable("10 $ incompatibles avec les minimums et le pas de quantité XAU")
    return quantity


def side_name(side):
    return "bids" if side == 1 else "asks"


def liquidity(book, side, price):
    return book["external_" + side_name(side)].get(price, D(0)) * price


def stop_hit(position, book):
    size, entry = position["size"], position["entry"]
    return (size > 0 and book["bids"][0][0] <= entry - STOP_DISTANCE
            or size < 0 and book["asks"][0][0] >= entry + STOP_DISTANCE)


class MarketMaker:
    def __init__(self, exchange, path=STATE_FILE, round_up=ROUND_ORDER_UP):
        self.exchange, self.path, self.round_up = exchange, Path(path), round_up
        self.stop = asyncio.Event()
        self.market = None
        self.state = {"strategy": "lighter-xau-mm-v1", "identity": exchange.identity,
                      "expected_size": "0", "orders": [], "phase": "quoting",
                      "cycle_quantity": None, "cycle_filled": False}
        if self.path.exists():
            self.state = json.loads(self.path.read_text(encoding="utf8"))
            if (self.state.get("strategy") != "lighter-xau-mm-v1"
                    or self.state.get("identity") != exchange.identity):
                raise ValueError("Journal incompatible avec cette stratégie ou ce compte")
        self.initialized = False

    def save(self):
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state, indent=2), encoding="utf8")
        for attempt in range(5):
            try:
                temporary.replace(self.path)
                return
            except PermissionError:
                # Certains scanners Windows ouvrent brièvement le journal.
                if attempt == 4:
                    raise
                time.sleep(0.02)

    def audit(self, event, **fields):
        try:
            with self.path.with_suffix(".orders.jsonl").open("a", encoding="utf8") as stream:
                stream.write(json.dumps({"time": time.time(), "event": event, **fields}, default=str) + "\n")
        except OSError:
            log.exception("Journal d'audit indisponible ; état durable conservé séparément")

    def live(self, kind=None):
        return [o for o in self.state["orders"] if o["status"] not in TERMINAL
                and (kind is None or o["kind"] == kind)]

    def update(self, order, row):
        if row is None:
            raise UncertainOrder(f"Ordre {order['id']} invisible ; nouvelles cotations suspendues")
        filled = decimal(row["filled"])
        previous = D(order["filled"])
        if filled < previous or filled > D(order["quantity"]):
            raise ValueError("Quantité exécutée incohérente")
        delta = filled - previous
        if delta:
            expected = D(self.state["expected_size"]) + order["side"] * delta
            self.state["expected_size"] = str(expected)
            self.state["cycle_filled"] = True
        order.update(status=row["status"], filled=str(filled))
        self.save()
        if delta:
            self.audit("fill", id=order["id"], side=order["side"], quantity=delta, expected=expected)

    async def refresh(self):
        for order in list(self.live()):
            self.update(order, await self.exchange.order_status(order["id"]))

    async def position(self):
        position = await self.exchange.position()
        if position["size"] != D(self.state["expected_size"]):
            raise ReadModelLag("Position et exécutions non réconciliées ; aucune nouvelle cotation")
        return position

    async def cancel(self, order, reason):
        self.audit("cancel_requested", id=order["id"], reason=reason)
        self.update(order, await self.exchange.cancel(order))
        if order["status"] not in TERMINAL:
            raise UncertainOrder(f"Annulation {order['id']} non confirmée")

    async def cancel_quotes(self, reason):
        failures = []
        for order in list(self.live("quote")):
            try:
                await self.cancel(order, reason)
            except Exception as exc:
                failures.append(str(exc))
        if failures:
            raise UncertainOrder("; ".join(failures))

    async def place(self, kind, side, quantity, price, reduce_only=False):
        order = {"id": str(uuid.uuid4().int % (2 ** 48 - 1) + 1),
                 "kind": kind, "side": side, "quantity": str(quantity),
                 "price": str(price), "reduce_only": reduce_only,
                 "filled": "0", "status": "PENDING"}
        self.state["orders"].append(order)
        self.save()  # Enregistrer AVANT tout appel réseau.
        self.audit("order_requested", order=order)
        self.update(order, await self.exchange.place(order))
        return order

    async def initialize(self):
        self.market = await self.exchange.load_market()
        known = {o["id"] for o in self.state["orders"]}
        if any(o["client_id"] not in known for o in await self.exchange.active_orders()):
            raise ValueError("Ordres externes détectés : compte dédié XAU requis")
        # Au redémarrage, annuler aussi les placements dont la réponse a été perdue.
        await self.cancel_quotes("démarrage / réconciliation")
        await self.refresh()
        position = await self.position()
        if not position["size"]:
            await self.exchange.set_leverage()
        self.save()
        self.initialized = True

    def reset_cycle(self):
        if self.live() or D(self.state["expected_size"]):
            raise RuntimeError("Impossible de réinitialiser une exposition active")
        if self.state["orders"]:
            self.audit("cycle_closed", orders=self.state["orders"])
        self.state.pop("exit_price", None)
        self.state.update(orders=[], cycle_quantity=None, cycle_filled=False, phase="quoting")
        self.save()

    async def close_position(self):
        """Annuler les quotes avant l'IOC reduce-only : aucun retournement au stop."""
        await self.cancel_quotes("stop loss / arrêt")
        await self.refresh()
        position = await self.position()
        if not position["size"]:
            self.reset_cycle()
            return
        if self.live("close"):
            raise UncertainOrder("Fermeture précédente encore incertaine")
        book = await self.exchange.book()
        side = -1 if position["size"] > 0 else 1
        best = book["asks" if side == 1 else "bids"][0][0]
        tick = self.market["tick"]
        price = (best * (1 + side * CLOSE_SLIPPAGE) / tick).to_integral_value(
            rounding=ROUND_CEILING if side == 1 else ROUND_FLOOR) * tick
        await self.place("close", side, abs(position["size"]), price, reduce_only=True)
        # Un IOC peut être partiel : la phase stopping reste durable jusqu'à zéro.

    async def step(self):
        await self.refresh()
        position = await self.position()
        if self.state["phase"] == "stopping":
            await self.close_position()
            return

        book = await self.exchange.book()
        size = position["size"]
        if size and stop_hit(position, book):
            self.state["phase"] = "stopping"
            self.save()
            self.audit("stop_triggered", position=position)
            await self.close_position()
            return

        if size:
            entry_side = 1 if size > 0 else -1
            # Dès le premier fill, supprimer tout reliquat dans le même sens.
            same_side = [o for o in self.live("quote") if o["side"] == entry_side]
            if same_side:
                for order in same_side:
                    await self.cancel(order, "inventaire déjà présent dans ce sens")
                await self.refresh()
                position = await self.position()
                size = position["size"]
                if not size:
                    await self.cancel_quotes("position soldée pendant l'annulation")
                    return
                entry_side = 1 if size > 0 else -1
            opposite = [o for o in self.live("quote") if o["side"] == -entry_side]
            if len(opposite) > 1:
                raise RuntimeError("Plusieurs ordres de sortie détectés")
            price = book[side_name(-entry_side)][0][0]
            previous_exit = next((o for o in reversed(self.state["orders"])
                                  if o["kind"] == "quote" and o["side"] == -entry_side), None)
            if previous_exit:
                # Une expiration ne déplace pas le prix de la sortie conservée.
                self.state["exit_price"] = previous_exit["price"]
                self.save()
            if opposite:
                order = opposite[0]
                price = D(order["price"])
                remaining = D(order["quantity"]) - D(order["filled"])
                # Garder l'ordre et son prix tant qu'il ne peut pas inverser la position.
                if remaining > abs(size):
                    await self.cancel(order, "redimensionnement après fill partiel")
                    self.state["exit_price"] = str(price)
                    self.save()
                    return
                if liquidity(book, -entry_side, price) < MIN_LIQUIDITY_USD:
                    await self.cancel(order, "liquidité au tick inférieure à 100 $")
                return
            price = D(self.state.get("exit_price", price))
            if liquidity(book, -entry_side, price) < MIN_LIQUIDITY_USD:
                price = book[side_name(-entry_side)][0][0]
            if liquidity(book, -entry_side, price) >= MIN_LIQUIDITY_USD:
                # Les sorties peuvent être inférieures à 10 $ après un fill partiel.
                await self.place("quote", -entry_side, abs(size), price, reduce_only=True)
                self.state.pop("exit_price", None)
                self.save()
            return

        if self.state["cycle_filled"]:
            # Deux fills peuvent s'être compensés entre deux lectures.
            await self.cancel_quotes("position soldée ; nouvelle paire de quotes")
            await self.refresh()
            if not (await self.position())["size"]:
                self.state.pop("exit_price", None)
                self.reset_cycle()
            return

        bid, ask = book["bids"][0][0], book["asks"][0][0]
        try:
            quantity = quote_quantity(self.market, bid, ask, self.round_up)
        except QuoteUnavailable as exc:
            await self.cancel_quotes(str(exc))
            log.info("%s", exc)
            return
        # Recalculer les deux quantités ensemble pour préserver leur symétrie.
        previous = self.state["cycle_quantity"]
        if previous is not None and D(previous) != quantity:
            await self.cancel_quotes("nouvelle taille arrondie")
            if self.state["cycle_filled"]:
                return
        self.state["cycle_quantity"] = str(quantity)
        self.save()

        stale = [o for o in self.live("quote")
                 if D(o["price"]) != (bid if o["side"] == 1 else ask)
                 or liquidity(book, o["side"], D(o["price"])) < MIN_LIQUIDITY_USD]
        if stale:
            for order in stale:
                await self.cancel(order, "best bid/ask déplacé ou liquidité insuffisante")
            return

        for side, price in ((1, bid), (-1, ask)):
            orders = [o for o in self.live("quote") if o["side"] == side]
            if len(orders) > 1:
                raise RuntimeError("Plusieurs entrées dans le même sens")
            if not orders and liquidity(book, side, price) >= MIN_LIQUIDITY_USD:
                # Relecture juste avant chaque placement, notamment le deuxième.
                await self.refresh()
                if (await self.position())["size"] or self.state["cycle_filled"] or self.stop.is_set():
                    return
                fresh = await self.exchange.book()
                if (fresh[side_name(side)][0][0] != price
                        or liquidity(fresh, side, price) < MIN_LIQUIDITY_USD):
                    return
                await self.place("quote", side, quantity, price)
                if self.state["cycle_filled"]:
                    return

    async def shutdown(self):
        self.state["phase"] = "stopping"
        self.save()
        for _ in range(5):
            try:
                await self.close_position()
                if not self.live() and not D(self.state["expected_size"]):
                    return
            except Exception:
                log.exception("Arrêt : annulation ou fermeture à retenter")
            await asyncio.sleep(POLL_SECONDS)
        raise RuntimeError("URGENT : vérifier XAU sur Lighter ; fermeture non confirmée")

    async def run(self):
        await self.initialize()
        try:
            while not self.stop.is_set():
                try:
                    await self.step()
                except Exception:
                    log.exception("Cycle suspendu ; tentative d'annulation des quotes")
                    try:
                        await self.cancel_quotes("données indisponibles ou ordre incertain")
                    except Exception:
                        log.exception("URGENT : annulation non confirmée ; quotes expirent sous 60 s")
                try:
                    await asyncio.wait_for(self.stop.wait(), POLL_SECONDS)
                except TimeoutError:
                    pass
        finally:
            if self.initialized:
                await self.shutdown()


async def main():
    async with Http() as http:
        exchange = Lighter(http)
        maker = MarketMaker(exchange)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, maker.stop.set)
            except NotImplementedError:
                signal.signal(sig, lambda *_: loop.call_soon_threadsafe(maker.stop.set))
        try:
            await maker.run()
        finally:
            await exchange.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    with state_lock(STATE_FILE):
        asyncio.run(main())
