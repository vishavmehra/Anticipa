"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "./api";

// Stale while revalidate: the last good answer per key is kept in memory (bounded), shown at once when the key is used again
// and refreshed in the background. prefetchApi warms a key before the page that reads it opens (for example on link hover).
const CACHE = new Map<string, unknown>();
const CACHE_MAX = 300;
function remember(key: string, data: unknown) {
  CACHE.delete(key);
  CACHE.set(key, data);
  if (CACHE.size > CACHE_MAX) CACHE.delete(CACHE.keys().next().value as string);
}
export function prefetchApi<T>(key: string, fetcher: (signal: AbortSignal) => Promise<T>): void {
  if (CACHE.has(key)) return;
  fetcher(new AbortController().signal).then((d) => remember(key, d)).catch(() => undefined);
}

/**
 * Minimal fetch-state hook (no SWR dependency). Refetches when `key` changes or `reload()` runs.
 * Cached data for the key shows immediately while it refreshes; otherwise previous data stays visible while the next page
 * loads. `loading` is derived, not set in effects.
 */
export function useApi<T>(key: string, fetcher: (signal: AbortSignal) => Promise<T>) {
  const [tick, setTick] = useState(0);
  const [res, setRes] = useState<{ k: string; data?: T; error?: ApiError } | null>(null);
  const fetcherRef = useRef(fetcher);
  useEffect(() => {
    fetcherRef.current = fetcher;
  });

  const reqKey = `${key}#${tick}`;
  useEffect(() => {
    const ac = new AbortController();
    fetcherRef
      .current(ac.signal)
      .then((data) => {
        remember(key, data);
        setRes({ k: reqKey, data });
      })
      .catch((e: unknown) => {
        if (e instanceof DOMException && e.name === "AbortError") return;
        setRes({ k: reqKey, error: e instanceof ApiError ? e : new ApiError("INTERNAL_ERROR", e instanceof Error ? e.message : "Unexpected client error.", 0) });
      });
    return () => ac.abort();
  }, [reqKey, key]);

  const settled = res?.k === reqKey;
  const reload = useCallback(() => setTick((t) => t + 1), []);
  return {
    data: settled ? (res?.data ?? null) : ((CACHE.get(key) as T | undefined) ?? res?.data ?? null),
    error: settled ? (res?.error ?? null) : null,
    loading: !settled,
    reload,
  };
}
