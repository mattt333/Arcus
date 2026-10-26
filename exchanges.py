"""Accès Lighter Robinhood Chain pour le market making XAU."""
import asyncio
import os
import time
from decimal import Decimal as D

import aiohttp

TIMEOUT = 10
ORDER_TIMEOUT = 10
MARKET = "XAU-USD"
MARGIN_MULTIPLIER = D("1.5")
TERMINAL = {"FILLED", "CANCELED", "REJECTED", "EXPIRED"}


class ApiError(RuntimeError):
    pass


class OrderRejected(ApiError):
    """Refus explicite avant acceptation : aucun ordre à annuler."""


class UncertainOrder(RuntimeError):
    """Un placement inconnu doit être réconcilié, jamais renvoyé."""


def decimal(value, positive=False):
    value = D(str(value))
    if not value.is_finite() or (positive and value <= 0):
        raise ValueError("Valeur numérique invalide")
    return value


class Http:
    async def __aenter__(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT))
        return self

    async def __aexit__(self, *args):
        await self.session.close()

    async def get(self, url, params, headers=None):
        try:
            async with self.session.get(url, params=params, headers=headers) as response:
                data = await response.json(content_type=None)
                if (response.status >= 400 or not isinstance(data, dict)
                        or data.get("code", 200) != 200 or data.get("error")):
                    raise ApiError(f"Lighter HTTP {response.status} : réponse refusée")
                return data
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise ApiError("Lecture Lighter indisponible") from exc


