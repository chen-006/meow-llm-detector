# Contributing benchmarks

[中文](CONTRIBUTING.md)

Export a calibrated `.meow.json` from the generator and inspect its URLs, prompts, and metadata for private information. Never submit keys, runtime databases, or unchecked raw responses.

1. Fork and add `benchmarks/community/<package-id>/<version>.meow.json`.
2. Add an index entry following the official structure, with publisher=`community` and a community path. `sha256` hashes actual file bytes; `content_sha256` comes from the package. Recomputing a content hash is not calibration.
3. Open a pull request. CI checks structure, both digests, and calibration binding without executing package code or sending model requests. A maintainer reviews before merging; checks are not identity certification.

Do not overwrite an existing ID/version. Security withdrawals use `"withdrawn":"security"` in the maintained index, with an explanation. Offline clients learn of new withdrawals only after refreshing.

Run public tests for code changes. Use synthetic credentials/local fake responses, not paid providers in CI. Disclose missing independent validation instead of claiming99% real-world accuracy.
