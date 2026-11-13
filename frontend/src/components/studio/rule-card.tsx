"use client";

import { useState } from "react";
import type { Rule } from "@/lib/types";
import { Card, Icon, Pill, btnPrimary, btnSecondary, inputClass, type Tone } from "@/components/ui";
import { type Expr, type Selectors, applyEdits, childrenOf, describeLeaf, groupLabel, isGroup, outcomeLines, plain } from "./criteria";

export interface Issue {
  field?: string;
  reason?: string;
}
export interface SaveOutcome {
  ok: boolean;
  message?: string;
  issues?: Issue[];
}

const TYPE: Record<string, { label: string; tone: Tone }> = {
  prerequisite: { label: "Prerequisite", tone: "info" },
  inclusion: { label: "Inclusion", tone: "ok" },
  exclusion: { label: "Exclusion", tone: "danger" },
  temporal: { label: "Timing", tone: "warn" },
};

interface Ctx {
  selectors: Selectors;
  editable: boolean;
  raw: Record<string, string>;
  setRaw: (path: string, v: string) => void;
  ruleName: string;
}

function Leaf({ expr, path, ctx }: { expr: Expr; path: string; ctx: Ctx }) {
  const v = describeLeaf(expr, ctx.selectors);
  const e = v.edit;
  const shown = e ? (ctx.raw[path] ?? String(e.value)) : "";
  const n = Number(shown);
  const invalid = !!e && (shown === "" || !Number.isInteger(n) || n < e.min || n > e.max);
  const changed = !!e && !invalid && n !== e.value;
  return (
    <p className="flex flex-wrap items-center gap-x-2 gap-y-1.5 text-sm text-ink">
      <span className="min-w-0">{v.prefix}</span>
      {e &&
        (ctx.editable ? (
          <input
            type="number"
            inputMode="numeric"
            min={e.min}
            max={e.max}
            step={1}
            value={shown}
            onChange={(ev) => ctx.setRaw(path, ev.target.value)}
            aria-label={`${ctx.ruleName}: ${v.prefix} (${e.unit})`}
            aria-invalid={invalid || undefined}
            className={`${inputClass} tnum !min-h-9 !w-20 text-center font-semibold ${invalid ? "border-danger" : changed ? "border-brand-mid ring-2 ring-brand-soft" : ""}`}
          />
        ) : (
          <strong className="tnum">{e.value}</strong>
        ))}
      {v.suffix && <span>{v.suffix}</span>}
      {changed && e && <span className="text-xs text-ink-muted">was {e.value}</span>}
      {invalid && e && <span className="text-xs font-semibold text-danger">Enter a whole number from {e.min} to {e.max}</span>}
    </p>
  );
}

function Node({ expr, path, ctx }: { expr: Expr; path: string; ctx: Ctx }) {
  if (!isGroup(expr)) return <Leaf expr={expr} path={path} ctx={ctx} />;
  return (
    <div className="min-w-0">
      <p className="text-[0.8125rem] font-semibold text-ink-soft">{groupLabel(expr)}</p>
      <ul className="mt-1 space-y-1.5 border-l-2 border-brand-soft pl-3">
        {childrenOf(expr).map((c, i) => (
          <li key={i}>
            <Node expr={c} path={`${path}.${i}`} ctx={ctx} />
          </li>
        ))}
      </ul>
    </div>
  );
}

/** Numbers a reviewer changed, as { path: number }, or null while an input holds something invalid. */
function parsedEdits(expr: Expr, raw: Record<string, string>, selectors: Selectors): { edits: Record<string, number>; invalid: boolean } {
  const edits: Record<string, number> = {};
  let invalid = false;
  const walk = (n: Expr, path: string) => {
    if (isGroup(n)) return childrenOf(n).forEach((c, i) => walk(c, `${path}.${i}`));
    const e = describeLeaf(n, selectors).edit;
    const text = raw[path];
    if (!e || text === undefined) return;
    const num = Number(text);
    if (text === "" || !Number.isInteger(num) || num < e.min || num > e.max) invalid = true;
    else if (num !== e.value) edits[path] = num;
  };
  walk(expr, "r");
  return { edits, invalid };
}

