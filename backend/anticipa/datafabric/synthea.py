"""Batch Synthea orchestration (contract 17 §7, Docs/03 §4, §13).

    uv run --directory backend python -m anticipa.datafabric.synthea generate \
        --residents 1000 --batch-size 500 --base-seed 1 --workers 2

Each batch is one Synthea JVM (seed = base_seed + batch_index, one generation thread, FHIR R4 bulk NDJSON) writing
into data/synthea/<dataset_version>/batch-<i>/. On success the NDJSON files are gzipped (deterministic bytes) and the
batch is recorded in the atomic manifest.json. Re-running is a resume: completed batches are skipped, failed or
interrupted batches are deleted and regenerated from scratch.
"""
# ruff: noqa: E501

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import orjson
import psutil

from anticipa.config import ROOT_DIR
from anticipa.core.ids import resident_uuid
from anticipa.datafabric.assign import assignment_config, count_assignments

SCRIPT_VERSION = "datafabric-synthea/1"
DEFAULT_YEARS_OF_HISTORY = (
    11  # >= 10-year screening lookback + 1-year margin (see Docs/benchmarks/synthea_2026-11-06.md)
)
DATASET_VERSION_RE = re.compile(r"^syn-(\d{8})-s(\d+)$")
SYNTHEA_DIR = ROOT_DIR / "data" / "synthea"
CONFIG_DIR = ROOT_DIR / "config" / "synthea"
TOOLS_DIR = ROOT_DIR / "tools"


def source_system(dataset_version: str) -> str:
    """`residents.source_system` / `clinical_records.source_system` for a Synthea dataset."""
    return f"synthea:{dataset_version}"


# --------------------------------------------------------------------------------------------- manifest
def atomic_write_json(path: Path, obj: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as fh:
        fh.write(orjson.dumps(obj, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS))
        fh.flush()
        os.fsync(fh.fileno())
    for _ in range(
        80
    ):  # Windows refuses to replace a file another thread/process has open for reading: retry briefly
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.25)
    os.replace(tmp, path)


def load_manifest(dataset_dir: Path) -> dict | None:
    path = dataset_dir / "manifest.json"
    for _ in range(20):  # the writer's os.replace can momentarily deny a concurrent reader on Windows
        try:
            return orjson.loads(path.read_bytes()) if path.exists() else None
        except PermissionError:
            time.sleep(0.25)
    return orjson.loads(path.read_bytes())


def sorted_batches(manifest: dict) -> list[tuple[int, dict]]:
    return sorted(((int(k), v) for k, v in manifest["batches"].items()), key=lambda kv: kv[0])


# --------------------------------------------------------------------------------------------- toolchain
def toolchain() -> dict:
    """Locate the JRE + jar installed by scripts/bootstrap_synthea.ps1 and read VERSION.json."""
    version_file = CONFIG_DIR / "VERSION.json"
    jar = TOOLS_DIR / "synthea" / "synthea-with-dependencies.jar"
    exe = "java.exe" if platform.system() == "Windows" else "java"
    java = TOOLS_DIR / "jre" / "bin" / exe
    if not (version_file.exists() and jar.exists() and java.exists()):
        raise SystemExit("Synthea toolchain missing: run scripts/bootstrap_synthea.ps1")
    info = json.loads(version_file.read_text(encoding="utf-8-sig"))
    return {"java": str(java), "jar": str(jar), **info}


# --------------------------------------------------------------------------------------------- batch runner
@dataclass(frozen=True)
class BatchSpec:
    index: int
    residents: int
    seed: int
    batch_dir: Path
    cmd: list[str]
    timeout_s: float


Runner = Callable[[BatchSpec], float]  # runs the generator into spec.batch_dir/fhir; returns peak RSS (MB)


class BatchFailed(RuntimeError):
    pass


_ABORT = threading.Event()  # set on Ctrl-C so worker threads stop their JVMs promptly


