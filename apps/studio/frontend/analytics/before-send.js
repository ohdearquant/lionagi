/*
 * Umami before-send hook for the hosted Studio build.
 *
 * The tracker calls `window[data-before-send](type, payload)` before every
 * send and awaits the result: a falsy value drops the send, any other value is
 * sent as the payload. The hook keeps page views useful as aggregate traffic
 * while removing per-visitor detail: ids, search strings, and hashes never
 * leave the browser. It is injected inline into the hosted `index.html` by
 * `hostedAnalyticsInjector` in vite.config.mts, ahead of the tracker tag, and
 * is never present in local, source, Docker, or packaged builds.
 */
(function () {
  "use strict";

  // Every route directory under src/routes/ resolves to a static route. Kept
  // in step with that directory by the route-coverage test, which fails until
  // a new static route gets an entry here.
  var STATIC_ROUTES = [
    "/",
    "/admin",
    "/admin/health",
    "/admin/maintenance",
    "/agents",
    "/attention",
    "/canvas",
    "/definitions",
    "/designer",
    "/engine-runs",
    "/engines",
    "/fleet",
    "/history",
    "/invocations",
    "/kanban",
    "/library",
    "/mission",
    "/outcomes",
    "/playbooks",
    "/playbooks/new",
    "/playfield",
    "/plugins",
    "/routing",
    "/runs",
    "/schedules",
    "/shows",
    "/skills",
    "/system",
    "/teams",
  ];

  var ONE_SEGMENT = "[^/]+";

  // Dynamic routes collapse to a fixed template so an id never reaches the
  // tracker: /runs/abc-123 and /runs/def-456 are both "/runs/[id]".
  var DYNAMIC_ROUTES = [
    { pattern: new RegExp("^/runs/" + ONE_SEGMENT + "$"), template: "/runs/[id]" },
    { pattern: new RegExp("^/invocations/" + ONE_SEGMENT + "$"), template: "/invocations/[id]" },
    { pattern: new RegExp("^/playbooks/" + ONE_SEGMENT + "$"), template: "/playbooks/[name]" },
    {
      pattern: new RegExp("^/playbooks/" + ONE_SEGMENT + "/edit$"),
      template: "/playbooks/[name]/edit",
    },
  ];

  function template(pathname) {
    var path = pathname;
    // The router may leave one trailing slash ("/fleet/"); a page view of
    // "/" itself keeps its single slash.
    if (path.length > 1 && path.charAt(path.length - 1) === "/") {
      path = path.slice(0, -1);
    }
    if (STATIC_ROUTES.indexOf(path) !== -1) {
      return path;
    }
    for (var i = 0; i < DYNAMIC_ROUTES.length; i += 1) {
      if (DYNAMIC_ROUTES[i].pattern.test(path)) {
        return DYNAMIC_ROUTES[i].template;
      }
    }
    // An unrecognized path collapses rather than being sent raw: a bad route
    // shape must never leak a path that could carry an id. `/playbooks/new`
    // is matched above as a static route before this point.
    return "/(other)";
  }

  window.__studioAnalyticsBeforeSend = function (type, payload) {
    try {
      // We run no identify calls and no custom events; the tracker sends
      // nothing for a falsy result.
      if (type === "identify" || (payload && payload.name !== undefined)) {
        return false;
      }

      var result = {};
      for (var key in payload) {
        if (Object.prototype.hasOwnProperty.call(payload, key)) {
          result[key] = payload[key];
        }
      }

      if (typeof payload.url !== "string") {
        return false;
      }
      // The URL and the title both carry the route template, never the raw
      // path or the page title (a run detail title contains the run id).
      // The tracker sends an absolute URL; a path is resolved against the page the same way.
      var route = template(new URL(payload.url, location.href).pathname);
      result.url = location.origin + route;
      result.title = route;

      if (typeof payload.referrer === "string" && payload.referrer !== "") {
        var referrer = new URL(payload.referrer, location.href);
        if (referrer.origin === location.origin) {
          // Same-origin means a client-side navigation; the previous route's
          // path is templated so its ids do not leak.
          result.referrer = location.origin + template(referrer.pathname);
        }
        // A referrer from another origin is left as-is.
      }
      return result;
    } catch {
      // A malformed payload sends nothing rather than a raw value.
      return false;
    }
  };
})();
