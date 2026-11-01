"""Final-3 staged partial exits; the reversal variant lives in cascade_final3_reversal.py."""

import argparse
import asyncio
from pathlib import Path

from binance_cascade_hunter_dynamic_v2_paper import V2PaperHunter
from binance_cascade_hunter_dynamic_v2 import log
from cascade_final3_reversal import Final2PaperHunter as ReversalHunter

HARD_STOP_PCT = 0.028
PARTIAL_LEVEL_1 = 0.012
PARTIAL_FRAC_1 = 0.40
PARTIAL_LEVEL_2 = 0.020
PARTIAL_FRAC_2 = 0.50

PROFIT_LOCK_LEVELS = [
    (0.010, 0.000),
    (0.015, 0.005),
    (0.020, 0.010),
    (0.025, 0.015),
    (0.030, 0.020),
    (0.035, 0.025),
    (0.040, 0.030),
]


class Final2PaperHunter(ReversalHunter):
    def __init__(
        self,
        state_path="paper_state_final3.json",
        fee_rate=0.0005,
        reset=False,
        market_data=None,
    ):
        if Path(state_path).exists() and not reset:
            from migrate_idle_state import migrate

            migrate(state_path)
        V2PaperHunter.__init__(self, state_path, fee_rate, reset, market_data)

    def settings(self):
        return {
            **V2PaperHunter.settings(self),
            "mode": "final3_staged_partial",
            "STOP_LOSS_PCT": HARD_STOP_PCT,
            "TAKE_PROFIT_PCT": None,
            "hard_stop_pct": HARD_STOP_PCT,
            "hedge_positions": False,
            "partial_level_1": PARTIAL_LEVEL_1,
            "partial_frac_1": PARTIAL_FRAC_1,
            "partial_level_2": PARTIAL_LEVEL_2,
            "partial_frac_2": PARTIAL_FRAC_2,
            "partial_stop_1": 0.020,
            "partial_stop_2": 0.013,
            "profit_lock_levels": [list(level) for level in PROFIT_LOCK_LEVELS],
        }

    def position(self, key, symbol, fill):
        p = super().position(key, symbol, fill)
        entry = p["entry"]
        side = p["side"]
        p.update(
            original_entry=entry,
            original_notional=entry * fill["amount"],
            initial_sl=entry
            * (1 - HARD_STOP_PCT if side == "buy" else 1 + HARD_STOP_PCT),
            entry=entry,
            amount=fill["amount"],
            partial_1_done=False,
            partial_2_done=False,
            profit_locked=0.0,
            stop_kind="SL",
        )
        p["sl"] = p["initial_sl"]
        return p

    async def enter(self, symbol, side, price):
        # Shared entry uses this variant's position factory and actual fill price.
        return await super().enter(symbol, side, price)

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

                # ===== 2. الإغلاق الجزئي المتدرج (الذكي) =====
                if not p.get("partial_1_done") and adverse >= PARTIAL_LEVEL_1:
                    await self.partial_close(symbol, PARTIAL_FRAC_1, price, "PARTIAL_1")
                    p["partial_1_done"] = True
                    new_sl = p["entry"] * (1 - direction * 0.020)
                    self.tighten_stop(p, new_sl, "TIGHT_1")
                    if symbol not in self.positions:
                        continue

                if not p.get("partial_2_done") and adverse >= PARTIAL_LEVEL_2:
                    await self.partial_close(symbol, PARTIAL_FRAC_2, price, "PARTIAL_2")
                    p["partial_2_done"] = True
                    new_sl = p["entry"] * (1 - direction * 0.013)
                    self.tighten_stop(p, new_sl, "TIGHT_2")
                    if symbol not in self.positions:
                        continue

                # ===== 3. نظام جني الأرباح المتحرك =====
                for trigger, lock in PROFIT_LOCK_LEVELS:
                    if move >= trigger and p.get("profit_locked", 0) < lock:
                        new_sl = p["entry"] * (1 + direction * lock)
                        self.tighten_stop(p, new_sl, "PROFIT_LOCK")
                        p["profit_locked"] = lock
                        log.info("PROFIT LOCK %s | +%.1f%% locked", symbol, lock * 100)

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
        if amount <= 0 or amount >= p["amount"]:
            return False
        try:
            side = "sell" if p["side"] == "buy" else "buy"
            await self.exchange.create_order(
                symbol,
                "market",
                side,
                amount,
                params={"reduceOnly": True, "exit_reason": reason},
            )
            p["amount"] = self.exchange.positions[symbol]["amount"]
            log.info(
                "%s %s | closed %.0f%% @ %.6f | remaining=%.4f",
                reason,
                symbol,
                fraction * 100,
                price,
                p["amount"],
            )
            self.save()
            return True
        except Exception as e:
            log.warning("Partial close failed %s: %s", symbol, e)
            return False

    async def try_open_reversal(self, *args, **kwargs):
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", default="paper_state_final3.json")
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()
    bot = Final2PaperHunter(args.state, reset=args.reset)
    try:
        asyncio.run(bot.start())
    except KeyboardInterrupt:
        log.info("Final-3 staged partial stopped; state saved")


if __name__ == "__main__":
    main()
