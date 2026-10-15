/**
 * Hosted-analytics injection and before-send hook.
 *
 * `hostedAnalyticsInjector` gates the Umami tracker on STUDIO_UMAMI_WEBSITE_ID
 * and injects two head tags: the inline before-send hook, then the deferred
 * tracker. These tests evaluate the shipped hook text (the exact children the
 * plugin returns) against the URLs the tracker produces, so the assertions are
 * about the string that reaches the deployed page, not a reimplementation.
 *
 * The route-coverage block walks src/routes/ and fails, naming the file, when a
 * route has no template — the intent is that a new route cannot ship without a
 * template entry, because an untemplated id would then reach the tracker.
 */

import { describe, it, expect, vi } from "vitest";
import * as fs from "node:fs";
import * as path from "node:path";

vi.mock("@tanstack/router-plugin/vite", () => ({
  TanStackRouterVite: () => ({ name: "router-test-plugin" }),
}));

import { hostedAnalyticsInjector } from "../../vite.config.mjs";

const UUID = "00000000-0000-4000-8000-000000000000";
const HOOK_PATH = path.resolve(__dirname, "../../analytics/before-send.js");
const HOOK_SOURCE = fs.readFileSync(HOOK_PATH, "utf-8");

type HtmlTag = {
  tag: string;
  attrs?: Record<string, string | boolean | undefined>;
  children?: string;
  injectTo?: string;
};

function tagsFor(env: NodeJS.ProcessEnv): HtmlTag[] {
  const transform = hostedAnalyticsInjector(env).transformIndexHtml as () => HtmlTag[] | undefined;
  return (transform() ?? []) as HtmlTag[];
}

/** Runs the shipped hook source in the jsdom window and returns the hook. */
function loadHook(): (type: string, payload: Record<string, unknown>) => unknown {
  (window as unknown as { eval: (src: string) => void }).eval(HOOK_SOURCE);
  const hook = (window as unknown as { __studioAnalyticsBeforeSend?: unknown })
    .__studioAnalyticsBeforeSend;
  if (typeof hook !== "function") throw new Error("hook was not installed");
  return hook as (type: string, payload: Record<string, unknown>) => unknown;
}

const ORIGIN = window.location.origin;

function pageView(pathnameAndRest: string) {
  return { url: ORIGIN + pathnameAndRest, title: "whatever", referrer: "" };
}

describe("hostedAnalyticsInjector", () => {
  it("injects nothing when the website id is unset", () => {
    expect(tagsFor({})).toEqual([]);
  });

  it("injects nothing when the website id is empty", () => {
    expect(tagsFor({ STUDIO_UMAMI_WEBSITE_ID: "" })).toEqual([]);
  });

  it("throws naming the variable when the website id is not a UUID", () => {
    expect(() => hostedAnalyticsInjector({ STUDIO_UMAMI_WEBSITE_ID: "not-a-uuid" })).toThrow(
      /STUDIO_UMAMI_WEBSITE_ID/,
    );
  });

  it("emits the hook then the tracker, in order, with exact attributes", () => {
    const tags = tagsFor({ STUDIO_UMAMI_WEBSITE_ID: UUID });
    expect(tags).toHaveLength(2);

    expect(tags[0]).toEqual({
      tag: "script",
      children: HOOK_SOURCE,
      injectTo: "head",
    });

    expect(tags[1]).toEqual({
      tag: "script",
      attrs: {
        defer: true,
        src: "https://analytics.khive.ai/script.js",
        "data-website-id": UUID,
        "data-domains": "lion-studio.khive.ai",
        "data-exclude-search": "true",
        "data-exclude-hash": "true",
        "data-before-send": "__studioAnalyticsBeforeSend",
      },
      injectTo: "head",
    });
  });

  it("does not inject analytics for a plain build even with VERCEL set", () => {
    expect(tagsFor({ VERCEL: "1" })).toEqual([]);
  });
});

describe("before-send hook", () => {
  it("scrubs a page view of a dynamic route to url and title templates", () => {
    const hook = loadHook();
    const result = hook("event", pageView("/runs/abc-123?q=1#x")) as Record<string, string>;
    expect(result.url).toBe(`${ORIGIN}/runs/[id]`);
    expect(result.title).toBe("/runs/[id]");
  });

  it("templates a same-origin referrer", () => {
    const hook = loadHook();
    const payload = { ...pageView("/runs/abc-123"), referrer: `${ORIGIN}/invocations/xyz` };
    const result = hook("event", payload) as Record<string, string>;
    expect(result.referrer).toBe(`${ORIGIN}/invocations/[id]`);
  });

  it("keeps a referrer from another origin as-is", () => {
    const hook = loadHook();
    const payload = {
      ...pageView("/runs/abc-123"),
      referrer: "https://example.com/invocations/xyz",
    };
    const result = hook("event", payload) as Record<string, string>;
    expect(result.referrer).toBe("https://example.com/invocations/xyz");
  });

  it("keeps the static /playbooks/new and templates /playbooks/foo/edit", () => {
    const hook = loadHook();
    expect((hook("event", pageView("/playbooks/new")) as Record<string, string>).url).toBe(
      `${ORIGIN}/playbooks/new`,
    );
    expect((hook("event", pageView("/playbooks/foo/edit")) as Record<string, string>).url).toBe(
      `${ORIGIN}/playbooks/[name]/edit`,
    );
  });

  it("templates a path-only url and referrer the same way as absolute ones", () => {
    const hook = loadHook();
    const result = hook("event", {
      url: "/runs/abc-123",
      title: "x",
      referrer: "/playbooks/secret-name/edit",
    }) as Record<string, string>;
    expect(result.url).toBe(`${ORIGIN}/runs/[id]`);
    expect(result.referrer).toBe(`${ORIGIN}/playbooks/[name]/edit`);
  });

  it("collapses an unknown path to /(other)", () => {
    const hook = loadHook();
    expect((hook("event", pageView("/not/a/real/route")) as Record<string, string>).url).toBe(
      `${ORIGIN}/(other)`,
    );
  });

  it("returns falsy for identify and for a named custom event", () => {
    const hook = loadHook();
    expect(hook("identify", { ...pageView("/"), data: { x: 1 } })).toBeFalsy();
    expect(hook("event", { ...pageView("/"), name: "button-click", data: {} })).toBeFalsy();
  });

  it("does not throw on an unparsable url, and sends nothing", () => {
    const hook = loadHook();
    let result: unknown;
    expect(() => {
      result = hook("event", { url: "https://[not-a-host/runs/abc", title: "x", referrer: "" });
    }).not.toThrow();
    expect(result).toBeFalsy();
  });

  it("reports a url that is not a route path as /(other), never the raw text", () => {
    const hook = loadHook();
    const result = hook("event", { url: "{not a url}", title: "x", referrer: "" }) as Record<
      string,
      string
    >;
    expect(result.url).toBe(`${ORIGIN}/(other)`);
    expect(result.title).toBe("/(other)");
  });
});

