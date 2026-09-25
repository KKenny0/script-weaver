import { expect, test } from "@playwright/test";
import {
  gate,
  gateRespond,
  installGate,
  installSSEStub,
  modelEgressWatcher,
  openApp,
  releaseAndSettle,
  seedProject,
  seedSecondRevision,
} from "./helpers";

/**
 * Regression: history requests (list + snapshot) must only update the view
 * session they belong to. Late responses must not pollute another project,
 * reopen a closed panel, override a newer view intent — or, since round 5,
 * leave the loading state of the request they superseded stuck on screen.
 *
 * The first four tests guard the round-4 fixes; the last two FAIL on the
 * round-5 baseline (07a69bb) where a superseded snapshot load kept
 * viewingLoading=true forever ("正在载入快照", all version buttons disabled).
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

test("late history list response must not pollute another project", async ({ page, request }) => {
  const a = await seedProject(request, "历史隔离项目A");
  const b = await seedProject(request, "历史隔离项目B");
  await seedSecondRevision(request, a.project_id, `${a.title}-r2`);

  await openApp(page, `/?project=${a.project_id}`);
  await expect(page.getByText(new RegExp(`${a.title}-r2`)).first()).toBeVisible();

  await gate(page, "GET /versions");
  await page.getByRole("button", { name: "查看版本历史" }).click();
  // The list request is now held.

  await page.getByRole("button", { name: new RegExp(`^${b.title}`) }).click();
  await expect(page).toHaveURL(new RegExp(`project=${b.project_id}`));
  await expect(page.getByText(/已打开项目「历史隔离项目B-/)).toBeVisible();

  await releaseAndSettle(page, "GET /versions", "/versions");

  // A's late list must not open B's history panel.
  await expect(page.getByText("版本历史", { exact: true })).toHaveCount(0);
  // B's live content is untouched and stays usable.
  await expect(page.getByText(/已打开项目「历史隔离项目B-/)).toBeVisible();
});

test("late snapshot response must not pollute another project's history", async ({ page, request }) => {
  const a = await seedProject(request, "快照隔离项目A");
  await seedSecondRevision(request, a.project_id, `${a.title}-r2`);
  const b = await seedProject(request, "快照隔离项目B");

  await openApp(page, `/?project=${a.project_id}`);
  await expect(page.getByText(new RegExp(`${a.title}-r2`)).first()).toBeVisible();

  await page.getByRole("button", { name: "查看版本历史" }).click();
  await expect(page.getByRole("button", { name: "查看版本 r1" })).toBeVisible();

  await gate(page, "GET /versions/1");
  await page.getByRole("button", { name: "查看版本 r1" }).click();
  // A's snapshot request is held.

  await page.getByRole("button", { name: new RegExp(`^${b.title}`) }).click();
  await expect(page).toHaveURL(new RegExp(`project=${b.project_id}`));

  // Open B's own history (fresh list view, no snapshot selected).
  await page.getByRole("button", { name: "查看版本历史" }).click();
  await expect(page.getByRole("button", { name: "查看版本 r1" })).toBeVisible();

  await releaseAndSettle(page, "GET /versions/1", "/versions/1");
  // A's late snapshot must NOT turn B's fresh list into a snapshot view.
  await expect(page.getByText("正在查看历史版本", { exact: false })).toHaveCount(0);
  await expect(page.getByText("版本历史", { exact: true })).toBeVisible();
});

test("closing the history panel invalidates the in-flight list load", async ({ page, request }) => {
  const a = await seedProject(request, "关闭后迟到项目A");

  await openApp(page, `/?project=${a.project_id}`);
  await expect(page.getByText(new RegExp(a.title)).first()).toBeVisible();

  await gate(page, "GET /versions");
  await page.getByRole("button", { name: "查看版本历史" }).click();
  await expect(page.getByText("正在加载版本列表")).toBeVisible();

  // Toggle the panel closed while the list request is held.
  await page.getByRole("button", { name: "关闭版本历史" }).click();
  await expect(page.getByText("版本历史", { exact: true })).toHaveCount(0);

  await releaseAndSettle(page, "GET /versions", "/versions");
  // Neither a late success nor a late failure may reopen the panel.
  await expect(page.getByText("版本历史", { exact: true })).toHaveCount(0);
  await expect(page.getByText("正在加载版本列表")).toHaveCount(0);
});

test("reopen during an in-flight load keeps the newest intent (out-of-order)", async ({ page, request }) => {
  const a = await seedProject(request, "乱序返回项目A");
  await seedSecondRevision(request, a.project_id, `${a.title}-r2`);

  await openApp(page, `/?project=${a.project_id}`);
  await expect(page.getByText(new RegExp(`${a.title}-r2`)).first()).toBeVisible();

  await gate(page, "GET /versions");
  await page.getByRole("button", { name: "查看版本历史" }).click(); // request 1 held
  await expect(page.getByText("正在加载版本列表")).toBeVisible();

  await page.getByRole("button", { name: "关闭版本历史" }).click(); // close (request 1 stale now)
  await page.getByRole("button", { name: "查看版本历史" }).click(); // request 2 held too
  await expect(page.getByText("正在加载版本列表")).toBeVisible();

  // Release ONLY the first request: its data belongs to a superseded intent.
  await releaseAndSettle(page, "GET /versions", "/versions");
  await expect(page.getByText("正在加载版本列表")).toBeVisible();
  await expect(page.getByRole("button", { name: "查看版本 r2" })).toHaveCount(0);

  // The newest request, once released, populates the view.
  await releaseAndSettle(page, "GET /versions", "/versions");
  await expect(page.getByRole("button", { name: "查看版本 r2" })).toBeVisible();
});

test("superseded snapshot load: refreshing the list must clear its loading state (late success)", async ({ page, request }) => {
  const a = await seedProject(request, "快照被刷新替代项目A");
  await seedSecondRevision(request, a.project_id, `${a.title}-r2`);

  await openApp(page, `/?project=${a.project_id}`);
  await expect(page.getByText(new RegExp(`${a.title}-r2`)).first()).toBeVisible();

  await page.getByRole("button", { name: "查看版本历史" }).click();
  await expect(page.getByRole("button", { name: "查看版本 r1" })).toBeVisible();

  await gate(page, "GET /versions/1");
  await page.getByRole("button", { name: "查看版本 r1" }).click();
  await expect(page.getByText("正在载入快照")).toBeVisible();
  await expect(page.getByRole("button", { name: "查看版本 r1" })).toBeDisabled();

  // Refreshing the list supersedes the pending snapshot load: the loading
  // state it owned ends immediately (this is exactly what stuck on 07a69bb).
  await gate(page, "GET /versions");
  await page.getByRole("button", { name: "刷新版本列表" }).click();
  await expect(page.getByText("正在载入快照")).toHaveCount(0);

  // The old snapshot response arrives late and is discarded…
  await releaseAndSettle(page, "GET /versions/1", "/versions/1");
  await expect(page.getByText("正在载入快照")).toHaveCount(0);

  // …the refreshed list lands, and with it the buttons are usable again.
  await releaseAndSettle(page, "GET /versions", "/versions");
  await expect(page.getByRole("button", { name: "查看版本 r2" })).toBeVisible();
  await expect(page.getByRole("button", { name: "查看版本 r1" })).toBeEnabled();

  // A fresh snapshot load works again end-to-end.
  await page.getByRole("button", { name: "查看版本 r1" }).click();
  await expect(page.getByText("正在载入快照")).toBeVisible();
  await releaseAndSettle(page, "GET /versions/1", "/versions/1");
  await expect(page.getByText("正在查看历史版本 r1")).toBeVisible();
});

test("superseded snapshot load: a late FAILURE must not leave the loading state stuck either", async ({ page, request }) => {
  const a = await seedProject(request, "迟到失败快照项目A");
  await seedSecondRevision(request, a.project_id, `${a.title}-r2`);

  await openApp(page, `/?project=${a.project_id}`);
  await expect(page.getByText(new RegExp(`${a.title}-r2`)).first()).toBeVisible();

  await page.getByRole("button", { name: "查看版本历史" }).click();
  await expect(page.getByRole("button", { name: "查看版本 r1" })).toBeVisible();

  await gateRespond(page, "GET /versions/1", 500, { detail: { message: "模拟快照失败" } });
  await page.getByRole("button", { name: "查看版本 r1" }).click();
  await expect(page.getByText("正在载入快照")).toBeVisible();

  await gate(page, "GET /versions");
  await page.getByRole("button", { name: "刷新版本列表" }).click();
  // The superseded loading state ends immediately.
  await expect(page.getByText("正在载入快照")).toHaveCount(0);

  // The old snapshot fails late — the failure belongs to a superseded
  // intent, so it is ignored and must not freeze the loading state.
  await releaseAndSettle(page, "GET /versions/1", "/versions/1");
  await expect(page.getByText("正在载入快照")).toHaveCount(0);

  // The list lands; a fresh snapshot load can still fail on its own terms,
  // visibly and retryable.
  await releaseAndSettle(page, "GET /versions", "/versions");
  await expect(page.getByRole("button", { name: "查看版本 r2" })).toBeVisible();
  await expect(page.getByRole("button", { name: "查看版本 r1" })).toBeEnabled();
  await page.getByRole("button", { name: "查看版本 r1" }).click();
  await expect(page.getByText("正在载入快照")).toBeVisible();
  await releaseAndSettle(page, "GET /versions/1", "/versions/1");
  await expect(page.getByText("版本历史加载失败：模拟快照失败")).toBeVisible();
  await expect(page.getByText("正在载入快照")).toHaveCount(0);
});
