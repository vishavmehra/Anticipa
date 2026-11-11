"""Prompt registry: versioned prompt files `anticipa/<module>/prompts/<key>@<version>.txt`.

A prompt change means a new file (new version); the content hash is recorded with each use so results
can be tied to the exact text that produced them (06 §13)."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

_REF = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)*@[0-9]+$")
_PACKAGE_ROOT = Path(__file__).resolve().parents[1]  # backend/anticipa


@dataclass(frozen=True)
class Prompt:
    ref: str  # "evidence.extract@1"
    text: str
    sha256: str


class PromptNotFound(KeyError):
    pass


class PromptRegistry:
    def __init__(self, roots: list[Path] | None = None) -> None:
        self._roots = roots
        self._cache: dict[str, Prompt] = {}

    def _search_roots(self) -> list[Path]:
        return self._roots if self._roots is not None else sorted(_PACKAGE_ROOT.glob("*/prompts"))

    def get(self, ref: str) -> Prompt:
        if not _REF.match(ref):  # also blocks path traversal
            raise PromptNotFound(f"invalid prompt reference {ref!r}; expected 'module.name@version'")
        if ref not in self._cache:
            for root in self._search_roots():
                path = root / f"{ref}.txt"
                if path.is_file():
                    text = path.read_text(encoding="utf-8").replace("\r\n", "\n").strip() + "\n"
                    self._cache[ref] = Prompt(ref, text, hashlib.sha256(text.encode("utf-8")).hexdigest())
                    break
            else:
                raise PromptNotFound(f"prompt {ref!r} not found")
        return self._cache[ref]


_default = PromptRegistry()


def get_prompt(ref: str) -> Prompt:
    return _default.get(ref)
