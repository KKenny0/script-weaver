import { expect, test, type Page } from "@playwright/test";

import {
  fetchProject,
  gate,
  gateRespond,
  installGate,
  modelEgressWatcher,
  openApp,
  release,
  seedCardsProject,
  seedProject,
  ungate,
  waitForHeld,
} from "./helpers";

/**
 * Ticket #16 browser regression: safe single-card editing.
 *
 * Runs against the model-less isolated backend (see playwright.config.ts):
 * every edit here is pure storage, and the egress watcher asserts no test
 * ever triggers a model call.
 */

let egress: string[];

test.beforeEach(({ page }) => {
  egress = modelEgressWatcher(page);
});

test.afterEach(async () => {
  expect(egress, "no model-bound request may leave the page").toEqual([]);
});

async function openCardTab(page: Page, tabLabel: string) {
  await page.locator(".segment-tab", { hasText: tabLabel }).click();
}

async function openCardProject(page: Page, projectId: string, tabLabel = "主角列表") {
  await openApp(page, `/?project=${projectId}`);
  await openCardTab(page, tabLabel);
  const locator =
    tabLabel === "主角列表"
      ? page.getByTestId("edit-card-characters-char_e2e_01")
      : tabLabel === "场景列表"
        ? page.getByTestId("edit-card-scenes-scene_e2e_01")
        : page.getByTestId("edit-card-shots-shot_e2e_01");
  await expect(locator).toBeVisible();
}

async function openEditor(page: Page, testId: string) {
  await page.getByTestId(testId).click();
  await expect(page.getByTestId("card-edit-drawer")).toBeVisible();
  await expect(page.getByTestId("drawer-basis")).toHaveText(/r2/);
}

/** Capture the PATCH save response (it carries the review metadata — the
 * GET project endpoint intentionally does not). */
function expectSaveResponse(page: Page) {
  return page.waitForResponse(
    (r) =>
      r.request().method() === "PATCH" && r.url().includes("/artifacts/"),
    { timeout: 10_000 },
  );
}

// ── Three card kinds: full save round-trips ───────────────


test("character card: edit fields, save, review banner, persisted", async ({ page, request }) => {
  const p = await seedProject(request, "角色编辑");
  await seedCardsProject(p.project_id);
  await openCardProject(page, p.project_id);

  await openEditor(page, "edit-card-characters-char_e2e_01");
  await page.getByTestId("field-name").fill("阿芸·改");
  await page.getByTestId("add-key_props").click();
  await page.getByTestId("field-key_props-1").fill("铜哨");
  const saveResponse = expectSaveResponse(page);
  await page.getByTestId("save-card").click();

  // Drawer closes, server snapshot is adopted, review banner appears.
  await expect(page.getByTestId("card-edit-drawer")).toHaveCount(0);
  await expect(page.getByTestId("review-notice")).toBeVisible();
  await expect(page.getByTestId("review-notice")).toContainText("待复核");
  await expect(page.locator(".character-card", { hasText: "阿芸·改" })).toBeVisible();

  const saveBody = await (await saveResponse).json();
  expect(saveBody.revision).toBe(3);
  expect(saveBody.changed).toBe(true);
  expect(saveBody.review.review_flags.map((f: any) => f.artifact).sort())
    .toEqual(["script", "storyboard", "visual_highlights"]);
  expect(saveBody.review.review_flags[0].upstream_id).toBe("char_e2e_01");
  expect(saveBody.review.review_flags[0].since_revision).toBe(3);

  const after = await fetchProject(request, p.project_id);
  expect(after.revision).toBe(3);
  const saved = after.characters.find((c: any) => c.id === "char_e2e_01");
  expect(saved.name).toBe("阿芸·改");
  expect(saved.key_props).toEqual(["马灯", "铜哨"]);

  // The notice is dismissible.
  await page.getByTestId("dismiss-review-notice").click();
  await expect(page.getByTestId("review-notice")).toHaveCount(0);
});


