"""Paper-only account for the approved Final-3 strategy; no private trading endpoints."""

import asyncio
import json
import math
import time
from datetime import datetime, timezone
import config as strategy

MAX_TICKER_AGE = 10.0
TICKER_TIMEOUT = 10.0


class PaperExchange:
    def __init__(self, market_data, fee_rate=0.0005, leverage=None):
        self.market_data = market_data
        self.fee_rate = fee_rate
        self.leverage = strategy.LEVERAGE if leverage is None else leverage
        self.balance = 100.0
        self.positions = {}
        self.prices = {}
        self.trades = []

    @property
    def used(self):
        return sum(p["margin"] for p in self.positions.values())

    @property
    def free(self):
        return max(0.0, self.balance - self.used)

    @property
    def equity(self):
        return self.balance + sum(
            (self.prices.get(s, p["entry"]) - p["entry"])
            * p["amount"]
            * (1 if p["side"] == "buy" else -1)
            for s, p in self.positions.items()
        )

    async def load_markets(self):
        return await self.market_data.load_markets()

    async def fetch_tickers(self):
        return await self.market_data.fetch_tickers()

    async def fetch_ohlcv(self, symbol, timeframe, limit):
        return await self.market_data.fetch_ohlcv(symbol, timeframe, limit=limit)

    async def fetch_order_book(self, symbol, limit):
        return await self.market_data.fetch_order_book(symbol, limit=limit)

    def amount_to_precision(self, symbol, amount):
        return self.market_data.amount_to_precision(symbol, amount)

    async def fetch_ticker(self, symbol):
        ticker = await asyncio.wait_for(
            self.market_data.fetch_ticker(symbol), TICKER_TIMEOUT
        )
        timestamp = ticker.get("timestamp")
        if timestamp is None:
            raise ValueError("Missing market timestamp; paper execution rejected")
        age = time.time() - float(timestamp) / 1000
        if not math.isfinite(age) or abs(age) > MAX_TICKER_AGE:
            raise ValueError(
                f"Stale market price ({age:.1f}s); paper execution rejected"
            )
        price = float(ticker["last"])
        if not math.isfinite(price) or price <= 0:
            raise ValueError("Invalid market price")
        self.prices[symbol] = price
        return ticker

    async def create_order(self, symbol, type, side, amount, params=None):
        params = params or {}
        if type != "market" or side not in ("buy", "sell"):
            raise ValueError("Invalid paper order")
        amount = float(amount)
        if not math.isfinite(amount) or amount <= 0:
            raise ValueError("Invalid amount")
        if params.get("positionId", symbol) != symbol:
            raise ValueError("Final-3 uses one position per market")
        closing = bool(params.get("reduceOnly"))
        position = self.positions.get(symbol)
        if closing:
            if (
                not position
                or position["side"] == side
                or (
                    amount > position["amount"]
                    and not math.isclose(amount, position["amount"])
                )
            ):
                raise ValueError("Invalid reduce-only close")
        elif position:
            raise ValueError("Position already exists")
        full_close = bool(closing and math.isclose(amount, position["amount"]))
        if full_close:
            amount = position["amount"]
        ticker = await self.fetch_ticker(symbol)
        price = float(ticker["last"])
        if not closing and "quoteNotional" in params:
            quote = float(params["quoteNotional"])
            if not math.isfinite(quote) or quote <= 0:
                raise ValueError("Invalid quote notional")
            amount = float(self.amount_to_precision(symbol, quote / price))
            if not math.isfinite(amount) or amount <= 0:
                raise ValueError("Invalid rounded amount")
        notional = amount * price
        fee = notional * self.fee_rate
        pnl = 0.0
        if closing:
            pnl = (
                (price - position["entry"])
                * amount
                * (1 if position["side"] == "buy" else -1)
            )
            fees_paid = (
                position.get(
                    "fees_paid", position["entry"] * position["amount"] * self.fee_rate
                )
                + fee
            )
            realized_pnl = position.get("realized_pnl", 0) + pnl
            released_margin = position["margin"] * amount / position["amount"]
            self.balance += pnl - fee
            if full_close:
                del self.positions[symbol]
            else:
                self.positions[symbol] = {
                    **position,
                    "amount": position["amount"] - amount,
                    "margin": position["margin"] - released_margin,
                    "fees_paid": fees_paid,
                    "realized_pnl": realized_pnl,
                }
        else:
            limits = self.market_data.market(symbol).get("limits", {})
            for key, value in [("amount", amount), ("cost", notional)]:
                bounds = limits.get(key) or {}
                if bounds.get("min") is not None and value < bounds["min"]:
                    raise ValueError(f"Below market minimum {key}")
                if bounds.get("max") is not None and value > bounds["max"]:
                    raise ValueError(f"Above market maximum {key}")
            margin = notional / self.leverage
            if margin + fee > self.free:
                raise ValueError("Insufficient virtual margin including entry fee")
            self.balance -= fee
            self.positions[symbol] = dict(
                symbol=symbol,
                position_id=symbol,
                side=side,
                entry=price,
                amount=amount,
                margin=margin,
                fees_paid=fee,
                realized_pnl=0.0,
            )
        trade = dict(
            id=f"paper-{len(self.trades)+1}",
            timestamp=datetime.now(timezone.utc).isoformat(),
            symbol=symbol,
            position_id=symbol,
            side=side,
            amount=amount,
            price=price,
            market_timestamp=ticker["timestamp"],
            price_source="binance_usdm_last",
            reduceOnly=closing,
            fee_usdt=fee,
            pnl_usdt=pnl,
            balance_usdt=self.balance,
        )
        if closing:
            trade.update(
                partial_close=not full_close,
                exit_reason=params.get("exit_reason"),
                remaining_amount=(
                    0.0 if full_close else self.positions[symbol]["amount"]
                ),
                released_margin_usdt=released_margin,
            )
            if full_close:
                trade["position_net_pnl_usdt"] = realized_pnl - fees_paid
        self.trades.append(trade)
        strategy.log.info(
            "PAPER FILL %s | %s %s | balance=%.4f",
            trade["id"],
            side,
            symbol,
            self.balance,
        )
        return {**trade, "average": price, "filled": amount, "status": "closed"}

    async def close(self):
        await self.market_data.close()


