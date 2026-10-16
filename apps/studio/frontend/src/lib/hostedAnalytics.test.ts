/**
 * Hosted-analytics injection and before-send hook.
 *
 * `hostedAnalyticsInjector` gates the Umami tracker on the hosted production
 * build (VERCEL=1 and VERCEL_ENV=production) together with a
 * STUDIO_UMAMI_WEBSITE_ID, and injects one inline head
 * script: the before-send hook followed by the tracker loader. These tests
 * evaluate the shipped hook text (the exact children the plugin returns) against
 * the values the page and the tracker produce, so the assertions are about the
 * string that reaches the deployed page, not a reimplementation.
 *
 * The tracker the page loads is pinned by Subresource Integrity; the copy of that
 * build at analytics/umami-script.pinned.js exists so a test fails when the copy
 * and the hash constant disagree. Whether the host still serves that build is
 * not checked here: when it changes, the browser refuses the script.
 *
 * The route-coverage block walks src/routes/ and fails, naming the file, when a
 * route has no template — the intent is that a new route cannot ship without a
 * template entry, because its pages would otherwise be reported as `/(other)`,
 * never under their own template.
 */

import { describe, it, expect, vi } from "vitest";
import * as crypto from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";

vi.mock("@tanstack/router-plugin/vite", () => ({
  TanStackRouterVite: () => ({ name: "router-test-plugin" }),
}));

import { hostedAnalyticsInjector, TRACKER_INTEGRITY } from "../../vite.config.mjs";

const UUID = "00000000-0000-4000-8000-000000000000";
const HOSTED_ENV: NodeJS.ProcessEnv = {
  VERCEL: "1",
  VERCEL_ENV: "production",
  STUDIO_UMAMI_WEBSITE_ID: UUID,
};
const HOOK_PATH = path.resolve(__dirname, "../../analytics/before-send.js");
const HOOK_SOURCE = fs.readFileSync(HOOK_PATH, "utf-8");
const LOADER_PATH = path.resolve(__dirname, "../../analytics/load-tracker.js");
// The plugin drops the formatter's trailing semicolon before wrapping the loader
// in parentheses and calling it, so the tested text is the bare expression too.
const LOADER_SOURCE = fs.readFileSync(LOADER_PATH, "utf-8").trim().replace(/;$/, "");

// The pinned tracker build without its header line: the bytes the host serves
// today, and the bytes the integrity pin covers.
const PINNED_PATH = path.resolve(__dirname, "../../analytics/umami-script.pinned.js");
const PINNED_FILE = fs.readFileSync(PINNED_PATH, "utf-8");
const PINNED_TRACKER = PINNED_FILE.slice(PINNED_FILE.indexOf("\n") + 1);
const PINNED_SHA384 = `sha384-${crypto.createHash("sha384").update(PINNED_TRACKER).digest("base64")}`;

/** The tracker attributes the loader is called with, as the plugin builds them. */
const TRACKER_ATTRS = {
  src: "https://analytics.khive.ai/script.js",
  integrity: TRACKER_INTEGRITY,
  crossorigin: "anonymous",
  "data-website-id": UUID,
  "data-domains": "lion-studio.khive.ai",
  "data-exclude-search": "true",
  "data-exclude-hash": "true",
  "data-do-not-track": "true",
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
  document.head.innerHTML = "";
  (window as unknown as { eval: (src: string) => void }).eval(HOOK_SOURCE);
  const hook = (window as unknown as { __studioAnalyticsBeforeSend?: unknown })
    .__studioAnalyticsBeforeSend;
  if (typeof hook !== "function") throw new Error("hook was not installed");
  return hook as (type: string, payload: Record<string, unknown>) => unknown;
}

