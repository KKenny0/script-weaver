import React from "react";
import { FileText, Users, Map, Palette, Film, Eye, Pencil } from "lucide-react";

// ── Types ────────────────────────────────────────

export interface ArtifactData {
  refined_idea?: string | null;
  outline?: object | null;
  characters?: object[] | null;
  scenes?: object[] | null;
  art_style?: object | null;
  script?: object | null;
  storyboard?: object | null;
  visual_highlights?: object[] | null;
}

/** Entry point for single-card editing; absent for read-only history views. */
export type EditCardHandler = (
  kind: "characters" | "scenes" | "shots",
  id: string,
) => void;

// ── Label helpers ────────────────────────────────

function shotSizeLabel(size: string): string {
  const map: Record<string, string> = {
    extreme_long_shot: "大远景",
    long_shot: "远景",
    full_shot: "全景",
    medium_long_shot: "中远景",
    medium_shot: "中景",
    medium_close_up: "近景",
    close_up: "特写",
    extreme_close_up: "大特写",
  };
  return map[size] || size;
}

function cameraAngleLabel(angle: string): string {
  const map: Record<string, string> = {
    eye_level: "平视", low_angle: "仰拍", high_angle: "俯拍",
    dutch_angle: "倾斜角", bird_eye: "鸟瞰", over_shoulder: "过肩",
    point_of_view: "主观", two_shot: "双人",
  };
  return map[angle] || angle;
}

function cameraMovementLabel(movement: string): string {
  const map: Record<string, string> = {
    static: "固定", push_in: "推", pull_out: "拉",
    pan_left: "左摇", pan_right: "右摇", tilt_up: "上仰", tilt_down: "下俯",
    dolly: "移动跟拍", tracking: "跟踪", arc: "弧形",
    crane_up: "升", crane_down: "降", handheld: "手持",
    steadicam: "斯坦尼康", aerial: "航拍",
    zoom_in: "变焦推", zoom_out: "变焦拉",
  };
  return map[movement] || movement;
}

function transitionLabel(transition: string): string {
  const map: Record<string, string> = {
    cut: "硬切", fade_in: "淡入", fade_out: "淡出", dissolve: "叠化",
    smash_cut: "碎切", match_cut: "匹配剪辑", jump_cut: "跳切",
    cross_dissolve: "交叉叠化", hard_cut: "硬切", wipe: "划像",
  };
  return transition ? (map[transition] || transition) : "";
}

const ROLE_LABELS: Record<string, string> = {
  protagonist: "主角",
  antagonist: "反派",
  supporting: "配角",
  extra: "龙套",
};

// ── Sub-components ───────────────────────────────

function EmptyState({ text }: { text: string }) {
  const isOutline = text.includes("大纲");
  return (
    <div className="empty-state">
      <div className="empty-state-mark"><FileText size={26} strokeWidth={1.5} /></div>
      <p className="empty-state-title">{text}</p>
      <p className="empty-state-hint">
        {isOutline ? "先在左侧输入故事想法并点击生成" : "该部分内容将在 Pipeline 完成后显示"}
      </p>
    </div>
  );
}

function InfoRow({ label, value, highlight }: { label: string; value: string; highlight?: boolean }) {
  if (!value) return null;
  return (
    <div className="info-row">
      <span className="info-row-label">{label}</span>
      <span className={`info-row-value${highlight ? " highlight" : ""}`}>{value}</span>
    </div>
  );
}

/** Card field row: muted label inline with the value text. */
function FieldLine({ label, value }: { label: string; value?: string | null }) {
  if (!value) return null;
  return (
    <div className="field-line">
      <span className="field-line-label">{label}</span>
      <span className="field-line-value">{value}</span>
    </div>
  );
}

/** Prompt block with a tracked micro label; clamped via CSS, full text on hover title. */
function PromptBlock({ label, text, lines = 3 }: { label: string; text: string; lines?: number }) {
  return (
    <div className="prompt-box" title={text}>
      <span className="prompt-label">{label}</span>
      <code style={{ WebkitLineClamp: lines }}>{text}</code>
    </div>
  );
}

// ── Card edit entry ──────────────────────────────

function CardEditButton({
  kind, id, name, onEditCard,
}: {
  kind: "characters" | "scenes" | "shots";
  id: string;
  name: string;
  onEditCard?: EditCardHandler;
}) {
  if (!onEditCard) return null;
  const label = `编辑${kind === "characters" ? "角色" : kind === "scenes" ? "场景" : "镜头"} ${name}`;
  return (
    <button
      type="button"
      className="card-edit-btn"
      data-testid={`edit-card-${kind}-${id}`}
      aria-label={label}
      title={label}
      onClick={() => onEditCard(kind, id)}
    >
      <Pencil size={11} /> 编辑
    </button>
  );
}

// ── Tab renderers ────────────────────────────────

