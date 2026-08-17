import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const projectRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const entryPoint = join(projectRoot, "dist", "server.js");
assert.ok(
  existsSync(entryPoint),
  "compiled entry point is missing; run npm run build first",
);
assert.equal(
  existsSync(join(projectRoot, "dist", "src", "server.js")),
  false,
  "stale nested entry point should not be emitted",
);
const compiledConfig = await import(
  `${pathToFileURL(join(projectRoot, "dist", "config.js")).href}?smoke=${Date.now()}`
);
assert.equal(
  compiledConfig.DEFAULT_DATA_FILE,
  join(projectRoot, "data", "agentic-money.sqlite"),
  "compiled default data path must remain anchored to the project root",
);
assert.equal(
  compiledConfig.PUBLIC_DIR,
  join(projectRoot, "public"),
  "compiled static path must remain anchored to the project root",
);

const temporaryDirectory = await mkdtemp(
  join(tmpdir(), "agentic-money-smoke-"),
);
const apiToken = "production-smoke-token";
const environment = {
  ...process.env,
  HOST: "127.0.0.1",
  PORT: "0",
  DATA_FILE: join(temporaryDirectory, "smoke.sqlite"),
  ENABLE_RESET: "false",
  SEED_DEMO: "false",
  API_TOKEN: apiToken,
  TRUST_PROXY: "0",
};
delete environment.LEGACY_DATA_FILE;

const child = spawn(process.execPath, [entryPoint], {
  cwd: projectRoot,
  env: environment,
  stdio: ["ignore", "pipe", "pipe"],
});

let stdout = "";
let stderr = "";
child.stdout.setEncoding("utf8");
child.stderr.setEncoding("utf8");
child.stdout.on("data", (chunk) => {
  stdout += chunk;
});
child.stderr.on("data", (chunk) => {
  stderr += chunk;
});

try {
  const url = await waitForUrl(child, () => stdout, () => stderr);
  const [dashboard, script, health] = await Promise.all([
    fetch(`${url}/`),
    fetch(`${url}/app.js`),
    fetch(`${url}/api/health`),
  ]);

  assert.equal(dashboard.status, 200);
  assert.match(
    dashboard.headers.get("content-security-policy") ?? "",
    /default-src 'self'/,
  );
  assert.match(await dashboard.text(), /Manage budgets/);
  assert.equal(script.status, 200);
  assert.match(await script.text(), /renderBudgets/);
  assert.equal(health.status, 200);
  assert.deepEqual(await health.json(), {
    status: "ok",
    storage: {
      status: "ok",
      type: "sqlite",
      persistent: true,
    },
  });

  const mutationStatuses = [];
  for (const transaction of [
    { description: "Smoke test coffee", amount: -4.25 },
    { description: "Smoke test income", amount: 10 },
  ]) {
    const response = await fetch(`${url}/api/transactions`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${apiToken}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(transaction),
    });
    mutationStatuses.push(response.status);
    const created = await response.json();
    assert.equal(created.description, transaction.description);
  }
  assert.deepEqual(
    mutationStatuses,
    [201, 201],
    "authenticated compiled mutations should succeed",
  );

  const firstPageResponse = await fetch(
    `${url}/api/transactions?limit=1&offset=0`,
  );
  assert.equal(firstPageResponse.status, 200);
  const firstPage = await firstPageResponse.json();
  assert.equal(firstPage.data.length, 1);
  assert.deepEqual(firstPage.pagination, {
    limit: 1,
    offset: 0,
    total: 2,
    hasMore: true,
  });

  const secondPageResponse = await fetch(
    `${url}/api/transactions?limit=1&offset=1`,
  );
  assert.equal(secondPageResponse.status, 200);
  const secondPage = await secondPageResponse.json();
  assert.equal(secondPage.data.length, 1);
  assert.deepEqual(secondPage.pagination, {
    limit: 1,
    offset: 1,
    total: 2,
    hasMore: false,
  });

  child.kill("SIGTERM");
  const result = await waitForExit(child, 12_000);
  assert.equal(result.signal, null, `server exited via ${result.signal}`);
  assert.equal(
    result.code,
    0,
    `server exited with ${result.code}\nstdout:\n${stdout}\nstderr:\n${stderr}`,
  );
  assert.match(stdout, /Server stopped cleanly\./);

  console.log(
    "Production smoke passed: compiled server, static dashboard, authenticated mutation, pagination, persistent health, and graceful shutdown.",
  );
} finally {
  if (child.exitCode === null && child.signalCode === null) {
    child.kill("SIGTERM");
    await waitForExit(child, 12_000).catch(() => {
      child.kill("SIGKILL");
    });
  }
  await rm(temporaryDirectory, { recursive: true, force: true });
}

function waitForUrl(process, getStdout, getStderr) {
  return new Promise((resolveUrl, rejectUrl) => {
    const timeout = setTimeout(() => {
      rejectUrl(
        new Error(
          `server did not start in time\nstdout:\n${getStdout()}\nstderr:\n${getStderr()}`,
        ),
      );
    }, 10_000);

    const inspectOutput = () => {
      const match = getStdout().match(
        /agentic-money listening on (http:\/\/127\.0\.0\.1:\d+)/,
      );
      if (match) {
        cleanup();
        resolveUrl(match[1]);
      }
    };
    const exited = (code, signal) => {
      cleanup();
      rejectUrl(
        new Error(
          `server exited before listening (${code ?? signal})\nstdout:\n${getStdout()}\nstderr:\n${getStderr()}`,
        ),
      );
    };
    const cleanup = () => {
      clearTimeout(timeout);
      process.stdout.off("data", inspectOutput);
      process.off("exit", exited);
    };

    process.stdout.on("data", inspectOutput);
    process.once("exit", exited);
    inspectOutput();
  });
}

function waitForExit(process, timeoutMs) {
  if (process.exitCode !== null || process.signalCode !== null) {
    return Promise.resolve({
      code: process.exitCode,
      signal: process.signalCode,
    });
  }

  return new Promise((resolveExit, rejectExit) => {
    const timeout = setTimeout(() => {
      cleanup();
      rejectExit(new Error("server did not exit in time"));
    }, timeoutMs);
    const exited = (code, signal) => {
      cleanup();
      resolveExit({ code, signal });
    };
    const cleanup = () => {
      clearTimeout(timeout);
      process.off("exit", exited);
    };
    process.once("exit", exited);
  });
}
