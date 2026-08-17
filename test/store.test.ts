import { execFile } from "node:child_process";
import { readFile, writeFile, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import Database from "better-sqlite3";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  DataIntegrityError,
  Store,
  type StoreOptions,
} from "../src/store.js";
import type { Budget } from "../src/types.js";

const execFileAsync = promisify(execFile);
const projectRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const writerPath = join(projectRoot, "test", "helpers", "store-writer.ts");

describe("Store", () => {
  let directory: string;
  let stores: Store[];

  beforeEach(async () => {
    directory = await mkdtemp(join(tmpdir(), "agentic-money-store-"));
    stores = [];
  });

  afterEach(async () => {
    for (const store of stores) store.close();
    await rm(directory, { recursive: true, force: true });
  });

  function open(path?: string, options?: StoreOptions): Store {
    const store = new Store(path, options);
    stores.push(store);
    return store;
  }

  it("persists normalized cents across restarts", async () => {
    const databasePath = join(directory, "money.sqlite");
    const first = open(databasePath);
    const transaction = first.addTransaction({
      description: "  Consulting payment  ",
      amount: 1.005,
      category: "income",
    });
    const budget = first.setBudget({ category: "other", limit: 10.005 });

    expect(transaction).toMatchObject({
      description: "Consulting payment",
      amount: 1.01,
    });
    expect(budget.limit).toBe(10.01);
    first.close();

    const second = open(databasePath);
    expect(second.listTransactions()).toEqual([transaction]);
    expect(second.listBudgets()).toEqual([budget]);
    expect((await readFile(databasePath)).subarray(0, 16).toString()).toBe(
      "SQLite format 3\u0000",
    );
  });

  it("supports bounded pagination with total metadata", () => {
    const store = open();
    for (let index = 0; index < 5; index += 1) {
      store.addTransaction({
        description: `Item ${index}`,
        amount: -(index + 1),
        category: "other",
      });
    }

    const first = store.getTransactionsPage({ limit: 2, offset: 0 });
    const second = store.getTransactionsPage({ limit: 2, offset: 2 });

    expect(first.pagination).toEqual({
      limit: 2,
      offset: 0,
      total: 5,
      hasMore: true,
    });
    expect(second.data).toHaveLength(2);
    expect(second.data.map(({ id }) => id)).not.toEqual(
      first.data.map(({ id }) => id),
    );
    expect(() => store.getTransactionsPage({ limit: 101 })).toThrow(/limit/);
  });

  it("coordinates writes from separate processes", async () => {
    const databasePath = join(directory, "concurrent.sqlite");
    open(databasePath).close();

    await Promise.all([
      runWriter(databasePath, "first", 25),
      runWriter(databasePath, "second", 25),
    ]);

    const store = open(databasePath);
    expect(store.transactionCount()).toBe(50);
    expect(
      new Set(
        store.listTransactions().map((transaction) => transaction.description),
      ).size,
    ).toBe(50);
  });

  it("imports valid legacy JSON once and preserves the source", async () => {
    const legacyPath = join(directory, "store.json");
    const legacy = JSON.stringify(
      {
        transactions: [
          {
            id: "legacy-transaction",
            description: "Old coffee",
            amount: -1.005,
            category: "dining",
            createdAt: "2025-01-02T03:04:05.000Z",
          },
        ],
        budgets: [{ category: "dining", limit: 20.005 }],
      },
      null,
      2,
    );
    await writeFile(legacyPath, legacy);

    const first = open(legacyPath);
    expect(first.listTransactions()[0]?.amount).toBe(-1.01);
    expect(first.listBudgets()).toEqual([
      { category: "dining", limit: 20.01 },
    ]);
    first.close();

    const second = open(legacyPath);
    expect(second.transactionCount()).toBe(1);
    expect(await readFile(legacyPath, "utf8")).toBe(legacy);
  });

  it("rejects corrupt legacy JSON without changing it", async () => {
    const legacyPath = join(directory, "corrupt.json");
    const corrupt = '{"transactions": [';
    await writeFile(legacyPath, corrupt);

    expect(() => new Store(legacyPath)).toThrow(DataIntegrityError);
    expect(await readFile(legacyPath, "utf8")).toBe(corrupt);
  });

  it("normalizes old sign/category combinations and drops income budgets", async () => {
    const legacyPath = join(directory, "old-rules.json");
    const legacy = JSON.stringify({
      transactions: [
        {
          id: "positive-expense",
          description: "Restaurant refund",
          amount: 25,
          category: "dining",
          createdAt: "2025-01-02T03:04:05.000Z",
        },
        {
          id: "negative-income",
          description: "Whole Foods reversal",
          amount: -10,
          category: "income",
          createdAt: "2025-01-03T03:04:05.000Z",
        },
      ],
      budgets: [
        { category: "income", limit: 1_000 },
        { category: "dining", limit: 100 },
      ],
    });
    await writeFile(legacyPath, legacy);

    const store = open(legacyPath);
    const categories = Object.fromEntries(
      store
        .listTransactions()
        .map((transaction) => [transaction.id, transaction.category]),
    );
    expect(categories).toEqual({
      "positive-expense": "income",
      "negative-income": "groceries",
    });
    expect(store.listBudgets()).toEqual([
      { category: "dining", limit: 100 },
    ]);
    expect(await readFile(legacyPath, "utf8")).toBe(legacy);
  });

  it("still rejects malformed and non-finite legacy values", async () => {
    const cases = [
      {
        name: "unknown-category.json",
        contents: JSON.stringify({
          transactions: [
            {
              id: "unknown-category",
              description: "Mystery",
              amount: -10,
              category: "crypto",
              createdAt: "2025-01-02T03:04:05.000Z",
            },
          ],
          budgets: [],
        }),
        error: /category is invalid/,
      },
      {
        name: "overflow-amount.json",
        contents:
          '{"transactions":[{"id":"overflow","description":"Overflow","amount":1e400,"category":"income","createdAt":"2025-01-02T03:04:05.000Z"}],"budgets":[]}',
        error: /finite/,
      },
      {
        name: "overflow-income-budget.json",
        contents:
          '{"transactions":[],"budgets":[{"category":"income","limit":1e400}]}',
        error: /finite/,
      },
    ];

    for (const migrationCase of cases) {
      const legacyPath = join(directory, migrationCase.name);
      await writeFile(legacyPath, migrationCase.contents);
      expect(() => new Store(legacyPath)).toThrow(migrationCase.error);
      expect(await readFile(legacyPath, "utf8")).toBe(
        migrationCase.contents,
      );
    }
  });

  it("rejects an existing zero-byte data file without initializing it", async () => {
    const databasePath = join(directory, "empty.sqlite");
    await writeFile(databasePath, "");

    expect(() => new Store(databasePath)).toThrow(/empty.*not a valid SQLite/);
    expect((await readFile(databasePath)).byteLength).toBe(0);
  });

  it("rejects an incompatible SQLite schema without modifying it", () => {
    const databasePath = join(directory, "wrong-schema.sqlite");
    const database = new Database(databasePath);
    database.exec(`
      CREATE TABLE transactions (unexpected TEXT);
      PRAGMA user_version = 1;
    `);
    database.close();

    expect(() => new Store(databasePath)).toThrow(DataIntegrityError);

    const unchanged = new Database(databasePath, { readonly: true });
    try {
      const columns = unchanged
        .prepare<[], { name: string }>("PRAGMA table_info(transactions)")
        .all();
      expect(columns.map(({ name }) => name)).toEqual(["unexpected"]);
    } finally {
      unchanged.close();
    }
  });

  it("rejects non-finite values and inconsistent categories", () => {
    const store = open();
    expect(() =>
      store.addTransaction({
        description: "Overflow",
        amount: Number.POSITIVE_INFINITY,
      }),
    ).toThrow(/finite/);
    expect(() =>
      store.addTransaction({
        description: "Wrong positive category",
        amount: 10,
        category: "groceries",
      }),
    ).toThrow(/positive amounts/);
    expect(() =>
      store.addTransaction({
        description: "Wrong negative category",
        amount: -10,
        category: "income",
      }),
    ).toThrow(/negative amounts/);
    expect(() =>
      store.setBudget({
        category: "income",
        limit: 10,
      } as unknown as Budget),
    ).toThrow(/income/);
  });

  it("reports persistence health and detects a closed database", () => {
    const persistent = open(join(directory, "health.sqlite"));
    expect(persistent.health()).toEqual({
      status: "ok",
      storage: {
        status: "ok",
        type: "sqlite",
        persistent: true,
      },
    });

    const database = (
      persistent as unknown as { database: Database.Database }
    ).database;
    const quickCheck = vi
      .spyOn(database, "pragma")
      .mockReturnValue("database disk image is malformed");
    expect(persistent.health()).toMatchObject({
      status: "error",
      storage: {
        status: "error",
        message: expect.stringContaining("integrity check failed"),
      },
    });
    expect(quickCheck).toHaveBeenCalledWith("quick_check(1)", {
      simple: true,
    });
    quickCheck.mockRestore();

    persistent.close();
    expect(persistent.health()).toMatchObject({
      status: "error",
      storage: { status: "error", message: "database is closed" },
    });
  });

  it("seeds only when explicitly called and never reseeds after reset", () => {
    const databasePath = join(directory, "seed.sqlite");
    const first = open(databasePath);
    expect(first.transactionCount()).toBe(0);
    expect(first.seedDemo()).toBe(true);
    expect(first.seedDemo()).toBe(false);
    expect(first.transactionCount()).toBe(5);
    first.reset();
    first.close();

    const restarted = open(databasePath);
    expect(restarted.seedDemo()).toBe(false);
    expect(restarted.transactionCount()).toBe(0);
    expect(restarted.listBudgets()).toEqual([]);
  });
});

async function runWriter(
  databasePath: string,
  prefix: string,
  count: number,
): Promise<void> {
  await execFileAsync(
    process.execPath,
    ["--import", "tsx", writerPath, databasePath, prefix, String(count)],
    {
      cwd: projectRoot,
      timeout: 20_000,
    },
  );
}
