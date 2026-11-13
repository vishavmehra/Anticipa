"use client";

import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { api } from "@/lib/api";
import { useApi } from "@/lib/use-api";
import { Icon } from "@/components/ui";
import { TurnView } from "./answer-view";
import { plain } from "./format";
import type { AskSuggestions } from "./types";
import { useAsk } from "./use-ask";

// Used only when the live suggestions cannot be loaded; they are generic, not numbers.
const FALLBACK_CHIPS = [
  "How many care gaps are open, and how many are waiting for outreach?",
  "Which district has the lowest closure rate?",
  "Which screening programme has the most open cases?",
  "How full is clinic capacity over the next two weeks?",
];
const MAX_CHARS = 500;

/** The conversation: suggested questions, answers with chart, table and sources, and the question box. */
export function AskPanel({ onClose, autoFocus = true }: { onClose?: () => void; autoFocus?: boolean }) {
  const { turns, busy, ask, clear, retry } = useAsk();
  const [text, setText] = useState("");
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const endRef = useRef<HTMLDivElement>(null);
  const suggestions = useApi("ask:suggestions", async (signal) => (await api<AskSuggestions>("/ask/suggestions", { signal })).data);

  useEffect(() => {
    if (autoFocus) inputRef.current?.focus();
  }, [autoFocus]);
  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "end", behavior: "smooth" });
  }, [turns]);

  const submit = (q: string) => {
    if (busy || q.trim().length < 3) return;
    void ask(q);
    setText("");
  };
  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    submit(text);
  };
  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      submit(text);
    }
  };

  const chips = suggestions.data?.suggestions.map((s) => s.question) ?? (suggestions.loading ? [] : FALLBACK_CHIPS);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <header className="flex items-start justify-between gap-3 border-b border-line px-4 py-3.5 sm:px-5">
        <div className="flex min-w-0 items-start gap-3">
          <span className="grid size-9 shrink-0 place-items-center rounded-full bg-brand-soft text-brand-ink">
            <Icon name="sparkles" className="size-[1.1rem]" />
          </span>
          <div className="min-w-0">
            <h2 id="ask-title" className="font-display text-lg leading-6 font-bold tracking-tight">
              Ask Anticipa
            </h2>
            <p className="text-[0.8125rem] text-ink-muted">Questions about the population data. Answers use verified totals only, never individual records.</p>
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {turns.length > 0 && (
            <button type="button" onClick={clear} className="rounded-control px-2.5 py-1.5 text-[0.8125rem] font-medium text-brand-ink hover:bg-brand-soft">
              New conversation
            </button>
          )}
          {onClose && (
            <button type="button" onClick={onClose} aria-label="Close Ask Anticipa" className="grid size-9 place-items-center rounded-control text-ink-soft hover:bg-brand-soft">
              <Icon name="close" className="size-[1.1rem]" />
            </button>
          )}
        </div>
      </header>

      <div role="log" aria-live="polite" aria-label="Conversation" className="scroll-quiet min-h-0 flex-1 space-y-5 overflow-y-auto px-4 py-4 sm:px-5">
        {turns.length === 0 && (
          <section aria-labelledby="ask-start" className="space-y-3">
            <h3 id="ask-start" className="text-[0.8125rem] font-semibold tracking-wide text-ink-muted uppercase">
              Try a question
            </h3>
            {suggestions.loading && chips.length === 0 ? (
              <div role="status" aria-label="Loading suggested questions" className="space-y-2">
                {[0, 1, 2, 3].map((i) => (
                  <div key={i} className="skeleton h-11 w-full rounded-control" />
                ))}
              </div>
            ) : (
              <ul className="grid gap-2">
                {chips.map((q) => (
                  <li key={q}>
                    <button type="button" onClick={() => submit(q)} disabled={busy} className="glass-card w-full rounded-control px-3.5 py-2.5 text-left text-[0.875rem] font-medium text-ink hover:text-brand-ink">
                      {plain(q)}
                    </button>
                  </li>
                ))}
              </ul>
            )}
            <p className="pt-1 text-[0.8125rem] text-ink-muted">
              Ask Anticipa explains figures from the dashboard data. It does not give medical advice and cannot say whether a particular person is due for screening. All data is generated or simulated.
            </p>
          </section>
        )}
        {turns.map((t) => (
          <TurnView key={t.id} turn={t} onFollowUp={submit} onRetry={retry} />
        ))}
        <div ref={endRef} />
      </div>

      <form onSubmit={onSubmit} className="border-t border-line bg-[var(--glass-bg-strong)] px-4 py-3 sm:px-5">
        <label htmlFor="ask-input" className="sr-only">
          Your question
        </label>
        <div className="flex items-end gap-2">
          <textarea
            ref={inputRef}
            id="ask-input"
            value={text}
            onChange={(e) => setText(e.target.value.slice(0, MAX_CHARS))}
            onKeyDown={onKey}
            rows={1}
            placeholder="Ask about the population data"
            className="min-h-11 max-h-32 w-full resize-none rounded-control border border-line-strong bg-[var(--control-bg)] px-3 py-2.5 text-[0.9375rem] text-ink placeholder:text-ink-muted"
          />
          <button type="submit" disabled={busy || text.trim().length < 3} className="grad-brand inline-flex min-h-11 shrink-0 items-center gap-1.5 rounded-control px-4 text-[0.875rem] font-semibold text-white disabled:opacity-60">
            <Icon name="zap" className="size-4" />
            Ask
          </button>
        </div>
        <p className="mt-1.5 flex justify-between text-[0.75rem] text-ink-muted">
          <span>Enter to send, Shift and Enter for a new line</span>
          {text.length > 400 && <span aria-live="polite">{MAX_CHARS - text.length} characters left</span>}
        </p>
      </form>
    </div>
  );
}
