# meow LLM Detector v4.5.0

[中文](README_CN.md) · [Download](https://github.com/chen-006/meow-llm-detector/releases/latest)

Download and extract the English ZIP. On Windows, double-click `start.bat`; on macOS / Linux, run `sh start.sh` inside the folder. Install Python 3.11 or newer first. The launcher asks before installing dependencies. In the browser, choose a model, enter your API URL and key, and click Start.

Research reference: [One Token Is Enough](https://arxiv.org/abs/2607.10252). Community links: [Linux.do discussion](https://linux.do/t/topic/2704354) · [Routing discussion](https://linux.do/t/topic/2728901). Implementation inspiration and thanks: [hlwy-ai-checker](https://github.com/hanlinwenyuan/hlwy-ai-checker).

## What does it do?

It sends a few short questions to your API and compares the answer distribution with reference models. Ask different models to name an arbitrary country, for example, and their preferred answers can differ.

The app runs locally and includes GPT and Claude benchmarks. You do not need a separate reference API to run a test. Green means a strong match to your claimed model; yellow means insufficient evidence; red means a strong match to another candidate. **It is not an identity certificate or proof that a provider is cheating.**

## Using it

1. Choose GPT, Claude, or Other. Bundled GPT candidates are Astra, Sol, Terra, and Luna. Claude candidates are Fable 5.1, Opus 5, Sonnet 5, and Haiku 4.5.
2. Enter the provider's API base URL, such as `https://example.com/v1`. The request model is filled in automatically; edit it if your provider uses an alias. OpenRouter usually also needs a provider prefix.
3. Enter your key and choose low, medium, or high. Bundled benchmarks send **20 / 50 / 100 requests**, excluding retries. Your provider charges for these calls.
4. Start. The report lists the benchmark collection URL separately from this run's URL, plus model scores and thresholds.

Optional request/response retention is next to Start, with export after the run. Your temporary key stays on the current page for repeat tests and is cleared on reload, close, or a detected backend disconnect. Saved API connections keep keys in the OS credential vault, not ordinary files. Check URLs and response contents for private information before sharing reports.

## Benchmarks, generation, and updates

- Both benchmarks ship with the app. The Benchmark library can fetch new packages from the [public index](benchmarks/index.json). [Package files](benchmarks/official) are published too. Installed packages work offline.
- Generate benchmark supports manual entry, import, and AI candidate generation, followed by reference sampling, selection suggestions, simulation, and export. AI and sampling calls cost money; local simulation does not call a model.
- New collection windows must be at least one minute apart. You can choose probes manually instead of following recommendations.
- Scheduled runs are separate tests, not accumulated evidence. Check for updates finds a release and asks before downloading it. Extract into a new folder and restart; the app does not replace running files.
- v4.5.0 removes Juice, long-context and tool wrappers. Node.js is not required.

See the [technical report](TECHNICAL_REPORT_EN.md) for details and reproduction commands. Both languages share the same code. **Switching the UI language does not translate calibrated prompts.**

## Startup help

Install [Python](https://www.python.org/downloads/) first; enable PATH during Windows installation. Dependency installation needs internet access. The launcher creates a local `.venv`, does not install Python automatically, and does not request administrator privileges. macOS / Linux need a working OS credential backend to save connections; temporary keys still work without it.

Manual startup:

```sh
python -m venv .venv
# Windows interpreter: .venv\Scripts\python.exe
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -B -m gpt56_vnext --locale en
```

Open `http://127.0.0.1:8765/`. Close the launcher terminal to stop the backend. Local data is stored in `meow_runs`. Try updates in a new folder; stop the old backend before copying that data directory if you want to migrate it.

## Limits and license

Fixed probes can receive special routing. Model updates, hidden prompts, and sampling settings can change distributions. Same-pool simulations are not real-provider accuracy measurements. Yellow does not necessarily mean a bad model, and green does not prove every request goes to the same model. Prefer a revocable, spending-limited key and do not expose the local server to the internet.

Licensed under [PolyForm Noncommercial 1.0.0](LICENSE), with attribution to chen-006 and contributors. This is not an OSI-approved open-source license; use and redistribution remain subject to its terms.
