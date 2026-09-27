import { expect, test, type Page } from "@playwright/test";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

import {
  fetchProject,
  gate,
  gateRespond,
  inspectZip,
  installGate,
  installSSEStub,
  modelEgressWatcher,
  openApp,
  release,
  seedCardsProject,
  seedExportableProject,
  seedProject,
  releaseAll,
  ungate,
  waitForHeld,
} from "./helpers";

/**
 * Ticket #17 browser regression: confirm downstream content with a stated
 * scope, and exports that warn before downloading an unreviewed snapshot.
 *
 * Runs against the model-less isolated backend: every confirm here is pure
 * storage (no model call, no artifact rewrite), and the egress watcher
 * asserts no test ever triggers a model call.
 */

const API_BASE = "http://127.0.0.1:8310";
const LONG_PROMPT = "夜色中的灯塔守望者" + "，海风呼啸而过".repeat(30);

let egress: string[];

test.beforeEach(({ page }) => {
  egress = modelEgressWatcher(page);
});

test.afterEach(async () => {
  expect(egress, "no model-bound request may leave the page").toEqual([]);
});

/** A cards project whose manual character edit flagged all three artifacts
 * (r2 cards seed → r3 edit → flags since r3). */
async function seedFlaggedProject(request: any, title: string) {
  const p = await seedProject(request, title);
  await seedCardsProject(p.project_id);
  const r = await request.patch(
    `${API_BASE}/api/projects/${p.project_id}/artifacts/characters/char_e2e_01`,
    { data: { expected_revision: 2, changes: { name: "阿芸·改" } } },
  );
  expect(r.ok()).toBe(true);
  return p;
}

async function openReviewPanel(page: Page) {
  await expect(page.getByTestId("review-notice")).toBeVisible();
  await page.getByTestId("open-review-panel").click();
  await expect(page.getByTestId("review-panel")).toBeVisible();
  await expect(page.getByTestId("review-basis")).toContainText("依据 r3");
  await expect(page.getByTestId("review-count")).toHaveText("3");
}

function countDownloads(page: Page): () => number {
  let downloads = 0;
  page.on("download", () => downloads++);
  return () => downloads;
}

// ── Review panel: reasons, partial confirm, persistence ────


test("shows reasons, confirms a partial scope, keeps the rest", async ({ page, request }) => {
  const p = await seedFlaggedProject(request, "部分确认");
  await openApp(page, `/?project=${p.project_id}`);
  await openReviewPanel(page);

  // Every flag states its reason, the recorded upstream object and its own
  // since_revision — plus the conservative-scope disclaimer.
  await expect(page.getByTestId("review-flag-0")).toContainText("剧本");
  await expect(page.getByTestId("review-flag-0")).toContainText("上游内容被手工修改");
  await expect(page.getByTestId("review-flag-0")).toContainText("角色「阿芸·改」");
  await expect(page.getByTestId("review-flag-0")).toContainText("自 r3 起");
  await expect(page.getByTestId("review-panel")).toContainText("保守影响范围");

  // Partial scope: confirm script + storyboard, keep the highlight flag.
  await page.getByTestId("review-select-2").uncheck();
  await expect(page.getByTestId("review-selected-count")).toHaveText("已选 2/3 项");

  const confirmRequest = page.waitForRequest(
    (r) => r.url().includes("/review/confirm") && r.method() === "POST",
  );
  await page.getByTestId("confirm-review-keep").click();
  const req = await confirmRequest;
  const body = req.postDataJSON();
  expect(body.expected_revision).toBe(3);
  expect(body.selections.map((s: any) => s.artifact).sort())
    .toEqual(["script", "storyboard"]);
  expect(body.selections[0].since_revision).toBe(3);
  // Ownership rides every selection (review round 2).
  expect(body.selections.every((s: any) => s.project_id === p.project_id)).toBe(true);

  await expect(page.getByTestId("review-panel")).toHaveCount(0);
  // The chat session acknowledges without resetting; the banner narrows to
  // the remaining scope.
  await expect(page.getByTestId("review-notice")).toBeVisible();
  await expect(page.getByTestId("review-notice")).toContainText("影像亮点");
  await expect(page.getByTestId("review-notice")).not.toContainText("剧本");

  const after = await fetchProject(request, p.project_id);
  expect(after.revision).toBe(4);
  expect(after.review.review_flags.map((f: any) => f.artifact))
    .toEqual(["visual_highlights"]);
  // Artifacts were never rewritten.
  expect(after.script.scenes).toHaveLength(1);
  expect(after.characters.find((c: any) => c.id === "char_e2e_01").name).toBe("阿芸·改");
  const versions = (
    await (await request.get(`${API_BASE}/api/projects/${p.project_id}/versions`)).json()
  ).versions;
  expect(versions[0].summary).toContain("确认沿用");
  expect(versions).toHaveLength(4);
});