/**
 * A script element shaped like the one the loader creates, so the hook can read
 * the website id the page was built with. The hook sends nothing without it. Its
 * `data-domains` names the host this test page is on: that restriction belongs
 * to the tracker, and in jsdom the page is not on the hosted domain, so the
 * shipped value would make the tracker drop every send before the hook runs.
 * The element carries no `src`, because jsdom does not fetch external scripts --
 * a test that needs the tracker to run evaluates the pinned copy as its text,
 * the way an inline script would.
 */
function addTrackerElement(text = "", websiteId: string = UUID): HTMLScriptElement {
  const element = document.createElement("script");
  element.setAttribute("data-website-id", websiteId);
  element.setAttribute("data-domains", window.location.hostname);
  element.setAttribute("data-before-send", "__studioAnalyticsBeforeSend");
  // jsdom runs a script element's text only when the text is already on it as it
  // is inserted, so the text goes on before the append.
  if (text) element.textContent = text;
  document.head.appendChild(element);
  return element;
}

/** Loads the hook with the tracker element present, as the built page has it. */
function loadHookOnPage(): (type: string, payload: Record<string, unknown>) => unknown {
  const hook = loadHook();
  addTrackerElement();
  return hook;
}

const ORIGIN = window.location.origin;

function pageView(pathnameAndRest: string) {
  return { url: ORIGIN + pathnameAndRest, title: "whatever", referrer: "" };
}

const evalWindow = window as unknown as { eval: (src: string) => void };

