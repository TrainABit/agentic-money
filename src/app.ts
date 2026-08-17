import { timingSafeEqual } from "node:crypto";
import express, {
  type ErrorRequestHandler,
  type Express,
  type NextFunction,
  type Request,
  type RequestHandler,
  type Response,
} from "express";
import { rateLimit } from "express-rate-limit";
import helmet from "helmet";
import { PUBLIC_DIR } from "./config.js";
import { toMinorUnits } from "./money.js";
import { Store } from "./store.js";
import {
  isCategory,
  isSpendingCategory,
  type NewTransactionInput,
} from "./types.js";

export interface AppOptions {
  apiToken?: string;
  resetEnabled?: boolean;
  publicDir?: string;
  rateLimit?: number | false;
  trustProxy?: number;
}

class HttpError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "HttpError";
  }
}

const DEFAULT_RATE_LIMIT = 120;

export function createApp(store: Store, options: AppOptions = {}): Express {
  if (options.apiToken !== undefined && options.apiToken.length === 0) {
    throw new Error("API token cannot be empty");
  }
  if (options.resetEnabled === true && options.apiToken === undefined) {
    throw new Error("reset cannot be enabled without an API token");
  }
  if (
    options.trustProxy !== undefined &&
    (!Number.isSafeInteger(options.trustProxy) ||
      options.trustProxy < 0 ||
      options.trustProxy > 255)
  ) {
    throw new Error("trustProxy must be an integer between 0 and 255");
  }

  const app = express();
  app.disable("x-powered-by");
  app.set("trust proxy", options.trustProxy ?? 0);
  app.use(
    helmet({
      contentSecurityPolicy: {
        directives: {
          defaultSrc: ["'self'"],
          baseUri: ["'self'"],
          connectSrc: ["'self'"],
          fontSrc: ["'self'"],
          formAction: ["'self'"],
          frameAncestors: ["'none'"],
          imgSrc: ["'self'", "data:"],
          objectSrc: ["'none'"],
          scriptSrc: ["'self'"],
          styleSrc: ["'self'"],
          styleSrcAttr: ["'unsafe-inline'"],
        },
      },
    }),
  );
  app.use(express.json({ limit: "32kb", strict: true }));

  if (options.rateLimit !== false) {
    app.use(
      "/api",
      rateLimit({
        windowMs: 60_000,
        limit: options.rateLimit ?? DEFAULT_RATE_LIMIT,
        standardHeaders: "draft-8",
        legacyHeaders: false,
        handler: (_request, response) => {
          sendError(
            response,
            429,
            "rate_limit_exceeded",
            "too many requests; please try again later",
          );
        },
      }),
    );
  }

  const authenticate = requireApiToken(options.apiToken);

  app.get("/api/health", (_req: Request, res: Response) => {
    const health = store.health();
    res.status(health.status === "ok" ? 200 : 503).json(health);
  });

  app.get("/api/transactions", (req: Request, res: Response) => {
    const limit = parsePaginationQuery(req.query.limit, "limit", 50, 1, 100);
    const offset = parsePaginationQuery(
      req.query.offset,
      "offset",
      0,
      0,
      Number.MAX_SAFE_INTEGER,
    );
    res.json(store.getTransactionsPage({ limit, offset }));
  });

  app.post("/api/transactions", authenticate, (req: Request, res: Response) => {
    const body = requireObjectBody(req.body);
    const { description, amount, category } = body;

    if (typeof description !== "string" || description.trim().length === 0) {
      throw new HttpError(400, "validation_error", "description is required");
    }
    if (typeof amount !== "number" || !Number.isFinite(amount)) {
      throw new HttpError(
        400,
        "validation_error",
        "amount must be a finite number",
      );
    }
    let amountMinor: number;
    try {
      amountMinor = toMinorUnits(amount, "amount");
    } catch (error) {
      throw validationError(error);
    }
    if (amountMinor === 0) {
      throw new HttpError(
        400,
        "validation_error",
        "amount must be at least one cent and non-zero",
      );
    }
    if (category !== undefined && !isCategory(category)) {
      throw new HttpError(400, "validation_error", "invalid category");
    }

    const input: NewTransactionInput = {
      description,
      amount,
      ...(category === undefined ? {} : { category }),
    };
    try {
      const created = store.addTransaction(input);
      res.status(201).json(created);
    } catch (error) {
      throw validationError(error);
    }
  });

  app.get("/api/budgets", (_req: Request, res: Response) => {
    res.json(store.listBudgets());
  });

  app.post("/api/budgets", authenticate, (req: Request, res: Response) => {
    const body = requireObjectBody(req.body);
    const { category, limit } = body;

    if (!isSpendingCategory(category)) {
      throw new HttpError(
        400,
        "validation_error",
        category === "income"
          ? "budgets cannot be set for income"
          : "invalid category",
      );
    }
    if (typeof limit !== "number" || !Number.isFinite(limit)) {
      throw new HttpError(
        400,
        "validation_error",
        "limit must be a finite number",
      );
    }

    try {
      const saved = store.setBudget({ category, limit });
      res.status(201).json(saved);
    } catch (error) {
      throw validationError(error);
    }
  });

  app.get("/api/summary", (_req: Request, res: Response) => {
    res.json(store.getSummary());
  });

  if (options.resetEnabled === true) {
    app.post("/api/reset", authenticate, (_req: Request, res: Response) => {
      store.reset();
      res.status(204).end();
    });
  }

  app.use("/api", (_req: Request, _res: Response, next: NextFunction) => {
    next(new HttpError(404, "not_found", "API route not found"));
  });

  app.use(express.static(options.publicDir ?? PUBLIC_DIR));

  const errorHandler: ErrorRequestHandler = (
    error: unknown,
    _request,
    response,
    _next,
  ) => {
    if (error instanceof HttpError) {
      sendError(response, error.status, error.code, error.message);
      return;
    }
    if (isExpressClientError(error)) {
      const isTooLarge = error.status === 413;
      const isUnsupported = error.status === 415;
      sendError(
        response,
        error.status,
        isTooLarge
          ? "payload_too_large"
          : isUnsupported
            ? "unsupported_media_type"
            : error.type === "entity.parse.failed"
              ? "invalid_json"
              : "invalid_request",
        isTooLarge
          ? "request body is too large"
          : isUnsupported
            ? "request content type or encoding is not supported"
            : error.type === "entity.parse.failed"
              ? "request body must be valid JSON"
              : "request could not be processed",
      );
      return;
    }

    console.error("Unhandled request error", error);
    sendError(
      response,
      500,
      "internal_error",
      "an unexpected server error occurred",
    );
  };
  app.use(errorHandler);
  return app;
}

