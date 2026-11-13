"""Seeded, deterministic family linker (Docs/03 §5, Docs/09 §6.4.3).

Synthea has no family model, so families are *constructed* among the loaded residents: per district, in id order, with
`random.Random(f"anticipa-family:{dataset_version}:{base_seed}:{district}")` — the same dataset, seed and config always
give the same edges. A family unit is an anchor adult plus (optionally) a spouse, the anchor's mother/father, the anchor's
siblings and the anchor's children, with age-consistent birth dates. Every resident belongs to at most one unit.

Edge semantics: a `family_edges` row `(resident_id=A, related_resident_id=B, relationship_type=T)` reads "B is A's T"
(`relationship_direction = 'related_to_resident'`); both directions are stored as separate rows. Types follow the evidence
vocabulary (mother, father, sister, brother, daughter, son, spouse, maternal/paternal grandmother/grandfather,
maternal/paternal aunt/uncle) plus the reciprocal-only types granddaughter, grandson, niece, nephew. Cousins, half-siblings
and in-laws are not generated here (they appear as document-only relatives from the document generator).

Ground truth (`data/ground_truth/<V>/families/family_graph.jsonl`, schema `gt.family_edge.v1`) is written next to the
DB rows with the same ids (`edge_id` = `family_edges.id`), plus `family_id` and `in_family_edges`. It never enters the DB
free text or any prompt; only `anticipa.evaluation` may read it.

    uv run --directory backend python -m anticipa.datafabric.family link --dataset-version V [--database-url ...]
"""
# ruff: noqa: E501, S608, S311

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import random
import time
import uuid
from bisect import bisect_left, bisect_right
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

import orjson
from sqlalchemy import text
from sqlalchemy.orm import Session

from anticipa.config import DB_SCHEMA, ROOT_DIR
from anticipa.core.ids import resident_uuid, uuid5_of
from anticipa.datafabric.loader import engine_for
from anticipa.datafabric.synthea import SYNTHEA_DIR

S = DB_SCHEMA
GT_DIR = ROOT_DIR / "data" / "ground_truth"
SOURCE_TYPE = "deterministic_linker"
DIRECTION = "related_to_resident"
LINKER_VERSION = "family-linker/1"
YEAR = 365.2425  # days


@dataclass(frozen=True)
class LinkerConfig:
    family_fraction: float = 0.60  # stop forming units once this share of residents is in a unit
    p_spouse: float = 0.70
    child_counts: tuple[float, ...] = (0.20, 0.28, 0.32, 0.14, 0.06)  # P(0..4 children)
    p_parents: float = 0.55  # anchor has resident parents
    p_mother: float = 0.90  # given parents
    p_father: float = 0.75  # given parents
    p_siblings: float = 0.45  # anchor has resident siblings (1 or 2)
    p_two_siblings: float = 0.35
    p_parents_in_household: float = 0.25
    min_parent_age: float = 18.0
    max_mother_age: float = 42.0
    max_father_age: float = 55.0
    anchor_age: tuple[int, int] = (22, 70)


@dataclass
class Unit:
    anchor: int
    spouse: int | None = None
    mother: int | None = None
    father: int | None = None
    siblings: list[int] = field(default_factory=list)
    children: list[int] = field(default_factory=list)

    def members(self) -> list[int]:
        return [
            m
            for m in (self.anchor, self.spouse, self.mother, self.father, *self.siblings, *self.children)
            if m is not None
        ]


