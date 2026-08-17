import { Store } from "../../src/store.js";

const [databasePath, prefix, countText] = process.argv.slice(2);
if (databasePath === undefined || prefix === undefined || countText === undefined) {
  throw new Error("usage: store-writer DATABASE PREFIX COUNT");
}

const count = Number(countText);
if (!Number.isSafeInteger(count) || count < 1) {
  throw new Error("COUNT must be a positive integer");
}

const store = new Store(databasePath);
try {
  for (let index = 0; index < count; index += 1) {
    store.addTransaction({
      description: `${prefix}-${index}`,
      amount: -(index + 1) / 100,
      category: "other",
    });
  }
} finally {
  store.close();
}