def run_synthea_process(spec: BatchSpec) -> float:
    """Run one Synthea JVM, sampling its resident set size; raises BatchFailed on non-zero exit / timeout."""
    peak = 0.0
    log_path = spec.batch_dir / "synthea.log"
    with open(log_path, "wb") as log:
        proc = subprocess.Popen(spec.cmd, stdout=log, stderr=subprocess.STDOUT, cwd=spec.batch_dir)  # noqa: S603
        ps = psutil.Process(proc.pid)
        deadline = time.monotonic() + spec.timeout_s
        try:
            while proc.poll() is None:
                try:
                    mi = ps.memory_info()
                    peak = max(peak, getattr(mi, "peak_wset", mi.rss) / 2**20)
                except psutil.Error:
                    pass
                if _ABORT.is_set():
                    raise BatchFailed("aborted")
                if time.monotonic() > deadline:
                    raise BatchFailed(f"timeout after {spec.timeout_s:.0f}s")
                time.sleep(0.25)
        except BaseException:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            raise
    if proc.returncode != 0:
        tail = log_path.read_text(errors="replace").splitlines()[-15:]
        raise BatchFailed(f"synthea exit {proc.returncode}: " + " | ".join(tail))
    return peak


class _HashingWriter:
    """File-like that hashes and counts the bytes passing through to the real file."""

    def __init__(self, fh):
        self.fh, self.sha, self.n = fh, hashlib.sha256(), 0

    def write(self, b):
        self.sha.update(b)
        self.n += len(b)
        return self.fh.write(b)

    def flush(self):
        self.fh.flush()

    def close(self):  # GzipFile closes its fileobj only when it opened it; keep ours open
        pass


def gzip_ndjson(src: Path, dst: Path) -> dict:
    """Stream-gzip src -> dst (mtime=0 so identical content gives identical bytes). Returns sha256/bytes/lines."""
    lines = 0
    with open(dst, "wb") as out:
        hw = _HashingWriter(out)
        with (
            gzip.GzipFile(filename="", mode="wb", fileobj=hw, compresslevel=3, mtime=0) as gz,
            open(src, "rb") as fh,
        ):
            while chunk := fh.read(1 << 20):
                lines += chunk.count(b"\n")
                gz.write(chunk)
    return {"sha256": hw.sha.hexdigest(), "bytes": hw.n, "raw_bytes": src.stat().st_size, "lines": lines}


def _finalise_batch(spec: BatchSpec, dataset_version: str) -> dict:
    """Validate Synthea output, gzip it, drop raw files. Returns the manifest fragment for the batch."""
    fhir = spec.batch_dir / "fhir"
    patient_file = fhir / "Patient.ndjson"
    if not patient_file.exists():
        raise BatchFailed("no Patient.ndjson produced")
    files: dict[str, dict] = {}
    counts: dict[str, int] = {}
    for src in sorted(fhir.glob("*.ndjson")):
        info = gzip_ndjson(src, spec.batch_dir / (src.name + ".gz"))
        files[src.name + ".gz"] = info
        counts[src.name.removesuffix(".ndjson")] = info["lines"]
    if counts.get("Patient") != spec.residents:
        raise BatchFailed(f"expected {spec.residents} Patient resources, found {counts.get('Patient')}")
    with gzip.open(spec.batch_dir / "Patient.ndjson.gz", "rb") as fh:
        ids = [orjson.loads(line)["id"] for line in fh]
    if len(set(ids)) != len(ids):
        raise BatchFailed("duplicate Patient ids inside the batch")
    for sub in spec.batch_dir.iterdir():  # raw fhir/ and any other directories Synthea created
        if sub.is_dir():
            shutil.rmtree(sub)
    return {
        "resource_counts": counts,
        "files": files,
        "assignment": count_assignments(resident_uuid(dataset_version, pid) for pid in ids),
    }


