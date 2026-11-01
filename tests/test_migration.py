import json
import tempfile
import unittest
from pathlib import Path
from cascade_final3_paper import Final3PaperHunter
from migrate_state import migrate, plan, current_settings
from fixtures import DynamicPublicData


class MigrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.json"
        self.data = DynamicPublicData()
        self.bot = Final3PaperHunter(self.path, market_data=self.data)

    async def test_current_account_resumes_without_migration(self):
        await self.bot.enter("BTC/USDT:USDT", "buy", 100)
        before = self.path.read_bytes()
        self.assertIsNone(migrate(self.path))
        restored = Final3PaperHunter(self.path, market_data=self.data)
        self.assertEqual(restored.positions, self.bot.positions)
        self.assertEqual(self.path.read_bytes(), before)

    async def test_old_partial_metadata_upgrade_preserves_active_positions(self):
        await self.bot.enter("BTC/USDT:USDT", "buy", 100)
        state = json.loads(self.path.read_text())
        state["settings"]["stop_basis"] = "signal_price"
        self.path.write_text(json.dumps(state))
        before = self.path.read_bytes()
        _, changed = plan(self.path)
        self.assertTrue(changed)
        self.assertEqual(self.path.read_bytes(), before)
        restored = Final3PaperHunter(self.path, market_data=self.data)
        after = json.loads(self.path.read_text())
        self.assertEqual(after, {**state, "settings": current_settings()})
        self.assertEqual(restored.positions, self.bot.positions)
        self.assertEqual(next(self.path.parent.glob("*.bak")).read_bytes(), before)

    async def test_old_idle_account_keeps_wallet_and_trades(self):
        self.bot.exchange.balance = 105
        self.bot.save()
        state = json.loads(self.path.read_text())
        state["settings"]["mode"] = "final3_smart_reversal"
        self.path.write_text(json.dumps(state))
        before = self.path.read_bytes()
        restored = Final3PaperHunter(self.path, market_data=self.data)
        self.assertEqual(restored.exchange.balance, 105)
        self.assertEqual(next(self.path.parent.glob("*.bak")).read_bytes(), before)

    async def test_other_open_strategies_and_risk_changes_are_rejected(self):
        await self.bot.enter("BTC/USDT:USDT", "buy", 100)
        original = json.loads(self.path.read_text())
        for mode, stop in [
            ("final3_smart_reversal", 0.03),
            ("final3_scale_trail", 0.07),
            ("final3_staged_partial", 0.03),
        ]:
            state = json.loads(json.dumps(original))
            state["settings"].update(mode=mode, STOP_LOSS_PCT=stop)
            self.path.write_text(json.dumps(state))
            before = self.path.read_bytes()
            with self.assertRaises(ValueError):
                Final3PaperHunter(self.path, market_data=self.data)
            self.assertEqual(self.path.read_bytes(), before)

    async def test_approved_old_partial_rules_upgrade_without_replaying_or_loosening(
        self,
    ):
        await self.bot.enter("BTC/USDT:USDT", "buy", 100)
        self.data.price = 98.79
        await self.bot.manage_exits()
        state = json.loads(self.path.read_text())
        p = state["strategy_positions"]["BTC/USDT:USDT"]
        p["sl"] = 98
        state["settings"].update(partial_stop_1=0.020, partial_stop_2=0.013)
        state["settings"].pop("partial_stop_2_basis")
        state["settings"]["profit_lock_levels"][0][1] = 0.0
        self.path.write_text(json.dumps(state))
        before = self.path.read_bytes()
        restored = Final3PaperHunter(self.path, market_data=self.data)
        self.assertEqual(restored.positions["BTC/USDT:USDT"]["sl"], 98)
        self.assertTrue(restored.positions["BTC/USDT:USDT"]["partial_1_done"])
        self.assertEqual(restored.exchange.balance, state["balance_usdt"])
        self.assertEqual(restored.exchange.trades, state["trades"])
        self.assertEqual(restored.settings()["partial_stop_1"], 0.024)
        self.assertEqual(next(self.path.parent.glob("*.bak")).read_bytes(), before)

    async def test_previous_fixed_second_stop_upgrades_without_loosening_saved_stop(
        self,
    ):
        await self.bot.enter("BTC/USDT:USDT", "buy", 100)
        state = json.loads(self.path.read_text())
        state["settings"].update(partial_stop_2=0.015)
        state["settings"].pop("partial_stop_2_basis")
        state["strategy_positions"]["BTC/USDT:USDT"].update(
            sl=98.5, partial_1_done=True, partial_2_done=True
        )
        self.path.write_text(json.dumps(state))
        before = self.path.read_bytes()
        restored = Final3PaperHunter(self.path, market_data=self.data)
        self.assertEqual(restored.positions["BTC/USDT:USDT"]["sl"], 98.5)
        self.assertTrue(restored.positions["BTC/USDT:USDT"]["partial_2_done"])
        self.assertEqual(restored.settings()["partial_stop_2"], 0.008)
        self.assertEqual(restored.settings()["partial_stop_2_basis"], "current_price")
        self.assertEqual(next(self.path.parent.glob("*.bak")).read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
