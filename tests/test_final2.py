import json
import tempfile
import unittest
from pathlib import Path
from cascade_final2_paper import Final2PaperHunter
from fixtures import DynamicPublicData

S = 'BTC/USDT:USDT'

class Final2Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.t = tempfile.TemporaryDirectory(); self.addCleanup(self.t.cleanup)
        self.path = Path(self.t.name)/'state.json'; self.data = DynamicPublicData()
        self.bot = Final2PaperHunter(self.path, fee_rate=0, market_data=self.data)
        await self.bot.enter(S, 'buy', 100)

    async def price(self, value):
        self.data.price = value
        await self.bot.manage_exits()

    async def test_settings_and_initial_margin(self):
        s = self.bot.settings()
        self.assertEqual((s['MAX_POSITION_PCT'],s['STOP_LOSS_PCT'],s['MAX_CONCURRENT_POSITIONS']),(.10,.05,5))
        self.assertEqual(s['stop_basis'], 'original_fill_price')
        self.assertAlmostEqual(self.bot.exchange.used, 10)
        self.assertAlmostEqual(self.bot.positions[S]['original_notional'], 500)

    async def test_no_add_before_threshold(self):
        await self.price(97.51)
        self.assertEqual(len(self.bot.exchange.trades), 1)

    async def test_equal_original_notional_at_threshold(self):
        await self.price(97.5)
        p = self.bot.positions[S]; fill = self.bot.exchange.trades[-1]
        self.assertEqual(p['adds_done'], 1)
        self.assertAlmostEqual(fill['price']*fill['amount'], 500, delta=.1)
        self.assertAlmostEqual(self.bot.exchange.used, 20, delta=.01)
        self.assertEqual(p['sl'], 95)
        self.assertEqual(len(self.bot.positions), 1)

    async def test_one_add_only(self):
        await self.price(97.5); await self.price(96); await self.price(95.1)
        self.assertEqual(self.bot.positions[S]['adds_done'], 1)
        self.assertEqual(len(self.bot.exchange.trades), 2)

    async def test_gap_past_add_before_stop(self):
        await self.price(96)
        self.assertEqual(self.bot.positions[S]['adds_done'], 1)

    async def test_stop_priority_on_gap(self):
        await self.price(94)
        self.assertFalse(self.bot.positions)
        self.assertEqual(len(self.bot.exchange.trades), 2)
        self.assertEqual(self.bot.exchange.trades[-1]['exit_reason'], 'SL')

    async def test_stop_at_five_percent(self):
        await self.price(95)
        self.assertFalse(self.bot.positions)
        self.assertEqual(len(self.bot.exchange.trades), 2)

    async def test_averaged_stop_remains_original(self):
        await self.price(97.5)
        self.assertNotEqual(self.bot.positions[S]['entry'],100)
        self.assertEqual(self.bot.positions[S]['sl'],95)
        await self.price(95)
        self.assertFalse(self.bot.positions)
        self.assertEqual(self.bot.exchange.trades[-1]['exit_reason'],'SL')

    async def test_fill_basis_when_signal_differs(self):
        bot=Final2PaperHunter(Path(self.t.name)/'different.json',fee_rate=0,market_data=self.data)
        await bot.enter(S,'buy',80)
        self.assertEqual(bot.positions[S]['sl'],95)
        self.assertEqual(bot.positions[S]['original_entry'],100)
        self.data.price=97.5;await bot.manage_exits()
        self.assertEqual(bot.positions[S]['adds_done'],1)

    async def test_current_wallet_margin(self):
        self.bot.exchange.balance=80
        await self.bot.enter('ETH/USDT:USDT','buy',100)
        self.assertAlmostEqual(self.bot.exchange.positions['ETH/USDT:USDT']['margin'],8)

    async def test_five_positions_sixth_rejected(self):
        for coin in ('ETH','SOL','XRP','ADA'):
            self.assertTrue(await self.bot.enter(coin+'/USDT:USDT','buy',100))
        self.assertEqual(len(self.bot.positions),5)
        self.assertFalse(await self.bot.enter('DOGE/USDT:USDT','buy',100))
        self.assertEqual(self.bot.scanner.config.MAX_CONCURRENT_POSITIONS,5)
        self.assertEqual(len(self.bot.exchange.trades),5)

    async def test_trailing_unchanged(self):
        await self.price(101.8);p=self.bot.positions[S]
        self.assertTrue(p['trailing']);self.assertAlmostEqual(p['sl'],101.8*.991)
        self.assertAlmostEqual(p['tp'],102.3)
        await self.price(105);stop=p['sl'];self.assertAlmostEqual(stop,105*.991)
        await self.price(104.5);self.assertEqual(p['sl'],stop)
        await self.price(104);self.assertFalse(self.bot.positions)
        self.assertEqual(self.bot.exchange.trades[-1]['exit_reason'],'TRAIL_SL')

    async def test_averaged_target_still_one_point_eight(self):
        await self.price(97.5);p=self.bot.positions[S]
        self.assertAlmostEqual(p['tp'],p['entry']*1.018)
        await self.price(p['tp']);self.assertTrue(p['trailing'])
        self.assertAlmostEqual(p['sl'],p['best_price']*.991)

    async def test_no_add_after_trailing(self):
        await self.price(102);await self.price(97.5)
        self.assertFalse(self.bot.positions)
        self.assertEqual(len(self.bot.exchange.trades),2)
        self.assertEqual(self.bot.exchange.trades[-1]['exit_reason'],'TRAIL_SL')

    async def test_restore_no_duplicate_add(self):
        await self.price(97.5)
        self.bot=Final2PaperHunter(self.path,fee_rate=0,market_data=self.data)
        await self.price(96)
        self.assertEqual(len(self.bot.exchange.trades),2)
        self.assertEqual(self.bot.positions[S]['adds_done'],1)

    async def test_restore_trailing(self):
        await self.price(103);stop=self.bot.positions[S]['sl']
        self.bot=Final2PaperHunter(self.path,fee_rate=0,market_data=self.data)
        await self.price(102.5);self.assertEqual(self.bot.positions[S]['sl'],stop)

    async def test_insufficient_equity_retry(self):
        self.bot.exchange.balance=5;await self.price(97.5)
        self.assertEqual(self.bot.positions[S]['adds_done'],0)
        self.assertEqual(len(self.bot.exchange.trades),1)
        self.bot.exchange.balance=100;await self.price(97.5)
        self.assertEqual(self.bot.positions[S]['adds_done'],1)

    async def test_short_add_and_trailing(self):
        await self.bot.close_position(S,'TEST',100);self.bot.last_trade.clear()
        await self.bot.enter(S,'sell',100)
        await self.price(102.49);self.assertEqual(self.bot.positions[S]['adds_done'],0)
        await self.price(102.5);p=self.bot.positions[S]
        self.assertEqual(p['adds_done'],1);self.assertEqual(p['sl'],105)
        fill=self.bot.exchange.trades[-1]
        self.assertAlmostEqual(fill['amount']*fill['price'],500,delta=.11)
        await self.price(104);self.assertEqual(p['adds_done'],1)
        await self.price(p['entry']*.982);self.assertTrue(p['trailing'])
        self.assertAlmostEqual(p['sl'],p['best_price']*1.009)
        await self.price(p['sl']+1);self.assertFalse(self.bot.positions)

    async def test_short_stop_priority(self):
        await self.bot.close_position(S,'TEST',100);self.bot.last_trade.clear()
        await self.bot.enter(S,'sell',100);await self.price(105)
        self.assertFalse(self.bot.positions)
        self.assertEqual(self.bot.exchange.trades[-1]['exit_reason'],'SL')
        self.assertEqual(len(self.bot.exchange.trades),4)

    async def test_all_fees_and_weighted_pnl(self):
        self.bot=Final2PaperHunter(Path(self.t.name)/'fee.json',market_data=self.data)
        await self.bot.enter(S,'buy',100);await self.price(97.5)
        p=self.bot.exchange.positions[S].copy();await self.price(95)
        fees=sum(t['fee_usdt'] for t in self.bot.exchange.trades)
        pnl=(95-p['entry'])*p['amount']
        self.assertAlmostEqual(self.bot.exchange.balance,100+pnl-fees)
        self.assertEqual(len(self.bot.exchange.trades),3)

    async def test_reject_old_settings_without_altering_state(self):
        state=json.loads(self.path.read_text());state['settings']['add_levels']=[.05,.10]
        self.path.write_text(json.dumps(state));before=self.path.read_bytes()
        with self.assertRaises(ValueError):Final2PaperHunter(self.path,fee_rate=0,market_data=self.data)
        self.assertEqual(self.path.read_bytes(),before)
