"""Measure complete CLI launches against one staged one-line text file."""
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tempfile
import time

cli = Path(__file__).resolve().parents[1] / 'jev0.py'
with tempfile.TemporaryDirectory() as directory:
    env = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)
    subprocess.run(['git', 'init', '-q', directory], env=env, check=True)
    (Path(directory) / 'sample.txt').write_text('sample\n')
    subprocess.run(['git', 'add', 'sample.txt'], cwd=directory, env=env, check=True)
    samples = []
    for _ in range(50):
        start = time.perf_counter()
        subprocess.run([sys.executable, str(cli), 'staged'], cwd=directory, env=env, check=True)
        samples.append((time.perf_counter() - start) * 1000)
print(json.dumps({'platform': platform.platform(), 'python': platform.python_version(),
                  'workload': 'one staged text file, one added line',
                  'samples': len(samples), 'median_ms': round(statistics.median(samples), 2),
                  'p95_ms': round(sorted(samples)[47], 2),
                  'note': 'fresh CLI processes; OS caches are not flushed'}, indent=2))