def adopt_finished_batch(batch_dir: Path, residents: int, dataset_version: str) -> dict | None:
    """Manifest fragment for a batch whose output was finalised (gzipped, raw dir removed) but never recorded, e.g. because
    the run died between `_finalise_batch` and the manifest save. None if the directory is not a complete batch."""
    if (batch_dir / "fhir").exists() or not (batch_dir / "Patient.ndjson.gz").exists():
        return None
    files: dict[str, dict] = {}
    counts: dict[str, int] = {}
    try:
        for gz in sorted(batch_dir.glob("*.ndjson.gz")):
            sha, raw, lines = hashlib.sha256(), 0, 0
            with open(gz, "rb") as fh:
                while chunk := fh.read(1 << 20):
                    sha.update(chunk)
            with gzip.open(gz, "rb") as g:
                while chunk := g.read(1 << 22):
                    raw += len(chunk)
                    lines += chunk.count(b"\n")
            files[gz.name] = {
                "sha256": sha.hexdigest(),
                "bytes": gz.stat().st_size,
                "raw_bytes": raw,
                "lines": lines,
            }
            counts[gz.name.removesuffix(".ndjson.gz")] = lines
        if counts.get("Patient") != residents:
            return None
        with gzip.open(batch_dir / "Patient.ndjson.gz", "rb") as fh:
            ids = [orjson.loads(line)["id"] for line in fh]
    except (OSError, EOFError, ValueError):  # truncated gzip, bad json
        return None
    if len(set(ids)) != len(ids):
        return None
    assignment = count_assignments(resident_uuid(dataset_version, pid) for pid in ids)
    return {"resource_counts": counts, "files": files, "assignment": assignment}


def _redact(cmd: list[str]) -> list[str]:
    root = str(ROOT_DIR)
    return [c.replace(root, "<repo>") for c in cmd]


# --------------------------------------------------------------------------------------------- generate
def _totals(manifest: dict) -> dict:
    done = [b for _, b in sorted_batches(manifest) if b["status"] == "completed"]
    counts: dict[str, int] = {}
    district: dict[str, int] = {}
    language: dict[str, int] = {}
    for b in done:
        for k, v in b["resource_counts"].items():
            counts[k] = counts.get(k, 0) + v
        for k, v in b["assignment"]["district"].items():
            district[k] = district.get(k, 0) + v
        for k, v in b["assignment"]["language"].items():
            language[k] = language.get(k, 0) + v
    return {
        "batches_completed": len(done),
        "residents_completed": sum(b["residents"] for b in done),
        "resource_counts": dict(sorted(counts.items())),
        "gz_bytes": sum(f["bytes"] for b in done for f in b["files"].values()),
        "raw_bytes": sum(f["raw_bytes"] for b in done for f in b["files"].values()),
        "generation_seconds": round(sum(b["duration_s"] for b in done), 1),
        "assigned_distribution": {
            "district": dict(sorted(district.items())),
            "language": dict(sorted(language.items())),
        },
    }


def _config_hash(props: Path, *, years: int, ref: str, base_seed: int, batch_size: int, jar_sha: str) -> str:
    blob = orjson.dumps(
        {
            "years": years,
            "ref": ref,
            "seed": base_seed,
            "batch": batch_size,
            "jar": jar_sha,
            "script": SCRIPT_VERSION,
        },
        option=orjson.OPT_SORT_KEYS,
    )
    return hashlib.sha256(props.read_bytes() + blob).hexdigest()


