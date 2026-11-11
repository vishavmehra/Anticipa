import type { BrowserContext, Page, Request, Route } from "@playwright/test";

export const SUPABASE = "http://localhost:54321";
export const COOKIE_NAME = "sb-localhost-auth-token"; // sb-<first hostname label>-auth-token

// Permission sets copied from backend/anticipa/core/rbac.py (+ observer per ADR-060: every *.view, no writes).
export const PERMS = {
  population_admin: [
    "analytics.view", "residents.view", "records.view", "documents.view", "evidence.view", "guidelines.view", "rules.view",
    "engagement.view", "processing.execute",
    "jobs.view", "capacity.view", "capacity.manage", "caregaps.view", "caregaps.manage", "caregaps.close",
    "audit.view", "integrations.view",
  ],
  care_coordinator: [
    "analytics.view_limited", "residents.view", "guidelines.view", "capacity.view", "caregaps.view",
    "caregaps.manage", "caregaps.close", "audit.view_scoped", "integrations.view",
  ],
  clinical_reviewer: [
    "analytics.view", "residents.view", "records.view", "documents.view", "evidence.view", "guidelines.view",
    "rules.view", "caregaps.view", "closure.view", "integrations.view", "audit.view_scoped",
    "evidence.review", "guidelines.approve", "caregaps.review",
  ],
  // Mirrors ROLE_PERMISSIONS[OBSERVER] exactly: _ADMIN_VIEWS + analytics.view + audit.view_redacted.
  observer: [
    "analytics.view", "residents.view", "records.view", "documents.view", "evidence.view", "guidelines.view",
    "rules.view", "caregaps.view", "capacity.view", "engagement.view", "appointments.view", "closure.view",
    "jobs.view", "integrations.view", "audit.view_redacted",
  ],
} as const;

export type TestRole = keyof typeof PERMS;

export const meFor = (role: TestRole) => ({
  id: "11111111-1111-4111-8111-111111111111",
  email: `${role}@example.test`,
  display_name: null,
  role,
  permissions: [...PERMS[role]],
});

const b64url = (s: string) => Buffer.from(s).toString("base64url");
const jwt = (marker: string) =>
  `${b64url('{"alg":"HS256","typ":"JWT"}')}.${b64url(JSON.stringify({ sub: "u1", marker, exp: 4102444800 }))}.sig`;

export function sessionFor(role: TestRole, marker = "access-1") {
  return {
    access_token: jwt(marker),
    token_type: "bearer",
    expires_in: 3600,
    expires_at: Math.floor(Date.now() / 1000) + 3600,
    refresh_token: `refresh-${marker}`,
    user: {
      id: "11111111-1111-4111-8111-111111111111",
      aud: "authenticated",
      role: "authenticated",
      email: `${role}@example.test`,
      app_metadata: {},
      user_metadata: {},
      created_at: "2026-01-01T00:00:00Z",
    },
  };
}

/** Pretend the browser already signed in: writes the cookie @supabase/ssr reads (base64url encoding). */
export async function signIn(context: BrowserContext, role: TestRole, marker = "access-1") {
  const value = `base64-${Buffer.from(JSON.stringify(sessionFor(role, marker))).toString("base64url")}`;
  await context.addCookies([{ name: COOKIE_NAME, value, url: "http://localhost:3100" }]);
}

const CORS = {
  "access-control-allow-origin": "*",
  "access-control-allow-headers": "*",
  "access-control-allow-methods": "GET,POST,PUT,DELETE,OPTIONS",
};
const json = (route: Route, status: number, body: unknown, headers: Record<string, string> = {}) =>
  route.fulfill({ status, contentType: "application/json", headers: { ...CORS, ...headers }, body: JSON.stringify(body) });

export interface SupabaseMock {
  calls: { passwordBodies: unknown[]; refreshes: number; logouts: number };
}

