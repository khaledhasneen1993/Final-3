"""Preserve accounts while applying approved partial-rule updates for the approved Final-3 configuration."""

from risk_controls import upgrade_margin_only
import argparse
import json
import math
import shutil
from datetime import datetime, timezone
from pathlib import Path
from config import settings as current_settings

LEGACY_APPROVED_SETTINGS = {
    "mode": "final3_staged_partial",
    "LEVERAGE": 50,
    "MAX_POSITION_PCT": 0.1,
    "CASCADE_THRESHOLD": 0.012,
    "VOLUME_SPIKE": 3.5,
    "TAKE_PROFIT_PCT": None,
    "STOP_LOSS_PCT": 0.028,
    "COOLDOWN_SEC": 8,
    "SCAN_INTERVAL": 600,
    "MAX_SYMBOLS_TO_SCAN": 40,
    "MAX_CONCURRENT_POSITIONS": 5,
    "MIN_QUOTE_VOLUME": 15000000,
    "position_basis": "wallet_total",
    "stop_basis": "original_fill_price",
    "hard_stop_pct": 0.028,
    "hedge_positions": False,
    "partial_level_1": 0.012,
    "partial_frac_1": 0.4,
    "partial_level_2": 0.02,
    "partial_frac_2": 0.5,
    "partial_stop_1": 0.024,
    "partial_stop_2": 0.008,
    "partial_stop_2_basis": "current_price",
    "profit_lock_levels": [
        [0.01, 0.0001],
        [0.015, 0.005],
        [0.02, 0.01],
        [0.025, 0.015],
        [0.03, 0.02],
        [0.035, 0.025],
        [0.04, 0.03],
    ],
}


def plan(path):
    state = json.loads(Path(path).read_text(encoding="utf-8"))
    current = current_settings()
    if upgrade_margin_only(state, current):
        return state, True
    if state.get("settings") == LEGACY_APPROVED_SETTINGS:
        if state.get("account_positions") or state.get("strategy_positions"):
            raise ValueError(
                "Previous risk profile has OPEN positions. Keep that account unchanged; start a NEW --state or --fresh session for risk v2"
            )
        if (
            state.get("version") != 1
            or not isinstance(state.get("trades"), list)
            or not isinstance(state.get("last_trade"), dict)
        ):
            raise ValueError("Invalid previous account; original preserved")
        wallet = state.get("balance_usdt")
        if not isinstance(wallet, (int, float)) or not math.isfinite(wallet):
            raise ValueError("Invalid previous wallet; original preserved")
        state["settings"] = current
        state["risk_memory"] = {
            "peak_balance": max(100.0, wallet),
            "entries_paused": False,
        }
        return state, True
    if state.get("version") != 1:
        raise ValueError("Unknown state version; original preserved")
    previous = state.get("settings", {})
    if previous == current:
        return state, False
    if previous.get("mode") not in (
        "final3_staged_partial",
        "final3_scale_trail",
        "final3_smart_reversal",
    ):
        raise ValueError("Unrecognized account; original preserved")
    accounts = state.get("account_positions")
    positions = state.get("strategy_positions")
    if (
        not isinstance(accounts, dict)
        or not isinstance(positions, dict)
        or set(accounts) != set(positions)
    ):
        raise ValueError("Inconsistent positions; original preserved")
    wallet = state.get("balance_usdt")
    if (
        not isinstance(wallet, (int, float))
        or not math.isfinite(wallet)
        or not isinstance(state.get("trades"), list)
        or not isinstance(state.get("last_trade"), dict)
    ):
        raise ValueError("Invalid account; original preserved")
    if previous.get("mode") == "final3_staged_partial":
        # Accept only the approved previous partial rules or the current rules.
        # Existing stops, quantities and completion flags are deliberately preserved.
        fields = set(current) - {"stop_basis"}
        fixed_entry_stop = dict(current)
        fixed_entry_stop.pop("partial_stop_2_basis")
        fixed_entry_stop["partial_stop_2"] = 0.015
        prior = dict(fixed_entry_stop)
        prior.update(
            partial_stop_1=0.020,
            partial_stop_2=0.013,
            profit_lock_levels=[
                [trigger, 0.0 if index == 0 else lock]
                for index, (trigger, lock) in enumerate(current["profit_lock_levels"])
            ],
        )
        if not any(
            all(previous.get(key) == candidate.get(key) for key in fields)
            for candidate in (current, fixed_entry_stop, prior)
        ):
            raise ValueError(
                "Strategy settings differ; use another state path; original preserved"
            )
        if previous.get("stop_basis") not in ("signal_price", "original_fill_price"):
            raise ValueError("Unknown stop basis; original preserved")
    elif accounts:
        raise ValueError(
            "Previous-strategy positions are open; use their matching version or a new state path; original preserved"
        )
    if not accounts:
        for key, value in [
            ("used_usdt", 0),
            ("equity_usdt", wallet),
            ("free_usdt", max(0, wallet)),
        ]:
            if not math.isclose(float(state.get(key, math.nan)), value, abs_tol=1e-8):
                raise ValueError("Inconsistent idle account; original preserved")
    state["settings"] = current
    return state, True


def migrate(path):
    path = Path(path)
    state, changed = plan(path)
    if not changed:
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = path.with_name(path.name + ".before-settings-update-" + stamp + ".bak")
    shutil.copy2(path, backup)
    temp = path.with_name(path.name + ".migrating")
    temp.write_text(
        json.dumps(state, indent=2, ensure_ascii=False, allow_nan=False),
        encoding="utf-8",
    )
    temp.replace(path)
    return backup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        _, changed = plan(args.state)
        if not changed:
            print("State matches approved Final-3 settings.")
            return
        if not args.apply:
            print("Compatible metadata update; stop writers before using --apply.")
            return
        print("Updated with backup:", migrate(args.state))
    except (OSError, ValueError, TypeError, KeyError) as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    main()
