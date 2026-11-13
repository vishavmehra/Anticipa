# ruff: noqa: E501, S608  (identifiers come from constants; every value is a bound parameter)
"""HAPI FHIR demo load (gap-audit B05): the judge-subset residents, their Synthea resources and the Anticipa write-back
resources, as FHIR R4 transaction bundles.

    uv run --directory backend anticipa datafabric hapi-load [--dry-run] [--verify-only] [--workers 2]

* Residents: exactly the cloud judging subset (`judge_subset.select_residents`: same seed, target and cap, so HAPI holds
  the same ~1,100 whole family blocks as the Supabase judge database).
* Synthea resources: only the ones Anticipa stored for those residents (`clinical_records`, i.e. the full storage profile)
  plus each Patient, read from the raw NDJSON.gz of their batch. Anticipa enrichment rows (database only) are rebuilt as
  Procedure / Observation. Conditional references become `Reference.identifier`; references to resources that are not in
  the subset are dropped (counted in the report).
* Write-back / context resources (mapping in `fhir_map`): Location (clinics), RelatedPerson (family edges),
  DocumentReference (generated documents, text inline), Consent, DetectedIssue + Task + Flag + Provenance (care-gap
  cases), Communication (simulated outreach, responses, conversation messages), Appointment (appointments and planned
  allocations). FamilyMemberHistory is not produced: Synthea has none and AI-extracted family facts are not source data.
* Idempotent: every resource is `PUT [type]/[id]` with a stable id (Synthea id, or the source row UUID); a second run
  updates nothing. Resumable: finished batches are recorded in `data/hapi/<dataset>/state.json`. A failed bundle is
  retried entry by entry; entries that still fail are listed in the report, never silently skipped.
* The source database is read in one READ ONLY REPEATABLE READ transaction; nothing is written to it.
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import sys
import threading
import time
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import httpx
import orjson
import psycopg
from psycopg import IsolationLevel
from psycopg.rows import dict_row
from sqlalchemy import select
from sqlalchemy.engine import make_url

from anticipa.config import DB_SCHEMA, ROOT_DIR, get_settings
from anticipa.datafabric import fhir_map as fm
from anticipa.datafabric.docstore import DocStoreSettings, LocalDocumentStore
from anticipa.datafabric.judge_subset import DEFAULT_SOURCE_VERSION, _conninfo, select_residents
from anticipa.datafabric.synthea import SYNTHEA_DIR

log = logging.getLogger("anticipa.hapi_load")
logging.getLogger("httpx").setLevel(logging.WARNING)  # one INFO line per request would drown the progress log

TOOL_VERSION = "hapi-load/1"
HAPI_DIR = ROOT_DIR / "data" / "hapi"
SYNTHEA_TYPES = (  # load order inside a batch (a stable topological sort then fixes the rest)
    "Patient", "Encounter", "Observation", "Condition", "Procedure", "Immunization", "MedicationRequest", "DiagnosticReport",
)  # fmt: skip
FHIR_JSON = {"Content-Type": "application/fhir+json", "Accept": "application/fhir+json"}
_S = DB_SCHEMA


class HapiLoadError(RuntimeError):
    """A refusal or a failed safety check."""


# ------------------------------------------------------------------------------------------------ HAPI client


class Hapi:
    def __init__(self, base_url: str, timeout: float = 300.0) -> None:
        self.base = base_url.rstrip("/").replace(
            "//localhost", "//127.0.0.1"
        )  # see config.prefer_ipv4_loopback
        self.http = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self.http.close()

    def ready(self) -> str:
        """The server's FHIR version; raises HapiLoadError when it is not an R4 server."""
        try:
            resp = self.http.get(f"{self.base}/metadata", params={"_summary": "true"}, headers=FHIR_JSON)
        except httpx.HTTPError as exc:
            raise HapiLoadError(
                f"HAPI FHIR is unreachable at {self.base} ({type(exc).__name__}); start it with `docker compose up -d hapi`"
            ) from exc
        version = str(resp.json().get("fhirVersion", "")) if resp.status_code == 200 else ""
        if not version.startswith("4.0"):
            raise HapiLoadError(
                f"{self.base} answered HTTP {resp.status_code}, fhirVersion {version or 'unknown'}; need R4"
            )
        return version

    def count(self, resource_type: str, **params: str) -> int:
        resp = self.http.get(
            f"{self.base}/{resource_type}",
            params={"_summary": "count", "_total": "accurate", **params},
            headers=FHIR_JSON,
        )
        resp.raise_for_status()
        return int(resp.json().get("total", 0))

    def put_all(self, resources: list[dict[str, Any]]) -> tuple[Counter[str], list[dict[str, str]]]:
        """PUT the resources in one transaction bundle; on failure retry them one by one.
        Returns (counter of created/updated/failed by type, failures)."""
        counts: Counter[str] = Counter()
        if not resources:
            return counts, []
        entries = [
            {"resource": r, "request": {"method": "PUT", "url": f"{r['resourceType']}/{r['id']}"}}
            for r in resources
        ]
        body = orjson.dumps({"resourceType": "Bundle", "type": "transaction", "entry": entries})
        for attempt in range(3):
            try:
                resp = self.http.post(self.base, content=body, headers=FHIR_JSON)
            except httpx.TransportError:
                time.sleep(2**attempt)
                continue
            if resp.status_code < 300:
                for r, entry in zip(resources, resp.json().get("entry", []), strict=False):
                    created = str(entry.get("response", {}).get("status", "")).startswith("201")
                    counts[f"{r['resourceType']}:{'created' if created else 'updated'}"] += 1
                return counts, []
            if resp.status_code < 500:
                break
            time.sleep(2**attempt)
        failures: list[dict[str, str]] = []
        for r in resources:  # a single bad resource must not hide the others
            ok, detail = self._put_one(r)
            if ok is None:
                failures.append({"resource": f"{r['resourceType']}/{r['id']}", "error": detail})
                counts[f"{r['resourceType']}:failed"] += 1
            else:
                counts[f"{r['resourceType']}:{'created' if ok else 'updated'}"] += 1
        return counts, failures

    def _put_one(self, resource: dict[str, Any]) -> tuple[bool | None, str]:
        url = f"{self.base}/{resource['resourceType']}/{resource['id']}"
        detail = ""
        for attempt in range(3):
            try:
                resp = self.http.put(url, content=orjson.dumps(resource), headers=FHIR_JSON)
            except httpx.TransportError as exc:
                detail = type(exc).__name__
                time.sleep(2**attempt)
                continue
            if resp.status_code < 300:
                return resp.status_code == 201, ""
            detail = f"HTTP {resp.status_code}: {_diagnostics(resp)}"
            if resp.status_code < 500:
                break
            time.sleep(2**attempt)
        return None, detail


