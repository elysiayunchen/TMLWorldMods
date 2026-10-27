# -*- coding: utf-8 -*-
"""跨进程验证单实例互斥锁：持有者存活时，第二个进程必须拿不到锁。"""
import subprocess
import sys
import textwrap
import time
import os

os.chdir(os.path.dirname(os.path.abspath(__file__)))
py = r"C:\Users\19645\miniconda3\python.exe"

holder = textwrap.dedent("""
    import tml_world_mods as t, time
    assert t.acquire_single_instance() is True
    time.sleep(6)
""")
p = subprocess.Popen([py, "-c", holder])
time.sleep(2.5)
r = subprocess.run(
    [py, "-c",
     "import tml_world_mods as t; print('second-acquire:', t.acquire_single_instance())"],
    capture_output=True, text=True)
print(r.stdout.strip())
p.wait()
ok = "second-acquire: False" in r.stdout
print("MUTEX-TEST", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
