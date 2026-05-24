"use client";

import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { listProjects } from "@/lib/api";
import type { ProjectSummary } from "@/lib/types";

export default function ProjectChip() {
  const router = useRouter();
  const pathname = usePathname() ?? "/";
  const searchParams = useSearchParams();
  const currentProject = searchParams.get("project") ?? "";

  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    listProjects()
      .then((data) => setProjects(data.projects))
      .catch(() => {});
  }, []);

  useEffect(() => {
    function handleOutsideClick(e: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    function handleEscape(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    if (open) {
      document.addEventListener("mousedown", handleOutsideClick);
      document.addEventListener("keydown", handleEscape);
    }
    return () => {
      document.removeEventListener("mousedown", handleOutsideClick);
      document.removeEventListener("keydown", handleEscape);
    };
  }, [open]);

  function selectProject(name: string) {
    const params = new URLSearchParams(searchParams.toString());
    if (name) {
      params.set("project", name);
    } else {
      params.delete("project");
    }
    const qs = params.toString();
    router.push(qs ? `${pathname}?${qs}` : pathname);
    setOpen(false);
  }

  const chipLabel = currentProject ? `[project: ${currentProject} ▾]` : "[All projects ▾]";

  return (
    <div ref={containerRef} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-label="Filter by project"
        aria-expanded={open}
        className="h-6 rounded border border-edge bg-surface-nav px-1.5 text-[11px] text-content-secondary focus:border-interactive-primary focus:outline-none hover:border-edge-strong transition-colors cursor-pointer"
      >
        {chipLabel}
      </button>

      {open && (
        <div className="absolute right-0 top-full z-50 mt-1 w-64 rounded border border-edge bg-surface-nav py-1 shadow-card">
          <button
            type="button"
            onClick={() => selectProject("")}
            className={[
              "flex w-full items-center justify-between px-3 py-2 text-left text-body transition-colors duration-150 hover:bg-surface-overlay hover:text-content-primary",
              currentProject === ""
                ? "font-semibold text-content-primary"
                : "text-content-secondary",
            ].join(" ")}
          >
            All projects
          </button>

          {projects.map((p) => (
            <button
              key={p.name}
              type="button"
              onClick={() => selectProject(p.name)}
              className={[
                "flex w-full items-center justify-between px-3 py-2 text-left text-body transition-colors duration-150 hover:bg-surface-overlay hover:text-content-primary",
                currentProject === p.name
                  ? "font-semibold text-content-primary"
                  : "text-content-secondary",
              ].join(" ")}
            >
              <span>{p.name}</span>
              {p.source && (
                <span className="ml-2 shrink-0 text-meta text-content-muted">{p.source}</span>
              )}
            </button>
          ))}

          <div className="mt-1 border-t border-edge pt-1">
            <Link
              href="/projects"
              onClick={() => setOpen(false)}
              className="block px-3 py-2 text-body text-content-secondary transition-colors duration-150 hover:bg-surface-overlay hover:text-content-primary"
            >
              View all projects
            </Link>
          </div>
        </div>
      )}
    </div>
  );
}