/** Mocks the Supabase Auth endpoints the app calls. `refresh` decides what a refresh_token grant returns. */
export async function mockSupabase(
  page: Page,
  opts: { role?: TestRole; refresh?: "ok" | "fail"; passwordOk?: boolean } = {},
): Promise<SupabaseMock> {
  const role = opts.role ?? "population_admin";
  const calls: SupabaseMock["calls"] = { passwordBodies: [], refreshes: 0, logouts: 0 };
  await page.route(`${SUPABASE}/auth/v1/**`, async (route) => {
    const req = route.request();
    if (req.method() === "OPTIONS") return route.fulfill({ status: 204, headers: CORS });
    const url = new URL(req.url());
    if (url.pathname.endsWith("/token")) {
      const grant = url.searchParams.get("grant_type");
      if (grant === "password") {
        calls.passwordBodies.push(req.postDataJSON());
        if (opts.passwordOk === false)
          return json(route, 400, { code: 400, error_code: "invalid_credentials", msg: "Invalid login credentials" });
        return json(route, 200, sessionFor(role, "access-login"));
      }
      if (grant === "refresh_token") {
        calls.refreshes++;
        if (opts.refresh === "fail")
          return json(route, 400, { code: 400, error_code: "refresh_token_not_found", msg: "Invalid Refresh Token" });
        return json(route, 200, sessionFor(role, "access-2"));
      }
    }
    if (url.pathname.endsWith("/logout")) {
      calls.logouts++;
      return route.fulfill({ status: 204, headers: CORS });
    }
    if (url.pathname.endsWith("/user")) return json(route, 200, sessionFor(role).user);
    return json(route, 404, { msg: "not mocked" });
  });
  return { calls };
}

export const REQ_ID = "req-test";
export const ok = <T>(data: T) => ({ data, meta: { request_id: REQ_ID } });
export const paged = <T>(rows: T[], page = 1, pageSize = 25, total = rows.length) => ({
  data: rows,
  meta: { request_id: REQ_ID, page, page_size: pageSize, total, total_pages: total ? Math.ceil(total / pageSize) : 0 },
});
export const errorBody = (code: string, message: string, status = 400) => ({
  status,
  body: { error: { code, message, details: [], request_id: REQ_ID } },
});

export type ApiHandler = (req: Request, url: URL) => { status?: number; body: unknown } | Promise<{ status?: number; body: unknown }>;

/** Routes /api/v1/* by "METHOD /path". Anything unmocked answers 501 and is recorded in `missed`. */
export async function mockApi(page: Page, handlers: Record<string, ApiHandler>) {
  const seen: { method: string; path: string; headers: Record<string, string> }[] = [];
  const missed: string[] = [];
  await page.route("**/api/v1/**", async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    const path = url.pathname.replace(/^\/api\/v1/, "");
    seen.push({ method: req.method(), path, headers: req.headers() });
    const handler = handlers[`${req.method()} ${path}`];
    if (!handler) {
      missed.push(`${req.method()} ${path}`);
      return json(route, 501, { error: { code: "INTERNAL_ERROR", message: "not mocked", details: [], request_id: REQ_ID } });
    }
    const res = await handler(req, url);
    return json(route, res.status ?? 200, res.body);
  });
  return { seen, missed };
}

export const job = (over: Record<string, unknown> = {}) => ({
  id: "aaaaaaaa-0000-4000-8000-000000000001",
  job_type: "datafabric.ingest_batch",
  status: "completed",
  config_version: "v1",
  total_items: 100,
  processed_items: 100,
  succeeded_items: 100,
  failed_items: 0,
  retry_count: 0,
  max_retries: 3,
  error_summary: null,
  cancel_requested: false,
  created_by: null,
  created_at: "2026-11-05T09:00:00Z",
  started_at: "2026-11-05T09:00:04Z",
  updated_at: "2026-11-05T09:14:31Z",
  completed_at: "2026-11-05T09:14:31Z",
  checkpoint: null,
  ...over,
});

/** Signed-in browser with mocked Supabase + API. Extra handlers override the defaults. */
export async function boot(page: Page, context: BrowserContext, role: TestRole, handlers: Record<string, ApiHandler> = {}) {
  const supabase = await mockSupabase(page, { role });
  await signIn(context, role);
  const api = await mockApi(page, { "GET /auth/me": () => ({ body: ok(meFor(role)) }), ...handlers });
  return { supabase, api };
}

/** role=alert elements with content. Next.js always mounts an empty role=alert route announcer; ignore it. */
export const alerts = (page: Page) => page.getByRole("alert").filter({ hasText: /\S/ });
