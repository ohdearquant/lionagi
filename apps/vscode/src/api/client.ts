import type {
  LaunchRequest,
  LaunchResult,
  ProjectGroupsPage,
  Run,
  RunsPage,
} from "./types.js";

export class StudioApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly detail: string
  ) {
    super(`Studio API error ${status}: ${detail}`);
    this.name = "StudioApiError";
  }
}

export interface ListRunsOptions {
  page?: number;
  per_page?: number;
  status?: string;
  playbook?: string;
  project?: string;
  project_null?: boolean;
}

export class StudioClient {
  constructor(
    private readonly getBaseUrl: () => string,
    private readonly getToken: () => string | undefined
  ) {}

  private headers(): Record<string, string> {
    const h: Record<string, string> = { "Content-Type": "application/json" };
    const token = this.getToken();
    if (token) {
      h["Authorization"] = `Bearer ${token}`;
    }
    return h;
  }

  private url(path: string): string {
    return `${this.getBaseUrl()}${path}`;
  }

  private async request<T>(
    method: string,
    path: string,
    body?: unknown
  ): Promise<T> {
    const res = await fetch(this.url(path), {
      method,
      headers: this.headers(),
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    if (!res.ok) {
      let detail = res.statusText;
      try {
        const json = (await res.json()) as { detail?: string };
        if (json.detail) {
          detail = json.detail;
        }
      } catch {
        // ignore parse errors on error body
      }
      throw new StudioApiError(res.status, detail);
    }
    return res.json() as Promise<T>;
  }

  async listRuns(opts: ListRunsOptions = {}): Promise<RunsPage> {
    const params = new URLSearchParams();
    if (opts.page !== undefined) {
      params.set("page", String(opts.page));
    }
    if (opts.per_page !== undefined) {
      params.set("per_page", String(opts.per_page));
    }
    if (opts.status) {
      params.set("status", opts.status);
    }
    if (opts.playbook) {
      params.set("playbook", opts.playbook);
    }
    if (opts.project) {
      params.set("project", opts.project);
    }
    if (opts.project_null) {
      params.set("project_null", "true");
    }
    const qs = params.toString();
    return this.request<RunsPage>("GET", `/api/runs/${qs ? `?${qs}` : ""}`);
  }

  async listProjectGroups(): Promise<ProjectGroupsPage> {
    return this.request<ProjectGroupsPage>("GET", "/api/runs/projects");
  }

  async getRun(runId: string): Promise<Run> {
    return this.request<Run>("GET", `/api/runs/${runId}`);
  }

  async launch(req: LaunchRequest): Promise<LaunchResult> {
    return this.request<LaunchResult>("POST", "/api/launches/", req);
  }
}
