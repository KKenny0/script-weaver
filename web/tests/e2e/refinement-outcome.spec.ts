import { expect, test, type Page } from "@playwright/test";
import { gateRespond, installGate, installSSEStub, modelEgressWatcher, openApp, release, seedCardsProject, seedProject, fetchProject, waitForHeld, seedGlobal } from "./helpers";

let egress: string[];
test.beforeEach(async ({ page }) => { egress = modelEgressWatcher(page); await installGate(page); await installSSEStub(page); });
test.afterEach(() => { expect(egress).toEqual([]); });
const input = (page: Page) => page.getByPlaceholder("输入修改指令，按 Enter 发送...");
const panel = (page: Page) => page.getByTestId("global-candidates");
async function show(page: Page) { await page.getByRole("button", { name: "全局修改候选", exact: true }).click(); }

test("submission is not adoption; complete differences, impacts, refresh and actual adoption", async ({ page, request }) => {
  const p = await seedProject(request, "全局候选确认"); await seedCardsProject(p.project_id);
  await openApp(page, `/?project=${p.project_id}`);
  const seeded = await seedGlobal(p.project_id);
  await gateRespond(page, "POST /refine", 200, { run: { run_id: seeded.run_id, kind: "global_candidate" }, created: true });
  await input(page).fill("让角色更坚定"); await page.locator("textarea ~ button").click();
  await release(page, "POST /refine");
  await expect(page.getByText("全局候选已提交", { exact: false })).toBeVisible();
  await expect(panel(page).getByText("characters[0].personality", { exact: true })).toBeVisible();
  await expect(panel(page).getByText("保守下游影响", { exact: false })).toContainText("剧本、分镜、视觉亮点");
  expect((await fetchProject(request, p.project_id)).revision).toBe(2);
  await page.reload(); await show(page);
  await expect(panel(page).getByText("待采用 · 依据 r2")).toBeVisible();
  await panel(page).getByRole("button", { name: "采用候选", exact: true }).click();
  await expect(page.getByText("已采用全局候选", { exact: false })).toBeVisible();
  expect((await fetchProject(request, p.project_id)).revision).toBe(3);
  await expect(panel(page).getByText("已采用 · 依据 r2 → r3")).toBeVisible();
});

test("reject persists without revision, and stale candidates cannot adopt", async ({ page, request }) => {
  const p = await seedProject(request, "全局放弃"); await seedCardsProject(p.project_id); await seedGlobal(p.project_id);
  await openApp(page, `/?project=${p.project_id}`); await show(page);
  await panel(page).getByRole("button", { name: "放弃候选" }).click();
  await expect(panel(page).getByText("已放弃 · 依据 r2")).toBeVisible();
  expect((await fetchProject(request, p.project_id)).revision).toBe(2);
  await seedGlobal(p.project_id);
  await request.patch(`/api/projects/${p.project_id}/artifacts/characters/char_e2e_01`, { data: { expected_revision: 2, changes: { name: "手工优先" } } });
  await page.reload(); await show(page);
  await expect(panel(page).getByText(/已过期/)).toBeVisible();
  await expect(panel(page).getByRole("button", { name: "采用候选" })).toHaveCount(0);
});

test("admission refusal keeps draft; revision conflict refreshes displayed basis", async ({ page, request }) => {
  const p = await seedProject(request, "全局冲突"); await seedCardsProject(p.project_id);
  await openApp(page, `/?project=${p.project_id}`);
  await gateRespond(page, "POST /refine", 409, { detail: { code: "revision_conflict", message: "项目已变化" } });
  await input(page).fill("修改草稿"); await page.locator("textarea ~ button").click(); await release(page, "POST /refine");
  await expect(page.getByText("已载入最新内容", { exact: false })).toBeVisible();
  await expect(input(page)).toHaveValue("修改草稿");
  await expect(page.getByText("修改已保存", { exact: false })).toHaveCount(0);
});