def _diagnostics(resp: httpx.Response) -> str:
    try:
        issues = resp.json().get("issue", [])
        return "; ".join(
            str(i.get("diagnostics") or i.get("details", {}).get("text") or "") for i in issues[:2]
        )[:300]
    except ValueError:
        return resp.text[:200]


# ------------------------------------------------------------------------------------------------ source index


class Subset:
    """Everything known about the selected residents before a single resource is read."""

    def __init__(self) -> None:
        self.residents: dict[uuid.UUID, dict[str, Any]] = {}
        self.by_patient: dict[str, dict[str, Any]] = {}  # Synthea patient id -> resident
        self.loaded: set[tuple[str, str]] = set()  # (type, id) of every resource that will exist in HAPI
        self.wanted: dict[str, dict[str, set[str]]] = defaultdict(
            lambda: defaultdict(set)
        )  # batch -> type -> ids
        self.enrichment: list[dict[str, Any]] = []
        self.missing_patients: list[str] = []

    @property
    def patient_of(self) -> dict[uuid.UUID, str]:
        return {rid: r["source_patient_id"] for rid, r in self.residents.items()}


def build_subset(src: psycopg.Connection, resident_ids: list[uuid.UUID], dataset_dir: Path) -> Subset:
    sub = Subset()
    with src.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"SELECT id, source_patient_id, district_code, preferred_language FROM {_S}.residents WHERE id = ANY(%s::uuid[])",
            (resident_ids,),
        )
        for r in cur:
            sub.residents[r["id"]] = r
            sub.by_patient[r["source_patient_id"]] = r
            sub.loaded.add(("Patient", r["source_patient_id"]))
    patient_batch: dict[str, str] = {}
    with src.cursor(name="hapi_clinical", row_factory=dict_row) as cur:
        cur.itersize = 20000
        cur.execute(
            f"SELECT resident_id, resource_type, source_resource_id, source_system, source_reference, code_system, code, "
            f"display, effective_start, status, value_json FROM {_S}.clinical_records WHERE resident_id = ANY(%s::uuid[])",
            (resident_ids,),
        )
        for row in cur:
            kind, rid = row["resource_type"], row["source_resource_id"]
            sub.loaded.add((kind, rid))
            if row["source_system"] == "anticipa_enrichment":
                sub.enrichment.append(row)
                continue
            batch = row["source_reference"].split("/")[3]  # synthea://<dataset>/<batch>/<Type>/<id>
            sub.wanted[batch][kind].add(rid)
            patient_batch[sub.residents[row["resident_id"]]["source_patient_id"]] = batch
    unresolved = {p for p in sub.by_patient if p not in patient_batch}
    if unresolved:  # a resident without any stored Synthea record: find the batch from the Patient files
        for batch_dir in sorted(dataset_dir.glob("batch-*")):
            path = batch_dir / "Patient.ndjson.gz"
            if not unresolved or not path.exists():
                continue
            for pid in _scan_ids(path) & unresolved:
                patient_batch[pid] = batch_dir.name
                unresolved.discard(pid)
    sub.missing_patients = sorted(unresolved)
    for pid, batch in patient_batch.items():
        sub.wanted[batch]["Patient"].add(pid)
    for pid in sub.missing_patients:  # no Patient resource will exist, so nothing may reference one
        sub.loaded.discard(("Patient", pid))
    return sub


