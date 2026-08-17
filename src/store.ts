import { randomUUID } from "node:crypto";
import {
  closeSync,
  existsSync,
  mkdirSync,
  openSync,
  readFileSync,
  readSync,
} from "node:fs";
import { dirname } from "node:path";
import Database from "better-sqlite3";
import { categorize, summarize } from "./agent.js";
import { fromMinorUnits, normalizeMoney, toMinorUnits } from "./money.js";
import {
  isCategory,
  isSpendingCategory,
  type Budget,
  type Category,
  type NewTransactionInput,
  type Page,
  type Summary,
  type Transaction,
} from "./types.js";

interface PersistedState {
  transactions: Transaction[];
  budgets: Budget[];
}

interface TransactionRow {
  id: string;
  description: string;
  amount_minor: number;
  category: Category;
  created_at: string;
}

interface BudgetRow {
  category: Budget["category"];
  limit_minor: number;
}

interface AggregateRow {
  category: Category;
  amount_minor: number;
}

interface TableInfoRow {
  name: string;
  type: string;
  pk: number;
}

export interface StoreOptions {
  legacyJsonPath?: string;
}

export interface TransactionPageOptions {
  limit?: number;
  offset?: number;
}

export interface StoreHealth {
  status: "ok" | "error";
  storage: {
    status: "ok" | "error";
    type: "sqlite";
    persistent: boolean;
    message?: string;
  };
}

export class DataIntegrityError extends Error {
  override readonly name = "DataIntegrityError";
}

const SCHEMA_VERSION = 1;
const SQLITE_HEADER = "SQLite format 3\u0000";
const DEFAULT_PAGE_SIZE = 50;
const MAX_PAGE_SIZE = 100;
const MAX_DESCRIPTION_LENGTH = 200;

/**
 * Transactional SQLite store. Values are persisted as integer minor units,
 * writes use atomic transactions, and WAL mode coordinates multiple processes.
 * Omitting `filePath` creates an isolated in-memory database for tests.
 */
export class Store {
  private readonly database: Database.Database;
  private readonly persistent: boolean;

  constructor(filePath?: string, options: StoreOptions = {}) {
    this.persistent = filePath !== undefined && filePath !== ":memory:";

    let databasePath = filePath ?? ":memory:";
    let legacyJsonPath = options.legacyJsonPath;
    if (filePath !== undefined && existsSync(filePath)) {
      const fileKind = inspectDataFile(filePath);
      if (fileKind === "empty") {
        throw new DataIntegrityError(
          `data file at ${filePath} is empty and is not a valid SQLite database; the file was left untouched`,
        );
      }
      if (fileKind === "other") {
        // A caller using the old Store(JSON_PATH) API gets a companion database;
        // the source JSON is intentionally retained as an untouched backup.
        legacyJsonPath = filePath;
        databasePath = `${filePath}.sqlite`;
      }
    }

    if (databasePath !== ":memory:") {
      mkdirSync(dirname(databasePath), { recursive: true });
    }

    this.database = new Database(databasePath, { timeout: 5_000 });
    try {
      this.configure();
      this.migrate();
      this.validateSchema();
      this.assertIntegrity();
      if (legacyJsonPath !== undefined && existsSync(legacyJsonPath)) {
        this.importLegacyJson(legacyJsonPath);
      }
    } catch (error) {
      this.database.close();
      throw error;
    }
  }

  listTransactions(): Transaction[] {
    const rows = this.database
      .prepare<[], TransactionRow>(
        `SELECT id, description, amount_minor, category, created_at
         FROM transactions
         ORDER BY created_at DESC, id DESC`,
      )
      .all();
    return rows.map(toTransaction);
  }

  getTransactionsPage(
    options: TransactionPageOptions = {},
  ): Page<Transaction> {
    const limit = validatePageInteger(
      options.limit ?? DEFAULT_PAGE_SIZE,
      "limit",
      1,
      MAX_PAGE_SIZE,
    );
    const offset = validatePageInteger(
      options.offset ?? 0,
      "offset",
      0,
      Number.MAX_SAFE_INTEGER,
    );
    const rows = this.database
      .prepare<[number, number], TransactionRow>(
        `SELECT id, description, amount_minor, category, created_at
         FROM transactions
         ORDER BY created_at DESC, id DESC
         LIMIT ? OFFSET ?`,
      )
      .all(limit, offset);
    const total = this.transactionCount();

    return {
      data: rows.map(toTransaction),
      pagination: {
        limit,
        offset,
        total,
        hasMore: offset + rows.length < total,
      },
    };
  }