/**
 * One rule as sentences. On a working version a reviewer can change its numbers (age limits, look back windows, counts);
 * Check and save validates the whole rule set against the API first, then replaces the rule. Nothing here touches an
 * approved version: those rules are shown read only.
 */
export function RuleCard({
  rule,
  selectors,
  editable,
  busy,
  onSave,
}: {
  rule: Rule;
  selectors: Selectors;
  editable: boolean;
  busy: boolean;
  onSave: (next: Record<string, unknown>) => Promise<SaveOutcome>;
}) {
  const def = (rule.rule_definition ?? {}) as Record<string, unknown>;
  const expr = (def.expression ?? {}) as Expr;
  const [raw, setRaw] = useState<Record<string, string>>({});
  const [outcome, setOutcome] = useState<SaveOutcome | null>(null);
  const [saving, setSaving] = useState(false);
  const { edits, invalid } = parsedEdits(expr, raw, selectors);
  const dirty = Object.keys(edits).length > 0;
  const type = TYPE[rule.rule_type] ?? { label: plain(rule.rule_type), tone: "neutral" as Tone };
  const name = plain(rule.name);
  const ctx: Ctx = { selectors, editable, raw, setRaw: (p, v) => (setOutcome(null), setRaw((r) => ({ ...r, [p]: v }))), ruleName: name };

  const save = async () => {
    setSaving(true);
    const res = await onSave({ ...def, expression: applyEdits(expr, edits) });
    setSaving(false);
    if (!res.ok) setOutcome(res);
  };

  return (
    <Card as="article" aria-label={`Rule: ${name}`} className="glass-strong px-5 py-4" data-rule-id={rule.rule_id}>
      <div className="flex flex-wrap items-start justify-between gap-x-3 gap-y-1">
        <h3 className="min-w-0 font-semibold text-ink [overflow-wrap:anywhere]">{name}</h3>
        <Pill tone={type.tone}>{type.label}</Pill>
      </div>
      {rule.description && <p className="mt-1 max-w-[70ch] text-[0.8125rem] text-ink-muted">{plain(rule.description)}</p>}
      <div className="mt-3">
        <Node expr={expr} path="r" ctx={ctx} />
      </div>
      <dl className="mt-3 grid gap-x-6 gap-y-1 text-[0.8125rem] sm:grid-cols-3">
        {outcomeLines(def.outcome_by_expression_result).map((l) => (
          <div key={l.when}>
            <dt className="text-ink-muted">{l.when}</dt>
            <dd className="font-medium text-ink-soft">{l.outcome}</dd>
          </div>
        ))}
      </dl>
      <p className="mt-2 text-xs text-ink-muted">
        Reason code <span className="font-mono">{String(def.reason_code ?? rule.rule_key)}</span>
      </p>
      {editable && (dirty || invalid) && (
        <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-line pt-3">
          <button type="button" className={btnPrimary} onClick={save} disabled={busy || saving || invalid}>
            <Icon name="check" className="size-4" />
            {saving ? "Checking" : "Check and save change"}
          </button>
          <button type="button" className={btnSecondary} onClick={() => (setRaw({}), setOutcome(null))} disabled={saving}>
            Discard change
          </button>
        </div>
      )}
      {outcome && !outcome.ok && (
        <div role="alert" className="mt-3 rounded-control border border-danger-line bg-danger-bg px-4 py-3 text-sm text-danger">
          <p className="font-semibold">{outcome.message ?? "This change was not saved"}</p>
          {outcome.issues && outcome.issues.length > 0 && (
            <ul className="mt-1 list-disc pl-5">
              {outcome.issues.map((i, k) => (
                <li key={k}>
                  {i.field ? <span className="font-mono text-xs">{i.field}</span> : null}
                  {i.field && i.reason ? ": " : ""}
                  {plain(i.reason)}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </Card>
  );
}
