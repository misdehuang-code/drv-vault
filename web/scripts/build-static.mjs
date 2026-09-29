/**
 * Cloudflare Pages static build:
 * 1) slice commercial_catalog → public/data chunks (<25MB each)
 * 2) stash app/api (Route Handlers incompatible with output:'export')
 * 3) next build → out/
 * 4) restore app/api
 * 5) assert no artifact ≥ 25MB
 */
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const webRoot = path.resolve(__dirname, "..");
const repoRoot = path.resolve(webRoot, "..");
const apiDir = path.join(webRoot, "app", "api");
const apiBak = path.join(webRoot, ".api_backup_cf");
const outDir = path.join(webRoot, "out");
const LIMIT = 25 * 1024 * 1024;

function run(cmd, args, opts = {}) {
  const r = spawnSync(cmd, args, {
    cwd: webRoot,
    stdio: "inherit",
    shell: process.platform === "win32",
    env: { ...process.env, ...opts.env },
  });
  if (r.status !== 0) {
    throw new Error(`${cmd} ${args.join(" ")} exited ${r.status}`);
  }
}

function stashApi() {
  if (!fs.existsSync(apiDir)) return false;
  if (fs.existsSync(apiBak)) {
    fs.rmSync(apiBak, { recursive: true, force: true });
  }
  // copy+remove is more reliable than rename on Windows (file locks)
  fs.cpSync(apiDir, apiBak, { recursive: true });
  fs.rmSync(apiDir, { recursive: true, force: true });
  console.log("[build-static] stashed app/api → .api_backup_cf");
  return true;
}

function restoreApi(moved) {
  if (!moved) return;
  if (fs.existsSync(apiDir)) {
    fs.rmSync(apiDir, { recursive: true, force: true });
  }
  if (fs.existsSync(apiBak)) {
    fs.cpSync(apiBak, apiDir, { recursive: true });
    fs.rmSync(apiBak, { recursive: true, force: true });
    console.log("[build-static] restored app/api");
  }
}

function assertNoOversized(root) {
  const bad = [];
  const walk = (dir) => {
    for (const ent of fs.readdirSync(dir, { withFileTypes: true })) {
      const p = path.join(dir, ent.name);
      if (ent.isDirectory()) walk(p);
      else {
        const n = fs.statSync(p).size;
        if (n >= LIMIT) bad.push({ n, p: path.relative(root, p) });
      }
    }
  };
  if (!fs.existsSync(root)) throw new Error(`missing out dir: ${root}`);
  walk(root);
  if (bad.length) {
    for (const b of bad) {
      console.error(
        `[build-static] OVER_LIMIT ${(b.n / 1024 / 1024).toFixed(2)}MB ${b.p}`,
      );
    }
    throw new Error(`${bad.length} file(s) >= 25MB in out/`);
  }
  console.log("[build-static] OK — no out/ file >= 25MB");
}

let moved = false;
try {
  console.log("[build-static] slicing catalog…");
  run("python", [path.join(repoRoot, "slice_catalog.py")]);

  moved = stashApi();

  console.log("[build-static] next build (CF_PAGES=1, output:export)…");
  run("npx", ["next", "build"], {
    env: {
      CF_PAGES: "1",
      NEXT_TELEMETRY_DISABLED: "1",
    },
  });

  assertNoOversized(outDir);
  console.log("[build-static] done →", outDir);
} catch (err) {
  console.error("[build-static] FAIL:", err instanceof Error ? err.message : err);
  process.exitCode = 1;
} finally {
  restoreApi(moved);
}
