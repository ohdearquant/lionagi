import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { RouterProvider, createRouter } from "@tanstack/react-router";
import { routeTree } from "./routeTree.gen";
import { applyDocumentLocale, getLocaleFromCookie } from "@/i18n/locales";

// Apply the persisted locale's dir/lang before the first paint so a
// returning ar/ur user never sees an LTR flash while React mounts.
applyDocumentLocale(getLocaleFromCookie());

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
