import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";

interface DetailProject {
  sessionId: string;
  project: string | null;
}

type PublishProject = (detail: DetailProject) => () => void;
const DetailProjectContext = createContext<DetailProject | null>(null);
const PublishProjectContext = createContext<PublishProject>(() => () => {});

/** Shares the existing detail response with the sibling Operator panel without
 * fetching run lists or guessing a selected run's project from list filters. */
export function OperatorProjectProvider({ children }: { children: ReactNode }) {
  const [detail, setDetail] = useState<DetailProject | null>(null);
  const publish = useCallback((next: DetailProject) => {
    setDetail(next);
    // An older detail's cleanup must not remove a newer detail's publication.
    return () => setDetail((current) => (current === next ? null : current));
  }, []);

  return (
    <PublishProjectContext.Provider value={publish}>
      <DetailProjectContext.Provider value={detail}>{children}</DetailProjectContext.Provider>
    </PublishProjectContext.Provider>
  );
}

export function useReportOperatorProject(sessionId: string | null, project?: string | null) {
  const publish = useContext(PublishProjectContext);
  useEffect(() => {
    if (!sessionId) return;
    return publish({ sessionId, project: project || null });
  }, [sessionId, project, publish]);
}

export function useOperatorProject(pathname: string, search: Record<string, unknown>) {
  const detail = useContext(DetailProjectContext);
  if (pathname !== "/fleet" && pathname !== "/fleet/") return null;
  if (search.s != null) {
    return typeof search.s === "string" && detail?.sessionId === search.s ? detail.project : null;
  }
  // An invocation link can still be resolving its session selection. Only an
  // unselected list may use the router's scalar project filter as its scope.
  if (search.invocation != null || search.project_null === true) return null;
  const project = typeof search.project === "string" ? search.project.trim() : "";
  // Match the turn-context limit in Unicode code points, as the server does.
  return project && [...project].length <= 512 ? project : null;
}
