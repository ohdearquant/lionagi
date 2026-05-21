import assert from "node:assert/strict";
import { describe, it } from "node:test";
import {
  extractAttending,
  extractEmitting,
  extractThinking,
  type StreamChunk,
} from "../lib/extractors";

const goldenChunks: StreamChunk[] = [
  {
    id: 141,
    flow_id: "20260420T183524-153e9455",
    branch_id: "01d13d18-3c14-4919-bd7e-4ba0870b1d65",
    api_call_id: "0edb6ea7-d867-4cb8-9b1d-a4ed72584447",
    ts: 1776725055.926235914,
    chunk_type: "text",
    content: [
      "Created the Phase 1 audit artifact at [audit.md](/Users/lion/projects/lionagi/.khive/flows/lion-studio-init/artifacts/audit.md).",
      "",
      "It includes P0/P1/P2 fix lists with absolute paths and line ranges, and it verifies the `flow_id` mismatch: `ingest_run` sets `flow_id = run_dir.name` at `persist.py:318`, then writes flow/manifest/branches/events under that id, with no `state_root` reconciliation before `flow_create`.",
      "",
      "I only wrote the audit artifact. No implementation files were changed.",
      "",
      "Next concrete follow-ups:",
      "1. Start Phase 2 backend patch from the `persist.py` reconciliation item.",
      "2. Start Phase 3 data population, with the audit note that current `li task new` writes JSON sessions, not the SQLite `tasks` table read by the monitor.",
    ].join("\n"),
    tool_name: null,
    tool_id: null,
    tool_input: null,
    tool_output: null,
    is_delta: 0,
    is_error: 0,
    metadata: "{}",
  },
  {
    id: 166,
    flow_id: "20260420T183524-153e9455",
    branch_id: "42ed8221-3f86-47a5-aea2-90ade319bc8d",
    api_call_id: "0da3868c-94db-40ca-8d7f-8007e42d5c72",
    ts: 1776725287.229624987,
    chunk_type: "text",
    content: [
      "Implemented Phase 4b in [CostChart.tsx](/Users/lion/projects/lionagi/apps/studio/frontend/components/CostChart.tsx:12) only.",
      "",
      "What changed:",
      "- Added optional `cost_usd`, `tokens_in`, `tokens_out`, and `duration_ms` fields to `CostChartRow`, keeping the existing props API backwards-compatible.",
      "- Rows with explicit `cost_usd: 0` or `cost_usd: null` now render a compact usage tile instead of a `$0` bar.",
      "- Legacy zero-total rows also render the usage tile, so current callers do not show misleading `$0.00`.",
      "- Non-zero cost rows still use the existing bar rendering path.",
      "- The tile shows `tokens_in`, `tokens_out`, `duration_ms`, and the inline label `cost not reported by CLI`.",
      "",
      "Verification:",
      "- Ran `npm run build` in `apps/studio/frontend`; build passed with type checking.",
      "- No npm dependencies were added.",
      "",
      "Note: the Next build uses/leaves `apps/studio/frontend/.next/` as an untracked build artifact in this checkout. Source edits were limited to `apps/studio/frontend/components/CostChart.tsx`.",
      "",
      "Next useful step:",
      "1. Have the flow detail integration pass aggregate `tokens_in`, `tokens_out`, and `duration_ms` into each `CostChartRow` so the new tiles show real branch metrics.",
    ].join("\n"),
    tool_name: null,
    tool_id: null,
    tool_input: null,
    tool_output: null,
    is_delta: 0,
    is_error: 0,
    metadata: "{}",
  },
  {
    id: 184,
    flow_id: "20260420T183524-153e9455",
    branch_id: "79939d24-d096-4d39-a369-6861021b0891",
    api_call_id: "31d928a4-c777-4560-a33c-2d41b29594cf",
    ts: 1776725538.020176888,
    chunk_type: "text",
    content: [
      "Implemented Phase 4c empty-state microcopy only, editing the four requested files:",
      "",
      '- [tasks/page.tsx](/Users/lion/projects/lionagi/apps/studio/frontend/app/tasks/page.tsx:164): added `li task new "your description" --check "your verification command"`',
      "- [teams/page.tsx](/Users/lion/projects/lionagi/apps/studio/frontend/app/teams/page.tsx:200): noted there is no `li team` CLI and teams are created via `services/team/`",
      "- [schedules/page.tsx](/Users/lion/projects/lionagi/apps/studio/frontend/app/schedules/page.tsx:187): added `li schedule add ... --lambda ... --instruction ... --every ...` with the exact CLI flags",
      "- [api-calls/page.tsx](/Users/lion/projects/lionagi/apps/studio/frontend/app/api-calls/page.tsx:470): added `li o flow -p path/to/flow.yaml` and noted calls are recorded when flows run",
      "",
      "Verification passed with:",
      "",
      "```bash",
      "cd /Users/lion/projects/lionagi/apps/studio/frontend",
      "./node_modules/.bin/tsc --noEmit --incremental false --pretty false",
      "```",
      "",
      "I did not run `npm run build` because it would write Next build artifacts; the no-emit typecheck kept the verification read-only. Next useful step: run the Phase 5 `npm run build` when you're ready for full monitor validation.",
    ].join("\n"),
    tool_name: null,
    tool_id: null,
    tool_input: null,
    tool_output: null,
    is_delta: 0,
    is_error: 0,
    metadata: "{}",
  },
  {
    id: 185,
    flow_id: "20260420T183524-153e9455",
    branch_id: "682e31b0-d14d-41be-82a9-ef8af0912fc9",
    api_call_id: "9950b6f7-2892-4061-abf8-b172473aebf0",
    ts: 1776725626.334614038,
    chunk_type: "text",
    content: [
      "Implemented Phase 4a in [page.tsx](/Users/lion/projects/lionagi/apps/studio/frontend/app/flows/[id]/page.tsx:283).",
      "",
      "Changes:",
      "- Added DAG branch fallback: when the event-derived DAG has no nodes but branch/agent evidence exists, the page feeds `DagGraph` one node per branch with no edges.",
      "- Updated flow cost display: reported positive costs still go through `CostChart`; CLI/unreported rows are shown separately with `tokens_in`, `tokens_out`, `duration_ms`, and `cost not reported by CLI endpoint`, so `$0.00` is no longer shown for missing CLI cost data.",
      "- Added top section nav for `#dag`, `#cost`, `#timeline`, `#branches`, and `#messages`, with matching section ids and scroll margins.",
      "",
      "Verification:",
      "- `cd apps/studio/frontend && npm run build` passed.",
      "- I did not modify `CostChart.tsx` or any sibling-owned empty-state pages.",
      "- `DagGraph.tsx` was read and its API was preserved; no internal change was needed because the fallback belongs at the caller data layer.",
      "",
      "One follow-up worth doing in Phase 5: run the monitor against a completed event-less flow and confirm the DAG panel shows branch nodes with no edges in the live UI.",
    ].join("\n"),
    tool_name: null,
    tool_id: null,
    tool_input: null,
    tool_output: null,
    is_delta: 0,
    is_error: 0,
    metadata: "{}",
  },
  {
    id: 194,
    flow_id: "20260420T183524-153e9455",
    branch_id: "40d255da-1bc2-4c8a-bdc6-d9f358ef95aa",
    api_call_id: "98f4dcbe-2584-4afd-a7d8-3573fb725f0e",
    ts: 1776725793.938086986,
    chunk_type: "text",
    content: [
      "Phase 5 verification is complete, with no application fixes attempted.",
      "",
      "Report artifact written: [.khive/flows/lion-studio-init/artifacts/test_report.md](/Users/lion/projects/lionagi/.khive/flows/lion-studio-init/artifacts/test_report.md:1)",
      "",
      "Results:",
      "",
      "- `cd /Users/lion/projects/lionagi/apps/studio/frontend && npm run build`",
      "  - Exit code: `0`",
      "  - Result: build passed; Next.js compiled successfully and generated all 11 static pages.",
      "",
      "- `curl -s http://localhost:8765/api/stats`",
      "  - Exit code: `7`",
      "  - Result: empty response; JSON parse failed with `Unexpected end of JSON input`.",
      "  - Could not verify `tasks > 0` or `schedules > 0`.",
      "",
      "- `curl -s http://localhost:8765/api/flows`",
      "  - Exit code: `7`",
      "  - Result: empty response; JSON parse failed with `Unexpected end of JSON input`.",
      "  - Could not confirm any flow has non-empty `messages` or `branches`.",
      "",
      "Based on the requested output, the build is good, but the API-backed P0 verification failed because `localhost:8765` was unreachable from the test shell.",
      "",
      "Concrete next steps:",
      "",
      "1. Start or restore the backend on `localhost:8765`, then rerun Phase 5.",
      "2. Let the critic consume the current `test_report.md` as a failed API verification artifact.",
    ].join("\n"),
    tool_name: null,
    tool_id: null,
    tool_input: null,
    tool_output: null,
    is_delta: 0,
    is_error: 0,
    metadata: "{}",
  },
];

