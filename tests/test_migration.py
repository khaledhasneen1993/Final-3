import json
import tempfile
import unittest
from pathlib import Path
from migrate_idle_state import (
    current_settings,
    legacy_settings,
    previous_final3_settings,
    migrate,
    plan,
)
from cascade_final2_paper import Final2PaperHunter
from fixtures import DynamicPublicData


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.addCleanup(self.t.cleanup)
        self.path = Path(self.t.name) / "idle.json"
        self.state = dict(
            version=1,
            settings=legacy_settings(),
            fee_rate=0.0005,
            balance_usdt=101,
            equity_usdt=101,
            free_usdt=101,
            used_usdt=0,
            account_positions={},
            strategy_positions={},
            prices={},
            last_trade={},
            trades=[{"symbol": "OLD", "id": "paper-1"}],
        )
        self.path.write_text(json.dumps(self.state))

    def test_idle_preserves_wallet_and_ledger_with_backup(self):
        before = self.path.read_bytes()
        backup = migrate(self.path)
        self.assertEqual(backup.read_bytes(), before)
        after = json.loads(self.path.read_text())
        self.assertEqual(after, {**self.state, "settings": current_settings()})
        bot = Final2PaperHunter(self.path, market_data=DynamicPublicData())
        self.assertEqual(bot.exchange.balance, 101)
        self.assertEqual(bot.exchange.trades, self.state["trades"])
        self.assertIsNone(migrate(self.path))

    def test_open_or_unknown_state_untouched(self):
        for change in (
            {"account_positions": {"BTC": {}}},
            {"strategy_positions": {"BTC": {}}},
            {"settings": {"mode": "different"}},
            {"used_usdt": 1},
        ):
            self.path.write_text(json.dumps({**self.state, **change}))
            before = self.path.read_bytes()
            with self.assertRaises(ValueError):
                migrate(self.path)
            self.assertEqual(self.path.read_bytes(), before)

    def test_plan_does_not_write(self):
        before = self.path.read_bytes()
        _, changed = plan(self.path)
        self.assertTrue(changed)
        self.assertEqual(before, self.path.read_bytes())

    def test_final3_five_percent_idle_wallet_and_fills_preserved(self):
        self.state["settings"] = previous_final3_settings()
        self.path.write_text(json.dumps(self.state))
        before = self.path.read_bytes()
        backup = migrate(self.path)
        self.assertEqual(backup.read_bytes(), before)
        after = json.loads(self.path.read_text())
        self.assertEqual(after, {**self.state, "settings": current_settings()})
        self.assertEqual(after["settings"]["STOP_LOSS_PCT"], 0.03)

    def test_final3_five_percent_open_state_rejected(self):
        self.state["settings"] = previous_final3_settings()
        self.state["strategy_positions"] = {"BTC": {"sl": 95}}
        self.path.write_text(json.dumps(self.state))
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            migrate(self.path)
        self.assertEqual(self.path.read_bytes(), before)


class HedgeMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_unfinished_reversal_open_positions_migrate_with_backup(self):
        from migrate_idle_state import unfinished_reversal_settings

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            data = DynamicPublicData()
            bot = Final2PaperHunter(path, market_data=data)
            await bot.enter("BTC/USDT:USDT", "buy", 100)
            state = json.loads(path.read_text())
            state["settings"] = unfinished_reversal_settings()
            path.write_text(json.dumps(state))
            before = path.read_bytes()
            restored = Final2PaperHunter(path, market_data=data)
            self.assertEqual(restored.exchange.balance, bot.exchange.balance)
            self.assertEqual(restored.positions["BTC/USDT:USDT"]["sl"], 97)
            self.assertEqual(len(list(path.parent.glob("*.bak"))), 1)
            self.assertEqual(next(path.parent.glob("*.bak")).read_bytes(), before)

    async def test_old_seven_percent_open_positions_are_not_silently_converted(self):
        from migrate_idle_state import old_scale_settings

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            data = DynamicPublicData()
            bot = Final2PaperHunter(path, market_data=data)
            await bot.enter("BTC/USDT:USDT", "buy", 100)
            state = json.loads(path.read_text())
            state["settings"] = old_scale_settings()
            path.write_text(json.dumps(state))
            before = path.read_bytes()
            with self.assertRaisesRegex(ValueError, "Old scale-in positions"):
                Final2PaperHunter(path, market_data=data)
            self.assertEqual(path.read_bytes(), before)
