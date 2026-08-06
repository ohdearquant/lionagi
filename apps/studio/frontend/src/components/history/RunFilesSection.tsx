"use client";

import { useMemo, useState } from "react";
import { useTranslations } from "use-intl";
import { IconChevronRight, IconFile } from "@/components/ui/icons";

/** One file row, resolved against the run's artifact root. */
export interface RunFileEntry {
  /** Full path as the run recorded it — what the read endpoint needs. */
  path: string;
  /** What the row shows: the path inside its group. */
  name: string;
  /** True only when the backend can actually serve it: the run has an
   * artifact root and the file sits inside it. Files a run touched outside
   * its root are provenance, not readable artifacts, and drawing them as
   * links would manufacture clicks that can only 403. */
  openable: boolean;
}

export interface RunFileGroup {
  label: string;
  files: RunFileEntry[];
  openable: boolean;
}

const HOME_RE = /^\/(?:Users|home)\/[^/]+/;

function shortDir(dir: string): string {
  return dir.replace(HOME_RE, "~");
}

/** Group a run's flat file list for display.
 *
 * Files under the artifact root group by their first path segment — for a
 * play that is one group per agent (coordinator/, explorer-2/, …), which is
 * the organization the workspace already has on disk. Everything else groups
 * by its directory, home-shortened, and is not openable.
 */
export function groupRunFiles(
  files: string[],
  artifactRoot: string | null | undefined,
): RunFileGroup[] {
  const root = artifactRoot ? artifactRoot.replace(/\/+$/, "") : null;
  const inRoot = new Map<string, RunFileEntry[]>();
  const outside = new Map<string, RunFileEntry[]>();

  for (const path of files) {
    if (root && path.startsWith(root + "/")) {
      const rel = path.slice(root.length + 1);
      const slash = rel.indexOf("/");
      const label = slash === -1 ? "·" : rel.slice(0, slash);
      const name = slash === -1 ? rel : rel.slice(slash + 1);
      const list = inRoot.get(label) ?? [];
      list.push({ path, name, openable: true });
      inRoot.set(label, list);
    } else {
      const slash = path.lastIndexOf("/");
      const label = slash <= 0 ? "/" : shortDir(path.slice(0, slash));
      const name = slash === -1 ? path : path.slice(slash + 1);
      const list = outside.get(label) ?? [];
      list.push({ path, name, openable: false });
      outside.set(label, list);
    }
  }

  const byLabel = (a: [string, RunFileEntry[]], b: [string, RunFileEntry[]]) =>
    a[0].localeCompare(b[0]);
  const toGroup = (openable: boolean) => (entry: [string, RunFileEntry[]]) => ({
    label: entry[0],
    files: entry[1].slice().sort((a, b) => a.name.localeCompare(b.name)),
    openable,
  });

  return [
    ...[...inRoot.entries()].sort(byLabel).map(toGroup(true)),
    ...[...outside.entries()].sort(byLabel).map(toGroup(false)),
  ];
}

/** Above this many files the section starts collapsed and shows a filter. */
const EXPAND_ALL_LIMIT = 12;
const FILTER_LIMIT = 15;

export default function RunFilesSection({
  files,
  artifactRoot,
  partial,
  onOpen,
}: {
  files: string[];
  artifactRoot?: string | null;
  partial?: boolean;
  onOpen: (path: string) => void;
}) {
  const t = useTranslations("history.detail");
  const [query, setQuery] = useState("");
  const [toggled, setToggled] = useState<Record<string, boolean>>({});

  const groups = useMemo(() => groupRunFiles(files, artifactRoot), [files, artifactRoot]);

  const normalized = query.trim().toLowerCase();
  const visible = useMemo(() => {
    if (!normalized) return groups;
    return groups
      .map((g) => ({
        ...g,
        files: g.files.filter((f) => f.path.toLowerCase().includes(normalized)),
      }))
      .filter((g) => g.files.length > 0);
  }, [groups, normalized]);

  const expandDefault = files.length <= EXPAND_ALL_LIMIT || normalized.length > 0;

  return (
    <div id="run-files" className="scroll-mt-4">
      <div className="flex items-baseline justify-between gap-3">
        <SectionLabel label={t("sectionFiles")} count={files.length} />
        {files.length > FILTER_LIMIT && (
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={t("filesFilter")}
            className="focus-ring mb-2 w-44 rounded border border-edge bg-surface-raised px-2 py-1 font-mono text-[length:var(--t-xs)] text-content-primary placeholder:text-content-muted"
          />
        )}
      </div>

      {files.length === 0 ? (
        <div className="rounded border border-edge bg-surface-raised px-4 py-3 text-sm text-content-muted">
          {partial ? t("noFilesPartial") : t("noFiles")}
        </div>
      ) : (
        <div className="max-h-96 overflow-y-auto rounded border border-edge bg-surface-raised">
          {!artifactRoot && (
            <p className="border-b border-edge px-3 py-1.5 text-[length:var(--t-xs)] text-content-muted">
              {t("filesNotReadable")}
            </p>
          )}
          {visible.map((g) => {
            const open = toggled[g.label] ?? expandDefault;
            return (
              <div key={g.label} className="border-b border-edge last:border-b-0">
                <button
                  type="button"
                  onClick={() => setToggled((s) => ({ ...s, [g.label]: !open }))}
                  className="focus-ring flex w-full items-center gap-1.5 px-3 py-1.5 text-left hover:bg-surface-overlay"
                >
                  <IconChevronRight
                    size={10}
                    className={`shrink-0 text-content-muted transition-transform ${open ? "rotate-90" : ""}`}
                  />
                  <span className="truncate font-mono text-[length:var(--t-xs)] font-semibold text-content-secondary">
                    {g.label}
                  </span>
                  <span className="ml-auto shrink-0 font-mono text-[length:var(--t-xs)] tabular-nums text-content-muted">
                    {g.files.length}
                  </span>
                </button>
                {open && (
                  <ul className="pb-1">
                    {g.files.map((f) =>
                      f.openable ? (
                        <li key={f.path}>
                          <button
                            type="button"
                            onClick={() => onOpen(f.path)}
                            title={f.path}
                            className="focus-ring flex w-full items-center gap-1.5 py-0.5 pl-7 pr-3 text-left hover:bg-surface-overlay"
                          >
                            <IconFile size={10} className="shrink-0 text-content-muted" />
                            <span className="truncate font-mono text-[length:var(--t-xs)] text-content-secondary hover:text-content-primary">
                              {f.name}
                            </span>
                          </button>
                        </li>
                      ) : (
                        <li
                          key={f.path}
                          title={f.path}
                          className="flex items-center gap-1.5 py-0.5 pl-7 pr-3"
                        >
                          <span className="truncate font-mono text-[length:var(--t-xs)] text-content-muted">
                            {f.name}
                          </span>
                        </li>
                      ),
                    )}
                  </ul>
                )}
              </div>
            );
          })}
          {visible.length === 0 && (
            <p className="px-3 py-2 text-[length:var(--t-xs)] text-content-muted">
              {t("filesFilterEmpty")}
            </p>
          )}
        </div>
      )}
    </div>
  );
}

function SectionLabel({ label, count }: { label: string; count: number }) {
  return (
    <h3 className="mb-2 flex items-baseline gap-2 text-[length:var(--t-xs)] font-semibold uppercase tracking-wider text-content-muted">
      {label}
      <span className="font-mono tabular-nums">{count}</span>
    </h3>
  );
}
