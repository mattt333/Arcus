"""Arbitrage de funding Arcus / Lighter, uniquement sur les RWA communs."""
import asyncio
import json
import logging
import math
import os
import signal
import time
import uuid
from decimal import Decimal, ROUND_DOWN
from pathlib import Path

from exchanges import Arcus, Lighter, Http, TERMINAL, UncertainOrder, wait_position, check_book_spread, MAX_SPREAD, OPEN_SLIPPAGE

D = Decimal
POSITION_USD = D(os.getenv("POSITION_USD", "100"))
MAX_PAIRS = int(os.getenv("MAX_PAIRS", "2"))
MIN_FUNDING_GAP = D(os.getenv("MIN_FUNDING_GAP", "0.30"))
HISTORY_SECONDS = 4 * 3600  # Stabilite minimale avant ouverture.
MAX_PRICE_MOVE = D("0.10")
LIQUIDATION_BUFFER = D("0.10")  # 10 % du trajet entrée -> liquidation restant.
SCAN_SECONDS = 30
MONITOR_SECONDS = 2
STATE_FILE = Path(os.getenv("STATE_FILE", "funding-state.json"))
log = logging.getLogger("funding")


def common_step(a, b):
    scale = D(10) ** max(-a.as_tuple().exponent, -b.as_tuple().exponent, 0)
    return D(math.lcm(int(a * scale), int(b * scale))) / scale


def direction(a, b):
    """Positif = long Arcus, short Lighter ; taux horaires en fractions."""
    return 1 if b > a else -1


def relative_gap(a, b):
    divisor = max(abs(a), abs(b))
    return abs(b - a) / divisor if divisor else D(0)


def match_markets(arcus, lighter):
    result = {}
    for a in arcus:
        if a["category"] not in ("EQUITIES", "COMMODITIES") or a["status"] != "ONLINE":
            continue
        symbol = a["marketDisplayName"]
        candidates = [e for e in lighter if e["name"] == symbol
                      and e["market_type"] == "perp" and e["status"] == "active"
                      and D(e["multiplier"]) == 1 and e["quote_multiplier"] == 1
                      and not e.get("is_frozen")
                      and not e.get("market_config", {}).get("force_reduce_only")]
        if len(candidates) == 1:
            result[symbol] = (a, candidates[0])
    return result


def update_history(samples, now, a, b):
    duration = HISTORY_SECONDS
    if samples and (now - samples[-1][0] > SCAN_SECONDS * 3 or now < samples[-1][0]):
        samples.clear()
    if relative_gap(a, b) < MIN_FUNDING_GAP or a == b:
        samples.clear()
        return
    if samples and direction(D(samples[-1][1]), D(samples[-1][2])) != direction(a, b):
        samples.clear()
    samples.append([now, str(a), str(b)])
    while len(samples) > 1 and samples[1][0] <= now - duration:
        samples.pop(0)


def funding_score(samples, now):
    gains = [abs(D(b) - D(a)) for timestamp, a, b in samples
             if now - HISTORY_SECONDS <= timestamp <= now]
    if not gains:
        return None
    return sum(gains, D(0)) / len(gains)


