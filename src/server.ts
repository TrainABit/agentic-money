import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { createApp } from "./app.js";
import { Store } from "./store.js";

const __dirname = dirname(fileURLToPath(import.meta.url));

const PORT = Number(process.env.PORT ?? 3000);
const HOST = process.env.HOST ?? "0.0.0.0";
const DATA_FILE =
  process.env.DATA_FILE ?? join(__dirname, "..", "data", "store.json");

const store = new Store(DATA_FILE);

// Seed a small, realistic demo dataset on first run so the dashboard is not
// empty when someone opens it for the first time.
if (store.listTransactions().length === 0) {
  store.setBudget({ category: "groceries", limit: 400 });
  store.setBudget({ category: "dining", limit: 200 });
  store.setBudget({ category: "transport", limit: 150 });
  store.addTransaction({ description: "Monthly paycheck", amount: 3200 });
  store.addTransaction({ description: "Whole Foods groceries", amount: -180.42 });
  store.addTransaction({ description: "Starbucks coffee", amount: -6.75 });
  store.addTransaction({ description: "Uber ride downtown", amount: -23.1 });
  store.addTransaction({ description: "Netflix subscription", amount: -15.49 });
}

const app = createApp(store);

app.listen(PORT, HOST, () => {
  console.log(`agentic-money running at http://${HOST}:${PORT}`);
});