test("confirming everything clears the banner for good", async ({ page, request }) => {
  const p = await seedFlaggedProject(request, "全部确认");
  await openApp(page, `/?project=${p.project_id}`);
  await openReviewPanel(page);

  await page.getByTestId("confirm-review-keep").click();
  await expect(page.getByTestId("review-panel")).toHaveCount(0);
  await expect(page.getByTestId("review-notice")).toHaveCount(0);

  // A refresh restores the facts from data: nothing pending, nothing shown.
  await page.reload();
  await page.locator(".segment-tab", { hasText: "主角列表" }).click();
  await expect(page.locator(".character-card", { hasText: "阿芸·改" }).first()).toBeVisible();
  await expect(page.getByTestId("review-notice")).toHaveCount(0);
  expect((await fetchProject(request, p.project_id)).review.review_flags).toEqual([]);
});


test("stale confirm conflicts, re-read re-bases, then it lands", async ({ page, request }) => {
  const p = await seedFlaggedProject(request, "过期确认");
  await openApp(page, `/?project=${p.project_id}`);
  await openReviewPanel(page);

  // The upstream edits AGAIN while the panel is open: r4 refreshes the same
  // flags with a new since_revision.
  const second = await request.patch(
    `${API_BASE}/api/projects/${p.project_id}/artifacts/characters/char_e2e_02`,
    { data: { expected_revision: 3, changes: { name: "陈叔·改" } } },
  );
  expect(second.ok()).toBe(true);

  // The panel's r3 basis is now stale: 409, nothing cleared, selection kept.
  await page.getByTestId("confirm-review-keep").click();
  await expect(page.getByTestId("review-conflict")).toBeVisible();
  await expect(page.getByTestId("review-conflict")).toContainText("重新读取");

  // Explicit re-read re-bases the panel on the current flags (all selected
  // again) — never an automatic retry.
  await page.getByTestId("review-reload").click();
  await expect(page.getByTestId("review-conflict")).toHaveCount(0);
  await expect(page.getByTestId("review-basis")).toContainText("依据 r4");
  await expect(page.getByTestId("review-count")).toHaveText("3");

  await page.getByTestId("confirm-review-keep").click();
  await expect(page.getByTestId("review-panel")).toHaveCount(0);
  await expect(page.getByTestId("review-notice")).toHaveCount(0);

  const after = await fetchProject(request, p.project_id);
  expect(after.revision).toBe(5);
  expect(after.review.review_flags).toEqual([]);
  // The second edit's flag would have been wiped by a name-only clear —
  // it survived until the user confirmed against r4 instead.
  expect(after.characters.find((c: any) => c.id === "char_e2e_02").name).toBe("陈叔·改");
});


test("查看 jumps to the artifact tab; the banner offers re-entry", async ({ page, request }) => {
  const p = await seedFlaggedProject(request, "查看产物");
  await openApp(page, `/?project=${p.project_id}`);
  await openReviewPanel(page);

  await page.getByTestId("review-view-visual_highlights").click();
  await expect(page.getByTestId("review-panel")).toHaveCount(0);
  await expect(page.locator(".segment-tab.active")).toHaveText(/影像亮点/);
  // The unhandled scope is still visible and re-openable.
  await expect(page.getByTestId("review-notice")).toBeVisible();
  await page.getByTestId("open-review-panel").click();
  await expect(page.getByTestId("review-panel")).toBeVisible();
  await expect(page.getByTestId("review-selected-count")).toHaveText("已选 3/3 项");
  await page.getByTestId("review-cancel").click();
  await expect(page.getByTestId("review-panel")).toHaveCount(0);
});


