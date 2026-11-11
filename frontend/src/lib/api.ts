// Typed client for the contract envelope (Docs/17 §2). Browser calls go same-origin through the Next.js
// rewrite to FastAPI (CORS-free); auth is the Supabase access token sent as a Bearer header. The backend
// verifies the JWT and is the source of truth for role and permissions. No provider keys here (ADR-036).

import { createClient } from "./supabase/client";

export interface Meta {
  request_id: string;
  page?: number;
  page_size?: number;
  total?: number;
  total_pages?: number;
}
export interface Envelope<T> {
  data: T;
  meta: Meta;
}
export interface ListEnvelope<T> extends Envelope<T[]> {
  meta: Required<Meta>;
}

export type ApiErrorCode =
  | "VALIDATION_ERROR"
  | "UNAUTHENTICATED"
  | "FORBIDDEN"
  | "NOT_FOUND"
  | "CONFLICT"
  | "INVALID_TRANSITION"
  | "RATE_LIMITED"
  | "PROVIDER_UNAVAILABLE"
  | "INTERNAL_ERROR"
  | "NETWORK_ERROR"; // client-side only: backend unreachable / non-JSON reply

export interface ErrorDetail {
  field?: string;
  reason?: string;
  [k: string]: unknown;
}

export class ApiError extends Error {
  constructor(
    readonly code: ApiErrorCode,
    message: string,
    readonly status: number,
    readonly details: ErrorDetail[] | null = null,
    readonly requestId: string | null = null,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

type Query = Record<string, string | number | boolean | null | undefined>;
export interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  query?: Query;
  body?: unknown;
  signal?: AbortSignal;
}

export const API_BASE = "/api/v1";

export function toQuery(q?: Query): string {
  if (!q) return "";
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(q)) if (v !== undefined && v !== null && v !== "") p.set(k, String(v));
  const s = p.toString();
  return s ? `?${s}` : "";
}

/**
 * One authenticated fetch + envelope parse, shared by the browser and server clients.
 * Throws ApiError for contract errors, non-envelope failures and network errors.
 */
export async function send<T>(url: string, token: string, opts: RequestOptions): Promise<Envelope<T>> {
  const headers: Record<string, string> = { Accept: "application/json", Authorization: `Bearer ${token}` };
  if (opts.body !== undefined) headers["Content-Type"] = "application/json";

  let res: Response;
  try {
    res = await fetch(url, {
      method: opts.method ?? "GET",
      headers,
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
      signal: opts.signal,
      cache: "no-store",
    });
  } catch (e) {
    if (e instanceof DOMException && e.name === "AbortError") throw e;
    throw new ApiError("NETWORK_ERROR", "The API could not be reached.", 0);
  }

  let json: unknown = null;
  try {
    json = await res.json();
  } catch {
    /* non-JSON body (e.g. proxy error page) handled below */
  }

  if (!res.ok) {
    const err = (json as { error?: { code?: ApiErrorCode; message?: string; details?: ErrorDetail[]; request_id?: string } } | null)
      ?.error;
    if (err?.code) {
      throw new ApiError(err.code, err.message ?? "Request failed.", res.status, err.details ?? null, err.request_id ?? null);
    }
    // No contract envelope: usually the Next.js rewrite failing to reach FastAPI.
    throw new ApiError(
      res.status === 401 ? "UNAUTHENTICATED" : res.status === 403 ? "FORBIDDEN" : res.status >= 500 ? "NETWORK_ERROR" : "INTERNAL_ERROR",
      res.status >= 500 ? `The API is not responding (HTTP ${res.status}).` : `Unexpected response (HTTP ${res.status}).`,
      res.status,
    );
  }
  if (!json || typeof json !== "object" || !("data" in json)) {
    throw new ApiError("INTERNAL_ERROR", "The API returned an unexpected response shape.", res.status);
  }
  return json as Envelope<T>;
}

function redirectToLogin() {
  if (typeof window === "undefined" || window.location.pathname.startsWith("/login")) return;
  const next = window.location.pathname + window.location.search;
  // Hard navigation on purpose: discards in-memory state and the router cache of the dead session.
  // eslint-disable-next-line @next/next/no-location-assign-relative-destination
  window.location.assign(`/login?next=${encodeURIComponent(next)}`);
}

const unauthenticated = () => new ApiError("UNAUTHENTICATED", "Your session has ended. Sign in again.", 401);

/** Browser client: Bearer token from the Supabase session; on 401 refresh once, then go to /login. */
export async function api<T>(path: string, opts: RequestOptions = {}): Promise<Envelope<T>> {
  const url = `${API_BASE}${path}${toQuery(opts.query)}`;
  const supabase = createClient();

  const { data: current } = await supabase.auth.getSession();
  if (!current.session) {
    redirectToLogin();
    throw unauthenticated();
  }
  try {
    return await send<T>(url, current.session.access_token, opts);
  } catch (e) {
    if (!(e instanceof ApiError) || e.status !== 401) throw e;
  }

  const { data: refreshed } = await supabase.auth.refreshSession();
  if (refreshed.session) {
    try {
      return await send<T>(url, refreshed.session.access_token, opts);
    } catch (e) {
      if (!(e instanceof ApiError) || e.status !== 401) throw e;
    }
  }
  await supabase.auth.signOut({ scope: "local" }); // drop the dead session so /login is not bounced back
  redirectToLogin();
  throw unauthenticated();
}

export const apiList = <T>(path: string, query?: Query, signal?: AbortSignal) =>
  api<T[]>(path, { query, signal }) as Promise<ListEnvelope<T>>;

export const apiPost = <T>(path: string, body?: unknown) => api<T>(path, { method: "POST", body });

/**
 * True when a *collection or aggregate* route is not in the backend build (FastAPI answers an unknown path with the
 * contract 404 NOT_FOUND). Callers use it to show an honest "not yet available" state instead of an error. Do not use it
 * for detail routes, where 404 means "that record does not exist".
 */
export const isMissingEndpoint = (e: ApiError | null | undefined): boolean =>
  !!e && (e.status === 404 || e.status === 405 || e.status === 501);