function renderOutline(data: ArtifactData) {
  if (!data.outline) return <EmptyState text="暂无大纲数据" />;
  const o = data.outline as any;
  return (
    <div className="artifact-content">
      {o.basic_info && (
        <div className="info-card">
          <h4>基本信息</h4>
          <InfoRow label="类型" value={o.basic_info?.genre} />
          <InfoRow label="基调" value={o.basic_info?.tone} />
          <InfoRow label="主题" value={o.basic_info?.theme} />
          <InfoRow label="一句话梗概" value={o.basic_info?.logline} highlight />
        </div>
      )}
      {o.plot_outline?.length > 0 && (
        <div className="info-card">
          <h4>情节节拍</h4>
          <div className="beat-list">
            {o.plot_outline.map((beat: any, i: number) => (
              <div key={i} className="beat-item">
                <span className="beat-num">{beat.sequence_number}</span>
                <div className="beat-body">
                  <span className="beat-title">{beat.title}</span>
                  <p className="beat-synopsis">{beat.synopsis}</p>
                  {beat.emotional_arc && <span className="beat-emotion">情绪: {beat.emotional_arc}</span>}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function renderCharacters(data: ArtifactData, onEditCard?: EditCardHandler) {
  if (!data.characters?.length) return <EmptyState text="暂无角色数据" />;
  return (
    <div className="artifact-content grid-cards">
      {data.characters.map((char: any) => (
        <div key={char.id} className="card character-card">
          <div className="card-header">
            <div className="char-identity">
              <span className="char-disc">{(char.name || "?").slice(0, 1)}</span>
              <span className="char-name">{char.name}</span>
            </div>
            <span className={`role-tag role-${char.role}`}>{ROLE_LABELS[char.role] || char.role}</span>
            <CardEditButton kind="characters" id={char.id} name={char.name} onEditCard={onEditCard} />
          </div>
          <div className="card-body">
            {char.appearance && <p className="text-sm muted" title={char.appearance}>{char.appearance}</p>}
            <FieldLine label="性格" value={char.personality} />
            <FieldLine label="动机" value={char.motivation} />
            {char.image_prompt && <PromptBlock label="Image Prompt" text={char.image_prompt} lines={2} />}
          </div>
        </div>
      ))}
    </div>
  );
}

function renderScenes(data: ArtifactData, onEditCard?: EditCardHandler) {
  if (!data.scenes?.length) return <EmptyState text="暂无场景数据" />;
  return (
    <div className="artifact-content grid-cards">
      {data.scenes.map((scene: any) => (
        <div key={scene.id} className="card scene-card">
          <div className="card-header">
            <Map size={15} />
            <span>{scene.name}</span>
            <CardEditButton kind="scenes" id={scene.id} name={scene.name} onEditCard={onEditCard} />
          </div>
          <div className="card-body">
            <InfoRow label="类型" value={scene.location_type} />
            <InfoRow label="时间" value={scene.time_of_day} />
            <InfoRow label="氛围" value={scene.mood} highlight />
            {scene.environment && (
              <p className="text-sm muted mt-1" style={{
                display: "-webkit-box", WebkitLineClamp: 3, WebkitBoxOrient: "vertical", overflow: "hidden",
              }} title={scene.environment}>{scene.environment}</p>
            )}
            {scene.color_palette?.length > 0 && (
              <div className="color-swatches">
                {scene.color_palette.map((c: string, j: number) => (
                  <span key={j} className="color-dot" style={{ background: c }} title={c} />
                ))}
              </div>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}

function renderArtStyle(data: ArtifactData) {
  if (!data.art_style) return <EmptyState text="暂无美术风格数据" />;
  const s = data.art_style as any;
  return (
    <div className="artifact-content">
      <div className="info-card">
        <h4>整体风格</h4>
        <p className="highlight-text" style={{ lineHeight: 1.7, fontSize: 13.5 }}>{s.overall_style}</p>
      </div>
      <div className="grid-2col">
        <div className="info-card">
          <h5>主色调</h5>
          <div className="color-swatches">
            {(s.color_palette_primary || []).map((c: string, i: number) => (
              <span key={i} className="color-dot large" style={{ background: c }} title={c} />
            ))}
          </div>
        </div>
        <div className="info-card">
          <h5>辅助色</h5>
          <div className="color-swatches">
            {(s.color_palette_secondary || []).map((c: string, i: number) => (
              <span key={i} className="color-dot large" style={{ background: c }} title={c} />
            ))}
          </div>
        </div>
      </div>
      <div className="info-card"><h5>光影风格</h5><p style={{ fontSize: 13, lineHeight: 1.7 }}>{s.lighting_style}</p></div>
      <div className="info-card">
        <h5>参考美学</h5>
        <div className="tag-list">
          {(s.reference_aesthetics || []).map((r: string, i: number) => (
            <span key={i} className="tag">{r}</span>
          ))}
        </div>
      </div>
    </div>
  );
}

function renderScript(data: ArtifactData) {
  if (!data.script) return <EmptyState text="暂无剧本数据" />;
  const script = data.script as any;
  return (
    <div className="artifact-content script-viewer">
      <div className="script-header">
        <h3>{script.title || "未命名剧本"}</h3>
        <span className="badge">{script.scenes?.length || 0} 场 · ~{script.total_estimated_duration || "?"}s</span>
      </div>
      {script.scenes?.map((scene: any, si: number) => (
        <div key={si} className="script-scene">
          <div className="scene-heading">
            <span className="scene-num">{scene.heading?.scene_number || si + 1}</span>
            <span className="scene-location">
              {scene.heading?.int_ext} {scene.heading?.location} — {scene.heading?.time_of_day}
            </span>
          </div>
          {scene.blocks?.map((block: any, bi: number) => {
            if (block.block_type === "action")
              return <p key={bi} className="action-text">{block.content?.description}</p>;
            if (block.block_type === "dialogue") {
              const dc = block.content || {};
              return (
                <div key={bi} className="dialogue-block">
                  <span className="dialogue-name">{dc.character_name}</span>
                  {dc.parenthetical && <span className="parenthetical">({dc.parenthetical})</span>}
                  <p className="dialogue-line">{dc.dialogue}</p>
                </div>
              );
            }
            if (block.block_type === "transition")
              return <p key={bi} className="transition-text">→ {block.content?.type?.replace("_", " ").toUpperCase()}</p>;
            return null;
          })}
        </div>
      ))}
    </div>
  );
}

function renderStoryboard(data: ArtifactData, onEditCard?: EditCardHandler) {
  if (!data.visual_highlights?.length && !data.storyboard)
    return <EmptyState text="暂无影像亮点数据" />;

  const sb = data.storyboard as any;
  return (
    <div className="artifact-content">
      {data.visual_highlights?.length > 0 && (
        <div className="info-card">
          <h4><Eye size={15} />影像亮点</h4>
          {data.visual_highlights.map((vh: any, i: number) => (
            <div key={i} className="highlight-item">
              <span className="highlight-title">{vh.title}</span>
              <p className="highlight-desc">{vh.description}</p>
              {vh.visual_technique && <span className="highlight-tech">{vh.visual_technique}</span>}
            </div>
          ))}
        </div>
      )}

      {sb && (
        <>
          <div className="sb-header">
            <h4><Film size={15} />分镜脚本</h4>
            <span className="badge">{sb.total_shot_count} 镜头 · ~{Math.round(sb.total_estimated_duration)}s</span>
            <span className="badge mono">{sb.aspect_ratio} · {sb.fps}fps</span>
          </div>
          {/* Every shot stays reachable in a plain scrolling list — no cap,
              no virtualization (ticket #16). Stable ids key and locate cards. */}
          <div className="shots-grid">
            {sb.shots?.map((shot: any, i: number) => (
              <div key={shot.shot_id} className="shot-card">
                <div className="shot-slate">
                  <span className="shot-index">{String(shot.sequence_number || i + 1).padStart(2, "0")}</span>
                  <span className="shot-size">{shotSizeLabel(shot.shot_size)}</span>
                  <span className="shot-id-chip" title={shot.shot_id}>{shot.shot_id?.slice(-6)}</span>
                  <span className="shot-duration">{shot.duration_seconds}s</span>
                  <CardEditButton kind="shots" id={shot.shot_id} name={shot.shot_id} onEditCard={onEditCard} />
                </div>
                <div className="shot-body">
                  <p className="shot-desc" title={shot.visual_description}>{shot.visual_description}</p>
                  <div className="shot-meta-row">
                    <span className="shot-meta-chip">{cameraAngleLabel(shot.camera_angle)}</span>
                    <span className="shot-meta-chip">{cameraMovementLabel(shot.camera_movement)}</span>
                    {transitionLabel(shot.transition_to_next) && (
                      <span className="shot-meta-chip">转场 · {transitionLabel(shot.transition_to_next)}</span>
                    )}
                  </div>
                  {shot.video_prompt && <PromptBlock label="Video Prompt" text={shot.video_prompt} />}
                </div>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

// ── Main renderer ────────────────────────────────

export function renderArtifactContent(
  tabId: string,
  artifactData: ArtifactData,
  onEditCard?: EditCardHandler,
): React.ReactNode {
  switch (tabId) {
    case "outline": return renderOutline(artifactData);
    case "characters": return renderCharacters(artifactData, onEditCard);
    case "scenes": return renderScenes(artifactData, onEditCard);
    case "art_style": return renderArtStyle(artifactData);
    case "script": return renderScript(artifactData);
    case "storyboard": return renderStoryboard(artifactData, onEditCard);
    default: return <EmptyState text="选择一个标签页查看内容" />;
  }
}
