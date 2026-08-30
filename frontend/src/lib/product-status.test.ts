import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { REQUEST_STATUSES } from "./product-status";

/**
 * Status-machine parity.
 *
 * The intake renders one branch per status returned by /v1/product/requests.
 * The response body is an untyped dict on the FastAPI side, so the generated
 * client cannot catch a new or renamed status — this reads the statuses
 * app/product.py can actually emit and checks the UI still handles them all.
 */
const PRODUCT_PY = join(__dirname, "../../../app/product.py");

function statusesInBackend(): string[] {
  const source = readFileSync(PRODUCT_PY, "utf8");
  const found = [...source.matchAll(/"status":\s*"([A-Z_]+)"/g)].map((match) => match[1]);
  return [...new Set(found)].sort();
}

describe("product request status parity", () => {
  const available = existsSync(PRODUCT_PY);

  it.skipIf(!available)("the UI handles every status the backend can emit", () => {
    const backend = statusesInBackend();
    // Guards against the scan silently matching nothing after a refactor.
    expect(backend.length).toBeGreaterThan(3);

    const unhandled = backend.filter(
      (status) => !(REQUEST_STATUSES as readonly string[]).includes(status),
    );
    expect(unhandled, "statuses app/product.py can return that the intake does not render").toEqual(
      [],
    );
  });

  it.skipIf(!available)("the UI does not claim to handle statuses the backend never sends", () => {
    const backend = statusesInBackend();
    const stale = (REQUEST_STATUSES as readonly string[]).filter(
      (status) => !backend.includes(status),
    );
    expect(stale, "statuses the intake branches on that no longer exist in the backend").toEqual([]);
  });
});
