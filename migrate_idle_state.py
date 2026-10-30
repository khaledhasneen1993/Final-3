"""Explicit migration of an idle legacy Final-2 ledger; never alters open trades."""
import argparse
import json
import math
import shutil
from datetime import datetime, timezone
from pathlib import Path


def current_settings():
    # Settings only: no exchange client, no market calls, no state restoration.
    from cascade_final2_paper import Final2PaperHunter
    import binance_cascade_hunter_dynamic_v2 as config
    probe = Final2PaperHunter.__new__(Final2PaperHunter)
    probe.strategy = config
    return probe.settings()


def legacy_settings():
    return {**current_settings(), 'MAX_POSITION_PCT': .10, 'STOP_LOSS_PCT': .15,
            'MAX_CONCURRENT_POSITIONS': 3, 'add_levels': [.05, .10],
            'add_fraction': .50, 'stop_basis': 'original_signal_price'}


def plan(path):
    path = Path(path)
    state = json.loads(path.read_text(encoding='utf-8'))
    if state.get('version') != 1 or state.get('settings') not in (legacy_settings(), current_settings()):
        raise ValueError('Unrecognized state settings; no files changed')
    if state['settings'] == current_settings():
        return state, False
    if state.get('account_positions') != {} or state.get('strategy_positions') != {}:
        raise ValueError('Open or inconsistent positions: finish them under the old version first; no files changed')
    if not isinstance(state.get('trades'), list) or not isinstance(state.get('last_trade'), dict):
        raise ValueError('Incomplete ledger; no files changed')
    wallet = state.get('balance_usdt')
    if not isinstance(wallet, (int,float)) or not math.isfinite(wallet):
        raise ValueError('Invalid wallet; no files changed')
    if not all(math.isclose(float(state[k]), float(v), abs_tol=1e-8) for k,v in (
        ('used_usdt',0),('equity_usdt',wallet),('free_usdt',max(0,wallet)))):
        raise ValueError('Idle wallet totals are inconsistent; no files changed')
    state['settings'] = current_settings()
    return state, True


def migrate(path):
    path = Path(path)
    state, changed = plan(path)
    if not changed:
        return None
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup = path.with_name(path.name+'.before-risk-update-'+stamp+'.bak')
    shutil.copy2(path, backup)
    temp = path.with_name(path.name+'.migrating')
    temp.write_text(json.dumps(state,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    temp.replace(path)
    return backup


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('state',type=Path)
    parser.add_argument('--apply',action='store_true',help='Write migration after backing up the original')
    args=parser.parse_args()
    try:
        _,changed=plan(args.state)
        if not changed:print('State already matches current settings.');return
        if not args.apply:print('Idle legacy state is compatible. Stop all writers; rerun with --apply.');return
        backup=migrate(args.state)
        print('Migrated idle state; wallet and fills preserved. Backup:',backup)
    except (OSError,ValueError,KeyError,TypeError) as error:
        parser.exit(1,str(error)+'\n')

if __name__=='__main__':main()
