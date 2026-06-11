import { createFileRoute } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import Badge from "@/components/Badge";
import { listSkills, getSkill } from "@/lib/api";
import type { SkillSummary, SkillDetail } from "@/lib/api";
import { empty } from "@/lib/copy";

export const Route = createFileRoute("/skills/")({
  component: SkillsPage,
});

function SkillsPage() {
  const [skills, setSkills] = useState<SkillSummary[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<SkillDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [filter, setFilter] = useState("");

  useEffect(() => {
    let active = true;
    listSkills()
      .then((data) => {
        if (active) {
          setSkills(data.skills);
          // Use functional update so selected is read from state, not closure —
          // avoids a stale-closure dep and removes need to list `selected` in deps
          setSelected((prev) => prev ?? (data.skills.length > 0 ? data.skills[0].name : null));
        }
      })
      .catch(() => {
        if (active) setSkills([]);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    if (!selected) return;
    let active = true;
    void Promise.resolve()
      .then(() => {
        setDetailLoading(true);
        return getSkill(selected);
      })
      .then((d) => {
        if (active) setDetail(d);
      })
      .catch(() => {
        if (active) setDetail(null);
      })
      .finally(() => {
        if (active) setDetailLoading(false);
      });
    return () => {
      active = false;
    };
  }, [selected]);

  const filtered = filter
    ? skills.filter(
        (s) =>
          s.name.toLowerCase().includes(filter.toLowerCase()) ||
          s.description.toLowerCase().includes(filter.toLowerCase()),
      )
    : skills;

  return (
    <div className="flex h-[calc(100vh-44px)]">
      {/* Left pane — skill list */}
      <aside className="flex w-72 shrink-0 flex-col border-r border-edge bg-surface-raised">
        <div className="border-b border-edge px-3 py-2.5">
          <h2 className="text-label font-semibold text-content-primary">Skills</h2>
          <p className="text-meta text-content-muted">
            {skills.length} skill{skills.length !== 1 ? "s" : ""}
          </p>
        </div>

        <div className="border-b border-edge px-3 py-2">
          <input
            type="text"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="Filter skills..."
            className="w-full rounded border border-edge bg-surface-input px-2 py-1 text-body text-content-primary placeholder-content-muted focus:border-interactive-primary focus:outline-none"
          />
        </div>

        <div className="flex-1 overflow-y-auto">
          {loading ? (
            <div className="px-3 py-4 text-body text-content-muted">Loading...</div>
          ) : filtered.length === 0 ? (
            <div className="px-3 py-4 text-body text-content-muted">
              {filter ? empty.skillsFiltered : empty.skills}
            </div>
          ) : (
            filtered.map((s) => (
              <button
                key={s.name}
                type="button"
                onClick={() => setSelected(s.name)}
                className={[
                  "flex w-full flex-col gap-0.5 border-b border-edge/50 px-3 py-2 text-left transition-colors",
                  selected === s.name
                    ? "bg-interactive-primary/10 border-l-2 border-l-interactive-primary"
                    : "hover:bg-surface-input/50",
                ].join(" ")}
              >
                <span className="text-body font-medium text-content-primary">{s.name}</span>
                {s.description && (
                  <span className="line-clamp-2 text-meta text-content-muted">{s.description}</span>
                )}
              </button>
            ))
          )}
        </div>
      </aside>

      {/* Right pane — skill detail */}
      <main className="flex flex-1 flex-col overflow-hidden">
        {!selected ? (
          <div className="flex flex-1 items-center justify-center">
            <p className="text-body text-content-muted">Select a skill to view its details</p>
          </div>
        ) : detailLoading ? (
          <div className="flex flex-1 items-center justify-center">
            <p className="text-body text-content-muted">Loading...</p>
          </div>
        ) : !detail ? (
          <div className="flex flex-1 items-center justify-center">
            <p className="text-body text-content-muted">Skill not found</p>
          </div>
        ) : (
          <>
            {/* Detail header */}
            <header className="flex items-center gap-3 border-b border-edge px-4 py-2.5">
              <h1 className="text-label font-semibold text-content-primary">{detail.name}</h1>
              {detail.allowed_tools.length > 0 && (
                <div className="flex flex-wrap gap-1">
                  {detail.allowed_tools.map((t) => (
                    <Badge key={t} tone="default">
                      {t}
                    </Badge>
                  ))}
                </div>
              )}
              <span className="ml-auto font-mono text-meta text-content-muted" title={detail.path}>
                {detail.path.split("/").slice(-2).join("/")}
              </span>
            </header>

            {detail.description && (
              <div className="border-b border-edge bg-surface-raised px-4 py-2">
                <p className="text-body text-content-secondary">{detail.description}</p>
              </div>
            )}

            {/* Content */}
            <div className="flex-1 overflow-y-auto px-4 py-3">
              <pre className="whitespace-pre-wrap break-words rounded border border-edge bg-surface-base p-4 font-mono text-body text-content-secondary leading-relaxed">
                {detail.content}
              </pre>
            </div>
          </>
        )}
      </main>
    </div>
  );
}