  listBudgets(): Budget[] {
    return this.database
      .prepare<[], BudgetRow>(
        `SELECT category, limit_minor
         FROM budgets
         ORDER BY category`,
      )
      .all()
      .map(toBudget);
  }

  addTransaction(input: NewTransactionInput): Transaction {
    const description = validateDescription(input.description);
    const amountMinor = toMinorUnits(input.amount, "amount");
    if (amountMinor === 0) {
      throw new RangeError("amount must be at least one cent and non-zero");
    }

    const category = input.category ?? categorize(description, amountMinor);
    validateTransactionCategory(category, amountMinor);

    const transaction: Transaction = {
      id: randomUUID(),
      description,
      amount: fromMinorUnits(amountMinor),
      category,
      createdAt: new Date().toISOString(),
    };

    this.database
      .transaction(() => {
        this.insertTransaction(transaction);
      })
      .immediate();
    return transaction;
  }

  setBudget(budget: Budget): Budget {
    if (!isSpendingCategory(budget.category)) {
      throw new RangeError("budgets cannot be set for income");
    }
    const limitMinor = toMinorUnits(budget.limit, "limit");
    if (limitMinor < 0) {
      throw new RangeError("limit must be non-negative");
    }

    this.database
      .transaction(() => {
        this.database
          .prepare<[Budget["category"], number]>(
            `INSERT INTO budgets (category, limit_minor)
             VALUES (?, ?)
             ON CONFLICT(category)
             DO UPDATE SET limit_minor = excluded.limit_minor`,
          )
          .run(budget.category, limitMinor);
      })
      .immediate();

    return { category: budget.category, limit: fromMinorUnits(limitMinor) };
  }

  reset(): void {
    this.database
      .transaction(() => {
        this.database.prepare("DELETE FROM transactions").run();
        this.database.prepare("DELETE FROM budgets").run();
      })
      .immediate();
  }

  seedDemo(): boolean {
    let seeded = false;
    this.database
      .transaction(() => {
        if (this.hasMetadata("demo_seed_v1")) return;

        const timestamp = new Date().toISOString();
        const budgets: Budget[] = [
          { category: "groceries", limit: 400 },
          { category: "dining", limit: 200 },
          { category: "transport", limit: 150 },
        ];
        const transactions: Array<
          NewTransactionInput & { id: string; createdAt: string }
        > = [
          {
            id: "demo-monthly-paycheck",
            description: "Monthly paycheck",
            amount: 3_200,
            createdAt: timestamp,
          },
          {
            id: "demo-whole-foods",
            description: "Whole Foods groceries",
            amount: -180.42,
            createdAt: timestamp,
          },
          {
            id: "demo-starbucks",
            description: "Starbucks coffee",
            amount: -6.75,
            createdAt: timestamp,
          },
          {
            id: "demo-uber",
            description: "Uber ride downtown",
            amount: -23.1,
            createdAt: timestamp,
          },
          {
            id: "demo-netflix",
            description: "Netflix subscription",
            amount: -15.49,
            createdAt: timestamp,
          },
        ];

        const insertBudget = this.database.prepare<
          [Budget["category"], number]
        >(
          `INSERT INTO budgets (category, limit_minor)
           VALUES (?, ?)
           ON CONFLICT(category) DO NOTHING`,
        );
        for (const budget of budgets) {
          insertBudget.run(
            budget.category,
            toMinorUnits(budget.limit, "budget limit"),
          );
        }

        for (const input of transactions) {
          const amountMinor = toMinorUnits(input.amount, "amount");
          const category = input.category ?? categorize(input.description, amountMinor);
          this.insertTransaction(
            {
              id: input.id,
              description: input.description,
              amount: fromMinorUnits(amountMinor),
              category,
              createdAt: input.createdAt,
            },
            true,
          );
        }

        this.setMetadata("demo_seed_v1", timestamp);
        seeded = true;
      })
      .immediate();
    return seeded;
  }

  getSummary(): Summary {
    const transactions = this.database
      .prepare<[], AggregateRow>(
        `SELECT category, SUM(amount_minor) AS amount_minor
         FROM transactions
         GROUP BY category`,
      )
      .all()
      .map(
        (row): Transaction => ({
          id: `aggregate-${row.category}`,
          description: row.category,
          amount: fromMinorUnits(row.amount_minor),
          category: row.category,
          createdAt: "",
        }),
      );
    return summarize(transactions, this.listBudgets());
  }

