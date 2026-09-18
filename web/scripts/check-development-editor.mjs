import assert from 'node:assert/strict';
import {parseDevelopment, composeDevelopment, replaceDevelopmentBody, validateDevelopment} from '../src/workbench/development-editor.mjs';

const entry = {episode: 1, title: '第一集', story: '她收到一封未来的信。'};
const block = rows => '```script-weaver-episode-map\n' + rows.map(row => JSON.stringify(row)).join('\n') + '\n```';
const before = '# 故事\n\n保留  空格、表情 ✨\n```text\n其他代码\n```\n\n';
const after = '\n\n## 创作笔记\n不要丢弃尾部。\n';
const original = before + block([entry]) + after;
assert.equal(parseDevelopment(original).body, before + after);
assert.equal(composeDevelopment(original, parseDevelopment(original).entries), original);
assert.equal(replaceDevelopmentBody(original, parseDevelopment(original).body), original);
const revised = composeDevelopment(original, [{...entry, title: '新的标题'}]);
assert.equal(revised, before + block([{...entry, title: '新的标题'}]) + after);
assert.equal(replaceDevelopmentBody(original, '新正文'), '新正文\n\n' + block([entry]));
assert.equal(composeDevelopment(original, []), before + after);

const plain = '# 一个想法\n\n保留我的原文。\n';
assert.deepEqual(parseDevelopment(plain).entries, []);
assert.equal(parseDevelopment(plain).body, plain);
assert.equal(parseDevelopment(plain).error, '');
assert.match(validateDevelopment(plain), /分集地图/);
const draft = composeDevelopment(plain, [{episode: 1, title: '', story: ''}]);
assert.equal(parseDevelopment(draft).error, '');
assert.equal(parseDevelopment(draft).entries.length, 1);
assert.match(validateDevelopment(draft), /补齐/);
assert.equal(validateDevelopment(composeDevelopment(draft, [entry, {...entry, episode: 2}])), '');

const span = {...entry, source_span: {start: 0, end: 37, sha256: 'a'.repeat(64)}};
const sourced = before + block([span]) + after;
assert.equal(parseDevelopment(sourced).hasSourceSpans, true);
assert.deepEqual(parseDevelopment(sourced).entries[0].source_span, span.source_span);
assert.equal(composeDevelopment(sourced, parseDevelopment(sourced).entries), sourced);
assert.throws(() => composeDevelopment(sourced, [{...span, title: '不可改'}]), /原著片段/);
assert.throws(() => composeDevelopment(sourced, []), /原著片段/);
assert.equal(replaceDevelopmentBody(sourced, '修改正文'), '修改正文\n\n' + block([span]));

for (const [content, hint] of [
  [block([entry]) + '\n' + block([entry]), /多个/],
  ['```script-weaver-episode-map\n{bad}\n```', /JSON/],
  [block([{...entry, episode: 2}]), /连续/],
  [block([{...entry, extra: true}]), /字段/],
  [block([{...entry, title: 1}]), /文字/],
  ['```script-weaver-episode-map\n{}', /闭合/],
]) {
  const parsed = parseDevelopment(content);
  assert.match(parsed.error, hint);
  assert.equal(parsed.body, content);
  assert.deepEqual(parsed.entries, []);
  assert.throws(() => composeDevelopment(content, [entry]), hint);
  assert.throws(() => replaceDevelopmentBody(content, '修改'), hint);
}
assert.throws(() => replaceDevelopmentBody(original, block([entry])), /正文/);
assert.throws(() => composeDevelopment(plain, [{...entry, episode: 3}]), /连续/);
console.log('Development editor: prose preservation, draft cards, source spans, and malformed-map recovery passed.');
