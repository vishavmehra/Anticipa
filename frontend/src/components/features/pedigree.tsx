"use client";

// Family pedigree: the resident, the stored relatives (family_edges rows) and each relative's cancer history with its
// source (structured clinical record or a fact read from a note). Custom SVG, no chart library. Reads
// GET /residents/{id}/pedigree (backend/anticipa/analytics/journey.py `resident_pedigree`). A relative is never inferred
// from a document; lines are drawn only where the relationship word itself fixes the link (a maternal aunt is a sibling
// of the mother). Symbols: circle female, square male, diamond not stated; hatched fill = cancer history on file; a
// slash = deceased. A list under the drawing carries every detail. Purpose: show judges the family evidence behind a rule.

import Link from "next/link";
import { api, isMissingEndpoint } from "@/lib/api";
import { humanize } from "@/lib/format";
import { useApi } from "@/lib/use-api";
import { StatusBadge } from "@/components/status-badge";
import { Card, CardHeader, DataLabel, ErrorPanel, Unavailable } from "@/components/ui";
import { ShowInNote } from "./note-viewer";
import type { Pedigree, PedigreeRelative } from "./payloads";
import { Tag, plain } from "./shared";

type Sex = "female" | "male" | "unknown";
const SEX_OF: Record<string, Sex> = {
  mother: "female", sister: "female", daughter: "female", maternal_aunt: "female", paternal_aunt: "female", maternal_grandmother: "female", paternal_grandmother: "female", niece: "female", granddaughter: "female", half_sister: "female",
  father: "male", brother: "male", son: "male", maternal_uncle: "male", paternal_uncle: "male", maternal_grandfather: "male", paternal_grandfather: "male", nephew: "male", grandson: "male", half_brother: "male",
};
/** Generation row (relative to the resident at 0) and left to right order inside the row. Other relationships go to "Other relatives". */
const ROW: Record<string, [number, number]> = {
  maternal_grandmother: [-2, 0], maternal_grandfather: [-2, 1], paternal_grandmother: [-2, 2], paternal_grandfather: [-2, 3],
  maternal_aunt: [-1, 0], maternal_uncle: [-1, 1], mother: [-1, 2], father: [-1, 3], paternal_aunt: [-1, 4], paternal_uncle: [-1, 5],
  half_sister: [0, 0], sister: [0, 1], brother: [0, 3], half_brother: [0, 4], cousin: [0, 5], spouse: [0, 6],
  daughter: [1, 0], son: [1, 1], niece: [1, 2], nephew: [1, 3],
  granddaughter: [2, 0], grandson: [2, 1],
};

const W = 128; // horizontal slot
const H = 118; // row height
const R = 20; // symbol half size

interface Node {
  id: string;
  rel: string;
  label: string;
  sub: string;
  x: number;
  y: number;
  sex: Sex;
  affected: boolean | null; // null = this role cannot see cancer history
  deceased: boolean;
  href?: string;
  resident?: boolean;
}

const hasHistory = (r: PedigreeRelative): boolean | null => {
  const rec = r.cancer_history.clinical_records;
  const fact = r.cancer_history.evidence_facts;
  if (rec === null && fact === null) return null;
  return (rec?.length ?? 0) > 0 || (fact?.length ?? 0) > 0;
};

function layout(p: Pedigree) {
  const rows = new Map<number, { rel: PedigreeRelative; order: number }[]>();
  const other: PedigreeRelative[] = [];
  for (const r of p.relatives) {
    const pos = ROW[r.relationship];
    if (!pos) other.push(r);
    else rows.set(pos[0], [...(rows.get(pos[0]) ?? []), { rel: r, order: pos[1] }]);
  }
  const resident = p.resident;
  // The resident sits between the siblings (order 2) in generation 0.
  const gen0 = rows.get(0) ?? [];
  const slots: Record<number, { rel?: PedigreeRelative; self?: boolean; order: number }[]> = {};
  for (const [g, list] of rows) slots[g] = list.sort((a, b) => a.order - b.order);
  slots[0] = [...gen0, { self: true, order: 2 }].sort((a, b) => a.order - b.order);
  const gens = Object.keys(slots).map(Number).sort((a, b) => a - b);
  const maxCols = Math.max(...gens.map((g) => slots[g].length), 3);
  const width = maxCols * W + 24;
  const minGen = Math.min(...gens, 0);
  const nodes: Node[] = [];
  for (const g of gens) {
    const list = slots[g];
    const offset = (width - list.length * W) / 2;
    list.forEach((s, i) => {
      const x = offset + i * W + W / 2;
      const y = (g - minGen) * H + 36;
      if (s.self) {
        const sex: Sex = resident.administrative_sex === "female" ? "female" : resident.administrative_sex === "male" ? "male" : "unknown";
        nodes.push({ id: "self", rel: "self", label: "This resident", sub: [resident.age != null ? `${resident.age}` : null, sex === "female" ? "F" : sex === "male" ? "M" : null].filter(Boolean).join(", "), x, y, sex, affected: null, deceased: false, resident: true });
      } else if (s.rel) {
        const r = s.rel;
        const lr = r.linked_resident;
        const sex: Sex = SEX_OF[r.relationship] ?? (lr?.administrative_sex === "female" ? "female" : lr?.administrative_sex === "male" ? "male" : "unknown");
        nodes.push({
          id: r.edge_id, rel: r.relationship, label: humanize(r.relationship),
          sub: lr ? [lr.age != null ? `${lr.age}` : null, lr.deceased ? "deceased" : null].filter(Boolean).join(", ") : "Not a resident",
          x, y, sex, affected: hasHistory(r), deceased: !!lr?.deceased, href: lr ? `/residents/${lr.id}` : undefined,
        });
      }
    });
  }
  const height = (gens[gens.length - 1] - minGen) * H + 36 + R + 44;
  return { nodes, other, width, height };
}