  transactionCount(): number {
    const row = this.database
      .prepare<[], { count: number }>(
        "SELECT COUNT(*) AS count FROM transactions",
      )
      .get();
    return row?.count ?? 0;
  }

  health(): StoreHealth {
    if (!this.database.open) {
      return {
        status: "error",
        storage: {
          status: "error",
          type: "sqlite",
          persistent: this.persistent,
          message: "database is closed",
        },
      };
    }

    try {
      this.assertIntegrity();
      return {
        status: "ok",
        storage: {
          status: "ok",
          type: "sqlite",
          persistent: this.persistent,
        },
      };
    } catch (error) {
      return {
        status: "error",
        storage: {
          status: "error",
          type: "sqlite",
          persistent: this.persistent,
          message: error instanceof Error ? error.message : "storage check failed",
        },
      };
    }
  }

  close(): void {
    if (this.database.open) this.database.close();
  }

  private configure(): void {
    this.database.pragma("foreign_keys = ON");
    this.database.pragma("busy_timeout = 5000");
    if (this.persistent) {
      this.database.pragma("journal_mode = WAL");
      this.database.pragma("synchronous = NORMAL");
    }
  }

  private migrate(): void {
    const version = Number(
      this.database.pragma("user_version", { simple: true }),
    );
    if (!Number.isInteger(version) || version < 0) {
      throw new DataIntegrityError("database has an invalid schema version");
    }
    if (version > SCHEMA_VERSION) {
      throw new DataIntegrityError(
        `database schema ${version} is newer than supported schema ${SCHEMA_VERSION}`,
      );
    }

    if (version === 0) {
      this.database
        .transaction(() => {
          this.database.exec(`
            CREATE TABLE IF NOT EXISTS transactions (
              id TEXT PRIMARY KEY,
              description TEXT NOT NULL
                CHECK(length(trim(description)) BETWEEN 1 AND ${MAX_DESCRIPTION_LENGTH}),
              amount_minor INTEGER NOT NULL
                CHECK(
                  amount_minor != 0
                  AND (
                    (amount_minor > 0 AND category = 'income')
                    OR (amount_minor < 0 AND category != 'income')
                  )
                ),
              category TEXT NOT NULL
                CHECK(category IN (
                  'income', 'groceries', 'dining', 'transport', 'housing',
                  'utilities', 'entertainment', 'health', 'shopping', 'other'
                )),
              created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS transactions_created_at_idx
              ON transactions(created_at DESC, id DESC);

            CREATE TABLE IF NOT EXISTS budgets (
              category TEXT PRIMARY KEY
                CHECK(category IN (
                  'groceries', 'dining', 'transport', 'housing', 'utilities',
                  'entertainment', 'health', 'shopping', 'other'
                )),
              limit_minor INTEGER NOT NULL CHECK(limit_minor >= 0)
            );

            CREATE TABLE IF NOT EXISTS metadata (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );
          `);
          this.database.pragma(`user_version = ${SCHEMA_VERSION}`);
        })
        .immediate();
    }
  }

  private assertIntegrity(): void {
    const result = this.database.pragma("quick_check(1)", { simple: true });
    if (result !== "ok") {
      throw new DataIntegrityError(
        `SQLite integrity check failed: ${String(result)}`,
      );
    }
  }

  private validateSchema(): void {
    const expectedTables: Record<string, Array<[string, string, number]>> = {
      transactions: [
        ["id", "TEXT", 1],
        ["description", "TEXT", 0],
        ["amount_minor", "INTEGER", 0],
        ["category", "TEXT", 0],
        ["created_at", "TEXT", 0],
      ],
      budgets: [
        ["category", "TEXT", 1],
        ["limit_minor", "INTEGER", 0],
      ],
      metadata: [
        ["key", "TEXT", 1],
        ["value", "TEXT", 0],
      ],
    };

    for (const [table, expected] of Object.entries(expectedTables)) {
      const actual = this.database
        .prepare<[], TableInfoRow>(`PRAGMA table_info(${table})`)
        .all()
        .map(
          ({ name, type, pk }): [string, string, number] => [
            name,
            type.toUpperCase(),
            pk,
          ],
        );
      if (JSON.stringify(actual) !== JSON.stringify(expected)) {
        throw new DataIntegrityError(
          `database table ${table} does not match schema version ${SCHEMA_VERSION}`,
        );
      }
    }
  }

