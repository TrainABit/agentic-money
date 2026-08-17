export const CATEGORIES = [
  "income",
  "groceries",
  "dining",
  "transport",
  "housing",
  "utilities",
  "entertainment",
  "health",
  "shopping",
  "other",
] as const;

export type Category = (typeof CATEGORIES)[number];
export type SpendingCategory = Exclude<Category, "income">;

export const SPENDING_CATEGORIES = CATEGORIES.filter(
  (category): category is SpendingCategory => category !== "income",
);

export function isCategory(value: unknown): value is Category {
  return typeof value === "string" && CATEGORIES.includes(value as Category);
}

export function isSpendingCategory(value: unknown): value is SpendingCategory {
  return (
    typeof value === "string" &&
    SPENDING_CATEGORIES.includes(value as SpendingCategory)
  );
}

export interface Transaction {
  id: string;
  description: string;
  /** Major currency units, normalized to two decimals. Positive means income. */
  amount: number;
  category: Category;
  createdAt: string;
}

export interface NewTransactionInput {
  description: string;
  amount: number;
  /** Optional explicit category; when omitted the agent infers one. */
  category?: Category;
}

export interface Budget {
  category: SpendingCategory;
  /** Major currency units, normalized to two decimals. */
  limit: number;
}

export interface Page<T> {
  data: T[];
  pagination: {
    limit: number;
    offset: number;
    total: number;
    hasMore: boolean;
  };
}

export interface CategorySummary {
  category: Category;
  spent: number;
  limit: number | null;
  remaining: number | null;
  /** Fraction of the budget used (0..1+), null when no budget is set. */
  utilization: number | null;
}

export interface Insight {
  level: "info" | "warning" | "danger" | "success";
  message: string;
}

export interface Summary {
  income: number;
  spending: number;
  net: number;
  categories: CategorySummary[];
  insights: Insight[];
}
