"""Final-3 paper runner: independent hedge positions and profit locks."""

import argparse
import asyncio
import fcntl
from pathlib import Path
from cascade_final2_paper import Final2PaperHunter
from binance_cascade_hunter_dynamic_v2 import log

# Legacy implementation/module names retained for source compatibility.
Final3PaperHunter = Final2PaperHunter


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=Path("paper_state_final3.json"))
    args = parser.parse_args()
    args.state.parent.mkdir(parents=True, exist_ok=True)
    with args.state.with_name(args.state.name + ".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.exit(1, "Final-3 already running on this state file.\n")
        bot = Final3PaperHunter(args.state)
        log.warning(
            "FINAL-3 PAPER | 10%% initial | 3%% stop | 60%% reversal | profit locks | paper hedge account"
        )
        try:
            asyncio.run(bot.start())
        except KeyboardInterrupt:
            log.info("Final-3 stopped; paper state saved")


if __name__ == "__main__":
    main()
