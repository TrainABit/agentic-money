const fmt = (value) =>
  `${value < 0 ? "-" : ""}$${Math.abs(value).toFixed(2)}`;

async function api(path, options) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    let message = `Request failed (${res.status})`;
    try {
      const body = await res.json();
      if (body && body.error) message = body.error;
    } catch {
      /* ignore body parse errors */
    }
    throw new Error(message);
  }
  return res.status === 204 ? null : res.json();
}

function renderStats(summary) {
  document.getElementById("stat-income").textContent = fmt(summary.income);
  document.getElementById("stat-spending").textContent = fmt(-summary.spending);
  const net = document.getElementById("stat-net");
  net.textContent = fmt(summary.net);
  net.style.color =
    summary.net >= 0 ? "var(--income)" : "var(--spending)";
}

function renderInsights(summary) {
  const list = document.getElementById("insights");
  list.innerHTML = "";
  for (const insight of summary.insights) {
    const li = document.createElement("li");
    li.className = `insight ${insight.level}`;
    li.textContent = insight.message;
    list.appendChild(li);
  }
}

function renderCategories(summary) {
  const container = document.getElementById("categories");
  container.innerHTML = "";
  const spending = summary.categories.filter((c) => c.spent > 0);

  if (spending.length === 0) {
    container.innerHTML = '<p class="hint">No spending recorded yet.</p>';
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
      c.limit && c.limit > 0
        ? Math.min((c.spent / c.limit) * 100, 100)
        : (c.spent / maxSpent) * 100;
    fill.className = "bar-fill" + (c.remaining !== null && c.remaining < 0 ? " over" : "");
    fill.style.width = `${pct}%`;
    bar.appendChild(fill);

    wrap.append(head, bar);
    container.appendChild(wrap);
  }
}

function renderTransactions(transactions) {
  const list = document.getElementById("transactions");
  list.innerHTML = "";
  if (transactions.length === 0) {
    list.innerHTML = '<p class="hint">No transactions yet.</p>';
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

async function refresh() {
  const [summary, transactions] = await Promise.all([
    api("/api/summary"),
    api("/api/transactions"),
  ]);
  renderStats(summary);
  renderInsights(summary);
  renderCategories(summary);
  renderTransactions(transactions);
}

document.getElementById("txn-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const description = document.getElementById("txn-description").value.trim();
  const amount = Number(document.getElementById("txn-amount").value);
  if (!description || Number.isNaN(amount) || amount === 0) return;

  try {
    await api("/api/transactions", {
      method: "POST",
      body: JSON.stringify({ description, amount }),
    });
    event.target.reset();
    document.getElementById("txn-description").focus();
    await refresh();
  } catch (err) {
    alert(err.message);
  }
});

refresh().catch((err) => console.error(err));
