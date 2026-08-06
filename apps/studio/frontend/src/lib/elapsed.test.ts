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
});
