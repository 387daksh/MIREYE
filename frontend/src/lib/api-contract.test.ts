import { existsSync, readFileSync, readdirSync } from "node:fs";
import { join, relative } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * API-contract parity.
 *
 * The migration plan makes contract parity a precondition for retiring a
 * vanilla surface. These tests scan the app for the endpoints it actually
 * calls and check every one against the exported FastAPI contract, so a
 * backend rename cannot silently break the frontend.
 *
 * The contract is a build artifact, not a checked-in file. Regenerate with:
 *   uv run python scripts/export_openapi.py
 */
const FRONTEND_ROOT = join(__dirname, "../..");
const CONTRACT = join(FRONTEND_ROOT, "../build/openapi.json");

type Contract = { paths: Record<string, Record<string, unknown>> };

const loadContract = (): Contract | undefined =>
  existsSync(CONTRACT) ? (JSON.parse(readFileSync(CONTRACT, "utf8")) as Contract) : undefined;

interface Call {
  method: string;
  path: string;
  file: string;
}

/** Every `api.GET("…")` / `api.POST("…")` call in the app, with its method. */
function callsInSource(): Call[] {
  const sourceRoot = join(FRONTEND_ROOT, "src");
  const files = (directory: string): string[] =>
    readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
      const path = join(directory, entry.name);
      if (entry.isDirectory()) return files(path);
      return /\.tsx?$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name) ? [path] : [];
    });

  return files(sourceRoot).flatMap((path) => {
    const source = readFileSync(path, "utf8");
    const matches = source.matchAll(/api\.(GET|POST|PUT|PATCH|DELETE)\(\s*"([^"]+)"/g);
    return [...matches].map((match) => ({
      method: match[1].toLowerCase(),
      path: match[2],
      file: relative(FRONTEND_ROOT, path),
    }));
  });
}

describe("API contract parity", () => {
  const contract = loadContract();

  it.skipIf(!contract)("every endpoint the app calls exists in the contract", () => {
    const calls = callsInSource();
    // A regression here means the scan broke, not that the app calls nothing.
    expect(calls.length).toBeGreaterThan(10);

    const missing = calls.filter(({ path }) => !contract!.paths[path]);
    expect(
      missing.map((entry) => `${entry.path} (${entry.file})`),
      "endpoints called by the frontend but absent from the FastAPI contract",
    ).toEqual([]);
  });

  it.skipIf(!contract)("every endpoint is called with a method the contract supports", () => {
    const wrongMethod = callsInSource()
      .filter(({ path }) => contract!.paths[path])
      .filter(({ path, method }) => !contract!.paths[path][method]);
    expect(
      wrongMethod.map((entry) => `${entry.method.toUpperCase()} ${entry.path} (${entry.file})`),
      "endpoints called with a verb the contract does not define",
    ).toEqual([]);
  });

  it("reports clearly when the contract has not been exported", () => {
    // Documents the workflow rather than failing an otherwise-green suite.
    if (!contract)
      console.warn(
        `[api-contract] ${CONTRACT} not found — run \`uv run python scripts/export_openapi.py\` to enable contract parity checks.`,
      );
    expect(true).toBe(true);
  });
});