// ── Warned exports: confirm or cancel before the download ──


test("warned export: cancel downloads nothing, 仍然导出 saves the shown snapshot", async ({
  page,
  request,
}) => {
  const p = await seedFlaggedProject(request, "警告导出");
  // The exportable content lands on TOP of the flagged project (r4);
  // save_state preserves the review metadata, so the flags stay pending.
  await seedExportableProject(p.project_id, LONG_PROMPT);

  await openApp(page, `/?project=${p.project_id}`);
  await expect(page.getByRole("button", { name: "导出 JSON" })).toBeEnabled();
  const downloads = countDownloads(page);

  // First click: the response carries warnings → dialog, no download yet.
  await page.getByRole("button", { name: "导出 JSON" }).click();
  await expect(page.getByTestId("export-confirm")).toBeVisible();
  await expect(page.getByTestId("export-confirm-scope")).toContainText("即将导出 r4");
  await expect(page.getByTestId("export-confirm-list")).toContainText("剧本");
  await expect(page.getByTestId("export-confirm-list")).toContainText("上游内容被手工修改");
  expect(downloads()).toBe(0);

  // Cancel: the already-downloaded blob is discarded without saving a file.
  await page.getByTestId("export-confirm-cancel").click();
  await expect(page.getByTestId("export-confirm")).toHaveCount(0);
  await page.waitForTimeout(400);
  expect(downloads()).toBe(0);

  // Second click: confirming downloads THAT response's blob.
  await page.getByRole("button", { name: "导出 JSON" }).click();
  await expect(page.getByTestId("export-confirm")).toBeVisible();
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page.getByTestId("export-confirm-yes").click(),
  ]);
  expect(download.suggestedFilename()).toBe(`${p.project_id}.json`);
  const dir = await mkdtemp(path.join(tmpdir(), "sw-review-export-"));
  try {
    const file = path.join(dir, download.suggestedFilename());
    await download.saveAs(file);
    const project = JSON.parse(await readFile(file, "utf-8"));
    expect(project.meta.id).toBe(p.project_id);
    expect(project.script.title).toBe("夜行灯塔");
    // The file is the real ProjectState object — warnings never leaked in.
    expect("review_warnings" in project).toBe(false);
  } finally {
    await rm(dir, { recursive: true, force: true });
  }

  // Exporting is read-only: the flags are still pending afterwards.
  const after = await fetchProject(request, p.project_id);
  expect(after.review.review_flags).toHaveLength(3);
  expect(after.revision).toBe(4);
});


test("all three formats warn; fountain and ZIP stay tool-readable", async ({
  page,
  request,
}) => {
  const p = await seedFlaggedProject(request, "三格式警告");
  await seedExportableProject(p.project_id, LONG_PROMPT);
  await openApp(page, `/?project=${p.project_id}`);

  const dir = await mkdtemp(path.join(tmpdir(), "sw-review-formats-"));
  try {
    // Fountain: warned, confirmed, plain readable text.
    await page.getByRole("button", { name: "导出 Fountain 格式" }).click();
    await expect(page.getByTestId("export-confirm")).toBeVisible();
    await expect(page.getByTestId("export-confirm-scope")).toContainText("r4");
    const [fountainDownload] = await Promise.all([
      page.waitForEvent("download"),
      page.getByTestId("export-confirm-yes").click(),
    ]);
    const fountainFile = path.join(dir, fountainDownload.suggestedFilename());
    await fountainDownload.saveAs(fountainFile);
    const fountain = await readFile(fountainFile, "utf-8");
    expect(fountain.startsWith("{")).toBe(false);
    expect(fountain).toContain("灯不能灭。");

    // VideoGen: warned, confirmed, real ZIP with the expected members.
    await page.getByRole("button", { name: "导出 VideoGen 提示词" }).click();
    await expect(page.getByTestId("export-confirm")).toBeVisible();
    const [zipDownload] = await Promise.all([
      page.waitForEvent("download"),
      page.getByTestId("export-confirm-yes").click(),
    ]);
    expect(zipDownload.suggestedFilename()).toBe(`${p.project_id}_video_gen.zip`);
    const zipFile = path.join(dir, zipDownload.suggestedFilename());
    await zipDownload.saveAs(zipFile);
    const zip = await inspectZip(zipFile);
    expect(zip.isZip).toBe(true);
    expect(zip.badMember).toBeNull();
    expect(zip.shotsJson.map((s: any) => s.shot_id)).toEqual([
      "shot_e2e_01", "shot_e2e_02",
    ]);
    expect(zip.firstShotTxt).toContain(LONG_PROMPT);

    // One project, one scope: every format warned about the same three
    // artifacts (already exercised above), and none of them cleared a flag.
    expect((await fetchProject(request, p.project_id)).review.review_flags).toHaveLength(3);
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});


test("a flagless project exports immediately, without any dialog", async ({ page, request }) => {
  const p = await seedProject(request, "无警告直接导出");
  await seedExportableProject(p.project_id, LONG_PROMPT);
  await openApp(page, `/?project=${p.project_id}`);

  const downloads = countDownloads(page);
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page.getByRole("button", { name: "导出 JSON" }).click(),
  ]);
  expect(download.suggestedFilename()).toBe(`${p.project_id}.json`);
  await expect(page.getByTestId("export-confirm")).toHaveCount(0);
  expect(downloads()).toBe(1);
});