def stable_funding_history(arcus, lighter, now, orientation):
    """Comparer les memes heures, avec un point couvrant le debut de la fenetre."""
    end = int(now // 3600) * 3600
    start = int((now - HISTORY_SECONDS) // 3600) * 3600
    rates = []
    for rows in (arcus, lighter):
        by_time = {}
        for timestamp, rate in rows:
            if timestamp in by_time and by_time[timestamp] != rate:
                return False
            by_time[timestamp] = rate
        rates.append(by_time)
    for hour in range(start, end + 1, 3600):
        a, b = rates[0].get(hour), rates[1].get(hour)
        if (a is None or b is None or not a.is_finite() or not b.is_finite()
                or a == b or direction(a, b) != orientation or relative_gap(a, b) < MIN_FUNDING_GAP):
            return False
    return True


def funding_history_score(arcus, lighter, now, orientation):
    """Ecart absolu moyen sur exactement 4 h, pondere par la duree de chaque taux horaire."""
    if not stable_funding_history(arcus, lighter, now, orientation):
        return None
    a, b = dict(arcus), dict(lighter)
    end = D(str(now))
    start = end - HISTORY_SECONDS
    total = D(0)
    for hour in range(int(start // 3600) * 3600, int(end // 3600) * 3600 + 1, 3600):
        duration = min(end, D(hour + 3600)) - max(start, D(hour))
        total += abs(b[hour] - a[hour]) * duration
    return total / HISTORY_SECONDS


def vwap(levels, quantity):
    remaining, cost = quantity, D(0)
    for price, size in levels:
        take = min(remaining, size)
        cost += price * take
        remaining -= take
        if remaining == 0:
            return cost / quantity
    raise ValueError("Profondeur insuffisante pour la taille demandée")


def entry_plan(a, e, books, orientation):
    ab, eb = books
    for book in books:
        check_book_spread(book)
    prices = (ab["asks" if orientation == 1 else "bids"][0][0],
              eb["bids" if orientation == 1 else "asks"][0][0])
    step = common_step(D(a["stepSize"]), D(e["stepSize"]))
    quantity = (POSITION_USD / max(prices) / step).to_integral_value(rounding=ROUND_DOWN) * step
    if quantity <= 0 or quantity < max(D(a["minOrderSize"]), D(e["minOrderSize"])):
        raise ValueError("Taille sous les minimums de marché")
    if quantity > D(a["maxOrderSize"]) or quantity * min(prices) < D(a["minOrderNotional"]):
        raise ValueError("Taille non autorisée sur Arcus")
    if quantity * min(prices) < D(e["minOrderNotional"]) or quantity * max(prices) > D(e["maxOrderNotional"]):
        raise ValueError("Taille non autorisée sur Lighter")
    fills = (vwap(ab["asks" if orientation == 1 else "bids"], quantity),
             vwap(eb["bids" if orientation == 1 else "asks"], quantity))
    for price, fill in zip(prices, fills):
        if abs(fill - price) / price > OPEN_SLIPPAGE:
            raise ValueError("Slippage trop élevé")
    return quantity


def risk_reason(position):
    if position is None:
        return "une jambe a disparu"
    mark, entry, liquidation = (position[k] for k in ("mark", "entry", "liquidation"))
    if mark <= 0 or entry <= 0:
        return "prix de risque indisponible"
    if abs(mark / entry - 1) >= MAX_PRICE_MOVE:
        return "mouvement de prix de 10 %"
    if liquidation is None:
        return "prix de liquidation indisponible"
    signed_distance = (mark - liquidation) * (1 if position["size"] > 0 else -1)
    initial_distance = abs(entry - liquidation)
    if initial_distance == 0 or signed_distance <= initial_distance * LIQUIDATION_BUFFER:
        return "proximité de liquidation"
    return None


class Farmer:
    def __init__(self, arcus, lighter, state_path=STATE_FILE):
        self.arcus, self.lighter = arcus, lighter
        self.path = state_path
        owner = {"arcus": arcus.identity, "lighter": lighter.identity}
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {"owner": owner, "active": {}, "history": {}}
        if self.state["owner"] != owner:
            raise ValueError("Le journal appartient à d'autres comptes ou environnements")
        for trade in self.state["active"].values():
            for venue, exchange in (("arcus", arcus), ("lighter", lighter)):
                if isinstance(exchange, (Arcus, Lighter)) and trade.get("market_info", {}).get(venue):
                    exchange.info[trade[venue]] = trade["market_info"][venue]
        self.pairs = {}
        self.stop = asyncio.Event()

    def save(self):
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf8") as handle:
            json.dump(self.state, handle)
            handle.flush()
            os.fsync(handle.fileno())
        # Windows peut bri?vement retenir le fichier pendant une lecture/indexation.
        for attempt in range(10):
            try:
                temporary.replace(self.path)
                break
            except PermissionError:
                if os.name != "nt" or attempt == 9:
                    raise
                time.sleep(0.02)

    def audit(self, event, **fields):
        record = {"timestamp": time.time(), "event": event, **fields}
        with self.path.with_suffix(".orders.jsonl").open("a", encoding="utf8") as handle:
            handle.write(json.dumps(record, default=str, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        log.info("%s", json.dumps(record, default=str, ensure_ascii=False))

    async def place_order(self, exchange, market, side, quantity, reason, reduce_only=False, trade=None):
        client_id = ("fund" + uuid.uuid4().hex if exchange.name == "Arcus"
                     else str(uuid.uuid4().int % (2 ** 48 - 1) + 1))
        reference = {"venue": exchange.name, "market": market, "client_id": client_id, "terminal": False,
                     "side": side, "reduce_only": reduce_only}
        if trade is not None:
            trade.setdefault("orders", []).append(reference)
            self.save()  # L'identifiant est durable AVANT transmission, y compris en fermeture.
        self.audit("order_requested", venue=exchange.name, market=market, side=side,
                   quantity=quantity, reduce_only=reduce_only, reason=reason, client_id=client_id)
        try:
            result = await exchange.order(market, side, quantity, reduce_only=reduce_only, client_id=client_id)
        except Exception as exc:
            if isinstance(exc, UncertainOrder):
                reference["client_id"] = exc.client_id
            else:
                reference["terminal"] = True  # Les adaptateurs n'ont pas transmis cet ordre.
            if trade is not None:
                self.save()
            self.audit("order_failed", venue=exchange.name, market=market, reason=reason,
                       client_id=reference["client_id"], error=str(exc))
            raise
        reference["terminal"] = result["status"] in TERMINAL
        reference["filled_size"] = result.get("filledSize")
        if trade is not None:
            self.save()
        self.audit("order_result", venue=exchange.name, market=market, reason=reason, result=result)
        return result

    async def positions(self):
        results = await asyncio.gather(self.arcus.positions(), self.lighter.positions(), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result
        return results

    async def close_pair(self, symbol, reason):
        trade = self.state["active"][symbol]
        trade["phase"] = "closing"
        trade["close_reason"] = reason
        self.save()
        log.warning("Fermeture %s : %s", symbol, reason)
        failures = []
        reconciled_venues = set()
        # Annuler/faire confirmer les placements incertains AVANT lecture des positions.
        for reference in trade.get("orders", []):
            if reference["terminal"]:
                continue
            exchange = self.arcus if reference["venue"] == "Arcus" else self.lighter
            self.audit("cancel_requested", venue=exchange.name, market=reference["market"],
                       client_id=reference["client_id"], reason=reason)
            try:
                row = await exchange.cancel_order(reference["market"], reference["client_id"])
                reference["terminal"] = bool(row and row["status"] in TERMINAL)
                reference["filled_size"] = row.get("filledSize") if row else None
                self.save()
                self.audit("cancel_result", venue=exchange.name, reason=reason, result=row)
                if not reference["terminal"]:
                    failures.append("Placement encore incertain")
                else:
                    reconciled_venues.add(exchange.name)
            except Exception as exc:
                failures.append(f"Placement encore incertain sur {exchange.name}: {exc}")
                self.audit("cancel_failed", venue=exchange.name, reason=reason, error=str(exc))

        for exchange, market in ((self.arcus, trade["arcus"]), (self.lighter, trade["lighter"])):
            if exchange.name in reconciled_venues and not failures:
                expected = sum((r["side"] * D(r.get("filled_size") or 0)
                                for r in trade["orders"] if r["venue"] == exchange.name), D(0))
                try:
                    await wait_position(exchange, market, expected)
                except Exception as exc:
                    failures.append(f"Read-model non reconcilie sur {exchange.name}: {exc}")

        async def close_leg(exchange, market):
            for attempt in range(5):
                try:
                    position = (await exchange.positions()).get(market)
                    if position is None:
                        return
                    size = position["size"]
                    self.audit("position_snapshot", venue=exchange.name, market=market,
                               reason=reason, unrealized_pnl=(position["mark"] - position["entry"]) * size,
                               funding=position.get("funding"), fees=position.get("fees"))
                    await self.place_order(exchange, market, -1 if size > 0 else 1,
                                           abs(size), reason, reduce_only=True, trade=trade)
                except UncertainOrder:
                    # Un reduce-only sans statut terminal reste durable : pas de renvoi.
                    raise
                except Exception as exc:
                    log.error("Fermeture %s tentative %s : %s", exchange.name, attempt + 1, exc)
                await asyncio.sleep(1)
            if market in await exchange.positions():
                raise RuntimeError(f"Exposition restante sur {exchange.name} {market}")

        results = await asyncio.gather(close_leg(self.arcus, trade["arcus"]),
                                       close_leg(self.lighter, trade["lighter"]), return_exceptions=True)
        failures.extend(str(r) for r in results if isinstance(r, BaseException))
        if failures or any(not r["terminal"] for r in trade.get("orders", [])):
            raise RuntimeError("Fermeture incomplete ou placement encore incertain ; journal conserve : " + "; ".join(failures))
        trade["closed_at"] = time.time()
        reports = await asyncio.gather(self.arcus.accounting(trade["arcus"], trade["opened_at"], trade["closed_at"]),
                                       self.lighter.accounting(trade["lighter"], trade["opened_at"], trade["closed_at"]),
                                       return_exceptions=True)
        pnl = {}
        for exchange, report in zip((self.arcus, self.lighter), reports):
            expected = sum((D(r.get("filled_size") or 0) for r in trade.get("orders", [])
                            if r["venue"] == exchange.name), D(0))
            if isinstance(report, BaseException) or D(report.get("filled_size", "0")) < expected:
                pnl[exchange.name] = {"price_pnl": None, "funding": None, "fees": None, "net_pnl": None,
                                      "status": "unavailable", "error": str(report) if isinstance(report, BaseException) else "Historique de fills incomplet"}
            else:
                pnl[exchange.name] = report
        net = sum((D(r["net_pnl"]) for r in pnl.values()), D(0)) if all(r["net_pnl"] is not None for r in pnl.values()) else None
        self.audit("pair_closed", symbol=symbol, reason=reason, pnl=pnl, net_pnl=net, trade=trade)
        self.state.setdefault("closed", []).append({**trade, "symbol": symbol, "pnl": pnl, "net_pnl": str(net) if net is not None else None})
        del self.state["active"][symbol]
        self.state["history"].pop(symbol, None)
        self.save()

    async def candidate_score(self, symbol, e, orientation, now):
        invalid_at = self.state.get("funding_invalid_at", {}).get(symbol)
        if invalid_at is not None and now - invalid_at < HISTORY_SECONDS:
            raise ValueError("Funding instable observe pendant les quatre dernieres heures")
        for timestamp, ar, lr in self.state["history"].get(symbol, []):
            if timestamp >= now - HISTORY_SECONDS:
                ar, lr = D(ar), D(lr)
                if (not ar.is_finite() or not lr.is_finite() or ar == lr
                        or direction(ar, lr) != orientation or relative_gap(ar, lr) < MIN_FUNDING_GAP):
                    raise ValueError("Historique observe incompatible avec le seuil de funding")
        start = int((now - HISTORY_SECONDS) // 3600) * 3600
        try:
            ah, lh = await asyncio.gather(self.arcus.funding_history(symbol, start, now),
                                          self.lighter.funding_history(e["name"], start, now))
        except Exception as exc:
            raise ValueError("Historique de funding indisponible ; entree bloquee") from exc
        score = funding_history_score(ah, lh, now, orientation)
        if score is None:
            raise ValueError("Ecart de funding non confirme sur les quatre dernieres heures")
        return score

    async def open_pair(self, symbol, a, e, orientation, quantity):
        score = await self.candidate_score(symbol, e, orientation, time.time())
        await asyncio.gather(self.arcus.set_leverage(a), self.lighter.set_leverage(e))
        # Relecture des taux ET des prix avant l'envoi, pas seulement des carnets.
        am, lm = await asyncio.gather(self.arcus.markets(), self.lighter.markets())
        fresh = match_markets(am, lm).get(symbol)
        if fresh is None:
            raise ValueError("Marche devenu indisponible avant entree")
        a, e = fresh
        ar, lr = D(a["nextFundingRate"]), D(e["fundingRate"])
        if direction(ar, lr) != orientation or relative_gap(ar, lr) < MIN_FUNDING_GAP:
            raise ValueError("Funding change avant entree")
        books = await asyncio.gather(self.arcus.book(symbol), self.lighter.book(e["name"]))
        quantity = min(quantity, entry_plan(a, e, books, orientation))
        if self.stop.is_set():
            return
        if (lr * D(e["markPrice"]) - ar * D(a["markPrice"])) * orientation <= 0:
            raise ValueError("Funding combine non positif avant entree")
        reason = (f"ecart de funding eligible : Arcus={ar}/h, Lighter={lr}/h ; "
                  f"ecart moyen 4 h={score}/h ; funding et liquidite valides")
        trade = {"arcus": symbol, "lighter": e["name"], "quantity": str(quantity),
                 "orientation": orientation, "phase": "opening", "opened_at": time.time(),
                 "entry_reason": reason, "orders": [], "market_info": {"arcus": a, "lighter": e}}
        self.state["active"][symbol] = trade
        self.save()
        try:
            # Lighter d'abord : une panne du hedge ne declenche aucun ordre Arcus.
            await self.place_order(self.lighter, trade["lighter"], -orientation, quantity, reason, trade=trade)
            q = (await self.lighter.positions()).get(trade["lighter"])
            if not q or q["size"] != -quantity * orientation:
                raise RuntimeError("Quantites executees non neutres : fill Lighter incomplet")
            if risk_reason(q):
                raise RuntimeError("Marge Lighter insuffisante apres ouverture")
            if self.stop.is_set():
                raise RuntimeError("Arret demande pendant ouverture")
            await self.place_order(self.arcus, trade["arcus"], orientation, quantity, reason, trade=trade)
            ap, lp = await self.positions()
            p, q = ap.get(trade["arcus"]), lp.get(trade["lighter"])
            if not p or not q or p["size"] != quantity * orientation or q["size"] != -quantity * orientation:
                raise RuntimeError("Quantites executees non neutres")
            if risk_reason(p) or risk_reason(q):
                raise RuntimeError("Marge de securite insuffisante apres ouverture")
        except Exception:
            await self.close_pair(symbol, "hedge impossible ou remplissage incomplet")
            raise
        trade["phase"] = "open"
        self.save()
        self.audit("pair_opened", symbol=symbol, reason=reason, quantity=quantity,
                   arcus_side=orientation, lighter_side=-orientation)

    async def monitor(self):
        ap, ep = await self.positions()
        expected_a = {t["arcus"] for t in self.state["active"].values()}
        expected_e = {t["lighter"] for t in self.state["active"].values()}
        if set(ap) - expected_a or set(ep) - expected_e:
            raise RuntimeError("Positions externes détectées : comptes dédiés requis")
        for symbol, trade in list(self.state["active"].items()):
            p, q = ap.get(trade["arcus"]), ep.get(trade["lighter"])
            reason = risk_reason(p) or risk_reason(q)
            if trade["phase"] != "open":
                reason = "opération interrompue à terminer"
            elif p and q and (p["size"] + q["size"] != 0 or p["size"] * trade["orientation"] <= 0):
                reason = "couverture delta perdue"
            if reason:
                await self.close_pair(symbol, reason)
        if len(self.state["active"]) > MAX_PAIRS:
            ranked = sorted(self.state["active"], key=lambda symbol:
                            funding_score(self.state["history"].get(symbol, []), time.time()) or 0)
            for symbol in ranked[:len(self.state["active"]) - MAX_PAIRS]:
                await self.close_pair(symbol, "réduction du nombre maximal de paires")

    async def scan(self):
        am, em = await asyncio.gather(self.arcus.markets(), self.lighter.markets())
        self.pairs = match_markets(am, em)
        if not self.pairs:
            raise RuntimeError("Aucun contrat stock/commodity commun aux deux plateformes")
        now = time.time()
        ranked = []
        for symbol, (a, e) in self.pairs.items():
            # L'adaptateur Lighter a converti le pourcentage horaire en fraction.
            ar, er = D(a["nextFundingRate"]), D(e["fundingRate"])
            if not ar.is_finite() or not er.is_finite():
                raise ValueError("Funding non fini")
            samples = self.state["history"].setdefault(symbol, [])
            if (ar == er or relative_gap(ar, er) < MIN_FUNDING_GAP
                    or samples and direction(D(samples[-1][1]), D(samples[-1][2])) != direction(ar, er)):
                self.state.setdefault("funding_invalid_at", {})[symbol] = now
            update_history(samples, now, ar, er)
            trade = self.state["active"].get(symbol)
            if trade:
                # À quantité égale, les paiements portent sur les notionnels
                # propres à chaque plateforme, qui peuvent diverger en cours de vie.
                gain_per_unit = (er * D(e["markPrice"])
                                 - ar * D(a["markPrice"])) * trade["orientation"]
                if gain_per_unit <= 0:
                    await self.close_pair(symbol, "funding combiné nul ou inversé")
                continue
            if samples:
                orientation = direction(ar, er)
                try:
                    score = await self.candidate_score(symbol, e, orientation, now)
                except ValueError as exc:
                    log.info("Ignore %s : %s", symbol, exc)
                    continue
                ranked.append((score, symbol, orientation, a, e))
        for symbol in list(self.state["history"]):
            if symbol not in self.pairs:
                del self.state["history"][symbol]
        for symbol in list(self.state["active"]):
            if symbol not in self.pairs:
                await self.close_pair(symbol, "marché ou funding devenu indisponible")
        self.save()
        log.info("%s RWA communs ; %s paires ouvertes ; %s candidats avec historique de 4 h valide",
                 len(self.pairs), len(self.state["active"]), len(ranked))
        for score, symbol, orientation, a, e in sorted(ranked, reverse=True, key=lambda x: x[0]):
            if len(self.state["active"]) >= MAX_PAIRS or self.stop.is_set():
                break
            try:
                books = await asyncio.gather(self.arcus.book(symbol), self.lighter.book(e["name"]))
                quantity = entry_plan(a, e, books, orientation)
            except ValueError as exc:
                log.info("Ignoré %s : %s", symbol, exc)
                continue
            log.info("Candidat %s : ecart absolu moyen sur 4 h %.6g/h", symbol, score)
            try:
                await self.open_pair(symbol, a, e, orientation, quantity)
            except ValueError as exc:
                log.info("Ignore %s : %s", symbol, exc)

    async def run(self):
        next_scan = 0
        try:
            await asyncio.gather(self.arcus.markets(), self.lighter.markets())
            # Reconcile journal-owned uncertain orders before rejecting external orders.
            await self.monitor()
            await asyncio.gather(self.arcus.check_orders(), self.lighter.check_orders())
            while not self.stop.is_set():
                await self.monitor()
                if time.monotonic() >= next_scan:
                    await self.scan()
                    next_scan = time.monotonic() + SCAN_SECONDS
                try:
                    await asyncio.wait_for(self.stop.wait(), MONITOR_SECONDS)
                except TimeoutError:
                    pass
        finally:
            for symbol in list(self.state["active"]):
                try:
                    await self.close_pair(symbol, "arrêt du bot")
                except Exception:
                    log.exception("URGENT : vérifier manuellement les deux jambes de %s", symbol)


async def main():
    if POSITION_USD <= 0 or MAX_PAIRS < 1 or MIN_FUNDING_GAP <= 0 or MAX_SPREAD <= 0:
        raise ValueError("Paramètres de stratégie invalides")
    async with Http() as http:
        lighter = Lighter(http)
        farmer = Farmer(Arcus(http), lighter)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, farmer.stop.set)
            except NotImplementedError:
                signal.signal(sig, lambda *_: loop.call_soon_threadsafe(farmer.stop.set))
        try:
            await farmer.run()
        finally:
            await lighter.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    asyncio.run(main())
