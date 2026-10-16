/*
 * Umami before-send hook for the hosted Studio build.
 *
 * The tracker calls `window[data-before-send](type, payload)` before every
 * send and awaits the result: a falsy value drops the send, any other value is
 * sent as the payload. The hook rebuilds the payload from an allowlist of
 * page-view fields, so the payload holds those fields and nothing else. Four
 * of them come from the page rather than from the caller's object: `hostname`
 * from `location.hostname`, `screen` from the screen's width and height,
 * `language` from `navigator.language`, and `website` from the tracker
 * element's `data-website-id`, read at the first send that finds it and kept. So a
 * `hostname`, `screen`, `language`, or `website` that a caller passed to
 * `umami.track({...})` is replaced by the page's own value. `url`, `title` and
 * `referrer` come from the caller and are reduced: `url` and `title` to the
 * route template (an unknown path to `/(other)`), a referrer to the previous
 * route's template or to its origin. The hook sends nothing for any send type
 * other than `event`, and nothing for an `event` payload that carries a `name`.
 * It is injected inline into the hosted `index.html` by
 * `hostedAnalyticsInjector` in vite.config.mts, ahead of the tracker loader,
 * and is present only in the hosted production build.
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

  // The only page-view fields the tracker is allowed to send.
  var ALLOWED_FIELDS = ["website", "hostname", "screen", "language", "url", "referrer", "title"];

  // The attribute the loader points at this hook, so the hook can find the
  // tracker element and read the website id it was built with.
  var LOADER_ATTRIBUTE = "data-before-send";
  var HOOK_NAME = "__studioAnalyticsBeforeSend";

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

  // The tracker element the loader added, found by the attribute that names
  // this hook. Null when the page holds no such element.
  function trackerElement() {
    var elements = document.querySelectorAll("script[" + LOADER_ATTRIBUTE + "]");
    for (var i = 0; i < elements.length; i += 1) {
      if (elements[i].getAttribute(LOADER_ATTRIBUTE) === HOOK_NAME) {
        return elements[i];
      }
    }
    return null;
  }

  // The website id, once a send has read it from the tracker element.
  var website = null;

  window.__studioAnalyticsBeforeSend = function (type, payload) {
    try {
      // The tracker sends page views, and page views only; anything else —
      // identify calls (which carry an `id`) and named custom events (which
      // carry `data`) — sends nothing.
      if (type !== "event" || (payload && payload.name !== undefined)) {
        return false;
      }

      // Copy only the allowlisted page-view fields, so every other key the
      // caller's object carried is not sent.
      var result = {};
      for (var i = 0; i < ALLOWED_FIELDS.length; i += 1) {
        var key = ALLOWED_FIELDS[i];
        if (Object.prototype.hasOwnProperty.call(payload, key)) {
          result[key] = payload[key];
        }
      }

      if (typeof payload.url !== "string") {
        return false;
      }

      // The tracker's own defaults are absent from the payload it hands the
      // hook for a call like `umami.track({...})`, so the page values are read
      // here. The website id is read from the tracker element at the first
      // send that finds it and kept, so an attribute changed later, or another
      // element added later, does not change it. Without the tracker element
      // there is no website id to send, and the hook sends nothing at all.
      if (website === null) {
        var element = trackerElement();
        if (!element) {
          return false;
        }
        var id = element.getAttribute("data-website-id");
        if (typeof id !== "string" || id === "") {
          return false;
        }
        website = id;
      }
      result.website = website;
      result.hostname = location.hostname;
      result.screen = screen.width + "x" + screen.height;
      result.language = navigator.language;

      // The URL and the title both carry the route template, never the raw
      // path. The tracker sends page views with an absolute URL; a path is
      // resolved against the page the same way. The title is replaced by the
      // template so whatever the page title holds is not sent.
      var route = template(new URL(payload.url, location.href).pathname);
      result.url = location.origin + route;
      result.title = route;

      // A referrer that is not a string is dropped before the rule below, so
      // the object or array a caller passed is not sent.
      if (typeof result.referrer !== "string") {
        delete result.referrer;
      } else if (result.referrer !== "") {
        var referrer = new URL(result.referrer, location.href);
        if (referrer.origin === location.origin) {
          // Same-origin means a client-side navigation; the previous route's
          // path is templated so its ids do not leak.
          result.referrer = location.origin + template(referrer.pathname);
        } else if (referrer.protocol === "http:" || referrer.protocol === "https:") {
          // A referrer from another origin keeps its origin only: its path,
          // search string, and hash are dropped.
          result.referrer = referrer.origin;
        } else {
          // An opaque origin ("null", e.g. android-app:// or about:blank) has
          // nothing safe to send.
          result.referrer = "";
        }
      }
      return result;
    } catch {
      // A malformed payload sends nothing rather than a raw value.
      return false;
    }
  };
})();
