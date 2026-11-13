"use client";

import { useCallback, useRef, useState } from "react";
import { ApiError, apiPost } from "@/lib/api";
import { plain } from "./format";
import type { AskAnswer, AskHistoryTurn } from "./types";

export interface Turn {
  id: number;
  question: string;
  status: "loading" | "done" | "error";
  answer?: AskAnswer;
  error?: { code: string; message: string };
}

const MAX_HISTORY_TURNS = 6; // the API accepts at most this many (three question and answer pairs)

/** Friendly, official wording for each failure the API can return; never the raw server message. */
export function describeError(e: unknown): { code: string; message: string } {
  if (e instanceof ApiError) {
    switch (e.code) {
      case "RATE_LIMITED":
        return { code: e.code, message: "The question limit has been reached for now. Please try again later." };
      case "FORBIDDEN":
        return { code: e.code, message: "Your role does not include access to Ask Anticipa." };
      case "UNAUTHENTICATED":
        return { code: e.code, message: "Your session has ended. Sign in again to ask a question." };
      case "PROVIDER_UNAVAILABLE":
        return { code: e.code, message: "The analytics assistant is temporarily unavailable. Please try again in a moment." };
      case "VALIDATION_ERROR":
        return { code: e.code, message: "That question could not be sent. Check its length and try again." };
      case "NETWORK_ERROR":
        return { code: e.code, message: "The service could not be reached. Check the connection and try again." };
    }
  }
  return { code: "INTERNAL_ERROR", message: "Something went wrong while answering. Please try again." };
}

/** Conversation state for the panel: one in flight question at a time, earlier answers sent back as short history. */
export function useAsk() {
  const [turns, setTurns] = useState<Turn[]>([]);
  const idRef = useRef(0);
  const busy = turns.some((t) => t.status === "loading");

  const ask = useCallback(
    async (raw: string) => {
      const question = raw.trim().replace(/\s+/g, " ").slice(0, 500);
      if (question.length < 3 || busy) return;
      const id = ++idRef.current;
      const history: AskHistoryTurn[] = turns
        .filter((t) => t.status === "done" && t.answer && !t.answer.refused)
        .flatMap<AskHistoryTurn>((t) => [
          { role: "user", content: t.question.slice(0, 1500) },
          { role: "assistant", content: plain(t.answer!.answer).slice(0, 1500) },
        ])
        .slice(-MAX_HISTORY_TURNS);
      setTurns((prev) => [...prev, { id, question, status: "loading" }]);
      try {
        const res = await apiPost<AskAnswer>("/ask", { question, history });
        setTurns((prev) => prev.map((t) => (t.id === id ? { ...t, status: "done", answer: res.data } : t)));
      } catch (e) {
        setTurns((prev) => prev.map((t) => (t.id === id ? { ...t, status: "error", error: describeError(e) } : t)));
      }
    },
    [busy, turns],
  );

  const clear = useCallback(() => setTurns([]), []);
  const retry = useCallback(
    (id: number) => {
      const t = turns.find((x) => x.id === id);
      if (!t) return;
      setTurns((prev) => prev.filter((x) => x.id !== id));
      void ask(t.question);
    },
    [ask, turns],
  );
  return { turns, busy, ask, clear, retry };
}
