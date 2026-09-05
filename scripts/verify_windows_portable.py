"""Test the actual archives after relocation with system Python removed from PATH."""
import argparse
import hashlib
import http.cookiejar
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
import zipfile


def verify(archives):
    assert os.name == 'nt'
    for index, archive_path in enumerate(sorted(archives.glob('*portable-*.zip'))):
        with tempfile.TemporaryDirectory(prefix='meow smoke ') as temp:
            temp = Path(temp)
            with zipfile.ZipFile(archive_path) as archive:
                assert archive.testzip() is None
                archive.extractall(temp)
            folder = temp / archive_path.stem
            moved = temp / '中文路径 with spaces' / 'app moved'
            moved.parent.mkdir()
            shutil.move(folder, moved)
            folder = moved
            for line in (folder / 'SHA256SUMS.txt').read_text().splitlines():
                expected, name = line.split('  ', 1)
                assert hashlib.sha256((folder / name).read_bytes()).hexdigest() == expected, name
            env = os.environ.copy()
            env['PATH'] = str(Path(env['SYSTEMROOT']) / 'System32')
            env['PYTHONHOME'] = str(temp / 'invalid-system-python')
            env['PYTHONPATH'] = str(temp / 'invalid-system-packages')
            env['PYTHONUSERBASE'] = str(temp / 'invalid-user-packages')
            env['HTTP_PROXY'] = env['HTTPS_PROXY'] = 'http://127.0.0.1:1'
            python = str(folder / 'portable-python/python.exe')
            check = '''import sys, ssl, sqlite3, httpx, keyring, numpy, json, uuid
from pathlib import Path
root = Path(sys.executable).resolve().parents[1]
assert sys.flags.isolated and sys.flags.no_user_site
assert all(Path(p).resolve().is_relative_to(root) for p in sys.path)
assert httpx.__version__ == '0.28.1' and numpy.__version__ == '2.3.5'
assert 'Windows' in type(keyring.get_keyring()).__module__
identity = 'meow-portable-smoke-' + uuid.uuid4().hex
try:
    keyring.set_password(identity, 'test', 'synthetic-smoke-not-real')
    assert keyring.get_password(identity, 'test') == 'synthetic-smoke-not-real'
finally:
    keyring.delete_password(identity, 'test')
ssl.create_default_context()
with sqlite3.connect(':memory:') as db: assert db.execute('select 1').fetchone()[0] == 1
print('Isolated runtime, NumPy, HTTPS and Windows credential round-trip passed')
'''
            subprocess.run([python, '-B', '-X', 'utf8', '-c', check], cwd=temp, env=env, check=True, timeout=60)
            if index == 0:
                subprocess.run([python, '-B', '-X', 'utf8', '-m', 'unittest', 'discover', '-s', str(folder / 'tests'), '-v'],
                               cwd=folder, env=env, check=True, timeout=180)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            url = f'http://127.0.0.1:{port}'
            log_path = temp / 'launcher.log'
            with log_path.open('wb') as log:
                command = f'call "{folder / "start.bat"}" --no-browser --port {port}'
                process = subprocess.Popen(command, shell=True, cwd=temp, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log)
                try:
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
                    deadline = time.monotonic() + 45
                    while True:
                        try:
                            with opener.open(url + '/', timeout=2) as response:
                                assert response.status == 200
                                assert b'/assets/app.js' in response.read()
                            break
                        except OSError:
                            if process.poll() is not None or time.monotonic() > deadline:
                                raise AssertionError(log_path.read_text(encoding='utf-8', errors='replace'))
                            time.sleep(0.25)
                    with opener.open(url + '/api/bootstrap', timeout=5) as response:
                        assert response.status == 200
                        json.load(response)
                    with opener.open(url + '/assets/workbench.js', timeout=5) as response:
                        assert response.read() == (folder / 'gpt56_vnext/web/workbench.js').read_bytes()
                    assert not (folder / '.venv').exists()
                    assert (folder / 'meow_runs/state.sqlite3').exists()
                    print(f'PASS {archive_path.name}: moved Unicode/space path; start.bat; no system Python; no pip; HTTP UI', flush=True)
                finally:
                    subprocess.run([str(Path(env['SYSTEMROOT']) / 'System32/taskkill.exe'), '/PID', str(process.pid), '/T', '/F'], capture_output=True)
                    process.wait(timeout=15)
    assert len(list(archives.glob('*portable-*.zip'))) == 2


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archives', type=Path)
    verify(parser.parse_args().archives.resolve())
