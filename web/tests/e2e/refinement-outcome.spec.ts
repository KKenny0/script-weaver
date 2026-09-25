import { expect, test } from "@playwright/test";
import {
  gate,
  gateRespond,
  installGate,
  installSSEStub,
  modelEgressWatcher,
  openApp,
  release,
  seedExportableProject,
  seedOutline,
  seedProject,
  seedSecondRevision,
} from "./helpers";

/**
 * Regression for issue #22: the chat must report refine outcomes from the
 * backend's real diff — "已保存" only together with actual changes, refused
 * modifications as "未应用" with a retryable draft, and a saved-but-reload-
 * failed response that is clearly distinct from "未保存". Late responses
 * after a project switch must write nothing.
 *
 * All refine POSTs are answered in-page (gateRespond): the model-less e2e
 * backend must never receive one (see the egress watcher below).
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

const REFINE_OK_BODY = (projectId: string) => ({
  project_id: projectId,
  revision: 3,
  stage: "storyboarding",
  message: "Refinement applied",
  changed_artifacts: ["script"],
  total_changes: 1,
  changes: [
    {
      path: "script.scenes[0].blocks[1].content.dialogue",
      before: "灯不能灭。",
      after: "灯，不能灭。",
    },
  ],
  notice: "本次仅更新剧本，已有分镜和视频提示词未自动同步。",
});

test("success shows the real diff summary, not a bare 已应用", async ({
  page,
  request,
}) => {
  const p = await seedProject(request, "修改结果展示");
  await seedExportableProject(p.project_id, "长提示词占位");

  await openApp(page, `/?project=${p.project_id}`);
  await gateRespond(page, "POST /refine", 200, REFINE_OK_BODY(p.project_id));

  await page.getByPlaceholder("输入修改指令，按 Enter 发送...").fill("第一句对白加个顿号");
  await page.locator("textarea ~ button").click();
  await release(page, "POST /refine");

  // Only a 200 WITH actual changes may claim the modification was saved.
  await expect(page.getByText("修改已保存")).toBeVisible();
  await expect(
    page.getByText("script.scenes[0].blocks[1].content.dialogue"),
  ).toBeVisible();
  await expect(page.getByText("修改前: 灯不能灭。")).toBeVisible();
  await expect(page.getByText("修改后: 灯，不能灭。")).toBeVisible();
  // The script-only notice must be surfaced, not swallowed.
  await expect(page.getByText("未自动同步", { exact: false })).toBeVisible();
  // The submitted instruction was consumed on success.
  await expect(page.getByPlaceholder("输入修改指令，按 Enter 发送...")).toHaveValue("");
});

test("refused modification keeps the draft retryable and updates nothing", async ({
  page,
  request,
}) => {
  const p = await seedProject(request, "未应用保留草稿");
  await seedExportableProject(p.project_id, "长提示词占位");

  await openApp(page, `/?project=${p.project_id}`);
  await page.getByRole("button", { name: "分镜脚本" }).click();
  await expect(page.getByText("灯不能灭。", { exact: true })).toBeVisible();

  await gateRespond(page, "POST /refine", 422, {
    detail: {
      code: "refine_constraint_failed",
      message: "修改超出允许范围（仅最后一句对白可改）: script.notes",
    },
  });
  await page.getByPlaceholder("输入修改指令，按 Enter 发送...").fill("只改备注声称完成");
  await page.locator("textarea ~ button").click();
  await release(page, "POST /refine");

  await expect(page.getByText("未应用修改：修改超出允许范围（仅最后一句对白可改）")).toBeVisible();
  await expect(page.getByText("修改已保存")).toHaveCount(0);
  // The instruction stays editable for a retry instead of being consumed.
  await expect(page.getByPlaceholder("输入修改指令，按 Enter 发送...")).toHaveValue("只改备注声称完成");
  // Artifacts were not touched by a refused modification.
  await expect(page.getByText("灯不能灭。", { exact: true })).toBeVisible();
});

test("saved-but-reload-failure is distinct from 未保存", async ({ page, request }) => {
  const p = await seedProject(request, "保存后刷新失败");
  await seedExportableProject(p.project_id, "长提示词占位");

  await openApp(page, `/?project=${p.project_id}`);
  await gateRespond(page, "POST /refine", 200, REFINE_OK_BODY(p.project_id));
  // After the project has loaded, every further project read fails at the
  // network layer (page.route needs no release, so there is no race with
  // the moment React issues the reload).
  await page.route(`**/api/projects/${p.project_id}`, (route) =>
    route.fulfill({
      status: 500,
      contentType: "application/json",
      body: JSON.stringify({ detail: { code: "internal_error", message: "模拟读取失败" } }),
    }),
  );

  await page.getByPlaceholder("输入修改指令，按 Enter 发送...").fill("第一句对白加个顿号");
  await page.locator("textarea ~ button").click();
  await release(page, "POST /refine");

  await expect(page.getByText("修改已保存，但刷新项目显示失败")).toBeVisible();
  await expect(page.getByText("无需重新提交")).toBeVisible();
  await expect(page.getByText("❌ 修改失败")).toHaveCount(0);
});

test("revision conflict reloads latest content and asks to retry", async ({ page, request }) => {
  const p = await seedProject(request, "修改冲突提示");
  await seedExportableProject(p.project_id, "长提示词占位");

  await openApp(page, `/?project=${p.project_id}`);
  await gateRespond(page, "POST /refine", 409, {
    detail: {
      code: "revision_conflict",
      message: "项目内容在生成期间已被修改，生成结果未覆盖当前内容",
      current_revision: 3,
    },
  });

  await page.getByPlaceholder("输入修改指令，按 Enter 发送...").fill("会冲突的修改");
  await page.locator("textarea ~ button").click();
  await release(page, "POST /refine");

  await expect(page.getByText("已在其他窗口被修改", { exact: false })).toBeVisible();
  await expect(page.getByText("修改已保存")).toHaveCount(0);
  // The conflict path reloads the real latest state through the backend.
  await expect(page.getByRole("button", { name: "分镜脚本" })).toBeEnabled();
});

test("late refused response after a project switch writes nothing", async ({ page, request }) => {
  const a = await seedProject(request, "修改发起项目A");
  await seedExportableProject(a.project_id, "长提示词占位");
  const b = await seedProject(request, "迟到响应目标项目B");
  await seedSecondRevision(request, b.project_id, `${b.title}-r2`);
  await seedOutline(b.project_id, "B的专属大纲内容");

  await openApp(page, `/?project=${a.project_id}`);
  await expect(page.getByText(new RegExp(`已打开项目「${a.title}`))).toBeVisible();

  await gate(page, "POST /refine");
  await page.getByPlaceholder("输入修改指令，按 Enter 发送...").fill("对A的修改指令");
  await page.locator("textarea ~ button").click();
  await expect(page.getByText("正在应用修改...")).toBeVisible();

  // Switch to B while A's refine response is still held.
  await page.getByRole("button", { name: new RegExp(`^${b.title}`) }).click();
  await expect(page).toHaveURL(new RegExp(`project=${b.project_id}`));
  await expect(page.getByText("B的专属大纲内容")).toBeVisible();

  await gateRespond(page, "POST /refine", 422, {
    detail: { code: "refine_no_meaningful_change", message: "模型未对 script 产生实质修改" },
  });
  await release(page, "POST /refine");

  // The stale refusal belongs to A's dead session: B's chat and content stay clean.
  await expect(page.getByText("未应用修改")).toHaveCount(0);
  await expect(page.getByText("正在应用修改...")).toHaveCount(0);
  await expect(page.getByText("B的专属大纲内容")).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`project=${b.project_id}`));
});
