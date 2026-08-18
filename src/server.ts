import { once } from "node:events";
import type { Server } from "node:http";
import type { AddressInfo } from "node:net";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { createApp } from "./app.js";
import {
  assertSecureRuntimeConfig,
  loadConfig,
  type RuntimeConfig,
} from "./config.js";
import { Store } from "./store.js";

interface Logger {
  log(message: string): void;
  error(message: string, error?: unknown): void;
}

interface StartOptions {
  installSignalHandlers?: boolean;
  logger?: Logger;
}

export interface RunningServer {
  server: Server;
  store: Store;
  url: string;
  shutdown(signal?: string): Promise<void>;
}

export async function startServer(
  config: RuntimeConfig = loadConfig(),
  options: StartOptions = {},
): Promise<RunningServer> {
  assertSecureRuntimeConfig(config);
  const logger = options.logger ?? console;
  const store = new Store(config.dataFile, {
    legacyJsonPath: config.legacyDataFile,
  });

  try {
    if (config.seedDemo) {
      const seeded = store.seedDemo();
      logger.log(
        seeded
          ? "Demo data seeded."
          : "Demo data already seeded; no changes made.",
      );
    }

    const app = createApp(store, {
      apiToken: config.apiToken,
      resetEnabled: config.resetEnabled,
      publicDir: config.publicDir,
      rateLimit: config.rateLimit,
      trustProxy: config.trustProxy,
      hyperliquid: {
        infoUrl: config.hyperliquidInfoUrl,
        coins: config.hyperliquidCoins,
        address: config.hyperliquidAddress,
      },
    });
    const server = app.listen(config.port, config.host);

    try {
      await once(server, "listening");
    } catch (error) {
      store.close();
      throw error;
    }

    const address = server.address() as AddressInfo;
    const displayHost = config.host.includes(":")
      ? `[${config.host}]`
      : config.host;
    const url = `http://${displayHost}:${address.port}`;
    logger.log(`agentic-money listening on ${url}`);

    let shutdownPromise: Promise<void> | undefined;
    const signalHandlers = new Map<NodeJS.Signals, () => void>();
    const removeSignalHandlers = (): void => {
      for (const [signal, handler] of signalHandlers) {
        process.off(signal, handler);
      }
      signalHandlers.clear();
    };

    const shutdown = (signal = "shutdown"): Promise<void> => {
      if (shutdownPromise !== undefined) return shutdownPromise;
      logger.log(`Graceful shutdown requested (${signal}).`);
      removeSignalHandlers();

      shutdownPromise = new Promise<void>((resolveShutdown, rejectShutdown) => {
        const forceTimer = setTimeout(() => {
          logger.error("Graceful shutdown timed out; closing open connections.");
          server.closeAllConnections();
        }, 10_000);
        forceTimer.unref();

        server.close((error) => {
          clearTimeout(forceTimer);
          store.close();
          if (error !== undefined) {
            rejectShutdown(error);
            return;
          }
          logger.log("Server stopped cleanly.");
          resolveShutdown();
        });
      });
      return shutdownPromise;
    };

    if (options.installSignalHandlers !== false) {
      for (const signal of ["SIGINT", "SIGTERM"] as const) {
        const handler = (): void => {
          void shutdown(signal).catch((error: unknown) => {
            logger.error("Graceful shutdown failed.", error);
            process.exitCode = 1;
          });
        };
        signalHandlers.set(signal, handler);
        process.once(signal, handler);
      }
    }

    server.once("close", () => {
      removeSignalHandlers();
      store.close();
    });
    server.on("error", (error) => {
      logger.error("HTTP server error.", error);
    });

    return { server, store, url, shutdown };
  } catch (error) {
    store.close();
    throw error;
  }
}

async function main(): Promise<void> {
  try {
    await startServer();
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    console.error(`Startup failed: ${message}`);
    process.exitCode = 1;
  }
}

const invokedPath = process.argv[1] ? resolve(process.argv[1]) : undefined;
if (invokedPath === fileURLToPath(import.meta.url)) {
  void main();
}
