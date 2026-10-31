import json
import tempfile
import time
import unittest
from pathlib import Path
from cascade_final2_paper import Final2PaperHunter
from fixtures import DynamicPublicData
from migrate_idle_state import previous_protection_settings

S='BTC/USDT:USDT'

class ProtectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.data=DynamicPublicData();self.path=Path(self.tmp.name)/'account.json'
        self.bot=Final2PaperHunter(self.path,market_data=self.data)

    async def entry(self,side='buy'):
        self.data.price=100
        self.assertTrue(await self.bot.enter(S,side,100))

    async def added(self,side='buy'):
        await self.entry(side)
        self.data.price=97.5 if side=='buy' else 102.5
        await self.bot.manage_exits()
        self.assertEqual(self.bot.positions[S]['adds_done'],1)

    async def test_tight_stop_both_sides(self):
        for side in ['buy','sell']:
            self.bot=Final2PaperHunter(Path(self.tmp.name)/(side+'.json'),market_data=self.data)
            await self.added(side);p=self.bot.positions[S]
            self.assertAlmostEqual(p['sl'],p['entry']*(.96 if side=='buy' else 1.04))
            self.assertEqual(p['stop_kind'],'TIGHT_SL')
            self.assertFalse(p['break_even_armed'])

    async def test_partial_once_releases_margin_and_preserves_average(self):
        await self.added();p=self.bot.positions[S];quantity=p['amount'];average=p['entry'];used=self.bot.exchange.used
        self.data.price=average*.97;await self.bot.manage_exits()
        self.assertTrue(p['partial_done']);self.assertAlmostEqual(p['amount'],quantity/2,places=3)
        self.assertAlmostEqual(self.bot.exchange.used,used/2,places=3)
        self.assertEqual(p['entry'],average)
        self.assertEqual(self.bot.exchange.trades[-1]['exit_reason'],'PARTIAL_RISK')
        self.assertTrue(self.bot.exchange.trades[-1]['partial_close'])
        n=len(self.bot.exchange.trades);await self.bot.manage_exits();self.assertEqual(len(self.bot.exchange.trades),n)

    async def test_partial_short_and_final_stop_reason(self):
        await self.added('sell');p=self.bot.positions[S]
        self.data.price=p['entry']*1.03;await self.bot.manage_exits()
        self.assertTrue(p['partial_done']);self.data.price=p['sl']+.1
        await self.bot.manage_exits();self.assertNotIn(S,self.bot.positions)
        self.assertEqual(self.bot.exchange.trades[-1]['exit_reason'],'TIGHT_SL')

    async def test_break_even_covers_partial_losses_and_all_fees_both_sides(self):
        for side in ['buy','sell']:
            self.bot=Final2PaperHunter(Path(self.tmp.name)/(side+'.json'),market_data=self.data)
            await self.added(side);p=self.bot.positions[S];direction=1 if side=='buy' else -1
            self.data.price=p['entry']*(1-direction*.03);await self.bot.manage_exits()
            be=self.bot.break_even_price(S);fill=self.bot.exchange.positions[S]
            net=fill['realized_pnl']+direction*(be-fill['entry'])*fill['amount']-fill['fees_paid']-be*fill['amount']*self.bot.exchange.fee_rate
            self.assertAlmostEqual(net,0,places=8)
            self.data.price=be;await self.bot.close_position(S,'BE_SL',be)
            self.assertAlmostEqual(self.bot.exchange.balance,100,places=8)
            self.assertAlmostEqual(self.bot.exchange.trades[-1]['position_net_pnl_usdt'],0,places=8)

    async def test_be_arms_only_after_recovery(self):
        await self.added();p=self.bot.positions[S];original=p['sl']
        self.data.price=p['entry'];await self.bot.manage_exits()
        self.assertFalse(p['break_even_armed']);self.assertEqual(p['sl'],original)
        be=self.bot.break_even_price(S);self.data.price=be+.01;await self.bot.manage_exits()
        self.assertTrue(p['break_even_armed']);self.assertAlmostEqual(p['sl'],be)
        self.assertEqual(p['stop_kind'],'BE_SL')
        self.data.price=be-.01;await self.bot.manage_exits();self.assertEqual(self.bot.exchange.trades[-1]['exit_reason'],'BE_SL')

    async def test_stop_has_priority_over_partial_on_gap(self):
        await self.added();self.data.price=self.bot.positions[S]['sl']-.1
        await self.bot.manage_exits();self.assertEqual(len(self.bot.exchange.trades),3)
        self.assertEqual(self.bot.exchange.trades[-1]['exit_reason'],'TIGHT_SL')

    async def test_stop_crossed_on_execution_recheck_closes_all(self):
        await self.added();p=self.bot.positions[S];trigger=p['entry']*.97;gap=p['sl']-.1
        calls=0
        async def ticker(symbol):
            nonlocal calls
            calls+=1
            return {'last':trigger if calls==1 else gap,'timestamp':time.time()*1000}
        self.data.fetch_ticker=ticker
        await self.bot.manage_exits()
        self.assertNotIn(S,self.bot.positions)
        self.assertEqual(len(self.bot.exchange.trades),3)
        self.assertEqual(self.bot.exchange.trades[-1]['exit_reason'],'TIGHT_SL')

    async def test_stale_partial_does_not_change_account(self):
        await self.added();trigger=self.bot.positions[S]['entry']*.97;calls=0;before=self.bot.exchange.balance
        async def ticker(symbol):
            nonlocal calls
            calls+=1
            return {'last':trigger,'timestamp':(time.time()-(60 if calls>1 else 0))*1000}
        self.data.fetch_ticker=ticker
        await self.bot.manage_exits()
        self.assertFalse(self.bot.positions[S]['partial_done']);self.assertEqual(self.bot.exchange.balance,before)
        self.assertEqual(len(self.bot.exchange.trades),2)

    async def test_restart_preserves_partial_and_cost_basis(self):
        await self.added();self.data.price=self.bot.positions[S]['entry']*.97;await self.bot.manage_exits()
        be=self.bot.break_even_price(S);n=len(self.bot.exchange.trades)
        self.bot=Final2PaperHunter(self.path,market_data=self.data);await self.bot.manage_exits()
        self.assertTrue(self.bot.positions[S]['partial_done']);self.assertEqual(len(self.bot.exchange.trades),n)
        self.assertAlmostEqual(self.bot.break_even_price(S),be)

    async def test_prior_seven_percent_open_account_migrates_with_backup(self):
        await self.added();state=json.loads(self.path.read_text());state['settings']=previous_protection_settings()
        state['strategy_positions'][S]['sl']=93
        for key in ['stop_kind','partial_done','break_even_armed']:state['strategy_positions'][S].pop(key)
        for key in ['fees_paid','realized_pnl']:state['account_positions'][S].pop(key)
        self.path.write_text(json.dumps(state));before=self.path.read_bytes()
        self.bot=Final2PaperHunter(self.path,market_data=self.data)
        self.assertEqual(self.bot.exchange.balance,state['balance_usdt'])
        self.assertEqual(self.bot.exchange.trades,state['trades'])
        self.assertGreater(self.bot.positions[S]['sl'],93)
        backups=list(self.path.parent.glob('*.bak'));self.assertEqual(len(backups),1)
        self.assertEqual(backups[0].read_bytes(),before)

    async def test_restored_stronger_trailing_stop_is_never_loosened(self):
        await self.added();p=self.bot.positions[S];p.update(sl=101,trailing=True,best_price=102,stop_kind='TRAIL_SL');self.bot.save()
        self.bot=Final2PaperHunter(self.path,market_data=self.data)
        self.assertEqual(self.bot.positions[S]['sl'],101)

if __name__=='__main__':unittest.main()
