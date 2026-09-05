from __future__ import annotations

from dataclasses import dataclass
import fnmatch
import ipaddress
import os
import re
from typing import Any, Callable
import urllib.parse


def http_client_options(url: str) -> dict:
    """One proxy policy for model calls, catalog files and release downloads."""
    host = urllib.parse.urlsplit(url).hostname
    try:
        local = ipaddress.ip_address(host).is_loopback
    except ValueError:
        local = host == "localhost"
    return {"proxy": None if local else resolve_proxy(url).proxy_url,
            "trust_env": False, "follow_redirects": False}

@dataclass(frozen=True)
class ProxyDecision:
    mode: str
    proxy_url: str | None
    source: str


def _environment_value(environment: dict[str, str], name: str) -> str:
    for key in (name, name.casefold()):
        value = str(environment.get(key) or "").strip()
        if value:
            return value
    return ""


def _proxy_host_matches(host: str, port: int | None, raw_rules: str, *, windows: bool = False) -> bool:
    host = host.casefold().strip("[]")
    for raw_rule in re.split(r"[;,]" if windows else r",", raw_rules or ""):
        rule = raw_rule.strip().casefold()
        if not rule:
            continue
        if windows and rule == "<local>" and "." not in host:
            return True
        if rule == "*":
            return True
        if ":" in rule and not rule.startswith("["):
            candidate, separator, candidate_port = rule.rpartition(":")
            if separator and candidate_port.isdigit():
                if port != int(candidate_port):
                    continue
                rule = candidate
        rule = rule.lstrip(".")
        if "*" in rule:
            if fnmatch.fnmatch(host, rule):
                return True
        elif host == rule or host.endswith("." + rule):
            return True
    return False


def _normalize_http_proxy(value: str) -> str:
    raw = value.strip()
    if not raw:
        raise ValueError("代理地址为空")
    if "://" not in raw:
        raw = "http://" + raw
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme.casefold() != "http":
        raise ValueError("仅支持HTTP/mixed代理端口；请改用HTTP代理端口或系统级TUN")
    if not parsed.hostname or parsed.port is None:
        raise ValueError("HTTP代理地址必须包含主机和端口")
    return urllib.parse.urlunsplit(("http", parsed.netloc, "", "", ""))


def _parse_windows_proxy_server(server: str) -> str:
    entries: dict[str, str] = {}
    fallback = ""
    for item in str(server or "").split(";"):
        item = item.strip()
        if not item:
            continue
        if "=" in item:
            protocol, value = item.split("=", 1)
            entries[protocol.strip().casefold()] = value.strip()
        elif not fallback:
            fallback = item
    return entries.get("https") or fallback or entries.get("http") or ""


def read_windows_manual_proxy() -> dict[str, Any]:
    if os.name != "nt":
        return {"enabled": False, "server": "", "bypass": "", "auto_config_url": ""}
    try:
        import winreg

        path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            def read(name: str, default: Any) -> Any:
                try:
                    return winreg.QueryValueEx(key, name)[0]
                except OSError:
                    return default

            return {
                "enabled": bool(read("ProxyEnable", 0)),
                "server": str(read("ProxyServer", "") or ""),
                "bypass": str(read("ProxyOverride", "") or ""),
                "auto_config_url": str(read("AutoConfigURL", "") or ""),
            }
    except OSError:
        return {"enabled": False, "server": "", "bypass": "", "auto_config_url": ""}


def resolve_proxy(
    target_url: str,
    environment: dict[str, str] | None = None,
    windows_proxy_reader: Callable[[], dict[str, Any]] = read_windows_manual_proxy,
) -> ProxyDecision:
    environment = dict(os.environ if environment is None else environment)
    target = urllib.parse.urlsplit(target_url)
    host = target.hostname or ""
    port = target.port or (443 if target.scheme.casefold() == "https" else 80)
    no_proxy = _environment_value(environment, "NO_PROXY")
    if no_proxy and _proxy_host_matches(host, port, no_proxy):
        return ProxyDecision("direct", None, "bypass")
    for name, source in (
        ("HTTPS_PROXY", "environment_https"),
        ("ALL_PROXY", "environment_all"),
        ("HTTP_PROXY", "environment_http"),
    ):
        value = _environment_value(environment, name)
        if value:
            return ProxyDecision("proxy", _normalize_http_proxy(value), source)
    settings = windows_proxy_reader() or {}
    if bool(settings.get("enabled")):
        bypass = str(settings.get("bypass") or "")
        if bypass and _proxy_host_matches(host, port, bypass, windows=True):
            return ProxyDecision("direct", None, "bypass")
        value = _parse_windows_proxy_server(str(settings.get("server") or ""))
        if value:
            return ProxyDecision("proxy", _normalize_http_proxy(value), "windows_manual")
    if str(settings.get("auto_config_url") or "").strip():
        raise ValueError("不解析PAC脚本；请改用HTTP/mixed代理端口或系统级TUN")
    return ProxyDecision("direct", None, "none")
