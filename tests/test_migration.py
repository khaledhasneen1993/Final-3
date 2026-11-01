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
        self.assertEqual(after["settings"]["STOP_LOSS_PCT"], 0.028)

    def test_final3_five_percent_open_state_rejected(self):
        self.state["settings"] = previous_final3_settings()
        self.state["strategy_positions"] = {"BTC": {"sl": 95}}
        self.path.write_text(json.dumps(self.state))
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            migrate(self.path)
        self.assertEqual(self.path.read_bytes(), before)


class VariantIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_reversal_open_state_is_preserved_and_not_converted(self):
        from cascade_final3_reversal import Final2PaperHunter as Reversal

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "reverse.json"
            data = DynamicPublicData()
            old = Reversal(path, market_data=data)
            await old.enter("BTC/USDT:USDT", "buy", 100)
            before = path.read_bytes()
            with self.assertRaisesRegex(ValueError, "Previous-strategy positions"):
                Final2PaperHunter(path, market_data=data)
            self.assertEqual(path.read_bytes(), before)
            restored = Reversal(path, market_data=data)
            self.assertEqual(restored.positions, old.positions)
            self.assertEqual(restored.positions["BTC/USDT:USDT"]["sl"], 97)

    async def test_idle_reversal_state_can_migrate_with_backup(self):
        from cascade_final3_reversal import Final2PaperHunter as Reversal

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "idle.json"
            data = DynamicPublicData()
            old = Reversal(path, market_data=data)
            old.exchange.balance = 105
            old.save()
            before = path.read_bytes()
            new = Final2PaperHunter(path, market_data=data)
            self.assertEqual(new.exchange.balance, 105)
            self.assertEqual(new.settings()["STOP_LOSS_PCT"], 0.028)
            self.assertEqual(next(path.parent.glob("*.bak")).read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
