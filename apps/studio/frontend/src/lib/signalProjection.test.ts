import { describe, expect, it } from "vitest";

import type { SignalEvent } from "./api";
import { buildNodeActivityByName } from "./nodeActivity";
import { buildNodeStatusesByName, buildOperationGraph } from "./operationGraph";
import { DEFAULT_SIGNAL_WINDOW_CAP, SignalProjection } from "./signalProjection";

function event(
  seq: number,
  kind = "HookSignal",
  opId = "",
  payload: Record<string, unknown> = {},
): SignalEvent {
  return {
    id: `event-${seq}`,
    session_id: "session-1",
    seq,
    kind,
    op_id: opId,
    ts: seq * 1_000,
    payload,
  };
}

describe("SignalProjection", () => {
  it("retains only the newest 2,000 raw rows while counting a 100k replay exactly", () => {
    const projection = new SignalProjection();

    for (let seq = 1; seq <= 100_000; seq += 1) {
      expect(projection.append(event(seq))).toBe(true);
    }

    expect(DEFAULT_SIGNAL_WINDOW_CAP).toBe(2_000);
    expect(projection.events).toHaveLength(DEFAULT_SIGNAL_WINDOW_CAP);
    expect(projection.events[0]?.seq).toBe(98_001);
    expect(projection.events.at(-1)?.seq).toBe(100_000);
    expect(projection.totalCount).toBe(100_000);
    expect(projection.hasOlder).toBe(true);
    expect(projection.oldestRetainedSeq).toBe(98_001);
  });

  it("ignores replayed sequence/id pairs without changing raw rows or projections", () => {
    const projection = new SignalProjection(3);
    const rows = [
      event(1, "NodeQueued", "op-a", { name: "draft" }),
      event(2, "NodeStarted", "op-a", { name: "draft" }),
      event(3, "NodeCompleted", "op-a", { name: "draft", elapsed: 3 }),
    ];
    for (const row of rows) expect(projection.append(row)).toBe(true);

    const before = {
      events: projection.events,
      graph: projection.operationGraph,
      statuses: Array.from(projection.nodeStatuses.entries()),
      total: projection.totalCount,
    };

    for (const row of rows) expect(projection.append(row)).toBe(false);

    expect(projection.events).toEqual(before.events);
    expect(projection.operationGraph).toEqual(before.graph);
    expect(Array.from(projection.nodeStatuses.entries())).toEqual(before.statuses);
    expect(projection.totalCount).toBe(before.total);
  });

  it("keeps complete incremental projections equal to the full-history reference folds", () => {
    const rows = [
      event(1, "NodeSpawned", "op-b", { parent_id: "op-a", independent: true }),
      event(2, "NodeQueued", "op-a", { name: "draft" }),
      event(3, "NodeStarted", "op-a", { name: "draft" }),
      event(4, "AssistantDelta", "op-a", { text: "working", token_count: 12 }),
      event(5, "NodeEscalated", "op-a", {
        name: "draft",
        route: "higher_tier",
      }),
      event(6, "NodeQueued", "op-b", {
        parent_id: "op-a",
        depends_on: ["op-a"],
      }),
      event(7, "NodeStarted", "op-b", { name: "review" }),
      event(8, "ToolCallStarted", "op-b", { tool_name: "pytest" }),
      event(9, "NodeCompleted", "op-b", { name: "review", elapsed: 4 }),
      event(10, "StructuredOutput", "", {
        data: {
          gate_verdict: "request-changes",
          findings: [{ severity: "high" }, { severity: "low" }],
        },
      }),
    ];
    const projection = new SignalProjection(4);
    for (const row of rows) projection.append(row);

    expect(projection.events.map((row) => row.seq)).toEqual([7, 8, 9, 10]);
    expect(projection.operationGraph).toEqual(buildOperationGraph(rows));
    expect(Array.from(projection.nodeStatuses.entries())).toEqual(
      Array.from(buildNodeStatusesByName(rows).entries()),
    );
    expect(Array.from(projection.nodeActivity.entries())).toEqual(
      Array.from(buildNodeActivityByName(rows).entries()),
    );
    expect(projection.gateOutcome).toEqual({
      verdict: "request-changes",
      major: 1,
      minor: 1,
      hasFindings: true,
    });
    expect(projection.laneSummaries).toEqual([
      { op_id: "op-b", lane: "succeeded", count: 5 },
      { op_id: "op-a", lane: "escalated", count: 4 },
    ]);
  });

  it("retroactively correlates compact pre-name activity without retaining its raw rows", () => {
    const rows = [
      event(1, "AssistantDelta", "op-a", { text: "early" }),
      event(2, "AssistantDelta", "op-b", { name: "draft", text: "middle" }),
      event(3, "NodeStarted", "op-a", { name: "draft" }),
      event(4, "AssistantDelta", "op-a", { text: "latest", token_count: 7 }),
    ];
    const projection = new SignalProjection(1);
    for (const row of rows) projection.append(row);

    expect(projection.events).toHaveLength(1);
    expect(projection.nodeActivity.get("draft")).toEqual(
      buildNodeActivityByName(rows).get("draft"),
    );
  });
});