  private insertTransaction(
    transaction: Transaction,
    ignoreConflict = false,
  ): void {
    const conflict = ignoreConflict ? "OR IGNORE" : "";
    this.database
      .prepare<[string, string, number, Category, string]>(
        `INSERT ${conflict} INTO transactions
           (id, description, amount_minor, category, created_at)
         VALUES (?, ?, ?, ?, ?)`,
      )
      .run(
        transaction.id,
        transaction.description,
        toMinorUnits(transaction.amount, "amount"),
        transaction.category,
        transaction.createdAt,
      );
  }

  private importLegacyJson(filePath: string): void {
    const importKey = "legacy_json_import_v1";
    if (this.hasMetadata(importKey)) return;

    const state = readAndValidateLegacyState(filePath);
    this.database
      .transaction(() => {
        if (this.hasMetadata(importKey)) return;
        const existing =
          this.transactionCount() +
          (this.database
            .prepare<[], { count: number }>(
              "SELECT COUNT(*) AS count FROM budgets",
            )
            .get()?.count ?? 0);
        if (existing > 0) {
          throw new DataIntegrityError(
            `cannot safely import legacy data from ${filePath}: target database is not empty`,
          );
        }

        for (const transaction of state.transactions) {
          this.insertTransaction(transaction);
        }
        const insertBudget = this.database.prepare<
          [Budget["category"], number]
        >(
          "INSERT INTO budgets (category, limit_minor) VALUES (?, ?)",
        );
        for (const budget of state.budgets) {
          insertBudget.run(
            budget.category,
            toMinorUnits(budget.limit, "budget limit"),
          );
        }
        this.setMetadata(importKey, new Date().toISOString());
      })
      .immediate();
  }

  private hasMetadata(key: string): boolean {
    return (
      this.database
        .prepare<[string], { value: string }>(
          "SELECT value FROM metadata WHERE key = ?",
        )
        .get(key) !== undefined
    );
  }

  private setMetadata(key: string, value: string): void {
    this.database
      .prepare<[string, string]>(
        `INSERT INTO metadata (key, value)
         VALUES (?, ?)
         ON CONFLICT(key) DO UPDATE SET value = excluded.value`,
      )
      .run(key, value);
  }
}

function toTransaction(row: TransactionRow): Transaction {
  return {
    id: row.id,
    description: row.description,
    amount: fromMinorUnits(row.amount_minor),
    category: row.category,
    createdAt: row.created_at,
  };
}

function toBudget(row: BudgetRow): Budget {
  return {
    category: row.category,
    limit: fromMinorUnits(row.limit_minor),
  };
}

function validateDescription(value: unknown): string {
  if (typeof value !== "string" || value.trim().length === 0) {
    throw new TypeError("description is required");
  }
  const description = value.trim();
  if (description.length > MAX_DESCRIPTION_LENGTH) {
    throw new RangeError(
      `description must be ${MAX_DESCRIPTION_LENGTH} characters or fewer`,
    );
  }
  return description;
}

function validateTransactionCategory(
  category: unknown,
  amountMinor: number,
): asserts category is Category {
  if (!isCategory(category)) {
    throw new RangeError("invalid category");
  }
  if (amountMinor > 0 && category !== "income") {
    throw new RangeError("positive amounts must use the income category");
  }
  if (amountMinor < 0 && category === "income") {
    throw new RangeError("negative amounts cannot use the income category");
  }
}

function validatePageInteger(
  value: number,
  field: string,
  minimum: number,
  maximum: number,
): number {
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) {
    throw new RangeError(
      `${field} must be an integer between ${minimum} and ${maximum}`,
    );
  }
  return value;
}

function inspectDataFile(filePath: string): "sqlite" | "empty" | "other" {
  const descriptor = openSync(filePath, "r");
  try {
    const buffer = Buffer.alloc(SQLITE_HEADER.length);
    const bytesRead = readSync(
      descriptor,
      buffer,
      0,
      buffer.length,
      0,
    );
    if (bytesRead === 0) return "empty";
    if (
      bytesRead === SQLITE_HEADER.length &&
      buffer.toString("utf8") === SQLITE_HEADER
    ) {
      return "sqlite";
    }
    return "other";
  } finally {
    closeSync(descriptor);
  }
}