for (const lateStatus of [200, 422]) test(`late ${lateStatus} submission after A B A or collapse cannot alter new chat`, async ({ page, request }) => {
  const a = await seedProject(request, "全局迟到甲"), b = await seedProject(request, "全局迟到乙");
  await seedCardsProject(a.project_id); await seedCardsProject(b.project_id);
  await openApp(page, `/?project=${a.project_id}`);
  await gateRespond(page, "POST /refine", lateStatus, lateStatus === 200 ? { run: { run_id: "held" }, created: true } : { detail: { message: "旧错误" } });
  await input(page).fill("旧修改"); await page.locator("textarea ~ button").click(); await waitForHeld(page, "POST /refine");
  await page.locator("button:has(.lucide-panel-left-close)").first().click();
  await page.getByRole("button", { name: new RegExp(`^${b.title}`) }).click();
  await page.getByRole("button", { name: new RegExp(`^${a.title}`) }).click();
  await page.getByRole("button", { name: "展开对话面板" }).click();
  await input(page).fill("新草稿"); await release(page, "POST /refine");
  await expect(input(page)).toHaveValue("新草稿");
  await expect(page.getByText("全局候选已提交", { exact: false })).toHaveCount(0);
  await expect(page.getByText("旧错误", { exact: false })).toHaveCount(0);
});

async function holdAdoptionBody(page: Page) {
  await page.evaluate(() => {
    const original = window.fetch;
    window.fetch = async (...args) => {
      const response = await original(...args);
      if (String(args[0]).endsWith("/accept")) {
        const json = response.json.bind(response);
        response.json = async () => {
          const body = await json(); (window as any).__heldGlobal = true;
          await new Promise<void>(resolve => { (window as any).__releaseGlobal = resolve; });
          return body;
        };
      }
      return response;
    };
  });
}

test("late adoption body does not discard a newly opened card draft", async ({ page, request }) => {
  const p = await seedProject(request, "全局保护草稿"); await seedCardsProject(p.project_id); await seedGlobal(p.project_id);
  await openApp(page, `/?project=${p.project_id}`); await show(page); await holdAdoptionBody(page);
  await panel(page).getByRole("button", { name: "采用候选" }).click(); await page.waitForFunction(() => (window as any).__heldGlobal);
  await expect(panel(page).getByRole("button", { name: "载入最新内容后重新发起" })).toBeDisabled();
  await page.locator(".segment-tab", { hasText: "主角列表" }).click();
  await page.getByTestId("edit-card-characters-char_e2e_01").click();
  await page.getByTestId("field-name").fill("未保存的新草稿");
  await page.evaluate(() => (window as any).__releaseGlobal());
  await expect(page.getByTestId("field-name")).toHaveValue("未保存的新草稿");
  await expect(page.getByTestId("card-edit-drawer")).toBeVisible();
});

test("closed and reopened global panel ignores late adoption body", async ({ page, request }) => {
  const p = await seedProject(request, "全局实例保护"); await seedCardsProject(p.project_id); await seedGlobal(p.project_id);
  await openApp(page, `/?project=${p.project_id}`); await show(page); await holdAdoptionBody(page);
  await panel(page).getByRole("button", { name: "采用候选" }).click(); await page.waitForFunction(() => (window as any).__heldGlobal);
  await expect(panel(page).getByRole("button", { name: "载入最新内容后重新发起" })).toBeDisabled();
  await show(page); await show(page);
  await page.evaluate(() => (window as any).__releaseGlobal());
  await expect(panel(page).getByText("已采用 · 依据 r2 → r3")).toBeVisible();
  await expect(page.getByText("已采用全局候选", { exact: false })).toHaveCount(0);
});


test("narrow light layout compares and rejects with keyboard", async ({ page, request }) => {
  const p = await seedProject(request, "全局窄窗"); await seedCardsProject(p.project_id); await seedGlobal(p.project_id);
  await page.emulateMedia({ colorScheme: "dark" }); await page.setViewportSize({ width: 900, height: 700 });
  await openApp(page, `/?project=${p.project_id}`);
  await page.locator('button[title="切换到亮色模式"]').click();
  await show(page);
  await expect(panel(page).getByText("characters[0].personality", { exact: true })).toBeVisible();
  const reject = panel(page).getByRole("button", { name: "放弃候选" });
  await reject.focus(); await page.keyboard.press("Enter");
  await expect(panel(page).getByText("已放弃 · 依据 r2")).toBeVisible();
});
