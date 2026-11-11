"""Document store for synthetic clinical documents (contract 17 §1, ADR-058/059).

Key layout (identical for every backend): `<dataset_version>/<resident_id>/<document_id>.txt`, stored as the exact
UTF-8 bytes of the text (no newline translation: ground-truth offsets and `documents.content_hash` depend on it).
`documents.storage_key` holds this key. Readers go through `get_document_store().read_text(storage_key)`.

Backends: LocalDocumentStore (default, `data/documents/`) and SupabaseStorageDocumentStore (private bucket
`documents`, Storage REST with the service-role key; server side only, never in a browser). Selection:
`ANTICIPA_DOCUMENT_STORE=local|supabase` (repo-root `.env` or process env), `ANTICIPA_DOCUMENT_DIR`,
`ANTICIPA_DOCUMENT_BUCKET`. The Supabase adapter is exercised with mocked HTTP only (tests); it has not been run
against a live project.
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable
from urllib.parse import quote

import httpx
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from anticipa.config import ENV_FILE, ROOT_DIR, get_settings

DEFAULT_BUCKET = "documents"
DEFAULT_LOCAL_DIR = ROOT_DIR / "data" / "documents"
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)*$")


def content_hash(text: str) -> str:
    """sha256 (hex) of the UTF-8 bytes of the stored text: the value of `documents.content_hash`."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def make_storage_key(dataset_version: str, resident_id: uuid.UUID | str, document_id: uuid.UUID | str) -> str:
    return f"{dataset_version}/{resident_id}/{document_id}.txt"


def validate_key(key: str) -> str:
    """Relative, slash-separated, no `..`, no empty segments, no leading dot: safe on every backend."""
    if not _KEY_RE.fullmatch(key) or ".." in key.split("/"):
        raise ValueError(f"invalid document storage key: {key!r}")
    return key


@runtime_checkable
class DocumentStore(Protocol):
    def write_text(self, key: str, text: str) -> str:
        """Store `text` under `key` (overwrite allowed); return its sha256 content hash."""

    def read_text(self, key: str) -> str:
        """Return the stored text exactly; raise FileNotFoundError if absent."""

    def exists(self, key: str) -> bool: ...


class LocalDocumentStore:
    def __init__(self, root: Path | str = DEFAULT_LOCAL_DIR) -> None:
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        return self.root / validate_key(key)

    def write_text(self, key: str, text: str) -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_bytes(text.encode("utf-8"))
        os.replace(tmp, path)  # atomic: a reader never sees a half-written document
        return content_hash(text)

    def read_text(self, key: str) -> str:
        return self._path(key).read_bytes().decode("utf-8")

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()


class SupabaseStorageDocumentStore:
    """Supabase Storage REST (`/storage/v1`), private bucket, service-role key."""

    def __init__(
        self,
        base_url: str,
        secret_key: str,
        bucket: str = DEFAULT_BUCKET,
        client: httpx.Client | None = None,
    ) -> None:
        self.base = base_url.rstrip("/") + "/storage/v1"
        self.bucket = bucket
        self._headers = {"Authorization": f"Bearer {secret_key}", "apikey": secret_key}
        self._client = client or httpx.Client(timeout=30.0, transport=httpx.HTTPTransport(retries=2))

    def _object(self, key: str, *, authenticated: bool = False) -> str:
        mid = "object/authenticated" if authenticated else "object"
        return f"{self.base}/{mid}/{self.bucket}/{quote(validate_key(key), safe='/')}"

    @staticmethod
    def _not_found(resp: httpx.Response) -> bool:
        # Storage answers a missing object with 404, or 400 + {"statusCode": "404"} depending on version.
        return resp.status_code == 404 or (resp.status_code == 400 and "not_found" in resp.text.lower().replace(" ", "_"))

    def ensure_bucket(self) -> None:
        resp = self._client.post(
            f"{self.base}/bucket",
            headers=self._headers,
            json={"id": self.bucket, "name": self.bucket, "public": False},
        )
        if resp.status_code in (200, 201, 409) or "already exists" in resp.text.lower():
            return
        resp.raise_for_status()

    def write_text(self, key: str, text: str) -> str:
        resp = self._client.post(
            self._object(key),
            headers={**self._headers, "Content-Type": "text/plain; charset=utf-8", "x-upsert": "true"},
            content=text.encode("utf-8"),
        )
        resp.raise_for_status()
        return content_hash(text)

    def read_text(self, key: str) -> str:
        resp = self._client.get(self._object(key, authenticated=True), headers=self._headers)
        if self._not_found(resp):
            raise FileNotFoundError(key)
        resp.raise_for_status()
        return resp.content.decode("utf-8")

    def exists(self, key: str) -> bool:
        resp = self._client.head(self._object(key, authenticated=True), headers=self._headers)
        if resp.status_code == 200:
            return True
        if resp.status_code in (400, 404):
            return False
        resp.raise_for_status()
        return False


class DocStoreSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore", populate_by_name=True)

    backend: Literal["local", "supabase"] = Field("local", validation_alias="ANTICIPA_DOCUMENT_STORE")
    local_dir: Path = Field(DEFAULT_LOCAL_DIR, validation_alias="ANTICIPA_DOCUMENT_DIR")
    bucket: str = Field(DEFAULT_BUCKET, validation_alias="ANTICIPA_DOCUMENT_BUCKET")


@lru_cache
def get_document_store() -> DocumentStore:
    """The configured store (local unless ANTICIPA_DOCUMENT_STORE=supabase). Call `.cache_clear()` after changing env."""
    cfg = DocStoreSettings()
    if cfg.backend == "local":
        return LocalDocumentStore(cfg.local_dir)
    s = get_settings()
    if not s.supabase_url or s.supabase_secret_key is None:
        raise RuntimeError("ANTICIPA_DOCUMENT_STORE=supabase needs SUPABASE_URL and SUPABASE_SECRET_KEY")
    return SupabaseStorageDocumentStore(s.supabase_url, s.supabase_secret_key.get_secret_value(), cfg.bucket)
