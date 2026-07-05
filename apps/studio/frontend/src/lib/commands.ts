export interface Command {
  id: string;
  label: string;
  labelZh?: string;
  keywords?: string[];
  href?: string;
  action?: () => void;
  section?: string;
}

export type CommandRegistry = Command[];

export function buildRegistry(
  navigate: (href: string) => void,
  toggleTheme: () => void,
  toggleLocale: () => void,
): CommandRegistry {
  return [
    /* ── Primary spaces ── */
    {
      id: "nav:home",
      label: "Mission Control",
      labelZh: "任务中心",
      keywords: ["home", "dashboard", "overview"],
      href: "/",
      section: "Navigate",
    },
    {
      id: "nav:designer",
      label: "Designer",
      labelZh: "设计器",
      keywords: ["canvas", "graph", "playbook", "workflow", "dag"],
      href: "/designer",
      section: "Navigate",
    },
    {
      id: "nav:library",
      label: "Library",
      labelZh: "资源库",
      keywords: ["agents", "playbooks", "skills", "plugins", "catalog"],
      href: "/library",
      section: "Navigate",
    },
    {
      id: "nav:history",
      label: "History",
      labelZh: "历史",
      keywords: ["runs", "invocations", "shows", "timeline"],
      href: "/history",
      section: "Navigate",
    },
    {
      id: "nav:schedules",
      label: "Schedules",
      labelZh: "计划任务",
      keywords: ["schedules", "cron", "automation", "calendar", "kanban"],
      href: "/schedules",
      section: "Navigate",
    },
    {
      id: "nav:system",
      label: "System",
      labelZh: "系统",
      keywords: ["health", "maintenance", "settings"],
      href: "/system",
      section: "Navigate",
    },
    /* ── Space tabs ── */
    {
      id: "nav:fleet",
      label: "Fleet",
      labelZh: "调度中心",
      keywords: ["fleet", "agents", "monitor", "live", "orchestration"],
      href: "/fleet",
      section: "Navigate",
    },
    {
      id: "lib:workflows",
      label: "Workflows",
      labelZh: "工作流",
      keywords: ["workflows", "dag", "pipeline"],
      href: "/library?tab=workflow",
      section: "Library",
    },
    {
      id: "lib:playbooks",
      label: "Playbooks",
      labelZh: "剧本",
      keywords: ["playbooks", "workers"],
      href: "/library?tab=playbook",
      section: "Library",
    },
    {
      id: "lib:agents",
      label: "Agents",
      labelZh: "智能体",
      keywords: ["agents"],
      href: "/library?tab=agent",
      section: "Library",
    },
    {
      id: "lib:skills",
      label: "Skills",
      labelZh: "技能",
      keywords: ["skills"],
      href: "/library?tab=skill",
      section: "Library",
    },
    {
      id: "lib:plugins",
      label: "Plugins",
      labelZh: "插件",
      keywords: ["plugins"],
      href: "/library?tab=plugin",
      section: "Library",
    },
    /* ── Actions ── */
    {
      id: "action:new-playbook",
      label: "New Playbook",
      labelZh: "新建剧本",
      keywords: ["new", "create", "playbook"],
      action: () => navigate("/designer"),
      section: "Actions",
    },
    {
      id: "action:toggle-theme",
      label: "Toggle theme",
      labelZh: "切换主题",
      keywords: ["theme", "dark", "light"],
      action: toggleTheme,
      section: "Actions",
    },
    {
      id: "action:toggle-locale",
      label: "Switch language",
      labelZh: "切换语言",
      keywords: ["language", "locale", "chinese", "english"],
      action: toggleLocale,
      section: "Actions",
    },
  ];
}

export function fuzzyMatch(query: string, cmd: Command): boolean {
  if (!query) return true;
  const q = query.toLowerCase();
  const haystack = [cmd.label, cmd.labelZh ?? "", ...(cmd.keywords ?? []), cmd.id]
    .join(" ")
    .toLowerCase();
  const words = q.split(/\s+/).filter(Boolean);
  return words.every((w) => haystack.includes(w));
}
