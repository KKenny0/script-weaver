import { expect, test } from "@playwright/test";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import {
  gateRespond,
  installGate,
  installSSEStub,
  inspectZip,
  modelEgressWatcher,
  openApp,
  release,
  seedExportableProject,
  seedProject,
  ungate,
} from "./helpers";

/**
 * Regression for issue #21: the three browser downloads must be real,
 * tool-readable files served from the current persisted snapshot — not
 * JSON.stringify'd envelopes (the defect: a "ZIP" that was actually JSON
 * text, a Fountain file wrapped in JSON, a double-encoded project JSON).
 *
 * Every test saves the actual download and verifies file CONTENT, then
 * confirms the export wrote no new version and never touched a model
 * endpoint.
 */

const API_BASE = "http://127.0.0.1:8310";
// >200 chars: CSV keeps its 200-char summary, JSON/TXT must carry it whole.
const LONG_PROMPT = "夜色中的灯塔守望者" + "，海风呼啸而过".repeat(30);

let egress: string[];

test.beforeEach(async ({ page }) => {
  egress = modelEgressWatcher(page);
  await installGate(page);
  await installSSEStub(page);
});

test.afterEach(async () => {
  expect(egress, "no real request may reach a model-bound endpoint").toEqual([]);
});

test("downloads are real, readable files from the persisted snapshot", async ({
  page,
  request,
}) => {
  const p = await seedProject(request, "导出下载验收");
  await seedExportableProject(p.project_id, LONG_PROMPT);

  const versionsBefore = (
    await (await request.get(`${API_BASE}/api/projects/${p.project_id}/versions`)).json()
  ).versions.length;

  await openApp(page, `/?project=${p.project_id}`);
  await expect(page.getByRole("button", { name: "导出 Fountain 格式" })).toBeEnabled();
  await expect(page.getByRole("button", { name: "导出 VideoGen 提示词" })).toBeEnabled();

  const dir = await mkdtemp(path.join(tmpdir(), "sw-export-"));
  try {
    // ── JSON: raw ProjectState, no envelope, no double encoding ──
    const [jsonDownload] = await Promise.all([
      page.waitForEvent("download"),
      page.getByRole("button", { name: "导出 JSON" }).click(),
    ]);
    expect(jsonDownload.suggestedFilename()).toBe(`${p.project_id}.json`);
    const jsonFile = path.join(dir, jsonDownload.suggestedFilename());
    await jsonDownload.saveAs(jsonFile);
    const project = JSON.parse(await readFile(jsonFile, "utf-8"));
    expect(project.meta.id).toBe(p.project_id);
    expect(project.meta.title).toBe(p.title);
    expect(project.script.title).toBe("夜行灯塔");
    expect(project.storyboard.shots[0].dialogue).toBe("灯不能灭。");
    expect("content" in project).toBe(false);

    // ── Fountain: plain text, not JSON-wrapped ──
    const [fountainDownload] = await Promise.all([
      page.waitForEvent("download"),
      page.getByRole("button", { name: "导出 Fountain 格式" }).click(),
    ]);
    expect(fountainDownload.suggestedFilename()).toBe(`${p.project_id}.fountain`);
    const fountainFile = path.join(dir, fountainDownload.suggestedFilename());
    await fountainDownload.saveAs(fountainFile);
    const fountain = await readFile(fountainFile, "utf-8");
    expect(fountain.startsWith("{")).toBe(false);
    expect(fountain).toContain("Title: 夜行灯塔");
    expect(fountain).toContain("INT 灯塔顶层 - 夜");
    expect(fountain).toContain("灯不能灭。");

    // ── VideoGen: a real ZIP that standard tools can open ──
    const [zipDownload] = await Promise.all([
      page.waitForEvent("download"),
      page.getByRole("button", { name: "导出 VideoGen 提示词" }).click(),
    ]);
    expect(zipDownload.suggestedFilename()).toBe(`${p.project_id}_video_gen.zip`);
    const zipFile = path.join(dir, zipDownload.suggestedFilename());
    await zipDownload.saveAs(zipFile);
    const zip = await inspectZip(zipFile);
    expect(zip.isZip).toBe(true);
    expect(zip.badMember).toBeNull(); // zipfile.testzip: every member decompresses
    expect(zip.members).toEqual([
      "shots/shot_e2e_01.txt",
      "shots/shot_e2e_02.txt",
      "video_gen_shots.csv",
      "video_gen_shots.json",
    ]);
    // JSON/CSV/TXT describe the same two shots.
    expect(zip.shotsJson.map((s) => s.shot_id)).toEqual(["shot_e2e_01", "shot_e2e_02"]);
    expect(zip.shotsJson[0].dialogue_text).toBe("灯不能灭。");
    // Full long prompt survives in JSON and TXT; CSV shows the 200-char summary.
    expect(zip.shotsJson[0].image_prompt).toBe(LONG_PROMPT);
    expect(zip.firstShotTxt).toContain(LONG_PROMPT);
    expect(zip.csvText).toContain(LONG_PROMPT.slice(0, 200) + "...");
    expect(zip.csvText).not.toContain(LONG_PROMPT);

    // Exports are read-only: no new version, nothing written.
    const versionsAfter = (
      await (await request.get(`${API_BASE}/api/projects/${p.project_id}/versions`)).json()
    ).versions.length;
    expect(versionsAfter).toBe(versionsBefore);
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

test("export failure shows the API detail message", async ({ page, request }) => {
  const p = await seedProject(request, "导出错误提示");
  await seedExportableProject(p.project_id, LONG_PROMPT);

  await openApp(page, `/?project=${p.project_id}`);
  await expect(page.getByRole("button", { name: "导出 Fountain 格式" })).toBeEnabled();

  await gateRespond(page, "GET /export/fountain", 400, {
    detail: "No script to export as Fountain",
  });
  const dialog = page.waitForEvent("dialog");
  await page.getByRole("button", { name: "导出 Fountain 格式" }).click();
  await release(page, "GET /export/fountain"); // deliver the held 400 response
  const dlg = await dialog;
  expect(dlg.message()).toContain("No script to export as Fountain");
  await dlg.dismiss();
  await ungate(page, "GET /export/fountain");
});

test("history view still exports the current project, not the snapshot", async ({
  page,
  request,
}) => {
  const p = await seedProject(request, "历史视图导出");
  await seedExportableProject(p.project_id, LONG_PROMPT); // bumps revision to 2
  const currentTitle = `${p.title}-r2`;
  const rename = await request.patch(`${API_BASE}/api/projects/${p.project_id}`, {
    data: { title: currentTitle, expected_revision: 2 },
  });
  expect(rename.ok()).toBe(true);

  await openApp(page, `/?project=${p.project_id}`);
  await expect(page.getByRole("button", { name: "导出 JSON" })).toBeEnabled();

  // Open history and view an older revision so the panel shows a snapshot.
  await page.getByRole("button", { name: "查看版本历史" }).click();
  await expect(page.getByRole("button", { name: "查看版本 r2" })).toBeVisible();
  const snapshotLoaded = page.waitForResponse((r) =>
    r.url().includes(`/versions/2`),
  );
  await page.getByRole("button", { name: "查看版本 r2" }).click();
  await snapshotLoaded;

  // The export must hit the current project's export endpoint and carry the
  // CURRENT persisted state (renamed title), not the viewed r2 snapshot.
  const exportRequest = page.waitForRequest(
    (r) => r.url().includes(`/api/projects/${p.project_id}/export/json`),
  );
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page.getByRole("button", { name: "导出 JSON" }).click(),
  ]);
  const req = await exportRequest;
  expect(req.url()).not.toContain("/versions");

  expect(download.suggestedFilename()).toBe(`${p.project_id}.json`);
  const dir = await mkdtemp(path.join(tmpdir(), "sw-export-"));
  try {
    const file = path.join(dir, download.suggestedFilename());
    await download.saveAs(file);
    const project = JSON.parse(await readFile(file, "utf-8"));
    expect(project.meta.id).toBe(p.project_id);
    expect(project.meta.title).toBe(currentTitle);
    expect(project.script.title).toBe("夜行灯塔");
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});