test("scene card: select and text fields save", async ({ page, request }) => {
  const p = await seedProject(request, "场景编辑");
  await seedCardsProject(p.project_id);
  await openCardProject(page, p.project_id, "场景列表");

  await openEditor(page, "edit-card-scenes-scene_e2e_01");
  await page.getByTestId("field-location_type").selectOption("exterior");
  await page.getByTestId("field-weather").fill("雨后初晴");
  await page.getByTestId("save-card").click();

  await expect(page.getByTestId("card-edit-drawer")).toHaveCount(0);
  await expect(page.locator(".scene-card", { hasText: "礁石滩" })).toBeVisible();

  const after = await fetchProject(request, p.project_id);
  const scene = after.scenes.find((s: any) => s.id === "scene_e2e_01");
  expect(scene.location_type).toBe("exterior");
  expect(scene.weather).toBe("雨后初晴");
  // Untouched sibling stays put.
  expect(after.scenes.map((s: any) => s.id)).toEqual(["scene_e2e_01", "scene_e2e_02"]);
});


test("shot card: duration validation, select, save, totals update", async ({ page, request }) => {
  const p = await seedProject(request, "镜头编辑");
  await seedCardsProject(p.project_id);
  await openCardProject(page, p.project_id, "影像亮点");

  await openEditor(page, "edit-card-shots-shot_e2e_01");

  // Zero duration is refused client-side, input kept, nothing saved.
  await page.getByTestId("field-duration_seconds").fill("0");
  await page.getByTestId("save-card").click();
  await expect(page.getByTestId("drawer-error")).toBeVisible();
  await expect(page.getByTestId("drawer-error")).toContainText("正数");

  await page.getByTestId("field-duration_seconds").fill("5");
  await page.getByTestId("field-transition_to_next").selectOption("dissolve");
  const saveResponse = expectSaveResponse(page);
  await page.getByTestId("save-card").click();
  await expect(page.getByTestId("card-edit-drawer")).toHaveCount(0);

  // Recomputed totals show up in the storyboard header (5 + 2 = 7).
  await expect(page.locator(".sb-header .badge").first()).toHaveText(/2 镜头 · ~7s/);

  const saveBody = await (await saveResponse).json();
  expect(saveBody.review.review_flags.map((f: any) => f.artifact))
    .toEqual(["visual_highlights"]);
  expect(saveBody.storyboard.total_estimated_duration).toBe(7);

  const after = await fetchProject(request, p.project_id);
  const shot = after.storyboard.shots.find((s: any) => s.shot_id === "shot_e2e_01");
  expect(shot.duration_seconds).toBe(5);
  expect(shot.transition_to_next).toBe("dissolve");
  expect(after.storyboard.total_estimated_duration).toBe(7);
});


test("shots beyond the 20th are editable (25-shot project)", async ({ page, request }) => {
  const p = await seedProject(request, "大量镜头");
  await seedCardsProject(p.project_id, 25);
  await openCardProject(page, p.project_id, "影像亮点");

  const shot21 = page.getByTestId("edit-card-shots-shot_e2e_21");
  await expect(shot21).toBeVisible();
  await shot21.scrollIntoViewIfNeeded();
  await shot21.click();
  await expect(page.getByTestId("card-edit-drawer")).toBeVisible();
  await page.getByTestId("field-duration_seconds").fill("5");
  await page.getByTestId("save-card").click();
  await expect(page.getByTestId("card-edit-drawer")).toHaveCount(0);

  // 24 × 2s + 5s = 53s across 25 shots.
  await expect(page.locator(".sb-header .badge").first()).toHaveText(/25 镜头 · ~53s/);

  const after = await fetchProject(request, p.project_id);
  const shot = after.storyboard.shots.find((s: any) => s.shot_id === "shot_e2e_21");
  expect(shot.duration_seconds).toBe(5);
  expect(after.storyboard.total_estimated_duration).toBe(53);
  expect(after.storyboard.total_shot_count).toBe(25);
});