function requireApiToken(expectedToken: string | undefined): RequestHandler {
  if (expectedToken === undefined) {
    return (_request, _response, next) => {
      next();
    };
  }
  const expected = Buffer.from(expectedToken);

  return (request, response, next) => {
    const authorization = request.get("authorization");
    const bearer = authorization?.startsWith("Bearer ")
      ? authorization.slice("Bearer ".length)
      : undefined;
    const headerToken = request.get("x-api-token");
    const supplied = bearer ?? headerToken;
    const suppliedBuffer =
      supplied === undefined ? undefined : Buffer.from(supplied);

    if (
      suppliedBuffer === undefined ||
      suppliedBuffer.length !== expected.length ||
      !timingSafeEqual(suppliedBuffer, expected)
    ) {
      sendError(
        response,
        401,
        "unauthorized",
        "a valid API token is required",
      );
      return;
    }
    next();
  };
}

function requireObjectBody(value: unknown): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new HttpError(
      400,
      "validation_error",
      "request body must be a JSON object",
    );
  }
  return value as Record<string, unknown>;
}

function parsePaginationQuery(
  value: unknown,
  field: string,
  fallback: number,
  minimum: number,
  maximum: number,
): number {
  if (value === undefined) return fallback;
  if (typeof value !== "string" || !/^\d+$/.test(value)) {
    throw new HttpError(
      400,
      "validation_error",
      `${field} must be an integer`,
    );
  }
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed < minimum || parsed > maximum) {
    throw new HttpError(
      400,
      "validation_error",
      `${field} must be between ${minimum} and ${maximum}`,
    );
  }
  return parsed;
}

function validationError(error: unknown): HttpError {
  if (error instanceof TypeError || error instanceof RangeError) {
    return new HttpError(400, "validation_error", error.message);
  }
  throw error;
}

function sendError(
  response: Response,
  status: number,
  code: string,
  message: string,
): void {
  response.status(status).json({ error: { code, message } });
}

function isExpressClientError(
  error: unknown,
): error is { status: number; type?: string } {
  if (typeof error !== "object" || error === null) return false;
  const candidate = error as { status?: unknown; type?: unknown };
  return (
    typeof candidate.status === "number" &&
    candidate.status >= 400 &&
    candidate.status < 500
  );
}
