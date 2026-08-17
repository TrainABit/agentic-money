import { describe, expect, it } from "vitest";
import {
  fromMinorUnits,
  normalizeMoney,
  toMinorUnits,
} from "../src/money.js";

describe("minor-unit money conversion", () => {
  it("rounds decimal halves away from zero", () => {
    expect(toMinorUnits(1.005)).toBe(101);
    expect(toMinorUnits(-1.005)).toBe(-101);
    expect(normalizeMoney(12.345)).toBe(12.35);
    expect(normalizeMoney(-12.345)).toBe(-12.35);
  });

  it("handles exponent notation and values below one cent", () => {
    expect(toMinorUnits(1e-2)).toBe(1);
    expect(toMinorUnits(0.0049)).toBe(0);
    expect(toMinorUnits(0.005)).toBe(1);
  });

  it("rejects all non-finite and unsafe values", () => {
    expect(() => toMinorUnits(Number.NaN)).toThrow(/finite/);
    expect(() => toMinorUnits(Number.POSITIVE_INFINITY)).toThrow(/finite/);
    expect(() => toMinorUnits(Number.NEGATIVE_INFINITY)).toThrow(/finite/);
    expect(() => toMinorUnits(Number.MAX_VALUE)).toThrow(/too large/);
    expect(() => fromMinorUnits(Number.MAX_SAFE_INTEGER + 1)).toThrow(
      /safe integer/,
    );
  });
});
