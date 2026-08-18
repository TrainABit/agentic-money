import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  DEFAULT_DATA_FILE,
  PROJECT_ROOT,
  isLoopbackHost,
  loadConfig,
} from "../src/config.js";
import { startServer, type RunningServer } from "../src/server.js";

describe("runtime configuration", () => {
  it("uses stable project-root paths and a loopback bind by default", () => {
    const config = loadConfig({});

    expect(config.host).toBe("127.0.0.1");
    expect(config.dataFile).toBe(DEFAULT_DATA_FILE);
    expect(config.dataFile).toBe(
      join(PROJECT_ROOT, "data", "agentic-money.sqlite"),
    );
    expect(config.resetEnabled).toBe(false);
    expect(config.seedDemo).toBe(false);
    expect(config.trustProxy).toBe(0);
  });

  it("recognizes loopback addresses conservatively", () => {
    expect(isLoopbackHost("localhost")).toBe(true);
    expect(isLoopbackHost("127.0.0.1")).toBe(true);
    expect(isLoopbackHost("127.99.1.2")).toBe(true);
    expect(isLoopbackHost("::1")).toBe(true);
    expect(isLoopbackHost("[::1]")).toBe(true);
    expect(isLoopbackHost("::ffff:127.0.0.1")).toBe(true);
    expect(isLoopbackHost("0.0.0.0")).toBe(false);
    expect(isLoopbackHost("192.168.1.10")).toBe(false);
    expect(isLoopbackHost("example.test")).toBe(false);
  });

  it("refuses non-loopback binds without authentication", () => {
    expect(() => loadConfig({ HOST: "0.0.0.0" })).toThrow(/API_TOKEN/);
    expect(
      loadConfig({ HOST: "0.0.0.0", API_TOKEN: "secret" }),
    ).toMatchObject({
      host: "0.0.0.0",
      apiToken: "secret",
    });
  });

  it("requires authentication to enable reset and parses explicit seeding", () => {
    expect(() => loadConfig({ ENABLE_RESET: "true" })).toThrow(/API_TOKEN/);
    expect(
      loadConfig({
        ENABLE_RESET: "true",
        SEED_DEMO: "true",
        API_TOKEN: "secret",
      }),
    ).toMatchObject({
      resetEnabled: true,
      seedDemo: true,
    });
    expect(() => loadConfig({ SEED_DEMO: "yes" })).toThrow(/true.*false/);
  });

  it("resolves relative data paths from the project root", () => {
    expect(loadConfig({ DATA_FILE: "var/custom.sqlite" }).dataFile).toBe(
      join(PROJECT_ROOT, "var", "custom.sqlite"),
    );
  });

  it("loads read-only Hyperliquid settings and refuses private keys", () => {
    const defaults = loadConfig({});
    expect(defaults.hyperliquidInfoUrl).toContain("hyperliquid-testnet");
    expect(defaults.hyperliquidCoins).toEqual(["BTC", "ETH"]);
    expect(defaults.hyperliquidAddress).toBeUndefined();

    expect(
      loadConfig({
        HYPERLIQUID_INFO_URL: "https://api.hyperliquid.xyz/info",
        HYPERLIQUID_COINS: "btc, ETH,BTC",
        HYPERLIQUID_ADDRESS: "0xabc",
      }),
    ).toMatchObject({
      hyperliquidInfoUrl: "https://api.hyperliquid.xyz/info",
      hyperliquidCoins: ["BTC", "ETH"],
      hyperliquidAddress: "0xabc",
    });

    expect(() =>
      loadConfig({ HYPERLIQUID_PRIVATE_KEY: "0xsecret" }),
    ).toThrow(/read-only Hyperliquid/);
  });

  it("accepts an explicit bounded reverse-proxy hop count", () => {
    expect(loadConfig({ TRUST_PROXY: "1" }).trustProxy).toBe(1);
    expect(loadConfig({ TRUST_PROXY: "2" }).trustProxy).toBe(2);
    expect(() => loadConfig({ TRUST_PROXY: "true" })).toThrow(/integer/);
    expect(() => loadConfig({ TRUST_PROXY: "256" })).toThrow(/0.*255/);
  });
});

describe("server lifecycle", () => {
  let directory: string;
  const servers: RunningServer[] = [];
  const logger = {
    log: () => undefined,
    error: () => undefined,
  };

  beforeEach(async () => {
    directory = await mkdtemp(join(tmpdir(), "agentic-money-server-"));
  });

  afterEach(async () => {
    await Promise.all(
      servers.splice(0).map(async (running) => {
        if (running.server.listening) await running.shutdown("test cleanup");
      }),
    );
    await rm(directory, { recursive: true, force: true });
  });

  it("starts on an ephemeral port and shuts down gracefully", async () => {
    const running = await startServer(
      loadConfig({
        HOST: "127.0.0.1",
        PORT: "0",
        DATA_FILE: join(directory, "server.sqlite"),
      }),
      { installSignalHandlers: false, logger },
    );
    servers.push(running);

    const response = await fetch(`${running.url}/api/health`);
    expect(response.status).toBe(200);
    await running.shutdown("test");
    expect(running.server.listening).toBe(false);
    expect(running.store.health().status).toBe("error");
  });

  it("enforces bind security even for programmatic startup", async () => {
    const config = loadConfig({
      DATA_FILE: join(directory, "insecure.sqlite"),
    });

    await expect(
      startServer(
        { ...config, host: "0.0.0.0", apiToken: undefined },
        { installSignalHandlers: false, logger },
      ),
    ).rejects.toThrow(/API_TOKEN/);
  });

  it("rejects startup errors without leaving a second server running", async () => {
    const first = await startServer(
      loadConfig({
        HOST: "127.0.0.1",
        PORT: "0",
        DATA_FILE: join(directory, "first.sqlite"),
      }),
      { installSignalHandlers: false, logger },
    );
    servers.push(first);
    const address = first.server.address();
    if (address === null || typeof address === "string") {
      throw new Error("expected an IP socket address");
    }

    await expect(
      startServer(
        loadConfig({
          HOST: "127.0.0.1",
          PORT: String(address.port),
          DATA_FILE: join(directory, "second.sqlite"),
        }),
        { installSignalHandlers: false, logger },
      ),
    ).rejects.toThrow(/EADDRINUSE|address already in use/);

    expect(first.server.listening).toBe(true);
  });
});
