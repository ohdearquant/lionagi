/**
 * Schedules route deep-link contract.
 *
 * ?s=<id> must open the detail modal on subsequent in-app navigations, not
 * only on a fresh mount — the fix is deriving modal visibility straight from
 * the reactive route search rather than a useState initializer that only
 * evaluates once.
 */
import { describe, it, expect } from "vitest";
import * as fs from "node:fs";
import * as path from "node:path";
import { validateScheduleSearch } from "./index";

const ROUTE_FILE = path.resolve(__dirname, "index.tsx");

describe("validateScheduleSearch", () => {
  it("accepts s independently of create", () => {
    expect(validateScheduleSearch({ s: "sched-1" })).toEqual({ s: "sched-1" });
  });

  it("preserves create + prefill fields together", () => {
    expect(
      validateScheduleSearch({
        create: "1",
        name: "Nightly digest",
        cron: "0 6 * * *",
        prompt: "summarize",
        desc: "daily",
      }),
    ).toEqual({
      create: "1",
      name: "Nightly digest",
      cron: "0 6 * * *",
      prompt: "summarize",
      desc: "daily",
    });
  });

  it("drops non-string or empty values", () => {
    expect(validateScheduleSearch({ s: "", create: 1 })).toEqual({});
  });
});

describe("routes/schedules/index.tsx — reactive ?s= deep link", () => {
  const src = fs.readFileSync(ROUTE_FILE, "utf-8");

  it("derives the open schedule directly from the route search, not local state", () => {
    expect(src).toMatch(/search\.s\s*&&[\s\S]{0,40}<ScheduleDetailModal/);
  });

  it("does not reintroduce a local selected-schedule id state", () => {
    expect(src).not.toMatch(/useState[^;]*[Ss]elected(Schedule)?Id/);
  });
});
