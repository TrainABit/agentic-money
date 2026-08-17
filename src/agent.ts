import type {
  Budget,
  Category,
  CategorySummary,
  Insight,
  SpendingCategory,
  Summary,
  Transaction,
} from "./types.js";
import { fromMinorUnits, toMinorUnits } from "./money.js";

/**
 * Keyword rules the agent uses to infer a category from a free-text
 * description. Ordering matters: the first category with a matching keyword
 * wins, so more specific categories are listed before generic ones.
 */
const CATEGORY_KEYWORDS: Array<[SpendingCategory, string[]]> = [
  ["groceries", ["grocery", "groceries", "supermarket", "whole foods", "trader joe", "aldi", "costco"]],
  ["dining", ["restaurant", "cafe", "coffee", "starbucks", "mcdonald", "pizza", "dining", "bar", "takeout", "uber eats", "doordash"]],
  ["transport", ["uber", "lyft", "taxi", "gas", "fuel", "shell", "chevron", "metro", "subway pass", "parking", "flight", "airline"]],
  ["housing", ["rent", "mortgage", "landlord", "hoa", "lease"]],
  ["utilities", ["electric", "water bill", "gas bill", "internet", "comcast", "verizon", "at&t", "phone bill", "utility"]],
  ["entertainment", ["netflix", "spotify", "hulu", "disney", "movie", "cinema", "concert", "game", "steam"]],
  ["health", ["pharmacy", "cvs", "walgreens", "doctor", "clinic", "gym", "dental", "hospital", "insurance"]],
  ["shopping", ["amazon", "target", "walmart", "clothing", "shoes", "apple store", "best buy", "shopping"]],
];

const CATEGORY_PATTERNS = CATEGORY_KEYWORDS.map(
  ([category, keywords]) =>
    [
      category,
      keywords.map(
        (keyword) =>
          new RegExp(
            `(?:^|[^\\p{L}\\p{N}])${escapeRegExp(keyword)}(?:$|[^\\p{L}\\p{N}])`,
            "iu",
          ),
      ),
    ] as const,
);

/**
 * Infer a spending/income category from a transaction's description and amount.
 * The amount sign takes precedence: every positive amount is income, including
 * refunds, while negative amounts can only be assigned spending categories.
 */
export function categorize(description: string, amount: number): Category {
  if (!Number.isFinite(amount)) {
    throw new RangeError("amount must be a finite number");
  }
  if (amount > 0) return "income";

  for (const [category, patterns] of CATEGORY_PATTERNS) {
    if (patterns.some((pattern) => pattern.test(description))) {
      return category;
    }
  }

  return "other";
}

/**
 * Build a spending summary and a set of human-readable insights from the full
 * transaction history and the configured budgets. This is the "agentic" core:
 * it turns raw records into advice a user can act on.
 */
export function summarize(
  transactions: Transaction[],
  budgets: Budget[],
): Summary {
  const incomeMinor = transactions
    .filter((transaction) => transaction.amount > 0)
    .reduce(
      (sum, transaction) =>
        sum + toMinorUnits(transaction.amount, "transaction amount"),
      0,
    );

  const spendingMinor = transactions
    .filter((transaction) => transaction.amount < 0)
    .reduce(
      (sum, transaction) =>
        sum + Math.abs(toMinorUnits(transaction.amount, "transaction amount")),
      0,
    );

  const spentByCategory = new Map<Category, number>();
  for (const transaction of transactions) {
    if (transaction.amount < 0) {
      spentByCategory.set(
        transaction.category,
        (spentByCategory.get(transaction.category) ?? 0) +
          Math.abs(toMinorUnits(transaction.amount, "transaction amount")),
      );
    }
  }

  const budgetByCategory = new Map<Category, number>();
  for (const budget of budgets) {
    budgetByCategory.set(
      budget.category,
      toMinorUnits(budget.limit, "budget limit"),
    );
  }

  const categoryNames = new Set<Category>([
    ...spentByCategory.keys(),
    ...budgetByCategory.keys(),
  ]);

  const categories: CategorySummary[] = [...categoryNames]
    .map((category) => {
      const spentMinor = spentByCategory.get(category) ?? 0;
      const limitMinor = budgetByCategory.has(category)
        ? budgetByCategory.get(category)!
        : null;
      const spent = fromMinorUnits(spentMinor);
      const limit = limitMinor === null ? null : fromMinorUnits(limitMinor);
      const remaining =
        limitMinor === null ? null : fromMinorUnits(limitMinor - spentMinor);
      const utilization =
        limitMinor !== null && limitMinor > 0
          ? round(spentMinor / limitMinor)
          : null;
      return { category, spent, limit, remaining, utilization };
    })
    .sort((a, b) => b.spent - a.spent);

  const income = fromMinorUnits(incomeMinor);
  const spending = fromMinorUnits(spendingMinor);
  return {
    income,
    spending,
    net: fromMinorUnits(incomeMinor - spendingMinor),
    categories,
    insights: buildInsights(income, spending, categories),
  };
}

function buildInsights(
  income: number,
  spending: number,
  categories: CategorySummary[],
): Insight[] {
  const insights: Insight[] = [];
  const net = round(income - spending);

  if (spending === 0 && income === 0) {
    insights.push({
      level: "info",
      message: "No activity yet. Add a transaction to get personalized advice.",
    });
    return insights;
  }

  if (net >= 0) {
    insights.push({
      level: "success",
      message: `You're net positive by ${formatMoney(net)} this period. Consider moving the surplus into savings.`,
    });
  } else {
    insights.push({
      level: "danger",
      message: `You're spending ${formatMoney(-net)} more than you earn this period. Time to trim a category below.`,
    });
  }

  for (const c of categories) {
    if (c.limit === 0 && c.spent > 0) {
      insights.push({
        level: "danger",
        message: `${label(c.category)} is over budget: ${formatMoney(c.spent)} spent of a ${formatMoney(c.limit)} limit.`,
      });
      continue;
    }
    if (c.utilization === null || c.limit === null) continue;
    if (c.utilization >= 1) {
      insights.push({
        level: "danger",
        message: `${label(c.category)} is over budget: ${formatMoney(c.spent)} spent of a ${formatMoney(c.limit)} limit.`,
      });
    } else if (c.utilization >= 0.8) {
      insights.push({
        level: "warning",
        message: `${label(c.category)} is at ${Math.round(c.utilization * 100)}% of its budget. ${formatMoney(c.remaining ?? 0)} left.`,
      });
    }
  }

  const topSpend = categories.find((c) => c.spent > 0);
  if (topSpend && spending > 0) {
    const share = Math.round((topSpend.spent / spending) * 100);
    insights.push({
      level: "info",
      message: `${label(topSpend.category)} is your biggest expense at ${share}% of total spending.`,
    });
  }

  return insights;
}

function label(category: Category): string {
  return category.charAt(0).toUpperCase() + category.slice(1);
}

function formatMoney(value: number): string {
  return `$${Math.abs(value).toFixed(2)}`;
}

function round(value: number): number {
  return Math.round(value * 100) / 100;
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
