import {
  listAgents,
  listBuiltinPlaybooks,
  listMcpServers,
  listPlaybooks,
  listPlugins,
  listSkills,
} from "@/lib/api";
import type { EngineDef } from "@/lib/api";
import type { AgentProfileSummary } from "@/lib/types";
import type { LibraryKind } from "./KindBadge";

// "hooks" is a tab with its own view rather than a kind of library item, so it
// matches no catalog and correctly loads nothing. It is admitted here so the
// tab union and the loader's input stay the same set.
export type LibraryDataTab = "all" | LibraryKind | "hooks";
export type PlaybookSubKind = "builtin" | "custom";

export interface LibraryItem {
  key: string;
  kind: LibraryKind;
  subKind?: PlaybookSubKind;
  name: string;
  description?: string;
  meta?: string;
}

export interface LibraryCatalogResult {
  items: LibraryItem[];
  allAgents: AgentProfileSummary[];
  /** Always empty: no request loads engine defs while the engine tab is unfinished. */
  allEngines: EngineDef[];
  degraded: boolean;
}

interface CatalogSlice {
  items: LibraryItem[];
  agents?: AgentProfileSummary[];
}

const wants = (active: LibraryDataTab, kind: LibraryKind) => active === "all" || active === kind;

/**
 * Load only the catalogs that can populate the active Library tab. Workflow
 * and engine remain intentionally absent while their tabs are unfinished.
 */
export async function loadLibraryCatalogs(tab: LibraryDataTab): Promise<LibraryCatalogResult> {
  const requests: Array<Promise<CatalogSlice>> = [];

  if (wants(tab, "agent")) {
    requests.push(
      listAgents().then(({ agents }) => ({
        agents,
        items: agents.map((agent) => ({
          key: `agent:${agent.name}`,
          kind: "agent" as const,
          name: agent.name,
          description: agent.description ?? undefined,
          meta: agent.model ?? undefined,
        })),
      })),
    );
  }

  if (wants(tab, "playbook")) {
    requests.push(
      listBuiltinPlaybooks().then(({ playbooks }) => ({
        items: playbooks.map((playbook) => ({
          key: `playbook:builtin:${playbook.name}`,
          kind: "playbook" as const,
          subKind: "builtin" as const,
          name: playbook.name,
          description: playbook.description,
          meta: playbook.description,
        })),
      })),
      listPlaybooks().then(({ playbooks }) => ({
        items: playbooks.map((playbook) => ({
          key: `playbook:custom:${playbook.name}`,
          kind: "playbook" as const,
          subKind: "custom" as const,
          name: playbook.name,
          description: playbook.description ?? undefined,
          meta: playbook.description ?? undefined,
        })),
      })),
    );
  }

  if (wants(tab, "skill")) {
    requests.push(
      listSkills().then(({ skills }) => ({
        items: skills.map((skill) => ({
          key: `skill:${skill.name}`,
          kind: "skill" as const,
          name: skill.name,
          description: skill.description ?? undefined,
        })),
      })),
    );
  }

  if (wants(tab, "plugin")) {
    requests.push(
      listPlugins().then(({ plugins }) => {
        const seen = new Set<string>();
        const items: LibraryItem[] = [];
        for (const plugin of plugins) {
          if (seen.has(plugin.name)) continue;
          seen.add(plugin.name);
          items.push({
            key: `plugin:${plugin.name}`,
            kind: "plugin",
            name: plugin.name,
            description: plugin.description ?? undefined,
            meta: `v${plugin.version}`,
          });
        }
        return { items };
      }),
    );
  }

  if (wants(tab, "mcp")) {
    requests.push(
      listMcpServers().then(({ servers }) => ({
        items: servers.map((server) => ({
          key: `mcp:${server.name}`,
          kind: "mcp" as const,
          name: server.name,
          description: server.command ?? server.url ?? undefined,
          meta: server.enabled ? server.transport : `${server.transport} · disabled`,
        })),
      })),
    );
  }

  const settled = await Promise.allSettled(requests);
  const result: LibraryCatalogResult = {
    items: [],
    allAgents: [],
    allEngines: [],
    degraded: false,
  };
  for (const entry of settled) {
    if (entry.status === "rejected") {
      result.degraded = true;
      continue;
    }
    result.items.push(...entry.value.items);
    if (entry.value.agents) result.allAgents = entry.value.agents;
  }
  return result;
}
