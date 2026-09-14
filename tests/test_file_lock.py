import os
from pathlib import Path
import subprocess
import sys

from serein.file_lock import exclusive_lock


def test_lock_excludes_another_process_then_releases(tmp_path):
    lock=tmp_path/'operation.lock'
    code='from serein.file_lock import exclusive_lock; import sys\nwith exclusive_lock(sys.argv[1]): print("acquired")'
    env=dict(os.environ,PYTHONPATH=str(Path(__file__).parents[1]/'src'),PYTHONUTF8='1')
    with exclusive_lock(lock):
        blocked=subprocess.run([sys.executable,'-c',code,str(lock)],env=env,capture_output=True,text=True,encoding='utf-8')
        assert blocked.returncode!=0 and 'RuntimeError' in blocked.stderr
    acquired=subprocess.run([sys.executable,'-c',code,str(lock)],env=env,capture_output=True,text=True,encoding='utf-8')
    assert acquired.returncode==0 and 'acquired' in acquired.stdout
