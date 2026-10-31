"""Validate or migrate only an idle Final-3 account under its writer lock."""
import fcntl
import sys
from paths import ROOT, STATE_PATHS
sys.path.insert(0, str(ROOT))
from migrate_idle_state import migrate

def prepare():
    path = STATE_PATHS['final3']
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name+'.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if path.exists():
            backup = migrate(path)
            if backup:
                print('Final-3 idle settings updated to 7% stop; wallet and fills preserved. Backup:', backup)
            else:
                print('Final-3 account settings match; resuming saved account.')
        else:
            print('No saved Final-3 account; starting a separate 100 USDT paper account.')

if __name__ == '__main__':
    prepare()
