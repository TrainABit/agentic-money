export const DEFAULT_INFO_URL = "https://api.hyperliquid-testnet.xyz/info";
export const DEFAULT_COINS = ["BTC", "ETH"] as const;

export interface HyperliquidPosition {
  coin: string;
  size: number;
  entryPx?: number;
}

export interface HyperliquidSnapshot {
  venue: "hyperliquid";
  infoUrl: string;
  coins: string[];
  mids: Record<string, number>;
  address?: string;
  accountValue?: number | null;
  positions: HyperliquidPosition[];
}

export interface FetchHyperliquidOptions {
  infoUrl: string;
  coins: readonly string[];
  address?: string;
  fetchImpl?: typeof fetch;
}

export class HyperliquidUnavailableError extends Error {
  override readonly name = "HyperliquidUnavailableError";
}

export async function fetchHyperliquidSnapshot(
  options: FetchHyperliquidOptions,
): Promise<HyperliquidSnapshot> {
  const fetchImpl = options.fetchImpl ?? fetch;
  const coins = [...options.coins];
  const midsRaw = await postInfo(fetchImpl, options.infoUrl, { type: "allMids" });
  const mids = parseMids(midsRaw, coins);

  const snapshot: HyperliquidSnapshot = {
    venue: "hyperliquid",
    infoUrl: options.infoUrl,
    coins,
    mids,
    positions: [],
  };

  if (options.address !== undefined) {
    snapshot.address = options.address;
    const state = await postInfo(fetchImpl, options.infoUrl, {
      type: "clearinghouseState",
      user: options.address,
    });
    snapshot.accountValue = parseAccountValue(state);
    snapshot.positions = parsePositions(state);
  }

  return snapshot;
}

async function postInfo(
  fetchImpl: typeof fetch,
  infoUrl: string,
  body: Record<string, unknown>,
): Promise<unknown> {
  let response: Response;
  try {
    response = await fetchImpl(infoUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(body),
    });
  } catch (error) {
    throw new HyperliquidUnavailableError(
      error instanceof Error ? error.message : "hyperliquid request failed",
    );
  }
  if (!response.ok) {
    throw new HyperliquidUnavailableError(
      `hyperliquid info returned HTTP ${response.status}`,
    );
  }
  try {
    return await response.json();
  } catch {
    throw new HyperliquidUnavailableError("hyperliquid info returned invalid JSON");
  }
}

function parseMids(raw: unknown, coins: readonly string[]): Record<string, number> {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    throw new HyperliquidUnavailableError("allMids: expected an object");
  }
  const source = raw as Record<string, unknown>;
  const mids: Record<string, number> = {};
  for (const coin of coins) {
    const value = Number(source[coin]);
    if (Number.isFinite(value) && value > 0) {
      mids[coin] = value;
    }
  }
  return mids;
}

function parseAccountValue(raw: unknown): number | null {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    return null;
  }
  const summary = (raw as { marginSummary?: { accountValue?: unknown } })
    .marginSummary;
  const value = Number(summary?.accountValue);
  return Number.isFinite(value) ? value : null;
}

function parsePositions(raw: unknown): HyperliquidPosition[] {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    return [];
  }
  const assets = (raw as { assetPositions?: unknown }).assetPositions;
  if (!Array.isArray(assets)) return [];
  const positions: HyperliquidPosition[] = [];
  for (const asset of assets) {
    if (typeof asset !== "object" || asset === null) continue;
    const position = (asset as { position?: Record<string, unknown> }).position;
    if (!position) continue;
    const coin = String(position.coin ?? "");
    const size = Number(position.szi);
    if (!coin || !Number.isFinite(size) || size === 0) continue;
    const entryPx = Number(position.entryPx);
    positions.push({
      coin,
      size,
      ...(Number.isFinite(entryPx) ? { entryPx } : {}),
    });
  }
  return positions;
}
