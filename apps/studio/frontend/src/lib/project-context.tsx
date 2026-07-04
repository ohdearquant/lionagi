import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  type ReactNode,
} from "react";
import { useRouter, useRouterState } from "@tanstack/react-router";

const STORAGE_KEY = "studio.project";

export interface ProjectContextValue {
  /** Selected project name, or "" for "All projects". */
  project: string;
  setProject: (name: string) => void;
}

const ProjectContext = createContext<ProjectContextValue | null>(null);

function readStoredProject(): string {
  try {
    return window.localStorage.getItem(STORAGE_KEY) ?? "";
  } catch {
    return "";
  }
}

/**
 * Shares the active project scope across the app. The URL's `?project=`
 * search param is the single source of truth (read live via router state) so
 * a change made through this context OR through a page's own project filter
 * (e.g. /runs, which already validates its own `project` search param) stays
 * in sync everywhere — including the localStorage-backed default a fresh tab
 * starts from.
 */
export function ProjectProvider({ children }: { children: ReactNode }) {
  const router = useRouter();
  const project = useRouterState({
    select: (s) => {
      const search = s.location.search as Record<string, unknown>;
      return typeof search.project === "string" ? search.project : "";
    },
  });

  // Routed through the router's own navigate (not raw history.pushState) so
  // pages reading `project` via a typed `useSearch()` re-render reactively —
  // the root route has no search schema of its own, so the reducer's input
  // and output are widened to a plain record rather than inferred per-route.
  const setProject = useCallback(
    (name: string) => {
      void router.navigate({
        to: router.state.location.pathname,
        search: (prev: Record<string, unknown>) => ({ ...prev, project: name || undefined }),
        replace: true,
      });
    },
    [router],
  );

  // Persist every effective project change (from any source) so the next
  // fresh tab without a `?project=` link starts from the last used scope.
  useEffect(() => {
    try {
      if (project) window.localStorage.setItem(STORAGE_KEY, project);
      else window.localStorage.removeItem(STORAGE_KEY);
    } catch {
      // localStorage unavailable (private browsing) — URL state still works
    }
  }, [project]);

  // First mount only: the URL didn't carry ?project= but a prior selection
  // was stored — adopt it so the address bar reflects the active scope from
  // the first render (shareable links stay accurate after a hard refresh).
  const didInit = useRef(false);
  useEffect(() => {
    if (didInit.current) return;
    didInit.current = true;
    if (!project) {
      const stored = readStoredProject();
      if (stored) setProject(stored);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- run once on mount
  }, []);

  const value = useMemo(() => ({ project, setProject }), [project, setProject]);

  return <ProjectContext.Provider value={value}>{children}</ProjectContext.Provider>;
}

export function useProject(): ProjectContextValue {
  const ctx = useContext(ProjectContext);
  if (!ctx) throw new Error("useProject must be used within a ProjectProvider");
  return ctx;
}
