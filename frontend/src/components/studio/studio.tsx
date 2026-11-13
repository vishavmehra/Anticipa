"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useState } from "react";
import { ApiError, api, apiList, apiPost } from "@/lib/api";
import { displayText } from "@/lib/display";
import { fmtDate, humanize } from "@/lib/format";
import { PERM } from "@/lib/nav";
import type { Guideline, Rule } from "@/lib/types";
import { useAction } from "@/lib/use-action";
import { useApi } from "@/lib/use-api";
import { useCan } from "@/components/app-shell";
import { GatedButton } from "@/components/gated-button";
import { StatusBadge } from "@/components/status-badge";
import { Card, CardHeader, EmptyState, ErrorPanel, FilterField, Icon, PageHeader, Pill, btnPrimary, btnSecondary, selectClass } from "@/components/ui";
import { type Selectors, plain } from "./criteria";
import { DryRunSection } from "./dry-run";
import { type Issue, RuleCard, type SaveOutcome } from "./rule-card";
import type { VersionFull } from "./types";

const AUTHOR = [PERM.guidelinesApprove, PERM.processingExecute] as const;
const today = () => new Date().toISOString().slice(0, 10);

function Details({ error }: { error: ApiError }) {
  return (
    <ul className="mt-1 list-disc pl-5">
      {(error.details ?? []).map((d, i) => (
        <li key={i}>
          {d.field ? <span className="font-mono text-xs">{String(d.field)}</span> : null}
          {d.field && d.reason ? ": " : ""}
          {plain(String(d.reason ?? ""))}
        </li>
      ))}
    </ul>
  );
}