/** Lets any awaited tracker work settle. */
function flush(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

type Sent = { type: string; payload: Record<string, unknown> };

/**
 * Adds an inline script element carrying `source`. Script elements execute in
 * the jsdom window's own realm, so this is also how the tests enter that realm:
 * evaluating the same text from the test file would run it against the test
 * runner's scope instead.
 */
function addInlineScript(source: string): HTMLScriptElement {
  const element = document.createElement("script");
  element.textContent = source;
  document.head.appendChild(element);
  return element;
}

/**
 * Installs the shipped hook and runs the pinned tracker build, the way the built
 * page does, and returns a `track` that calls through the tracker with the given
 * argument source and resolves with the bodies the tracker posted. The tracker's
 * own automatic first page view is drained before anything is returned, so each
 * call resolves with only the sends its own call produced.
 *
 * The tracker element carries no `src` here: jsdom does not fetch external
 * scripts, so a test that needs the tracker to run evaluates the pinned copy as
 * the element's text, the way the browser would run the same bytes it fetched.
 */
async function trackerPage(): Promise<{
  track: (argumentSource: string) => Promise<Sent[]>;
}> {
  document.head.innerHTML = "";
  forgetTracker();
  const sent: Sent[] = [];
  (document.documentElement as unknown as Record<string, unknown>).__analyticsSent = sent;

  addInlineScript(`window.fetch = function (url, init) {
    document.documentElement.__analyticsSent.push(JSON.parse(init.body));
    return Promise.resolve({ json: function () { return Promise.resolve({}); } });
  };`);
  addInlineScript(HOOK_SOURCE);
  addTrackerElement(PINNED_TRACKER);

  for (let i = 0; i < 50 && sent.length === 0; i += 1) await flush();
  expect(sent.length, "the pinned tracker never ran").toBeGreaterThan(0);
  sent.length = 0;

  const track = async (argumentSource: string): Promise<Sent[]> => {
    const before = sent.length;
    addInlineScript(`window.umami.track(${argumentSource});`);
    for (let i = 0; i < 50 && sent.length === before; i += 1) await flush();
    return sent.slice(before);
  };
  return { track };
}

/**
 * The tracker installs `window.umami` only when it is not already there, so a
 * page that ran it before would keep calling that earlier run. The global is
 * removed from inside the page's realm, where the tracker installed it.
 */
function forgetTracker(): void {
  addInlineScript("delete window.umami;");
}

function onlySent(sent: Sent[]): Record<string, unknown> {
  expect(sent).toHaveLength(1);
  return sent[0].payload;
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

  it("injects nothing when the id is set but the build is not the hosted Vercel build", () => {
    // The desktop shell loads this same dist/ and its page holds the desktop
    // auth token, so the id alone must not turn the tracker on.
    expect(tagsFor({ STUDIO_UMAMI_WEBSITE_ID: UUID })).toEqual([]);
    expect(tagsFor({ STUDIO_UMAMI_WEBSITE_ID: UUID, VERCEL: "0" })).toEqual([]);
  });

  it("injects nothing when the build is Vercel but the id is unset", () => {
    expect(tagsFor({ VERCEL: "1", VERCEL_ENV: "production" })).toEqual([]);
  });

  it("injects nothing for a Vercel preview or development build", () => {
    for (const vercelEnv of [undefined, "preview", "development", "Production"]) {
      expect(
        tagsFor({ VERCEL: "1", VERCEL_ENV: vercelEnv, STUDIO_UMAMI_WEBSITE_ID: UUID }),
        String(vercelEnv),
      ).toEqual([]);
    }
  });

  it("emits exactly one tag for the hosted Vercel build", () => {
    const tags = tagsFor(HOSTED_ENV);
    expect(tags).toHaveLength(1);
    expect(tags[0].tag).toBe("script");
    expect(tags[0].injectTo).toBe("head");
  });

  it("evaluates its children to the async tracker tag", () => {
    const tags = tagsFor(HOSTED_ENV);
    // The children are the hook's source, then the loader call.
    const children = tags[0].children ?? "";
    expect(children.startsWith(HOOK_SOURCE)).toBe(true);
    expect(children).toContain(`\n;(${LOADER_SOURCE}\n)(${JSON.stringify(TRACKER_ATTRS)});`);
    expect(children.indexOf(HOOK_SOURCE)).toBeLessThan(children.indexOf(LOADER_SOURCE));

    // Evaluating the children installs the hook and adds exactly one tracker
    // script element, with exactly the expected attributes.
    document.head.innerHTML = "";
    delete (window as unknown as { __studioAnalyticsBeforeSend?: unknown })
      .__studioAnalyticsBeforeSend;
    evalWindow.eval(children);
    const scripts = Array.from(document.head.querySelectorAll("script"));
    expect(scripts).toHaveLength(1);
    const script = scripts[0];
    expect(script.getAttribute("src")).toBe("https://analytics.khive.ai/script.js");
    expect(script.getAttribute("integrity")).toBe(TRACKER_INTEGRITY);
    expect(script.getAttribute("crossorigin")).toBe("anonymous");
    expect(script.getAttribute("data-website-id")).toBe(UUID);
    expect(script.getAttribute("data-domains")).toBe("lion-studio.khive.ai");
    expect(script.getAttribute("data-exclude-search")).toBe("true");
    expect(script.getAttribute("data-exclude-hash")).toBe("true");
    expect(script.getAttribute("data-do-not-track")).toBe("true");
    expect(script.getAttribute("data-before-send")).toBe("__studioAnalyticsBeforeSend");
    expect(script.attributes.length).toBe(Object.keys(TRACKER_ATTRS).length);
    document.head.innerHTML = "";
  });

  it("carries the integrity pin and anonymous crossorigin on the tracker element", () => {
    const children = tagsFor(HOSTED_ENV)[0].children ?? "";
    expect(children).toContain(`"integrity":"${TRACKER_INTEGRITY}"`);
    expect(children).toContain('"crossorigin":"anonymous"');

    evalWindow.eval(children);
    const script = document.head.querySelector("script") as HTMLScriptElement;
    expect(script.getAttribute("integrity")).toBe(TRACKER_INTEGRITY);
    expect(script.getAttribute("crossorigin")).toBe("anonymous");
    document.head.innerHTML = "";
  });

  it("adds no defer attribute, since the dynamically inserted element is async", () => {
    const tags = tagsFor(HOSTED_ENV);
    const children = tags[0].children ?? "";
    expect(children).not.toContain('"defer"');

    evalWindow.eval(children);
    document.head.querySelectorAll("script").forEach((script) => {
      expect(script.hasAttribute("defer")).toBe(false);
    });
    document.head.innerHTML = "";
  });

  it("evaluating the loader alone, with no hook defined, adds no element", () => {
    document.head.innerHTML = "";
    delete (window as unknown as { __studioAnalyticsBeforeSend?: unknown })
      .__studioAnalyticsBeforeSend;
    evalWindow.eval(`(${LOADER_SOURCE})(${JSON.stringify(TRACKER_ATTRS)});`);
    expect(document.head.querySelectorAll("script")).toHaveLength(0);
  });

  /** Evaluates the plugin's children for the given sources on a clean head. */
  function scriptsAddedBy(sources: { hook: string; loader: string }): number {
    const children = (
      hostedAnalyticsInjector(HOSTED_ENV, sources).transformIndexHtml as () => HtmlTag[]
    )()[0].children;
    document.head.innerHTML = "";
    delete (window as unknown as { __studioAnalyticsBeforeSend?: unknown })
      .__studioAnalyticsBeforeSend;
    evalWindow.eval(children ?? "");
    const added = document.head.querySelectorAll("script").length;
    document.head.innerHTML = "";
    return added;
  }

  it("still adds the tracker for a hook without a final semicolon", () => {
    // The call starts after a semicolon, so the hook's last expression is never
    // called with the loader as its argument.
    expect(
      scriptsAddedBy({ hook: HOOK_SOURCE.replace(/;\s*$/, "\n"), loader: LOADER_SOURCE }),
    ).toBe(1);
  });

  it("still adds the tracker for a loader that is a bare function expression", () => {
    const bare = LOADER_SOURCE.replace(/\(function \(attrs\)/, "function (attrs)").replace(
      /\}\)$/,
      "}",
    );
    expect(bare).not.toBe(LOADER_SOURCE);
    expect(scriptsAddedBy({ hook: HOOK_SOURCE, loader: bare })).toBe(1);
  });

  it("still adds the tracker for a loader ending in a line comment", () => {
    // The parentheses close on a line of their own, so the comment cannot take
    // the call with it.
    expect(
      scriptsAddedBy({ hook: HOOK_SOURCE, loader: `${LOADER_SOURCE} // see the note (1)\n` }),
    ).toBe(1);
  });

  it("throws on a loader that is not one function expression", () => {
    for (const loader of [
      LOADER_SOURCE.replace(/if \(typeof/, "if typeof"),
      LOADER_SOURCE.replace(/\)$/, ";}\n"),
      `${LOADER_SOURCE};\n(function () {})`,
      '"not a function"',
    ]) {
      expect(
        () => hostedAnalyticsInjector(HOSTED_ENV, { hook: HOOK_SOURCE, loader }),
        loader.slice(-40),
      ).toThrow(/load-tracker\.js/);
    }
  });

  it("throws on a hook that does not compile", () => {
    expect(() =>
      hostedAnalyticsInjector(HOSTED_ENV, {
        hook: HOOK_SOURCE.replace(/\}\)\(\);\s*$/, "}));\n"),
        loader: LOADER_SOURCE,
      }),
    ).toThrow(/before-send\.js/);
  });

  it("throws on a hook or loader holding a closing script tag", () => {
    for (const sources of [
      { hook: `${HOOK_SOURCE}\n// </script>\n`, loader: LOADER_SOURCE },
      { hook: HOOK_SOURCE, loader: `/* </SCRIPT */ ${LOADER_SOURCE}` },
    ]) {
      expect(() => hostedAnalyticsInjector(HOSTED_ENV, sources)).toThrow(/<\/script/);
    }
  });
});

