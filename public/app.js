const fmt = (value) =>
  `${value < 0 ? "-" : ""}$${Math.abs(value).toFixed(2)}`;

const byId = (id) => document.getElementById(id);
const TRANSACTION_PAGE_SIZE = 20;
let apiToken = "";
let transactionOffset = 0;
let currentTransactionPagination = {
  limit: TRANSACTION_PAGE_SIZE,
  offset: 0,
  total: 0,
  hasMore: false,
};

async function api(path, options = {}) {
  const headers = { Accept: "application/json", ...options.headers };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (apiToken && options.method && options.method !== "GET") {
    headers.Authorization = `Bearer ${apiToken}`;
  }

  const res = await fetch(path, {
    ...options,
    headers,
  });
  if (!res.ok) {
    let message = `Request failed (${res.status})`;
    try {
      const body = await res.json();
      if (body?.error?.message) message = body.error.message;
    } catch {
      /* ignore body parse errors */
    }
    throw new Error(message);
  }
  return res.status === 204 ? null : res.json();
}

function renderStats(summary) {
  byId("stat-income").textContent = fmt(summary.income);
  byId("stat-spending").textContent = fmt(-summary.spending);
  const net = byId("stat-net");
  net.textContent = fmt(summary.net);
  net.classList.toggle("income", summary.net >= 0);
  net.classList.toggle("spending", summary.net < 0);
}

function renderInsights(summary) {
  const list = byId("insights");
  list.replaceChildren();
  for (const insight of summary.insights) {
    const li = document.createElement("li");
    li.className = `insight ${insight.level}`;
    li.textContent = insight.message;
    list.appendChild(li);
  }
}

function renderCategories(summary) {
  const container = byId("categories");
  container.replaceChildren();
  const spending = summary.categories.filter((c) => c.spent > 0);

  if (spending.length === 0) {
    container.appendChild(emptyMessage("No spending recorded yet."));
    return;
  }

  const maxSpent = Math.max(...spending.map((c) => c.spent));

  for (const c of spending) {
    const wrap = document.createElement("div");
    wrap.className = "category";

    const head = document.createElement("div");
    head.className = "category-head";
    const name = document.createElement("span");
    name.className = "category-name";
    name.textContent = c.category;
    const amount = document.createElement("span");
    amount.className = "category-amount";
    amount.textContent =
      c.limit !== null ? `${fmt(c.spent)} / ${fmt(c.limit)}` : fmt(c.spent);
    head.append(name, amount);

    const bar = document.createElement("div");
    bar.className = "bar";
    const fill = document.createElement("div");
    const pct =
      c.limit !== null && c.limit > 0
        ? Math.min((c.spent / c.limit) * 100, 100)
        : (c.spent / maxSpent) * 100;
    fill.className =
      "bar-fill" +
      (c.limit === 0 || (c.remaining !== null && c.remaining < 0)
        ? " over"
        : "");
    fill.style.width = `${Math.max(0, Math.min(pct, 100))}%`;
    bar.appendChild(fill);

    wrap.append(head, bar);
    container.appendChild(wrap);
  }
}

function renderTransactions(transactions) {
  const list = byId("transactions");
  list.replaceChildren();
  if (transactions.length === 0) {
    list.appendChild(emptyMessage("No transactions yet.", "li"));
    return;
  }
  for (const t of transactions) {
    const li = document.createElement("li");
    li.className = "txn";

    const desc = document.createElement("span");
    desc.className = "txn-desc";
    desc.textContent = t.description;

    const cat = document.createElement("span");
    cat.className = "txn-cat";
    cat.textContent = t.category;

    const amount = document.createElement("span");
    amount.className = "txn-amount " + (t.amount >= 0 ? "pos" : "neg");
    amount.textContent = fmt(t.amount);

    li.append(desc, cat, amount);
    list.appendChild(li);
  }
}

function renderTransactionPagination(pagination) {
  currentTransactionPagination = pagination;
  const first = pagination.total === 0 ? 0 : pagination.offset + 1;
  const last = Math.min(
    pagination.offset + pagination.limit,
    pagination.total,
  );
  byId("transactions-page").textContent =
    pagination.total === 0
      ? "Showing 0 transactions"
      : `Showing ${first}–${last} of ${pagination.total}`;
  byId("transactions-previous").disabled = pagination.offset === 0;
  byId("transactions-next").disabled = !pagination.hasMore;
}

function renderHyperliquid(snapshot) {
  const list = byId("hl-mids");
  const meta = byId("hl-meta");
  const account = byId("hl-account");
  list.replaceChildren();
  const host = (() => {
    try {
      return new URL(snapshot.infoUrl).host;
    } catch {
      return "hyperliquid";
    }
  })();
  meta.textContent = `Read-only mids from ${host}. No keys in this app.`;
  const coins = snapshot.coins.length > 0 ? snapshot.coins : Object.keys(snapshot.mids);
  for (const coin of coins) {
    const item = document.createElement("li");
    const name = document.createElement("span");
    name.className = "hl-coin";
    name.textContent = coin;
    const px = document.createElement("span");
    px.className = "hl-px";
    const mid = snapshot.mids[coin];
    px.textContent = Number.isFinite(mid) ? fmt(mid) : "—";
    item.append(name, px);
    list.appendChild(item);
  }
  if (coins.length === 0) {
    list.appendChild(emptyMessage("No Hyperliquid mids yet.", "li"));
  }
  if (snapshot.address) {
    const equity = Number.isFinite(snapshot.accountValue)
      ? fmt(snapshot.accountValue)
      : "—";
    account.textContent = `Account ${snapshot.address} · equity ${equity}`;
  } else {
    account.textContent = "";
  }
}