class Lighter:
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
        self.lock = asyncio.Lock()
        self.market = None
        self.identity = f"{self.base}/{self.CHAIN_ID}/{self.account}"

    def client(self):
        if self.signer is None:
            import lighter
            self.signer = lighter.SignerClient(url=self.base, account_index=self.account,
                api_private_keys={self.key_index: self.private_key}, chain_id=self.CHAIN_ID)
        return self.signer

    async def close(self):
        if self.signer is not None:
            await self.signer.close()

    async def get(self, path, private=False, **params):
        headers = None
        if private:
            token, error = self.client().create_auth_token_with_expiry(api_key_index=self.key_index)
            if error:
                raise ApiError("Authentification Lighter impossible")
            headers = {"Authorization": token}
        return await self.http.get(self.base + "/api/v1/" + path, params, headers)

    async def load_market(self):
        data = await self.get("orderBookDetails", filter="perp")
        rows = [row for row in data["order_book_details"]
                if row["symbol"] == "XAU" and row["market_type"] == "perp"]
        if len(rows) != 1:
            raise ValueError("Contrat perpétuel XAU introuvable ou ambigu")
        row = rows[0]
        if (row["status"] != "active" or row.get("is_frozen")
                or row.get("market_config", {}).get("force_reduce_only")
                or decimal(row["multiplier"]) != 1 or decimal(row["quote_multiplier"]) != 1):
            raise ValueError("Marché XAU indisponible ou contrat incompatible")
        self.market = {**row, "name": MARKET,
            "step": D(10) ** -int(row["supported_size_decimals"]),
            "tick": D(10) ** -int(row["supported_price_decimals"])}
        return self.market

    async def set_leverage(self):
        fraction = decimal(self.market["min_initial_margin_fraction"], positive=True) / 10000
        leverage = max(1, int(1 / (fraction * MARGIN_MULTIPLIER)))
        client = self.client()
        async with self.lock, asyncio.timeout(TIMEOUT):
            _, response, error = await client.update_leverage(market_index=self.market["market_id"],
                margin_mode=client.ISOLATED_MARGIN_MODE, leverage=leverage)
        if error or not response or response.code != 200:
            raise ApiError("Configuration de marge Lighter refusée")
        expected = D(10000 // leverage) / 100
        deadline = time.monotonic() + ORDER_TIMEOUT
        while time.monotonic() < deadline:
            if any(row["market_id"] == self.market["market_id"]
                   and row["margin_mode"] == client.ISOLATED_MARGIN_MODE
                   and decimal(row["initial_margin_fraction"]) == expected
                   for row in await self.account_rows()):
                return
            await asyncio.sleep(0.1)
        raise ApiError("Levier et marge isolée Lighter non confirmés")

    async def account_rows(self):
        result, cursor, seen = [], None, set()
        while True:
            data = await self.get("account", by="index", value=str(self.account),
                                  **({"cursor": cursor} if cursor else {}))
            account = next((r for r in data["accounts"] if int(r["index"]) == self.account), None)
            if account is None:
                raise ApiError("Compte Lighter introuvable")
            result.extend(account["positions"])
            cursor = data.get("next_cursor")
            if not cursor:
                return result
            if cursor in seen:
                raise ApiError("Pagination du compte Lighter bloquée")
            seen.add(cursor)

    async def position(self):
        size, entry = D(0), D(0)
        for row in await self.account_rows():
            amount = decimal(row["position"])
            if not amount:
                continue
            if row["market_id"] != self.market["market_id"]:
                raise ValueError("Position externe détectée : compte dédié XAU requis")
            sign = int(row["sign"])
            if amount < 0 or sign not in (-1, 1):
                raise ValueError("Position XAU invalide")
            size, entry = amount * sign, decimal(row["avg_entry_price"], positive=True)
        return {"size": size, "entry": entry}

    async def book(self):
        data = await self.get("orderBookOrders", market_id=self.market["market_id"], limit=250)
        result = {}
        for side in ("bids", "asks"):
            levels, external = {}, {}
            for row in data[side]:
                price = decimal(row["price"], positive=True)
                quantity = decimal(row["remaining_base_amount"])
                if quantity < 0:
                    raise ValueError("Quantité du carnet négative")
                if not quantity:
                    continue
                levels[price] = levels.get(price, D(0)) + quantity
                if int(row["owner_account_index"]) != self.account:
                    external[price] = external.get(price, D(0)) + quantity
            result[side] = sorted(levels.items(), reverse=side == "bids")
            result["external_" + side] = external
        if not result["bids"] or not result["asks"] or result["bids"][0][0] >= result["asks"][0][0]:
            raise ValueError("Carnet vide ou croisé")
        return result

    @staticmethod
    def normalize(row):
        status = row["status"]
        return {**row, "client_id": str(row["client_order_index"]),
                "status": "CANCELED" if status.startswith("canceled") else status.upper(),
                "filled": str(decimal(row["filled_base_amount"]))}

    async def active_orders(self):
        data = await self.get("accountActiveOrders", private=True, account_index=self.account)
        return [self.normalize(row) for row in data["orders"]]

    async def order_status(self, client_id):
        for row in await self.active_orders():
            if row["client_id"] == client_id:
                return row
        cursor, seen = None, set()
        while True:
            data = await self.get("accountInactiveOrders", private=True, account_index=self.account,
                market_id=self.market["market_id"], limit=100,
                **({"cursor": cursor} if cursor else {}))
            for row in data["orders"]:
                if str(row["client_order_index"]) == client_id:
                    return self.normalize(row)
            cursor = data.get("next_cursor")
            if not cursor:
                return None
            if cursor in seen:
                raise ApiError("Pagination des ordres bloquée")
            seen.add(cursor)

    async def confirm(self, client_id, terminal=False):
        deadline = time.monotonic() + ORDER_TIMEOUT
        row = None
        while time.monotonic() < deadline:
            row = await self.order_status(client_id)
            if row and (row["status"] in TERMINAL or
                        (not terminal and row["status"] == "OPEN")):
                return row
            await asyncio.sleep(0.1)
        status = row["status"] if row else "absent des ordres ouverts et de l'historique"
        raise UncertainOrder(f"Ordre {client_id} non confirmé : {status}")

    @staticmethod
    def units(value, step):
        units = decimal(value, positive=True) / step
        if units != units.to_integral_value():
            raise ValueError("Prix ou quantité XAU non aligné")
        return int(units)

    async def place(self, order):
        """Le journal de l'appelant contient déjà l'identifiant avant l'envoi."""
        client = self.client()
        kind = order["kind"]
        args = dict(market_index=self.market["market_id"], client_order_index=int(order["id"]),
            base_amount=self.units(order["quantity"], self.market["step"]),
            price=self.units(order["price"], self.market["tick"]),
            is_ask=order["side"] == -1, reduce_only=order["reduce_only"])
        if kind == "quote":
            args.update(order_type=client.ORDER_TYPE_LIMIT,
                time_in_force=client.ORDER_TIME_IN_FORCE_POST_ONLY,
                order_expiry=int(time.time() * 1000) + 60_000)
        elif kind == "close":
            args.update(order_type=client.ORDER_TYPE_MARKET,
                time_in_force=client.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL,
                order_expiry=client.DEFAULT_IOC_EXPIRY)
        else:
            raise ValueError("Type d'ordre inconnu")
        try:
            async with self.lock, asyncio.timeout(TIMEOUT):
                transaction, response, error = await client.create_order(**args)
        except Exception as exc:
            # Même un timeout peut cacher un ordre accepté : ne jamais le renvoyer.
            order["submission"] = {"state": "unknown", "reason": f"{type(exc).__name__}: {exc}"}
        else:
            if response is not None and response.code != 200:
                reason = f"code={response.code}, message={getattr(response, 'message', None)}"
                order["submission"] = {"state": "rejected", "reason": reason}
                raise OrderRejected(f"Ordre {order['id']} refusé par Lighter : {reason}")
            if error and transaction is None and response is None:
                # Le SDK renvoie ce triplet si la signature échoue ou si l'API
                # répond BadRequest. Il ne faut pas attendre un ordre inexistant.
                order["submission"] = {"state": "rejected", "reason": str(error)}
                raise OrderRejected(f"Ordre {order['id']} refusé par le SDK/Lighter : {error}")
            if response is not None and response.code == 200:
                order["submission"] = {"state": "accepted",
                    "tx_hash": getattr(response, "tx_hash", None)}
            else:
                order["submission"] = {"state": "unknown", "reason": str(error or "réponse SDK vide")}
        try:
            return await self.confirm(order["id"], terminal=kind == "close")
        except Exception as exc:
            raise UncertainOrder(
                f"Ordre {order['id']} non confirmé ; envoi={order['submission']} ; {exc}") from exc

    async def cancel(self, order):
        row = await self.order_status(order["id"])
        if row and row["status"] in TERMINAL:
            return row
        client = self.client()
        index = int(row["order_index"]) if row else int(order["id"])
        try:
            async with self.lock, asyncio.timeout(TIMEOUT):
                _, response, error = await client.cancel_order(
                    market_index=self.market["market_id"], order_index=index)
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
        else:
            reason = str(error or (f"code={response.code}, message={getattr(response, 'message', None)}"
                                  if response is not None else "réponse SDK vide"))
        try:
            return await self.confirm(order["id"], terminal=True)
        except Exception as exc:
            raise UncertainOrder(
                f"Annulation {order['id']} non confirmée ; réponse SDK/Lighter : {reason} ; {exc}") from exc
