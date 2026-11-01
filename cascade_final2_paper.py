"""Final-3: hard stop, profit locks and signal-confirmed reversal."""
import argparse
import asyncio
from pathlib import Path

from binance_cascade_hunter_dynamic_v2_paper import V2PaperHunter
from binance_cascade_hunter_dynamic_v2 import log

HARD_STOP_PCT = 0.03
REVERSAL_TRIGGER = 0.018
REVERSAL_SIZE_MULT = 0.60
REVERSAL_CONFIRM_PROFIT = 0.012

PROFIT_LOCK_LEVELS = [
    (0.010, 0.000),
    (0.015, 0.005),
    (0.020, 0.010),
    (0.025, 0.015),
    (0.030, 0.020),
    (0.035, 0.025),
    (0.040, 0.030),
]


class Final2PaperHunter(V2PaperHunter):
    def __init__(self, state_path='paper_state_final3.json', fee_rate=0.0005,
                 reset=False, market_data=None):
        if Path(state_path).exists() and not reset:
            from migrate_idle_state import migrate
            backup = migrate(state_path)
            if backup:
                log.info('Risk settings migrated; original state backed up: %s', backup)
        super().__init__(state_path, fee_rate, reset, market_data)

    def settings(self):
        return {
            **super().settings(),
            'mode': 'final3_smart_reversal',
            'STOP_LOSS_PCT': HARD_STOP_PCT,
            'hard_stop_pct': HARD_STOP_PCT,
            'reversal_trigger': REVERSAL_TRIGGER,
            'reversal_size_mult': REVERSAL_SIZE_MULT,
            'reversal_confirm_profit': REVERSAL_CONFIRM_PROFIT,
            'profit_lock_levels': [list(level) for level in PROFIT_LOCK_LEVELS],
        }

    async def enter(self, symbol, side, price):
        if symbol in self.positions:
            return False
        if not await super().enter(symbol, side, price):
            return False
        p = self.positions[symbol]
        fill = self.exchange.positions[symbol]
        p.update(
            original_entry=fill['entry'],
            original_notional=fill['entry'] * fill['amount'],
            initial_sl=fill['entry'] * (1 - HARD_STOP_PCT if side == 'buy' else 1 + HARD_STOP_PCT),
            adds_done=0,
            trailing=False,
            best_price=fill['entry'],
            entry=fill['entry'],
            amount=fill['amount'],
            reversed=False,
            reverse_of=None,
            profit_locked=0.0,
            stop_kind='SL'
        )
        p['sl'] = p['initial_sl']
        self.save()
        return True

    async def close_position(self, symbol, reason, trigger_price):
        p = self.positions[symbol]
        await self.exchange.create_order(
            symbol, 'market', 'sell' if p['side'] == 'buy' else 'buy',
            p['amount'], params={'reduceOnly': True, 'exit_reason': reason}
        )
        self.record_exit_reason(symbol, reason, trigger_price)
        del self.positions[symbol]
        self.save()
        log.info('EXIT %s %s @ %.8f', reason, symbol, trigger_price)

    @staticmethod
    def tighten_stop(p, candidate, kind):
        improves = candidate > p['sl'] if p['side'] == 'buy' else candidate < p['sl']
        if improves:
            p['sl'], p['stop_kind'] = candidate, kind

    async def manage_exits(self):
        for symbol, p in list(self.positions.items()):
            try:
                price = float((await self.exchange.fetch_ticker(symbol))['last'])
                direction = 1 if p['side'] == 'buy' else -1
                move = direction * (price / p['entry'] - 1)

                # Hard Stop 3%
                if direction * (price - p['sl']) <= 0:
                    await self.close_position(symbol, p.get('stop_kind', 'SL'), price)
                    continue

                # Profit Lock System
                for trigger, lock in PROFIT_LOCK_LEVELS:
                    if move >= trigger and p.get('profit_locked', 0) < lock:
                        new_sl = p['entry'] * (1 + direction * lock)
                        self.tighten_stop(p, new_sl, 'PROFIT_LOCK')
                        p['profit_locked'] = lock
                        log.info('PROFIT LOCK %s | locked +%.1f%%', symbol, lock * 100)

                # Smart Reversal
                adverse = -direction * (price / p['original_entry'] - 1)
                if not p.get('reversed') and adverse >= REVERSAL_TRIGGER:
                    await self.try_open_reversal(symbol, price, p)

                # Check if reversal position became successful
                if p.get('reversed') and p.get('reverse_of'):
                    await self.check_reversal_success(symbol, price)

                self.save()
            except Exception as error:
                log.warning('Position management failed %s: %s', symbol, error)
                self.save()

    async def try_open_reversal(self, symbol, price, original_pos):
        """فتح صفقة معاكسة بحجم 60% عند انعكاس 1.8% مع وجود إشارة معاكسة"""
        if original_pos.get('reversed'):
            return False

        reverse_side = 'sell' if original_pos['side'] == 'buy' else 'buy'

        # نتحقق من وجود إشارة معاكسة حقيقية
        sig = await self.detect_cascade(symbol) if hasattr(self, 'detect_cascade') else None
        if sig is None:
            return False

        has_signal = (reverse_side == 'buy' and sig.get('long')) or (reverse_side == 'sell' and sig.get('short'))
        if not has_signal:
            return False

        amount = float(self.exchange.amount_to_precision(
            symbol, original_pos['original_notional'] * REVERSAL_SIZE_MULT / price
        ))
        if amount <= 0:
            return False

        try:
            await self.exchange.create_order(symbol, 'market', reverse_side, amount)
            fill = self.exchange.positions.get(symbol)
            if not fill:
                return False

            # نسجل أن هذه صفقة انعكاس
            original_pos['reversed'] = True
            original_pos['reverse_side'] = reverse_side

            # نضيف المركز الجديد كصفقة مستقلة
            self.positions[symbol + '_REV'] = {
                'side': reverse_side,
                'entry': price,
                'amount': amount,
                'original_entry': price,
                'original_notional': price * amount,
                'sl': price * (1 - HARD_STOP_PCT if reverse_side == 'buy' else 1 + HARD_STOP_PCT),
                'profit_locked': 0.0,
                'reversed': False,
                'is_reversal': True,
                'parent': symbol,
                'stop_kind': 'SL'
            }
            log.info('REVERSAL OPENED %s %s | size=%.4f @ %.6f', reverse_side.upper(), symbol, amount, price)
            self.save()
            return True
        except Exception as e:
            log.warning('Reversal failed %s: %s', symbol, e)
            return False

    async def check_reversal_success(self, symbol, price):
        """إذا ربحت صفقة الانعكاس 1.2% نقفل الأصلية ونخلي الانعكاس هي الرئيسية"""
        p = self.positions.get(symbol)
        if not p or not p.get('is_reversal'):
            return

        direction = 1 if p['side'] == 'buy' else -1
        move = direction * (price / p['entry'] - 1)

        if move >= REVERSAL_CONFIRM_PROFIT:
            parent = p.get('parent')
            if parent and parent in self.positions:
                await self.close_position(parent, 'REVERSAL_WIN', price)
                log.info('REVERSAL SUCCESS | closed original %s | keeping reversal', parent)

            # نحول صفقة الانعكاس لصفقة عادية
            p['is_reversal'] = False
            p['parent'] = None
            self.save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', default='paper_state_final3.json')
    parser.add_argument('--reset', action='store_true')
    args = parser.parse_args()
    bot = Final2PaperHunter(args.state, reset=args.reset)
    log.warning('FINAL-3 PAPER: hard stop 3%% | profit locks | smart reversal')
    try:
        asyncio.run(bot.start())
    except KeyboardInterrupt:
        log.info('Final-3 stopped; state saved')


if __name__ == '__main__':
    main()
