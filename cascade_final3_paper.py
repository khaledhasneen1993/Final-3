"""Final-3 approved strategy: 2.8% hard stop and staged partial exits."""

import argparse
import asyncio
import fcntl
import math
import time
from pathlib import Path
import config
from config import (
    HARD_STOP_PCT,
    PARTIAL_LEVEL_1,
    PARTIAL_FRAC_1,
    PARTIAL_LEVEL_2,
    PARTIAL_FRAC_2,
    PROFIT_LOCK_LEVELS,
    log,
)
from paper_runtime import PaperHunter
from risk_controls import RiskControls
from paper_account import PartialStopCrossed


class Final3PaperHunter(RiskControls, PaperHunter):
    def __init__(
        self,
        state_path="paper_state_final3.json",
        fee_rate=0.0005,
        reset=False,
        market_data=None,
    ):
        if Path(state_path).exists() and not reset:
            from migrate_state import migrate

            migrate(state_path)
        super().__init__(state_path, fee_rate, reset, market_data)

    def has_market_position(self, symbol):
        return any(p.get("symbol", key) == symbol for key, p in self.positions.items())

    def position(self, key, symbol, fill):
        entry = fill.get("entry", fill.get("price"))
        side = fill["side"]
        initial_sl = entry * (1 - HARD_STOP_PCT if side == "buy" else 1 + HARD_STOP_PCT)
        return dict(
            symbol=symbol,
            position_id=key,
            side=side,
            entry=entry,
            amount=fill["amount"],
            original_entry=entry,
            original_notional=entry * fill["amount"],
            initial_sl=initial_sl,
            sl=initial_sl,
            tp=None,
            partial_1_done=False,
            partial_2_done=False,
            profit_locked=0.0,
            stop_kind="SL",
        )

    async def enter(self, symbol, side, price):
        now = time.time()
        if not self.entry_permitted(symbol):
            return False
        if (
            self.has_market_position(symbol)
            or len(self.positions) >= self.strategy.MAX_CONCURRENT_POSITIONS
        ):
            return False
        if now - self.last_trade.get(symbol, -math.inf) < self.strategy.COOLDOWN_SEC:
            return False
        try:
            quote = (
                max(0.0, self.exchange.balance)
                * self.strategy.MAX_POSITION_PCT
                * self.strategy.LEVERAGE
            )
            fill = await self.exchange.create_order(
                symbol,
                "market",
                side,
                1,
                params={
                    "quoteNotional": quote,
                    "positionId": symbol,
                    **self.guarded_entry_params(price),
                },
            )
            self.positions[symbol] = self.position(symbol, symbol, fill)
            self.last_trade[symbol] = now
            self.exchange.trades[-1].update(
                entry_reason="CASCADE_MOVE_VOLUME_ORDERBOOK",
                stop_loss_price=self.positions[symbol]["sl"],
            )
            self.save()
            return True
        except Exception as error:
            log.warning("Entry failed %s: %s", symbol, error)
            return False

    async def close_position(self, position_id, reason, trigger_price):
        p = self.positions.get(position_id)
        if p is None:
            return False
        symbol = p.get("symbol", position_id)
        await self.exchange.create_order(
            symbol,
            "market",
            "sell" if p["side"] == "buy" else "buy",
            p["amount"],
            params={
                "reduceOnly": True,
                "positionId": position_id,
                "exit_reason": reason,
                "stopPrice": (
                    p["sl"]
                    if reason
                    in ("SL", "TIGHT_1", "TIGHT_2", "TIGHT_AFTER_REV", "PROFIT_LOCK")
                    else None
                ),
            },
        )
        self.exchange.trades[-1]["trigger_price"] = float(trigger_price)
        self.last_trade[symbol] = time.time()
        del self.positions[position_id]
        self.save()
        log.info("EXIT %s %s", reason, symbol)
        return True

    @staticmethod
    def tighten_stop(p, candidate, kind):
        improves = candidate > p["sl"] if p["side"] == "buy" else candidate < p["sl"]
        if improves:
            p["sl"] = candidate
            p["stop_kind"] = kind

    async def manage_exits(self):
        for symbol, p in list(self.positions.items()):
            try:
                price = float((await self.exchange.fetch_ticker(symbol))["last"])
                direction = 1 if p["side"] == "buy" else -1
                move = direction * (price / p["entry"] - 1)
                adverse = -move

                # ===== 1. Hard Stop النهائي =====
                if direction * (price - p["sl"]) <= 0:
                    await self.close_position(symbol, p.get("stop_kind", "SL"), price)
                    continue

                # ===== 2. الإغلاق الجزئي المتدرج =====
                # نعمل الإغلاقات الجزئية أولاً قبل سحب الوقف
                if not p.get("partial_1_done") and adverse >= PARTIAL_LEVEL_1:
                    success = await self.partial_close(
                        symbol, PARTIAL_FRAC_1, price, "PARTIAL_1"
                    )
                    if success:
                        p["partial_1_done"] = True
                        # سحب الوقف لمستوى أبعد شوي من مستوى الجزئي الثاني
                        new_sl = p["entry"] * (1 - direction * 0.024)
                        self.tighten_stop(p, new_sl, "TIGHT_1")
                    if symbol not in self.positions:
                        continue

                if not p.get("partial_2_done") and adverse >= PARTIAL_LEVEL_2:
                    success = await self.partial_close(
                        symbol, PARTIAL_FRAC_2, price, "PARTIAL_2"
                    )
                    if success:
                        p["partial_2_done"] = True
                        # وقف على مسافة 0.3% من السعر الحالي، مع الحفاظ على وقف أقرب.
                        new_sl = price * (1 - direction * 0.003)
                        self.tighten_stop(p, new_sl, "TIGHT_2")
                    if symbol not in self.positions:
                        continue

                # ===== 3. نظام جني الأرباح المتحرك =====
                for trigger, lock in PROFIT_LOCK_LEVELS:
                    if move >= trigger and p.get("profit_locked", 0) < lock:
                        new_sl = self.fee_aware_stop(p, lock)
                        if direction * (price - new_sl) <= 0:
                            continue
                        self.tighten_stop(p, new_sl, "PROFIT_LOCK")
                        p["profit_locked"] = direction * (p["sl"] / p["entry"] - 1)
                        log.info(
                            "PROFIT LOCK %s | actual=%.3f%% | stop=%.8f",
                            symbol,
                            p["profit_locked"] * 100,
                            p["sl"],
                        )

                self.save()
            except Exception as error:
                log.warning("Position management failed %s: %s", symbol, error)
                self.save()

    async def partial_close(self, symbol, fraction, price, reason):
        p = self.positions.get(symbol)
        if not p:
            return False
        amount = float(
            self.exchange.amount_to_precision(symbol, p["amount"] * fraction)
        )
        if amount <= 0 or amount >= p["amount"] * 0.98:
            return False
        try:
            side = "sell" if p["side"] == "buy" else "buy"
            await self.exchange.create_order(
                symbol,
                "market",
                side,
                amount,
                params={
                    "reduceOnly": True,
                    "exit_reason": reason,
                    "partialStop": p["sl"],
                    "adverseEntry": p["entry"],
                    "adverseLevel": (
                        PARTIAL_LEVEL_1
                        if reason == "PARTIAL_1"
                        else PARTIAL_LEVEL_2 if reason == "PARTIAL_2" else None
                    ),
                },
            )
            # تحديث الكمية من الحساب الفعلي
            if symbol in self.exchange.positions:
                p["amount"] = self.exchange.positions[symbol]["amount"]
            else:
                # الصفقة اتقفلت بالكامل
                if symbol in self.positions:
                    del self.positions[symbol]
            log.info(
                "%s %s | closed %.0f%% @ %.6f", reason, symbol, fraction * 100, price
            )
            self.save()
            return True
        except PartialStopCrossed as error:
            await self.close_position(symbol, p.get("stop_kind", "SL"), error.price)
            return True
        except Exception as e:
            log.warning("Partial close failed %s: %s", symbol, e)
            return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=Path("paper_state_final3.json"))
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()
    args.state.parent.mkdir(parents=True, exist_ok=True)
    with args.state.with_name(args.state.name + ".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.exit(1, "Final-3 is already running on this state.\n")
        bot = Final3PaperHunter(args.state, reset=args.reset)
        try:
            asyncio.run(bot.start())
        except KeyboardInterrupt:
            log.info("Final-3 stopped; state saved")


if __name__ == "__main__":
    main()
