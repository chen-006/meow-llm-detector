# meow LLM Detector v4.5.0 · Windows portable edition

For Windows 10 / 11, Intel / AMD 64-bit (x64). Includes Python 3.13.15 and all application dependencies. No Python installation, PATH changes or first-run dependency downloads. Model API calls still require a network connection.

1. Download `windows-x64-portable-en.zip` (Chinese: `zh-CN`).
2. **Extract everything into a new writable folder and double-click `start.bat`.** Do not run inside the ZIP or remove `portable-python`.
3. If the browser does not open, visit `http://127.0.0.1:8765/`. Keep the terminal open while using the app; close it to stop.

Data lives in `meow_runs`. Stop the old backend before copying this folder into a new installation. Keys in Windows Credential Manager do not move to another PC with the folder.

The detection core and benchmarks match standard v4.5.0; no recalibration is needed. For macOS / Linux, use the original source archives. Installation instructions in `README_SOURCE_EN.md` apply only to source packages; portable users should run `start.bat`, not `launch.py`.

Build information: `PORTABLE_BUILD.json`. File checksums: `SHA256SUMS.txt`. Python license: `portable-python/LICENSE.txt`; dependency licenses remain in their `.dist-info` directories. Statistical limitations: `TECHNICAL_REPORT_EN.md`.
