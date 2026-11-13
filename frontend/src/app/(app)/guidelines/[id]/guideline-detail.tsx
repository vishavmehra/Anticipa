"use client";

import { selectClass } from "@/components/ui";
import Link from "next/link";
import { useState } from "react";
import { api, apiList, apiPost } from "@/lib/api";
import { fmtDate, fmtDateTime, humanize } from "@/lib/format";
import { PERM } from "@/lib/nav";
import type { Guideline, GuidelineVersion, Rule } from "@/lib/types";
import { useAction } from "@/lib/use-action";
import { useApi } from "@/lib/use-api";
import { DataTable, type Column } from "@/components/data-table";
import { GatedButton } from "@/components/gated-button";
import { StatusBadge } from "@/components/status-badge";
import { ErrorPanel, Facts, PageHeader, Section, btnPrimary, btnSecondary } from "@/components/ui";
import { displayText } from "@/lib/display";

function ApproveCell({ guidelineId, version, onDone }: { guidelineId: string; version: GuidelineVersion; onDone: () => void }) {
  const [confirm, setConfirm] = useState(false);
  const act = useAction();
  if (version.review_status !== "in_review") return <span className="text-ink-muted">None</span>;
  return (
    <div className="flex flex-col items-start gap-1">
      {confirm ? (
        <div className="flex flex-wrap gap-2">
          <GatedButton
            permission={PERM.guidelinesApprove}
            className={btnPrimary}
            disabled={act.pending}
            onClick={async () => {
              if (await act.run(() => apiPost(`/guidelines/${guidelineId}/versions/${version.id}/approve`))) onDone();
            }}
          >
            Confirm approval
          </GatedButton>
          <button type="button" className={btnSecondary} onClick={() => setConfirm(false)}>
            Keep in review
          </button>
        </div>
      ) : (
        <GatedButton permission={PERM.guidelinesApprove} className={btnSecondary} onClick={() => setConfirm(true)}>
          Approve version
        </GatedButton>
      )}
      {act.error && <ErrorPanel error={act.error} title="Approval was not saved" />}
    </div>
  );
}

const ruleCols: Column<Rule>[] = [
  {
    key: "name",
    header: "Rule",
    cell: (r) => (
      <>
        <p className="font-medium">{r.name}</p>
        {r.description && <p className="text-xs text-ink-muted">{displayText(r.description)}</p>}
        <p className="font-mono text-xs text-ink-muted">{r.rule_key}</p>
      </>
    ),
  },
  { key: "act", header: "Care activity", cell: (r) => humanize(r.care_activity) },
  { key: "pri", header: "Priority", cell: (r) => humanize(r.priority) },
  { key: "rv", header: "Rule version", align: "right", cell: (r) => r.version },
  { key: "status", header: "State", cell: (r) => <StatusBadge status={r.approval_status} /> },
];

function Rules({ versionId }: { versionId: string }) {
  const rules = useApi(`rules:${versionId}`, (signal) => apiList<Rule>("/rules", { guideline_version_id: versionId, page_size: 200 }, signal));
  return (
    <DataTable
      caption="Rules"
      columns={ruleCols}
      rows={rules.data?.data ?? null}
      getRowId={(r) => r.id}
      loading={rules.loading}
      error={rules.error}
      onRetry={rules.reload}
      unavailable={{ endpoint: "GET /api/v1/rules" }}
      emptyTitle="No rules defined for this version"
      emptyHint="Rules are deterministic, versioned code. They are what decides eligibility."
    />
  );
}

export function GuidelineDetailView({ id }: { id: string }) {
  const g = useApi(`guideline:${id}`, (signal) => api<Guideline>(`/guidelines/${id}`, { signal }));
  const [picked, setPicked] = useState<string | null>(null);
  const back = (
    <p className="mb-2">
      <Link href="/guidelines" className="text-brand-ink underline underline-offset-2">
        All guidelines
      </Link>
    </p>
  );

  if (g.error) {
    return (
      <>
        {back}
        <PageHeader title="Guideline" />
        <ErrorPanel error={g.error} onRetry={g.reload} title="Could not load this guideline" />
      </>
    );
  }
  const d = g.data?.data;
  if (!d) return <p role="status" className="text-ink-muted">Loading guideline…</p>;

  const versions = d.versions ?? [];
  const versionId = picked ?? versions[0]?.id ?? null;
  const versionCols: Column<GuidelineVersion>[] = [
    { key: "v", header: "Version", align: "right", cell: (v) => <span className="font-medium">v{v.version}</span> },
    { key: "s", header: "Review state", cell: (v) => <StatusBadge status={v.review_status} /> },
    { key: "clin", header: "Clinical review", cell: (v) => humanize(v.clinical_review_status) },
    { key: "src", header: "Source reference", cell: (v) => v.source_reference ?? <span className="text-ink-muted">Not recorded</span> },
    { key: "pub", header: "Published", className: "whitespace-nowrap tnum", cell: (v) => fmtDate(v.source_publication_date) },
    { key: "eff", header: "Effective from", className: "whitespace-nowrap tnum", cell: (v) => fmtDate(v.effective_from) },
    { key: "appr", header: "Approved", className: "whitespace-nowrap tnum", cell: (v) => (v.approved_at ? fmtDateTime(v.approved_at) : "None") },
    { key: "act", header: "Action", cell: (v) => <ApproveCell guidelineId={id} version={v} onDone={g.reload} /> },
  ];

  return (
    <>
      {back}
      <PageHeader title={d.name} description={d.description ? displayText(d.description) : undefined} />
      <Section id="about" title="About">
        <Facts
          items={[
            ["Topic", d.topic],
            ["Authoritative source", d.authoritative_source],
            ["Source link", d.source_url ? (/^https?:\/\//i.test(d.source_url) ? <a key="u" href={d.source_url} rel="noreferrer noopener" target="_blank" className="text-brand-ink underline underline-offset-2 break-all">{d.source_url}</a> : <span key="u" className="break-all">{d.source_url}</span>) : null],
            ["Key", <span key="k" className="font-mono text-xs">{d.guideline_key}</span>],
            ["Updated", fmtDateTime(d.updated_at)],
          ]}
        />
      </Section>
      <Section id="versions" title="Versions">
        <DataTable
          caption="Guideline versions"
          columns={versionCols}
          rows={versions}
          getRowId={(v) => v.id}
          loading={false}
          error={null}
          emptyTitle="No versions yet"
          emptyHint="A version records the source reference and criteria. It must be approved before its rules can raise care gaps."
        />
        {versions.some((v) => v.disclaimer) && <p className="mt-2 max-w-[65ch] text-ink-muted">{displayText(versions.find((v) => v.disclaimer)?.disclaimer)}</p>}
      </Section>
      {versionId && (
        <Section
          id="rules"
          title="Rules"
          aside={
            versions.length > 1 ? (
              <span className="flex items-center gap-2">
                <label htmlFor="rule-version" className="font-medium">
                  Version
                </label>
                <select
                  id="rule-version"
                  value={versionId}
                  onChange={(e) => setPicked(e.target.value)}
                  className={selectClass}
                >
                  {versions.map((v) => (
                    <option key={v.id} value={v.id}>
                      v{v.version} ({humanize(v.review_status)})
                    </option>
                  ))}
                </select>
              </span>
            ) : undefined
          }
        >
          <Rules key={versionId} versionId={versionId} />
        </Section>
      )}
    </>
  );
}
