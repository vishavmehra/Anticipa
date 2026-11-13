"use client";

// Ask Anticipa: a top bar button (also Command or Control + J) that opens a slide over panel for natural language
// questions about the population data. Mount <AskAnticipa /> once, in the top bar. Self contained: it owns its keyboard
// shortcut, focus handling and the panel; it reads nothing from the shell.

import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { DuoIcon, Icon } from "@/components/ui";
import { AskPanel } from "./ask-panel";

const FOCUSABLE = 'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), summary, [tabindex]:not([tabindex="-1"])';

export function AskAnticipa({ className = "", variant = "button" }: { className?: string; variant?: "button" | "launcher" }) {
  const [open, setOpen] = useState(false);
  // The idle glow on the launcher stops for good after the first open (remembered in this browser, storage guarded).
  const [seen, setSeen] = useState(() => {
    try {
      return typeof window !== "undefined" && window.localStorage.getItem("anticipa-ask-seen") === "1";
    } catch {
      return false;
    }
  });
  // On phones the launcher steps aside while the reader scrolls down and returns on scroll up or at either end of the page,
  // so it never sits on top of numbers or table controls mid page. On wider screens the shell reserves a gutter for it.
  const [away, setAway] = useState(false);
  useEffect(() => {
    if (variant !== "launcher") return;
    let last = window.scrollY;
    const onScroll = () => {
      const y = window.scrollY;
      const phone = window.matchMedia("(max-width: 767px)").matches;
      const atEdge = y < 80 || window.innerHeight + y >= document.documentElement.scrollHeight - 80;
      setAway((a) => (!phone || atEdge ? false : y > last + 2 ? true : y < last - 2 ? false : a));
      last = y;
    };
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, [variant]);

  const markSeen = useCallback(() => {
    setSeen(true);
    try {
      window.localStorage.setItem("anticipa-ask-seen", "1");
    } catch {
      /* storage can be blocked; the glow then stops for this visit only */
    }
  }, []);
  // Platform is read on the client only (server and hydration render the "Ctrl J" hint, then it may switch to the Command key).
  const mac = useSyncExternalStore(
    () => () => undefined,
    () => /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent),
    () => false,
  );
  const triggerRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);

  const close = useCallback(() => {
    setOpen(false);
    triggerRef.current?.focus(); // hand focus back to where the user was
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === "j") {
        e.preventDefault();
        setOpen((o) => !o);
        markSeen();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [markSeen]);

  // Keep the page behind from scrolling while the panel is open.
  useEffect(() => {
    if (!open) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prev;
    };
  }, [open]);

  const onDialogKey = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") {
      e.stopPropagation();
      close();
      return;
    }
    if (e.key !== "Tab") return;
    const nodes = Array.from(dialogRef.current?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? []).filter((n) => n.offsetParent !== null);
    if (!nodes.length) return;
    const first = nodes[0];
    const last = nodes[nodes.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  };

  return (
    <>
      {variant === "launcher" ? (
        <span className={`group/ask fixed right-4 bottom-[max(1rem,env(safe-area-inset-bottom))] z-40 transition-[transform,opacity] duration-200 focus-within:translate-y-0 focus-within:opacity-100 sm:right-6 sm:bottom-6 motion-reduce:transition-none ${away ? "pointer-events-none translate-y-24 opacity-0" : ""}`}>
          <button
            ref={triggerRef}
            type="button"
            onClick={() => {
              setOpen(true);
              markSeen();
            }}
            aria-label="Ask Anticipa"
            aria-describedby="ask-tip"
            aria-haspopup="dialog"
            aria-expanded={open}
            aria-keyshortcuts="Control+J Meta+J"
            className={`glass-strong grid size-12 place-items-center rounded-full! text-brand-ink shadow-[0_14px_28px_-10px_rgb(79_51_168/0.55)] transition-transform hover:-translate-y-0.5 focus-visible:-translate-y-0.5 sm:size-14 ${seen ? "" : "ask-pulse"} ${className}`}
          >
            <DuoIcon name="assistant" className="size-7 sm:size-8" />
          </button>
          <span
            id="ask-tip"
            role="tooltip"
            className="glass-pop pointer-events-none absolute right-0 bottom-full mb-2 flex items-center gap-2 px-3 py-1.5 text-sm font-semibold whitespace-nowrap text-ink opacity-0 transition-opacity group-focus-within/ask:opacity-100 group-hover/ask:opacity-100"
          >
            Ask Anticipa
            <kbd className="rounded-md bg-brand-soft px-1.5 py-0.5 text-[0.6875rem] font-semibold text-brand-ink">{mac ? "⌘J" : "Ctrl J"}</kbd>
          </span>
        </span>
      ) : (
        <button
        ref={triggerRef}
        type="button"
        onClick={() => setOpen(true)}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-keyshortcuts="Control+J Meta+J"
        className={`inline-flex min-h-10 items-center gap-2 rounded-full border border-line-strong/40 bg-[var(--control-bg)] px-3.5 text-[0.875rem] font-semibold text-brand-ink shadow-sm hover:bg-brand-soft ${className}`}
      >
        <Icon name="sparkles" className="size-4" />
        <span>Ask Anticipa</span>
        <kbd className="hidden rounded-md bg-brand-soft px-1.5 py-0.5 text-[0.6875rem] font-semibold text-brand-ink sm:inline">{mac ? "⌘J" : "Ctrl J"}</kbd>
        </button>
      )}

      {open && (
        <div className="fixed inset-0 z-[60]" onMouseDown={(e) => e.target === e.currentTarget && close()}>
          <div className="absolute inset-0 bg-ink/30 backdrop-blur-[2px]" aria-hidden="true" />
          <div
            ref={dialogRef}
            role="dialog"
            aria-modal="true"
            aria-labelledby="ask-title"
            onKeyDown={onDialogKey}
            className="glass-strong absolute inset-y-0 right-0 flex w-full flex-col shadow-pop sm:w-[34rem] sm:rounded-l-card"
          >
            <AskPanel onClose={close} />
          </div>
        </div>
      )}
    </>
  );
}