def generate(
    *,
    residents: int,
    batch_size: int,
    base_seed: int,
    workers: int = 2,
    dataset_version: str | None = None,
    reference_date: str | None = None,
    years_of_history: int = DEFAULT_YEARS_OF_HISTORY,
    heap_mb: int = 2048,
    max_attempts: int = 2,
    batch_timeout_s: float = 7200,
    data_dir: Path = SYNTHEA_DIR,
    runner: Runner = run_synthea_process,
    tools: dict | None = None,
    log: Callable[[str], None] = lambda m: print(m, file=sys.stderr, flush=True),
    on_batch: Callable[[int, dict], None] | None = None,
) -> dict:
    """Generate (or resume) a dataset; returns the final manifest. Raises SystemExit(1) if any batch failed.

    `on_batch(index, manifest_entry)` is called from the calling thread each time a batch completes (the pipeline uses
    it to start loading that batch while the others are still generating).
    """
    if residents < 1 or batch_size < 1 or workers < 1:
        raise SystemExit("--residents, --batch-size and --workers must be >= 1")
    _ABORT.clear()
    match = DATASET_VERSION_RE.match(dataset_version) if dataset_version else None
    if dataset_version and not match and not reference_date:
        raise SystemExit("custom --dataset-version needs --reference-date YYYYMMDD")
    ref = reference_date or (match.group(1) if match else datetime.now(UTC).strftime("%Y%m%d"))
    date.fromisoformat(f"{ref[:4]}-{ref[4:6]}-{ref[6:]}")  # validates
    dataset_version = dataset_version or f"syn-{ref}-s{base_seed}"
    tools = tools or toolchain()
    props = CONFIG_DIR / "synthea.properties"
    cfg_hash = _config_hash(
        props,
        years=years_of_history,
        ref=ref,
        base_seed=base_seed,
        batch_size=batch_size,
        jar_sha=tools["jar_sha256"],
    )

    ds_dir = data_dir / dataset_version
    ds_dir.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest(ds_dir)
    now = datetime.now(UTC).isoformat(timespec="seconds")
    if manifest is None:
        manifest = {
            "dataset_version": dataset_version,
            "generator": "synthea",
            "generator_version": tools["synthea_tag"],
            "jar_sha256": tools["jar_sha256"],
            "jre_version": tools["jre_version"],
            "script_version": SCRIPT_VERSION,
            "python_version": platform.python_version(),
            "base_seed": base_seed,
            "batch_size": batch_size,
            "reference_date": ref,
            "years_of_history": years_of_history,
            "config_hash": cfg_hash,
            "properties_file": "config/synthea/synthea.properties",
            "assignment_config": assignment_config(),
            "created_at": now,
            "batches": {},
        }
    elif manifest["config_hash"] != cfg_hash:
        raise SystemExit(
            f"{dataset_version}: existing manifest was made with a different configuration "
            "(seed/batch-size/years/reference-date/jar/properties); use a new --dataset-version"
        )
    manifest["resident_target"] = residents
    n_batches = -(-residents // batch_size)
    plan = [(i, min(batch_size, residents - i * batch_size)) for i in range(n_batches)]

    lock = threading.Lock()

    def save() -> None:
        with lock:
            manifest["updated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
            manifest["totals"] = _totals(manifest)
            all_done = all(manifest["batches"].get(str(i), {}).get("status") == "completed" for i, _ in plan)
            manifest["status"] = "generated" if all_done else "incomplete"
            try:
                atomic_write_json(ds_dir / "manifest.json", manifest)
            except (
                OSError
            ) as exc:  # never lose a run over a manifest write: the next event saves the full state again
                log(f"manifest save failed ({exc!r}); will retry on the next batch event")

    def is_done(i: int, n: int) -> bool:
        b = manifest["batches"].get(str(i))
        if not b or b["status"] != "completed" or b["residents"] != n:
            return False
        return all((ds_dir / f"batch-{i}" / name).exists() for name in b["files"])

    adopted: list[int] = []
    for i, n in plan:
        if is_done(i, n):
            continue
        batch_dir = ds_dir / f"batch-{i}"
        frag = adopt_finished_batch(batch_dir, n, dataset_version) if batch_dir.exists() else None
        if frag is not None:
            stamp = datetime.fromtimestamp((batch_dir / "Patient.ndjson.gz").stat().st_mtime, UTC).isoformat(
                timespec="seconds"
            )
            manifest["batches"][str(i)] = {
                "status": "completed",
                "index": i,
                "residents": n,
                "seed": base_seed + i,
                "attempts": 0,
                "recovered": True,
                "started_at": stamp,
                "finished_at": stamp,
                "duration_s": 0.0,
                "gzip_s": 0.0,
                "peak_rss_mb": 0.0,
                **frag,
            }
            adopted.append(i)
    if adopted:
        save()
        log(
            f"adopted {len(adopted)} finalised batches that were missing from the manifest: {adopted[:10]}..."
        )
        for i in adopted:
            if on_batch is not None:
                on_batch(i, manifest["batches"][str(i)])
    todo = [(i, n) for i, n in plan if not is_done(i, n)]
    log(
        f"{dataset_version}: {len(plan)} batches planned, {len(plan) - len(todo)} already complete, {len(todo)} to run"
    )

    def run_one(i: int, n: int) -> tuple[int, dict]:
        seed = base_seed + i
        batch_dir = ds_dir / f"batch-{i}"
        cmd = [
            tools["java"],
            f"-Xmx{heap_mb}m",
            "-XX:+UseSerialGC",
            "-Duser.timezone=UTC",
            "-Dfile.encoding=UTF-8",
            "-jar",
            tools["jar"],
            "-p",
            str(n),
            "-s",
            str(seed),
            "-cs",
            str(seed),
            "-r",
            ref,
            "-c",
            str(props),
            f"--exporter.baseDirectory={batch_dir}",
            f"--exporter.years_of_history={years_of_history}",
        ]
        spec = BatchSpec(i, n, seed, batch_dir, cmd, batch_timeout_s)
        err = ""
        for attempt in range(1, max_attempts + 1):
            if batch_dir.exists():
                shutil.rmtree(batch_dir)
            batch_dir.mkdir(parents=True)
            entry = {
                "status": "running",
                "index": i,
                "residents": n,
                "seed": seed,
                "attempts": attempt,
                "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "synthea_args": _redact(cmd),
            }
            with lock:
                manifest["batches"][str(i)] = entry
            save()
            t0 = time.monotonic()
            try:
                peak = runner(spec)
                t1 = time.monotonic()
                frag = _finalise_batch(spec, dataset_version)
            except (BatchFailed, OSError, ValueError) as exc:
                err = str(exc)[:2000]
                shutil.rmtree(batch_dir, ignore_errors=True)
                log(f"batch {i} attempt {attempt}/{max_attempts} failed: {err[:200]}")
                continue
            done = {
                **entry,
                "status": "completed",
                "duration_s": round(t1 - t0, 1),
                "gzip_s": round(time.monotonic() - t1, 1),
                "peak_rss_mb": round(peak, 1),
                "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
                **frag,
            }
            with lock:  # recorded here, not by the collector loop, so a collector failure cannot lose finished batches
                manifest["batches"][str(i)] = done
            save()
            return i, done
        return i, {
            **entry,
            "status": "failed",
            "error": err,
            "failed_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }

    failed = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(run_one, i, n) for i, n in todo]
        try:
            for fut in as_completed(futures):
                i, entry = fut.result()
                with lock:
                    manifest["batches"][str(i)] = entry
                save()
                if entry["status"] == "completed":
                    log(
                        f"batch {i} done: {entry['residents']} residents in {entry['duration_s']}s "
                        f"(+{entry['gzip_s']}s gzip), peak RSS {entry['peak_rss_mb']} MB"
                    )
                    if on_batch is not None:
                        on_batch(i, entry)
                else:
                    failed += 1
        except BaseException:
            _ABORT.set()  # running JVMs are killed by their polling loops; partial batches rerun on resume
            for f in futures:
                f.cancel()
            raise
    save()
    if failed:
        log(f"{failed} batch(es) failed; re-run the same command to retry them")
        raise SystemExit(1)
    return manifest


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m anticipa.datafabric.synthea")
    sub = p.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate", help="generate or resume a Synthea dataset")
    g.add_argument("--residents", type=int, required=True)
    g.add_argument("--batch-size", type=int, default=1000)
    g.add_argument("--base-seed", type=int, default=1)
    g.add_argument("--workers", type=int, default=2)
    g.add_argument("--dataset-version", default=None, help="default syn-<reference-date>-s<base-seed>")
    g.add_argument(
        "--reference-date",
        default=None,
        help="YYYYMMDD end of simulated history (default: from version, else today)",
    )
    g.add_argument("--years-of-history", type=int, default=DEFAULT_YEARS_OF_HISTORY)
    g.add_argument("--heap-mb", type=int, default=2048)
    g.add_argument("--max-attempts", type=int, default=2)
    g.add_argument("--batch-timeout-s", type=float, default=7200)
    g.add_argument("--data-dir", type=Path, default=SYNTHEA_DIR)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    a = _parse_args(argv)
    t0 = time.monotonic()
    m = generate(
        residents=a.residents,
        batch_size=a.batch_size,
        base_seed=a.base_seed,
        workers=a.workers,
        dataset_version=a.dataset_version,
        reference_date=a.reference_date,
        years_of_history=a.years_of_history,
        heap_mb=a.heap_mb,
        max_attempts=a.max_attempts,
        batch_timeout_s=a.batch_timeout_s,
        data_dir=a.data_dir,
    )
    t = m["totals"]
    print(
        json.dumps(
            {
                "dataset_version": m["dataset_version"],
                "status": m["status"],
                "residents": t["residents_completed"],
                "gz_mb": round(t["gz_bytes"] / 2**20, 1),
                "wall_s": round(time.monotonic() - t0, 1),
            }
        )
    )


if __name__ == "__main__":
    main()
