"""Final-2: independent paper account with one equal-notional scale-in and profit trailing."""
import argparse
import asyncio
import math
import os
import copy
from datetime import datetime, timezone
from pathlib import Path
import aiohttp
from binance_cascade_hunter_dynamic_v2_paper import V2PaperHunter
from binance_cascade_hunter_dynamic_v2 import log

ADD_LEVELS = (0.025,)
ADD_FRACTION = 1.0
TRAIL_ACTIVATION = 0.018
TRAIL_DISTANCE = 0.009
TARGET_STEP = 0.005


class Final2PaperHunter(V2PaperHunter):
    def __init__(self, state_path='paper_state_final3.json', fee_rate=0.0005,
                 reset=False, market_data=None):
        super().__init__(state_path, fee_rate, reset, market_data)

    def settings(self):
        return {**super().settings(), 'mode':'final3_scale_trail',
                'add_levels':list(ADD_LEVELS), 'add_fraction':ADD_FRACTION,
                'add_basis':'original_fill_notional', 'stop_basis':'original_fill_price',
                'trail_activation':TRAIL_ACTIVATION, 'trail_distance':TRAIL_DISTANCE,
                'target_step':TARGET_STEP}

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
                 entry=fill['entry'], amount=fill['amount'])
        p['sl']=p['initial_sl']
        p['tp']=p['entry']*(1+TRAIL_ACTIVATION if side=='buy' else 1-TRAIL_ACTIVATION)
        self.save()
        return True

    async def close_position(self, symbol, reason, trigger_price):
        p=self.positions[symbol]
        await self.exchange.create_order(symbol,'market','sell' if p['side']=='buy' else 'buy',
                                         p['amount'],params={'reduceOnly':True})
        self.record_exit_reason(symbol,reason,trigger_price)
        del self.positions[symbol]
        self.save()
        log.info('EXIT %s %s @ %.8f',reason,symbol,trigger_price)

    async def add_position(self,symbol,price):
        p=self.positions[symbol]
        amount=float(self.exchange.amount_to_precision(symbol,p['original_notional']*ADD_FRACTION/price))
        if amount<=0:
            return False
        # PaperExchange enforces market limits, free margin and fees before mutation.
        await self.exchange.create_order(symbol,'market',p['side'],amount,params={'scaleIn':True})
        fill=self.exchange.positions[symbol]
        p.update(entry=fill['entry'],amount=fill['amount'],adds_done=p['adds_done']+1)
        p['tp']=p['entry']*(1+TRAIL_ACTIVATION if p['side']=='buy' else 1-TRAIL_ACTIVATION)
        self.exchange.trades[-1].update(entry_reason='SCALE_IN',add_level=ADD_LEVELS[p['adds_done']-1],
                                       original_notional=p['original_notional'])
        self.save()
        log.info('SCALE IN %s level=%d average=%.8f',symbol,p['adds_done'],p['entry'])
        return True

    async def manage_exits(self):
        for symbol,p in list(self.positions.items()):
            try:
                price=float((await self.exchange.fetch_ticker(symbol))['last'])
                direction=1 if p['side']=='buy' else -1
                # Stops take priority over averaging, including price gaps past the stop.
                if direction*(price-p['sl'])<=0:
                    await self.close_position(symbol,'TRAIL_SL' if p['trailing'] else 'SL',price)
                    continue
                adverse=-direction*(price/p['original_entry']-1)
                if not p['trailing']:
                    while p['adds_done']<len(ADD_LEVELS) and adverse+1e-12>=ADD_LEVELS[p['adds_done']]:
                        if not await self.add_position(symbol,price):break
                    gain=direction*(price/p['entry']-1)
                    if gain+1e-12>=TRAIL_ACTIVATION:
                        p['trailing']=True
                        p['best_price']=price
                if p['trailing']:
                    p['best_price']=max(p['best_price'],price) if direction==1 else min(p['best_price'],price)
                    best=p['best_price']
                    candidate=best*(1-direction*TRAIL_DISTANCE)
                    p['sl']=max(p['sl'],candidate) if direction==1 else min(p['sl'],candidate)
                    # The TP becomes a moving objective; reaching it extends the run, not a forced exit.
                    gain=direction*(best/p['entry']-1)
                    steps=max(1,math.floor((gain-TRAIL_ACTIVATION)/TARGET_STEP)+1)
                    target=TRAIL_ACTIVATION+steps*TARGET_STEP
                    p['tp']=p['entry']*(1+direction*target)
                self.save()
            except Exception as error:
                log.warning('Position management failed %s: %s',symbol,error)
                self.save()




class DashboardPublisher:
    """Optional telemetry-only publisher; strategy and paper execution stay untouched."""
    URL = "https://coamowlsyuqbxngijxsu.supabase.co/functions/v1/cascade-dashboard"

    def __init__(self, bot, session_id, token):
        self.bot, self.session_id, self.token = bot, session_id, token

    def snapshot(self, status="running"):
        bot = self.bot
        return copy.deepcopy({
            "session_id": self.session_id, "status": status,
            "sent_at": datetime.now(timezone.utc).isoformat(),
            "initial_balance_usdt": 100.0, "settings": bot.settings(),
            "fee_rate": bot.exchange.fee_rate,
            "balance_usdt": bot.exchange.balance, "equity_usdt": bot.exchange.equity,
            "free_usdt": bot.exchange.free, "used_usdt": bot.exchange.used,
            "account_positions": bot.exchange.positions,
            "strategy_positions": bot.positions, "prices": bot.exchange.prices,
            "trades": bot.exchange.trades,
            "scanner": {"connected": bot.scanner.connected,
                        "symbols": len(bot.scanner.symbols), "events": bot.scanner.events,
                        "entries": bot.scanner.entries},
        })

    async def upload(self, session, status="running"):
        async with session.post(
            self.URL, json=self.snapshot(status),
            headers={"x-bot-id": "final2", "x-bot-token": self.token},
        ) as response:
            if response.status != 200 or (await response.json()).get("ok") is not True:
                raise RuntimeError("Combined dashboard rejected snapshot")

    async def loop(self):
        delay = 10
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
            while True:
                try:
                    await self.upload(session)
                    delay = 10
                except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError, ValueError):
                    log.warning("COMBINED DASHBOARD UPDATE FAILED | paper trading continues")
                    delay = min(30, delay * 2)
                await asyncio.sleep(delay)

    async def stopped(self):
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
                await self.upload(session, "stopped")
        except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError, ValueError):
            log.warning("Combined dashboard final update unavailable; local state remains saved")


async def run_with_dashboard(bot, publisher):
    tasks = [asyncio.create_task(bot.start()), asyncio.create_task(publisher.loop())]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await publisher.stopped()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state',default='paper_state_final3.json')
    parser.add_argument('--reset',action='store_true')
    args=parser.parse_args()
    bot=Final2PaperHunter(args.state,reset=args.reset)
    log.warning('FINAL-2 PAPER: excludes liquidation, funding and slippage; averaging increases exposure')
    token = os.environ.get("CASCADE_DASHBOARD_TOKEN", "").strip()
    try:
        if token:
            log.info("COMBINED DASHBOARD TELEMETRY ENABLED | session=%s", Path(args.state).stem)
            asyncio.run(run_with_dashboard(bot, DashboardPublisher(bot, Path(args.state).stem, token)))
        else:
            asyncio.run(bot.start())
    except KeyboardInterrupt:
        log.info("Final-2 stopped; state saved")


if __name__=='__main__':main()
