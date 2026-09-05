from __future__ import annotations

import uuid

import keyring

from .benchmark import MODES, identifier, text
from .errors import AppError
from .utils import normalize_api_base_url

VAULT_SERVICE = "meow-llm-detector"
SAFE_BACKENDS = {"keyring.backends.Windows", "keyring.backends.macOS", "keyring.backends.SecretService"}


class CredentialVault:
    def _backend(self):
        backend = keyring.get_keyring()
        if type(backend).__module__ not in SAFE_BACKENDS:
            raise AppError("secure_vault_unavailable", status=503)
        return backend

    def save(self, reference: str, value: str):
        try:
            self._backend().set_password(VAULT_SERVICE, reference, value)
        except AppError:
            raise
        except Exception as exc:
            raise AppError("secure_vault_write_failed", status=503) from exc

    def load(self, reference: str) -> str:
        try:
            value = self._backend().get_password(VAULT_SERVICE, reference)
        except AppError:
            raise
        except Exception as exc:
            raise AppError("secure_vault_locked", status=503) from exc
        if not value:
            raise AppError("credential_missing", status=409)
        return value

    def delete(self, reference: str):
        try:
            self._backend().delete_password(VAULT_SERVICE, reference)
        except keyring.errors.PasswordDeleteError:
            pass
        except AppError:
            raise
        except Exception as exc:
            raise AppError("secure_vault_delete_failed", status=503) from exc


class EndpointPresets:
    def __init__(self, store, vault=None):
        self.store = store
        self.vault = vault or CredentialVault()

    def list(self):
        return [{key: value for key, value in item.items() if key != "credential_ref"} |
                {"credential_saved": bool(item.get("credential_ref"))} for item in self.store.documents("endpoint")]

    def save(self, value: dict, key: str | None = None) -> dict:
        identity = identifier(value.get("id") or uuid.uuid4().hex)
        mode = value.get("mode")
        if mode not in MODES:
            raise AppError("invalid_mode")
        insecure = value.get("allow_insecure") is True
        base = normalize_api_base_url(value.get("base_url", ""), allow_insecure=insecure)
        item = {"id": identity, "name": text(value.get("name"), "name", limit=256), "mode": mode,
                "base_url": base, "model": text(value.get("model", ""), "model", empty=True, limit=256),
                "allow_insecure": insecure}
        # Check persisted user text, not JSON field names or ignored input fields.
        if key:
            for field in ("name", "base_url", "model"):
                if key in item[field]:
                    raise AppError("credential_in_preset", field=field)
        existing = self.store.document("endpoint", identity)
        reference = existing.get("credential_ref") if existing else None
        if reference and existing["base_url"] != base and not key:
            raise AppError("endpoint_change_requires_key")
        if key:
            new_reference = uuid.uuid4().hex
            self.vault.save(new_reference, key)
            item["credential_ref"] = new_reference
            try:
                self.store.put_document("endpoint", identity, item)
            except Exception:
                self.vault.delete(new_reference)
                raise
            if reference:
                self.vault.delete(reference)
        else:
            item["credential_ref"] = reference
            self.store.put_document("endpoint", identity, item)
        return next(row for row in self.list() if row["id"] == identity)

    def connection(self, identity: str) -> tuple[dict, str]:
        item = self.store.document("endpoint", identifier(identity))
        if not item:
            raise AppError("endpoint_not_found", status=404)
        return item, self.vault.load(item["credential_ref"]) if item.get("credential_ref") else ""

    def delete(self, identity: str):
        item = self.store.document("endpoint", identifier(identity))
        if item and item.get("credential_ref"):
            self.vault.delete(item["credential_ref"])
        self.store.delete_document("endpoint", identity)


def estimate_plan(package: dict, tier: str, retries: int = 2) -> dict:
    counts = package["tiers"][tier]["counts"]
    total = sum(counts.values())
    return {"logical_requests": total, "maximum_http_attempts": total * (retries + 1),
            "cells": counts, "known_price": False, "estimated_usd": None}
