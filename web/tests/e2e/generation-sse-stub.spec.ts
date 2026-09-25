import { expect, test } from "@playwright/test";
import {
  installGate,
  installSSEStub,
  modelEgressWatcher,
  openApp,
  seedOutline,
  seedProject,
  sseClosed,
  sseCount,
  sseDispatch,
} from "./helpers";

/**
 * Generation through the EventSource stand-in.
 *
 * The e2e backend is model-less by construction (playwright.config.ts
 * neutralizes every provider key), and this suite replaces window.EventSource
 * so generation runs never open a real /generate connection. The stub
 * supports the full connection lifecycle — waiting, progress, done, error,
 * close — which is exactly what the round-5 connection-ownership fix needs
 * to prove: connections are closed by their owner and late events can
 * never touch another project's state, artifacts or generation flags.
 *
 * Every test also asserts (afterEach) that not a single real request
 * reached a model-bound endpoint (/generate, /refine).
 */

let egress: string[];

test.beforeEach(async ({ page }) => {
  egress = modelEgressWatcher(page);
  await installGate(page);
  await installSSEStub(page);
});

test.afterEach(async () => {
  expect(egress, "no real request may reach a model-bound endpoint").toEqual([]);
});

test("the e2e backend itself is model-less: /generate is refused without keys", async ({ request }) => {
  // Structural guarantee: even a machine whose root .env holds real keys
  // cannot arm this backend — an engine build fails fast instead of
  // egressing, so the whole suite is safe by default.
  const seeded = await seedProject(request, "无模型探针项目P");
  const res = await request.get(`http://127.0.0.1:8310/api/projects/${seeded.project_id}/generate`, {
    // Hard stop: a model-less backend answers instantly with 400; if some
    // misconfiguration ever armed it, aborting the request disconnects the
    // stream and the backend cancels the run.
    timeout: 5_000,
  });
  expect(res.status()).toBe(400);
  const body = await res.json();
  expect(body.detail.code).toBe("model_not_configured");
});

test("stubbed generation: progress + done complete the project without any model call", async ({ page }) => {
  await openApp(page, "/");
  const composer = page.getByPlaceholder("输入你的故事想法，按 Enter 开始生成...");
  await composer.fill("替身生成成功项目S");
  await page.locator("textarea ~ button").click();

  // Creation is real (persisted in the isolated backend); the generation
  // connection is the stub — waiting state, no network.
  await expect(page).toHaveURL(/project=/);
  expect(await sseCount(page)).toBe(1);
  await expect(page.getByText("正在生成...")).toBeVisible();

  expect(await sseDispatch(page, "/generate", "progress", { message: "正在提炼大纲…" })).toBe(true);
  await expect(page.getByText("正在提炼大纲…")).toBeVisible();

  expect(await sseDispatch(page, "/generate", "done", {})).toBe(true);
  await expect(page.getByText(/🎉 全部生成完成/)).toBeVisible();
  await expect(page.getByText("已完成", { exact: true })).toBeVisible();
  await expect(page.getByText("正在生成...")).toHaveCount(0);
});

test("stubbed generation: a failed run reports the error and frees the composer", async ({ page }) => {
  await openApp(page, "/");
  const composer = page.getByPlaceholder("输入你的故事想法，按 Enter 开始生成...");
  await composer.fill("替身失败项目F");
  await page.locator("textarea ~ button").click();
  await expect(page).toHaveURL(/project=/);
  expect(await sseCount(page)).toBe(1);

  expect(await sseDispatch(page, "/generate", "done", { error: "模拟生成失败" })).toBe(true);
  await expect(page.getByText("❌ 生成出错: 模拟生成失败")).toBeVisible();
  await expect(page.getByText("出错了", { exact: true })).toBeVisible();
  await expect(page.getByText("正在生成...")).toHaveCount(0);
});

test("stubbed generation: a dropped connection reports the error and frees the composer", async ({ page }) => {
  await openApp(page, "/");
  await page.getByPlaceholder("输入你的故事想法，按 Enter 开始生成...").fill("替身断连项目X");
  await page.locator("textarea ~ button").click();
  await expect(page).toHaveURL(/project=/);
  expect(await sseCount(page)).toBe(1);

  expect(await sseDispatch(page, "/generate", "error", {})).toBe(true);
  await expect(page.getByText(/⚠️ 连接中断或生成启动失败/)).toBeVisible();
  await expect(page.getByText("出错了", { exact: true })).toBeVisible();
  await expect(page.getByText("正在生成...")).toHaveCount(0);
});

test("switching projects mid-generation closes the connection; late events cannot touch B", async ({ page, request }) => {
  const b = await seedProject(request, "切换关闭目标项目B");
  await seedOutline(b.project_id, "B的大纲不动如山");

  await openApp(page, "/");
  await page.getByPlaceholder("输入你的故事想法，按 Enter 开始生成...").fill("切换关闭项目C");
  await page.locator("textarea ~ button").click();
  await expect(page).toHaveURL(/project=/);
  expect(await sseCount(page)).toBe(1);

  // Opening B must close C's connection — the page's closeStreamRef must
  // point at the one live subscription owner.
  await page.getByRole("button", { name: new RegExp(`^${b.title}`) }).click();
  await expect(page).toHaveURL(new RegExp(`project=${b.project_id}`));
  expect(await sseClosed(page, "/generate")).toBe(true);

  // After close() nothing can be delivered any more (real EventSource
  // semantics) — and even a forced attempt leaves B intact.
  expect(await sseDispatch(page, "/generate", "done", { error: "迟到的失败" })).toBe(false);
  await expect(page.getByText("B的大纲不动如山")).toBeVisible();
  await expect(page.getByText("已完成", { exact: true })).toBeVisible();
  await expect(page.getByText("出错了", { exact: true })).toHaveCount(0);
});
