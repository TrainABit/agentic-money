import { describe, expect, it } from "vitest";
import { categorize, summarize } from "../src/agent.js";
import type { Budget, Transaction } from "../src/types.js";

function txn(
  description: string,
  amount: number,
  category: Transaction["category"],
): Transaction {
  return {
    id: Math.random().toString(36).slice(2),
    description,
    amount,
    category,
    createdAt: new Date().toISOString(),
  };
}

describe("categorize", () => {
  it("detects income from keywords", () => {
    expect(categorize("Monthly paycheck", 3000)).toBe("income");
  });

  it("detects groceries", () => {
    expect(categorize("Whole Foods groceries", -80)).toBe("groceries");
  });

  it("detects dining", () => {
    expect(categorize("Starbucks coffee", -6)).toBe("dining");
  });

  it("detects transport", () => {
    expect(categorize("Uber ride downtown", -20)).toBe("transport");
  });

  it("falls back to income for unknown positive amounts", () => {
    expect(categorize("Mystery credit", 50)).toBe("income");
  });

  it("falls back to other for unknown spending", () => {
    expect(categorize("Random purchase", -20)).toBe("other");
  });
});

describe("summarize", () => {
  it("computes income, spending and net", () => {
    const transactions = [
      txn("Paycheck", 2000, "income"),
      txn("Groceries", -300, "groceries"),
      txn("Dining", -100, "dining"),
    ];
    const summary = summarize(transactions, []);
    expect(summary.income).toBe(2000);
    expect(summary.spending).toBe(400);
    expect(summary.net).toBe(1600);
  });

  it("flags a category that is over budget", () => {
    const transactions = [txn("Groceries", -450, "groceries")];
    const budgets: Budget[] = [{ category: "groceries", limit: 400 }];
    const summary = summarize(transactions, budgets);

    const groceries = summary.categories.find((c) => c.category === "groceries");
    expect(groceries?.remaining).toBe(-50);
    expect(
      summary.insights.some(
        (i) => i.level === "danger" && i.message.includes("over budget"),
      ),
    ).toBe(true);
  });

  it("warns when a category nears its budget", () => {
    const transactions = [txn("Dining", -170, "dining")];
    const budgets: Budget[] = [{ category: "dining", limit: 200 }];
    const summary = summarize(transactions, budgets);

    expect(
      summary.insights.some((i) => i.level === "warning"),
    ).toBe(true);
  });

  it("returns an informative insight when there is no activity", () => {
    const summary = summarize([], []);
    expect(summary.insights).toHaveLength(1);
    expect(summary.insights[0].level).toBe("info");
  });
});
