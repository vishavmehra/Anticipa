import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import { bootB, expectNoDashes } from "./features-fixtures";
import { commandCenter, engagementOverview } from "./features-fixtures-ref";
import { capacityOverview } from "./features-fixtures-ref2";
import { ok, paged } from "./helpers";

// Operations and capacity (reference: Anticipa Operations Capacity Dashboard), route /operations.
const day = new Date(Date.now() + 86400000).toISOString().slice(0, 10);
const clinic = (n: number, district: string, status = "open") => ({
  id: `cccccccc-0000-4000-8000-00000000c0${n}`,
  clinic_key: `clinic-${n}`,
  name: `Clinic ${n}`,
  district_code: district,
  address: null,
  timezone: "UTC",
  operating_status: status,
  synthetic_status: "simulated",
  created_at: "2026-11-01T00:00:00Z",
  services: [{ id: `s${n}`, service_key: "mammography", service_name: "Mammography", appointment_duration_minutes: 30, active: true }],
});
const clinics = [clinic(1, "PUN"), clinic(2, "PUN"), clinic(3, "MUM"), clinic(4, "NAG", "closed")];
const slot = (n: number, svc: string, h: number, reserved: number, avail: number) => ({
  id: `slot-${n}`, clinic_id: "c1", clinic_key: "clinic-1", clinic_service_id: "s1", service_key: svc,
  start_time: `${day}T${String(h).padStart(2, "0")}:00:00Z`, end_time: `${day}T${String(h).padStart(2, "0")}:30:00Z`,
  available_capacity: avail, reserved_capacity: reserved, remaining_capacity: avail - reserved, slot_status: "open", source_type: "manual", synthetic_status: "simulated",
});
const slots = [slot(1, "mammography", 9, 24, 30), slot(2, "cervical_screening", 10, 26, 30), slot(3, "mammography", 11, 5, 30)];

const handlers = () => ({
  "GET /planning/clinics": () => ({ body: paged(clinics, 1, 100, clinics.length) }),
  "GET /planning/capacity-slots": () => ({ body: paged(slots, 1, 100, slots.length) }),
  "GET /analytics/capacity": () => ({ body: ok(capacityOverview()) }),
  "GET /analytics/engagement": () => ({ body: ok(engagementOverview()) }),
  "GET /analytics/command-center": () => ({ body: ok(commandCenter()) }),
});

test("operations: network, real slot schedule, capacity figures, rebalancing advice and alerts", async ({ page, context }) => {
  await bootB(page, context, "observer", handlers());
  await page.goto("/operations");
  await expect(page.getByRole("heading", { level: 1, name: "Operations and capacity" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Care network" })).toContainText("4 clinics in 3 districts, 3 open");
  await expect(page.getByRole("list", { name: "Districts and seat use" })).toContainText("PUN: 95% of seats booked");
  const day_ = page.getByRole("region", { name: "Day schedule" });
  await expect(day_.locator('[data-slot="slot-1"]')).toContainText("24 / 30 booked");
  await expect(day_.locator('[data-slot="slot-2"]')).toContainText("Cervical screening");
  await page.getByRole("button", { name: "Week", exact: true }).click();
  await expect(page.getByRole("table", { name: "Seats by day for this clinic" })).toContainText("55");
  await page.getByRole("button", { name: "Day", exact: true }).click();
  const kpis = page.getByRole("region", { name: "Capacity figures" });
  await expect(kpis).toContainText("1,500"); // completed appointments
  await expect(kpis).toContainText("80%");
  await expect(kpis).toContainText("2,000");
  await expect(kpis).toContainText("Not available"); // median time to attend has no source
  await expect(page.getByRole("list", { name: "Seat use by district" })).toContainText("MUM");
  await expect(page.getByRole("region", { name: "Rebalance appointments" })).toContainText("1,800 open seats");
  await expect(page.getByRole("region", { name: /Operational alerts \(1\)/ })).toContainText("Capacity nearly full");
  await expect(page.getByText(/synthetic/i)).toHaveCount(0);
  await expectNoDashes(page);
  await page.screenshot({ path: "test-results/screenshots/ref-operations.png" });
  await page.screenshot({ path: "test-results/screenshots/ref-operations-full.png", fullPage: true });
});

test("tabs: resource view lists clinics, rebalancing and network stand alone", async ({ page, context }) => {
  await bootB(page, context, "observer", handlers());
  await page.goto("/operations");
  await page.getByRole("tab", { name: "Resource view" }).click();
  await expect(page.getByRole("table", { name: "Clinics" }).getByRole("row")).toHaveCount(5);
  await page.getByRole("tab", { name: "Rebalancing" }).click();
  await expect(page.getByRole("region", { name: "Rebalance appointments" })).toBeVisible();
  await page.getByRole("tab", { name: "Site network" }).click();
  await expect(page.getByRole("region", { name: "Care network" })).toBeVisible();
});

test("a clinic without slots says so", async ({ page, context }) => {
  await bootB(page, context, "observer", { ...handlers(), "GET /planning/capacity-slots": () => ({ body: paged([], 1, 100, 0) }) });
  await page.goto("/operations");
  await expect(page.getByText("This clinic has no capacity slots in the next 14 days.")).toBeVisible();
});

for (const w of [1440, 390]) {
  test(`operations at ${w}px: axe and no horizontal scroll`, async ({ page, context }) => {
    await page.setViewportSize({ width: w, height: w === 1440 ? 900 : 844 });
    await bootB(page, context, "observer", handlers());
    await page.goto("/operations");
    await expect(page.getByRole("region", { name: "Day schedule" })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(w);
    const axe = await new AxeBuilder({ page }).include("#main").withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]).analyze();
    expect(axe.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`)).toEqual([]);
  });
}

test("selecting a district filters the network, the schedule clinics and the figures", async ({ page, context }) => {
  await bootB(page, context, "observer", handlers());
  await page.goto("/operations");
  await expect(page.getByLabel("Clinic", { exact: true }).locator("option")).toHaveCount(4);
  await page.getByRole("list", { name: "Seat use by district" }).getByRole("button", { name: /MUM/ }).click();
  await expect(page.getByText("Showing district")).toBeVisible();
  await expect(page.getByLabel("Clinic", { exact: true }).locator("option")).toHaveCount(1); // only the MUM clinic
  await page.getByRole("button", { name: "Show all districts" }).click();
  await expect(page.getByLabel("Clinic", { exact: true }).locator("option")).toHaveCount(4);
  // The map labels are controls too.
  await page.getByRole("group", { name: /Care network map/ }).getByRole("button", { name: /PUN 95%/ }).click();
  await expect(page.getByLabel("Clinic", { exact: true }).locator("option")).toHaveCount(2);
});
