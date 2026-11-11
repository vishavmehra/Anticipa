"""Optional file cache for generate() responses so evaluation reruns replay without re-billing.

Off by default (ANTICIPA_LLM_CACHE=1 enables it). Entries hold only the raw model output and accounting
metadata, never prompts or documents. A hit is re-validated by the router like a fresh response, and the
key covers the route (candidates/effort/temperature), prompt version, messages and schema, so any change
to those misses. Synthetic data only; data/ is gitignored."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


class ResponseCache:
    def __init__(self, root: Path) -> None:
        self.root = root

    @staticmethod
    def key(
        task: str,
        route: dict[str, Any],
        prompt_version: str,
        system: str,
        messages: list[dict[str, str]],
        schema: dict[str, Any] | None,
    ) -> str:
        blob = json.dumps(
            [task, route, prompt_version, system, messages, schema],
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def _path(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.json"

    def get(self, key: str) -> dict[str, Any] | None:
        try:
            return json.loads(self._path(key).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None  # missing or corrupt entry == miss

    def put(self, key: str, entry: dict[str, Any]) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(entry, fh, ensure_ascii=False, default=str)
            os.replace(tmp, path)  # atomic: concurrent workers never read a half-written entry
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
