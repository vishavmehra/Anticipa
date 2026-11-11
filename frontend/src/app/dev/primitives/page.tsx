"use client";

import { notFound } from "next/navigation";
import { SimulatedBadge, SyntheticBadge } from "@/components/badges";
import { DataTable, type Column } from "@/components/data-table";
import { EvidenceQuote } from "@/components/evidence-quote";
import { StatTile } from "@/components/stat-tile";
import { StatusBadge } from "@/components/status-badge";
import { Timeline } from "@/components/timeline";
import { ApiError } from "@/lib/api";
import { JOB_STATUSES } from "@/lib/types";

// Layout/visual-test page for the primitives. Development only: 404s in production builds.
// Sample sentences are invented demo text, not patient data and not metrics.

/** Code-point offsets of `quote` inside `text` (the unit the backend uses). */
function locate(text: string, quote: string) {
  const i = text.indexOf(quote);
  const start = Array.from(text.slice(0, i)).length;
  return { start, end: start + Array.from(quote).length };
}

const SAMPLES = [
  { lang: "en", label: "English", text: "Family history: mother diagnosed with breast cancer at age 46. Father has no history of colorectal cancer.", quote: "mother diagnosed with breast cancer at age 46" },
  { lang: "ar", label: "Arabic (right to left)", text: "التاريخ العائلي: أصيبت الأم بسرطان الثدي في عمر 46 سنة. لا يوجد تاريخ مرضي للأب.", quote: "أصيبت الأم بسرطان الثدي في عمر 46 سنة" },
  { lang: "hi", label: "Hindi (Devanagari)", text: "पारिवारिक इतिहास: माँ को 46 वर्ष की आयु में स्तन कैंसर का निदान हुआ था। पिता में कोई इतिहास नहीं है।", quote: "माँ को 46 वर्ष की आयु में स्तन कैंसर का निदान हुआ था" },
];

interface Row {
  id: string;
  name: string;
  count: number;
}
const cols: Column<Row>[] = [
  { key: "name", header: "Name", cell: (r) => r.name },
  { key: "count", header: "Items", align: "right", cell: (r) => r.count },
];
const rows: Row[] = [
  { id: "a", name: "Sample row one", count: 12 },
  { id: "b", name: "Sample row two", count: 4081 },
];

export default function PrimitivesPage() {
  if (process.env.NODE_ENV === "production") notFound();
  return (
    <main className="mx-auto max-w-4xl space-y-8 px-4 py-6">
      <h1 className="text-2xl font-semibold tracking-tight">Primitives</h1>

      <section aria-labelledby="p-badges" className="space-y-2">
        <h2 id="p-badges" className="text-base font-semibold">Labels and status</h2>
        <p className="flex flex-wrap gap-2"><SyntheticBadge /><SimulatedBadge /></p>
        <p className="flex flex-wrap gap-2">
          {JOB_STATUSES.map((s) => <StatusBadge key={s} status={s} />)}
          <StatusBadge status="healthy" /><StatusBadge status="authentication_failed" /><StatusBadge status="something_new" />
        </p>
      </section>

      <section aria-labelledby="p-tiles" className="space-y-2">
        <h2 id="p-tiles" className="text-base font-semibold">Stat tiles</h2>
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
          <StatTile label="Measured value" value="1,204" basis="numerator and denominator go here" />
          <StatTile label="No measurement" value={null} basis="endpoint unavailable" />
          <StatTile label="Loading" value={null} loading />
        </div>
      </section>

      <section aria-labelledby="p-quotes" className="space-y-3">
        <h2 id="p-quotes" className="text-base font-semibold">Evidence quote <SyntheticBadge /></h2>
        {SAMPLES.map((s) => {
          const { start, end } = locate(s.text, s.quote);
          return (
            <div key={s.lang} data-sample={s.lang}>
              <p className="mb-1 text-ink-muted">{s.label}</p>
              <EvidenceQuote text={s.text} start={start} end={end} quote={s.quote} lang={s.lang} context={40} />
            </div>
          );
        })}
        <div data-sample="mismatch">
          <p className="mb-1 text-ink-muted">Offsets that do not match the stored quote</p>
          <EvidenceQuote text={SAMPLES[0].text} start={0} end={6} quote="mother" lang="en" />
        </div>
        <div data-sample="out-of-range">
          <p className="mb-1 text-ink-muted">Offsets outside the document</p>
          <EvidenceQuote text="Short text." start={5} end={500} quote="missing" lang="en" />
        </div>
      </section>

      <section aria-labelledby="p-tl" className="space-y-2">
        <h2 id="p-tl" className="text-base font-semibold">Timeline</h2>
        <Timeline
          label="Example timeline"
          items={[
            { id: "1", at: "2026-11-05T09:00:00Z", title: "Created" },
            { id: "2", at: "2026-11-05T09:00:04Z", title: "Started", tone: "info" },
            { id: "3", at: "2026-11-05T09:14:31Z", title: "Completed", detail: "All items processed.", tone: "ok" },
          ]}
        />
      </section>

      <section aria-labelledby="p-tables" className="space-y-3">
        <h2 id="p-tables" className="text-base font-semibold">Data table states</h2>
        <DataTable caption="Rows" columns={cols} rows={rows} getRowId={(r) => r.id} loading={false} error={null} page={1} pageSize={2} total={4083} totalPages={2042} onPageChange={() => {}} />
        <DataTable caption="Loading" columns={cols} rows={null} getRowId={(r) => r.id} loading error={null} />
        <DataTable caption="Empty" columns={cols} rows={[]} getRowId={(r: Row) => r.id} loading={false} error={null} emptyTitle="No rows" emptyHint="Rows appear here when data exists." />
        <DataTable caption="Error" columns={cols} rows={null} getRowId={(r: Row) => r.id} loading={false} error={new ApiError("FORBIDDEN", "You do not have permission.", 403, null, "req-123")} />
      </section>
    </main>
  );
}