def _line_id(line: str) -> str:
    start = line.index('"id":"') + 6  # the first "id" is the resource's own (resourceType comes first)
    return line[start : line.index('"', start)]


def _scan_ids(path: Path) -> set[str]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return {_line_id(line) for line in fh}


def read_batch(
    dataset_dir: Path, batch: str, wanted: Mapping[str, set[str]]
) -> dict[str, list[dict[str, Any]]]:
    """The wanted resources of one batch, parsed; a missing id is an error (the database says it exists)."""
    out: dict[str, list[dict[str, Any]]] = {}
    for kind in SYNTHEA_TYPES:
        ids = wanted.get(kind)
        if not ids:
            continue
        found: list[dict[str, Any]] = []
        with gzip.open(dataset_dir / batch / f"{kind}.ndjson.gz", "rt", encoding="utf-8") as fh:
            for line in fh:
                if _line_id(line) in ids:
                    found.append(orjson.loads(line))
        if len(found) != len(ids):
            raise HapiLoadError(
                f"{batch}/{kind}: found {len(found)} of {len(ids)} wanted resources in the raw files"
            )
        out[kind] = found
    return out


def prepare_batch(
    sub: Subset, raw: Mapping[str, list[dict[str, Any]]], dataset_version: str, stats: Counter[str]
) -> list[dict[str, Any]]:
    ordered: list[dict[str, Any]] = []
    for kind in SYNTHEA_TYPES:
        for resource in raw.get(kind, []):
            if kind == "Patient":
                ordered.append(
                    fm.prepare_patient(
                        resource, sub.by_patient[resource["id"]], sub.loaded, stats, dataset_version
                    )
                )
            else:
                ordered.append(fm.prepare_synthea(resource, sub.loaded, stats, dataset_version))
    return fm.dependency_order(ordered)


# ------------------------------------------------------------------------------------------------ state


class State:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.data: dict[str, Any] = {"batches_done": [], "phases_done": [], "enrichment_done": False}
        if path.exists():
            self.data.update(json.loads(path.read_text(encoding="utf-8")))

    def save(self) -> None:
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
            tmp.replace(self.path)

    def mark(self, key: str, value: str) -> None:
        with self.lock:
            self.data[key].append(value)
        self.save()


# ------------------------------------------------------------------------------------------------ load


def post_in_chunks(
    hapi: Hapi, resources: list[dict[str, Any]], size: int
) -> tuple[Counter[str], list[dict[str, str]]]:
    total: Counter[str] = Counter()
    failures: list[dict[str, str]] = []
    for chunk in fm.chunked(resources, size):
        counts, failed = hapi.put_all(chunk)
        total.update(counts)
        failures.extend(failed)
    return total, failures


