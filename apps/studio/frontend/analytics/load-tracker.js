/*
 * Umami tracker loader for the hosted Studio build.
 *
 * The hosted `index.html` injects one inline script: the before-send hook
 * (analytics/before-send.js) followed by this loader, which `hostedAnalyticsInjector`
 * in vite.config.mts calls with the tracker's attributes as a JSON literal. The
 * loader adds the tracker tag only when the hook is present, so a build whose
 * inline script did not run (a Content-Security-Policy without its hash, say)
 * loads no tracker at all rather than a tracker that sends raw paths and page
 * titles. It takes its attributes as its single argument and defines no global.
 */
(function (attrs) {
  "use strict";

  if (typeof window.__studioAnalyticsBeforeSend !== "function") {
    return;
  }

  var script = document.createElement("script");
  for (var key in attrs) {
    if (Object.prototype.hasOwnProperty.call(attrs, key)) {
      if (key === "defer") {
        script.defer = attrs[key];
      } else {
        script.setAttribute(key, attrs[key]);
      }
    }
  }
  document.head.appendChild(script);
});
