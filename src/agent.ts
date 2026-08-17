import type {
  Budget,
  Category,
  CategorySummary,
  Insight,
  Summary,
  Transaction,
} from "./types.js";

/**
 * Keyword rules the agent uses to infer a category from a free-text
 * description. Ordering matters: the first category with a matching keyword
 * wins, so more specific categories are listed before generic ones.
 */
const CATEGORY_KEYWORDS: Array<[Category, string[]]> = [
  ["income", ["salary", "paycheck", "payroll", "deposit", "refund", "dividend", "invoice paid"]],
  ["groceries", ["grocery", "groceries", "supermarket", "whole foods", "trader joe", "aldi", "costco"]],
  ["dining", ["restaurant", "cafe", "coffee", "starbucks", "mcdonald", "pizza", "dining", "bar", "takeout", "uber eats", "doordash"]],
  ["transport", ["uber", "lyft", "taxi", "gas", "fuel", "shell", "chevron", "metro", "subway pass", "parking", "flight", "airline"]],
  ["housing", ["rent", "mortgage", "landlord", "hoa", "lease"]],
  ["utilities", ["electric", "water bill", "gas bill", "internet", "comcast", "verizon", "at&t", "phone bill", "utility"]],
  ["entertainment", ["netflix", "spotify", "hulu", "disney", "movie", "cinema", "concert", "game", "steam"]],
  ["health", ["pharmacy", "cvs", "walgreens", "doctor", "clinic", "gym", "dental", "hospital", "insurance"]],
  ["shopping", ["amazon", "target", "walmart", "clothing", "shoes", "apple store", "best buy", "shopping"]],
];

/**
 * Infer a spending/income category from a transaction's description and amount.
 * A positive amount with no clear expense keyword is treated as income.
 */
export function categorize(description: string, amount: number): Category {
  const text = description.toLowerCase();

  for (const [category, keywords] of CATEGORY_KEYWORDS) {
    if (keywords.some((keyword) => text.includes(keyword))) {
      return category;
    }
  }

  if (amount > 0) {
    return "income";
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
  const income = transactions
    .filter((t) => t.amount > 0)
    .reduce((sum, t) => sum + t.amount, 0);

  const spending = transactions
    .filter((t) => t.amount < 0)
    .reduce((sum, t) => sum + Math.abs(t.amount), 0);

  const spentByCategory = new Map<Category, number>();
  for (const t of transactions) {
    if (t.amount < 0) {
      spentByCategory.set(
        t.category,
        (spentByCategory.get(t.category) ?? 0) + Math.abs(t.amount),
      );
    }
  }

  const budgetByCategory = new Map<Category, number>();
  for (const b of budgets) {
    budgetByCategory.set(b.category, b.limit);
  }

  const categoryNames = new Set<Category>([
    ...spentByCategory.keys(),
    ...budgetByCategory.keys(),
  ]);

  const categories: CategorySummary[] = [...categoryNames]
    .map((category) => {
      const spent = round(spentByCategory.get(category) ?? 0);
      const limit = budgetByCategory.has(category)
        ? budgetByCategory.get(category)!
        : null;
      const remaining = limit === null ? null : round(limit - spent);
      const utilization = limit && limit > 0 ? round(spent / limit) : null;
      return { category, spent, limit, remaining, utilization };
    })
    .sort((a, b) => b.spent - a.spent);

  return {
    income: round(income),
    spending: round(spending),
    net: round(income - spending),
    categories,
    insights: buildInsights(round(income), round(spending), categories),
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
