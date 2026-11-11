"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "./api";

/**
 * Minimal fetch-state hook (no SWR dependency). Refetches when `key` changes or `reload()` runs.
 * Previous data stays visible while the next page loads; `loading` is derived, not set in effects.
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
      .then((data) => setRes({ k: reqKey, data }))
      .catch((e: unknown) => {
        if (e instanceof DOMException && e.name === "AbortError") return;
        setRes({ k: reqKey, error: e instanceof ApiError ? e : new ApiError("INTERNAL_ERROR", e instanceof Error ? e.message : "Unexpected client error.", 0) });
      });
    return () => ac.abort();
  }, [reqKey]);

  const settled = res?.k === reqKey;
  const reload = useCallback(() => setTick((t) => t + 1), []);
  return {
    data: res?.data ?? null,
    error: settled ? (res?.error ?? null) : null,
    loading: !settled,
    reload,
  };
}