class Pool:
    """Unused residents by sex, sorted by birth ordinal; picks a random unused one in a birth window."""

    def __init__(self, born: Sequence[int], sex: Sequence[str]):
        self.used = bytearray(len(born))
        self._keys: dict[str, list[int]] = {}
        self._idx: dict[str, list[int]] = {}
        for s in ("female", "male"):
            pairs = sorted((b, i) for i, (b, x) in enumerate(zip(born, sex, strict=True)) if x == s)
            self._keys[s] = [b for b, _ in pairs]
            self._idx[s] = [i for _, i in pairs]

    def take(self, i: int) -> None:
        self.used[i] = 1

    def release(self, i: int) -> None:
        self.used[i] = 0

    def pick(
        self, rng: random.Random, lo: float, hi: float, sexes: tuple[str, ...] = ("female", "male")
    ) -> int | None:
        spans = []
        for s in sexes:
            a, b = bisect_left(self._keys[s], lo), bisect_right(self._keys[s], hi)
            if b > a:
                spans.append((s, a, b - a))
        total = sum(n for _, _, n in spans)
        if not total:
            return None

        def at(r: int) -> int:
            for s, a, n in spans:
                if r < n:
                    return self._idx[s][a + r]
                r -= n
            raise AssertionError

        for _ in range(16):  # random probes first (cheap while the pool is mostly unused)
            i = at(rng.randrange(total))
            if not self.used[i]:
                self.take(i)
                return i
        start = rng.randrange(total)  # then a deterministic scan of the whole window
        for k in range(total):
            i = at((start + k) % total)
            if not self.used[i]:
                self.take(i)
                return i
        return None


def _weighted(rng: random.Random, weights: Sequence[float]) -> int:
    r, acc = rng.random() * sum(weights), 0.0
    for k, w in enumerate(weights):
        acc += w
        if r < acc:
            return k
    return len(weights) - 1


def build_units(
    born: Sequence[int], sex: Sequence[str], ref_ord: int, rng: random.Random, cfg: LinkerConfig
) -> list[Unit]:
    """Form family units over residents `0..n-1` (birth ordinals + 'female'/'male'). Pure and deterministic given `rng`."""
    n = len(born)
    pool = Pool(born, sex)
    eligible = [
        i
        for i in range(n)
        if sex[i] in ("female", "male")
        and cfg.anchor_age[0] * YEAR <= ref_ord - born[i] <= cfg.anchor_age[1] * YEAR
    ]
    rng.shuffle(eligible)
    target = int(cfg.family_fraction * n)
    in_units = 0
    units: list[Unit] = []
    y = YEAR
    for a in eligible:
        if in_units >= target:
            break
        if pool.used[a]:
            continue
        pool.take(a)
        u = Unit(anchor=a)
        # spouse (opposite sex, within 8 years, adult)
        if rng.random() < cfg.p_spouse:
            opposite = ("male",) if sex[a] == "female" else ("female",)
            s = pool.pick(rng, max(born[a] - 8 * y, 0), min(born[a] + 8 * y, ref_ord - 18 * y), opposite)
            u.spouse = s
        # children of the anchor (and spouse)
        mother = a if sex[a] == "female" else u.spouse
        father = a if sex[a] == "male" else u.spouse
        lo, hi = float("-inf"), float(ref_ord)
        for parent, max_age in ((mother, cfg.max_mother_age), (father, cfg.max_father_age)):
            if parent is not None:
                lo, hi = max(lo, born[parent] + cfg.min_parent_age * y), min(hi, born[parent] + max_age * y)
        for _ in range(_weighted(rng, cfg.child_counts)):
            if lo > hi:
                break
            c = pool.pick(rng, lo, hi)
            if c is not None:
                u.children.append(c)
        # the anchor's own parents
        if rng.random() < cfg.p_parents:
            if rng.random() < cfg.p_mother:
                u.mother = pool.pick(
                    rng, born[a] - cfg.max_mother_age * y, born[a] - cfg.min_parent_age * y, ("female",)
                )
            if rng.random() < cfg.p_father:
                f_lo, f_hi = born[a] - cfg.max_father_age * y, born[a] - cfg.min_parent_age * y
                if u.mother is not None:
                    f_lo, f_hi = max(f_lo, born[u.mother] - 10 * y), min(f_hi, born[u.mother] + 10 * y)
                u.father = pool.pick(rng, f_lo, f_hi, ("male",)) if f_lo <= f_hi else None
        # the anchor's siblings (must also fit the parents' age windows when parents are known)
        if rng.random() < cfg.p_siblings:
            s_lo, s_hi = born[a] - 12 * y, born[a] + 12 * y
            for parent, max_age in ((u.mother, cfg.max_mother_age), (u.father, cfg.max_father_age)):
                if parent is not None:
                    s_lo, s_hi = (
                        max(s_lo, born[parent] + cfg.min_parent_age * y),
                        min(s_hi, born[parent] + max_age * y),
                    )
            for _ in range(2 if rng.random() < cfg.p_two_siblings else 1):
                if s_lo > s_hi:
                    break
                sib = pool.pick(rng, max(s_lo, 0), min(s_hi, ref_ord))
                if sib is not None:
                    u.siblings.append(sib)
        if len(u.members()) < 2:
            pool.release(a)
            continue
        units.append(u)
        in_units += len(u.members())
    return units