// ── Failure, conflict, unsaved-draft guards ───────────────


test("failed save keeps the draft input and can retry", async ({ page, request }) => {
  const p = await seedProject(request, "保存失败");
  await seedCardsProject(p.project_id);
  await installGate(page);
  await openCardProject(page, p.project_id);

  await openEditor(page, "edit-card-characters-char_e2e_01");
  await page.getByTestId("field-name").fill("失败也不丢");
  await gate(page, "PATCH artifacts");
  await page.getByTestId("save-card").click();
  await waitForHeld(page, "PATCH artifacts");
  await gateRespond(page, "PATCH artifacts", 500, {
    detail: { code: "storage_error", message: "模拟存储故障" },
  });
  await release(page, "PATCH artifacts");

  await expect(page.getByTestId("drawer-error")).toBeVisible();
  await expect(page.getByTestId("drawer-error")).toContainText("模拟存储故障");
  // The draft survives the failure.
  await expect(page.getByTestId("field-name")).toHaveValue("失败也不丢");

  await ungate(page, "PATCH artifacts");
  await page.getByTestId("save-card").click();
  await expect(page.getByTestId("card-edit-drawer")).toHaveCount(0);
  const after = await fetchProject(request, p.project_id);
  expect(after.characters.find((c: any) => c.id === "char_e2e_01").name).toBe("失败也不丢");
});


test("two tabs: stale revision conflicts, inputs kept, reload latest works", async ({ browser, request }) => {
  const p = await seedProject(request, "双标签冲突");
  await seedCardsProject(p.project_id);

  const context = await browser.newContext();
  const pageA = await context.newPage();
  const pageB = await context.newPage();
  await openCardProject(pageA, p.project_id);
  await openCardProject(pageB, p.project_id);

  // Tab B saves first: the card moves to r3.
  await openEditor(pageB, "edit-card-characters-char_e2e_01");
  await pageB.getByTestId("field-name").fill("B 先保存");
  await pageB.getByTestId("save-card").click();
  await expect(pageB.getByTestId("card-edit-drawer")).toHaveCount(0);

  // Tab A still holds an r2 basis: its save must conflict, not overwrite.
  await openEditor(pageA, "edit-card-characters-char_e2e_01");
  await pageA.getByTestId("field-name").fill("A 的旧草稿");
  await pageA.getByTestId("save-card").click();

  await expect(pageA.getByTestId("drawer-conflict")).toBeVisible();
  await expect(pageA.getByTestId("field-name")).toHaveValue("A 的旧草稿");

  // Explicit, user-driven reload: drop the stale draft, edit the latest.
  await pageA.getByTestId("reload-latest").click();
  await expect(pageA.getByTestId("drawer-basis")).toHaveText(/r3/);
  await expect(pageA.getByTestId("field-name")).toHaveValue("B 先保存");
  await expect(pageA.getByTestId("drawer-conflict")).toHaveCount(0);

  // B's saved content was never overwritten.
  const after = await fetchProject(request, p.project_id);
  expect(after.characters.find((c: any) => c.id === "char_e2e_01").name).toBe("B 先保存");
  await context.close();
});


test("unsaved draft: escape asks, keep preserves input, discard closes", async ({ page, request }) => {
  const p = await seedProject(request, "未保存提示");
  await seedCardsProject(p.project_id);
  await openCardProject(page, p.project_id);

  await openEditor(page, "edit-card-characters-char_e2e_01");
  await page.getByTestId("field-name").fill("即将丢弃的草稿");
  await page.keyboard.press("Escape");
  await expect(page.getByTestId("discard-confirm")).toBeVisible();

  // 取消后保持原位置及输入。
  await page.getByTestId("confirm-keep").click();
  await expect(page.getByTestId("discard-confirm")).toHaveCount(0);
  await expect(page.getByTestId("card-edit-drawer")).toBeVisible();
  await expect(page.getByTestId("field-name")).toHaveValue("即将丢弃的草稿");

  await page.keyboard.press("Escape");
  await expect(page.getByTestId("discard-confirm")).toBeVisible();
  await page.getByTestId("confirm-discard").click();
  await expect(page.getByTestId("card-edit-drawer")).toHaveCount(0);
  // Nothing was saved.
  const after = await fetchProject(request, p.project_id);
  expect(after.revision).toBe(2);
});


