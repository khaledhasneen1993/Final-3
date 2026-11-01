"""Characterize the requested staged-partial rules, including their stated limitations."""

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
from cascade_final2_paper import Final2PaperHunter
from fixtures import DynamicPublicData

S = "BTC/USDT:USDT"


class PartialTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = DynamicPublicData()
        self.path = Path(self.tmp.name) / "state.json"
        self.bot = Final2PaperHunter(self.path, market_data=self.data)

    async def entry(self, side="buy"):
        self.data.price = 100
        self.assertTrue(await self.bot.enter(S, side, 100))

    async def test_initial_fields_and_hard_stop_both_sides(self):
        for side in ["buy", "sell"]:
            self.bot = Final2PaperHunter(
                Path(self.tmp.name) / (side + ".json"), market_data=self.data
            )
            await self.entry(side)
            p = self.bot.positions[S]
            self.assertAlmostEqual(p["sl"], 97.2 if side == "buy" else 102.8)
            self.assertFalse(p["partial_1_done"])
            self.assertFalse(p["partial_2_done"])
            self.assertEqual(p["original_notional"], 500)
            self.assertEqual(self.bot.settings()["STOP_LOSS_PCT"], 0.028)
            self.assertFalse(self.bot.settings()["hedge_positions"])

    async def test_first_partial_40_percent_once_and_margin_release(self):
        await self.entry()
        self.data.price = 98.79
        await self.bot.manage_exits()
        p = self.bot.positions[S]
        self.assertEqual(p["amount"], 3)
        self.assertEqual(self.bot.exchange.used, 6)
        self.assertTrue(p["partial_1_done"])
        self.assertFalse(p["partial_2_done"])
        self.assertEqual(p["sl"], 98)
        self.assertEqual(self.bot.exchange.trades[-1]["exit_reason"], "PARTIAL_1")
        n = len(self.bot.exchange.trades)
        await self.bot.manage_exits()
        self.assertEqual(len(self.bot.exchange.trades), n)

    async def test_short_partial_uses_same_fraction(self):
        await self.entry("sell")
        self.data.price = 101.21
        await self.bot.manage_exits()
        self.assertEqual(self.bot.positions[S]["amount"], 3)
        self.assertEqual(self.bot.positions[S]["sl"], 102)

    async def test_hard_stop_has_priority_before_any_partial(self):
        await self.entry()
        self.data.price = 97.1
        await self.bot.manage_exits()
        self.assertFalse(self.bot.positions)
        self.assertEqual(len(self.bot.exchange.trades), 2)
        self.assertEqual(self.bot.exchange.trades[-1]["exit_reason"], "SL")

    async def test_gradual_decline_hits_tight_stop_before_second_partial(self):
        await self.entry()
        self.data.price = 98.79
        await self.bot.manage_exits()
        self.data.price = 98
        await self.bot.manage_exits()
        self.assertFalse(self.bot.positions)
        self.assertEqual(
            [f.get("exit_reason") for f in self.bot.exchange.trades[1:]],
            ["PARTIAL_1", "TIGHT_1"],
        )

    async def test_jump_can_execute_both_partials_then_stop_next_pass(self):
        await self.entry()
        self.data.price = 97.9
        await self.bot.manage_exits()
        p = self.bot.positions[S]
        self.assertEqual(p["amount"], 1.5)
        self.assertTrue(p["partial_1_done"])
        self.assertTrue(p["partial_2_done"])
        self.assertEqual(p["sl"], 98.7)
        self.assertEqual(
            [f.get("exit_reason") for f in self.bot.exchange.trades[1:]],
            ["PARTIAL_1", "PARTIAL_2"],
        )
        await self.bot.manage_exits()
        self.assertFalse(self.bot.positions)
        self.assertEqual(self.bot.exchange.trades[-1]["exit_reason"], "TIGHT_2")

    async def test_profit_locks_and_supplied_zero_level_condition(self):
        await self.entry()
        self.data.price = 101
        await self.bot.manage_exits()
        self.assertAlmostEqual(self.bot.positions[S]["sl"], 97.2)
        self.data.price = 102
        await self.bot.manage_exits()
        self.assertEqual(self.bot.positions[S]["sl"], 101)
        self.data.price = 101.5
        await self.bot.manage_exits()
        self.assertEqual(self.bot.positions[S]["sl"], 101)

    async def test_restore_preserves_partial_flags_and_amount(self):
        await self.entry()
        self.data.price = 98.79
        await self.bot.manage_exits()
        restored = Final2PaperHunter(self.path, market_data=self.data)
        self.assertEqual(restored.positions, self.bot.positions)
        await restored.manage_exits()
        self.assertEqual(len(restored.exchange.trades), 2)

    async def test_partial_failure_keeps_amount_but_supplied_loop_marks_done(self):
        await self.entry()
        calls = 0

        async def ticker(symbol):
            nonlocal calls
            calls += 1
            return {
                "last": 98.79,
                "timestamp": (time.time() - (60 if calls > 1 else 0)) * 1000,
            }

        self.data.fetch_ticker = ticker
        await self.bot.manage_exits()
        self.assertEqual(self.bot.positions[S]["amount"], 5)
        self.assertTrue(self.bot.positions[S]["partial_1_done"])
        self.assertEqual(len(self.bot.exchange.trades), 1)

    async def test_no_reversal_or_scale_in(self):
        await self.entry()
        self.data.price = 98.1
        self.bot.detect_cascade = AsyncMock(return_value={"short": True})
        await self.bot.manage_exits()
        self.bot.detect_cascade.assert_not_awaited()
        self.assertEqual(len(self.bot.positions), 1)
        self.assertFalse(hasattr(self.bot, "try_open_reversal"))
        self.assertFalse(hasattr(self.bot, "add_position"))


if __name__ == "__main__":
    unittest.main()
