"use client";

import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { useEffect, useId, useRef, useState } from "react";
import { listProjects } from "@/lib/api";
import type { ProjectSummary } from "@/lib/types";

export default function ProjectChip() {
  const t = useTranslations("nav");
  const router = useRouter();
  const pathname = usePathname() ?? "/";
  const searchParams = useSearchParams();
  const currentProject = searchParams.get("project") ?? "";

  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const menuId = useId();

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

  function focusMenuItem(index: number) {
    const items = menuRef.current?.querySelectorAll<HTMLElement>(
      "[role='menuitem'], [role='menuitemradio']",
    );
    if (!items || items.length === 0) return;
    const i = (index + items.length) % items.length;
    items[i]?.focus();
  }

  function handleTriggerKeyDown(e: React.KeyboardEvent<HTMLButtonElement>) {
    if (e.key === "ArrowDown" || e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      setOpen(true);
      setTimeout(() => focusMenuItem(0), 0);
    } else if (e.key === "Escape") {
      setOpen(false);
    }
  }

  function handleMenuKeyDown(e: React.KeyboardEvent<HTMLDivElement>) {
    const items = menuRef.current?.querySelectorAll<HTMLElement>(
      "[role='menuitem'], [role='menuitemradio']",
    );
    if (!items || items.length === 0) return;
    const active = document.activeElement as HTMLElement | null;
    const currentIndex = Array.from(items).findIndex((el) => el === active);
    if (e.key === "ArrowDown") {
      e.preventDefault();
      focusMenuItem(currentIndex + 1);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      focusMenuItem(currentIndex - 1);
    } else if (e.key === "Home") {
      e.preventDefault();
      focusMenuItem(0);
    } else if (e.key === "End") {
      e.preventDefault();
      focusMenuItem(items.length - 1);
    } else if (e.key === "Escape" || e.key === "Tab") {
      setOpen(false);
    }
  }

  const chipLabel = currentProject
    ? t("projectChip.selected", { name: currentProject })
    : t("projectChip.allShort");

  return (
    <div ref={containerRef} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        onKeyDown={handleTriggerKeyDown}
        aria-label={t("projectChip.ariaLabel")}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={menuId}
        className="h-6 rounded border border-edge bg-surface-nav px-1.5 text-[11px] text-content-secondary focus:border-interactive-primary focus:outline-none hover:border-edge-strong transition-colors cursor-pointer"
      >
        {chipLabel}
      </button>

      {open && (
        // eslint-disable-next-line jsx-a11y/interactive-supports-focus -- TODO(#1020 follow-up): menu container; focus goes to menuitem children per ARIA spec
        <div
          ref={menuRef}
          id={menuId}
          role="menu"
          aria-label={t("projectChip.ariaLabel")}
          onKeyDown={handleMenuKeyDown}
          className="absolute right-0 top-full z-50 mt-1 w-64 rounded border border-edge bg-surface-nav py-1 shadow-card"
        >
          <button
            type="button"
            role="menuitemradio"
            aria-checked={currentProject === ""}
            onClick={() => selectProject("")}
            className={[
              "flex w-full items-center justify-between px-3 py-2 text-left text-body transition-colors duration-150 hover:bg-surface-overlay hover:text-content-primary focus:bg-surface-overlay focus:outline-none",
              currentProject === ""
                ? "font-semibold text-content-primary"
                : "text-content-secondary",
            ].join(" ")}
          >
            {t("projectChip.all")}
          </button>

          {projects.map((p) => (
            <button
              key={p.name}
              type="button"
              role="menuitemradio"
              aria-checked={currentProject === p.name}
              onClick={() => selectProject(p.name)}
              className={[
                "flex w-full items-center justify-between px-3 py-2 text-left text-body transition-colors duration-150 hover:bg-surface-overlay hover:text-content-primary focus:bg-surface-overlay focus:outline-none",
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
              role="menuitem"
              onClick={() => setOpen(false)}
              className="block px-3 py-2 text-body text-content-secondary transition-colors duration-150 hover:bg-surface-overlay hover:text-content-primary focus:bg-surface-overlay focus:outline-none"
            >
              {t("projectChip.viewAll")}
            </Link>
          </div>
        </div>
      )}
    </div>
  );
}
