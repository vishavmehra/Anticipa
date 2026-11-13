# ruff: noqa: E501
"""Render a planned document (DocSpec) into text, recording the exact code-point span of every planted quote.

Offsets are measured on the final stored text (0-based, half-open, Python `str` indices), accumulated while the text is
built, never recovered by searching afterwards. DC-13 style variants (native digits, ZWNJ / tatweel / NBSP inside
terms, NFD form) are applied per segment *before* offsets are taken, so labels always match the stored bytes.
"""

from __future__ import annotations

import random
import unicodedata
from dataclasses import dataclass, field

from anticipa.datafabric.docgen.context import ResidentContext
from anticipa.datafabric.docgen.phrases import (
    CONDITIONS,
    HI_DID,
    OTHER_PERSONS,
    PROCEDURES,
    QUOTE_CLOSE,
    QUOTE_OPEN,
    RELATIONS,
    SEX,
    T,
    format_date,
    native_digits,
)
from anticipa.datafabric.docgen.planner import SECTION_ORDER, DocSpec, Item, age_on

_ARABIC_CONNECTING = "بتثجحخسشصضطظعغفقكلمنهي"


@dataclass
class RenderedItem:
    item: Item
    quote: tuple[int, int] | None
    line: tuple[int, int] | None


@dataclass
class Rendered:
    text: str
    items: list[RenderedItem] = field(default_factory=list)
    code_mixed: bool = False
    unicode_variant: bool = False


class _Out:
    def __init__(self, nfd: bool) -> None:
        self.parts: list[str] = []
        self.n = 0
        self.nfd = nfd

    def add(self, s: str) -> None:
        if self.nfd:
            s = unicodedata.normalize("NFD", s)
        self.parts.append(s)
        self.n += len(s)

    def line(self, formatted: str, prefix: str = "") -> tuple[tuple[int, int] | None, tuple[int, int]]:
        """Append one line; return (quote span or None, whole-line span). `[[ ]]` marks the quote."""
        line_start = self.n
        self.add(prefix)
        quote = None
        if QUOTE_OPEN in formatted:
            pre, rest = formatted.split(QUOTE_OPEN, 1)
            body, post = rest.split(QUOTE_CLOSE, 1)
            self.add(pre)
            q0 = self.n
            self.add(body)
            quote = (q0, self.n)
            self.add(post)
        else:
            self.add(formatted)
        span = (line_start, self.n)
        self.add("\n")
        return quote, span


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def term_variant(term: str, lang: str) -> str:
    """DC-13 typographic variation inside a clinical term (NBSP / ZWNJ after virama / Arabic tatweel)."""
    if lang == "en":
        return term.replace(" ", " ")
    if lang == "hi":
        i = term.find("्")
        return term if i < 0 else term[: i + 1] + "‌" + term[i + 1 :]
    for i, ch in enumerate(term[:-1]):
        if ch in _ARABIC_CONNECTING and term[i + 1] != " ":
            return term[: i + 1] + "ـ" + term[i + 1 :]
    return term


def resolve(item: Item, lang: str, variant: bool, native: bool) -> tuple[dict, bool]:
    """Language-neutral params -> placeholder strings. Second value: an English clinical term was embedded."""
    p, out, mixed = item.params, {}, False

    def digits(v) -> str:
        return native_digits(str(v), lang) if native else str(v)

    for key in ("rel", "rel2", "rel_well"):
        if p.get(key):
            out[key] = RELATIONS[p[key]][lang]
            out[f"{key}_c"] = _cap(out[key])
    if "who_idx" in p:
        out["who"] = OTHER_PERSONS[lang][p["who_idx"]]
        out["who_c"] = _cap(out["who"])
    if p.get("cond"):
        terms = CONDITIONS[p["cond"]][lang]
        term = terms[min(p.get("term_idx", 0), len(terms) - 1)]
        out["cond"] = term_variant(term, lang) if variant else term
    if "cond_text" in p:
        out["cond"], mixed = p["cond_text"], lang != "en"
    if p.get("proc"):
        entry = PROCEDURES[p["proc"]][lang][0]
        text, gender = entry if lang == "hi" else (entry, None)
        out["proc"], out["proc_c"] = text, _cap(text)
        if gender:
            out["did"], out["did_short"], out["done"] = HI_DID[gender]
    if p.get("age") is not None:
        out["age"] = digits(p["age"])
    if p.get("date") is not None:
        out["date"] = format_date(p["date"], p["precision"], lang, native=native)
    for key in ("sys", "dia"):
        if key in p:
            out[key] = digits(p[key])
    for key in ("med", "name", "unit"):
        if key in p:
            out[key], mixed = str(p[key]), lang != "en"
    if "value" in p:
        out["value"] = digits(p["value"])
    return out, mixed


