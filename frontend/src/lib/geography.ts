"use client";

import { api } from "./api";
import type { GeoArea, GeoResponse } from "./module-types";
import { useApi } from "./use-api";

/** Placement of a pilot district in a real district and state (derived from GET /analytics/geography). */
export interface DistrictMeta {
  code: string;
  district: string;
  state: string;
  state_code: string;
}

/**
 * Country level (36 states and union territories, 32 without coverage) plus the districts of every pilot state, all from
 * GET /analytics/geography. Responses are snapshot backed (about 50 to 90 ms, ETag and a short private cache).
 */
export function useGeo() {
  return useApi("geography", async (signal) => {
    const c = await api<GeoResponse>("/analytics/geography?level=country", { signal });
    const pilot = c.data.areas.filter((a) => a.coverage_status === "pilot");
    const states = await Promise.all(pilot.map((s) => api<GeoResponse>(`/analytics/geography?level=state&code=${encodeURIComponent(s.code)}`, { signal })));
    return { country: c.data, states: c.data.areas as GeoArea[], districts: states.flatMap((r) => r.data.areas) as GeoArea[] };
  });
}

/** The code to district and state mapping the map and the top bar scope use. */
export function useDistrictMap() {
  const g = useGeo();
  const data: DistrictMeta[] | null = g.data ? g.data.districts.map((d) => ({ code: d.code, district: d.name, state: d.state ?? "", state_code: d.state_code ?? "" })) : null;
  return { data, loading: g.loading, error: g.error, reload: g.reload };
}
