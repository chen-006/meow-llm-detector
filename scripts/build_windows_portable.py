"""Build Windows x64 portable ZIPs from the pinned, published v4.5.0 source."""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

TOOLS_ROOT = Path(__file__).resolve().parents[1]
SOURCE_SHA = '67ea19892c5d75ba2076c08e19229d1d4b7184fa'
PYTHON_VERSION = '3.13.15'
PYTHON_URL = f'https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-embed-amd64.zip'
PYTHON_SHA256 = 'd1f04d990aee1253d8569e8e5104e30fa9f5fa830899f14843448872d936a2cf'


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value.encode('utf-8') if isinstance(value, str) else value)


def build(source, output):
    if os.name != 'nt' or sys.version_info[:2] != (3, 13):
        raise SystemExit('Build with 64-bit Python 3.13 on Windows')
    import struct
    assert struct.calcsize('P') == 8
    assert subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip() == SOURCE_SHA
    assert not subprocess.check_output(['git', '-C', str(source), 'status', '--porcelain'], text=True).strip()
    source_files = runpy.run_path(str(source / 'scripts/build_release.py'))['release_files'](source)
    assert source_files['VERSION'].strip() == b'4.5.0'
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as work:
        work = Path(work)
        runtime_zip = work / 'python.zip'
        urllib.request.urlretrieve(PYTHON_URL, runtime_zip)
        assert digest(runtime_zip) == PYTHON_SHA256, 'Official Python checksum mismatch'
        runtime = work / 'portable-python'
        with zipfile.ZipFile(runtime_zip) as archive:
            archive.extractall(runtime)
        # Keep the embedded runtime isolated; explicitly add only its dependencies and app root.
        write(runtime / 'python313._pth', 'python313.zip\n.\nLib/site-packages\n..\nimport site\n')
        wheels = work / 'wheels'
        subprocess.run([sys.executable, '-m', 'pip', 'download', '--only-binary=:all:',
                        '--dest', str(wheels), '-r', str(source / 'requirements.txt')], check=True)
        report = work / 'install-report.json'
        site_packages = runtime / 'Lib/site-packages'
        subprocess.run([sys.executable, '-m', 'pip', 'install', '--no-index', '--find-links', str(wheels),
                        '--only-binary=:all:', '--no-compile', '--target', str(site_packages),
                        '--report', str(report), '-r', str(source / 'requirements.txt')], check=True)
        # Console entry points contain the CI interpreter path and are not used by the application.
        shutil.rmtree(site_packages / 'bin', ignore_errors=True)
        for cache in list(runtime.rglob('__pycache__')):
            shutil.rmtree(cache)
        packages = sorted(({'name': d.metadata['Name'], 'version': d.version}
                           for d in importlib.metadata.distributions(path=[str(site_packages)])), key=lambda p: p['name'].lower())
        manifest = {'source_commit': SOURCE_SHA, 'builder_commit': os.environ.get('GITHUB_SHA'),
                    'python_version': PYTHON_VERSION, 'python_url': PYTHON_URL,
                    'python_sha256': PYTHON_SHA256, 'platform': 'windows-x64',
                    'packages': packages, 'wheels': {p.name: digest(p) for p in sorted(wheels.glob('*.whl'))}}
        install_report = json.loads(report.read_text(encoding='utf-8'))
        # Strip temporary CI paths; retain the wheel hashes and dependency metadata.
        for item in install_report['install']:
            item['download_info']['url'] = Path(item['download_info']['url']).name
        manifest['installation'] = install_report
        checksums = []
        for locale, language in [('zh-CN', 'CN'), ('en', 'EN')]:
            name = f'meow-llm-detector-v4.5.0-windows-x64-portable-{locale}'
            folder = work / name
            folder.mkdir()
            for filename, raw in source_files.items():
                write(folder / filename, raw)
            for lang in ('CN', 'EN'):
                shutil.move(folder / f'README_{lang}.md', folder / f'README_SOURCE_{lang}.md')
                write(folder / f'README_{lang}.md', (TOOLS_ROOT / f'docs/WINDOWS_PORTABLE_{lang}.md').read_bytes())
            shutil.copyfile(folder / f'README_{language}.md', folder / 'README.md')
            shutil.copytree(runtime, folder / 'portable-python')
            shutil.copyfile(TOOLS_ROOT / 'scripts/portable_launcher.py', folder / 'launch_portable.py')
            write(folder / 'locale.json', json.dumps({'locale': locale}) + '\n')
            write(folder / 'PORTABLE_BUILD.json', json.dumps(manifest, indent=2, ensure_ascii=False) + '\n')
            missing = ('Portable Python is missing. Extract the entire ZIP again.' if locale == 'en' else '便携 Python 缺失，请重新完整解压 ZIP。')
            start = ('@echo off\nsetlocal\nchcp 65001 >nul\ncd /d "%~dp0"\n'
                     'if not exist "%~dp0portable-python\\python.exe" (\n'
                     f'  echo {missing}\n  pause\n  exit /b 1\n)\n'
                     '"%~dp0portable-python\\python.exe" -B -X utf8 "%~dp0launch_portable.py" %*\n'
                     'if errorlevel 1 (\n  pause\n  exit /b 1\n)\n')
            write(folder / 'start.bat', start.replace('\n', '\r\n'))
            entries = sorted(p for p in folder.rglob('*') if p.is_file() and p != folder / 'SHA256SUMS.txt')
            write(folder / 'SHA256SUMS.txt', ''.join(f'{digest(p)}  {p.relative_to(folder).as_posix()}\n' for p in entries))
            target = output / (name + '.zip')
            if target.exists():
                raise FileExistsError(target)
            with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                for path in sorted(p for p in folder.rglob('*') if p.is_file()):
                    info = zipfile.ZipInfo(name + '/' + path.relative_to(folder).as_posix(), date_time=(2026, 9, 5, 0, 0, 0))
                    info.create_system = 3
                    info.external_attr = 0o100644 << 16
                    archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=6)
            checksums.append(f'{digest(target)}  {target.name}\n')
            print(f'Built {target.name}: {target.stat().st_size} bytes', flush=True)
        write(output / 'PORTABLE_SHA256SUMS.txt', ''.join(checksums))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    build(args.source.resolve(), args.output.resolve())