class PaperState:
    def restore(self):
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        if (
            state["version"] != 1
            or state["settings"] != self.settings()
            or state["fee_rate"] != self.exchange.fee_rate
        ):
            raise ValueError("State settings differ; use a new --state path or --reset")
        wallet = float(state["balance_usdt"])
        if not math.isfinite(wallet):
            raise ValueError("Invalid saved wallet")
        self.exchange.balance = wallet
        self.exchange.positions = state["account_positions"]
        self.exchange.prices = state["prices"]
        self.exchange.trades = state["trades"]
        self.positions = state["strategy_positions"]
        self.last_trade = state["last_trade"]
        if set(self.positions) != set(self.exchange.positions):
            raise ValueError("Inconsistent saved positions")

    def save(self):
        state = {
            "version": 1,
            "initial_balance_usdt": 100.0,
            "settings": self.settings(),
            "fee_rate": self.exchange.fee_rate,
            "balance_usdt": self.exchange.balance,
            "equity_usdt": self.exchange.equity,
            "free_usdt": self.exchange.free,
            "used_usdt": self.exchange.used,
            "account_positions": self.exchange.positions,
            "strategy_positions": self.positions,
            "prices": self.exchange.prices,
            "last_trade": self.last_trade,
            "trades": self.exchange.trades,
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(state, ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
        temporary.replace(self.state_path)

    async def report(self):
        while True:
            strategy.log.info(
                "PAPER | wallet=%.4f equity=%.4f free=%.4f used=%.4f positions=%d",
                self.exchange.balance,
                self.exchange.equity,
                self.exchange.free,
                self.exchange.used,
                len(self.positions),
            )
            await asyncio.sleep(30)
