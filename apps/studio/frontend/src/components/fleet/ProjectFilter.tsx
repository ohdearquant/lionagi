import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent, ReactNode } from "react";
import { useTranslations } from "use-intl";
import { listRunProjects } from "@/lib/api";
import type { RunProjectCount } from "@/lib/api";
import { IconCheck, IconChevronDown, IconClose, IconSearch } from "@/components/ui/icons";

const REFRESH_INTERVAL_MS = 30_000;
const RECENT_LIMIT = 5;

export interface ProjectFilterChange {
  project?: string;
  projectNull?: boolean;
}

interface ProjectFilterProps {
  project: string | null;
  projectNull: boolean;
  onChange: (next: ProjectFilterChange) => void;
}

/** Merge rows that share a project name so the option list never renders the
 * same project twice — counts sum, last_activity keeps the most recent. */
export function dedupeProjectCounts(rows: RunProjectCount[]): RunProjectCount[] {
  const byName = new Map<string, RunProjectCount>();
  for (const row of rows) {
    if (row.project == null) continue;
    const existing = byName.get(row.project);
    if (!existing) {
      byName.set(row.project, { ...row });
      continue;
    }
    existing.count += row.count;
    if ((row.last_activity ?? -Infinity) > (existing.last_activity ?? -Infinity)) {
      existing.last_activity = row.last_activity;
    }
  }
  return [...byName.values()];
}

/** Count descending — pushes the long tail of one-run/junk-attribution
 * projects below the projects people actually work in. */
export function sortByCountDesc(rows: RunProjectCount[]): RunProjectCount[] {
  return [...rows].sort((a, b) => b.count - a.count);
}

export function recentProjects(rows: RunProjectCount[], limit = RECENT_LIMIT): RunProjectCount[] {
  return [...rows]
    .filter((r) => r.last_activity != null)
    .sort((a, b) => (b.last_activity ?? 0) - (a.last_activity ?? 0))
    .slice(0, limit);
}

export function filterProjectsByQuery(rows: RunProjectCount[], query: string): RunProjectCount[] {
  const q = query.trim().toLowerCase();
  if (!q) return rows;
  return rows.filter((r) => (r.project ?? "").toLowerCase().includes(q));
}

type FlatOption = { kind: "all" } | { kind: "none" } | { kind: "project"; row: RunProjectCount };

function GroupHeader({ children }: { children: ReactNode }) {
  return (
    <div className="px-2 py-1 font-data text-[length:var(--t-xs)] uppercase tracking-[0.1em] text-content-muted">
      {children}
    </div>
  );
}

function OptionRow({
  id,
  label,
  count,
  active,
  current,
  onClick,
  onMouseEnter,
}: {
  id: string;
  label: string;
  count?: number;
  active: boolean;
  current: boolean;
  onClick: () => void;
  onMouseEnter: () => void;
}) {
  return (
    <div
      id={id}
      role="option"
      aria-selected={current}
      tabIndex={-1}
      onClick={onClick}
      onMouseEnter={onMouseEnter}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") onClick();
      }}
      className={`flex cursor-pointer items-center justify-between gap-2 px-2 py-1.5 font-data text-[length:var(--t-xs)] transition-colors ${
        active ? "bg-surface-raised text-content-primary" : "text-content-secondary"
      }`}
    >
      <span className="flex min-w-0 items-center gap-1.5">
        {current && <IconCheck size={10} aria-hidden="true" className="shrink-0 text-accent" />}
        <span className="truncate">{label}</span>
      </span>
      {count != null && <span className="shrink-0 text-content-muted">({count})</span>}
    </div>
  );
}

/** Searchable, keyboard-navigable project combobox — replaces the plain
 * `<select>` that scaled poorly once a production install accumulates
 * hundreds of projects. Options are pinned (all/no project), then a
 * "Recent" group by last activity, then the full list ordered by run count
 * so the long tail of one-off/junk-attribution projects sinks to the
 * bottom instead of dominating an alphabetical or fetch-order list. */
