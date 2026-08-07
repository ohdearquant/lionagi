import { describe, expect, it } from "vitest";
import { formatElapsed } from "./elapsed";

describe("formatElapsed", () => {
  it("renders a dash for missing or invalid input", () => {
    expect(formatElapsed(null)).toBe("—");
    expect(formatElapsed(undefined)).toBe("—");
    expect(formatElapsed(Number.NaN)).toBe("—");
    expect(formatElapsed(Number.POSITIVE_INFINITY)).toBe("—");
    expect(formatElapsed(-5)).toBe("—");
  });

  it("formats seconds and minutes with the default options", () => {
    expect(formatElapsed(0)).toBe("0s");
    expect(formatElapsed(59)).toBe("59s");
    expect(formatElapsed(60)).toBe("1m");
    expect(formatElapsed(61)).toBe("1m 1s");
    expect(formatElapsed(3599)).toBe("59m 59s");
  });

  it("omits leftover seconds when showSeconds is false", () => {
    expect(formatElapsed(61, { showSeconds: false })).toBe("1m");
    expect(formatElapsed(59, { showSeconds: false })).toBe("59s");
  });

  it("formats hours without a day cap by default", () => {
    expect(formatElapsed(3600)).toBe("1h");
    expect(formatElapsed(3660)).toBe("1h 1m");
    expect(formatElapsed(30 * 3600)).toBe("30h");
  });

  it("rolls hours into days when capAtDays is set", () => {
    expect(formatElapsed(24 * 3600 - 60, { capAtDays: true })).toBe("23h 59m");
    expect(formatElapsed(24 * 3600, { capAtDays: true })).toBe("1d");
    expect(formatElapsed(24 * 3600 + 3600, { capAtDays: true })).toBe("1d 1h");
    expect(formatElapsed(62 * 3600 + 34 * 60, { capAtDays: true })).toBe("2d 14h");
  });

  it("floors sub-minute spans by default, even with a fraction", () => {
    expect(formatElapsed(59.9)).toBe("59s");
  });

  it("keeps one decimal for raw sub-minute spans when subMinuteDecimal is set", () => {
    expect(formatElapsed(0, { subMinuteDecimal: true })).toBe("0s");
    expect(formatElapsed(0.94, { subMinuteDecimal: true })).toBe("0.9s");
    expect(formatElapsed(59.9, { subMinuteDecimal: true })).toBe("59.9s");
    expect(formatElapsed(5, { subMinuteDecimal: true })).toBe("5s");
  });

  it("floors to whole minute units past the 60s boundary regardless of subMinuteDecimal", () => {
    // Guards against the old wart where a fractional span rendered as "1m 0.9s".
    expect(formatElapsed(60.9, { subMinuteDecimal: true })).toBe("1m");
    expect(formatElapsed(60.9, { subMinuteDecimal: true, showSeconds: true })).toBe("1m");
  });
});
