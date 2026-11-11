"use client";

import { useCallback, useState } from "react";
import { ApiError } from "./api";

/** State for one mutating call: pending flag, last error. `run` resolves true on success so callers can reload. */
export function useAction() {
  const [state, setState] = useState<{ pending: boolean; error: ApiError | null }>({ pending: false, error: null });
  const run = useCallback(async (fn: () => Promise<unknown>): Promise<boolean> => {
    setState({ pending: true, error: null });
    try {
      await fn();
      setState({ pending: false, error: null });
      return true;
    } catch (e) {
      setState({ pending: false, error: e instanceof ApiError ? e : new ApiError("INTERNAL_ERROR", "Unexpected client error.", 0) });
      return false;
    }
  }, []);
  return { ...state, run };
}