describe("extractAttending", () => {
  it("dedupes and ranks real file references by recency", () => {
    const defaultItems = extractAttending(goldenChunks);

    assert.equal(defaultItems.length, 5);
    assert.equal(defaultItems[0].path.endsWith("test_report.md"), true);
    assert.equal(defaultItems[0].lineStart, 1);
    assert.equal(defaultItems[0].weight, 1);
    assert.equal(defaultItems[1].weight < defaultItems[0].weight, true);

    const wideItems = extractAttending(goldenChunks, { limit: 10 });
    assert.equal(
      wideItems.some((item) => item.path.endsWith("CostChart.tsx") && item.lineStart === 12),
      true,
    );
    assert.equal(
      wideItems.some((item) => item.path.endsWith("persist.py") && item.lineStart === 318),
      true,
    );
  });
});

describe("extractThinking", () => {
  it("extracts recent real decision lines as a completed timeline", () => {
    const items = extractThinking(goldenChunks);
    const texts = items.map((item) => item.text);

    assert.equal(items.length, 4);
    assert.equal(
      items.every((item) => item.status === "complete"),
      true,
    );
    assert.equal(
      texts.some((text) => text.includes("Implemented Phase 4c")),
      true,
    );
    assert.equal(
      texts.some((text) => text.includes("Implemented Phase 4a")),
      true,
    );
    assert.equal(
      texts.some((text) => text.includes("Phase 5 verification is complete")),
      true,
    );
  });
});

describe("extractEmitting", () => {
  it("detects emitted artifacts and edited files from real chunk text", () => {
    const items = extractEmitting(goldenChunks, undefined, { limit: 10 });

    assert.equal(
      items.some(
        (item) => item.name === "audit.md" && item.kind === "artifact" && item.status === "done",
      ),
      true,
    );
    assert.equal(
      items.some(
        (item) =>
          item.name === "test_report.md" && item.kind === "artifact" && item.status === "done",
      ),
      true,
    );
    assert.equal(
      items.some(
        (item) => item.name === "CostChart.tsx" && item.kind === "file" && item.status === "done",
      ),
      true,
    );
  });
});
