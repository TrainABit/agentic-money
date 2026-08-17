import express, { type Express, type Request, type Response } from "express";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { summarize } from "./agent.js";
import { Store } from "./store.js";
import type { Category, NewTransactionInput } from "./types.js";

const VALID_CATEGORIES: Category[] = [
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
];

const __dirname = dirname(fileURLToPath(import.meta.url));
const PUBLIC_DIR = join(__dirname, "..", "public");

export function createApp(store: Store): Express {
  const app = express();
  app.use(express.json());

  app.get("/api/health", (_req: Request, res: Response) => {
    res.json({ status: "ok" });
  });

  app.get("/api/transactions", (_req: Request, res: Response) => {
    res.json(store.listTransactions());
  });

  app.post("/api/transactions", (req: Request, res: Response) => {
    const { description, amount, category } = req.body ?? {};

    if (typeof description !== "string" || description.trim().length === 0) {
      return res.status(400).json({ error: "description is required" });
    }
    if (typeof amount !== "number" || Number.isNaN(amount) || amount === 0) {
      return res.status(400).json({ error: "amount must be a non-zero number" });
    }
    if (category !== undefined && !VALID_CATEGORIES.includes(category)) {
      return res.status(400).json({ error: "invalid category" });
    }

    const input: NewTransactionInput = { description, amount, category };
    const created = store.addTransaction(input);
    return res.status(201).json(created);
  });

  app.get("/api/budgets", (_req: Request, res: Response) => {
    res.json(store.listBudgets());
  });

  app.post("/api/budgets", (req: Request, res: Response) => {
    const { category, limit } = req.body ?? {};

    if (!VALID_CATEGORIES.includes(category)) {
      return res.status(400).json({ error: "invalid category" });
    }
    if (typeof limit !== "number" || Number.isNaN(limit) || limit < 0) {
      return res.status(400).json({ error: "limit must be a non-negative number" });
    }

    const saved = store.setBudget({ category, limit });
    return res.status(201).json(saved);
  });

  app.get("/api/summary", (_req: Request, res: Response) => {
    const summary = summarize(store.listTransactions(), store.listBudgets());
    res.json(summary);
  });

  app.post("/api/reset", (_req: Request, res: Response) => {
    store.reset();
    res.status(204).end();
  });

  app.use(express.static(PUBLIC_DIR));

  return app;
}
