export type Category =
  | "income"
  | "groceries"
  | "dining"
  | "transport"
  | "housing"
  | "utilities"
  | "entertainment"
  | "health"
  | "shopping"
  | "other";

export interface Transaction {
  id: string;
  description: string;
  /** Positive for income, negative for spending. */
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
  category: Category;
  limit: number;
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