function readAndValidateLegacyState(filePath: string): PersistedState {
  let parsed: unknown;
  try {
    parsed = JSON.parse(readFileSync(filePath, "utf8")) as unknown;
  } catch (error) {
    throw new DataIntegrityError(
      `legacy data at ${filePath} is not valid JSON; the source file was left untouched`,
      { cause: error },
    );
  }

  if (!isRecord(parsed)) {
    throw legacyValidationError(filePath, "root must be an object");
  }
  if (!Array.isArray(parsed.transactions) || !Array.isArray(parsed.budgets)) {
    throw legacyValidationError(
      filePath,
      "transactions and budgets must be arrays",
    );
  }

  const ids = new Set<string>();
  const transactions = parsed.transactions.map((entry, index): Transaction => {
    if (!isRecord(entry)) {
      throw legacyValidationError(
        filePath,
        `transactions[${index}] must be an object`,
      );
    }
    if (typeof entry.id !== "string" || entry.id.length === 0) {
      throw legacyValidationError(
        filePath,
        `transactions[${index}].id must be a non-empty string`,
      );
    }
    if (ids.has(entry.id)) {
      throw legacyValidationError(filePath, `duplicate transaction id ${entry.id}`);
    }
    ids.add(entry.id);

    const description = validateLegacyValue(
      filePath,
      `transactions[${index}].description`,
      () => validateDescription(entry.description),
    );
    if (typeof entry.amount !== "number") {
      throw legacyValidationError(
        filePath,
        `transactions[${index}].amount must be a number`,
      );
    }
    const amount = validateLegacyValue(
      filePath,
      `transactions[${index}].amount`,
      () => normalizeMoney(entry.amount as number, "amount"),
    );
    const amountMinor = toMinorUnits(amount, "amount");
    if (amountMinor === 0) {
      throw legacyValidationError(
        filePath,
        `transactions[${index}].amount must be non-zero after cent normalization`,
      );
    }
    if (!isCategory(entry.category)) {
      throw legacyValidationError(
        filePath,
        `transactions[${index}].category is invalid`,
      );
    }
    const category = isCategoryConsistentWithSign(entry.category, amountMinor)
      ? entry.category
      : categorize(description, amountMinor);
    validateTransactionCategory(category, amountMinor);
    if (
      typeof entry.createdAt !== "string" ||
      !Number.isFinite(Date.parse(entry.createdAt))
    ) {
      throw legacyValidationError(
        filePath,
        `transactions[${index}].createdAt must be a valid date`,
      );
    }

    return {
      id: entry.id,
      description,
      amount,
      category,
      createdAt: entry.createdAt,
    };
  });

  const categories = new Set<Category>();
  const budgets = parsed.budgets
    .map((entry, index): Budget | null => {
      if (!isRecord(entry) || !isCategory(entry.category)) {
        throw legacyValidationError(
          filePath,
          `budgets[${index}].category is invalid`,
        );
      }
      if (categories.has(entry.category)) {
        throw legacyValidationError(
          filePath,
          `duplicate budget category ${entry.category}`,
        );
      }
      categories.add(entry.category);
      if (typeof entry.limit !== "number") {
        throw legacyValidationError(
          filePath,
          `budgets[${index}].limit must be a number`,
        );
      }
      const limit = validateLegacyValue(
        filePath,
        `budgets[${index}].limit`,
        () => normalizeMoney(entry.limit as number, "limit"),
      );
      if (limit < 0) {
        throw legacyValidationError(
          filePath,
          `budgets[${index}].limit must be non-negative`,
        );
      }

      // Income budgets were accepted by the JSON store but have no meaning
      // under the sign-consistent model. Validate them fully, then omit them.
      if (entry.category === "income") return null;
      return { category: entry.category, limit };
    })
    .filter((budget): budget is Budget => budget !== null);

  return { transactions, budgets };
}

function isCategoryConsistentWithSign(
  category: Category,
  amountMinor: number,
): boolean {
  return (
    (amountMinor > 0 && category === "income") ||
    (amountMinor < 0 && category !== "income")
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function validateLegacyValue<T>(
  filePath: string,
  field: string,
  validate: () => T,
): T {
  try {
    return validate();
  } catch (error) {
    throw legacyValidationError(
      filePath,
      `${field}: ${error instanceof Error ? error.message : "invalid value"}`,
    );
  }
}

function legacyValidationError(
  filePath: string,
  message: string,
): DataIntegrityError {
  return new DataIntegrityError(
    `legacy data at ${filePath} is invalid (${message}); the source file was left untouched`,
  );
}
