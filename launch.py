"""Small source-distribution launcher; no global installs or silent upgrades."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import webbrowser


def main():
    root = Path(__file__).resolve().parent
    locale_path = root / 'locale.json'
    locale = json.loads(locale_path.read_text(encoding='utf-8'))['locale'] if locale_path.exists() else 'zh-CN'
    english = locale == 'en'
    if sys.version_info < (3, 11):
        raise SystemExit('Python 3.11+ required / 需要 Python 3.11 或更新版本')
    env = root / '.venv'
    python = env / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    requirements = root / 'requirements.txt'
    digest = hashlib.sha256(requirements.read_bytes()).hexdigest()
    stamp = env / 'meow-requirements.sha256'
    if not python.exists() or not stamp.exists() or stamp.read_text().strip() != digest:
        prompt = 'Install dependencies into .venv using pip? [y/N] ' if english else '是否联网安装依赖到本目录 .venv？[y/N] '
        if input(prompt).strip().lower() not in {'y', 'yes'}:
            return
        if not python.exists():
            subprocess.run([sys.executable, '-m', 'venv', str(env)], check=True)
        subprocess.run([str(python), '-m', 'pip', 'install', '-r', str(requirements)], check=True)
        stamp.write_text(digest, encoding='ascii')
    url = 'http://127.0.0.1:8765/?lang=' + ('en' if english else 'zh-CN')
    timer = threading.Timer(2, webbrowser.open, args=(url,))
    timer.daemon = True
    timer.start()
    print(('Close this terminal to stop. ' if english else '关闭此终端可停止后台。 ') + url, flush=True)
    try:
        return subprocess.call([str(python), '-B', '-m', 'gpt56_vnext', '--locale', locale], cwd=root)
    except KeyboardInterrupt:
        return 0
    finally:
        timer.cancel()


if __name__ == '__main__':
    raise SystemExit(main())