Edge = tuple[int, int, str, int | None, str | None]  # (a, b, type of b relative to a, degree, side)


def _fm(sx: str, female: str, male: str) -> str:
    return female if sx == "female" else male


def unit_edges(u: Unit, sex: Sequence[str]) -> list[Edge]:
    """All directed edges of a unit: for each related pair both rows ("b is a's <type>" and the reverse)."""
    out: list[Edge] = []

    def parent_child(p: int, c: int) -> None:
        out.append((c, p, _fm(sex[p], "mother", "father"), 1, _fm(sex[p], "maternal", "paternal")))
        out.append((p, c, _fm(sex[c], "daughter", "son"), 1, None))

    def siblings(group: list[int]) -> None:
        for i, x in enumerate(group):
            for y in group[i + 1 :]:
                out.append((x, y, _fm(sex[y], "sister", "brother"), 1, None))
                out.append((y, x, _fm(sex[x], "sister", "brother"), 1, None))

    a = u.anchor
    if u.spouse is not None:
        out += [(a, u.spouse, "spouse", None, None), (u.spouse, a, "spouse", None, None)]
    if u.mother is not None and u.father is not None:
        out += [(u.mother, u.father, "spouse", None, None), (u.father, u.mother, "spouse", None, None)]
    for p in (a, u.spouse):
        if p is not None:
            for c in u.children:
                parent_child(p, c)
    for gp in (u.mother, u.father):
        if gp is not None:
            for child_of_gp in [a, *u.siblings]:
                parent_child(gp, child_of_gp)
    siblings([a, *u.siblings])
    siblings(u.children)
    side = _fm(sex[a], "maternal", "paternal")  # the anchor is the mother/father of the children
    for gp in (u.mother, u.father):
        if gp is not None:
            for c in u.children:
                out.append((c, gp, f"{side}_{_fm(sex[gp], 'grandmother', 'grandfather')}", 2, side))
                out.append((gp, c, _fm(sex[c], "granddaughter", "grandson"), 2, None))
    for sib in u.siblings:
        for c in u.children:
            out.append((c, sib, f"{side}_{_fm(sex[sib], 'aunt', 'uncle')}", 2, side))
            out.append((sib, c, _fm(sex[c], "niece", "nephew"), 2, None))
    return out


# --------------------------------------------------------------------------------------------- database + ground truth
# Families are formed inside fixed BLOCKS of consecutive batches (default 20 batches = 10,000 residents at batch size
# 500). A block is linked once, when all its batches are loaded, so growing a dataset (100k -> 1M) never changes the edges
# or ground truth of residents that were already linked.
BLOCK_BATCHES = 20


@dataclass
class LinkResult:
    dataset_version: str
    block: int = 0
    districts: int = 0
    residents: int = 0
    residents_in_families: int = 0
    units: int = 0
    edges: int = 0
    edges_by_type: dict[str, int] = field(default_factory=dict)
    ground_truth_file: str = ""
    ground_truth_sha256: str = ""
    seconds: float = 0.0