def load_clinical(
    hapi: Hapi, sub: Subset, dataset_dir: Path, dataset_version: str, state: State, *, workers: int, bundle_size: int,
    limit_batches: int | None, report: dict[str, Any],
) -> None:  # fmt: skip
    batches = sorted(sub.wanted, key=lambda b: int(b.split("-")[1]))
    todo = [b for b in batches if b not in state.data["batches_done"]]
    if limit_batches is not None:
        todo = todo[:limit_batches]
    log.info("clinical: %d of %d batches to load", len(todo), len(batches))
    started = time.perf_counter()
    done = 0

    def one(batch: str) -> tuple[str, Counter[str], list[dict[str, str]], Counter[str]]:
        stats: Counter[str] = Counter()
        raw = read_batch(dataset_dir, batch, sub.wanted[batch])
        resources = prepare_batch(sub, raw, dataset_version, stats)
        counts, failures = post_in_chunks(hapi, resources, bundle_size)
        return batch, counts, failures, stats

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(one, b) for b in todo]
        for fut in as_completed(futures):
            batch, counts, failures, stats = fut.result()
            report["counts"].update(counts)
            report["reference_rewrites"].update(stats)
            report["failures"].extend(failures)
            if not failures:
                state.mark("batches_done", batch)
            done += 1
            rate = sum(v for k, v in report["counts"].items() if not k.endswith(":failed")) / max(
                time.perf_counter() - started, 1e-9
            )
            log.info(
                "clinical %d/%d  %s  %d failures  %.0f resources/s",
                done,
                len(todo),
                batch,
                len(failures),
                rate,
            )


def load_enrichment(
    hapi: Hapi, sub: Subset, dataset_version: str, state: State, *, bundle_size: int, report: dict[str, Any]
) -> None:
    if state.data["enrichment_done"]:
        return
    resources = []
    for row in sub.enrichment:
        patient = sub.residents[row["resident_id"]]["source_patient_id"]
        if ("Patient", patient) not in sub.loaded:
            continue
        resource = fm.enrichment_resource(row, patient, dataset_version)
        if resource is not None:
            resources.append(resource)
    counts, failures = post_in_chunks(hapi, resources, bundle_size)
    report["counts"].update(counts)
    report["failures"].extend(failures)
    if not failures:
        state.data["enrichment_done"] = True
        state.save()


def _rows(src: psycopg.Connection, query: str, *params: Any) -> list[dict[str, Any]]:
    with src.cursor(row_factory=dict_row) as cur:
        cur.execute(query, params)
        return cur.fetchall()