function Symbol({ n }: { n: Node }) {
  const common = { stroke: "currentColor", strokeWidth: n.resident ? 3 : 1.6, strokeDasharray: n.affected === null && !n.resident ? "4 3" : undefined } as const;
  const fill = n.affected ? "url(#affected)" : "var(--color-surface)";
  return (
    <g className="text-ink">
      {n.sex === "female" ? <circle cx={n.x} cy={n.y} r={R} fill={fill} {...common} /> : n.sex === "male" ? <rect x={n.x - R} y={n.y - R} width={R * 2} height={R * 2} fill={fill} {...common} /> : <path d={`M${n.x} ${n.y - R - 3}L${n.x + R + 3} ${n.y}L${n.x} ${n.y + R + 3}L${n.x - R - 3} ${n.y}Z`} fill={fill} {...common} />}
      {n.deceased && <path d={`M${n.x - R - 5} ${n.y + R + 5}L${n.x + R + 5} ${n.y - R - 5}`} stroke="currentColor" strokeWidth="1.6" />}
      {n.affected === null && !n.resident && (
        <text x={n.x} y={n.y + 5} textAnchor="middle" fontSize="14" fill="currentColor" aria-hidden="true">
          ?
        </text>
      )}
    </g>
  );
}

function Drawing({ p }: { p: Pedigree }) {
  const { nodes, width, height } = layout(p);
  const at = (rel: string) => nodes.filter((n) => n.rel === rel);
  const mid = (list: Node[]) => (list.length ? list.reduce((s, n) => s + n.x, 0) / list.length : null);
  const self = nodes.find((n) => n.resident)!;
  const lines: { key: string; d: string; dashed?: boolean }[] = [];
  const bus = (key: string, parents: Node[], kids: Node[]) => {
    if (!parents.length || !kids.length) return;
    const px = mid(parents)!;
    const py = parents[0].y;
    const ky = kids[0].y;
    const by = (py + ky) / 2 + 6;
    const xs = [...kids.map((k) => k.x), px];
    lines.push({ key: `${key}-v`, d: `M${px} ${py}V${by}` });
    lines.push({ key: `${key}-h`, d: `M${Math.min(...xs)} ${by}H${Math.max(...xs)}` });
    for (const k of kids) lines.push({ key: `${key}-${k.id}`, d: `M${k.x} ${by}V${k.y - R - (k.sex === "unknown" ? 3 : 0)}` });
  };
  const pair = (key: string, a: Node[], b: Node[]) => {
    if (a[0] && b[0]) lines.push({ key, d: `M${a[0].x + R} ${a[0].y}H${b[0].x - R}` });
  };
  pair("mf", at("mother"), at("father"));
  pair("mgp", at("maternal_grandmother"), at("maternal_grandfather"));
  pair("pgp", at("paternal_grandmother"), at("paternal_grandfather"));
  pair("spouse", [self], at("spouse"));
  bus("mat", [...at("maternal_grandmother"), ...at("maternal_grandfather")], [...at("mother"), ...at("maternal_aunt"), ...at("maternal_uncle")]);
  bus("pat", [...at("paternal_grandmother"), ...at("paternal_grandfather")], [...at("father"), ...at("paternal_aunt"), ...at("paternal_uncle")]);
  bus("kids", [self], [...at("daughter"), ...at("son")]);
  const parents = [...at("mother"), ...at("father")];
  bus("sibs", parents, [self, ...at("sister"), ...at("brother")]);

  const label = `Pedigree of the resident with ${p.counts.relatives} stored relatives. ${p.counts.relatives_with_cancer_history} have cancer history on file.`;
  return (
    <div role="region" aria-label="Pedigree drawing" tabIndex={0} className="overflow-x-auto rounded-control border border-line bg-surface">
      <svg role="group" aria-label={label} viewBox={`0 0 ${width} ${height}`} width="100%" style={{ minWidth: Math.min(width, 560), maxHeight: 520 }} className="block">
        <defs>
          <pattern id="affected" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
            <rect width="6" height="6" fill="#fde3e1" />
            <line x1="0" y1="0" x2="0" y2="6" stroke="#b42318" strokeWidth="3" />
          </pattern>
        </defs>
        {lines.map((l) => (
          <path key={l.key} d={l.d} stroke="#7a8a9e" strokeWidth="1.6" fill="none" strokeDasharray={l.dashed ? "4 3" : undefined} />
        ))}
        {nodes.map((n) => {
          const body = (
            <g>
              <title>{`${n.label}${n.sub ? `, ${n.sub}` : ""}${n.affected ? ", cancer history on file" : n.affected === false ? ", no cancer history on file" : ""}`}</title>
              <Symbol n={n} />
              <text x={n.x} y={n.y + R + 18} textAnchor="middle" fontSize="12" fontWeight={n.resident ? 700 : 500} fill="#0f1b2d">
                {n.label}
              </text>
              {n.sub && (
                <text x={n.x} y={n.y + R + 32} textAnchor="middle" fontSize="11" fill="#556579">
                  {n.sub}
                </text>
              )}
            </g>
          );
          return n.href ? (
            <a key={n.id} href={n.href} className="focus-visible:outline-2">
              {body}
            </a>
          ) : (
            <g key={n.id}>{body}</g>
          );
        })}
      </svg>
    </div>
  );
}

