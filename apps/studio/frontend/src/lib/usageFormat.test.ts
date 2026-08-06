/**
 * usageFormat contract tests.
 *
 * The behavior under test: `null`/`undefined` (unreported/unknown) must
 * never render the same as a genuine `0` — the two are distinct facts about
 * the underlying cost and the formatter must keep them visually distinct.
 */

import { describe, it, expect } from "vitest";
import { formatCostLowerBound, formatCostUsd, formatTokenCount } from "./usageFormat";

describe("formatCostUsd", () => {
  it("renders null as an em dash, not $0.00", () => {
    expect(formatCostUsd(null)).toBe("—");
  });

  it("renders undefined as an em dash", () => {
    expect(formatCostUsd(undefined)).toBe("—");
  });

  it("renders a genuine zero as $0.00, distinct from unreported", () => {
    expect(formatCostUsd(0)).toBe("$0.00");
    expect(formatCostUsd(0)).not.toBe(formatCostUsd(null));
  });

  it("renders a normal positive value at 2 decimals", () => {
    expect(formatCostUsd(12.3)).toBe("$12.30");
    expect(formatCostUsd(1.005)).toMatch(/^\$1\.0[01]$/);
  });

  it("renders a sub-cent value at 4 decimals so it doesn't round to the unreported-adjacent $0.00", () => {
    expect(formatCostUsd(0.0034)).toBe("$0.0034");
  });

  it("large values still render at 2 decimals", () => {
    expect(formatCostUsd(1234.5)).toBe("$1234.50");
  });
});

describe("formatTokenCount", () => {
  it("renders null/undefined as an em dash", () => {
    expect(formatTokenCount(null)).toBe("—");
    expect(formatTokenCount(undefined)).toBe("—");
  });

  it("renders a genuine zero as 0, distinct from unreported", () => {
    expect(formatTokenCount(0)).toBe("0");
  });

  it("renders sub-1000 counts verbatim", () => {
    expect(formatTokenCount(842)).toBe("842");
  });

  it("renders thousands with a k suffix", () => {
    expect(formatTokenCount(4200)).toBe("4.2k");
    expect(formatTokenCount(42000)).toBe("42k");
  });

  it("renders millions with an m suffix", () => {
    expect(formatTokenCount(2_500_000)).toBe("2.5m");
  });
});

describe("formatCostLowerBound", () => {
  it("renders null as unreported, not a bound on nothing", () => {
    expect(formatCostLowerBound(null)).toBe("—");
  });

  it("prefixes a reported sum with a lower-bound marker", () => {
    expect(formatCostLowerBound(12.34)).toBe("≥ $12.34");
  });

  it("marks a genuine zero sum as a lower bound too (some rows unreported)", () => {
    expect(formatCostLowerBound(0)).toBe("≥ $0.00");
  });
});
