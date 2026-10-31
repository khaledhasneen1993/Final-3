"""Back up risk updates: prior 7% accounts may retain open positions; older risk modes must be idle."""
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


def previous_protection_settings():
    return {k:v for k,v in current_settings().items() if k not in (
        'post_add_stop','partial_adverse','partial_fraction','break_even_after_add')}


def legacy_settings():
    return {**previous_protection_settings(), 'MAX_POSITION_PCT': .10, 'STOP_LOSS_PCT': .15,
            'MAX_CONCURRENT_POSITIONS': 3, 'add_levels': [.05, .10],
            'add_fraction': .50, 'stop_basis': 'original_signal_price'}


def previous_final3_settings():
    return {**previous_protection_settings(), 'STOP_LOSS_PCT': .05}


def plan(path):
    path = Path(path)
    state = json.loads(path.read_text(encoding='utf-8'))
    if state.get('version') != 1 or state.get('settings') not in (legacy_settings(), previous_final3_settings(), previous_protection_settings(), current_settings()):
        raise ValueError('Unrecognized state settings; no files changed')
    if state['settings'] == current_settings():
        return state, False
    prior_seven = state['settings'] == previous_protection_settings()
    if prior_seven:
        if not isinstance(state.get('account_positions'),dict) or not isinstance(state.get('strategy_positions'),dict):
            raise ValueError('Invalid saved positions; no files changed')
        if set(state['account_positions']) != set(state['strategy_positions']):
            raise ValueError('Inconsistent saved positions; no files changed')
        for symbol, fill in state['account_positions'].items():
            p = state['strategy_positions'][symbol]
            if (fill.get('side') not in ('buy','sell') or p.get('side') != fill['side']
                    or any(not isinstance(fill.get(k),(int,float)) or not math.isfinite(fill[k]) or fill[k] <= 0 for k in ('entry','amount','margin'))
                    or not math.isclose(p.get('amount',0),fill['amount'])):
                raise ValueError('Invalid saved position; no files changed')
    elif state.get('account_positions') != {} or state.get('strategy_positions') != {}:
        raise ValueError('Open or inconsistent positions: finish them under the old version first; no files changed')
    if not isinstance(state.get('trades'), list) or not isinstance(state.get('last_trade'), dict):
        raise ValueError('Incomplete ledger; no files changed')
    wallet = state.get('balance_usdt')
    if not isinstance(wallet, (int,float)) or not math.isfinite(wallet):
        raise ValueError('Invalid wallet; no files changed')
    if (not prior_seven or not state.get('account_positions')) and not all(math.isclose(float(state[k]), float(v), abs_tol=1e-8) for k,v in (
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
    parser.add_argument('--apply',action='store_true',help='Write migration after backing up the original; all writers must be stopped')
    args=parser.parse_args()
    try:
        _,changed=plan(args.state)
        if not changed:print('State already matches current settings.');return
        if not args.apply:print('Saved state is compatible with the risk update. Stop all writers; rerun with --apply.');return
        backup=migrate(args.state)
        print('Migrated idle state; wallet and fills preserved. Backup:',backup)
    except (OSError,ValueError,KeyError,TypeError) as error:
        parser.exit(1,str(error)+'\n')

if __name__=='__main__':main()
