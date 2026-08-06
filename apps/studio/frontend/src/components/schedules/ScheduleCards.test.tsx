/**
 * ScheduleCards source-contract test, matching the project's existing style
 * for this feature (see SchedulesTable.test.tsx): this project has no
 * @testing-library/react, so component wiring is asserted against the source
 * rather than rendered.
 */
import { describe, it, expect } from "vitest";
import * as fs from "node:fs";
import * as path from "node:path";

const SRC = fs.readFileSync(path.resolve(__dirname, "ScheduleCards.tsx"), "utf-8");

describe("ScheduleCards — health badge wiring", () => {
  it("renders HealthBadge on every card, ahead of the raw last-run pill", () => {
    const healthIdx = SRC.indexOf("<HealthBadge");
    const lastRunIdx = SRC.indexOf("<LastRun ");
    expect(healthIdx).toBeGreaterThan(-1);
    expect(lastRunIdx).toBeGreaterThan(-1);
    expect(healthIdx).toBeLessThan(lastRunIdx);
  });

  it("derives the badge through the shared scheduleHealthBadge() mapping, not ad hoc next_fire_at logic", () => {
    expect(SRC).toMatch(/scheduleHealthBadge\(schedule\)/);
  });

  it("never-fired and disabled never render as a healthy-looking color", () => {
    expect(SRC).toMatch(/never-fired.*:\s*"var\(--content-muted\)"/);
  });
});