function renderBudgets(budgets) {
  const list = byId("budgets");
  list.replaceChildren();
  if (budgets.length === 0) {
    list.appendChild(emptyMessage("No budgets configured yet.", "li"));
    return;
  }

  for (const budget of budgets) {
    const item = document.createElement("li");
    const category = document.createElement("span");
    category.textContent = budget.category;
    const limit = document.createElement("strong");
    limit.textContent = fmt(budget.limit);
    item.append(category, limit);
    list.appendChild(item);
  }
}

function emptyMessage(message, element = "p") {
  const node = document.createElement(element);
  node.className = "hint";
  node.textContent = message;
  return node;
}

function setFormStatus(id, message, kind = "") {
  const status = byId(id);
  status.textContent = message;
  status.className = `form-status ${kind}`.trim();
}

function errorMessage(error) {
  return error instanceof Error ? error.message : "An unexpected error occurred";
}

async function refresh() {
  const [summary, transactionPage, budgets, hyperliquid] = await Promise.all([
    api("/api/summary"),
    api(
      `/api/transactions?limit=${TRANSACTION_PAGE_SIZE}&offset=${transactionOffset}`,
    ),
    api("/api/budgets"),
    api("/api/hyperliquid").catch(() => null),
  ]);
  renderStats(summary);
  renderInsights(summary);
  renderCategories(summary);
  renderTransactions(transactionPage.data);
  renderTransactionPagination(transactionPage.pagination);
  renderBudgets(budgets);
  if (hyperliquid) {
    renderHyperliquid(hyperliquid);
  } else {
    byId("hl-meta").textContent =
      "Hyperliquid mids unavailable. The rest of the dashboard still works.";
    byId("hl-mids").replaceChildren();
    byId("hl-account").textContent = "";
  }
}

async function navigateTransactions(offset) {
  const previousOffset = transactionOffset;
  transactionOffset = Math.max(0, offset);
  byId("transactions-previous").disabled = true;
  byId("transactions-next").disabled = true;

  try {
    const transactionPage = await api(
      `/api/transactions?limit=${TRANSACTION_PAGE_SIZE}&offset=${transactionOffset}`,
    );
    renderTransactions(transactionPage.data);
    renderTransactionPagination(transactionPage.pagination);
    byId("load-status").textContent = "";
  } catch (error) {
    transactionOffset = previousOffset;
    renderTransactionPagination(currentTransactionPagination);
    showLoadError(error);
  }
}

function showLoadError(error) {
  const status = byId("load-status");
  status.textContent = `Could not load the dashboard: ${errorMessage(error)}`;
  status.classList.add("error");
}

const tokenInput = byId("api-token");
tokenInput.value = "";
tokenInput.addEventListener("input", (event) => {
  apiToken = event.currentTarget.value;
});

byId("transactions-previous").addEventListener("click", () => {
  void navigateTransactions(
    currentTransactionPagination.offset -
      currentTransactionPagination.limit,
  );
});

byId("transactions-next").addEventListener("click", () => {
  if (!currentTransactionPagination.hasMore) return;
  void navigateTransactions(
    currentTransactionPagination.offset +
      currentTransactionPagination.limit,
  );
});

byId("txn-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector("button");
  const description = byId("txn-description").value.trim();
  const amount = Number(byId("txn-amount").value);
  setFormStatus("txn-status", "");
  if (!description || !Number.isFinite(amount) || amount === 0) {
    setFormStatus(
      "txn-status",
      "Enter a description and a non-zero finite amount.",
      "error",
    );
    return;
  }

  button.disabled = true;
  try {
    await api("/api/transactions", {
      method: "POST",
      body: JSON.stringify({ description, amount }),
    });
    form.reset();
    byId("txn-description").focus();
    setFormStatus("txn-status", "Transaction added.", "success");
    transactionOffset = 0;
    await refresh();
  } catch (error) {
    setFormStatus("txn-status", errorMessage(error), "error");
  } finally {
    button.disabled = false;
  }
});

byId("budget-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector("button");
  const category = byId("budget-category").value;
  const limit = Number(byId("budget-limit").value);
  setFormStatus("budget-status", "");
  if (!Number.isFinite(limit) || limit < 0) {
    setFormStatus(
      "budget-status",
      "Enter a finite, non-negative limit.",
      "error",
    );
    return;
  }

  button.disabled = true;
  try {
    await api("/api/budgets", {
      method: "POST",
      body: JSON.stringify({ category, limit }),
    });
    byId("budget-limit").value = "";
    setFormStatus("budget-status", "Budget saved.", "success");
    await refresh();
  } catch (error) {
    setFormStatus("budget-status", errorMessage(error), "error");
  } finally {
    button.disabled = false;
  }
});

refresh().catch(showLoadError);
