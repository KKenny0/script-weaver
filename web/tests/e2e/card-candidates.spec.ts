import { expect, test, type Page } from "@playwright/test";
import { execFile } from "node:child_process";
import { openApp, seedCardsProject, seedProject, fetchProject } from "./helpers";

// Controlled model substitute: real store admission/completion, no LLM or provider.
async function seedCandidate(pid: string, kind = "characters", target = "char_e2e_01", complete = true) {
  const script = `
import json, sys, uuid
from pathlib import Path
from script_weaver.core.project_store import ProjectStore, hash_run_request
s = ProjectStore(Path('/tmp/script-weaver-e2e-data/main-web/projects.sqlite3'))
pid, kind, target, complete = sys.argv[1:]
r = s.get_required(pid)
req = dict(kind=kind, target_id=target, instruction='test', expected_revision=r.revision)
run, _ = s.admit_generation_run(pid, kind='card_candidate', request_key=str(uuid.uuid4()), request=req, request_hash=hash_run_request(req), base_revision=r.revision, base_state_json=r.state_json, checkpoint_json=None)
c = None
if complete == 'true':
    changes = {'visual_description': '候选画面'} if kind == 'shots' else {'name': '候选名称'}
    c = s.complete_card_candidate(run.run_id, dict(kind=kind, target_id=target, changes=changes))
print(json.dumps(dict(run_id=run.run_id, candidate=c)))
s.close()
`;
  return new Promise<any>((resolve, reject) => execFile("../.venv/bin/python", ["-c", script, pid, kind, target, String(complete)],
    (error, stdout) => error ? reject(error) : resolve(JSON.parse(stdout))));
}
async function openCard(page: Page, pid: string, kind = "characters", id = "char_e2e_01") {
  await openApp(page, `/?project=${pid}`);
  await page.locator(".segment-tab", { hasText: { characters: "主角列表", scenes: "场景列表", shots: "影像亮点" }[kind] }).click();
  await page.getByTestId(`edit-card-${kind}-${id}`).click();
  await expect(page.getByTestId("card-candidates")).toBeVisible();
}

for (const [kind, id] of [["characters", "char_e2e_01"], ["scenes", "scene_e2e_01"], ["shots", "shot_e2e_01"]]) {
  test(`${kind}: generate compare refresh adopt`, async ({ page, request }) => {
    const p = await seedProject(request, "定向候选"); await seedCardsProject(p.project_id);
    const before = await fetchProject(request, p.project_id);
    await page.route(`**/artifacts/${kind}/${id}/candidates`, async route => {
      expect(route.request().postDataJSON().expected_revision).toBe(2);
      const seeded = await seedCandidate(p.project_id, kind, id);
      await route.fulfill({ json: { created: true, run: { run_id: seeded.run_id } } });
    });
    await openCard(page, p.project_id, kind, id);
    await page.getByLabel("修改要求", { exact: true }).fill("让此卡片更明确");
    await page.getByRole("button", { name: "生成候选", exact: true }).click();
    await expect(page.getByText("待采用 · 依据 r2")).toBeVisible();
    expect((await fetchProject(request, p.project_id)).revision).toBe(before.revision);
    await page.getByTestId("close-drawer").click();
    await page.reload();
    await page.locator(".segment-tab", { hasText: { characters: "主角列表", scenes: "场景列表", shots: "影像亮点" }[kind] }).click();
    await page.getByTestId(`edit-card-${kind}-${id}`).click();
    await expect(page.getByText("待采用 · 依据 r2")).toBeVisible();
    await page.getByRole("button", { name: "采用候选", exact: true }).click();
    await expect(page.getByTestId("card-edit-drawer")).toHaveCount(0);
    const after = await fetchProject(request, p.project_id);
    expect(after.revision).toBe(3);
    expect(after.review.review_flags.length).toBeGreaterThan(0);
  });
}

test("manual draft confirmation, reject persists and no project revision", async ({ page, request }) => {
  const p = await seedProject(request, "草稿保留"); await seedCardsProject(p.project_id); await seedCandidate(p.project_id);
  await openCard(page, p.project_id);
  await page.getByTestId("field-name").fill("手工草稿");
  await page.getByRole("button", { name: "采用候选", exact: true }).click();
  await expect(page.getByRole("alertdialog", { name: "采用候选前确认丢弃草稿" })).toBeVisible();
  await page.getByRole("button", { name: "继续编辑", exact: true }).click();
  await expect(page.getByTestId("field-name")).toHaveValue("手工草稿");
  await page.getByRole("button", { name: "放弃候选", exact: true }).click();
  await expect(page.getByText("已放弃 · 依据 r2")).toBeVisible();
  expect((await fetchProject(request, p.project_id)).revision).toBe(2);
});

