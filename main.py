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

from exchanges import ApiError, Http, Lighter, OrderRejected, QUOTE_TTL_SECONDS, TERMINAL, UncertainOrder, decimal

ORDER_USD = D("10")
MIN_LIQUIDITY_USD = D("100")
STOP_DISTANCE = D("1")
POLL_SECONDS = 0.5
STARTUP_RETRY_SECONDS = 5
MAX_STARTUP_RETRY_SECONDS = 60
CLOSE_SLIPPAGE = D("0.02")
RECOVERY_GRACE_SECONDS = 5
# Old versions without a persisted expiry submitted 60-second quotes.
LEGACY_QUOTE_WAIT_SECONDS = 60 + RECOVERY_GRACE_SECONDS
STATE_FILE = Path(os.getenv("STATE_FILE", "xau-mm-state.json"))
# 10 $ est une cible : utiliser le premier pas XAU qui atteint ce notionnel.
ROUND_ORDER_UP = os.getenv("ROUND_ORDER_UP", "1") == "1"
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


class QuoteRetryPending(RuntimeError):
    pass


class ReadModelLag(RuntimeError):
    def __init__(self, message, observed=None):
        super().__init__(message)
        self.observed = observed


def quote_quantity(market, bid, ask, round_up=True):
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


def reduce_only_quantity(market, size, price):
    """Respecter les minimums ; reduce-only borne les fills à la position.

    https://docs.lighter.xyz/trading/order-types-and-matching
    """
    price = decimal(price, positive=True)
    step = decimal(market["step"], positive=True)
    minimum = max(decimal(size, positive=True), decimal(market["min_base_amount"]),
                  decimal(market["min_quote_amount"]) / price)
    quantity = (minimum / step).to_integral_value(rounding=ROUND_CEILING) * step
    if quantity * price > decimal(market["order_quote_limit"]):
        raise QuoteUnavailable("Sortie reduce-only au-delà de la limite de notionnel XAU")
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
        self.legacy_recovery_deadline = time.monotonic() + LEGACY_QUOTE_WAIT_SECONDS

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

    def pin_exit_price(self, entry_order):
        side = -entry_order["side"]
        if self.state.get("exit_side") == side and "exit_price" in self.state:
            return D(self.state["exit_price"])

        def profitable(price):
            return price is not None and side * (D(price) - D(entry_order["price"])) < 0

        # Prendre l'ordre opposé effectivement présent au fill, ou le prix de
        # la paire enregistré avant l'envoi si le deuxième ordre n'existe pas.
        active = [o for o in self.live("quote") if o["side"] == side
                  and not o["reduce_only"] and profitable(o["price"])]
        price = active[0]["price"] if active else entry_order.get("opposite_price")
        if not profitable(price):
            # Reprise d'un ancien journal : retrouver la quote d'origine près
            # de l'entrée, plutôt qu'une sortie reduce-only déjà déplacée.
            orders = self.state["orders"]
            entry_index = orders.index(entry_order)
            candidates = [(abs(i - entry_index), o) for i, o in enumerate(orders)
                          if o["kind"] == "quote" and o["side"] == side
                          and not o["reduce_only"] and o["status"] != "REJECTED"
                          and profitable(o["price"])]
            if candidates:
                price = min(candidates, key=lambda candidate: candidate[0])[1]["price"]
            else:
                price = self.state.get("exit_price")
        if not profitable(price):
            return None
        self.state.update(exit_price=str(price), exit_side=side)
        self.save()
        self.audit("exit_price_locked", entry_id=entry_order["id"], side=side, price=price)
        log.info("Prix de sortie figé : %s XAU à %s ; entrée %s",
                 "vente" if side == -1 else "achat", price, entry_order["id"])
        return D(price)

    def position_exit_price(self, size):
        side = -1 if size > 0 else 1
        if self.state.get("exit_side") == side and "exit_price" in self.state:
            return D(self.state["exit_price"])
        entry = next((o for o in self.state["orders"] if o["kind"] == "quote"
                      and o["side"] == -side and D(o["filled"]) > 0), None)
        price = self.pin_exit_price(entry) if entry else None
        if price is None:
            raise UncertainOrder("Prix opposé d'origine introuvable ; sortie limite suspendue, "
                                 "stop toujours surveillé")
        return price

    def update(self, order, row):
        if row is None:
            raise UncertainOrder(f"Ordre {order['id']} invisible ; nouvelles cotations suspendues")
        filled = decimal(row["filled"])
        previous = D(order["filled"])
        if filled < previous or filled > D(order["quantity"]):
            raise ValueError("Quantité exécutée incohérente")
        delta = filled - previous
        previous_size = D(self.state["expected_size"])
        if delta:
            expected = previous_size + order["side"] * delta
            self.state["expected_size"] = str(expected)
            self.state["cycle_filled"] = True
        order.update(status=row["status"], filled=str(filled))
        if "order_index" in row:
            order["order_index"] = row["order_index"]
        self.save()
        if delta:
            if order["kind"] == "quote" and expected and previous_size * expected <= 0:
                # Figer la cible avant les lectures suivantes ou une annulation.
                self.pin_exit_price(order)
            self.audit("fill", id=order["id"], side=order["side"], quantity=delta, expected=expected)

    async def refresh(self):
        orders = list(self.live())
        rows = await self.exchange.order_statuses([order["id"] for order in orders])
        for order in orders:
            self.update(order, rows[order["id"]])

    async def position(self):
        position = await self.exchange.position()
        expected = D(self.state["expected_size"])
        if position["size"] != expected:
            raise ReadModelLag(
                f"Position et exécutions non réconciliées : Lighter={position['size']} XAU, "
                f"journal={expected} XAU ; aucune nouvelle cotation", observed=position["size"])
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

    async def place(self, kind, side, quantity, price, reduce_only=False, opposite_price=None):
        retry = self.state.get("quote_retry", {}).get(str(side), {}) if kind == "quote" else {}
        if time.time() < retry.get("retry_at", 0):
            raise QuoteRetryPending("Nouvelle tentative de quote différée après refus explicite")
        order = {"id": str(uuid.uuid4().int % (2 ** 48 - 1) + 1),
                 "kind": kind, "side": side, "quantity": str(quantity),
                 "price": str(price), "reduce_only": reduce_only,
                 "filled": "0", "status": "PENDING"}
        if kind == "quote":
            order["expires_at_ms"] = int(time.time() * 1000) + QUOTE_TTL_SECONDS * 1000
            if opposite_price is not None:
                order["opposite_price"] = str(opposite_price)
        self.state["orders"].append(order)
        self.save()  # Enregistrer AVANT tout appel réseau.
        self.audit("order_requested", order=order)
        try:
            row = await self.exchange.place(order)
        except OrderRejected as exc:
            if kind == "quote":
                delay = min(retry.get("delay", STARTUP_RETRY_SECONDS / 2) * 2, MAX_STARTUP_RETRY_SECONDS)
                self.state.setdefault("quote_retry", {})[str(side)] = {
                    "retry_at": time.time() + delay, "delay": delay, "reason": str(exc)}
                log.warning("Quote refusée : quantité=%s XAU, prix=%s, reduce_only=%s ; "
                            "nouvelle tentative de ce côté dans %.1f s", quantity, price, reduce_only, delay)
            self.update(order, {"status": "REJECTED", "filled": "0"})
            self.audit("order_rejected", id=order["id"], reason=str(exc))
            raise
        finally:
            # Conserver aussi le résultat d'envoi si la confirmation échoue.
            self.save()
        if kind == "quote":
            self.state.get("quote_retry", {}).pop(str(side), None)
        self.update(order, row)
        return order

    async def startup_snapshot(self, check_position=True):
        """Lire et réconcilier le compte, sans envoyer ni annuler d'ordre."""
        known = {o["id"] for o in self.state["orders"]}
        active = {o["client_id"]: o for o in await self.exchange.active_orders()}
        if set(active) - known:
            raise ValueError("Ordres externes détectés : compte dédié XAU requis")
        missing = []
        for order in list(self.live()):
            row = active.get(order["id"])
            if row is None:
                row = await self.exchange.order_status(order["id"])
            if row is None:
                missing.append(order)
            else:
                self.update(order, row)
                if row["status"] not in TERMINAL:
                    active[order["id"]] = row
        position = await self.position() if check_position else await self.exchange.position()
        return active, missing, position

    def missing_quote_delay(self, missing):
        return max((max(0, o["expires_at_ms"] / 1000 + RECOVERY_GRACE_SECONDS - time.time())
                    if "expires_at_ms" in o else max(0, self.legacy_recovery_deadline - time.monotonic())
                    for o in missing), default=0)

    async def reconcile_external_flat(self):
        """Réconcilier une fermeture externe après deux lectures du compte vide."""
        active, missing, position = await self.startup_snapshot(check_position=False)
        if active or position["size"] or any(o["kind"] != "quote" for o in missing):
            return False

        # Une quote envoyée mais encore invisible peut réapparaître : attendre
        # aussi son expiration avant toute remise à zéro de l'inventaire.
        delay = max(RECOVERY_GRACE_SECONDS, self.missing_quote_delay(missing))
        log.warning("Position Lighter nulle avec inventaire local : vérification du compte "
                    "sans ordre actif dans %.1f s avant réconciliation", delay)
        await self.wait_for_retry(delay)
        if self.stop.is_set():
            return False
        active, missing, position = await self.startup_snapshot(check_position=False)
        if active or position["size"] or any(o["kind"] != "quote" for o in missing):
            return False

        checked_at = time.time()
        for order in missing:
            order.update(status="ABSENT", recovery={"checked_at": checked_at,
                "reason": "quote expirée ou non créée ; compte confirmé sans ordre ni position"})
        recovery = {"checked_at": checked_at, "previous_expected_size": self.state["expected_size"],
                    "reason": "ajustement externe ; compte confirmé sans ordre ni position",
                    "orders": self.state["orders"]}
        self.state["last_flat_recovery"] = recovery
        self.state["expected_size"] = "0"
        self.reset_cycle()
        self.audit("external_flat_reconciled", **recovery)
        log.warning("Compte Lighter confirmé sans position ni ordre actif : inventaire du journal "
                    "%s → 0 XAU ; ancien cycle archivé, reprise des cotations",
                    recovery["previous_expected_size"])
        return True

    async def reconcile_startup(self):
        active, missing, position = await self.startup_snapshot()
        if not missing:
            return
        if active or position["size"] or any(o["kind"] != "quote" for o in missing):
            raise UncertainOrder(
                "Reprise suspendue : référence introuvable avec un ordre actif, une position "
                "ou une fermeture incertaine ; aucune annulation envoyée au démarrage")

        delay = self.missing_quote_delay(missing)
        if delay:
            log.warning("Anciennes quotes introuvables : attente de %.1f s avant vérification du compte ; "
                        "aucune annulation envoyée", delay)
            try:
                await asyncio.wait_for(self.stop.wait(), delay)
                return
            except TimeoutError:
                pass

        # Deuxième lecture complète après expiration : une réponse en panne
        # ou une position non réconciliée ne peut jamais signifier compte vide.
        active, missing, position = await self.startup_snapshot()
        if not missing:
            return
        if active or position["size"] or any(o["kind"] != "quote" for o in missing):
            raise UncertainOrder("Le compte n'est pas vide : références introuvables conservées")
        for order in missing:
            order.update(status="ABSENT", recovery={"checked_at": time.time(),
                "reason": "quote expirée ou non créée ; introuvable ; compte sans ordre ni position"})
        self.save()
        for order in missing:
            self.audit("order_reconciled_absent", order=order)
            log.warning("Référence locale %s classée ABSENT : aucun ordre ni position sur Lighter ; "
                        "aucune annulation envoyée", order["id"])

    async def initialize(self):
        log.info("Journal XAU utilisé : %s", self.path.resolve())
        self.market = await self.exchange.load_market()
        try:
            await self.reconcile_startup()
        except ReadModelLag as exc:
            if exc.observed != 0 or not D(self.state["expected_size"]):
                raise
            if not await self.reconcile_external_flat():
                if self.stop.is_set():
                    return
                raise
        if self.stop.is_set():
            return
        position = await self.position()
        if not position["size"] and not self.live():
            await self.exchange.set_leverage()
        self.save()
        self.initialized = True

    def reset_cycle(self):
        if self.live() or D(self.state["expected_size"]):
            raise RuntimeError("Impossible de réinitialiser une exposition active")
        if self.state["orders"]:
            self.audit("cycle_closed", orders=self.state["orders"])
        self.state.pop("exit_price", None)
        self.state.pop("exit_side", None)
        self.state.pop("quote_retry", None)
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
        quantity = reduce_only_quantity(self.market, abs(position["size"]), min(best, price))
        await self.place("close", side, quantity, price, reduce_only=True)
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
            price = self.position_exit_price(size)
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
                price = self.position_exit_price(size)
            opposite = [o for o in self.live("quote") if o["side"] == -entry_side]
            if len(opposite) > 1:
                raise RuntimeError("Plusieurs ordres de sortie détectés")
            if opposite:
                order = opposite[0]
                remaining = D(order["quantity"]) - D(order["filled"])
                if D(order["price"]) != price:
                    await self.cancel(order, "retour au prix opposé figé au fill")
                    return
                # Garder l'ordre et son prix tant qu'il ne peut pas inverser la position.
                if remaining > abs(size) and not order["reduce_only"]:
                    await self.cancel(order, "redimensionnement après fill partiel")
                    return
                return
            quantity = reduce_only_quantity(self.market, abs(size), price)
            await self.place("quote", -entry_side, quantity, price, reduce_only=True)
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
                await self.place("quote", side, quantity, price,
                                 opposite_price=ask if side == 1 else bid)
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
            await asyncio.sleep(max(POLL_SECONDS, self.exchange.retry_after))
        raise RuntimeError("URGENT : vérifier XAU sur Lighter ; fermeture non confirmée")

    async def wait_for_retry(self, delay):
        try:
            await asyncio.wait_for(self.stop.wait(), delay)
        except TimeoutError:
            pass

    async def run(self):
        retry_delay = STARTUP_RETRY_SECONDS
        while not self.stop.is_set():
            try:
                await self.initialize()
                break
            except (ApiError, ReadModelLag, UncertainOrder) as exc:
                # Garder le même client et son délai de quota entre tentatives.
                # Les fills déjà confirmés sont conservés ; seul un compte
                # vérifié vide permet l'ajustement d'une fermeture externe.
                delay = max(retry_delay, self.exchange.retry_after)
                log.warning("Démarrage suspendu : %s ; nouvelle vérification dans %.1f s ; "
                            "aucune nouvelle cotation, stop non surveillé avant réconciliation", exc, delay)
                await self.wait_for_retry(delay)
                retry_delay = min(retry_delay * 2, MAX_STARTUP_RETRY_SECONDS)
        if not self.initialized:
            return
        try:
            while not self.stop.is_set():
                try:
                    await self.step()
                except QuoteRetryPending:
                    # Le prochain cycle relit position/carnet et surveille le
                    # stop ; seule la soumission des quotes refusées attend.
                    pass
                except Exception:
                    log.exception("Cycle suspendu ; tentative d'annulation des quotes")
                    try:
                        await self.cancel_quotes("données indisponibles ou ordre incertain")
                    except Exception:
                        log.exception("URGENT : annulation non confirmée ; expiration prévue des quotes "
                                      "au plus tard %s s après leur création", QUOTE_TTL_SECONDS)
                await self.wait_for_retry(max(POLL_SECONDS, self.exchange.retry_after))
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
