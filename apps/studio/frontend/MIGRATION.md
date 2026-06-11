# Studio Vite SPA Migration Guide

Phase 1a landed: Vite + TanStack Router + use-intl foundation is live. This guide
is the verbatim recipe for converting the remaining 23 app/ pages.

---

## File mapping

```
app/X/page.tsx                     → src/routes/X/index.tsx
app/X/[param]/page.tsx             → src/routes/X/$param.tsx
app/X/[param]/Y/page.tsx           → src/routes/X/$param/Y/index.tsx  (or Y.tsx if leaf)
app/X/new/page.tsx                 → src/routes/X/new/index.tsx
```

Full table for this repo:

| Source (app/)                  | Destination (src/routes/)      |
| ------------------------------ | ------------------------------ |
| page.tsx                       | index.tsx ← DONE               |
| runs/page.tsx                  | runs/index.tsx ← DONE          |
| runs/[id]/page.tsx             | runs/$id.tsx ← DONE            |
| admin/health/page.tsx          | admin/health/index.tsx         |
| admin/maintenance/page.tsx     | admin/maintenance/index.tsx    |
| agents/page.tsx                | agents/index.tsx               |
| agents/new/page.tsx            | agents/new/index.tsx           |
| agents/[name]/page.tsx         | agents/$name/index.tsx         |
| agents/[name]/edit/page.tsx    | agents/$name/edit/index.tsx    |
| invocations/page.tsx           | invocations/index.tsx          |
| invocations/[id]/page.tsx      | invocations/$id.tsx            |
| kanban/page.tsx                | kanban/index.tsx               |
| playbooks/page.tsx             | playbooks/index.tsx            |
| playbooks/new/page.tsx         | playbooks/new/index.tsx        |
| playbooks/[name]/page.tsx      | playbooks/$name/index.tsx      |
| playbooks/[name]/edit/page.tsx | playbooks/$name/edit/index.tsx |
| playfield/page.tsx             | playfield/index.tsx            |
| plugins/page.tsx               | plugins/index.tsx              |
| projects/page.tsx              | projects/index.tsx             |
| projects/[name]/page.tsx       | projects/$name/index.tsx       |
| schedules/page.tsx             | schedules/index.tsx            |
| shows/page.tsx                 | shows/index.tsx                |
| shows/[topic]/page.tsx         | shows/$topic/index.tsx         |
| skills/page.tsx                | skills/index.tsx               |
| teams/page.tsx                 | teams/index.tsx                |
| teams/[id]/page.tsx            | teams/$id/index.tsx            |

Naming rule: `[param]` in Next.js becomes `$param` in TanStack Router. Static
segments (`new`, `edit`, `health`, `maintenance`) stay as-is.

---

## Per-page conversion steps

1. Copy the file to the destination path.
2. Remove the `"use client"` directive (first line).
3. Add the route export (see Route export section below).
4. Replace Next.js imports (see API replacement table).
5. Replace dynamic params (see Params section).
6. Replace search params (see Search params section).
7. Remove any `export default` page function — the component is named and
   referenced in the Route export instead.

### Route export pattern

Every route file must export `Route` as a named const:

```tsx
// index route  (maps to /agents)
export const Route = createFileRoute("/agents/")({
  component: AgentsPage,
});

// dynamic segment  (maps to /agents/:name)
export const Route = createFileRoute("/agents/$name")({
  component: AgentDetailPage,
});

// nested static  (maps to /agents/:name/edit)
export const Route = createFileRoute("/agents/$name/edit")({
  component: AgentEditPage,
});
```

The `createFileRoute` string must match the file path under `src/routes/` with
`$` for dynamic segments and no trailing slash except for index routes
(`/runs/` not `/runs`). Check `src/routeTree.gen.ts` after adding files —
Vite plugin regenerates it on save during `npm run dev`.

### Redirect-only route

Use `beforeLoad` with `throw redirect`:

```tsx
import { createFileRoute, redirect } from "@tanstack/react-router";

export const Route = createFileRoute("/admin/")({
  beforeLoad: () => {
    throw redirect({ to: "/admin/health" });
  },
  component: () => null,
});
```

---

## API replacement table

| Next.js import                                      | SPA replacement                                         | Notes                     |
| --------------------------------------------------- | ------------------------------------------------------- | ------------------------- |
| `import Link from 'next/link'`                      | `import { Link } from '@tanstack/react-router'`         |                           |
| `<Link href="/foo">`                                | `<Link to="/foo">`                                      | `href` → `to`             |
| `<Link href="/foo" className="...">`                | `<Link to="/foo" className="...">`                      | all other props unchanged |
| `import { useRouter } from 'next/navigation'`       | `import { useNavigate } from '@tanstack/react-router'`  |                           |
| `const router = useRouter()`                        | `const navigate = useNavigate()`                        |                           |
| `router.push('/foo')`                               | `navigate({ to: '/foo' })`                              |                           |
| `router.push('/foo?bar=1')`                         | `navigate({ to: '/foo', search: { bar: '1' } })`        |                           |
| `import { usePathname } from 'next/navigation'`     | `import { useLocation } from '@tanstack/react-router'`  |                           |
| `usePathname()`                                     | `useLocation().pathname`                                |                           |
| `import { useSearchParams } from 'next/navigation'` | `Route.useSearch()` with `validateSearch`               | see below                 |
| `import { useTranslations } from 'next-intl'`       | `import { useTranslations } from 'use-intl'`            | identical API             |
| `import { useLocale } from 'next-intl'`             | `import { useLocale } from 'use-intl'`                  | identical API             |
| `import { useFormatter } from 'next-intl'`          | `import { useFormatter } from 'use-intl'`               | identical API             |
| `import dynamic from 'next/dynamic'`                | `const X = lazy(() => import('...'))` with `<Suspense>` |                           |
| `dynamic(() => import('...'), { ssr: false })`      | `lazy(() => import('...'))`                             | SSR option dropped        |
| `import Image from 'next/image'`                    | `<img>` with explicit width/height                      | no Next Image in Vite     |

