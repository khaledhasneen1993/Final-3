import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from ledger import round_trips

class LedgerTests(unittest.TestCase):
    def test_scale_fees_short_reentry(self):
        def fill(side,amount,price,fee,close=False,pnl=0):
            return dict(symbol='BTC/USDT:USDT',side=side,amount=amount,price=price,
                        fee_usdt=fee,reduceOnly=close,pnl_usdt=pnl,timestamp='2026-10-30')
        fills=[fill('sell',2,100,.1),fill('sell',1,110,.055),
               fill('buy',3,90,.135,True,40), fill('buy',1,91,.0455)]
        opened,closed=round_trips(fills)
        self.assertEqual(len(opened),1)
        self.assertEqual(len(closed),1)
        self.assertEqual(closed[0]['adds'],1)
        self.assertAlmostEqual(closed[0]['average_entry'],310/3)
        self.assertAlmostEqual(closed[0]['net_pnl_usdt'],39.71)
        self.assertEqual(opened[0]['side'],'buy')
    def test_partial_is_not_a_completed_position(self):
        def fill(side,amount,price,fee,close=False,pnl=0,partial=False):
            return dict(symbol='BTC',side=side,amount=amount,price=price,fee_usdt=fee,
                        reduceOnly=close,pnl_usdt=pnl,partial_close=partial,timestamp='2026-10-31')
        fills=[fill('buy',2,100,.1),fill('buy',2,98,.098),
               fill('sell',2,96,.096,True,-6,True)]
        opened,closed=round_trips(fills)
        self.assertEqual(len(opened),1);self.assertFalse(closed)
        self.assertEqual(opened[0]['remaining_amount'],2)
        fills.append(fill('sell',2,100,.1,True,2))
        opened,closed=round_trips(fills)
        self.assertFalse(opened);self.assertEqual(len(closed),1)
        self.assertAlmostEqual(closed[0]['net_pnl_usdt'],-4.394)

    def test_empty(self):
        self.assertEqual(round_trips([]),([],[]))

if __name__=='__main__': unittest.main()
