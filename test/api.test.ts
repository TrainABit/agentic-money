import { describe, expect, it, beforeEach } from "vitest";
import request from "supertest";
import { createApp } from "../src/app.js";
import { Store } from "../src/store.js";

function makeApp() {
  return createApp(new Store());
}

describe("agentic-money API", () => {
  let app: ReturnType<typeof makeApp>;

  beforeEach(() => {
    app = makeApp();
  });

  it("reports health", async () => {
    const res = await request(app).get("/api/health");
    expect(res.status).toBe(200);
    expect(res.body).toEqual({ status: "ok" });
  });

  it("creates a transaction and auto-categorizes it", async () => {
    const res = await request(app)
      .post("/api/transactions")
      .send({ description: "Whole Foods groceries", amount: -50 });

    expect(res.status).toBe(201);
    expect(res.body.category).toBe("groceries");
    expect(res.body.id).toBeTruthy();
  });

  it("rejects an invalid transaction", async () => {
    const res = await request(app)
      .post("/api/transactions")
      .send({ description: "", amount: 0 });
    expect(res.status).toBe(400);
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

  it("resets state", async () => {
    await request(app)
      .post("/api/transactions")
      .send({ description: "Coffee", amount: -4 });
    await request(app).post("/api/reset");
    const res = await request(app).get("/api/transactions");
    expect(res.body).toEqual([]);
  });
});