describe("pinned tracker build", () => {
  it("matches the integrity pin shipped in the tracker attributes", () => {
    expect(PINNED_SHA384).toBe(TRACKER_INTEGRITY);
    expect(TRACKER_ATTRS.integrity).toBe(TRACKER_INTEGRITY);
  });

  it("carries a header naming it as the pinned build, never loaded", () => {
    const header = PINNED_FILE.slice(0, PINNED_FILE.indexOf("\n"));
    expect(header.startsWith("//")).toBe(true);
    expect(header).toContain("never loaded");
  });
});

describe("before-send hook", () => {
  it("scrubs a page view of a dynamic route to url and title templates", () => {
    const hook = loadHookOnPage();
    const result = hook("event", pageView("/runs/abc-123?q=1#x")) as Record<string, string>;
    expect(result.url).toBe(`${ORIGIN}/runs/[id]`);
    expect(result.title).toBe("/runs/[id]");
  });

  it("templates a same-origin referrer", () => {
    const hook = loadHookOnPage();
    const payload = { ...pageView("/runs/abc-123"), referrer: `${ORIGIN}/invocations/xyz` };
    const result = hook("event", payload) as Record<string, string>;
    expect(result.referrer).toBe(`${ORIGIN}/invocations/[id]`);
  });

  it("reduces a referrer from another origin to its origin only", () => {
    const hook = loadHookOnPage();
    const payload = {
      ...pageView("/runs/abc-123"),
      referrer: "https://example.com/invocations/ref-secret?x=1#h",
    };
    const result = hook("event", payload) as Record<string, string>;
    expect(result.referrer).toBe("https://example.com");
  });

  it("keeps another http origin when the referrer is http", () => {
    const hook = loadHookOnPage();
    const payload = {
      ...pageView("/runs/abc-123"),
      referrer: "http://example.com/invocations/ref-secret?x=1#h",
    };
    const result = hook("event", payload) as Record<string, string>;
    expect(result.referrer).toBe("http://example.com");
  });

  it("drops a referrer whose origin is opaque", () => {
    const hook = loadHookOnPage();
    for (const referrer of ["android-app://com.example.app/runs/secret", "about:blank"]) {
      const result = hook("event", {
        ...pageView("/runs/abc-123"),
        referrer,
      }) as Record<string, string>;
      expect(result.referrer).toBe("");
    }
  });

  it("drops a referrer that is not a string", () => {
    const hook = loadHookOnPage();
    for (const referrer of [{ path: "/runs/abc-123" }, ["/runs/abc-123"], 42, null]) {
      const result = hook("event", { ...pageView("/fleet"), referrer }) as Record<string, unknown>;
      expect(result, `referrer ${JSON.stringify(referrer)}`).toBeTruthy();
      expect(result).not.toHaveProperty("referrer");
    }
  });

  it("keeps the static /playbooks/new and templates /playbooks/foo/edit", () => {
    const hook = loadHookOnPage();
    expect((hook("event", pageView("/playbooks/new")) as Record<string, string>).url).toBe(
      `${ORIGIN}/playbooks/new`,
    );
    expect((hook("event", pageView("/playbooks/foo/edit")) as Record<string, string>).url).toBe(
      `${ORIGIN}/playbooks/[name]/edit`,
    );
  });

  it("templates a path-only url and referrer the same way as absolute ones", () => {
    const hook = loadHookOnPage();
    const result = hook("event", {
      url: "/runs/abc-123",
      title: "x",
      referrer: "/playbooks/secret-name/edit",
    }) as Record<string, string>;
    expect(result.url).toBe(`${ORIGIN}/runs/[id]`);
    expect(result.referrer).toBe(`${ORIGIN}/playbooks/[name]/edit`);
  });

  it("collapses an unknown path to /(other)", () => {
    const hook = loadHookOnPage();
    expect((hook("event", pageView("/not/a/real/route")) as Record<string, string>).url).toBe(
      `${ORIGIN}/(other)`,
    );
  });

  it("returns falsy for identify and for a named custom event", () => {
    const hook = loadHookOnPage();
    expect(hook("identify", { ...pageView("/"), data: { x: 1 } })).toBeFalsy();
    expect(hook("event", { ...pageView("/"), name: "button-click", data: {} })).toBeFalsy();
  });

  it("returns falsy for performance and for any other type", () => {
    const hook = loadHookOnPage();
    expect(hook("performance", pageView("/"))).toBeFalsy();
    expect(hook("unknown-type", pageView("/"))).toBeFalsy();
  });

  it("sends exactly the allowlisted page-view keys, dropping everything else", () => {
    const hook = loadHookOnPage();
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
    expect(JSON.stringify(result)).not.toContain("site-id");
  });

  it("takes the page's own website, hostname, screen, and language, not the payload's", () => {
    const hook = loadHookOnPage();
    const result = hook("event", {
      ...pageView("/fleet"),
      website: "visitor-website",
      hostname: "visitor-host",
      screen: "1x1",
      language: "visitor-language",
    }) as Record<string, string>;
    expect(result.website).toBe(UUID);
    expect(result.hostname).toBe(window.location.hostname);
    expect(result.screen).toBe(`${window.screen.width}x${window.screen.height}`);
    expect(result.language).toBe(window.navigator.language);
    expect(JSON.stringify(result)).not.toContain("visitor-");
  });

  it("sends nothing when the tracker element is not on the page", () => {
    // loadHook clears the head, so no element carries the website id.
    const hook = loadHook();
    expect(hook("event", pageView("/fleet"))).toBeFalsy();
    // A send that found no element keeps nothing: the element added later is read.
    addTrackerElement();
    expect((hook("event", pageView("/fleet")) as Record<string, string>).website).toBe(UUID);
  });

  it("keeps the website id read at the first send", () => {
    const hook = loadHookOnPage();
    expect((hook("event", pageView("/fleet")) as Record<string, string>).website).toBe(UUID);

    const element = document.head.querySelector("script[data-website-id]") as HTMLScriptElement;
    element.setAttribute("data-website-id", "another-site");
    const earlier = document.createElement("script");
    earlier.setAttribute("data-website-id", "earlier-site");
    earlier.setAttribute("data-before-send", "__studioAnalyticsBeforeSend");
    document.head.insertBefore(earlier, document.head.firstChild);

    const result = hook("event", pageView("/fleet")) as Record<string, string>;
    expect(result.website).toBe(UUID);
  });

  it("fills the page's own values and leaves an absent referrer absent", () => {
    const hook = loadHookOnPage();
    const result = hook("event", {
      url: `${ORIGIN}/fleet`,
      title: "whatever",
    }) as Record<string, string>;
    expect(result.website).toBe(UUID);
    expect(result.hostname).toBe(window.location.hostname);
    expect(result.screen).toBe(`${window.screen.width}x${window.screen.height}`);
    expect(result.language).toBe(window.navigator.language);
    expect(result.referrer).toBeUndefined();
  });

  it("templates the path for trailing slashes, double slashes, and encoded slashes", () => {
    const hook = loadHookOnPage();
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
    const hook = loadHookOnPage();
    let result: unknown;
    expect(() => {
      result = hook("event", { url: "https://[not-a-host/runs/abc", title: "x", referrer: "" });
    }).not.toThrow();
    expect(result).toBeFalsy();
  });

  it("reports a url that is not a route path as /(other), never the raw text", () => {
    const hook = loadHookOnPage();
    const result = hook("event", { url: "{not a url}", title: "x", referrer: "" }) as Record<
      string,
      string
    >;
    expect(result.url).toBe(`${ORIGIN}/(other)`);
    expect(result.title).toBe("/(other)");
  });
});

