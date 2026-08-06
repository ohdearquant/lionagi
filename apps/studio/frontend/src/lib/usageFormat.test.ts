// Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect } from "vitest";
import { formatCostUsd, formatTokenCount } from "./usageFormat";

describe("formatCostUsd", () => {
  it("renders null as em dash, distinct from a genuine zero cost", () => {
    expect(formatCostUsd(null)).toBe("—");
  });

  it("renders undefined (absent field) as em dash", () => {
    expect(formatCostUsd(undefined)).toBe("—");
  });

  it("renders a genuine zero cost as $0.00, not em dash", () => {
    expect(formatCostUsd(0.0)).toBe("$0.00");
  });

  it("renders a sub-cent cost with four decimal places", () => {
    expect(formatCostUsd(0.0003)).toBe("$0.0003");
  });

  it("distinguishes null, 0.0, and 0.0003 as three different strings", () => {
    const values = [formatCostUsd(null), formatCostUsd(0.0), formatCostUsd(0.0003)];
    expect(new Set(values).size).toBe(3);
  });

  it("renders exactly $0.01 with two decimal places, at the sub-cent threshold", () => {
    expect(formatCostUsd(0.01)).toBe("$0.01");
  });

  it("renders costs above the cent threshold with two decimal places", () => {
    expect(formatCostUsd(12.5)).toBe("$12.50");
  });

  it("renders a value just under the cent threshold with four decimal places", () => {
    expect(formatCostUsd(0.0099)).toBe("$0.0099");
  });

  it("treats NaN as unreported, same as null", () => {
    expect(formatCostUsd(Number.NaN)).toBe("—");
  });
});

describe("formatTokenCount", () => {
  it("renders null as em dash", () => {
    expect(formatTokenCount(null)).toBe("—");
  });

  it("renders undefined as em dash", () => {
    expect(formatTokenCount(undefined)).toBe("—");
  });

  it("renders a genuine zero count as 0, not em dash", () => {
    expect(formatTokenCount(0)).toBe("0");
  });

  it("groups large counts with thousands separators", () => {
    expect(formatTokenCount(1234567)).toBe("1,234,567");
  });

  it("does not add separators below the first thousands boundary", () => {
    expect(formatTokenCount(987)).toBe("987");
  });
});
