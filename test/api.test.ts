import { afterEach, beforeEach, describe, expect, it } from "vitest";
import request from "supertest";
import { createApp } from "../src/app.js";
import { Store } from "../src/store.js";

describe("agentic-money API", () => {
  let store: Store;
  let app: ReturnType<typeof createApp>;

  beforeEach(() => {
    store = new Store();
    app = createApp(store, { rateLimit: false });
  });

  afterEach(() => {
    store.close();
  });

  it("reports persistence-aware health", async () => {
    const res = await request(app).get("/api/health");
    expect(res.status).toBe(200);
    expect(res.body).toEqual({
      status: "ok",
      storage: {
        status: "ok",
        type: "sqlite",
        persistent: false,
      },
    });
  });

  it("creates a transaction and auto-categorizes it", async () => {
    const res = await request(app)
      .post("/api/transactions")
      .send({ description: "Whole Foods groceries", amount: -50 });

    expect(res.status).toBe(201);
    expect(res.body.category).toBe("groceries");
    expect(res.body.amount).toBe(-50);
    expect(res.body.id).toBeTruthy();
  });

  it("normalizes transaction and budget amounts to cents", async () => {
    const transaction = await request(app)
      .post("/api/transactions")
      .send({ description: "Invoice paid", amount: 1.005 });
    const budget = await request(app)
      .post("/api/budgets")
      .send({ category: "other", limit: 10.005 });

    expect(transaction.status).toBe(201);
    expect(transaction.body.amount).toBe(1.01);
    expect(budget.status).toBe(201);
    expect(budget.body.limit).toBe(10.01);
  });

  it("rejects invalid and JSON-overflow monetary values", async () => {
    const res = await request(app)
      .post("/api/transactions")
      .send({ description: "", amount: 0 });
    expect(res.status).toBe(400);
    expect(res.body.error.code).toBe("validation_error");

    const overflow = await request(app)
      .post("/api/transactions")
      .set("Content-Type", "application/json")
      .send('{"description":"Overflow","amount":1e400}');
    expect(overflow.status).toBe(400);
    expect(overflow.body.error.message).toMatch(/finite/);

    const budgetOverflow = await request(app)
      .post("/api/budgets")
      .set("Content-Type", "application/json")
      .send('{"category":"other","limit":1e400}');
    expect(budgetOverflow.status).toBe(400);
    expect(budgetOverflow.body.error.message).toMatch(/finite/);
  });

  it("enforces amount-sign categories and disallows income budgets", async () => {
    const positiveExpense = await request(app)
      .post("/api/transactions")
      .send({ description: "Refund", amount: 10, category: "groceries" });
    const negativeIncome = await request(app)
      .post("/api/transactions")
      .send({ description: "Reversal", amount: -10, category: "income" });
    const incomeBudget = await request(app)
      .post("/api/budgets")
      .send({ category: "income", limit: 100 });

    expect(positiveExpense.status).toBe(400);
    expect(positiveExpense.body.error.message).toMatch(/positive amounts/);
    expect(negativeIncome.status).toBe(400);
    expect(negativeIncome.body.error.message).toMatch(/negative amounts/);
    expect(incomeBudget.status).toBe(400);
    expect(incomeBudget.body.error.message).toMatch(/income/);
  });

  it("computes a summary with budget insights end to end", async () => {
    await request(app)
      .post("/api/budgets")
      .send({ category: "groceries", limit: 100 });
    await request(app)
      .post("/api/transactions")
      .send({ description: "Costco groceries", amount: -120 });
    await request(app)
      .post("/api/transactions")
      .send({ description: "Monthly paycheck", amount: 1000 });

    const res = await request(app).get("/api/summary");
    expect(res.status).toBe(200);
    expect(res.body.income).toBe(1000);
    expect(res.body.spending).toBe(120);
    expect(
      res.body.insights.some((i: { message: string }) =>
        i.message.includes("over budget"),
      ),
    ).toBe(true);
  });

  it("paginates transactions and validates pagination parameters", async () => {
    for (let index = 0; index < 5; index += 1) {
      store.addTransaction({
        description: `Transaction ${index}`,
        amount: -(index + 1),
        category: "other",
      });
    }

    const res = await request(app).get(
      "/api/transactions?limit=2&offset=2",
    );
    expect(res.status).toBe(200);
    expect(res.body.data).toHaveLength(2);
    expect(res.body.pagination).toEqual({
      limit: 2,
      offset: 2,
      total: 5,
      hasMore: true,
    });

    const invalid = await request(app).get("/api/transactions?limit=101");
    expect(invalid.status).toBe(400);
    expect(invalid.body.error.code).toBe("validation_error");
  });

  it("protects every mutating route when a token is configured", async () => {
    app = createApp(store, {
      apiToken: "test-secret",
      resetEnabled: true,
      rateLimit: false,
    });

    const transaction = request(app)
      .post("/api/transactions")
      .send({ description: "Coffee", amount: -4 });
    const budget = request(app)
      .post("/api/budgets")
      .send({ category: "dining", limit: 10 });
    const reset = request(app).post("/api/reset");
    const unauthorized = await Promise.all([transaction, budget, reset]);
    expect(unauthorized.map(({ status }) => status)).toEqual([401, 401, 401]);
    expect(
      unauthorized.every(
        ({ body }) => body.error.code === "unauthorized",
      ),
    ).toBe(true);

    expect(
      (
        await request(app)
          .post("/api/transactions")
          .set("Authorization", "Bearer test-secret")
          .send({ description: "Coffee", amount: -4 })
      ).status,
    ).toBe(201);
    expect(
      (
        await request(app)
          .post("/api/budgets")
          .set("X-API-Token", "test-secret")
          .send({ category: "dining", limit: 10 })
      ).status,
    ).toBe(201);
    expect(
      (
        await request(app)
          .post("/api/reset")
          .set("Authorization", "Bearer test-secret")
      ).status,
    ).toBe(204);
    expect(store.transactionCount()).toBe(0);
  });

  it("keeps reset disabled unless explicitly enabled with auth", async () => {
    const disabled = await request(app).post("/api/reset");
    expect(disabled.status).toBe(404);
    expect(disabled.body.error).toEqual({
      code: "not_found",
      message: "API route not found",
    });
    expect(() =>
      createApp(store, { resetEnabled: true, rateLimit: false }),
    ).toThrow(/API token/);
    expect(() =>
      createApp(store, { apiToken: "", rateLimit: false }),
    ).toThrow(/cannot be empty/);
  });

  it("uses consistent JSON errors for malformed JSON and unknown API routes", async () => {
    const malformed = await request(app)
      .post("/api/transactions")
      .set("Content-Type", "application/json")
      .send('{"description":');
    expect(malformed.status).toBe(400);
    expect(malformed.body).toEqual({
      error: {
        code: "invalid_json",
        message: "request body must be valid JSON",
      },
    });

    const missing = await request(app).get("/api/not-a-route");
    expect(missing.status).toBe(404);
    expect(missing.type).toMatch(/json/);
    expect(missing.body.error.code).toBe("not_found");
  });

  it("serves an XSS-safe dashboard with security headers", async () => {
    const dashboard = await request(app).get("/");
    const script = await request(app).get("/app.js");

    expect(dashboard.status).toBe(200);
    expect(dashboard.headers["x-powered-by"]).toBeUndefined();
    expect(dashboard.headers["content-security-policy"]).toContain(
      "default-src 'self'",
    );
    expect(dashboard.text).toContain("Manage budgets");
    expect(dashboard.text).toContain("Hyperliquid");
    expect(dashboard.text).toContain('id="hl-mids"');
    expect(dashboard.text).toContain('id="transactions-previous"');
    expect(dashboard.text).toContain('id="transactions-page"');
    expect(dashboard.text).toContain('id="transactions-next"');
    expect(script.status).toBe(200);
    expect(script.text).toContain("textContent");
    expect(script.text).toContain("pagination.hasMore");
    expect(script.text).toContain("/api/hyperliquid");
    expect(script.text).toContain("renderHyperliquid");
    expect(script.text).not.toContain("localStorage");
    expect(script.text).not.toContain("sessionStorage");
    expect(script.text).not.toContain("innerHTML");
  });

  it("configures trusted proxy hops before API rate limiting", async () => {
    app = createApp(store, { rateLimit: 1, trustProxy: 1 });
    expect(app.get("trust proxy")).toBe(1);

    const firstClient = await request(app)
      .get("/api/health")
      .set("X-Forwarded-For", "203.0.113.10");
    const secondClient = await request(app)
      .get("/api/health")
      .set("X-Forwarded-For", "198.51.100.20");
    const secondClientAgain = await request(app)
      .get("/api/health")
      .set("X-Forwarded-For", "198.51.100.20");

    expect(firstClient.status).toBe(200);
    expect(secondClient.status).toBe(200);
    expect(secondClientAgain.status).toBe(429);
  });

  it("rate limits API requests with a JSON error", async () => {
    app = createApp(store, { rateLimit: 2 });
    expect((await request(app).get("/api/health")).status).toBe(200);
    expect((await request(app).get("/api/health")).status).toBe(200);
    const limited = await request(app).get("/api/health");

    expect(limited.status).toBe(429);
    expect(limited.body.error.code).toBe("rate_limit_exceeded");
    expect(limited.headers["ratelimit"]).toBeTruthy();
  });

  it("exposes a read-only Hyperliquid snapshot from an injected client", async () => {
    const fetchImpl: typeof fetch = async (_url, init) => {
      const body = JSON.parse(String(init?.body ?? "{}")) as { type?: string };
      if (body.type === "allMids") {
        return new Response(JSON.stringify({ BTC: "65000.5", ETH: "3500.25" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (body.type === "clearinghouseState") {
        return new Response(
          JSON.stringify({
            marginSummary: { accountValue: "12.5" },
            assetPositions: [
              { position: { coin: "BTC", szi: "0.01", entryPx: "64000" } },
            ],
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      return new Response("unexpected", { status: 500 });
    };
    app = createApp(store, {
      rateLimit: false,
      hyperliquid: {
        coins: ["BTC", "ETH"],
        address: "0xabc",
        fetchImpl,
      },
    });

    const res = await request(app).get("/api/hyperliquid");
    expect(res.status).toBe(200);
    expect(res.body).toMatchObject({
      venue: "hyperliquid",
      mids: { BTC: 65000.5, ETH: 3500.25 },
      address: "0xabc",
      accountValue: 12.5,
    });
    expect(res.body.positions).toEqual([
      { coin: "BTC", size: 0.01, entryPx: 64000 },
    ]);
    expect(JSON.stringify(res.body)).not.toMatch(/private|secret|mnemonic/i);
  });

  it("returns 502 when Hyperliquid info is unavailable", async () => {
    app = createApp(store, {
      rateLimit: false,
      hyperliquid: {
        fetchImpl: async () => {
          throw new Error("network down");
        },
      },
    });
    const res = await request(app).get("/api/hyperliquid");
    expect(res.status).toBe(502);
    expect(res.body.error.code).toBe("hyperliquid_unavailable");
  });

  it("returns 503 when persistent storage is unavailable", async () => {
    store.close();
    const res = await request(app).get("/api/health");
    expect(res.status).toBe(503);
    expect(res.body).toMatchObject({
      status: "error",
      storage: { status: "error", message: "database is closed" },
    });
  });
});