// ── Session isolation: a stale export belongs to its own session ──


test("a late export response never dialogs or downloads in the new session", async ({
  page,
  request,
}) => {
  const pa = await seedFlaggedProject(request, "导出隔离甲");
  await seedExportableProject(pa.project_id, LONG_PROMPT);
  const pb = await seedProject(request, "导出隔离乙");
  await seedExportableProject(pb.project_id, LONG_PROMPT);

  await installGate(page);
  await openApp(page, `/?project=${pa.project_id}`);
  await expect(page.getByRole("button", { name: "导出 JSON" })).toBeEnabled();

  // Hold A's export response, then leave for B while it is in flight.
  await gate(page, "GET /export/json");
  await page.getByRole("button", { name: "导出 JSON" }).click();
  await waitForHeld(page, "GET /export/json");
  await page.locator('button[title^="打开项目"]', { hasText: pb.title }).click();
  await expect(page.locator('button[title^="打开项目"][aria-current="true"]'))
    .toHaveText(new RegExp(pb.title));
  await expect(page.getByRole("button", { name: "导出 JSON" })).toBeEnabled();

  // The stale response lands in B's session: no dialog, no download, and
  // B's own buttons were never frozen by A's busy state.
  await release(page, "GET /export/json");
  await page.waitForTimeout(400);
  await expect(page.getByTestId("export-confirm")).toHaveCount(0);
  const downloads = countDownloads(page);
  expect(downloads()).toBe(0);

  // Back to A: the abandoned export must not pop up here either (A→B→A).
  await page.locator('button[title^="打开项目"]', { hasText: pa.title }).click();
  await expect(page.locator('button[title^="打开项目"][aria-current="true"]'))
    .toHaveText(new RegExp(pa.title));
  await expect(page.getByTestId("export-confirm")).toHaveCount(0);

  // A's export still works normally after the abandoned round trip (the
  // gate is removed first, or the click would just be held again).
  await ungate(page, "GET /export/json");
  await page.getByRole("button", { name: "导出 JSON" }).click();
  await expect(page.getByTestId("export-confirm")).toBeVisible();
  await page.getByTestId("export-confirm-cancel").click();
  await expect(page.getByTestId("export-confirm")).toHaveCount(0);
});


// ── Keyboard, theme, narrow viewport ───────────────────────


test("light theme and a narrow window complete a warned export via keyboard", async ({
  page,
  request,
}) => {
  const p = await seedFlaggedProject(request, "窄窗键盘导出");
  await seedExportableProject(p.project_id, LONG_PROMPT);

  await page.emulateMedia({ colorScheme: "dark" });
  await page.setViewportSize({ width: 900, height: 700 });
  await openApp(page, `/?project=${p.project_id}`);
  await page.locator('button[title="切换到亮色模式"]').click();
  expect(await page.evaluate(() => document.documentElement.dataset.theme)).toBe("light");

  // Open the dialog and cancel it from the keyboard: Escape never downloads.
  const downloads = countDownloads(page);
  await page.getByRole("button", { name: "导出 JSON" }).click();
  await expect(page.getByTestId("export-confirm")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByTestId("export-confirm")).toHaveCount(0);
  expect(downloads()).toBe(0);

  // Reopen and confirm from the keyboard: focus the button, press Enter.
  await page.getByRole("button", { name: "导出 JSON" }).click();
  await expect(page.getByTestId("export-confirm")).toBeVisible();
  await page.getByTestId("export-confirm-yes").focus();
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page.keyboard.press("Enter"),
  ]);
  expect(download.suggestedFilename()).toBe(`${p.project_id}.json`);
});