test("native beforeunload guard fires while the draft is dirty", async ({ page, request }) => {
  const p = await seedProject(request, "离开确认");
  await seedCardsProject(p.project_id);
  await openCardProject(page, p.project_id);

  await openEditor(page, "edit-card-characters-char_e2e_01");
  await page.getByTestId("field-name").fill("刷新前会被拦下");

  let unloadDialog = "";
  page.on("dialog", async (d) => {
    unloadDialog = d.type();
    await d.accept();
  });
  await page.reload();
  expect(unloadDialog).toBe("beforeunload");
  // After the forced reload the (discarded) draft is gone.
  await expect(page.getByTestId("card-edit-drawer")).toHaveCount(0);
});


// ── History, keyboard, session isolation ──────────────────


test("history snapshots stay read-only", async ({ page, request }) => {
  const p = await seedProject(request, "历史只读");
  await seedCardsProject(p.project_id);
  await openCardProject(page, p.project_id);

  // Make a card edit so a second version exists, then view history.
  await openEditor(page, "edit-card-characters-char_e2e_01");
  await page.getByTestId("field-name").fill("阿芸·历史");
  await page.getByTestId("save-card").click();
  await expect(page.getByTestId("card-edit-drawer")).toHaveCount(0);

  await page.locator('button[title="查看版本历史"], button[aria-label="查看版本历史"]').click();
  await expect(page.getByTestId("version-history")).toBeVisible();
  await page.locator('button[aria-label="查看版本 r2"]').click();
  await expect(page.getByText("正在查看历史版本 r2")).toBeVisible();
  // The snapshot renders WITHOUT any edit entry.
  await expect(page.getByTestId("version-history").locator(".card-edit-btn")).toHaveCount(0);

  // Back to current: edit entries are available again.
  await page.locator('button:has-text("返回当前版本")').click();
  await expect(page.getByTestId("edit-card-characters-char_e2e_01")).toBeVisible();
});


test("keyboard: open via Enter, escape-guard, focus restored on close", async ({ page, request }) => {
  const p = await seedProject(request, "键盘操作");
  await seedCardsProject(p.project_id);
  await openCardProject(page, p.project_id);

  const editBtn = page.getByTestId("edit-card-characters-char_e2e_01");
  await editBtn.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByTestId("card-edit-drawer")).toBeVisible();

  await page.getByTestId("field-name").fill("键盘输入的草稿");
  await page.keyboard.press("Escape");
  await expect(page.getByTestId("discard-confirm")).toBeVisible();
  await page.getByTestId("confirm-discard").click();
  await expect(page.getByTestId("card-edit-drawer")).toHaveCount(0);

  // Focus returns to the card's edit entry after the drawer closes.
  await expect(editBtn).toBeFocused();
  // A clean (non-dirty) close skips the confirmation entirely.
  await editBtn.click();
  await expect(page.getByTestId("card-edit-drawer")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByTestId("discard-confirm")).toHaveCount(0);
  await expect(page.getByTestId("card-edit-drawer")).toHaveCount(0);
  await expect(editBtn).toBeFocused();
});


