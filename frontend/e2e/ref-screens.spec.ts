import { test, type Locator, type Page } from "@playwright/test";
import { boot } from "./helpers";
import { RES_ID } from "./fixtures";
import { REF_HANDLERS } from "./ref-fixtures";

// Reference-comparison screenshots: 1440x900 of each owned page against the owner's reference images.
// Output: test-results/screenshots/ref-<page>.png. Not an assertion suite; modules/smoke/screens/copy do that.
test.use({ viewport: { width: 1440, height: 900 } });

const PAGES: { name: string; path: string; ready: (p: Page) => Locator }[] = [
  { name: "command-center", path: "/", ready: (p) => p.getByRole("heading", { name: "Population Health Overview" }) },
  { name: "population", path: "/population", ready: (p) => p.getByRole("heading", { level: 1 }) },
  { name: "insights", path: "/insights", ready: (p) => p.getByRole("heading", { name: "Population Insights", exact: true }) },
  { name: "residents", path: "/residents", ready: (p) => p.getByRole("heading", { name: /Resident [0-9a-f]{8}/ }) },
  { name: "resident-detail", path: `/residents/${RES_ID}`, ready: (p) => p.getByRole("heading", { name: "Care gaps" }) },
  { name: "reports", path: "/reports", ready: (p) => p.getByRole("heading", { name: "Reports and Insights" }) },
];

for (const pg of PAGES) {
  test(`ref ${pg.name}`, async ({ page, context }) => {
    await boot(page, context, "population_admin", REF_HANDLERS);
    await page.goto(pg.path);
    await pg.ready(page).waitFor();
    await page.waitForTimeout(900);
    await page.screenshot({ path: `test-results/screenshots/ref-${pg.name}.png` });
    await page.screenshot({ path: `test-results/screenshots/ref-${pg.name}-full.png`, fullPage: true });
  });
}

// Theme modes: the Command Center in dark and clinical (saved choice is read from localStorage before first paint).
for (const mode of ["dark", "clinical"] as const) {
  test(`ref command-center ${mode}`, async ({ page, context }) => {
    await page.addInitScript((m) => window.localStorage.setItem("anticipa-theme", m), mode);
    await boot(page, context, "population_admin", REF_HANDLERS);
    await page.goto("/");
    await page.getByRole("heading", { name: "Population Health Overview" }).waitFor();
    await page.waitForTimeout(1200);
    await page.screenshot({ path: `test-results/screenshots/ref-command-center-${mode}.png` });
  });
}