// ── Review round 2: ownership, abandoned results, chat preservation ──


/** Wait until at least `count` requests are held for this gate key (the
 * plain waitForHeld passes as soon as ANY older request is held). */
async function waitForHeldCount(page: Page, key: string, count: number) {
  await page.waitForFunction(
    ({ k, n }) => {
      const q = (window as any).__gatePending.get(k);
      return Array.isArray(q) && q.length >= n;
    },
    { k: key, n: count },
    { timeout: 5_000 },
  );
}


test("reviewer: abandoned export after A B A stays silent, new busy kept", async ({
  page,
  request,
}) => {
  const a = await seedFlaggedProject(request, "reviewer export A");
  const b = await seedFlaggedProject(request, "reviewer export B");
  await installGate(page);
  await openApp(page, `/?project=${a.project_id}`);
  await expect(page.getByRole("button", { name: "导出 JSON" })).toBeEnabled();

  await gate(page, "GET /export/json");
  await page.getByRole("button", { name: "导出 JSON" }).click();
  await waitForHeld(page, "GET /export/json");
  for (const p of [b, a]) {
    await page.locator('button[title^="打开项目"]', { hasText: p.title }).click();
    await expect(page.locator('button[title^="打开项目"][aria-current="true"]')).toHaveText(new RegExp(p.title));
    await expect(page.getByRole("button", { name: "导出 JSON" })).toBeEnabled();
  }

  // Overlap: launch a NEW export on the re-opened A session — both are now
  // held; FIFO release answers the OLD one first.
  await page.getByRole("button", { name: "导出 JSON" }).click();
  await waitForHeldCount(page, "GET /export/json", 2);
  await release(page, "GET /export/json");
  await page.waitForTimeout(600);

  // The abandoned response: no dialog, no download — and it must NOT
  // release the new request's busy state.
  await expect(page.getByTestId("export-confirm")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "导出 JSON" })).toBeDisabled();

  // The new response belongs here: the dialog opens for the new session.
  await release(page, "GET /export/json");
  await expect(page.getByTestId("export-confirm")).toBeVisible();
  await expect(page.getByTestId("export-confirm-scope")).toContainText("即将导出 r3");
  await page.getByTestId("export-confirm-cancel").click();
  await expect(page.getByTestId("export-confirm")).toHaveCount(0);
});


test("reviewer: old confirm preserves reopened panel; the POST still lands", async ({
  page,
  request,
}) => {
  const p = await seedFlaggedProject(request, "reviewer confirm");
  await installGate(page);
  await openApp(page, `/?project=${p.project_id}`);
  await openReviewPanel(page);
  await page.getByTestId("review-select-2").uncheck();
  await gate(page, "POST /review/confirm");
  await page.getByTestId("confirm-review-keep").click();
  await waitForHeld(page, "POST /review/confirm");

  // Close, then reopen: a NEW panel generation with a fresh selection.
  await page.getByTestId("review-close").click();
  await expect(page.getByTestId("review-panel")).toHaveCount(0);
  await openReviewPanel(page);
  await page.getByTestId("review-select-0").uncheck();
  await expect(page.getByTestId("review-selected-count")).toHaveText("已选 2/3 项");

  // The old confirm's SUCCESS response lands: it must not close the new
  // panel, not rewrite its selection and not append its notice.
  await release(page, "POST /review/confirm");
  await page.waitForTimeout(600);
  await expect(page.getByTestId("review-panel")).toBeVisible();
  await expect(page.getByTestId("review-select-0")).not.toBeChecked();
  await expect(page.getByTestId("review-selected-count")).toHaveText("已选 2/3 项");
  await expect(page.getByText(/已确认沿用/)).toHaveCount(0);

  // Ignoring the response is not a rollback: the transaction landed with
  // its own (partial) scope — script + storyboard cleared, the highlight
  // remains pending at r4.
  const after = await fetchProject(request, p.project_id);
  expect(after.revision).toBe(4);
  expect(after.review.review_flags.map((f: any) => f.artifact))
    .toEqual(["visual_highlights"]);

  // The stale panel recovers through the normal conflict → re-read path
  // (the gate rule from the first confirm comes off first).
  await ungate(page, "POST /review/confirm");
  await page.getByTestId("confirm-review-keep").click();
  await expect(page.getByTestId("review-conflict")).toBeVisible();
  await page.getByTestId("review-reload").click();
  await expect(page.getByTestId("review-conflict")).toHaveCount(0);
  await expect(page.getByTestId("review-basis")).toContainText("依据 r4");
  await expect(page.getByTestId("review-count")).toHaveText("1");
  await page.getByTestId("confirm-review-keep").click();
  await expect(page.getByTestId("review-panel")).toHaveCount(0);
  await expect(page.getByTestId("review-notice")).toHaveCount(0);
  const final = await fetchProject(request, p.project_id);
  expect(final.review.review_flags).toEqual([]);
  expect(final.revision).toBe(5);
});