def block_batch_range(block: int, block_batches: int, planned: int) -> range:
    return range(block * block_batches, min((block + 1) * block_batches, planned))


def block_resident_ids(dataset_version: str, data_dir: Path, batches: Sequence[int]) -> list[uuid.UUID]:
    """Resident ids of the given batches, recomputed from their Patient files (the UUIDv5 rule of contract 17 §2)."""
    ids: list[uuid.UUID] = []
    for i in batches:
        with gzip.open(Path(data_dir) / dataset_version / f"batch-{i}" / "Patient.ndjson.gz", "rb") as fh:
            ids.extend(resident_uuid(dataset_version, orjson.loads(line)["id"]) for line in fh)
    return ids


def _block_residents(session: Session, ids: Sequence[uuid.UUID]) -> dict[str, tuple[list, list, list]]:
    """{district: (ids, birth ordinals, sex)} for the residents in `ids`, each list ordered by resident id."""
    out: dict[str, tuple[list, list, list]] = {}
    for rid, birth, sex, district in session.execute(
        text(
            f"SELECT id, birth_date, administrative_sex, district_code FROM {S}.residents "
            "WHERE id = ANY(:ids) AND district_code IS NOT NULL AND birth_date IS NOT NULL ORDER BY id"
        ),
        {"ids": list(ids)},
    ):
        d = out.setdefault(district, ([], [], []))
        d[0].append(rid)
        d[1].append(birth.toordinal())
        d[2].append(sex or "unknown")
    return dict(sorted(out.items()))


def _iter_gt(
    dataset_version: str,
    ids: Sequence[uuid.UUID],
    units: Sequence[Unit],
    sex: Sequence[str],
    born: Sequence[int],
    ref_ord: int,
    cfg: LinkerConfig,
) -> Iterator[tuple[dict, tuple]]:
    """Yield (ground-truth record, DB row) per directed edge, in a deterministic order."""
    for u in units:
        family_id = uuid5_of(f"{dataset_version}:family:{ids[u.anchor]}")
        hh = {u.anchor, *([u.spouse] if u.spouse is not None else [])}
        hh |= {c for c in u.children if (ref_ord - born[c]) < 25 * YEAR}
        parents = [p for p in (u.mother, u.father) if p is not None]
        draw = uuid5_of(f"{dataset_version}:household-parents:{family_id}").int % 10_000
        if parents and draw < cfg.p_parents_in_household * 10_000:
            hh |= set(parents)
        household_id = uuid5_of(f"{dataset_version}:household:{family_id}")
        for a, b, rtype, degree, side in sorted(unit_edges(u, sex), key=lambda e: (e[0], e[1], e[2])):
            edge_id = uuid5_of(f"{dataset_version}:gt:family_edge:{ids[a]}:{ids[b]}:{rtype}")
            gt = {
                "schema_version": "gt.family_edge.v1",
                "edge_id": str(edge_id),
                "dataset_version": dataset_version,
                "resident_id": str(ids[a]),
                "related_resident_id": str(ids[b]),
                "related_person_key": None,
                "relationship_type": rtype,
                "degree": degree,
                "side": side,
                "household_id": str(household_id) if a in hh and b in hh else None,
                "provenance": SOURCE_TYPE,
                "decoy": False,
                "family_id": str(family_id),
                "in_family_edges": True,
            }
            row = (edge_id, ids[a], ids[b], rtype, DIRECTION, f"family:{family_id}", SOURCE_TYPE, "verified")
            yield gt, row


def _dataset_row(session: Session, dataset_version: str):
    row = session.execute(
        text(f"SELECT id, base_seed, manifest FROM {S}.datasets WHERE dataset_version = :v FOR UPDATE"),
        {"v": dataset_version},
    ).first()
    if row is None:
        raise ValueError(f"unknown dataset {dataset_version!r} (ingest it first)")
    return row