test("in-progress candidate can be stopped after reopen while manual edit stays enabled", async ({ page, request }) => {
  const p = await seedProject(request, "停止候选"); await seedCardsProject(p.project_id); await seedCandidate(p.project_id, "characters", "char_e2e_01", false);
  await openCard(page, p.project_id);
  await expect(page.getByRole("button", { name: "停止候选生成" })).toBeVisible();
  await expect(page.getByTestId("field-name")).toBeEnabled();
  await page.getByTestId("close-drawer").click(); await page.getByTestId("edit-card-characters-char_e2e_01").click();
  await page.getByRole("button", { name: "停止候选生成" }).click();
  await expect(page.getByText(/原内容保留，可重新发起/)).toBeVisible();
  expect((await fetchProject(request, p.project_id)).revision).toBe(2);
});

test("slow candidate reads finish and closed drawer responses cannot update a new instance", async ({ page, request }) => {
  const p = await seedProject(request, "缓慢候选"); await seedCardsProject(p.project_id); await seedCandidate(p.project_id);
  let calls = 0;
  await page.route(`**/projects/${p.project_id}/candidates`, async route => {
    const response = await route.fetch();
    calls++;
    await new Promise(resolve => setTimeout(resolve, calls === 1 ? 3000 : 1800));
    await route.fulfill({ response });
  });
  await openCard(page, p.project_id);
  await page.getByTestId("close-drawer").click(); await page.getByTestId("edit-card-characters-char_e2e_01").click();
  await expect(page.getByText("待采用 · 依据 r2")).toBeVisible({ timeout: 10000 });
  await expect(page.getByTestId("field-name")).toHaveValue("阿芸");
});

test("manual save invalidates candidate and provides reload path", async ({ page, request }) => {
  const p = await seedProject(request, "候选过期"); await seedCardsProject(p.project_id); await seedCandidate(p.project_id);
  await openCard(page, p.project_id);
  await page.getByTestId("field-name").fill("手工优先");
  await page.getByTestId("save-card").click();
  await expect(page.getByTestId("card-edit-drawer")).toHaveCount(0);
  await page.getByTestId("edit-card-characters-char_e2e_01").click();
  await expect(page.getByText(/已过期 · 项目发生变化/)).toBeVisible();
  await expect(page.getByRole("button", { name: "采用候选", exact: true })).toHaveCount(0);
  await expect(page.getByTestId("field-name")).toHaveValue("手工优先");
});

test("late adoption JSON after close and A B A cannot close or overwrite a new panel", async ({ page, request }) => {
  const a = await seedProject(request, "候选甲"), b = await seedProject(request, "候选乙");
  await seedCardsProject(a.project_id); await seedCardsProject(b.project_id); await seedCandidate(a.project_id);
  await openCard(page, a.project_id);
  await page.evaluate(() => {
    const original = window.fetch;
    (window as any).__adoptionHeld = false;
    window.fetch = async (...args) => {
      const response = await original(...args);
      if (String(args[0]).endsWith("/accept")) {
        const json = response.json.bind(response);
        response.json = async () => {
          const body = await json();
          (window as any).__adoptionHeld = true;
          await new Promise<void>(resolve => { (window as any).__releaseAdoption = resolve; });
          return body;
        };
      }
      return response;
    };
  });
  await page.getByRole("button", { name: "采用候选", exact: true }).click();
  await page.waitForFunction(() => (window as any).__adoptionHeld);
  await page.getByTestId("close-drawer").click();
  await page.locator('button[title^="打开项目"]', { hasText: b.title }).click();
  await page.locator('button[title^="打开项目"]', { hasText: a.title }).click();
  await page.locator(".segment-tab", { hasText: "主角列表" }).click();
  await page.getByTestId("edit-card-characters-char_e2e_01").click();
  await page.getByTestId("field-name").fill("新面板草稿");
  await page.evaluate(() => (window as any).__releaseAdoption());
  await expect(page.getByTestId("field-name")).toHaveValue("新面板草稿");
  await expect(page.getByTestId("card-edit-drawer")).toBeVisible();
});

test("unconfigured model terminates candidate and leaves manual editing available", async ({ page, request }) => {
  const p = await seedProject(request, "模型未配置"); await seedCardsProject(p.project_id);
  await openCard(page, p.project_id);
  await page.getByLabel("修改要求", { exact: true }).fill("让性格更明确");
  await page.getByRole("button", { name: "生成候选", exact: true }).click();
  await expect(page.getByText(/原内容保留，可重新发起/)).toBeVisible();
  await page.getByTestId("field-name").fill("仍可编辑");
  await page.getByTestId("save-card").click();
  await expect(page.getByTestId("card-edit-drawer")).toHaveCount(0);
  expect((await fetchProject(request, p.project_id)).revision).toBe(3);
});