// ─── Route coverage: every src/routes/ file maps to a template ──────────────

const ROUTES_DIR = path.resolve(__dirname, "../routes");

/** Static route list the shipped hook actually contains. */
function hookStaticRoutes(): string[] {
  const match = HOOK_SOURCE.match(/var STATIC_ROUTES = \[([\s\S]*?)\];/);
  if (!match) throw new Error("STATIC_ROUTES not found in the hook source");
  return new Function(`return [${match[1]}];`)() as string[];
}

function listRouteFiles(dir: string, prefix = ""): string[] {
  const out: string[] = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const rel = prefix ? `${prefix}/${entry.name}` : entry.name;
    if (entry.isDirectory()) {
      out.push(...listRouteFiles(path.join(dir, entry.name), rel));
    } else if (entry.name.startsWith("-") || entry.name === "__root.tsx") {
      continue; // router ignore prefix and the root layout
    } else {
      out.push(rel);
    }
  }
  return out;
}

interface RouteMapping {
  file: string;
  /** Route path with `$param` segments left intact. */
  route: string;
  /** Expected template: `$param` -> `[param]`. */
  template: string;
  /** Sample path: `$param` -> a sample value. */
  sample: string;
  dynamic: boolean;
}

function mapRouteFile(file: string): RouteMapping {
  const dir = path.dirname(file);
  const base = path.basename(file, path.extname(file));
  const segments: string[] = [];
  if (dir !== ".") segments.push(...dir.split(path.sep));
  if (base !== "index") segments.push(base);

  let dynamic = false;
  const routeSegments = segments.map((segment) => {
    if (segment.startsWith("_")) throw new Error(`${file}: unhandled layout segment "${segment}"`);
    if (segment.includes(".")) throw new Error(`${file}: unhandled dotted file name "${segment}"`);
    if (segment.includes("(") || segment.includes(")"))
      throw new Error(`${file}: unhandled group segment "${segment}"`);
    if (segment === "$") throw new Error(`${file}: unhandled splat segment "${segment}"`);
    if (segment.startsWith("$")) {
      dynamic = true;
      return segment;
    }
    return segment;
  });

  const route = `/${routeSegments.join("/")}`.replace(/^\/$/, "/");
  const template = `/${routeSegments
    .map((segment) => (segment.startsWith("$") ? `[${segment.slice(1)}]` : segment))
    .join("/")}`.replace(/^\/$/, "/");
  const sample = `/${routeSegments
    .map((segment) => (segment.startsWith("$") ? "sample-value" : segment))
    .join("/")}`.replace(/^\/$/, "/");

  return { file, route, template, sample, dynamic };
}

describe("route coverage", () => {
  const files = listRouteFiles(ROUTES_DIR);
  const routes = files.map(mapRouteFile);

  it("finds the known static routes", () => {
    const routePaths = routes.map((r) => r.route);
    for (const expected of ["/", "/admin", "/admin/health", "/playbooks/new", "/runs", "/teams"]) {
      expect(routePaths).toContain(expected);
    }
  });

  it("every route resolves to its expected template, never /(other)", () => {
    const hook = loadHook();
    for (const route of routes) {
      const result = hook("event", pageView(route.sample)) as Record<string, string>;
      expect(result.url, `route file ${route.file} fell through to /(other)`).toBe(
        `${ORIGIN}${route.template}`,
      );
      expect(result.url).not.toBe(`${ORIGIN}/(other)`);
      expect(result.title, `title for ${route.file}`).toBe(route.template);
    }
  });

  it("the hook's static list contains every static route file", () => {
    const statics = hookStaticRoutes();
    for (const route of routes.filter((r) => !r.dynamic)) {
      expect(statics, `static route ${route.template} (${route.file}) missing from hook`).toContain(
        route.template,
      );
    }
  });

  it("every entry in the hook's static list still has a route file", () => {
    const staticRoutePaths = new Set(routes.filter((r) => !r.dynamic).map((r) => r.template));
    for (const entry of hookStaticRoutes()) {
      expect(staticRoutePaths, `hook lists ${entry} but no route file maps to it`).toContain(entry);
    }
  });
});
