"use client";

import Link from "next/link";
import { useState } from "react";
import { api, apiList, isMissingEndpoint } from "@/lib/api";
import { LANGUAGES, fmtDateTime, fmtInt, humanize } from "@/lib/format";
import type { Appointment, EngagementOverview } from "@/lib/types";
import { useApi } from "@/lib/use-api";
import { SimulatedBadge } from "@/components/badges";
import { DataTable, type Column } from "@/components/data-table";
import { StatTile } from "@/components/stat-tile";
import { StatusBadge } from "@/components/status-badge";
import { Card, DataLabel, ErrorPanel, PageHeader, Section, Unavailable, btnSecondary, type IconName } from "@/components/ui";
import { displayText } from "@/lib/display";

const FUNNEL: [keyof EngagementOverview["funnel"], string, string, IconName, "teal" | "blue" | "green" | "violet" | "amber" | "rose" | "white"][] = [
  ["cases_contacted", "Contacted", "Cases with an outreach message", "megaphone", "teal"],
  ["cases_delivered", "Delivered", "Cases with a delivered message", "mail", "blue"],
  ["cases_accepted", "Accepted", "Cases where the resident accepted", "hand-heart", "green"],
  ["cases_with_appointment", "Booked", "Cases with a confirmed appointment", "calendar-check", "violet"],
  ["cases_attended", "Attended", "Cases where the appointment was completed", "user-check", "amber"],
  ["cases_with_verified_evidence", "Evidence verified", "Cases with verified completion evidence", "shield", "teal"],
  ["cases_completed", "Completed", "Cases closed as completed", "check-circle", "green"],
];

function Counts({ title, data, labels, badge }: { title: string; data: Record<string, number>; labels?: Record<string, string>; badge?: React.ReactNode }) {
  const rows = Object.entries(data);
  return (
    <Card>
      <table className="w-full border-collapse text-left">
        <caption className="flex items-center justify-between gap-2 px-5 py-3.5 text-left font-semibold">
          {title}
          {badge}
        </caption>
        <tbody>
          {rows.map(([k, n]) => (
            <tr key={k} className="border-t border-line">
              <th scope="row" className="px-5 py-2 text-left font-normal text-ink-soft">
                {labels?.[k] ?? humanize(k)}
              </th>
              <td className="tnum px-5 py-2 text-right font-semibold">{fmtInt(n)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Card>
  );
}

const apptCols: Column<Appointment>[] = [
  { key: "when", header: "Slot", className: "whitespace-nowrap tnum", cell: (a) => fmtDateTime(a.start_time) },
  {
    key: "case",
    header: "Case",
    cell: (a) => (
      <Link href={`/care-gaps/${a.care_gap_case_id}`} className="font-mono text-xs text-brand-ink underline underline-offset-2">
        {a.care_gap_case_id.slice(0, 8)}
      </Link>
    ),
  },
  { key: "where", header: "Clinic and service", cell: (a) => [a.clinic_key, a.service_key ? humanize(a.service_key) : null].filter(Boolean).join(", ") || "None" },
  {
    key: "status",
    header: "Status",
    cell: (a) => (
      <span className="inline-flex flex-wrap items-center gap-1.5">
        <StatusBadge status={a.appointment_status} />
        <SimulatedBadge>Simulated booking</SimulatedBadge>
      </span>
    ),
  },
];

export default function EngagementPage() {
  const [page, setPage] = useState(1);
  const overview = useApi("engagement-overview", (signal) => api<EngagementOverview>("/analytics/engagement", { signal }));
  const appts = useApi(`appointments:${page}`, (signal) => apiList<Appointment>("/appointments", { page, page_size: 25 }, signal));
  const o = overview.data?.data;
  const meta = appts.data?.meta;

  return (
    <>
      <PageHeader
        title="Outreach"
        description="Outreach to residents, their responses and the appointments that follow, tracked until the care is done. No message is sent to a real person and no real booking is made."
        actions={
          <button
            type="button"
            className={btnSecondary}
            onClick={() => {
              overview.reload();
              appts.reload();
            }}
          >
            Refresh
          </button>
        }
      />
      <Section id="funnel" title="From message to completed care" aside={<DataLabel kind="live" />}>
        {overview.error ? (
          isMissingEndpoint(overview.error) ? (
            <Unavailable endpoint="GET /api/v1/analytics/engagement" />
          ) : (
            <ErrorPanel error={overview.error} onRetry={overview.reload} />
          )
        ) : (
          <>
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
              {FUNNEL.map(([k, label, basis, icon, tint]) => (
                <StatTile key={k} loading={overview.loading && !o} icon={icon} tint={tint} label={label} value={o ? fmtInt(o.funnel[k]) : null} basis={basis} />
              ))}
            </div>
            {o && <p className="mt-2 max-w-[75ch] text-ink-muted">{displayText(o.note)}</p>}
            {o && (
              <div className="mt-5 grid gap-4 md:grid-cols-2 xl:grid-cols-3 [&>*]:min-w-0">
                <Counts title="Messages by delivery state" data={o.communications.by_delivery_state} badge={<SimulatedBadge>Simulated delivery</SimulatedBadge>} />
                <Counts title="Messages by channel" data={o.communications.by_channel} />
                <Counts title="Messages by language" data={o.communications.by_language} labels={LANGUAGES} />
                <Counts title="Resident responses" data={o.responses.by_type} />
                <Counts title="Appointments by status" data={o.appointments.by_status} />
                <Counts title="Completion evidence" data={o.completion_evidence.by_verification_status} />
              </div>
            )}
          </>
        )}
      </Section>
      <Section id="appointments" title="Appointments" aside={<SimulatedBadge>Simulated booking</SimulatedBadge>}>
        <DataTable
          caption="Appointments"
          columns={apptCols}
          rows={appts.data?.data ?? null}
          getRowId={(a) => a.id}
          loading={appts.loading}
          error={appts.error}
          onRetry={appts.reload}
          unavailable={{ endpoint: "GET /api/v1/appointments" }}
          emptyTitle="No appointments yet"
          emptyHint="Confirming an allocation in Planning creates a simulated appointment that holds the slot."
          page={meta?.page ?? page}
          pageSize={meta?.page_size ?? 25}
          total={meta?.total}
          totalPages={meta?.total_pages}
          onPageChange={setPage}
        />
      </Section>
    </>
  );
}
