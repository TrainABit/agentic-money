import { dirname, isAbsolute, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

export const PROJECT_ROOT = resolve(
  dirname(fileURLToPath(import.meta.url)),
  "..",
);
export const PUBLIC_DIR = join(PROJECT_ROOT, "public");
export const DEFAULT_DATA_FILE = join(
  PROJECT_ROOT,
  "data",
  "agentic-money.sqlite",
);
export const DEFAULT_LEGACY_DATA_FILE = join(
  PROJECT_ROOT,
  "data",
  "store.json",
);

export interface RuntimeConfig {
  port: number;
  host: string;
  dataFile: string;
  legacyDataFile?: string;
  publicDir: string;
  apiToken?: string;
  resetEnabled: boolean;
  seedDemo: boolean;
  rateLimit: number;
  trustProxy: number;
}

export class ConfigurationError extends Error {
  override readonly name = "ConfigurationError";
}

export function loadConfig(
  environment: NodeJS.ProcessEnv = process.env,
): RuntimeConfig {
  const host = environment.HOST?.trim() || "127.0.0.1";
  const port = parseInteger(environment.PORT, "PORT", 0, 65_535, 3_000);
  const apiToken = optionalNonEmpty(environment.API_TOKEN, "API_TOKEN");
  const resetEnabled = parseBoolean(
    environment.ENABLE_RESET,
    "ENABLE_RESET",
  );
  const seedDemo = parseBoolean(environment.SEED_DEMO, "SEED_DEMO");
  const rateLimit = parseInteger(
    environment.API_RATE_LIMIT,
    "API_RATE_LIMIT",
    1,
    100_000,
    120,
  );
  const trustProxy = parseInteger(
    environment.TRUST_PROXY,
    "TRUST_PROXY",
    0,
    255,
    0,
  );

  assertSecureRuntimeConfig({ host, apiToken, resetEnabled });

  const configuredDataFile = optionalNonEmpty(
    environment.DATA_FILE,
    "DATA_FILE",
  );
  const configuredLegacyFile = optionalNonEmpty(
    environment.LEGACY_DATA_FILE,
    "LEGACY_DATA_FILE",
  );

  return {
    port,
    host,
    dataFile: resolveFromProject(configuredDataFile ?? DEFAULT_DATA_FILE),
    legacyDataFile:
      configuredLegacyFile !== undefined
        ? resolveFromProject(configuredLegacyFile)
        : configuredDataFile === undefined
          ? DEFAULT_LEGACY_DATA_FILE
          : undefined,
    publicDir: PUBLIC_DIR,
    apiToken,
    resetEnabled,
    seedDemo,
    rateLimit,
    trustProxy,
  };
}

export function assertSecureRuntimeConfig(
  config: Pick<RuntimeConfig, "host" | "apiToken" | "resetEnabled">,
): void {
  if (config.apiToken !== undefined && config.apiToken.trim().length === 0) {
    throw new ConfigurationError("API_TOKEN cannot be empty");
  }
  if (!isLoopbackHost(config.host) && config.apiToken === undefined) {
    throw new ConfigurationError(
      "API_TOKEN is required when HOST binds to a non-loopback interface",
    );
  }
  if (config.resetEnabled && config.apiToken === undefined) {
    throw new ConfigurationError(
      "API_TOKEN is required when ENABLE_RESET=true",
    );
  }
}

export function isLoopbackHost(host: string): boolean {
  const normalized = host.trim().toLowerCase().replace(/^\[(.*)\]$/, "$1");
  if (normalized === "localhost" || normalized === "::1") return true;
  if (normalized.startsWith("::ffff:")) {
    return isLoopbackIpv4(normalized.slice("::ffff:".length));
  }
  return isLoopbackIpv4(normalized);
}

function isLoopbackIpv4(host: string): boolean {
  const octets = host.split(".");
  if (octets.length !== 4) return false;
  if (
    !octets.every(
      (octet) =>
        /^\d{1,3}$/.test(octet) &&
        Number(octet) >= 0 &&
        Number(octet) <= 255,
    )
  ) {
    return false;
  }
  return Number(octets[0]) === 127;
}

function parseBoolean(value: string | undefined, name: string): boolean {
  if (value === undefined || value === "" || value.toLowerCase() === "false") {
    return false;
  }
  if (value.toLowerCase() === "true") return true;
  throw new ConfigurationError(`${name} must be "true" or "false"`);
}

function parseInteger(
  value: string | undefined,
  name: string,
  minimum: number,
  maximum: number,
  fallback: number,
): number {
  if (value === undefined || value === "") return fallback;
  if (!/^\d+$/.test(value)) {
    throw new ConfigurationError(`${name} must be an integer`);
  }
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed < minimum || parsed > maximum) {
    throw new ConfigurationError(
      `${name} must be between ${minimum} and ${maximum}`,
    );
  }
  return parsed;
}

function optionalNonEmpty(
  value: string | undefined,
  name: string,
): string | undefined {
  if (value === undefined) return undefined;
  if (value.trim().length === 0) {
    throw new ConfigurationError(`${name} cannot be empty`);
  }
  return value;
}

function resolveFromProject(path: string): string {
  return isAbsolute(path) ? path : resolve(PROJECT_ROOT, path);
}