test("late save response from project A never touches project B", async ({ page, request }) => {
  const pa = await seedProject(request, "隔离甲");
  await seedCardsProject(pa.project_id);
  const pb = await seedProject(request, "隔离乙");
  await seedCardsProject(pb.project_id);

  await installGate(page);
  await openCardProject(page, pa.project_id);

  await openEditor(page, "edit-card-characters-char_e2e_01");
  await page.getByTestId("field-name").fill("甲项目的编辑");
  await gate(page, "PATCH artifacts");
  await page.getByTestId("save-card").click();
  await waitForHeld(page, "PATCH artifacts");

  // Leave the editor mid-save (discard confirm), then switch projects.
  await page.keyboard.press("Escape");
  await page.getByTestId("confirm-discard").click();
  await expect(page.getByTestId("card-edit-drawer")).toHaveCount(0);
  await page.locator('button[title^="打开项目"]', { hasText: pb.title }).click();
  await expect(page.locator('button[title^="打开项目"][aria-current="true"]')).toHaveText(new RegExp(pb.title));

  // openProject resets the tab — the panel shows project B's outline first.
  await openCardTab(page, "主角列表");

  // Now the stale PATCH lands: it must write nothing into B's session.
  await release(page, "PATCH artifacts");
  await page.waitForTimeout(300);
  await expect(page.getByTestId("review-notice")).toHaveCount(0);
  await expect(page.getByTestId("card-edit-drawer")).toHaveCount(0);
  // B's own card content is untouched by A's edit.
  await expect(page.locator(".character-card", { hasText: "阿芸" }).first()).toBeVisible();

  // The save itself did land server-side in project A (user-initiated).
  const afterA = await fetchProject(request, pa.project_id);
  expect(afterA.characters.find((c: any) => c.id === "char_e2e_01").name).toBe("甲项目的编辑");
  const afterB = await fetchProject(request, pb.project_id);
  expect(afterB.characters.find((c: any) => c.id === "char_e2e_01").name).toBe("阿芸");
});


test("a draft without real changes answers 没有需要保存的修改, no version born", async ({ page, request }) => {
  const p = await seedProject(request, "无变化保存");
  await seedCardsProject(p.project_id);
  await openCardProject(page, p.project_id);

  await openEditor(page, "edit-card-characters-char_e2e_01");
  // Open and save without touching anything: no PATCH may leave the page.
  let patchSeen = false;
  page.on("request", (req) => {
    if (req.method() === "PATCH" && req.url().includes("/artifacts/")) patchSeen = true;
  });
  await page.getByTestId("save-card").click();
  await expect(page.getByTestId("drawer-unchanged")).toBeVisible();
  await expect(page.getByTestId("drawer-unchanged")).toContainText("没有需要保存的修改");
  expect(patchSeen).toBe(false);

  // Reverting an edit also counts as unchanged.
  await page.getByTestId("field-name").fill("临时");
  await page.getByTestId("field-name").fill("阿芸");
  await page.getByTestId("save-card").click();
  await expect(page.getByTestId("drawer-unchanged")).toBeVisible();
  expect(patchSeen).toBe(false);
  await page.getByTestId("cancel-edit").click();
  await expect(page.getByTestId("card-edit-drawer")).toHaveCount(0);

  const after = await fetchProject(request, p.project_id);
  expect(after.revision).toBe(2);
});


// ── Theme & narrow viewport ───────────────────────────────


test("light theme and a narrow viewport complete an edit", async ({ page, request }) => {
  const p = await seedProject(request, "窄窗亮色");
  await seedCardsProject(p.project_id);
  // Playwright defaults to prefers-color-scheme: light; pin dark so the
  // exercise starts dark and the toggle then lands on light.
  await page.emulateMedia({ colorScheme: "dark" });
  await page.setViewportSize({ width: 900, height: 700 });
  await openCardProject(page, p.project_id);

  await page.locator('button[title="切换到亮色模式"]').click();
  expect(await page.evaluate(() => document.documentElement.dataset.theme)).toBe("light");

  await openEditor(page, "edit-card-characters-char_e2e_01");
  await expect(page.getByTestId("card-edit-drawer")).toBeVisible();
  await page.getByTestId("field-motivation").fill("让灯永远不灭");
  await page.getByTestId("save-card").click();
  await expect(page.getByTestId("card-edit-drawer")).toHaveCount(0);

  const after = await fetchProject(request, p.project_id);
  expect(after.characters.find((c: any) => c.id === "char_e2e_01").motivation).toBe("让灯永远不灭");
});