def _merge_ground_truth(gt_dir: Path, block: int) -> None:
    """Keep family_graph.jsonl = the block shards concatenated in block order. A block newer than everything already merged
    is appended (cheap, the normal case while a run progresses); anything else rebuilds the file from the shards."""
    merged, state_path = gt_dir / "family_graph.jsonl", gt_dir / "family_graph.merged.json"
    state = (
        orjson.loads(state_path.read_bytes()) if state_path.exists() and merged.exists() else {"blocks": []}
    )
    shard = gt_dir / f"family_graph.block-{block:04d}.jsonl"
    if state["blocks"] and block > max(state["blocks"]):
        with open(merged, "ab") as out, open(shard, "rb") as fh:
            while chunk := fh.read(1 << 22):
                out.write(chunk)
        blocks = [*state["blocks"], block]
    else:
        tmp = gt_dir / "family_graph.jsonl.tmp"
        shards = sorted(gt_dir.glob("family_graph.block-*.jsonl"))
        with open(tmp, "wb") as out:
            for sh in shards:
                with open(sh, "rb") as fh:
                    while chunk := fh.read(1 << 22):
                        out.write(chunk)
        os.replace(tmp, merged)
        blocks = [int(sh.name.split("-")[1].split(".")[0]) for sh in shards]
    state_path.write_bytes(orjson.dumps({"blocks": blocks}))


