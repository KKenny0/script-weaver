import { defineConfig } from "@playwright/test";

/**
 * Frontend regression suite for async-isolation behaviour (PR #20 review).
 *
 * Boots an isolated backend (uvicorn on 8310 with its own data dir) and a
 * Next dev server on 3100 whose /api proxy targets that backend. The
 * backend is forced model-less (see env below) and generation runs through
 * the in-page EventSource stand-in, so the suite never calls a real model.
 * Run with:
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
      env: {
        SCRIPTWEAVER_DATA_DIR: "/tmp/script-weaver-e2e-data",
        // The backend must stay model-less even on machines whose root .env
        // holds real keys: real environment variables win over pydantic
        // dotenv values, so empty overrides disarm every provider and any
        // accidental engine use fails fast with model_not_configured
        // instead of egressing to a paid API.
        SCRIPTWEAVER_ANTHROPIC_API_KEY: "",
        SCRIPTWEAVER_OPENAI_API_KEY: "",
        SCRIPTWEAVER_DEEPSEEK_API_KEY: "",
        SCRIPTWEAVER_GLM_API_KEY: "",
        SCRIPTWEAVER_QWEN_API_KEY: "",
      },
    },
    {
      command: "npm run dev -- --port 3100",
      url: "http://localhost:3100",
      reuseExistingServer: true,
      env: { SCRIPTWEAVER_API_TARGET: "http://127.0.0.1:8310" },
    },
  ],
});
