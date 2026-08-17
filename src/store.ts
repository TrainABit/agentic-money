import { randomUUID } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";
import { categorize } from "./agent.js";
import type { Budget, NewTransactionInput, Transaction } from "./types.js";

interface PersistedState {
  transactions: Transaction[];
  budgets: Budget[];
}

/**
 * A tiny JSON-file backed store. It keeps the whole state in memory and writes
 * through to disk on every mutation so a restarted server keeps its data.
 * When `filePath` is undefined the store stays purely in memory (used by tests).
 */
export class Store {
  private state: PersistedState = { transactions: [], budgets: [] };

  constructor(private readonly filePath?: string) {
    if (filePath && existsSync(filePath)) {
      try {
        this.state = JSON.parse(readFileSync(filePath, "utf8")) as PersistedState;
      } catch {
        this.state = { transactions: [], budgets: [] };
      }
    }
  }

  listTransactions(): Transaction[] {
    return [...this.state.transactions].sort((a, b) =>
      b.createdAt.localeCompare(a.createdAt),
    );
  }

  listBudgets(): Budget[] {
    return [...this.state.budgets];
  }

  addTransaction(input: NewTransactionInput): Transaction {
    const category = input.category ?? categorize(input.description, input.amount);
    const transaction: Transaction = {
      id: randomUUID(),
      description: input.description.trim(),
      amount: input.amount,
      category,
      createdAt: new Date().toISOString(),
    };
    this.state.transactions.push(transaction);
    this.persist();
    return transaction;
  }

  setBudget(budget: Budget): Budget {
    const existing = this.state.budgets.find((b) => b.category === budget.category);
    if (existing) {
      existing.limit = budget.limit;
    } else {
      this.state.budgets.push(budget);
    }
    this.persist();
    return budget;
  }

  reset(): void {
    this.state = { transactions: [], budgets: [] };
    this.persist();
  }

  private persist(): void {
    if (!this.filePath) return;
    mkdirSync(dirname(this.filePath), { recursive: true });
    writeFileSync(this.filePath, JSON.stringify(this.state, null, 2), "utf8");
  }
}