def writeback_phases(
    src: psycopg.Connection, sub: Subset, dataset_version: str, report: dict[str, Any]
) -> list[tuple[str, list[dict[str, Any]]]]:
    """(phase name, resources) in dependency order. Rows of a resident without a HAPI Patient are skipped and counted."""
    ids = list(sub.residents)
    patient_of = {rid: p for rid, p in sub.patient_of.items() if ("Patient", p) in sub.loaded}
    skipped: Counter[str] = Counter()

    def mapped(table: str, rows: Iterable[Mapping[str, Any]], build: Any) -> list[dict[str, Any]]:
        out = []
        for row in rows:
            patient = patient_of.get(row["resident_id"])
            if patient is None:
                skipped[table] += 1
                continue
            out.append(build(row, patient))
        return out

    clinics = _rows(src, f"SELECT * FROM {_S}.clinics")
    services = {
        r["id"]: r for r in _rows(src, f"SELECT id, service_key, service_name FROM {_S}.clinic_services")
    }
    cases = _rows(src, f"SELECT * FROM {_S}.care_gap_cases WHERE resident_id = ANY(%s::uuid[])", ids)
    events = _rows(src, f"SELECT * FROM {_S}.communication_events WHERE resident_id = ANY(%s::uuid[])", ids)
    responses = _rows(src, f"SELECT * FROM {_S}.resident_responses WHERE resident_id = ANY(%s::uuid[])", ids)
    messages = _rows(
        src, f"SELECT * FROM {_S}.conversation_messages WHERE resident_id = ANY(%s::uuid[])", ids
    )
    appointments = _rows(src, f"SELECT * FROM {_S}.appointments WHERE resident_id = ANY(%s::uuid[])", ids)
    allocations = _rows(
        src, f"SELECT * FROM {_S}.planned_allocations WHERE resident_id = ANY(%s::uuid[])", ids
    )
    appointed = {a["planned_allocation_id"] for a in appointments}
    edges = _rows(src, f"SELECT * FROM {_S}.family_edges WHERE resident_id = ANY(%s::uuid[])", ids)
    related_ids = {e["related_resident_id"] for e in edges if e["related_resident_id"]} - set(sub.residents)
    related_patient = dict(patient_of)
    if related_ids:  # an edge to a resident outside the subset still names that person's Synthea id
        for r in _rows(
            src,
            f"SELECT id, source_patient_id FROM {_S}.residents WHERE id = ANY(%s::uuid[])",
            list(related_ids),
        ):
            related_patient[r["id"]] = r["source_patient_id"]
    docs = _rows(src, f"SELECT * FROM {_S}.documents WHERE resident_id = ANY(%s::uuid[])", ids)

    store = LocalDocumentStore(Path(DocStoreSettings().local_dir))

    def doc_text(doc: Mapping[str, Any]) -> str | None:
        try:
            return store.read_text(doc["storage_key"])
        except FileNotFoundError:
            skipped["documents_without_text"] += 1
            return None

    flagged = [c for c in cases if fm.needs_flag(c)]
    phases = [
        ("Location", [fm.location(c, dataset_version) for c in clinics]),
        ("RelatedPerson", mapped("family_edges", edges, lambda e, p: fm.related_person(e, p, related_patient.get(e["related_resident_id"]), dataset_version))),
        ("DocumentReference", mapped("documents", docs, lambda d, p: fm.document_reference(d, p, doc_text(d), dataset_version))),
        ("Consent", _consents(ids, patient_of, dataset_version)),
        ("DetectedIssue", mapped("care_gap_cases", cases, lambda c, p: fm.detected_issue(c, p, dataset_version))),
        ("Task", mapped("care_gap_cases", cases, lambda c, p: fm.task(c, p, dataset_version))),
        ("Flag", mapped("care_gap_cases(flag)", flagged, lambda c, p: fm.flag(c, p, dataset_version))),
        ("Provenance", mapped("care_gap_cases(provenance)", cases, lambda c, p: fm.provenance(c, p, dataset_version))),
        ("Communication(events)", mapped("communication_events", events, lambda e, p: fm.communication_event(e, p, dataset_version))),
        (
            "Communication(responses)",
            mapped("resident_responses", responses, lambda r, p: fm.communication_response(r, p, dataset_version))
            + mapped("conversation_messages", messages, lambda m, p: fm.communication_message(m, p, dataset_version)),
        ),
        (
            "Appointment",
            mapped("appointments", appointments, lambda a, p: fm.appointment(a, p, services.get(a["clinic_service_id"]), dataset_version))
            + mapped(
                "planned_allocations",
                [a for a in allocations if a["id"] not in appointed],
                lambda a, p: fm.appointment_for_allocation(a, p, services.get(a["clinic_service_id"]), dataset_version),
            ),
        ),
    ]  # fmt: skip
    report["writeback_skipped"] = dict(skipped)
    return phases


def _consents(
    ids: list[uuid.UUID], patient_of: Mapping[uuid.UUID, str], dataset_version: str
) -> list[dict[str, Any]]:
    from anticipa.consent import service
    from anticipa.consent.models import ConsentRecord
    from anticipa.db import get_sessionmaker

    with get_sessionmaker()() as session:
        rows = (
            session.execute(select(ConsentRecord).where(ConsentRecord.resident_id.in_(ids))).scalars().all()
        )
        by_resident: dict[uuid.UUID, list[Any]] = defaultdict(list)
        for r in rows:
            by_resident[r.resident_id].append(r)
        out = []
        for rid, records in by_resident.items():
            if rid not in patient_of:
                continue
            in_force = set(service.evaluate(records).in_force)
            out.extend(
                fm.consent(service.to_fhir(r, in_force=r.id in in_force), patient_of[rid], dataset_version)
                for r in records
            )
        return out