### Locale cookie write

If a component sets the locale, keep the cookie name `NEXT_LOCALE` (it is read in
`__root.tsx` by that name) and trigger `window.location.reload()` after setting:

```ts
document.cookie = `NEXT_LOCALE=${locale}; path=/; max-age=31536000`;
window.location.reload();
```

The reload re-runs `getLocaleFromCookie()` in `__root.tsx` with the new value.

---

## Dynamic params

```tsx
// OLD (Next.js)
export default function Page({ params }: { params: { name: string } }) {
  const { name } = params
  ...
}

// NEW (TanStack Router)
export const Route = createFileRoute('/agents/$name')({
  component: AgentDetailPage,
})

function AgentDetailPage() {
  const { name } = Route.useParams()
  ...
}
```

---

## Search params

Use `validateSearch` to type the search params. TanStack Router requires an explicit
schema — it will not pass through arbitrary query strings.

```tsx
import { createFileRoute } from '@tanstack/react-router'
import { z } from 'zod'  // or use the built-in fallback pattern below

// With zod
export const Route = createFileRoute('/runs/')({
  validateSearch: z.object({
    project: z.string().optional(),
    status: z.string().optional(),
  }).parse,
  component: RunsPage,
})

// Without zod — manual validator
export const Route = createFileRoute('/runs/')({
  validateSearch: (raw: Record<string, unknown>) => ({
    project: typeof raw.project === 'string' ? raw.project : undefined,
    status: typeof raw.status === 'string' ? raw.status : undefined,
  }),
  component: RunsPage,
})

function RunsPage() {
  const { project, status } = Route.useSearch()
  ...
}
```

To navigate with search params:

```ts
const navigate = useNavigate({ from: Route.fullPath });
navigate({ search: (prev) => ({ ...prev, project: "my-project" }) });
```

---

## i18n import swap

Every `from 'next-intl'` in `src/` becomes `from 'use-intl'`. The hook APIs are
identical — `useTranslations`, `useLocale`, `useFormatter`, `useNow`, `useTimeZone`
all export from `use-intl` with the same signatures.

Messages live in `src/messages/en.json` and `src/messages/zh.json`. Keys are
unchanged. `IntlProvider` is mounted in `src/routes/__root.tsx`.

---

## Gotchas encountered in Phase 1a

### TanStack Router typed `to` rejects unregistered routes

`Link to="/agents"` causes a TypeScript error until `src/routes/agents/index.tsx`
exists and `routeTree.gen.ts` has been regenerated. During incremental conversion
use `as never` cast:

```tsx
<Link to={"/agents" as never}>Agents</Link>
```

Remove the cast once the route file is created.

### `next/dynamic` has no equivalent

Replace with React's built-in:

```tsx
// before
import dynamic from "next/dynamic";
const WorkerCanvas = dynamic(() => import("@/components/canvas/WorkerCanvas"), { ssr: false });

// after
import { lazy, Suspense } from "react";
const WorkerCanvas = lazy(() => import("@/components/canvas/WorkerCanvas"));
// wrap usage: <Suspense fallback={null}><WorkerCanvas ... /></Suspense>
```

### vite.config.mts test field requires vitest/config

Using `defineConfig` from `vite` with a `test:` key causes TypeScript overload
conflicts. Import from `vitest/config` instead:

```ts
import { defineConfig } from "vitest/config";
```

### npm install peer dep conflict

`next@16` conflicts with `next-intl@3.x` peers. Install with `--legacy-peer-deps`.
This is a transient state — next and next-intl are removed in Phase 1c.

### routeTree.gen.ts must exist before build

On a fresh clone the file is absent. Either run `npm run dev` (Vite plugin generates
it on first start) or run `npx vite build` once (the plugin also generates on build).
The file is committed, so clones have it.

### `__root.tsx` CSS import path

`globals.css` is at `src/globals.css`. From `src/routes/__root.tsx` the import is:

```ts
import "../globals.css";
```

Not `'./globals.css'` (which would look in `src/routes/`).

---

## Env vars

| Old variable                  | New variable           |
| ----------------------------- | ---------------------- |
| `NEXT_PUBLIC_STUDIO_API_BASE` | `VITE_STUDIO_API_BASE` |

Runtime override: set `window.__STUDIO_API_BASE__` before the app boots (e.g.
injected by a desktop shell). This takes precedence over the env var.