function Shape({ children }: { children: React.ReactNode }) {
  return (
    <svg viewBox="0 0 28 28" width="22" height="22" aria-hidden="true" className="text-ink">
      {children}
    </svg>
  );
}

function Legend() {
  return (
    <ul aria-label="Legend" className="mt-3 flex flex-wrap gap-x-5 gap-y-1.5 text-xs text-ink-soft">
      <li className="flex items-center gap-1.5"><Shape><circle cx="14" cy="14" r="9" fill="none" stroke="currentColor" strokeWidth="1.8" /></Shape>Female</li>
      <li className="flex items-center gap-1.5"><Shape><rect x="5" y="5" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.8" /></Shape>Male</li>
      <li className="flex items-center gap-1.5"><Shape><path d="M14 3L25 14L14 25L3 14Z" fill="none" stroke="currentColor" strokeWidth="1.8" /></Shape>Not stated</li>
      <li className="flex items-center gap-1.5">
        <Shape>
          <defs>
            <pattern id="affected-key" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
              <rect width="6" height="6" fill="#fde3e1" />
              <line x1="0" y1="0" x2="0" y2="6" stroke="#b42318" strokeWidth="3" />
            </pattern>
          </defs>
          <circle cx="14" cy="14" r="9" fill="url(#affected-key)" stroke="currentColor" strokeWidth="1.8" />
        </Shape>
        Cancer history on file
      </li>
      <li className="flex items-center gap-1.5"><Shape><circle cx="14" cy="14" r="9" fill="none" stroke="currentColor" strokeWidth="1.8" /><path d="M5 23L23 5" stroke="currentColor" strokeWidth="1.8" /></Shape>Deceased</li>
      <li className="flex items-center gap-1.5"><Shape><circle cx="14" cy="14" r="9" fill="none" stroke="currentColor" strokeWidth="1.8" strokeDasharray="4 3" /></Shape>History not visible to your role</li>
    </ul>
  );
}

