"""The sole active configuration for the approved Final-3 staged-partial bot."""

import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
log = logging.getLogger("FINAL3")
LEVERAGE = 50
MAX_POSITION_PCT = 0.10
CASCADE_THRESHOLD = 0.012
VOLUME_SPIKE = 3.5
COOLDOWN_SEC = 8
SCAN_INTERVAL = 600
MAX_SYMBOLS_TO_SCAN = 40
MAX_CONCURRENT_POSITIONS = 5
MIN_QUOTE_VOLUME = 15_000_000
HARD_STOP_PCT = 0.028
PARTIAL_LEVEL_1 = 0.012
PARTIAL_FRAC_1 = 0.40
PARTIAL_LEVEL_2 = 0.020
PARTIAL_FRAC_2 = 0.50
PROFIT_LOCK_LEVELS = [
    (0.010, 0.0001),
    (0.015, 0.005),
    (0.020, 0.010),
    (0.025, 0.015),
    (0.030, 0.020),
    (0.035, 0.025),
    (0.040, 0.030),
]


def settings():
    return dict(
        mode="final3_staged_partial",
        LEVERAGE=LEVERAGE,
        MAX_POSITION_PCT=MAX_POSITION_PCT,
        CASCADE_THRESHOLD=CASCADE_THRESHOLD,
        VOLUME_SPIKE=VOLUME_SPIKE,
        TAKE_PROFIT_PCT=None,
        STOP_LOSS_PCT=HARD_STOP_PCT,
        COOLDOWN_SEC=COOLDOWN_SEC,
        SCAN_INTERVAL=SCAN_INTERVAL,
        MAX_SYMBOLS_TO_SCAN=MAX_SYMBOLS_TO_SCAN,
        MAX_CONCURRENT_POSITIONS=MAX_CONCURRENT_POSITIONS,
        MIN_QUOTE_VOLUME=MIN_QUOTE_VOLUME,
        position_basis="wallet_total",
        stop_basis="original_fill_price",
        hard_stop_pct=HARD_STOP_PCT,
        hedge_positions=False,
        partial_level_1=PARTIAL_LEVEL_1,
        partial_frac_1=PARTIAL_FRAC_1,
        partial_level_2=PARTIAL_LEVEL_2,
        partial_frac_2=PARTIAL_FRAC_2,
        partial_stop_1=0.024,
        partial_stop_2=0.015,
        profit_lock_levels=[list(level) for level in PROFIT_LOCK_LEVELS],
    )
