"""Final-3: equal-notional scale-in, post-add protection and profit trailing."""
import argparse
import asyncio
import math
from pathlib import Path
from binance_cascade_hunter_dynamic_v2_paper import V2PaperHunter
from binance_cascade_hunter_dynamic_v2 import log
from paper_account import PartialStopCrossed

ADD_LEVELS = (0.025,)
ADD_FRACTION = 1.0
TRAIL_ACTIVATION = 0.018
TRAIL_DISTANCE = 0.009
TARGET_STEP = 0.005
POST_ADD_STOP_PCT = 0.04
PARTIAL_ADVERSE_PCT = 0.03
PARTIAL_CLOSE_FRACTION = 0.50


class Final2PaperHunter(V2PaperHunter):
    def __init__(self, state_path='paper_state_final3.json', fee_rate=0.0005,
                 reset=False, market_data=None):
        if Path(state_path).exists() and not reset:
            from migrate_idle_state import migrate
            backup = migrate(state_path)
            if backup:
                log.info('Risk settings migrated; original state backed up: %s', backup)
        super().__init__(state_path, fee_rate, reset, market_data)
        for symbol, p in self.positions.items():
            fill = self.exchange.positions[symbol]
            fill.setdefault('fees_paid', fill['entry'] * fill['amount'] * self.exchange.fee_rate)
            fill.setdefault('realized_pnl', 0.0)
            p.setdefault('partial_done', False)
            p.setdefault('break_even_armed', False)
            p.setdefault('stop_kind', 'TRAIL_SL' if p.get('trailing') else 'SL')
            if p.get('adds_done'):
                self.apply_tight_stop(p)

    def settings(self):
        return {**super().settings(), 'mode':'final3_scale_trail',
                'add_levels':list(ADD_LEVELS), 'add_fraction':ADD_FRACTION,
                'add_basis':'original_fill_notional', 'stop_basis':'original_fill_price',
                'trail_activation':TRAIL_ACTIVATION, 'trail_distance':TRAIL_DISTANCE,
                'target_step':TARGET_STEP, 'post_add_stop':POST_ADD_STOP_PCT,
                'partial_adverse':PARTIAL_ADVERSE_PCT, 'partial_fraction':PARTIAL_CLOSE_FRACTION,
                'break_even_after_add':True}

    async def enter(self, symbol, side, price):
        if symbol in self.positions:
            return False
        if not await super().enter(symbol, side, price):
            return False
        p=self.positions[symbol]
        fill=self.exchange.positions[symbol]
        p.update(original_entry=fill['entry'], original_notional=fill['entry']*fill['amount'],
                 initial_sl=fill['entry']*(1-self.strategy.STOP_LOSS_PCT if side=='buy'
                                        else 1+self.strategy.STOP_LOSS_PCT),
                 adds_done=0, trailing=False, best_price=fill['entry'],
                 entry=fill['entry'], amount=fill['amount'], partial_done=False,
                 break_even_armed=False, stop_kind='SL')
        p['sl']=p['initial_sl']
        p['tp']=p['entry']*(1+TRAIL_ACTIVATION if side=='buy' else 1-TRAIL_ACTIVATION)
        self.save()
        return True

    async def close_position(self, symbol, reason, trigger_price):
        p=self.positions[symbol]
        await self.exchange.create_order(symbol,'market','sell' if p['side']=='buy' else 'buy',
                                         p['amount'],params={'reduceOnly':True, 'exit_reason':reason})
        self.record_exit_reason(symbol,reason,trigger_price)
        del self.positions[symbol]
        self.save()
        log.info('EXIT %s %s @ %.8f',reason,symbol,trigger_price)

    async def add_position(self,symbol,price):
        p=self.positions[symbol]
        if p['adds_done'] >= len(ADD_LEVELS) or p.get('trailing'):
            return False
        amount=float(self.exchange.amount_to_precision(symbol,p['original_notional']*ADD_FRACTION/price))
        if amount<=0:
            return False
        # PaperExchange enforces market limits, free margin and fees before mutation.
        await self.exchange.create_order(symbol,'market',p['side'],amount,params={'scaleIn':True})
        fill=self.exchange.positions[symbol]
        p.update(entry=fill['entry'],amount=fill['amount'],adds_done=p['adds_done']+1)
        p['tp']=p['entry']*(1+TRAIL_ACTIVATION if p['side']=='buy' else 1-TRAIL_ACTIVATION)
        self.apply_tight_stop(p)
        self.exchange.trades[-1].update(stop_loss_price=p['sl'], entry_reason='SCALE_IN',add_level=ADD_LEVELS[p['adds_done']-1],
                                       original_notional=p['original_notional'])
        self.save()
        log.info('SCALE IN %s level=%d average=%.8f',symbol,p['adds_done'],p['entry'])
        return True

    @staticmethod
    def tighten_stop(p, candidate, kind):
        improves = candidate > p['sl'] if p['side'] == 'buy' else candidate < p['sl']
        if improves:
            p['sl'], p['stop_kind'] = candidate, kind

    def apply_tight_stop(self, p):
        direction = 1 if p['side'] == 'buy' else -1
        self.tighten_stop(p, p['entry'] * (1-direction*POST_ADD_STOP_PCT), 'TIGHT_SL')

    def break_even_price(self, symbol):
        fill = self.exchange.positions[symbol]
        direction = 1 if fill['side'] == 'buy' else -1
        amount = fill['amount']
        return (direction*fill['entry']*amount + fill['fees_paid'] - fill['realized_pnl']) / (
            amount*(direction-self.exchange.fee_rate))

    async def partial_close(self, symbol, price):
        p = self.positions[symbol]
        if not p['adds_done'] or p.get('partial_done'):
            return False
        direction = 1 if p['side'] == 'buy' else -1
        if direction*(price-p['sl']) <= 0:
            return False
        if -direction*(price/p['entry']-1) + 1e-12 < PARTIAL_ADVERSE_PCT:
            return False
        amount = float(self.exchange.amount_to_precision(symbol, p['amount']*PARTIAL_CLOSE_FRACTION))
        if amount <= 0 or amount >= p['amount']:
            log.warning('Partial close skipped %s: quantity too small to split', symbol)
            return False
        try:
            await self.exchange.create_order(symbol, 'market', 'sell' if direction==1 else 'buy', amount,
                params={'reduceOnly':True, 'exit_reason':'PARTIAL_RISK', 'partialStop':p['sl']})
        except PartialStopCrossed as error:
            await self.close_position(symbol, p.get('stop_kind','SL'), error.price)
            return False
        self.record_exit_reason(symbol, 'PARTIAL_RISK', price)
        p['amount'] = self.exchange.positions[symbol]['amount']
        p['partial_done'] = True
        self.save()
        log.info('PARTIAL_RISK %s | remaining=%.8f', symbol, p['amount'])
        return True

    async def manage_exits(self):
        for symbol,p in list(self.positions.items()):
            try:
                price=float((await self.exchange.fetch_ticker(symbol))['last'])
                direction=1 if p['side']=='buy' else -1
                if p['adds_done']:
                    self.apply_tight_stop(p)
                if direction*(price-p['sl'])<=0:
                    await self.close_position(symbol,p.get('stop_kind','SL'),price)
                    continue
                adverse=-direction*(price/p['original_entry']-1)
                if not p['trailing'] and not p['adds_done'] and adverse+1e-12>=ADD_LEVELS[0]:
                    await self.add_position(symbol,price)
                if direction*(price-p['sl'])<=0:
                    await self.close_position(symbol,p.get('stop_kind','SL'),price)
                    continue
                if p['adds_done']:
                    await self.partial_close(symbol,price)
                    if symbol not in self.positions:
                        continue
                    break_even = self.break_even_price(symbol)
                    p['break_even_price'] = break_even
                    if math.isfinite(break_even) and direction*(price-break_even)>1e-12:
                        self.tighten_stop(p,break_even,'BE_SL')
                        p['break_even_armed']=True
                gain=direction*(price/p['entry']-1)
                if not p['trailing'] and gain+1e-12>=TRAIL_ACTIVATION:
                    p['trailing']=True
                    p['best_price']=price
                if p['trailing']:
                    p['best_price']=max(p['best_price'],price) if direction==1 else min(p['best_price'],price)
                    best=p['best_price']
                    self.tighten_stop(p,best*(1-direction*TRAIL_DISTANCE),'TRAIL_SL')
                    gain=direction*(best/p['entry']-1)
                    steps=max(1,math.floor((gain-TRAIL_ACTIVATION)/TARGET_STEP)+1)
                    p['tp']=p['entry']*(1+direction*(TRAIL_ACTIVATION+steps*TARGET_STEP))
                self.save()
            except Exception as error:
                log.warning('Position management failed %s: %s',symbol,error)
                self.save()




def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state',default='paper_state_final3.json')
    parser.add_argument('--reset',action='store_true')
    args=parser.parse_args()
    bot=Final2PaperHunter(args.state,reset=args.reset)
    log.warning('FINAL-2 PAPER: excludes liquidation, funding and slippage; averaging increases exposure')
    try:
        asyncio.run(bot.start())
    except KeyboardInterrupt:
        log.info("Final-2 stopped; state saved")


if __name__=='__main__':main()
