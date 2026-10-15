/**
 * Hosted-analytics injection and before-send hook.
 *
 * `hostedAnalyticsInjector` gates the Umami tracker on STUDIO_UMAMI_WEBSITE_ID
 * and injects one inline head script: the before-send hook followed by the
 * tracker loader. These tests evaluate the shipped hook text (the exact
 * children the plugin returns) against the URLs the tracker produces, so the
 * assertions are about the string that reaches the deployed page, not a
 * reimplementation.
 *
 * The route-coverage block walks src/routes/ and fails, naming the file, when a
 * route has no template — the intent is that a new route cannot ship without a
 * template entry, because an untemplated path would then reach the tracker as a
 * raw path and page title rather than a collapsed template.
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
const LOADER_PATH = path.resolve(__dirname, "../../analytics/load-tracker.js");
// The plugin drops the formatter's trailing semicolon before appending its
// call, so the tested text is the bare IIFE too.
const LOADER_SOURCE = fs.readFileSync(LOADER_PATH, "utf-8").replace(/;\s*$/, "");

/** The tracker attributes the loader is called with, as the plugin builds them. */
const TRACKER_ATTRS = {
  defer: true,
  src: "https://analytics.khive.ai/script.js",
  "data-website-id": UUID,
  "data-domains": "lion-studio.khive.ai",
  "data-exclude-search": "true",
  "data-exclude-hash": "true",
  "data-before-send": "__studioAnalyticsBeforeSend",
};

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

  it("throws naming the variable, without echoing the value, when the id is not a UUID", () => {
    let message = "";
    try {
      hostedAnalyticsInjector({ STUDIO_UMAMI_WEBSITE_ID: "not-a-uuid" });
    } catch (error) {
      message = (error as Error).message;
    }
    expect(message).toContain("STUDIO_UMAMI_WEBSITE_ID");
    expect(message).toContain(String("not-a-uuid".length));
    expect(message).not.toContain("not-a-uuid");
  });

  it("emits exactly one tag, whose children evaluate to the deferred tracker tag", () => {
    const tags = tagsFor({ STUDIO_UMAMI_WEBSITE_ID: UUID });
    expect(tags).toHaveLength(1);
    expect(tags[0].tag).toBe("script");
    expect(tags[0].injectTo).toBe("head");

    // The children are the hook's source, then the loader call.
    const children = tags[0].children ?? "";
    expect(children.startsWith(HOOK_SOURCE)).toBe(true);
    expect(children).toContain(`${LOADER_SOURCE}(${JSON.stringify(TRACKER_ATTRS)});`);
    expect(children.indexOf(HOOK_SOURCE)).toBeLessThan(children.indexOf(LOADER_SOURCE));

    // Evaluating the children installs the hook and adds exactly one tracker
    // script element, with exactly the expected attributes.
    document.head.innerHTML = "";
    delete (window as unknown as { __studioAnalyticsBeforeSend?: unknown })
      .__studioAnalyticsBeforeSend;
    (window as unknown as { eval: (src: string) => void }).eval(children);
    const scripts = Array.from(document.head.querySelectorAll("script"));
    expect(scripts).toHaveLength(1);
    const script = scripts[0];
    expect(script.getAttribute("defer")).toBe("");
    expect(script.getAttribute("src")).toBe("https://analytics.khive.ai/script.js");
    expect(script.getAttribute("data-website-id")).toBe(UUID);
    expect(script.getAttribute("data-domains")).toBe("lion-studio.khive.ai");
    expect(script.getAttribute("data-exclude-search")).toBe("true");
    expect(script.getAttribute("data-exclude-hash")).toBe("true");
    expect(script.getAttribute("data-before-send")).toBe("__studioAnalyticsBeforeSend");
    expect(script.attributes.length).toBe(Object.keys(TRACKER_ATTRS).length);
    document.head.innerHTML = "";
  });

  it("evaluating the loader alone, with no hook defined, adds no element", () => {
    document.head.innerHTML = "";
    delete (window as unknown as { __studioAnalyticsBeforeSend?: unknown })
      .__studioAnalyticsBeforeSend;
    const evalWindow = window as unknown as { eval: (src: string) => void };
    evalWindow.eval(`(${LOADER_SOURCE})(${JSON.stringify(TRACKER_ATTRS)});`);
    expect(document.head.querySelectorAll("script")).toHaveLength(0);
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

  it("reduces a referrer from another origin to its origin only", () => {
    const hook = loadHook();
    const payload = {
      ...pageView("/runs/abc-123"),
      referrer: "https://example.com/invocations/ref-secret?x=1#h",
    };
    const result = hook("event", payload) as Record<string, string>;
    expect(result.referrer).toBe("https://example.com");
  });

  it("keeps another http origin when the referrer is http", () => {
    const hook = loadHook();
    const payload = {
      ...pageView("/runs/abc-123"),
      referrer: "http://example.com/invocations/ref-secret?x=1#h",
    };
    const result = hook("event", payload) as Record<string, string>;
    expect(result.referrer).toBe("http://example.com");
  });

  it("drops a referrer whose origin is opaque", () => {
    const hook = loadHook();
    for (const referrer of ["android-app://com.example.app/runs/secret", "about:blank"]) {
      const result = hook("event", {
        ...pageView("/runs/abc-123"),
        referrer,
      }) as Record<string, string>;
      expect(result.referrer).toBe("");
    }
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

  it("returns falsy for performance and for any other type", () => {
    const hook = loadHook();
    expect(hook("performance", pageView("/"))).toBeFalsy();
    expect(hook("unknown-type", pageView("/"))).toBeFalsy();
  });

  it("sends exactly the allowlisted page-view keys, dropping everything else", () => {
    const hook = loadHook();
    const payload = {
      website: "site-id",
      hostname: "lion-studio.khive.ai",
      screen: "1280x720",
      language: "en-US",
      url: `${ORIGIN}/runs/abc-123`,
      referrer: `${ORIGIN}/runs/def-456`,
      title: "whatever",
      id: "visitor-id",
      data: { x: 1 },
      tag: "tag-id",
      unknown: "surprise",
    };
    const result = hook("event", payload) as Record<string, string>;
    expect(Object.keys(result).sort()).toEqual([
      "hostname",
      "language",
      "referrer",
      "screen",
      "title",
      "url",
      "website",
    ]);
    expect(JSON.stringify(result)).not.toContain("visitor-id");
    expect(JSON.stringify(result)).not.toContain("tag-id");
    expect(JSON.stringify(result)).not.toContain("surprise");
  });

  it("copies an allowlisted field only when present", () => {
    const hook = loadHook();
    const result = hook("event", {
      url: `${ORIGIN}/fleet`,
      title: "whatever",
    }) as Record<string, string>;
    expect(result.website).toBeUndefined();
    expect(result.hostname).toBeUndefined();
    expect(result.screen).toBeUndefined();
    expect(result.language).toBeUndefined();
    expect(result.referrer).toBeUndefined();
  });

  it("templates the path for trailing slashes, double slashes, and encoded slashes", () => {
    const hook = loadHook();
    const cases: { input: string; template: string; raw: string }[] = [
      { input: "/fleet/", template: "/fleet", raw: "/fleet/" },
      { input: "/runs/abc/", template: "/runs/[id]", raw: "abc" },
      { input: "//runs/abc", template: "/(other)", raw: "abc" },
      { input: "/runs/a%2Fb", template: "/runs/[id]", raw: "a%2Fb" },
      { input: "/RUNS/abc", template: "/(other)", raw: "abc" },
      { input: "/playbooks/new/edit", template: "/playbooks/[name]/edit", raw: "new" },
      { input: "/runs/abc/extra", template: "/(other)", raw: "abc" },
    ];
    for (const { input, template, raw } of cases) {
      const result = hook("event", pageView(input)) as Record<string, string>;
      expect(result.url, `url for ${input}`).toBe(`${ORIGIN}${template}`);
      expect(result.title, `title for ${input}`).toBe(template);
      expect(result.url, `raw in url for ${input}`).not.toContain(raw);
      expect(result.title, `raw in title for ${input}`).not.toContain(raw);
    }
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
