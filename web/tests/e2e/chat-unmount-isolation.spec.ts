import { expect, test } from "@playwright/test";
import {
  gate,
  gateRespond,
  installGate,
  openApp,
  releaseAndSettle,
  seedOutline,
  release,
  seedProject,
  seedSecondRevision,
} from "./helpers";

/**
 * Regression: collapsing the chat panel unmounts ChatPanel, whose own
 * session refs stopped receiving updates — a creation/refine response that
 * landed after the user moved on would hijack the project, URL and status.
 *
 * Guards now check the parent-owned liveness predicates, which survive
 * unmounts. The first three tests FAIL on the pre-fix behaviour (53cb887);
 * the last two guard the legitimate flows that must keep working.
 */

test.beforeEach(async ({ page }) => {
  await installGate(page);
});

test("collapse + switch: late creation response must not steal the session", async ({ page, request }) => {
  const b = await seedProject(request, "折叠后打开的项目B");
  await seedSecondRevision(request, b.project_id, `${b.title}-r2`);

  const generateRequests: string[] = [];
  page.on("request", (req) => {
    if (req.url().includes("/generate")) generateRequests.push(req.url());
  });

  await openApp(page, "/");
  await expect(page.getByPlaceholder("输入你的故事想法，按 Enter 开始生成...")).toBeVisible();

  await gate(page, "POST /api/projects");
  await page.getByPlaceholder("输入你的故事想法，按 Enter 开始生成...").fill("被折叠的项目C");
  await page.locator("textarea ~ button").click();

  // Collapse the chat while the creation request is held.
  await page.locator("button:has(.lucide-panel-left-close)").first().click();
  await expect(page.getByRole("button", { name: "展开对话面板" })).toBeVisible();

  // Open B while the chat is collapsed.
  await page.getByRole("button", { name: new RegExp(`^${b.title}`) }).click();
  await expect(page).toHaveURL(new RegExp(`project=${b.project_id}`));

  await releaseAndSettle(page, "POST /api/projects", "/api/projects");

  // C's late response must not reclaim the project/URL nor start its SSE.
  await expect(page).toHaveURL(new RegExp(`project=${b.project_id}`));
  await expect(generateRequests).toEqual([]);

  // Re-expanding shows B's session, intact.
  await page.getByRole("button", { name: "展开对话面板" }).click();
  await expect(page.getByText(new RegExp(`已打开项目「${b.title}-r2」`))).toBeVisible();
});

test("collapse + switch: late creation FAILURE must not disturb B", async ({ page, request }) => {
  const b = await seedProject(request, "迟到失败目标项目B");
  await seedSecondRevision(request, b.project_id, `${b.title}-r2`);

  await openApp(page, "/");
  await expect(page.getByPlaceholder("输入你的故事想法，按 Enter 开始生成...")).toBeVisible();

  await gateRespond(page, "POST /api/projects", 500, {
    detail: { code: "internal_error", message: "模拟创建失败" },
  });
  await page.getByPlaceholder("输入你的故事想法，按 Enter 开始生成...").fill("会迟到失败的项目C");
  await page.locator("textarea ~ button").click();

  await page.locator("button:has(.lucide-panel-left-close)").first().click();
  await page.getByRole("button", { name: new RegExp(`^${b.title}`) }).click();
  await expect(page).toHaveURL(new RegExp(`project=${b.project_id}`));

  await releaseAndSettle(page, "POST /api/projects", "/api/projects");

  await expect(page).toHaveURL(new RegExp(`project=${b.project_id}`));
  // The stale failure must not flip B's status badge to error.
  await expect(page.getByText("出错了", { exact: true })).toHaveCount(0);

  await page.getByRole("button", { name: "展开对话面板" }).click();
  await expect(page.getByText(new RegExp(`已打开项目「${b.title}-r2」`))).toBeVisible();
  await expect(page.getByText("❌ 错误")).toHaveCount(0);
});

test("collapse + switch: late refine response must not overwrite B's content", async ({ page, request }) => {
  const a = await seedProject(request, "修改中折叠项目A");
  await seedSecondRevision(request, a.project_id, `${a.title}-r2`);
  const b = await seedProject(request, "内容保护目标项目B");
  await seedSecondRevision(request, b.project_id, `${b.title}-r2`);
  await seedOutline(b.project_id, "B的专属大纲内容");

  await openApp(page, `/?project=${a.project_id}`);
  await expect(page.getByText(new RegExp(`已打开项目「${a.title}-r2`))).toBeVisible();

  // Fake a successful refine response (no model key in the e2e backend).
  await gateRespond(page, "POST /refine", 200, {
    project_id: a.project_id, revision: 3, stage: "structured", message: "ok",
  });
  await page.getByPlaceholder("输入修改指令，按 Enter 发送...").fill("对A的修改指令");
  await page.locator("textarea ~ button").click();

  await page.locator("button:has(.lucide-panel-left-close)").first().click();
  await page.getByRole("button", { name: new RegExp(`^${b.title}`) }).click();
  await expect(page).toHaveURL(new RegExp(`project=${b.project_id}`));
  // B's seeded outline is visible in the artifact panel (chat collapsed).
  await expect(page.getByText("B的专属大纲内容")).toBeVisible();

  await releaseAndSettle(page, "POST /refine", "/refine");

  // A's late refine success must not repaint B's artifact panel with A's
  // (empty) content.
  await expect(page.getByText("B的专属大纲内容")).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`project=${b.project_id}`));
});

test("collapse and re-expand in the SAME session continues normally", async ({ page, request }) => {
  await openApp(page, "/");
  await expect(page.getByPlaceholder("输入你的故事想法，按 Enter 开始生成...")).toBeVisible();

  await gate(page, "POST /api/projects");
  await page.getByPlaceholder("输入你的故事想法，按 Enter 开始生成...").fill("同会话折叠再展开D");
  await page.locator("textarea ~ button").click();

  await page.locator("button:has(.lucide-panel-left-close)").first().click();
  await page.getByRole("button", { name: "展开对话面板" }).click();

  await releaseAndSettle(page, "POST /api/projects", "/api/projects");

  // No session change happened: the creation must be adopted as usual.
  await expect(page).toHaveURL(/project=/);
  await expect(page.getByPlaceholder("输入修改指令，按 Enter 发送...")).toBeVisible();
});

test("draft typed during a pending creation is preserved (kept regression)", async ({ page }) => {
  await openApp(page, "/");
  await expect(page.getByPlaceholder("输入你的故事想法，按 Enter 开始生成...")).toBeVisible();

  await gate(page, "POST /api/projects");
  const composer = page.getByPlaceholder("输入你的故事想法，按 Enter 开始生成...");
  await composer.fill("原始想法E");
  await page.locator("textarea ~ button").click();

  // Type a new draft while creation is pending.
  await composer.fill("等待期间的新草稿E2");

  await releaseAndSettle(page, "POST /api/projects", "/api/projects");

  await expect(page).toHaveURL(/project=/);
  // The new draft survives; the submitted idea was consumed into the project.
  await expect(page.getByPlaceholder("输入修改指令，按 Enter 发送...")).toHaveValue("等待期间的新草稿E2");
});
