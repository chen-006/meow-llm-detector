"""Launch the vendored Windows application without pip or a system Python."""
import argparse
import json
import os
from pathlib import Path
import threading
import webbrowser


def main():
    root = Path(__file__).resolve().parent
    os.chdir(root)
    locale = json.loads((root / 'locale.json').read_text(encoding='utf-8'))['locale']
    parser = argparse.ArgumentParser()
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    from gpt56_vnext.server import create_server
    server = create_server(port=args.port, locale=locale)
    url = f'http://127.0.0.1:{server.server_port}/?lang=' + ('en' if locale == 'en' else 'zh-CN')
    timer = None
    if not args.no_browser:
        timer = threading.Timer(1, webbrowser.open, args=(url,))
        timer.daemon = True
        timer.start()
    print(('Close this terminal to stop. ' if locale == 'en' else '关闭此终端可停止后台。 ') + url, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if timer:
            timer.cancel()
        server.server_close()


if __name__ == '__main__':
    main()