test("old confirm response after A→B→A stays silent", async ({ page, request }) => {
  const pa = await seedFlaggedProject(request, "确认隔离甲");
  const pb = await seedFlaggedProject(request, "确认隔离乙");
  await installGate(page);
  await openApp(page, `/?project=${pa.project_id}`);
  await openReviewPanel(page);
  await gate(page, "POST /review/confirm");
  await page.getByTestId("confirm-review-keep").click();
  await waitForHeld(page, "POST /review/confirm");
  // The modal panel blocks the sidebar; closing it also invalidates the
  // panel generation, so the A→B→A legs exercise the session guard too.
  await page.getByTestId("review-close").click();
  await expect(page.getByTestId("review-panel")).toHaveCount(0);

  for (const p of [pb, pa]) {
    await page.locator('button[title^="打开项目"]', { hasText: p.title }).click();
    await expect(page.locator('button[title^="打开项目"][aria-current="true"]')).toHaveText(new RegExp(p.title));
  }
  await release(page, "POST /review/confirm");
  await page.waitForTimeout(600);

  // The old success response (same project id again!) must reopen nothing,
  // append nothing. The POST itself did land server-side.
  await expect(page.getByTestId("review-panel")).toHaveCount(0);
  await expect(page.getByText(/已确认沿用/)).toHaveCount(0);
  const after = await fetchProject(request, pa.project_id);
  expect(after.review.review_flags).toEqual([]);
  expect(after.revision).toBe(4);
});


test("old confirm response after close stays silent and never reopens", async ({
  page,
  request,
}) => {
  const p = await seedFlaggedProject(request, "确认后关闭");
  await installGate(page);
  await openApp(page, `/?project=${p.project_id}`);
  await openReviewPanel(page);
  await gate(page, "POST /review/confirm");
  await page.getByTestId("confirm-review-keep").click();
  await waitForHeld(page, "POST /review/confirm");
  await page.getByTestId("review-close").click();
  await expect(page.getByTestId("review-panel")).toHaveCount(0);

  await release(page, "POST /review/confirm");
  await page.waitForTimeout(600);
  // A closed panel must never be rebuilt by its own late response.
  await expect(page.getByTestId("review-panel")).toHaveCount(0);
  await expect(page.getByText(/已确认沿用/)).toHaveCount(0);
  const after = await fetchProject(request, p.project_id);
  expect(after.revision).toBe(4); // landed server-side regardless
});


