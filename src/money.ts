const MAX_MINOR_UNITS = BigInt(Number.MAX_SAFE_INTEGER);
const MAX_MAJOR_UNITS = Number.MAX_SAFE_INTEGER / 100;

/**
 * Convert major currency units into an integer number of minor units.
 * Decimal conversion is used instead of binary floating-point multiplication
 * so values exactly halfway between cents round away from zero consistently.
 */
export function toMinorUnits(value: number, field = "value"): number {
  if (!Number.isFinite(value)) {
    throw new RangeError(`${field} must be a finite number`);
  }
  if (Math.abs(value) > MAX_MAJOR_UNITS) {
    throw new RangeError(`${field} is too large`);
  }

  const negative = value < 0;
  const [coefficient, exponentText] = Math.abs(value)
    .toString()
    .toLowerCase()
    .split("e");
  const exponent = exponentText === undefined ? 0 : Number(exponentText);
  const [whole, fraction = ""] = coefficient.split(".");
  const digits = BigInt(`${whole}${fraction}`);
  const minorExponent = exponent - fraction.length + 2;

  let minor: bigint;
  if (minorExponent >= 0) {
    minor = digits * 10n ** BigInt(minorExponent);
  } else {
    const divisor = 10n ** BigInt(-minorExponent);
    const quotient = digits / divisor;
    const remainder = digits % divisor;
    minor = quotient + (remainder * 2n >= divisor ? 1n : 0n);
  }

  if (negative) minor = -minor;
  if (minor > MAX_MINOR_UNITS || minor < -MAX_MINOR_UNITS) {
    throw new RangeError(`${field} is too large`);
  }

  return Number(minor);
}

export function fromMinorUnits(value: number): number {
  if (!Number.isSafeInteger(value)) {
    throw new RangeError("stored monetary value is not a safe integer");
  }
  return value / 100;
}

export function normalizeMoney(value: number, field = "value"): number {
  return fromMinorUnits(toMinorUnits(value, field));
}
