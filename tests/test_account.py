import unittest
from fixtures import DynamicPublicData
from paper_account import PaperExchange

S = "BTC/USDT:USDT"


class AccountTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.data = DynamicPublicData()
        self.account = PaperExchange(self.data)

    async def test_partial_and_final_pnl_fees_and_margin_match_wallet(self):
        await self.account.create_order(S, "market", "buy", 5)
        self.data.price = 98.79
        await self.account.create_order(S, "market", "sell", 2, {"reduceOnly": True})
        self.assertEqual(self.account.positions[S]["amount"], 3)
        self.assertEqual(self.account.used, 6)
        final = await self.account.create_order(
            S, "market", "sell", 3, {"reduceOnly": True}
        )
        gross = (98.79 - 100) * 5
        fees = 500 * 0.0005 + 98.79 * 5 * 0.0005
        self.assertAlmostEqual(self.account.balance, 100 + gross - fees)
        self.assertAlmostEqual(final["position_net_pnl_usdt"], gross - fees)
        self.assertEqual(self.account.used, 0)
        self.assertFalse(self.account.positions)

    async def test_insufficient_margin_rejects_without_fill(self):
        with self.assertRaises(ValueError):
            await self.account.create_order(S, "market", "buy", 1000)
        self.assertEqual(self.account.balance, 100)
        self.assertFalse(self.account.positions)
        self.assertFalse(self.account.trades)

    async def test_duplicate_and_wrong_direction_close_cannot_mutate_position(self):
        await self.account.create_order(S, "market", "buy", 5)
        balance = self.account.balance
        with self.assertRaises(ValueError):
            await self.account.create_order(S, "market", "sell", 1)
        with self.assertRaises(ValueError):
            await self.account.create_order(S, "market", "buy", 1, {"reduceOnly": True})
        self.assertEqual(self.account.balance, balance)
        self.assertEqual(self.account.positions[S]["amount"], 5)


if __name__ == "__main__":
    unittest.main()
