/**
 * Attention page filter contract — "active" is the default, discharged
 * items only surface under "discharged"/"all", and "all" never drops or
 * duplicates an item present in exactly one of the two source lists.
 */
import { describe, it, expect } from "vitest";
import { itemsForFilter } from "./attention";
import type { AttentionItem } from "@/components/mission/boardReducer";

function item(id: string): AttentionItem {
  return {
    id,
    kind: "run",
    name: id,
    reason: "failed",
    startedAt: 0,
    href: `/runs/${id}`,
    status: "failed",
  };
}

describe("itemsForFilter", () => {
  const active = [item("run:a"), item("run:b")];
  const discharged = [item("run:c")];

  it("active returns only the active list", () => {
    expect(itemsForFilter("active", active, discharged)).toEqual(active);
  });

  it("discharged returns only the discharged list", () => {
    expect(itemsForFilter("discharged", active, discharged)).toEqual(discharged);
  });

  it("all concatenates active and discharged without dropping either", () => {
    expect(itemsForFilter("all", active, discharged)).toEqual([...active, ...discharged]);
  });

  it("all returns an empty list when both sources are empty", () => {
    expect(itemsForFilter("all", [], [])).toEqual([]);
  });
});