def render_doc(spec: DocSpec, ctx: ResidentContext, rng: random.Random) -> Rendered:
    lang = ctx.language
    style_on = spec.unicode_variant
    native = style_on and lang != "en"
    out = _Out(nfd=style_on and lang == "ar" and rng.random() < 0.5)
    tpl = T[lang]
    items: list[RenderedItem] = []
    spans_by_tag: dict[str, tuple] = {}
    mixed_any = False

    def plain(key: str, **kw) -> None:
        out.line(rng.choice(tpl[key]).format(**kw))

    pid = f"R-{ctx.id.hex[:8]}"
    out.line(tpl["banner"][0])
    out.add("\n")
    out.line(tpl[f"title.{spec.doc_type}"][0])
    out.line(tpl["hdr_id"][0].format(pid=pid))
    out.line(
        tpl["hdr_date"][0].format(
            date=native_digits(spec.when.isoformat(), lang) if native else spec.when.isoformat()
        )
    )
    age = age_on(ctx.birth_date, spec.when)
    out.line(
        tpl["hdr_agesex"][0].format(
            age=native_digits(str(age), lang) if native else age,
            sex=SEX[lang].get(ctx.sex, SEX[lang]["unknown"]),
        )
    )
    out.add("\n")
    if spec.doc_type == "referral_letter":
        plain("salutation")
        out.add("\n")

    def emit(item: Item) -> None:
        nonlocal mixed_any
        if item.params.get("same_line_as"):  # a second label on a sentence that has already been rendered
            quote, line = spans_by_tag[item.params["same_line_as"]]
            items.append(RenderedItem(item, quote, line))
            return
        templates = tpl[item.kind]
        idx = item.params.get("variant")
        template = templates[idx % len(templates)] if idx is not None else rng.choice(templates)
        params, mixed = resolve(item, lang, style_on, native)
        mixed_any |= mixed
        prefix = tpl[item.prefix][0] if item.prefix else ""
        quote, line = out.line(template.format(**params), prefix)
        if item.tag:
            spans_by_tag[item.tag] = (quote, line)
        if item.label is not None or item.absent or item.kind == "injection":
            items.append(RenderedItem(item, quote, line))

    order = SECTION_ORDER[spec.doc_type]
    rendered_sections = [key for key in order if spec.sections.get(key)]
    for n, key in enumerate(rendered_sections):
        if key.startswith("sec."):
            out.line(tpl[key][0])
        for entry in spec.sections[key]:
            if isinstance(entry, str):
                plain(entry)
            else:
                emit(entry)
        out.add("\n")
        if spec.long and n < len(rendered_sections) - 1:
            for _ in range(rng.randint(5, 8)):
                plain("admin_boiler")
            out.add("\n")
    if spec.long:
        out.line(tpl["sec.admin"][0])
        for _ in range(rng.randint(8, 12)):
            plain("admin_boiler")
        out.add("\n")
    if spec.doc_type == "referral_letter":
        for part in tpl["closing"][0].split("\n"):
            out.line(part)
    return Rendered("".join(out.parts), items, mixed_any and lang != "en", style_on)
