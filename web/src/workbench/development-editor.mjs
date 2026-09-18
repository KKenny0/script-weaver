// Creator-facing cards serialize to the existing daemon Markdown contract.
const marker = '```script-weaver-episode-map';
const blockPattern = /```script-weaver-episode-map[ \t]*\n([\s\S]*?)\n```/g;
const missing = '接受前需要一份分集地图。请添加分集，或让 Codex 发展故事。';

function checkEntries(entries) {
  if (!Array.isArray(entries)) return '分集地图必须是一组分集。';
  for (const [index, entry] of entries.entries()) {
    if (!entry || typeof entry !== 'object' || Array.isArray(entry)
      || Object.keys(entry).some(key => !['episode', 'title', 'story', 'source_span'].includes(key))) {
      return `分集地图第 ${index + 1} 行包含不支持的字段，请在原文编辑中修复。`;
    }
    if (!Number.isSafeInteger(entry.episode) || entry.episode !== index + 1) {
      return '分集编号必须从 1 开始、连续且不重复。请在原文编辑中按 1、2、3… 修复。';
    }
    if (typeof entry.title !== 'string' || typeof entry.story !== 'string') {
      return `第 ${index + 1} 集的标题和剧情必须是文字，请在原文编辑中修复。`;
    }
  }
  return '';
}

function read(original) {
  const matches = [...original.matchAll(blockPattern)];
  const markers = original.split(marker).length - 1;
  if (markers > 1) return {error: '开发稿包含多个分集地图。请在原文编辑中只保留一份。'};
  if (markers && matches.length !== 1) {
    return {error: '分集地图代码块未正确闭合。请在原文编辑中检查开头、换行和结尾的三个反引号。'};
  }
  if (!matches.length) return {entries: [], match: null, error: ''};
  const match = matches[0];
  const entries = [];
  for (const [index, line] of match[1].split('\n').entries()) {
    try {
      entries.push(JSON.parse(line));
    } catch {
      return {error: `分集地图第 ${index + 1} 行不是有效 JSON。请在原文编辑中修复，每行填写一个 JSON 对象。`};
    }
  }
  return {entries, match, error: checkEntries(entries)};
}

export function parseDevelopment(original) {
  const {entries, match, error} = read(original);
  if (error) {
    return {body: original, entries: [], error, validationError: error, hasMap: false, hasSourceSpans: false};
  }
  const validationError = !entries.length ? missing
    : entries.some(entry => !entry.title.trim() || !entry.story.trim())
      ? '接受前请补齐每一集的标题和剧情。' : '';
  return {
    body: match ? original.slice(0, match.index) + original.slice(match.index + match[0].length) : original,
    entries,
    error: '',
    validationError,
    hasMap: Boolean(match),
    hasSourceSpans: entries.some(entry => Object.hasOwn(entry, 'source_span')),
  };
}

export function validateDevelopment(original) {
  const parsed = parseDevelopment(original);
  return parsed.error || parsed.validationError;
}

function appendMap(body, block) {
  return body + (body && !body.endsWith('\n\n') ? (body.endsWith('\n') ? '\n' : '\n\n') : '') + block;
}

export function composeDevelopment(original, entries) {
  const parsed = parseDevelopment(original);
  if (parsed.error) throw new Error(parsed.error);
  const error = checkEntries(entries);
  if (error) throw new Error(error);
  if (JSON.stringify(parsed.entries) === JSON.stringify(entries)) return original;
  if (parsed.hasSourceSpans) {
    throw new Error('这份分集地图绑定原著片段，暂不支持卡片修改或重新排序。请保留来源映射并通过原文编辑修订。');
  }
  const {match} = read(original);
  const block = entries.length ? `${marker}\n${entries.map(entry => JSON.stringify(entry)).join('\n')}\n\`\`\`` : '';
  if (match) return original.slice(0, match.index) + block + original.slice(match.index + match[0].length);
  return block ? appendMap(original, block) : original;
}

export function replaceDevelopmentBody(original, body) {
  const parsed = parseDevelopment(original);
  if (parsed.error) throw new Error(parsed.error);
  if (body === parsed.body) return original;
  if (body.includes(marker)) throw new Error('正文中请勿添加分集地图代码块，请使用下方分集卡片或原文编辑。');
  const {match} = read(original);
  // Editing prose moves the intact map to the end; stale offsets must not split new prose.
  return match ? appendMap(body, match[0]) : body;
}