describe("tracker calls through the shipped hook", () => {
  /** Runs the pinned tracker with the shipped attributes and returns what it sent. */
  async function sendsWithDoNotTrack(browserSetting: string | undefined): Promise<Sent[]> {
    document.head.innerHTML = "";
    forgetTracker();
    const sent: Sent[] = [];
    (document.documentElement as unknown as Record<string, unknown>).__analyticsSent = sent;
    Object.defineProperty(window.navigator, "doNotTrack", {
      value: browserSetting,
      configurable: true,
    });
    try {
      addInlineScript(`window.fetch = function (url, init) {
        document.documentElement.__analyticsSent.push(JSON.parse(init.body));
        return Promise.resolve({ json: function () { return Promise.resolve({}); } });
      };`);
      addInlineScript(HOOK_SOURCE);
      // The tracker reads its attributes as it runs, so they go on before the text.
      const element = document.createElement("script");
      element.setAttribute("data-website-id", UUID);
      element.setAttribute("data-domains", window.location.hostname);
      element.setAttribute("data-before-send", "__studioAnalyticsBeforeSend");
      element.setAttribute("data-do-not-track", TRACKER_ATTRS["data-do-not-track"]);
      element.textContent = PINNED_TRACKER;
      document.head.appendChild(element);
      addInlineScript(`window.umami.track({ url: "/fleet", title: "t" });`);
      for (let i = 0; i < 20; i += 1) await flush();
      return sent.slice();
    } finally {
      delete (window.navigator as unknown as { doNotTrack?: unknown }).doNotTrack;
      forgetTracker();
    }
  }

  it("sends nothing from a browser that asks not to be tracked", async () => {
    expect(await sendsWithDoNotTrack("1")).toEqual([]);
    expect((await sendsWithDoNotTrack(undefined)).length).toBeGreaterThan(0);
  });

  it("sends the page's hostname for the object form, not the caller's", async () => {
    const { track } = await trackerPage();
    const sent = await track(`{ url: "/fleet", hostname: "visitor-42" }`);
    const payload = onlySent(sent);
    expect(payload.hostname).toBe(window.location.hostname);
    expect(payload.url).toBe(`${ORIGIN}/fleet`);
    expect(JSON.stringify(payload)).not.toContain("visitor-42");
  });

  it("sends the page's website for the object form, not the caller's", async () => {
    const { track } = await trackerPage();
    const sent = await track(`{ url: "/fleet", website: "visitor-42" }`);
    expect(onlySent(sent).website).toBe(UUID);
  });

  it("sends the page's screen for the object form, not the caller's", async () => {
    const { track } = await trackerPage();
    const sent = await track(`{ url: "/fleet", screen: "visitor-42" }`);
    expect(onlySent(sent).screen).toBe(`${window.screen.width}x${window.screen.height}`);
  });

  it("sends the page's language for the object form, not the caller's", async () => {
    const { track } = await trackerPage();
    const sent = await track(`{ url: "/fleet", language: "visitor-42" }`);
    expect(onlySent(sent).language).toBe(window.navigator.language);
  });

  it("sends the page's own values for the function form, not the caller's", async () => {
    const { track } = await trackerPage();
    const sent = await track(`function (context) {
      return Object.assign({}, context, {
        hostname: "visitor-42",
        website: "visitor-43",
        screen: "visitor-44",
        language: "visitor-45",
      });
    }`);
    const payload = onlySent(sent);
    expect(payload.hostname).toBe(window.location.hostname);
    expect(payload.website).toBe(UUID);
    expect(payload.screen).toBe(`${window.screen.width}x${window.screen.height}`);
    expect(payload.language).toBe(window.navigator.language);
    expect(JSON.stringify(payload)).not.toContain("visitor-4");
  });

  it("does not send a referrer given as an object or an array", async () => {
    for (const referrer of [`{ path: "/fleet" }`, `["/fleet"]`]) {
      const { track } = await trackerPage();
      const sent = await track(`{ url: "/fleet", referrer: ${referrer} }`);
      expect(onlySent(sent), referrer).not.toHaveProperty("referrer");
    }
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
    const hook = loadHookOnPage();
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