export default function ProjectFilter({ project, projectNull, onChange }: ProjectFilterProps) {
  const t = useTranslations("fleet");
  const [projects, setProjects] = useState<RunProjectCount[]>([]);
  const [error, setError] = useState(false);
  const [dismissed, setDismissed] = useState(false);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [activeRaw, setActiveRaw] = useState(0);

  const triggerRef = useRef<HTMLButtonElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const rootRef = useRef<HTMLDivElement>(null);

  // Fetching the project facet is independent of the run list poll — a
  // failure here must never block or clear the runs already rendered.
  const fetchProjects = useCallback(async () => {
    try {
      const r = await listRunProjects();
      setProjects(dedupeProjectCounts(r.projects));
      setError(false);
      setDismissed(false);
    } catch {
      setError(true);
      setDismissed(false);
    }
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- initial async fetch owns the projects/error lifecycle; periodic refresh is the same fetch on a timer
    void fetchProjects();
    const id = window.setInterval(() => void fetchProjects(), REFRESH_INTERVAL_MS);
    return () => window.clearInterval(id);
  }, [fetchProjects]);

  useEffect(() => {
    if (!open) return;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- reset stale search/selection before the popover is shown
    setQuery("");
    setActiveRaw(0);
    const id = window.setTimeout(() => inputRef.current?.focus(), 0);
    return () => window.clearTimeout(id);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    function handleMouseDown(e: MouseEvent) {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    }
    window.addEventListener("mousedown", handleMouseDown);
    return () => window.removeEventListener("mousedown", handleMouseDown);
  }, [open]);

  const showRecent = query.trim() === "";
  const filtered = useMemo(() => filterProjectsByQuery(projects, query), [projects, query]);
  const sorted = useMemo(() => sortByCountDesc(filtered), [filtered]);
  const recent = useMemo(
    () => (showRecent ? recentProjects(projects) : []),
    [projects, showRecent],
  );
  const recentNames = useMemo(() => new Set(recent.map((r) => r.project)), [recent]);
  const rest = useMemo(
    () => (showRecent ? sorted.filter((r) => !recentNames.has(r.project)) : sorted),
    [sorted, showRecent, recentNames],
  );

  const options: FlatOption[] = useMemo(
    () => [
      { kind: "all" },
      { kind: "none" },
      ...recent.map((row) => ({ kind: "project" as const, row })),
      ...rest.map((row) => ({ kind: "project" as const, row })),
    ],
    [recent, rest],
  );

  // Clamped at render time (not via a setState-in-effect) — narrowing the
  // search query can shrink options.length below a keyboard-hovered index.
  const active = Math.min(activeRaw, Math.max(options.length - 1, 0));

  const select = useCallback(
    (opt: FlatOption) => {
      if (opt.kind === "all") onChange({});
      else if (opt.kind === "none") onChange({ projectNull: true });
      else onChange({ project: opt.row.project as string });
      setOpen(false);
      triggerRef.current?.focus();
    },
    [onChange],
  );

  function handleInputKeyDown(e: KeyboardEvent<HTMLInputElement>) {
    if (e.nativeEvent.isComposing) return;
    if (e.key === "Escape") {
      e.preventDefault();
      setOpen(false);
      triggerRef.current?.focus();
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      setActiveRaw((a) => Math.min(a + 1, options.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActiveRaw((a) => Math.max(a - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (query.trim() !== "" && filtered.length === 0) return;
      const opt = options[active];
      if (opt) select(opt);
    }
  }

  const label = projectNull ? t("filters.noProject") : project ? project : t("filters.allProjects");
  const activeId = options[active] ? `project-opt-${active}` : undefined;

  return (
    <div className="relative shrink-0" ref={rootRef}>
      <button
        ref={triggerRef}
        type="button"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={t("filters.projectAria")}
        onClick={() => setOpen((o) => !o)}
        className="flex max-w-[12rem] shrink-0 items-center gap-1 rounded border border-edge bg-surface-base px-2 py-1 font-data text-[length:var(--t-xs)] text-content-primary focus:border-accent/50 focus:outline-none"
      >
        <span className="truncate">{label}</span>
        <IconChevronDown size={12} aria-hidden="true" className="shrink-0 text-content-muted" />
      </button>

      {open && (
        <div
          className="absolute left-0 top-full z-20 mt-1 w-64 overflow-hidden rounded border border-edge-strong bg-surface-overlay"
          style={{ boxShadow: "0 12px 24px rgba(0,0,0,0.4)" }}
        >
          <div className="flex items-center gap-2 border-b border-edge px-2 py-1.5">
            <IconSearch size={12} aria-hidden="true" className="shrink-0 text-content-muted" />
            <input
              ref={inputRef}
              role="combobox"
              aria-expanded={open}
              aria-controls="project-filter-listbox"
              aria-activedescendant={activeId}
              aria-autocomplete="list"
              type="text"
              value={query}
              onChange={(e) => {
                const value = e.target.value;
                setQuery(value);
                if (value.trim() === "") {
                  setActiveRaw(0);
                } else {
                  // Non-empty query never has a "Recent" group (see `showRecent`),
                  // so the first filtered match always lands right after the two
                  // pinned options — index 2 — when there is one.
                  const hasMatch = filterProjectsByQuery(projects, value).length > 0;
                  setActiveRaw(hasMatch ? 2 : 0);
                }
              }}
              onKeyDown={handleInputKeyDown}
              placeholder={t("filters.projectSearchPlaceholder")}
              aria-label={t("filters.projectSearchAria")}
              className="min-w-0 flex-1 bg-transparent font-data text-[length:var(--t-xs)] text-content-primary outline-none placeholder:text-content-muted"
            />
          </div>

          {error && !dismissed && (
            <div className="flex items-center justify-between gap-2 border-b border-status-error/30 bg-status-error-bg px-2 py-1.5 font-data text-[length:var(--t-xs)] text-status-error">
              <span>{t("filters.loadError")}</span>
              <div className="flex shrink-0 items-center gap-2">
                <button type="button" onClick={() => void fetchProjects()} className="underline">
                  {t("filters.retry")}
                </button>
                <button
                  type="button"
                  onClick={() => setDismissed(true)}
                  aria-label={t("filters.dismiss")}
                >
                  <IconClose size={10} aria-hidden="true" />
                </button>
              </div>
            </div>
          )}

          <div
            id="project-filter-listbox"
            role="listbox"
            aria-label={t("filters.projectAria")}
            className="max-h-64 overflow-y-auto py-1"
          >
            {options.map((opt, i) => {
              const isRecentHeader = i === 2 && recent.length > 0;
              const isAllHeader = i === 2 + recent.length && rest.length > 0 && recent.length > 0;
              const id = `project-opt-${i}`;
              const isActive = active === i;
              let node: ReactNode;
              if (opt.kind === "all") {
                node = (
                  <OptionRow
                    id={id}
                    label={t("filters.allProjects")}
                    active={isActive}
                    current={!project && !projectNull}
                    onClick={() => select(opt)}
                    onMouseEnter={() => setActiveRaw(i)}
                  />
                );
              } else if (opt.kind === "none") {
                node = (
                  <OptionRow
                    id={id}
                    label={t("filters.noProject")}
                    active={isActive}
                    current={projectNull}
                    onClick={() => select(opt)}
                    onMouseEnter={() => setActiveRaw(i)}
                  />
                );
              } else {
                node = (
                  <OptionRow
                    id={id}
                    label={opt.row.project as string}
                    count={opt.row.count}
                    active={isActive}
                    current={!projectNull && project === opt.row.project}
                    onClick={() => select(opt)}
                    onMouseEnter={() => setActiveRaw(i)}
                  />
                );
              }
              return (
                <div key={id}>
                  {isRecentHeader && <GroupHeader>{t("filters.groupRecent")}</GroupHeader>}
                  {isAllHeader && <GroupHeader>{t("filters.groupAll")}</GroupHeader>}
                  {node}
                </div>
              );
            })}

            {projects.length > 0 && filtered.length === 0 && (
              <div className="px-2 py-3 text-center font-data text-[length:var(--t-xs)] text-content-muted">
                {t("filters.noMatches")}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