// ---- one version: rules, dry run, submit ---------------------------------------------------------------------------------
function Workspace({ guideline, versionId, canAuthor, onListChanged, onPick }: { guideline: Guideline; versionId: string; canAuthor: boolean; onListChanged: () => void; onPick: (id: string) => void }) {
  const detail = useApi(`studio:version:${versionId}`, (signal) => api<VersionFull>(`/guidelines/${guideline.id}/versions/${versionId}`, { signal }));
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const copy = useAction();
  const submit = useAction();
  const full = detail.data?.data;

  if (detail.error) return <ErrorPanel error={detail.error} onRetry={detail.reload} title="Could not load this version" />;
  if (!full) return <div className="skeleton h-40" role="status" aria-label="Loading the version" />;

  const rules: Rule[] = [...(full.rules ?? [])].sort((a, b) => a.precedence - b.precedence || a.name.localeCompare(b.name));
  const isWorking = full.review_status === "draft";
  const editable = canAuthor && isWorking;
  const selectors = (full.selectors ?? {}) as Selectors;

  /** Validate the whole rule set with the change, then replace the rule. The original is put back if the add fails. */
  const saveRule = async (rule: Rule, next: Record<string, unknown>): Promise<SaveOutcome> => {
    setBusy(true);
    setNotice(null);
    try {
      const all = rules.map((r) => (r.id === rule.id ? next : (r.rule_definition ?? {})));
      const check = await apiPost<{ valid: boolean; errors: Issue[] }>("/rules/validate", { rules: all, selectors: full.selectors ?? {}, decision_policy: full.decision_policy ?? null });
      if (!check.data.valid) return { ok: false, message: "The change is not valid, so it was not saved", issues: check.data.errors };
      await api(`/rules/${rule.id}`, { method: "DELETE" });
      try {
        await apiPost("/rules", { guideline_version_id: versionId, definition: next });
      } catch (e) {
        await apiPost("/rules", { guideline_version_id: versionId, definition: rule.rule_definition ?? {} }).catch(() => undefined);
        throw e;
      }
      setNotice(`Saved. ${plain(rule.name)} now uses the new numbers. Run the dry run again to see the effect.`);
      detail.reload();
      onListChanged();
      return { ok: true };
    } catch (e) {
      const err = e instanceof ApiError ? e : null;
      return { ok: false, message: err ? displayText(err.message) : "Unexpected client error.", issues: (err?.details as Issue[] | null) ?? undefined };
    } finally {
      setBusy(false);
    }
  };

  const makeCopy = async () => {
    const ok = await copy.run(async () => {
      const res = await apiPost<VersionFull>(`/guidelines/${guideline.id}/versions`, {
        effective_from: today(),
        source_reference: full.source_reference,
        source_publication_date: full.source_publication_date,
        population_scope: full.population_scope,
        disclaimer: full.disclaimer,
        change_summary: `Working copy of version ${full.version}, made in Guideline Studio`,
        care_activity: full.care_activity,
        priority: full.priority,
        source_version: full.source_version,
        selectors: full.selectors ?? {},
        decision_policy: full.decision_policy ?? null,
        sources: full.sources ?? [],
        metadata: full.version_metadata ?? {},
        rules: rules.map((r) => r.rule_definition ?? {}),
      });
      onPick(res.data.id);
    });
    if (ok) onListChanged();
  };

  const submitForReview = async () => {
    if (await submit.run(() => apiPost(`/guidelines/${guideline.id}/versions/${versionId}/submit-review`))) {
      detail.reload();
      onListChanged();
    }
  };

  const stateText =
    full.review_status === "approved"
      ? "This version is approved and in force. Its rules are locked."
      : full.review_status === "in_review"
        ? "This version is waiting for a clinical reviewer. Its rules are locked."
        : full.review_status === "retired"
          ? "This version is retired. Its rules are locked."
          : null;

  return (
    <div className="space-y-5">
      <Card as="section" aria-labelledby="studio-rules" className="glass-strong">
        <CardHeader
          id="studio-rules"
          icon="scroll"
          title={`Rules of version ${full.version}`}
          subtitle={`${rules.length} ${rules.length === 1 ? "rule" : "rules"}. ${editable ? "Change a number, then check and save it." : "Read only."}`}
          aside={<StatusBadge status={full.review_status} />}
        />
        <div className="px-5 pb-1 text-sm text-ink-soft">
          <p>
            Effective from {fmtDate(full.effective_from)}. Source: {full.source_reference ? plain(full.source_reference) : "Not recorded"}.
          </p>
        </div>
        {stateText && (
          <div className="flex flex-wrap items-center gap-3 px-5 pt-3">
            <Pill tone="neutral" icon="lock">
              Locked
            </Pill>
            <p className="min-w-0 text-sm text-ink-soft">{stateText} Make an editable copy to try different numbers.</p>
            <GatedButton permission={AUTHOR} className={btnSecondary} onClick={makeCopy} disabled={copy.pending || full.review_status === "retired"}>
              <Icon name="layers" className="size-4" />
              {copy.pending ? "Making a copy" : "Make an editable copy"}
            </GatedButton>
          </div>
        )}
        {copy.error && (
          <div className="px-5 pt-3">
            <ErrorPanel error={copy.error} title="The copy was not made" />
          </div>
        )}
        {notice && (
          <p role="status" className="mx-5 mt-3 flex items-center gap-2 rounded-control border border-ok-line bg-ok-bg px-4 py-2.5 text-ok">
            <Icon name="check-circle" className="size-4" />
            <span>{notice}</span>
          </p>
        )}
        <div className="space-y-3 px-5 pt-4 pb-5">
          {rules.length === 0 ? (
            <p className="text-ink-muted">This version has no rules yet.</p>
          ) : (
            rules.map((r) => <RuleCard key={r.id} rule={r} selectors={selectors} editable={editable} busy={busy} onSave={(next) => saveRule(r, next)} />)
          )}
        </div>
      </Card>

      <DryRunSection key={versionId} guidelineId={guideline.id} versionId={versionId} versionNumber={full.version} hasRules={rules.length > 0} contentHash={full.content_hash ?? null} />

      <Card as="section" aria-labelledby="studio-submit" className="glass-strong">
        <CardHeader id="studio-submit" icon="clipboard-check" title="Send for review" subtitle="Approval stays with a clinical reviewer. Nothing changes for residents until a reviewer approves." />
        <div className="space-y-3 px-5 pb-5">
          {isWorking ? (
            <>
              <p className="max-w-[70ch] text-sm text-ink-soft">When the dry run looks right, submit this version for review. It is checked for source details, a population scope and valid rules first.</p>
              <GatedButton permission={AUTHOR} className={btnPrimary} onClick={submitForReview} disabled={submit.pending || rules.length === 0}>
                <Icon name="check" className="size-4" />
                {submit.pending ? "Submitting" : "Submit for review"}
              </GatedButton>
              {submit.error && (
                <div role="alert" className="rounded-control border border-danger-line bg-danger-bg px-4 py-3 text-sm text-danger">
                  <p className="font-semibold">{displayText(submit.error.message)}</p>
                  <Details error={submit.error} />
                </div>
              )}
            </>
          ) : (
            <p className="text-sm text-ink-soft">
              This version is {humanize(full.review_status).toLowerCase()}.{" "}
              <Link href={`/guidelines/${guideline.id}`} className="text-brand-ink underline underline-offset-2">
                Open the guideline to review or approve it
              </Link>
              .
            </p>
          )}
        </div>
      </Card>
    </div>
  );
}

