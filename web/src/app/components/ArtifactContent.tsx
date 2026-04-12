import React from "react";
import { FileText, Users, Map, Palette, Film, Eye } from "lucide-react";

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

// ── Sub-components ───────────────────────────────

function EmptyState({ text }: { text: string }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", height: "100%", color: "var(--text-tertiary)", gap: 12, padding: 40 }}>
      <FileText size={40} opacity={0.3} />
      <p style={{ fontSize: 14 }}>{text}</p>
      <p style={{ fontSize: 12 }}>
        {text.includes("大纲") ? "先在左侧输入故事想法并点击生成" : "该部分内容将在 Pipeline 完成后显示"}
      </p>
    </div>
  );
}

function InfoRow({ label, value, highlight }: { label: string; value: string; highlight?: boolean }) {
  if (!value) return null;
  return (
    <div style={{ display: "flex", justifyContent: "space-between", fontSize: 13, padding: "4px 0", borderBottom: "1px solid var(--border-default)" }}>
      <span style={{ color: "var(--text-secondary)" }}>{label}</span>
      <span style={{ color: highlight ? "var(--brand-primary)" : "var(--text-primary)", fontWeight: highlight ? 500 : 400 }}>{value}</span>
    </div>
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
      )}
    </div>
  );
}

function renderCharacters(data: ArtifactData) {
  if (!data.characters?.length) return <EmptyState text="暂无角色数据" />;
  return (
    <div className="artifact-content grid-cards">
      {data.characters.map((char: any, i: number) => (
        <div key={i} className="card character-card">
          <div className="card-header">
            <Users size={16} />
            <span>{char.name}</span>
            <span className={`role-tag role-${char.role}`}>{char.role}</span>
          </div>
          <div className="card-body">
            {char.appearance && <p className="text-sm muted">{char.appearance}</p>}
            {char.personality && <p className="text-sm"><strong>性格:</strong> {char.personality}</p>}
            {char.motivation && <p className="text-sm"><strong>动机:</strong> {char.motivation}</p>}
            {char.image_prompt && (
              <div className="prompt-box"><code className="text-xs">{char.image_prompt.slice(0, 120)}...</code></div>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}

function renderScenes(data: ArtifactData) {
  if (!data.scenes?.length) return <EmptyState text="暂无场景数据" />;
  return (
    <div className="artifact-content grid-cards">
      {data.scenes.map((scene: any, i: number) => (
        <div key={i} className="card scene-card">
          <div className="card-header">
            <Map size={16} />
            <span>{scene.name}</span>
          </div>
          <div className="card-body">
            <InfoRow label="类型" value={scene.location_type} />
            <InfoRow label="时间" value={scene.time_of_day} />
            <InfoRow label="氛围" value={scene.mood} highlight />
            {scene.environment && <p className="text-sm muted mt-1">{scene.environment.slice(0, 150)}...</p>}
            {scene.color_palette?.length > 0 && (
              <div className="color-swatches">
                {scene.color_palette.map((c: string, j: number) => (
                  <span key={j} className="color-dot" style={{ background: c }} />
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
        <p className="highlight-text">{s.overall_style}</p>
      </div>
      <div className="grid-2col">
        <div className="info-card">
          <h5>主色调</h5>
          <div className="color-swatches">
            {(s.color_palette_primary || []).map((c: string, i: number) => (
              <span key={i} className="color-dot large" style={{ background: c }} />
            ))}
          </div>
        </div>
        <div className="info-card">
          <h5>辅助色</h5>
          <div className="color-swatches">
            {(s.color_palette_secondary || []).map((c: string, i: number) => (
              <span key={i} className="color-dot large" style={{ background: c }} />
            ))}
          </div>
        </div>
      </div>
      <div className="info-card"><h5>光影风格</h5><p>{s.lighting_style}</p></div>
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

function renderStoryboard(data: ArtifactData) {
  if (!data.visual_highlights?.length && !data.storyboard)
    return <EmptyState text="暂无影像亮点数据" />;

  return (
    <div className="artifact-content">
      {data.visual_highlights?.length > 0 && (
        <div className="info-card">
          <h4><Eye size={16} style={{ display: "inline", marginRight: 6 }} />影像亮点</h4>
          {data.visual_highlights.map((vh: any, i: number) => (
            <div key={i} className="highlight-item">
              <span className="highlight-title">{vh.title}</span>
              <p className="highlight-desc">{vh.description}</p>
              {vh.visual_technique && <span className="highlight-tech">{vh.visual_technique}</span>}
            </div>
          ))}
        </div>
      )}

      {data.storyboard && (
        <>
          <div className="sb-header">
            <h4><Film size={16} style={{ display: "inline", marginRight: 6 }} />分镜脚本</h4>
            <span className="badge">{(data.storyboard as any).total_shot_count} 镜头 · ~{Math.round((data.storyboard as any).total_estimated_duration)}s</span>
            <span className="badge muted">{(data.storyboard as any).aspect_ratio} · {(data.storyboard as any).fps}fps</span>
          </div>
          <div className="shots-grid">
            {(data.storyboard as any).shots?.slice(0, 20).map((shot: any, i: number) => (
              <div key={i} className="shot-card">
                <div className="shot-header-row">
                  <span className="shot-id">{shot.shot_id?.slice(-6)}</span>
                  <span className="shot-size">{shotSizeLabel(shot.shot_size)}</span>
                  <span className="shot-duration">{shot.duration_seconds}s</span>
                </div>
                <p className="shot-desc">{shot.visual_description?.slice(0, 120)}</p>
                <div className="shot-meta-row">
                  <span>{cameraAngleLabel(shot.camera_angle)}</span>
                  <span>{cameraMovementLabel(shot.camera_movement)}</span>
                  <span>{shot.transition_to_next}</span>
                </div>
                {shot.video_prompt && (
                  <div className="prompt-box">
                    <span className="prompt-label">Video Prompt:</span>
                    <code className="text-xs">{shot.video_prompt.slice(0, 150)}...</code>
                  </div>
                )}
              </div>
            ))}
          </div>
          {(data.storyboard as any).shots?.length > 20 && (
            <p className="text-center muted text-sm mt-2">... 还有 {(data.storyboard as any).shots.length - 20} 个镜头</p>
          )}
        </>
      )}
    </div>
  );
}

// ── Main renderer ────────────────────────────────

export function renderArtifactContent(tabId: string, artifactData: ArtifactData): React.ReactNode {
  switch (tabId) {
    case "outline": return renderOutline(artifactData);
    case "characters": return renderCharacters(artifactData);
    case "scenes": return renderScenes(artifactData);
    case "art_style": return renderArtStyle(artifactData);
    case "script": return renderScript(artifactData);
    case "storyboard": return renderStoryboard(artifactData);
    default: return <EmptyState text="选择一个标签页查看内容" />;
  }
}