function History({ r }: { r: PedigreeRelative }) {
  const rec = r.cancer_history.clinical_records;
  const facts = r.cancer_history.evidence_facts;
  return (
    <div className="space-y-2">
      {rec === null ? (
        <p className="text-xs text-ink-muted">Your role cannot view clinical records for this relative.</p>
      ) : (
        rec.map((c) => (
          <p key={c.id}>
            <Tag>From a clinical record</Tag> <span className="font-medium">{c.display ?? "Condition"}</span>
            {c.effective_start ? <span className="text-ink-muted">, from {c.effective_start.slice(0, 4)}</span> : null}
            {c.source_reference ? <span className="block font-mono text-xs break-all text-ink-muted">{c.source_reference}</span> : null}
          </p>
        ))
      )}
      {facts === null ? (
        <p className="text-xs text-ink-muted">Your role cannot view facts read from notes.</p>
      ) : (
        facts.map((f) => (
          <div key={f.id}>
            <p className="flex flex-wrap items-center gap-1.5">
              <Tag>{f.basis === "relative_own_document" ? "From the relative's own note" : "Stated in this resident's note"}</Tag>
              <span className="font-medium">{humanize(f.fact_type)}</span>
              <StatusBadge status={f.assertion_status} />
              {f.age_at_onset != null ? <span className="text-ink-muted">at age {f.age_at_onset}</span> : null}
            </p>
            {f.sources[0]?.quote && (
              <blockquote dir="auto" className="mt-1 rounded-control bg-canvas px-3 py-2 text-ink-soft">
                {f.sources[0].quote}
              </blockquote>
            )}
            <ShowInNote factId={f.id} />
          </div>
        ))
      )}
      {rec?.length === 0 && facts?.length === 0 && <p className="text-ink-muted">No cancer history on file for this relative.</p>}
    </div>
  );
}

export function FamilyPedigree({ residentId }: { residentId: string }) {
  const res = useApi(`pedigree:${residentId}`, (signal) => api<Pedigree>(`/residents/${residentId}/pedigree`, { signal }));
  const p = res.data?.data;

  let body;
  if (res.error) {
    body = isMissingEndpoint(res.error) ? <Unavailable endpoint="GET /api/v1/residents/{id}/pedigree" /> : <ErrorPanel error={res.error} onRetry={res.reload} title="Could not load the family" />;
  } else if (!p) {
    body = <div role="status" aria-label="Loading the family" className="skeleton mx-5 mb-5 h-48" />;
  } else if (p.relatives.length === 0) {
    body = <p className="px-5 pb-5 text-ink-muted">No relatives are stored for this resident.</p>;
  } else {
    const { other } = layout(p);
    body = (
      <div className="space-y-5 px-5 pb-5">
        <p className="text-ink-soft">
          {p.counts.relatives} stored relatives, {p.counts.linked_relatives} of them residents, {p.counts.relatives_with_cancer_history} with cancer history on file.
        </p>
        <Drawing p={p} />
        <Legend />
        <ul aria-label="Relatives" className="divide-y divide-line overflow-hidden rounded-control border border-line">
          {[...p.relatives].sort((a, b) => (a.degree ?? 9) - (b.degree ?? 9) || a.relationship.localeCompare(b.relationship)).map((r) => (
            <li key={r.edge_id} data-relative={r.relationship} className="px-4 py-3">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-semibold">{humanize(r.relationship)}</span>
                {r.degree_label && <Tag>{humanize(plain(r.degree_label))} relative</Tag>}
                {r.blood_relative === false && <Tag>Not a blood relative</Tag>}
                {r.linked_resident ? (
                  <Link href={`/residents/${r.linked_resident.id}`} className="text-brand-ink underline underline-offset-2">
                    Resident <span className="font-mono text-xs">{r.linked_resident.id.slice(0, 8)}</span>
                  </Link>
                ) : (
                  <span className="text-ink-muted">Not a resident</span>
                )}
                {r.linked_resident && <span className="text-ink-muted">{[r.linked_resident.age != null ? `${r.linked_resident.age} years` : null, humanize(r.linked_resident.administrative_sex), r.linked_resident.district_code, r.linked_resident.deceased ? "deceased" : null].filter((x) => x && x !== "None").join(", ")}</span>}
              </div>
              <div className="mt-2">
                <History r={r} />
              </div>
            </li>
          ))}
        </ul>
        {other.length > 0 && <p className="text-xs text-ink-muted">Relatives with a relationship the drawing does not place are listed above only: {other.map((r) => humanize(r.relationship)).join(", ")}.</p>}
        {(p.unattributed_family_facts?.length ?? 0) > 0 && (
          <div className="rounded-control border border-warn-line bg-warn-bg px-4 py-3 text-warn">
            <p className="font-semibold">{p.unattributed_family_facts!.length} facts about family could not be tied to one relative</p>
            <p className="text-sm">{plain(p.unattributed_note)}</p>
          </div>
        )}
        <p className="max-w-[80ch] text-xs text-ink-muted">{plain(p.definition)}</p>
      </div>
    );
  }
  return (
    <Card as="section" aria-labelledby="pedigree-h" className="mt-6">
      <CardHeader id="pedigree-h" icon="users-round" title="Family history" subtitle="Stored relatives and their cancer history, with the source of each fact." aside={<DataLabel kind="live" />} />
      {body}
    </Card>
  );
}