def link_block(
    session: Session,
    dataset_version: str,
    block: int,
    *,
    block_batches: int = BLOCK_BATCHES,
    data_dir: Path = SYNTHEA_DIR,
    cfg: LinkerConfig | None = None,
    gt_root: Path = GT_DIR,
    replace: bool = True,
    allow_partial: bool = False,
    log=lambda m: None,
) -> LinkResult:
    """Build the family graph of one block of batches; write DB rows + a ground-truth shard. Idempotent."""
    cfg = cfg or LinkerConfig()
    t0 = time.perf_counter()
    dataset_id, base_seed, manifest = _dataset_row(session, dataset_version)
    gen = manifest.get("generation") or {}
    planned = int(gen.get("batches_planned") or 0)
    batches = block_batch_range(block, block_batches, planned)
    if not batches:
        raise ValueError(f"block {block} is outside the {planned} planned batches")
    if len(batches) < block_batches and not allow_partial:
        raise ValueError(
            f"block {block} is partial ({len(batches)}/{block_batches} planned batches); not linked"
        )
    loaded = (manifest.get("ingest") or {}).get("batches") or {}
    missing = [i for i in batches if str(i) not in loaded]
    if missing:
        raise ValueError(f"block {block} is not fully loaded (missing batches {missing[:5]}...)")
    ref = gen.get("reference_date")
    ref_ord = date(int(ref[:4]), int(ref[4:6]), int(ref[6:])).toordinal() if ref else date.today().toordinal()
    ids = block_resident_ids(dataset_version, data_dir, list(batches))
    by_district = _block_residents(session, ids)
    result = LinkResult(dataset_version, block=block, districts=len(by_district))
    gt_dir = gt_root / dataset_version / "families"
    gt_dir.mkdir(parents=True, exist_ok=True)
    shard, tmp = (
        gt_dir / f"family_graph.block-{block:04d}.jsonl",
        gt_dir / f"family_graph.block-{block:04d}.jsonl.tmp",
    )
    sha = hashlib.sha256()
    by_type: dict[str, int] = {}
    in_family: set[uuid.UUID] = set()

    raw = session.connection().connection.driver_connection
    with raw.cursor() as cur, open(tmp, "wb") as gt_fh:
        cur.execute("SET LOCAL synchronous_commit = off")
        cur.execute("DROP TABLE IF EXISTS pg_temp.stg_family")
        cur.execute(
            "CREATE TEMP TABLE stg_family (id uuid, resident_id uuid, related_resident_id uuid, relationship_type text, "
            "relationship_direction text, source_resource_id text, source_type text, confidence_status text) "
            "ON COMMIT DROP"
        )
        if replace:
            cur.execute(
                f"DELETE FROM {S}.family_edges WHERE source_type = %s AND resident_id = ANY(%s)",
                (SOURCE_TYPE, list(ids)),
            )
        for district, (d_ids, born, sex) in by_district.items():
            result.residents += len(d_ids)
            rng = random.Random(f"anticipa-family:{dataset_version}:{base_seed}:{block}:{district}")
            units = build_units(born, sex, ref_ord, rng, cfg)
            result.units += len(units)
            with cur.copy("COPY stg_family FROM STDIN") as cp:
                for gt, db_row in _iter_gt(dataset_version, d_ids, units, sex, born, ref_ord, cfg):
                    line = orjson.dumps(gt, option=orjson.OPT_SORT_KEYS) + b"\n"
                    sha.update(line)
                    gt_fh.write(line)
                    cp.write_row(db_row)
                    by_type[gt["relationship_type"]] = by_type.get(gt["relationship_type"], 0) + 1
                    result.edges += 1
            for u in units:
                in_family.update(d_ids[m] for m in u.members())
            log(f"block {block} district {district}: {len(d_ids)} residents, {len(units)} families")
        gt_fh.flush()
        os.fsync(gt_fh.fileno())
        cur.execute(
            f"INSERT INTO {S}.family_edges (id, resident_id, related_resident_id, relationship_type, "
            "relationship_direction, source_resource_id, source_type, confidence_status, synthetic_status) "
            "SELECT id, resident_id, related_resident_id, relationship_type, relationship_direction, "
            "source_resource_id, source_type, confidence_status, 'synthetic' FROM stg_family "
            "ON CONFLICT (resident_id, related_resident_id, relationship_type) DO NOTHING"
        )
    os.replace(tmp, shard)
    _merge_ground_truth(gt_dir, block)
    result.residents_in_families = len(in_family)
    result.edges_by_type = dict(sorted(by_type.items()))
    result.ground_truth_file = str(gt_dir / "family_graph.jsonl")
    result.ground_truth_sha256 = sha.hexdigest()  # of this block's shard
    result.seconds = round(time.perf_counter() - t0, 1)

    # block summary into datasets.manifest.family_linker (under the row lock taken by _dataset_row)
    manifest = dict(manifest)
    fl = dict(manifest.get("family_linker") or {})
    blocks = dict(fl.get("blocks") or {})
    profiles = {
        loaded[str(i)].get("profile", "full") for i in batches
    }  # entries without a profile are full loads
    blocks[str(block)] = {
        "profile": profiles.pop() if len(profiles) == 1 else "mixed",
        "batches": [batches[0], batches[-1] + 1],
        "residents": result.residents,
        "residents_in_families": result.residents_in_families,
        "units": result.units,
        "edges": result.edges,
        "edges_by_type": result.edges_by_type,
        "shard": shard.name,
        "shard_sha256": result.ground_truth_sha256,
        "linked_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    fl.update(
        linker_version=LINKER_VERSION,
        config=asdict(cfg),
        block_batches=block_batches,
        base_seed=base_seed,
        reference_date=ref,
        blocks=dict(sorted(blocks.items(), key=lambda kv: int(kv[0]))),
        units=sum(b["units"] for b in blocks.values()),
        edges=sum(b["edges"] for b in blocks.values()),
        residents=sum(b["residents"] for b in blocks.values()),
        residents_in_families=sum(b["residents_in_families"] for b in blocks.values()),
        ground_truth_file=f"data/ground_truth/{dataset_version}/families/family_graph.jsonl",
    )
    fl["ground_truth_edges"] = fl["edges"]
    # tree hash: sha256 over the per-block shard hashes in block order (cheap; each shard hash is also recorded)
    fl["ground_truth_sha256"] = hashlib.sha256(
        "".join(b["shard_sha256"] for b in fl["blocks"].values()).encode()
    ).hexdigest()
    manifest["family_linker"] = fl
    session.execute(
        text(f"UPDATE {S}.datasets SET manifest = CAST(:m AS jsonb) WHERE id = :id"),
        {"m": orjson.dumps(manifest).decode(), "id": dataset_id},
    )
    session.commit()
    (gt_dir / "family_graph.meta.json").write_bytes(
        orjson.dumps(
            {"schema_version": "gt.family_graph_meta.v1", "dataset_version": dataset_version, **fl},
            option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS,
        )
    )
    return result


