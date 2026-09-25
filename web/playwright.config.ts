import { defineConfig } from "@playwright/test";

/**
 * Frontend regression suite for async-isolation behaviour (PR #20 review).
 *
 * Boots an isolated backend (uvicorn on 8310 with its own data dir) and a
 * Next dev server on 3100 whose /api proxy targets that backend. Run with:
 *
 *   npx playwright install chromium   # first time only
 *   npm --prefix web run test:e2e
 */
export default defineConfig({
  testDir: "./tests/e2e",
  timeout: 60_000,
  retries: 0,
  workers: 1, // one backend: avoid project-list and SSE interference
  reporter: [["list"]],
  use: {
    baseURL: "http://localhost:3100",
    trace: "off",
  },
  webServer: [
    {
      command: ".venv/bin/python -m uvicorn web.api.main:app --host 127.0.0.1 --port 8310 --workers 1 --log-level warning",
      cwd: "..",
      url: "http://127.0.0.1:8310/api/projects",
      reuseExistingServer: true,
      env: { SCRIPTWEAVER_DATA_DIR: "/tmp/script-weaver-e2e-data" },
    },
    {
      command: "npm run dev -- --port 3100",
      url: "http://localhost:3100",
      reuseExistingServer: true,
      env: { SCRIPTWEAVER_API_TARGET: "http://127.0.0.1:8310" },
    },
  ],
});
