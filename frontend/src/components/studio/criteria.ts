import { displayText } from "@/lib/display";
import { humanize } from "@/lib/format";

// Turns a rule expression (Docs/08 section 6: constrained JSON, never executed) into sentences a reviewer can read, and
// applies threshold edits to a copy of it. Pure functions, no network. Owner copy rule: no hyphen or dash in UI text,
// so backend prose goes through plain().

export interface Expr {
  operator?: string;
  conditions?: Expr[];
  condition?: Expr;
  field?: string;
  value?: unknown;
}
export type Selectors = Record<string, { description?: unknown } | undefined>;

/** Backend prose for display: spaced dashes and number ranges rewritten, and word joining hyphens read as spaces. */
export const plain = (s: string | null | undefined): string => displayText(s).replace(/(?<=[A-Za-z])-(?=[A-Za-z])/g, " ");

const CMP: Record<string, string> = {
  GREATER_THAN_OR_EQUAL: "at least",
  GREATER_THAN: "more than",
  LESS_THAN_OR_EQUAL: "at most",
  LESS_THAN: "less than",
  EQUALS: "equal to",
  NOT_EQUALS: "not equal to",
  IN: "one of",
  NOT_IN: "none of",
};

export const childrenOf = (n: Expr): Expr[] => n.conditions ?? (n.condition ? [n.condition] : []);
export const isGroup = (n: Expr) => n.operator === "AND" || n.operator === "OR" || n.operator === "NOT";

export function groupLabel(n: Expr): string {
  if (n.operator === "AND") return "All of these";
  if (n.operator === "OR") return "Any one of these";
  return childrenOf(n).length > 1 ? "None of these" : "Not this";
}

/** First sentence of the selector description, which is how the rule library explains what the selector finds. */
export function selectorLabel(name: string, selectors: Selectors | null | undefined): string {
  const d = selectors?.[name]?.description;
  if (typeof d === "string" && d.trim()) {
    const first = d.split(/\.(?:\s|$)/)[0].trim();
    return plain(first.length > 110 ? `${first.slice(0, 107).trimEnd()}...` : first);
  }
  return humanize(name);
}

const UNITS = ["years", "months", "days"] as const;
type Unit = (typeof UNITS)[number];
const windowUnit = (v: unknown): Unit | null => {
  if (!v || typeof v !== "object") return null;
  return UNITS.find((u) => typeof (v as Record<string, unknown>)[u] === "number") ?? null;
};

export interface LeafView {
  prefix: string;
  suffix: string;
  /** Present when the leaf holds a number a reviewer may change. */
  edit?: { value: number; unit: string; min: number; max: number };
}

const list = (v: unknown) => (Array.isArray(v) ? v.map((x) => humanize(String(x)).toLowerCase()).join(", ") : String(v));
const ref = (v: unknown, selectors: Selectors | null | undefined): string | null => {
  const f = v && typeof v === "object" ? (v as { field?: unknown }).field : null;
  const m = typeof f === "string" ? /^evidence\.([a-z0-9_]+)\.([a-z_]+)$/.exec(f) : null;
  return m ? `the ${humanize(m[2]).toLowerCase()} from ${selectorLabel(m[1], selectors)}` : null;
};

export function describeLeaf(leaf: Expr, selectors: Selectors | null | undefined): LeafView {
  const f = leaf.field ?? "";
  const op = leaf.operator ?? "";
  const v = leaf.value;
  const cmp = CMP[op] ?? humanize(op).toLowerCase();
  const numeric = (prefix: string, unit: string, max = 130): LeafView => {
    const r = ref(v, selectors);
    if (r) return { prefix: `${prefix} ${cmp} ${r}`, suffix: "" };
    if (typeof v === "number") return { prefix: `${prefix} ${cmp}`, suffix: unit, edit: { value: v, unit, min: 0, max } };
    return { prefix: `${prefix} ${cmp} ${list(v)}`, suffix: unit };
  };

  if (f === "resident.age") return numeric("Age is", "years");
  if (f === "resident.sex_assigned_at_birth") {
    const is = op === "EQUALS" ? "is" : op === "NOT_EQUALS" ? "is not" : op === "IN" ? "is one of" : "is none of";
    return { prefix: `Sex recorded at birth ${is} ${list(v)}`, suffix: "" };
  }
  if (f === "evidence.selector" && typeof v === "string") {
    const label = selectorLabel(v, selectors);
    return { prefix: op === "NOT_EXISTS" ? `No evidence found: ${label}` : `Evidence found: ${label}`, suffix: "" };
  }
  const shared = /^evidence\.([a-z0-9_]+)\.shared_subjects\.([a-z0-9_]+)$/.exec(f);
  if (shared) {
    const head = `${selectorLabel(shared[1], selectors)} and ${selectorLabel(shared[2], selectors)}: relatives with both are`;
    return numeric(head, "relatives", 50);
  }
  const m = /^evidence\.([a-z0-9_]+)\.([a-z_]+)$/.exec(f);
  if (m) {
    const label = selectorLabel(m[1], selectors);
    if (m[2] === "latest_event_date") {
      const unit = windowUnit(v);
      if (unit)
        return {
          prefix: `${label}, latest event within the last`,
          suffix: unit,
          edit: { value: (v as Record<string, number>)[unit], unit, min: 0, max: unit === "years" ? 100 : 1200 },
        };
      return { prefix: `${label}, latest event within a set time`, suffix: "" };
    }
    if (m[2] === "distinct_subjects") return numeric(`${label}: number of relatives is`, "relatives", 50);
    if (m[2] === "min_age_at_onset") return numeric(`${label}: youngest age at diagnosis is`, "years");
    if (m[2] === "early_start_age") return numeric(`${label}: screening start age is`, "years");
  }
  return { prefix: `${humanize(f.replace(/\./g, " "))} ${cmp}`, suffix: typeof v === "object" ? "" : String(v ?? "") };
}

/** Outcome words for the "if met, if not met, if evidence is missing" lines. */
export const outcomeLabel = (o: string | null | undefined): string => (o ? humanize(o) : "No effect on the outcome");

/** A copy of `expr` with the numbers at `edits` (path "r", "r.0", "r.1.0" ...) replaced. The original is never changed. */
export function applyEdits(expr: Expr, edits: Record<string, number>): Expr {
  const clone = structuredClone(expr);
  for (const [key, n] of Object.entries(edits)) {
    let node = clone;
    for (const i of key.split(".").slice(1)) node = childrenOf(node)[Number(i)];
    const unit = node.operator === "DATE_WITHIN" ? windowUnit(node.value) : null;
    if (unit) node.value = { ...(node.value as object), [unit]: n };
    else node.value = n;
  }
  return clone;
}

/** One line per outcome mapping of a rule, for the card footer. */
export function outcomeLines(obr: unknown): { when: string; outcome: string }[] {
  const m = (obr && typeof obr === "object" ? obr : {}) as Record<string, string | null>;
  return [
    { when: "When the criteria are met", outcome: outcomeLabel(m.true) },
    { when: "When they are not met", outcome: outcomeLabel(m.false) },
    { when: "When evidence is missing", outcome: outcomeLabel(m.unknown) },
  ];
}