def load_writeback(
    hapi: Hapi, phases: list[tuple[str, list[dict[str, Any]]]], state: State, *, workers: int, bundle_size: int, report: dict[str, Any]
) -> None:  # fmt: skip
    for name, resources in phases:
        if name in state.data["phases_done"]:
            continue
        chunks = list(fm.chunked(resources, bundle_size))
        failures: list[dict[str, str]] = []
        started = time.perf_counter()
        with ThreadPoolExecutor(
            max_workers=workers
        ) as pool:  # one resource type per phase: no dependency inside it
            for counts, failed in pool.map(hapi.put_all, chunks):
                report["counts"].update(counts)
                failures.extend(failed)
        report["failures"].extend(failures)
        log.info(
            "write-back %-26s %6d resources  %d failures  %.1fs",
            name,
            len(resources),
            len(failures),
            time.perf_counter() - started,
        )
        if not failures:
            state.mark("phases_done", name)


# ------------------------------------------------------------------------------------------------ verify

VALIDATE_TYPES = (
    "Patient", "Encounter", "Condition", "Observation", "Procedure", "Immunization", "MedicationRequest", "DiagnosticReport",
    "RelatedPerson", "DocumentReference", "Consent", "DetectedIssue", "Task", "Flag", "Provenance", "Communication",
    "Appointment", "Location",
)  # fmt: skip


def verify(
    hapi: Hapi, resident: Mapping[str, Any] | None, expected: Mapping[str, int], samples: int
) -> dict[str, Any]:
    out: dict[str, Any] = {"counts": {}, "validate": {}, "round_trip": {}}
    for kind in VALIDATE_TYPES:
        actual = hapi.count(kind)
        out["counts"][kind] = {
            "hapi": actual,
            **({"expected": expected[kind], "match": actual == expected[kind]} if kind in expected else {}),
        }
    for kind in VALIDATE_TYPES:
        resp = hapi.http.get(
            f"{hapi.base}/{kind}", params={"_count": str(samples), "_sort": "_id"}, headers=FHIR_JSON
        )
        resp.raise_for_status()
        issues: Counter[str] = Counter()
        messages: list[str] = []
        entries = resp.json().get("entry", [])
        for entry in entries:
            result = hapi.http.post(
                f"{hapi.base}/{kind}/$validate", content=orjson.dumps(entry["resource"]), headers=FHIR_JSON
            )
            for issue in result.json().get("issue", []):
                issues[issue.get("severity", "?")] += 1
                if issue.get("severity") in ("error", "fatal") and len(messages) < 3:
                    messages.append(
                        str(issue.get("diagnostics") or issue.get("details", {}).get("text"))[:240]
                    )
        out["validate"][kind] = {"validated": len(entries), "issues": dict(issues), "first_errors": messages}
    if resident is not None:
        pid = resident["source_patient_id"]
        rt: dict[str, Any] = {"patient_id": pid}
        got = hapi.http.get(f"{hapi.base}/Patient/{pid}", headers=FHIR_JSON)
        rt["get_patient"] = got.status_code
        found = hapi.http.get(
            f"{hapi.base}/Patient",
            params={"identifier": f"{fm.RESIDENT_ID}|{resident['id']}", "_summary": "count"},
            headers=FHIR_JSON,
        )
        rt["search_by_resident_id_total"] = (
            found.json().get("total") if found.status_code == 200 else found.status_code
        )
        for kind, param in (
            ("Encounter", "patient"),
            ("Condition", "patient"),
            ("Task", "patient"),
            ("DetectedIssue", "patient"),
        ):
            rt[f"{kind}_for_patient"] = hapi.count(kind, **{param: pid})
        out["round_trip"] = rt
    return out


def health_status() -> dict[str, Any]:
    """The same check as GET /api/v1/integrations (key `hapi`), run in-process."""
    from anticipa.api.integrations import _hapi

    configured, status, detail = _hapi(None, get_settings())  # type: ignore[arg-type]  (the check ignores the request)
    return {"configured": configured, "status": status, "detail": detail}


