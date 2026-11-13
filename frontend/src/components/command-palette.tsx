"use client";

// Command palette (Command or Control + K): jump to a page, run an action, or find a resident by id.
// Residents come from GET /residents?search= (the same search the Residents page uses). Nothing is cached or invented.

import { useRouter } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";
import { apiList } from "@/lib/api";
import { humanize } from "@/lib/format";
import type { NavItem } from "@/lib/nav";
import type { Resident } from "@/lib/types";
import { useApi } from "@/lib/use-api";
import { Icon, type IconName } from "./ui";

export interface PaletteAction {
  id: string;
  label: string;
  icon: IconName;
  run?: () => void;
  href?: string;
}

interface Entry {
  id: string;
  group: "Pages" | "Actions" | "Residents";
  label: string;
  hint?: string;
  icon: IconName;
  run: () => void;
}

export function CommandPalette({ open, onClose, pages, actions, canResidents }: { open: boolean; onClose: () => void; pages: readonly NavItem[]; actions: readonly PaletteAction[]; canResidents: boolean }) {
  const router = useRouter();
  const [q, setQ] = useState("");
  const [dq, setDq] = useState("");
  const [cursor, setCursor] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const t = setTimeout(() => setDq(q.trim()), 220);
    return () => clearTimeout(t);
  }, [q]);
  useEffect(() => {
    if (open) inputRef.current?.focus();
  }, [open]);

  const people = useApi(`palette:${open ? dq : ""}`, async (signal) => (open && canResidents && dq.length >= 2 ? (await apiList<Resident>("/residents", { search: dq, page_size: 5 }, signal)).data : []));

  const entries = useMemo<Entry[]>(() => {
    const needle = q.trim().toLowerCase();
    const hit = (s: string) => !needle || s.toLowerCase().includes(needle);
    const go = (href: string) => () => {
      router.push(href);
      onClose();
    };
    return [
      ...pages.filter((p) => hit(p.label)).map<Entry>((p) => ({ id: `p-${p.href}`, group: "Pages", label: p.label, icon: p.icon, run: go(p.href) })),
      ...actions.filter((a) => hit(a.label)).map<Entry>((a) => ({ id: `a-${a.id}`, group: "Actions", label: a.label, icon: a.icon, run: a.href ? go(a.href) : () => { a.run?.(); onClose(); } })),
      ...(people.data ?? []).map<Entry>((r) => ({
        id: `r-${r.id}`,
        group: "Residents",
        label: `Resident ${r.id.slice(0, 8)}`,
        hint: [r.age == null ? null : `Age ${r.age}`, humanize(r.administrative_sex), r.district_code].filter(Boolean).join(", "),
        icon: "user",
        run: go(`/residents/${r.id}`),
      })),
    ];
  }, [q, pages, actions, people.data, router, onClose]);

  if (!open) return null;
  const at = Math.min(cursor, Math.max(0, entries.length - 1));
  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") onClose();
    else if (e.key === "ArrowDown") {
      e.preventDefault();
      setCursor((at + 1) % Math.max(1, entries.length));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setCursor((at - 1 + entries.length) % Math.max(1, entries.length));
    } else if (e.key === "Enter") entries[at]?.run();
  };
  let last = "";
  return (
    <div className="fixed inset-0 z-50 grid place-items-start bg-ink/30 px-4 pt-[12vh] backdrop-blur-[2px]" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div role="dialog" aria-modal="true" aria-label="Command palette" onKeyDown={onKey} className="glass-pop mx-auto w-full max-w-xl overflow-hidden">
        <div className="flex items-center gap-3 border-b border-line px-4">
          <Icon name="search" className="size-[1.1rem] text-ink-muted" />
          <label htmlFor="palette-input" className="sr-only">
            Search pages, actions and residents
          </label>
          <input
            ref={inputRef}
            id="palette-input"
            role="combobox"
            aria-expanded="true"
            aria-controls="palette-list"
            aria-activedescendant={entries[at] ? `palette-${entries[at].id}` : undefined}
            value={q}
            onChange={(e) => {
              setQ(e.target.value);
              setCursor(0);
            }}
            placeholder="Search pages, actions, or a resident id"
            autoComplete="off"
            className="min-h-14 w-full bg-transparent text-base text-ink placeholder:text-ink-muted focus:outline-none"
          />
          <kbd className="rounded-md bg-brand-soft px-2 py-0.5 text-xs font-semibold text-brand-ink">Esc</kbd>
        </div>
        <ul id="palette-list" role="listbox" aria-label="Results" className="scroll-quiet max-h-[22rem] overflow-y-auto p-2">
          {entries.map((en, i) => {
            const header = en.group !== last ? en.group : null;
            last = en.group;
            return (
              <li key={en.id} role="presentation">
                {header && <p className="px-3 pt-2 pb-1 text-xs font-semibold tracking-wide text-ink-muted uppercase">{header}</p>}
                <div
                  id={`palette-${en.id}`}
                  role="option"
                  aria-selected={i === at}
                  onMouseEnter={() => setCursor(i)}
                  onClick={en.run}
                  className={`flex cursor-pointer items-center gap-3 rounded-control px-3 py-2.5 ${i === at ? "bg-brand-soft text-brand-ink" : "text-ink"}`}
                >
                  <Icon name={en.icon} className="size-[1.1rem]" />
                  <span className="min-w-0 flex-1 truncate font-medium">{en.label}</span>
                  {en.hint && <span className="truncate text-xs text-ink-muted">{en.hint}</span>}
                </div>
              </li>
            );
          })}
          {entries.length === 0 && <li className="px-3 py-6 text-center text-ink-muted">{people.loading && dq.length >= 2 ? "Searching" : "No match. Enter at least 2 characters of a resident id."}</li>}
        </ul>
      </div>
    </div>
  );
}
