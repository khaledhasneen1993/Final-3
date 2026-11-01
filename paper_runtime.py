"""Public market data, continuous scanning and lifecycle for Final-3 paper trading."""

import asyncio
from pathlib import Path
import ccxt.async_support as ccxt
import config
from paper_account import PaperState, PaperExchange
from continuous_scanner import ContinuousScanner


class PaperHunter(PaperState):
    def __init__(self, state_path, fee_rate=0.0005, reset=False, market_data=None):
        self.strategy = config
        public = (
            market_data
            if market_data is not None
            else ccxt.binanceusdm(
                {"enableRateLimit": True, "options": {"defaultType": "future"}}
            )
        )
        self.exchange = PaperExchange(public, fee_rate, leverage=config.LEVERAGE)
        self.positions = {}
        self.last_trade = {}
        self.risk_memory = {"peak_balance": 100.0, "entries_paused": False}
        self.lock = asyncio.Lock()
        self.state_path = Path(state_path)
        if self.state_path.exists() and not reset:
            self.restore()
        self.scanner = ContinuousScanner(self, config=config)
        self.exchange.scanner = self.scanner

    def settings(self):
        return config.settings()

    async def get_top_symbols(self):
        try:
            tickers = await self.exchange.fetch_tickers()
            rows = [
                (s, float(t.get("quoteVolume") or 0))
                for s, t in tickers.items()
                if s.endswith(":USDT")
                and "/USDT:USDT" in s
                and float(t.get("quoteVolume") or 0) >= config.MIN_QUOTE_VOLUME
            ]
            rows.sort(key=lambda row: row[1], reverse=True)
            return [s for s, _ in rows[: config.MAX_SYMBOLS_TO_SCAN]]
        except Exception as error:
            config.log.warning("Universe refresh failed: %s", error)
            return ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"]

    def record_entry_signal(self, symbol, signal):
        for trade in reversed(self.exchange.trades):
            if trade["symbol"] == symbol and not trade["reduceOnly"]:
                trade.update(
                    entry_signal=dict(signal),
                    entry_reason="CASCADE_MOVE_VOLUME_ORDERBOOK",
                    stop_loss_price=self.positions[symbol]["sl"],
                )
                self.save()
                break

    async def exit_watcher(self):
        while True:
            try:
                async with self.lock:
                    await self.manage_exits()
                await asyncio.sleep(2)
            except Exception as error:
                config.log.warning("Exit watcher failed: %s", error)
                await asyncio.sleep(15)

    async def start(self):
        tasks = []
        try:
            await self.exchange.load_markets()
            self.save()
            config.log.info(
                "FINAL-3 RISK V2 | margin=1%% | stop=2.8%% | partial 40%% / 50%% | state=%s",
                self.state_path,
            )
            tasks = [
                asyncio.create_task(self.scanner.run()),
                asyncio.create_task(self.exit_watcher()),
                asyncio.create_task(self.report()),
            ]
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            try:
                self.save()
            finally:
                await self.exchange.close()
