import { spawnSync, spawn } from "node:child_process";
import { mkdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

import type { Page } from "@playwright/test";

const HERE = dirname(fileURLToPath(import.meta.url));
const PROJECT_ROOT = resolve(HERE, "..", "..");
const FIXTURE_SCRIPT = resolve(HERE, "fixture.py");
const WEB_DIST = resolve(HERE, "..", "dist");

export interface Fixture {
  vault_root: string;
  base_url: string;
  session_token: string;
  conflict_id: string;
  proposal_revision: number;
  memory_id: string;
  batch_ids: string[];
  trace_id: string;
  /** python process hosting the loopback server (kept alive while test runs). */
  server_pid: number;
}

/** Build the static dist once per test process. */
let distBuilt = false;
export function ensureDistBuilt(): void {
  if (distBuilt) return;
  const r = spawnSync("npx", ["vite", "build"], {
    cwd: resolve(HERE, ".."),
    stdio: "inherit",
    env: { ...process.env, NODE_ENV: "production" },
  });
  if (r.status !== 0) {
    throw new Error(`vite build failed: ${r.status}`);
  }
  distBuilt = true;
}

/**
 * Build a fresh fixture vault and start its loopback HTTP server.
 *
 * The Python loopback server is the SAME process that serves the API
 * **and** the static dist at ``/`` — so the React app loads on the
 * same origin as the API, eliminating CORS in tests.
 *
 * Returns once the server has bound and printed its JSON config to
 * stdout. The server stays alive until {@link teardownFixture} is
 * called.
 */
export async function buildFixture(): Promise<Fixture> {
  ensureDistBuilt();
  const tmp = join(tmpdir(), `ma-pw-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`);
  mkdirSync(tmp, { recursive: true });
  const vault = join(tmp, "vault");
  mkdirSync(vault, { recursive: true });
  const child = spawn(
    "python3",
    [
      FIXTURE_SCRIPT,
      "build",
      "--vault-root", vault,
      "--ui-dist", WEB_DIST,
      "--keep-running",
    ],
    {
      cwd: PROJECT_ROOT,
      env: {
        ...process.env,
        MEMORY_AGENT_MODEL_PROVIDER: "fake",
        MEMORY_AGENT_HTTP_TOKEN: "playwright-test-token",
      },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  child.stderr?.on("data", (c: Buffer) => {
    process.stderr.write(`[fixture stderr] ${c.toString("utf-8")}`);
  });
  const jsonLine: string = await new Promise((resolveLine, rejectLine) => {
    let buf = "";
    const timer = setTimeout(() => rejectLine(new Error("fixture timeout")), 60_000);
    child.stdout.on("data", (chunk: Buffer) => {
      buf += chunk.toString("utf-8");
      const idx = buf.lastIndexOf("\n");
      if (idx >= 0) {
        const lines = buf.slice(0, idx).trim().split("\n");
        const last = lines[lines.length - 1] ?? "";
        try {
          JSON.parse(last);
          clearTimeout(timer);
          resolveLine(last);
        } catch {
          // Keep reading until we get a valid JSON line.
        }
      }
    });
    child.on("error", (err: Error) => {
      clearTimeout(timer);
      rejectLine(err);
    });
  });
  const parsed = JSON.parse(jsonLine) as Omit<Fixture, "vault_root" | "server_pid">;
  return { ...parsed, vault_root: vault, server_pid: child.pid ?? -1 };
}

/** Stop the fixture loopback server. */
export function teardownFixture(fx: Fixture): void {
  if (fx.server_pid > 0) {
    try {
      process.kill(fx.server_pid, "SIGTERM");
    } catch {
      // process may already have exited
    }
  }
  try {
    rmSync(fx.vault_root, { recursive: true, force: true });
  } catch {
    // best-effort
  }
}

/**
 * Bump the conflict revision directly via the fixture's CLI. The
 * Playwright stale-revision test uses this to make the UI's cached
 * revision stale without going through the API (which would either
 * refuse or resolve the decide itself).
 */
export function bumpConflictRevision(fx: Fixture, conflictId: string): void {
  const r = spawnSync(
    "python3",
    [FIXTURE_SCRIPT, "bump-conflict", "--vault-root", fx.vault_root, "--conflict-id", conflictId],
    { cwd: PROJECT_ROOT, encoding: "utf-8" },
  );
  if (r.status !== 0) {
    throw new Error(`bump-conflict failed: ${r.stderr}`);
  }
}

/** Boot the React app on the fixture's loopback server. */
export async function bootAppWithFixture(page: Page, fx: Fixture): Promise<void> {
  await page.addInitScript({
    content: `window.__MA_CONFIG__ = ${JSON.stringify({
      origin: fx.base_url,
      sessionToken: fx.session_token,
    })};`,
  });
  await page.goto(fx.base_url + "/", { waitUntil: "domcontentloaded" });
}