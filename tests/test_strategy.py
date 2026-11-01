"""Characterize the requested staged-partial rules, including their stated limitations."""

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from cascade_final3_paper import Final3PaperHunter
from fixtures import DynamicPublicData

S = "BTC/USDT:USDT"


class PartialTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = DynamicPublicData()
        self.path = Path(self.tmp.name) / "state.json"
        self.bot = Final3PaperHunter(self.path, market_data=self.data)

    async def entry(self, side="buy"):
        self.data.price = 100
        self.assertTrue(await self.bot.enter(S, side, 100))

    async def test_initial_fields_and_hard_stop_both_sides(self):
        for side in ["buy", "sell"]:
            self.bot = Final3PaperHunter(
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
        self.assertEqual(p["sl"], 97.6)
        self.assertEqual(self.bot.exchange.trades[-1]["exit_reason"], "PARTIAL_1")
        n = len(self.bot.exchange.trades)
        await self.bot.manage_exits()
        self.assertEqual(len(self.bot.exchange.trades), n)

    async def test_short_partial_uses_same_fraction(self):
        await self.entry("sell")
        self.data.price = 101.21
        await self.bot.manage_exits()
        self.assertEqual(self.bot.positions[S]["amount"], 3)
        self.assertEqual(self.bot.positions[S]["sl"], 102.4)

    async def test_hard_stop_has_priority_before_any_partial(self):
        await self.entry()
        self.data.price = 97.1
        await self.bot.manage_exits()
        self.assertFalse(self.bot.positions)
        self.assertEqual(len(self.bot.exchange.trades), 2)
        self.assertEqual(self.bot.exchange.trades[-1]["exit_reason"], "SL")

    async def test_gradual_decline_reaches_second_partial_before_stop(self):
        await self.entry()
        self.data.price = 98.79
        await self.bot.manage_exits()
        self.data.price = 98
        await self.bot.manage_exits()
        self.assertEqual(self.bot.positions[S]["amount"], 1.5)
        self.assertTrue(self.bot.positions[S]["partial_2_done"])
        self.assertEqual(
            [f.get("exit_reason") for f in self.bot.exchange.trades[1:]],
            ["PARTIAL_1", "PARTIAL_2"],
        )
        await self.bot.manage_exits()
        self.assertFalse(self.bot.positions)
        self.assertEqual(self.bot.exchange.trades[-1]["exit_reason"], "TIGHT_2")

    async def test_jump_can_execute_both_partials_then_stop_next_pass(self):
        await self.entry()
        self.data.price = 97.9
        await self.bot.manage_exits()
        p = self.bot.positions[S]
        self.assertEqual(p["amount"], 1.5)
        self.assertTrue(p["partial_1_done"])
        self.assertTrue(p["partial_2_done"])
        self.assertEqual(p["sl"], 98.5)
        self.assertEqual(
            [f.get("exit_reason") for f in self.bot.exchange.trades[1:]],
            ["PARTIAL_1", "PARTIAL_2"],
        )
        await self.bot.manage_exits()
        self.assertFalse(self.bot.positions)
        self.assertEqual(self.bot.exchange.trades[-1]["exit_reason"], "TIGHT_2")

    async def test_profit_locks_include_one_percent_near_break_even(self):
        await self.entry()
        self.data.price = 101
        await self.bot.manage_exits()
        self.assertAlmostEqual(self.bot.positions[S]["sl"], 100.01)
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
        restored = Final3PaperHunter(self.path, market_data=self.data)
        self.assertEqual(restored.positions, self.bot.positions)
        await restored.manage_exits()
        self.assertEqual(len(restored.exchange.trades), 2)

    async def test_partial_failure_does_not_mark_done_or_tighten_stop(self):
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
        self.assertFalse(self.bot.positions[S]["partial_1_done"])
        self.assertAlmostEqual(self.bot.positions[S]["sl"], 97.2)
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

    async def test_failed_first_partial_retries_when_quote_becomes_fresh(self):
        await self.entry()
        self.data.price = 98.79
        original = self.data.fetch_ticker
        calls = 0

        async def ticker(symbol):
            nonlocal calls
            calls += 1
            return {
                "last": 98.79,
                "timestamp": (time.time() - (30 if calls == 2 else 0)) * 1000,
            }

        self.data.fetch_ticker = ticker
        await self.bot.manage_exits()
        self.assertFalse(self.bot.positions[S]["partial_1_done"])
        self.data.fetch_ticker = original
        await self.bot.manage_exits()
        self.assertTrue(self.bot.positions[S]["partial_1_done"])
        self.assertEqual(self.bot.positions[S]["amount"], 3)
        self.assertEqual(len(self.bot.exchange.trades), 2)

    async def test_second_partial_failure_does_not_tighten_or_mark_second(self):
        await self.entry()
        self.data.price = 98.79
        await self.bot.manage_exits()
        self.data.price = 98
        with patch.object(self.bot, "partial_close", new=AsyncMock(return_value=False)):
            await self.bot.manage_exits()
        self.assertFalse(self.bot.positions[S]["partial_2_done"])
        self.assertEqual(self.bot.positions[S]["sl"], 97.6)
        self.assertEqual(self.bot.positions[S]["amount"], 3)
        await self.bot.manage_exits()
        self.assertTrue(self.bot.positions[S]["partial_2_done"])
        self.assertEqual(self.bot.positions[S]["amount"], 1.5)

    async def test_short_second_partial_and_first_profit_lock(self):
        await self.entry("sell")
        self.data.price = 101.21
        await self.bot.manage_exits()
        self.data.price = 102
        await self.bot.manage_exits()
        self.assertTrue(self.bot.positions[S]["partial_2_done"])
        self.assertAlmostEqual(self.bot.positions[S]["sl"], 101.5)
        self.assertEqual(self.bot.positions[S]["amount"], 1.5)
        self.bot = Final3PaperHunter(
            Path(self.tmp.name) / "profit-short.json", market_data=self.data
        )
        await self.entry("sell")
        self.data.price = 99
        await self.bot.manage_exits()
        self.assertAlmostEqual(self.bot.positions[S]["sl"], 99.99)
        self.assertEqual(self.bot.positions[S]["profit_locked"], 0.0001)

    async def test_partial_close_rejects_almost_full_quantity(self):
        await self.entry()
        self.assertFalse(await self.bot.partial_close(S, 0.99, 100, "TEST"))
        self.assertFalse(await self.bot.partial_close(S, 0.00001, 100, "TEST"))
        self.assertEqual(self.bot.positions[S]["amount"], 5)
        self.assertEqual(len(self.bot.exchange.trades), 1)

    async def test_partial_handles_account_already_fully_closed(self):
        await self.entry()
        original = self.bot.exchange.create_order

        async def full(symbol, type, side, amount, params):
            return await original(symbol, type, side, 5, params)

        with patch.object(self.bot.exchange, "create_order", side_effect=full):
            self.assertTrue(await self.bot.partial_close(S, 0.4, 100, "TEST"))
        self.assertNotIn(S, self.bot.positions)
        self.assertNotIn(S, self.bot.exchange.positions)


if __name__ == "__main__":
    unittest.main()