# ------------------------------------------------------------------------------------------------ CLI


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="anticipa datafabric hapi-load", description="Load the judge-subset residents into HAPI FHIR."
    )
    p.add_argument("--source-version", default=DEFAULT_SOURCE_VERSION)
    p.add_argument("--seed", type=int, default=42, help="judge-subset selection seed")
    p.add_argument("--target", type=int, default=1100, help="judge-subset target residents")
    p.add_argument("--max-residents", type=int, default=1200)
    p.add_argument("--fhir-base-url", default=None, help="default: HAPI_FHIR_BASE_URL")
    p.add_argument("--bundle-size", type=int, default=100, help="resources per transaction bundle")
    p.add_argument("--workers", type=int, default=2, help="parallel batches (keep low on a laptop)")
    p.add_argument(
        "--limit-batches",
        type=int,
        default=None,
        help="load only the first N unfinished Synthea batches (testing)",
    )
    p.add_argument("--dry-run", action="store_true", help="select and count only; write nothing")
    p.add_argument("--verify-only", action="store_true", help="skip loading; count, $validate and round-trip")
    p.add_argument("--skip-clinical", action="store_true")
    p.add_argument("--skip-writeback", action="store_true")
    p.add_argument(
        "--reset-state", action="store_true", help="forget finished batches (the load is idempotent anyway)"
    )
    p.add_argument("--validate-samples", type=int, default=3, help="resources per type sent to $validate")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S", stream=sys.stderr
    )
    dataset_dir = SYNTHEA_DIR / args.source_version
    work_dir = HAPI_DIR / args.source_version
    if args.reset_state:
        (work_dir / "state.json").unlink(missing_ok=True)
    state = State(work_dir / "state.json")
    hapi = Hapi(args.fhir_base_url or get_settings().hapi_fhir_base_url)
    report: dict[str, Any] = {
        "tool": TOOL_VERSION, "dataset": args.source_version, "counts": Counter(), "reference_rewrites": Counter(), "failures": [],
    }  # fmt: skip
    try:
        report["fhir_version"] = hapi.ready()
        src_url = make_url(get_settings().sqlalchemy_url())
        with psycopg.connect(_conninfo(src_url)) as src:
            src.isolation_level = IsolationLevel.REPEATABLE_READ
            src.read_only = True
            t0 = time.perf_counter()
            ids, selection = select_residents(
                src, args.source_version, seed=args.seed, target=args.target, max_residents=args.max_residents
            )
            if not ids:
                raise HapiLoadError("selection is empty")
            sub = build_subset(src, ids, dataset_dir)
            report.update(
                residents=len(ids), selection=selection, batches=len(sub.wanted),
                enrichment_rows=len(sub.enrichment), patients_without_source=sub.missing_patients, select_seconds=round(time.perf_counter() - t0, 1),
            )  # fmt: skip
            log.info(
                "selected %d residents, %d clinical resources in %d batches (+%d enrichment)",
                len(ids),
                len(sub.loaded),
                len(sub.wanted),
                len(sub.enrichment),
            )
            phases = (
                writeback_phases(src, sub, args.source_version, report) if not args.skip_writeback else []
            )
            planned: Counter[str] = Counter(kind for kind, _ in sub.loaded)
            for _, resources in phases:
                planned.update(r["resourceType"] for r in resources)
            report["resources_planned"] = dict(sorted(planned.items()))
            report["writeback_phases"] = {name: len(resources) for name, resources in phases}
            if args.dry_run:
                print(json.dumps(report, indent=2, default=str))
                return 0
            started = time.perf_counter()
            if not args.verify_only:
                if not args.skip_clinical:
                    load_clinical(
                        hapi, sub, dataset_dir, args.source_version, state, workers=args.workers, bundle_size=args.bundle_size,
                        limit_batches=args.limit_batches, report=report,
                    )  # fmt: skip
                    if args.limit_batches is None:
                        load_enrichment(
                            hapi, sub, args.source_version, state, bundle_size=args.bundle_size, report=report
                        )
                if phases:
                    load_writeback(
                        hapi, phases, state, workers=args.workers, bundle_size=args.bundle_size, report=report
                    )
            report["load_seconds"] = round(time.perf_counter() - started, 1)
            complete = not (args.limit_batches or args.skip_clinical or args.skip_writeback)
            resident = next(iter(sub.residents.values()))
            report["verify"] = verify(hapi, resident, planned if complete else {}, args.validate_samples)
            report["health"] = health_status()
    except HapiLoadError as exc:
        print(f"hapi-load: {exc}", file=sys.stderr)
        return 2
    finally:
        hapi.close()
    report["counts"] = dict(sorted(report["counts"].items()))
    report["reference_rewrites"] = dict(report["reference_rewrites"])
    if not args.verify_only:
        work_dir.mkdir(parents=True, exist_ok=True)
        (work_dir / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report, indent=2, default=str))
    return 1 if report["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