// ---- page ---------------------------------------------------------------------------------------------------------------
export function GuidelineStudio() {
  const sp = useSearchParams();
  const can = useCan();
  const canAuthor = AUTHOR.some(can);
  const list = useApi("studio:guidelines", (signal) => apiList<Guideline>("/guidelines", { page_size: 100 }, signal));
  const [gidPick, setGid] = useState<string | null>(sp.get("guideline"));
  const [vidPick, setVid] = useState<string | null>(sp.get("version"));

  const guidelines = list.data?.data ?? [];
  const g = guidelines.find((x) => x.id === gidPick) ?? guidelines[0];
  const versions = g?.versions ?? [];
  const v = versions.find((x) => x.id === vidPick) ?? versions.find((x) => x.review_status === "draft") ?? versions[0];

  return (
    <>
      <PageHeader
        eyebrow="Guideline Studio"
        crumbs={["Guidelines", "Guideline Studio"]}
        title="Guideline Studio"
        description="Change a guideline and see who becomes due, in minutes, before anyone approves it. A dry run reads real records and saves nothing."
        actions={
          <Link href="/guidelines" className={btnSecondary}>
            All guidelines
          </Link>
        }
      />
      {!canAuthor && (
        <p role="note" className="mb-5 flex items-start gap-2 rounded-control border border-line bg-surface px-4 py-3 text-sm text-ink-soft">
          <Icon name="lock" className="mt-0.5 size-4 shrink-0" />
          <span>Read only access. You can read every rule and any dry run that has been run. Editing rules, running a dry run and submitting for review need a guideline author role.</span>
        </p>
      )}
      {list.error ? (
        <ErrorPanel error={list.error} onRetry={list.reload} title="Could not load the guidelines" />
      ) : list.loading && !list.data ? (
        <div className="skeleton h-32" role="status" aria-label="Loading guidelines" />
      ) : !g || !v ? (
        <EmptyState title="No guideline versions to show" icon="scroll">
          Guidelines and their versions appear here once they have been loaded.
        </EmptyState>
      ) : (
        <div className="space-y-5">
          <Card as="section" aria-labelledby="studio-pick" className="glass-strong">
            <CardHeader id="studio-pick" icon="scale" title="Choose a guideline version" subtitle="Pick any version. Only a version that has not been submitted can be edited." />
            <div className="flex flex-wrap items-end gap-x-4 gap-y-3 px-5 pb-5">
              <FilterField label="Guideline" className="min-w-64 flex-1">
                <select
                  aria-label="Guideline"
                  className={selectClass}
                  value={g.id}
                  onChange={(e) => {
                    setGid(e.target.value);
                    setVid(null);
                  }}
                >
                  {guidelines.map((x) => (
                    <option key={x.id} value={x.id}>
                      {plain(x.name)}
                    </option>
                  ))}
                </select>
              </FilterField>
              <FilterField label="Version" className="min-w-56">
                <select aria-label="Version" className={selectClass} value={v.id} onChange={(e) => setVid(e.target.value)}>
                  {versions.map((x) => (
                    <option key={x.id} value={x.id}>
                      Version {x.version}, {x.review_status === "draft" ? "not yet submitted" : humanize(x.review_status).toLowerCase()}
                    </option>
                  ))}
                </select>
              </FilterField>
            </div>
          </Card>
          <Workspace
            key={v.id}
            guideline={g}
            versionId={v.id}
            canAuthor={canAuthor}
            onListChanged={list.reload}
            onPick={(id) => {
              setVid(id);
            }}
          />
        </div>
      )}
    </>
  );
}