test("reload failure appends to the chat and keeps the panel usable", async ({
  page,
  request,
}) => {
  const p = await seedFlaggedProject(request, "重读失败保聊天");
  await installGate(page);
  await openApp(page, `/?project=${p.project_id}`);
  await openReviewPanel(page);

  // Force a conflict: the upstream edits again while the panel is open.
  const second = await request.patch(
    `${API_BASE}/api/projects/${p.project_id}/artifacts/characters/char_e2e_02`,
    { data: { expected_revision: 3, changes: { name: "陈叔·改" } } },
  );
  expect(second.ok()).toBe(true);
  await page.getByTestId("confirm-review-keep").click();
  await expect(page.getByTestId("review-conflict")).toBeVisible();

  // The re-read FAILS (simulated 500): the error is appended to the CURRENT
  // chat session — the earlier chat message must survive it.
  await gateRespond(page, `GET /projects/${p.project_id}`, 500, {
    detail: { message: "模拟读取失败" },
  });
  await page.getByTestId("review-reload").click();
  await release(page, `GET /projects/${p.project_id}`);
  await expect(page.getByText(/重新读取待复核内容失败/)).toBeVisible({ timeout: 3000 });
  await expect(page.getByText(`已打开项目「${p.title}」`)).toBeVisible();
  // The panel keeps its conflict box, selection and manual retry entry.
  await expect(page.getByTestId("review-conflict")).toBeVisible();
  await expect(page.getByTestId("review-select-0")).toBeChecked();
  await expect(page.getByTestId("review-reload")).toBeEnabled();

  // Retrying without the fault re-bases the panel on r4.
  await ungate(page, `GET /projects/${p.project_id}`);
  await page.getByTestId("review-reload").click();
  await expect(page.getByTestId("review-basis")).toContainText("依据 r4", { timeout: 3000 });
  await expect(page.getByTestId("review-conflict")).toHaveCount(0);
  await page.getByTestId("confirm-review-keep").click();
  await expect(page.getByTestId("review-panel")).toHaveCount(0);
  await expect(page.getByTestId("review-notice")).toHaveCount(0);
  const after = await fetchProject(request, p.project_id);
  expect(after.review.review_flags).toEqual([]);
  expect(after.revision).toBe(5);
});


test("abandoned reload after close and A→B→A never rebuilds the panel", async ({
  page,
  request,
}) => {
  const pa = await seedFlaggedProject(request, "重载隔离甲");
  const pb = await seedFlaggedProject(request, "重载隔离乙");
  await installGate(page);
  await openApp(page, `/?project=${pa.project_id}`);
  await openReviewPanel(page);

  // Conflict → gated re-read (held).
  const second = await request.patch(
    `${API_BASE}/api/projects/${pa.project_id}/artifacts/characters/char_e2e_02`,
    { data: { expected_revision: 3, changes: { name: "陈叔·改" } } },
  );
  expect(second.ok()).toBe(true);
  await page.getByTestId("confirm-review-keep").click();
  await expect(page.getByTestId("review-conflict")).toBeVisible();
  await gate(page, `GET /projects/${pa.project_id}`);
  await page.getByTestId("review-reload").click();
  await waitForHeld(page, `GET /projects/${pa.project_id}`);

  // Close the panel, then come back via B: the held reload belongs to a
  // dead panel generation and a dead project session.
  await page.getByTestId("review-close").click();
  await expect(page.getByTestId("review-panel")).toHaveCount(0);
  await page.locator('button[title^="打开项目"]', { hasText: pb.title }).click();
  await expect(page.locator('button[title^="打开项目"][aria-current="true"]')).toHaveText(new RegExp(pb.title));
  await page.locator('button[title^="打开项目"]', { hasText: pa.title }).click();
  await expect(page.locator('button[title^="打开项目"][aria-current="true"]')).toHaveText(new RegExp(pa.title));

  await release(page, `GET /projects/${pa.project_id}`);
  await page.waitForTimeout(600);
  // The late reload must rebuild nothing and append nothing.
  await expect(page.getByTestId("review-panel")).toHaveCount(0);
  await expect(page.getByText(/已确认沿用/)).toHaveCount(0);
  await expect(page.getByText(/重新读取待复核内容失败/)).toHaveCount(0);

  // Flush the re-open's own (still held) content GET: A settles with its
  // unconfirmed flags — nothing the abandoned reload did changed that.
  await ungate(page, `GET /projects/${pa.project_id}`);
  await releaseAll(page, `GET /projects/${pa.project_id}`);
  await expect(page.getByTestId("review-notice")).toBeVisible({ timeout: 3000 });
  await expect(page.getByTestId("review-panel")).toHaveCount(0);
  const after = await fetchProject(request, pa.project_id);
  expect(after.revision).toBe(4);
  expect(after.review.review_flags).toHaveLength(3);
});