def link_ready_blocks(
    session: Session,
    dataset_version: str,
    *,
    block_batches: int = BLOCK_BATCHES,
    data_dir: Path = SYNTHEA_DIR,
    cfg: LinkerConfig | None = None,
    gt_root: Path = GT_DIR,
    allow_partial: bool = False,
    relink: bool = False,
    log=lambda m: None,
) -> list[LinkResult]:
    """Link every block whose batches are all loaded and that has not been linked yet (cheap no-op otherwise)."""
    row = session.execute(
        text(f"SELECT manifest FROM {S}.datasets WHERE dataset_version = :v"), {"v": dataset_version}
    ).first()
    if row is None:
        raise ValueError(f"unknown dataset {dataset_version!r} (ingest it first)")
    manifest = row[0]
    planned = int((manifest.get("generation") or {}).get("batches_planned") or 0)
    loaded = {int(k) for k in ((manifest.get("ingest") or {}).get("batches") or {})}
    linked = {int(k) for k in ((manifest.get("family_linker") or {}).get("blocks") or {})}
    session.rollback()  # end the read transaction; link_block takes its own row lock
    out: list[LinkResult] = []
    for k in range(-(-planned // block_batches)):
        batches = block_batch_range(k, block_batches, planned)
        if (k in linked and not relink) or not set(batches) <= loaded:
            continue
        if len(batches) < block_batches and not allow_partial:
            log(f"block {k} is partial ({len(batches)}/{block_batches} batches): not linked")
            continue
        out.append(
            link_block(
                session,
                dataset_version,
                k,
                block_batches=block_batches,
                data_dir=data_dir,
                cfg=cfg,
                gt_root=gt_root,
                allow_partial=allow_partial,
                log=log,
            )
        )
    return out


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m anticipa.datafabric.family")
    sub = p.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser(
        "link", help="build family edges + ground truth for every fully loaded, unlinked block"
    )
    g.add_argument("--dataset-version", required=True)
    g.add_argument("--database-url", default=None)
    g.add_argument("--data-dir", type=Path, default=SYNTHEA_DIR)
    g.add_argument("--gt-dir", type=Path, default=GT_DIR)
    g.add_argument("--block-batches", type=int, default=BLOCK_BATCHES)
    g.add_argument("--family-fraction", type=float, default=LinkerConfig.family_fraction)
    g.add_argument("--allow-partial", action="store_true", help="also link a last block with fewer batches")
    g.add_argument(
        "--relink", action="store_true", help="re-link blocks already linked (same seed: same result)"
    )
    a = p.parse_args(argv)
    with Session(engine_for(a.database_url)) as session:
        results = link_ready_blocks(
            session,
            a.dataset_version,
            block_batches=a.block_batches,
            data_dir=a.data_dir,
            cfg=LinkerConfig(family_fraction=a.family_fraction),
            gt_root=a.gt_dir,
            allow_partial=a.allow_partial,
            relink=a.relink,
            log=lambda m: print(m, flush=True),
        )
    print(json.dumps([asdict(r) for r in results]))


if __name__ == "__main__":
    main()
