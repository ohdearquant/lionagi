import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { RouterProvider, createRouter } from "@tanstack/react-router";
import { routeTree } from "./routeTree.gen";
import { applyDocumentLocale, getLocaleFromCookie } from "@/i18n/locales";

// Apply the persisted locale's dir/lang before the first paint so a
// returning ar/ur user never sees an LTR flash while React mounts.
applyDocumentLocale(getLocaleFromCookie());

// A deploy rotates hashed chunk filenames, so a page loaded before the
// deploy can fail to lazy-load a route chunk it still references. Reload
// once to pick up the new index; the flag prevents a reload loop if the
// failure persists for another reason.
window.addEventListener("vite:preloadError", (event) => {
  const key = "studio-chunk-reload";
  if (sessionStorage.getItem(key)) return;
  sessionStorage.setItem(key, "1");
  event.preventDefault();
  window.location.reload();
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
