from __future__ import annotations

import hashlib
from copy import deepcopy
from pathlib import Path
import re

import httpx

from .benchmark import MAX_PACKAGE_BYTES, identifier, load_package
from .errors import AppError
from .proxies import http_client_options
from .utils import atomic_write_json, recognized_provider, strict_json_loads, utc_now

REPOSITORY = "chen-006/meow-llm-detector"
INDEX_PATH = "benchmarks/index.json"


def validate_index(value: dict) -> dict:
    if not isinstance(value, dict) or value.get("schema_version") != 1 or not isinstance(value.get("packages"), list):
        raise AppError("invalid_catalog")
    seen = set()
    for item in value["packages"]:
        if not isinstance(item, dict):
            raise AppError("invalid_catalog_entry")
        identity = (identifier(item.get("id")), item.get("version"))
        if not isinstance(identity[1], str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9.-]+)?", identity[1]):
            raise AppError("invalid_catalog_version")
        if identity in seen or item.get("mode") not in {"gpt", "claude", "chat"}:
            raise AppError("invalid_catalog_entry")
        seen.add(identity)
        if item.get("publisher") not in {"maintainer", "community"}:
            raise AppError("invalid_publisher")
        path = item.get("path", "")
        prefix = "benchmarks/official/" if item["publisher"] == "maintainer" else "benchmarks/community/"
        if not isinstance(path, str) or not path.startswith(prefix) or ".." in path or "\\" in path or not path.endswith(".meow.json"):
            raise AppError("invalid_catalog_path")
        if any(not re.fullmatch(r"[0-9a-f]{64}", str(item.get(key, ""))) for key in ("sha256", "content_sha256")):
            raise AppError("invalid_catalog_hash")
    return value


async def download_bytes(url: str, maximum: int) -> bytes:
    async with httpx.AsyncClient(**http_client_options(url), timeout=30) as client:
        async with client.stream("GET", url, headers={"User-Agent": "meow-llm-detector/4.5.0"}) as response:
            if response.status_code != 200:
                raise AppError("catalog_unavailable", status=502)
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > maximum:
                    raise AppError("download_too_large")
            return bytes(body)


class BenchmarkCatalog:
    def __init__(self, root: Path, bundled_root: Path | None = None):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = root / "index.json"
        self.bundled = {}
        self._packages = {}
        if bundled_root is not None:
            manifest = strict_json_loads((bundled_root / "manifest.json").read_bytes())
            for item in manifest["packages"]:
                package = load_package((bundled_root / item["file"]).read_bytes())
                if (package["id"], package["version"], package["content_sha256"]) != (item["id"], item["version"], item["content_sha256"]):
                    raise AppError("bundled_package_mismatch")
                self.install_local(package)
                self.bundled[(package["id"], package["version"])] = package["content_sha256"]

    def index(self):
        if not self.index_path.is_file():
            return {"schema_version": 1, "packages": [], "status": "not_connected"}
        return validate_index(strict_json_loads(self.index_path.read_bytes()))

    async def refresh(self):
        commit = strict_json_loads(await download_bytes(f"https://api.github.com/repos/{REPOSITORY}/commits/main", 1024 * 1024)).get("sha")
        if not re.fullmatch(r"[a-f0-9]{40}", str(commit)):
            raise AppError("catalog_commit_invalid")
        index = validate_index(strict_json_loads(await download_bytes(
            f"https://raw.githubusercontent.com/{REPOSITORY}/{commit}/{INDEX_PATH}", 1024 * 1024)))
        snapshot = {**index, "commit": commit, "checked_at": utc_now()}
        atomic_write_json(self.index_path, snapshot)
        return snapshot

    def _path(self, package):
        return self.root / f"{package['id']}--{package['version']}.meow.json"

    def _load(self, path):
        stat = path.stat()
        stamp = (stat.st_mtime_ns, stat.st_size, stat.st_ctime_ns)
        cached = self._packages.get(path)
        if cached is None or cached[0] != stamp:
            cached = (stamp, load_package(path.read_bytes()))
            self._packages[path] = cached
        return cached[1]

    def install_local(self, value) -> dict:
        package = load_package(value)
        path = self._path(package)
        if path.exists():
            if self._load(path)["content_sha256"] != package["content_sha256"]:
                raise AppError("immutable_version_conflict")
            return package
        atomic_write_json(path, package)
        return package

    async def install(self, identity: str, version: str) -> dict:
        index = self.index()
        item = next((item for item in index["packages"] if item["id"] == identity and item["version"] == version), None)
        if not item or item.get("withdrawn") == "security":
            raise AppError("catalog_package_unavailable")
        commit = index.get("commit", "")
        if not re.fullmatch(r"[a-f0-9]{40}", commit):
            raise AppError("catalog_commit_invalid")
        raw = await download_bytes(f"https://raw.githubusercontent.com/{REPOSITORY}/{commit}/{item['path']}", MAX_PACKAGE_BYTES)
        if hashlib.sha256(raw).hexdigest() != item["sha256"]:
            raise AppError("download_hash_mismatch")
        package = load_package(raw)
        if package["content_sha256"] != item["content_sha256"]:
            raise AppError("catalog_identity_mismatch")
        if (package["id"], package["version"], package["mode"]) != (identity, version, item["mode"]):
            raise AppError("catalog_identity_mismatch")
        return self.install_local(package)

    def local(self):
        result = []
        index = self.index()
        for path in sorted(self.root.glob("*.meow.json")):
            package = self._load(path)
            entry = next((item for item in index["packages"] if item["id"] == package["id"] and item["version"] == package["version"]), None)
            # A local import cannot promote itself to maintainer status.
            trusted = bool(entry and index.get("commit") and entry.get("content_sha256") == package["content_sha256"])
            bundled = self.bundled.get((package["id"], package["version"])) == package["content_sha256"]
            result.append({"id": package["id"], "version": package["version"], "mode": package["mode"],
                           "name": package["metadata"]["name"], "models": package["models"],
                           "publisher": entry["publisher"] if trusted else "maintainer" if bundled else "local",
                           "bundled": bundled,
                           "source_providers": sorted({provider for source in package["collection"].get("sources", []) if (provider := recognized_provider(source["url"]))}),
                           "collection": package["collection"], "withdrawn": entry.get("withdrawn") if trusted else None,
                           "content_sha256": package["content_sha256"]})
        return deepcopy(sorted(result, key=lambda item: (item["id"], tuple(map(int, item["version"].split("-")[0].split("."))), "-" not in item["version"], item["version"]), reverse=True))

    def get(self, identity, version):
        identifier(identity)
        if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9.-]+)?", str(version)):
            raise AppError("invalid_version")
        path = self._path({"id": identity, "version": version})
        if not path.is_file():
            raise AppError("package_not_installed", status=404)
        package = self._load(path)
        self.check_withdrawal(package)
        return deepcopy(package)

    def check_withdrawal(self, package):
        """Current local safety policy applies to frozen packages too; no download."""
        index = self.index()
        entry = next((item for item in index["packages"] if item["id"] == package["id"] and item["version"] == package["version"]), None)
        if entry and index.get("commit") and entry["content_sha256"] == package["content_sha256"] and entry.get("withdrawn") == "security":
            raise AppError("package_withdrawn")
