import { expect, test } from "@playwright/test";
import {
  gate,
  gateRespond,
  installGate,
  installSSEStub,
  modelEgressWatcher,
  openApp,
  release,
  seedProject,
  seedRun,
  sseFind,
  waitForHeld,
} from "./helpers";

/**
 * Ticket #15: the chat offers an explicit resume entry for a terminal
 * unfinished run. The offer states what completed and where a resume
 * continues from; clicking it asks for confirmation (replace current
 * artifacts, keep history), then POSTs /runs/{id}/resume and subscribes to
 * the new run. A server refusal shows its specific reason while 从头生成
 * stays available. All POSTs are gated/stubbed in-page: no test can ever
 * create a real backend run or reach a model.
 */

let egress: string[];

test.beforeEach(async ({ page }) => {
  egress = modelEgressWatcher(page);
  await installGate(page);
  await installSSEStub(page);
  // Auto-accept the resume confirmation for every test that reaches it.
  page.on("dialog", (dialog) => dialog.accept());
});

test.afterEach(async () => {
  expect(egress, "no real request may reach a model-bound endpoint").toEqual([]);
});

test("failed restore offers resume with stage facts; confirm submits and subscribes", async ({ page, request }) => {
  const p = await seedProject(request, "恢复入口故事");
  await seedRun(p.project_id, "failed", {
    message: "分镜阶段失败",
    completedSteps: ["idea_refiner", "structurer"],
  });

  await openApp(page, `/?project=${p.project_id}`);
  // The restore message reports the surviving stages and the continuation
  // point before any button is touched.
  await expect(page.getByText(/上次生成未完成/)).toBeVisible();
  await expect(page.getByText(/已完成阶段：概念精炼、故事大纲/)).toBeVisible();
  await expect(page.getByText(/恢复将从「角色设计」继续/)).toBeVisible();

  const resumeBtn = page.getByRole("button", { name: "从中断处继续生成" });
  await expect(resumeBtn).toBeVisible();
  await expect(page.getByRole("button", { name: "从头生成" })).toBeVisible();

  // Stub the resume POST: a new run the page must subscribe to.
  await gateRespond(page, "POST /resume", 200, {
    run: { run_id: "runresumed01", status: "running", completed_steps: ["idea_refiner", "structurer"] },
    created: true,
  });
  await resumeBtn.click();
  await waitForHeld(page, "POST /resume");
  await release(page, "POST /resume");

  await expect(page.getByText(/恢复已提交/)).toBeVisible();
  // The observation moved onto the resumed run's event stream.
  expect(await sseFind(page, "/runs/runresumed01/events")).toBeGreaterThanOrEqual(0);
  // The offer row is gone while the run is live.
  await expect(resumeBtn).toHaveCount(0);
});

test("cancelled confirmation never submits a resume", async ({ page, request }) => {
  const p = await seedProject(request, "取消确认故事");
  await seedRun(p.project_id, "failed", { completedSteps: ["idea_refiner"] });

  await openApp(page, `/?project=${p.project_id}`);
  await gate(page, "POST /resume");
  // This test dismisses the confirm dialog instead of accepting it.
  page.removeAllListeners("dialog");
  page.on("dialog", (dialog) => dialog.dismiss());

  await page.getByRole("button", { name: "从中断处继续生成" }).click();
  await page.waitForTimeout(300);
  // No request was held: the click never left the confirmation.
  expect(await page.evaluate(() => (window as any).__gatePending.get("POST /resume")?.length ?? 0)).toBe(0);
  await expect(page.getByRole("button", { name: "从中断处继续生成" })).toBeVisible();
});

test("server refusal shows the specific reason and keeps 从头生成", async ({ page, request }) => {
  const p = await seedProject(request, "拒绝原因故事");
  await seedRun(p.project_id, "interrupted", { completedSteps: ["idea_refiner"] });

  await openApp(page, `/?project=${p.project_id}`);
  await expect(page.getByText(/服务在生成期间重启/).or(page.getByText(/运行已中断/)).first()).toBeVisible();

  await gateRespond(page, "POST /resume", 409, {
    detail: { code: "config_changed", message: "执行依据与 checkpoint 不一致，已拒绝恢复（未调用模型）" },
  });
  await page.getByRole("button", { name: "从中断处继续生成" }).click();
  await waitForHeld(page, "POST /resume");
  await release(page, "POST /resume");

  await expect(page.getByText(/无法恢复：执行依据与 checkpoint 不一致/)).toBeVisible();
  // The explicit fallback remains available.
  const fresh = page.getByRole("button", { name: "从头生成" });
  await expect(fresh).toBeVisible();

  // 从头生成 submits a full run for THIS project and subscribes to it.
  await gateRespond(page, "POST /generate", 200, {
    run: { run_id: "runfresh00001", status: "running", completed_steps: [] },
    created: true,
  });
  await fresh.click();
  await waitForHeld(page, "POST /generate");
  await release(page, "POST /generate");
  await expect(page.getByText(/从头生成已提交/)).toBeVisible();
  expect(await sseFind(page, "/runs/runfresh00001/events")).toBeGreaterThanOrEqual(0);
});

test("content-complete and running runs never show the resume entry", async ({ page, request }) => {
  const done = await seedProject(request, "已完成项目");
  await seedRun(done.project_id, "succeeded", {
    completedSteps: ["idea_refiner", "structurer", "character_designer", "scene_designer",
                     "art_director", "scriptwriter", "storyboard_artist", "finalize"],
  });
  await openApp(page, `/?project=${done.project_id}`);
  await expect(page.getByText(/已恢复上次生成的结果/)).toBeVisible();
  await expect(page.getByRole("button", { name: "从中断处继续生成" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "从头生成" })).toHaveCount(0);

  const live = await seedProject(request, "进行中项目");
  await seedRun(live.project_id, "running");
  await openApp(page, `/?project=${live.project_id}`);
  await expect(page.getByText(/正在后台生成/)).toBeVisible();
  await expect(page.getByRole("button", { name: "从中断处继续生成" })).toHaveCount(0);
});

test("A→B→A round trip: the offer follows the project, not the page", async ({ page, request }) => {
  const a = await seedProject(request, "往返项目A");
  await seedRun(a.project_id, "failed", { completedSteps: ["idea_refiner"] });
  const b = await seedProject(request, "往返项目B");

  await openApp(page, `/?project=${a.project_id}`);
  await expect(page.getByRole("button", { name: "从中断处继续生成" })).toBeVisible();

  await page.getByRole("button", { name: new RegExp(`^${b.title}`) }).click();
  await expect(page.getByText(/已打开项目/)).toBeVisible();
  // B has no failed run: no resume entry leaked across the switch.
  await expect(page.getByRole("button", { name: "从中断处继续生成" })).toHaveCount(0);

  await page.getByRole("button", { name: new RegExp(`^${a.title}`) }).click();
  // Back on A the offer returns — without re-announcing the old failure
  // (the fresh session holds only the open notice).
  await expect(page.getByRole("button", { name: "从中断处继续生成" })).toBeVisible();
  await expect(page.getByText(/上次生成未完成/)).toHaveCount(0);
});
