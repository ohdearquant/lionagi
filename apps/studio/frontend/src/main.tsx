import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { RouterProvider, createRouter } from "@tanstack/react-router";
import { routeTree } from "./routeTree.gen";
import { applyDocumentLocale, getLocaleFromCookie } from "@/i18n/locales";
import { handlePreloadError } from "@/lib/preloadReload";

// Apply the persisted locale's dir/lang before the first paint so a
// returning ar/ur user never sees an LTR flash while React mounts.
applyDocumentLocale(getLocaleFromCookie());

// See preloadReload.ts for the reload-once guard rationale.
window.addEventListener("vite:preloadError", (event) => {
  // Swallow the error only when we actually reload; a repeat failure must
  // propagate so a persistently broken chunk surfaces instead of failing silently.
  if (handlePreloadError(sessionStorage, () => window.location.reload())) {
    event.preventDefault();
  }
});

const router = createRouter({ routeTree });

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}

const rootElement = document.getElementById("root")!;
createRoot(rootElement).render(
  <StrictMode>
    <RouterProvider router={router} />
  </StrictMode>,
);
