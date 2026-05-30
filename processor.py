import json
import re
import html
import os
from pathlib import Path
from datetime import datetime
import yaml
from youtube_transcript_api import YouTubeTranscriptApi
from openai import OpenAI
import yt_dlp


def load_config(config_path="config.yaml"):
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _strip_quotes(s):
    """Remove accidental surrounding quotes a user might type in the UI."""
    s = s.strip()
    for q in ("'''", '"""', '"', "'"):
        if s.startswith(q) and s.endswith(q) and len(s) > len(q) * 2:
            s = s[len(q):-len(q)].strip()
            break
    return s


def save_config(updates, config_path="config.yaml"):
    config = load_config(config_path)
    if "api_key" in updates:
        config["deepseek"]["api_key"] = updates["api_key"].strip()
    if "vault_path" in updates:
        config["obsidian"]["vault_path"] = _strip_quotes(updates["vault_path"])
    if "chunk_minutes" in updates:
        config["processing"]["chunk_minutes"] = int(updates["chunk_minutes"])
    with open(config_path, "w", encoding="utf-8") as f:
        yaml.dump(config, f, allow_unicode=True, default_flow_style=False)


def extract_video_id(url):
    patterns = [
        r"(?:v=)([a-zA-Z0-9_-]{11})",
        r"(?:youtu\.be/)([a-zA-Z0-9_-]{11})",
        r"(?:embed/)([a-zA-Z0-9_-]{11})",
        r"(?:shorts/)([a-zA-Z0-9_-]{11})",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def get_video_metadata(url):
    ydl_opts = {"quiet": True, "no_warnings": True}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        return {
            "title": info.get("title", "Unknown"),
            "channel": info.get("uploader", "Unknown"),
            "duration": info.get("duration", 0),
            "upload_date": info.get("upload_date", ""),
            "url": url,
            "thumbnail": info.get("thumbnail", ""),
            "video_id": info.get("id", ""),
        }


def get_transcript(video_id):
    # v1.x: instantiate first, then call api.list()
    api = YouTubeTranscriptApi()
    transcript_list = api.list(video_id)

    # Prefer manual English, then auto English, then anything available
    for finder in [
        lambda tl: tl.find_manually_created_transcript(["en", "en-US", "en-GB"]),
        lambda tl: tl.find_generated_transcript(["en", "en-US", "en-GB"]),
        lambda tl: next(iter(tl)),
    ]:
        try:
            t = finder(transcript_list)
            return t.fetch(), t.language_code
        except Exception:
            continue

    raise Exception("No transcript available for this video")


def _entry_attr(entry, key, default=0):
    """Compat helper: supports both v0.x (dict) and v1.x (object) entries."""
    if hasattr(entry, key):
        return getattr(entry, key)
    try:
        return entry[key]
    except (KeyError, TypeError):
        return default


def chunk_transcript(transcript, chunk_seconds=600):
    if not transcript:
        return []

    chunks = []
    current = []
    chunk_start = _entry_attr(transcript[0], "start")
    boundary = chunk_start + chunk_seconds

    for entry in transcript:
        start = _entry_attr(entry, "start")
        if start >= boundary and current:
            text = " ".join(_entry_attr(e, "text", "").replace("\n", " ") for e in current)
            chunks.append({"start": chunk_start, "end": start, "text": text})
            current = [entry]
            chunk_start = start
            boundary = start + chunk_seconds
        else:
            current.append(entry)

    if current:
        last = current[-1]
        text = " ".join(_entry_attr(e, "text", "").replace("\n", " ") for e in current)
        chunks.append({
            "start": chunk_start,
            "end": _entry_attr(last, "start") + _entry_attr(last, "duration"),
            "text": text,
        })

    return chunks


def format_time(seconds):
    if not seconds:
        return "0:00"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def safe_filename(title):
    clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", title).strip(". ")
    return clean[:80] if clean else "untitled"


def analyze_chunk(client, model, chunk, video_title, idx, total):
    start_str = format_time(chunk["start"])
    end_str = format_time(chunk["end"])
    text = chunk["text"][:4000]

    prompt = f"""请分析以下视频片段的文字稿（视频：《{video_title}》，时间段：{start_str}—{end_str}，第{idx+1}/{total}段）：

---
{text}
---

请以JSON格式返回以下字段：
- title_cn: 本段中文标题（10字以内）
- title_en: 本段英文标题（简短）
- summary_cn: 本段中文摘要（3-5句话，捕捉主要观点和论证逻辑）
- key_quotes: 最重要的原文引用（1-3条，保留原文，不翻译；若无则 []）
- research: 提到的研究、实验、调查或数据（每条30字内，中文；若无则 []）
- examples: 提到的例子、类比、故事或案例（每条30字内，中文；若无则 []）
- concepts: 出现的专业概念或术语（格式："中文（English）"；若无则 []）

只返回JSON，不要其他文字。"""

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "你是专业的内容分析师，擅长分析播客、演讲和学术对谈。请严格按JSON格式输出，不要有任何前缀或说明文字。"},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
        response_format={"type": "json_object"},
        max_tokens=1500,
    )

    try:
        return json.loads(response.choices[0].message.content)
    except json.JSONDecodeError:
        return {
            "title_cn": f"第{idx+1}段",
            "title_en": f"Section {idx+1}",
            "summary_cn": response.choices[0].message.content[:300],
            "key_quotes": [], "research": [], "examples": [], "concepts": [],
        }


def generate_overall_summary(client, model, video_title, sections):
    lines = []
    for i, s in enumerate(sections):
        a = s["analysis"]
        lines.append(
            f"第{i+1}段（{format_time(s['chunk']['start'])}—{format_time(s['chunk']['end'])}）：{a.get('title_cn','')}\n{a.get('summary_cn','')}"
        )

    prompt = f"""基于视频《{video_title}》的分段摘要，生成整体分析：

{chr(10).join(lines)}

请以JSON格式返回：
- overall_summary: 整体内容概述（2-3段，中文，每段约100字）
- main_themes: 主要话题或主题（5-8条，每条一句话，中文）
- key_takeaways: 最重要的观点/启示/结论（5-10条，每条一句话，中文）
- all_research: 汇总全部研究/实验/数据（去重，中文；若无则 []）
- all_examples: 汇总全部例子/故事/案例（去重，中文；若无则 []）
- all_concepts: 汇总全部关键概念（去重，中英双语；若无则 []）

只返回JSON，不要其他文字。"""

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "你是专业的内容分析师。请严格按JSON格式输出。"},
            {"role": "user", "content": prompt},
        ],
        temperature=0.3,
        response_format={"type": "json_object"},
        max_tokens=2000,
    )

    try:
        return json.loads(response.choices[0].message.content)
    except Exception:
        return {
            "overall_summary": "", "main_themes": [], "key_takeaways": [],
            "all_research": [], "all_examples": [], "all_concepts": [],
        }


# ── Markdown output ──────────────────────────────────────────────────────────

def generate_markdown(metadata, sections, overall):
    title = metadata["title"]
    date = datetime.now().strftime("%Y-%m-%d")
    dur = format_time(metadata["duration"])

    parts = [
        f'---',
        f'title: "{title.replace(chr(34), chr(39))}"',
        f'channel: "{metadata["channel"]}"',
        f'url: "{metadata["url"]}"',
        f'date: {date}',
        f'duration: "{dur}"',
        f'tags: [youtube, video-notes]',
        f'---',
        f'',
        f'# {title}',
        f'',
        f'> **频道**：{metadata["channel"]}  ',
        f'> **链接**：{metadata["url"]}  ',
        f'> **时长**：{dur}',
        f'',
        f'## 整体概述',
        f'',
        overall.get("overall_summary", ""),
        f'',
        f'## 主要话题',
        f'',
    ]
    for t in overall.get("main_themes", []):
        parts.append(f"- {t}")
    parts += ["", "## 核心要点", ""]
    for t in overall.get("key_takeaways", []):
        parts.append(f"- {t}")

    if overall.get("all_research"):
        parts += ["", "## 研究与数据", ""]
        for r in overall["all_research"]:
            parts.append(f"- {r}")
    if overall.get("all_examples"):
        parts += ["", "## 例子与故事", ""]
        for e in overall["all_examples"]:
            parts.append(f"- {e}")
    if overall.get("all_concepts"):
        parts += ["", "## 关键概念", ""]
        for c in overall["all_concepts"]:
            parts.append(f"- {c}")

    parts += ["", "---", "", f"## 分段详解（共{len(sections)}段）", ""]

    for i, s in enumerate(sections):
        chunk, a = s["chunk"], s["analysis"]
        start_str = format_time(chunk["start"])
        end_str = format_time(chunk["end"])
        parts += [
            f'### {i+1}. {a.get("title_cn", "")}　`{start_str} — {end_str}`',
            f'',
            f'*{a.get("title_en", "")}*',
            f'',
            a.get("summary_cn", ""),
            f'',
        ]
        for q in a.get("key_quotes", []):
            parts.append(f"> {q}")
        if a.get("key_quotes"):
            parts.append("")
        if a.get("research"):
            parts += ["**研究 / 数据**", ""]
            for r in a["research"]:
                parts.append(f"- {r}")
            parts.append("")
        if a.get("examples"):
            parts += ["**例子 / 故事**", ""]
            for e in a["examples"]:
                parts.append(f"- {e}")
            parts.append("")
        if a.get("concepts"):
            parts += ["**关键概念**", ""]
            for c in a["concepts"]:
                parts.append(f"- {c}")
            parts.append("")

    return "\n".join(parts)


# ── HTML output ───────────────────────────────────────────────────────────────

def generate_html(metadata, sections, overall):
    title = html.escape(metadata["title"])
    channel = html.escape(metadata["channel"])
    date = datetime.now().strftime("%Y-%m-%d")
    dur = format_time(metadata["duration"])
    video_url = html.escape(metadata["url"])

    # TOC
    toc_items = []
    for i, s in enumerate(sections):
        ts = format_time(s["chunk"]["start"])
        t_cn = html.escape(s["analysis"].get("title_cn", f"第{i+1}段"))
        toc_items.append(f'<li><a href="#sec-{i+1}"><span class="toc-ts">{ts}</span>{t_cn}</a></li>')
    toc_html = "\n".join(toc_items)

    # Section cards
    cards = []
    for i, s in enumerate(sections):
        chunk, a = s["chunk"], s["analysis"]
        start_str = format_time(chunk["start"])
        end_str = format_time(chunk["end"])
        ts_sec = int(chunk["start"])

        t_cn = html.escape(a.get("title_cn", ""))
        t_en = html.escape(a.get("title_en", ""))
        summary = html.escape(a.get("summary_cn", ""))

        quotes_html = "".join(
            f'<blockquote class="quote">{html.escape(q)}</blockquote>'
            for q in a.get("key_quotes", [])
        )

        def tag_block(items, css_class, label):
            if not items:
                return ""
            lis = "".join(f"<li>{html.escape(x)}</li>" for x in items)
            return f'<div class="tag-block {css_class}"><div class="tb-label">{label}</div><ul>{lis}</ul></div>'

        research_html = tag_block(a.get("research", []), "research", "🔬 研究 / 数据")
        examples_html = tag_block(a.get("examples", []), "examples", "💡 例子 / 故事")

        concept_tags = "".join(
            f'<span class="ctag">{html.escape(c)}</span>'
            for c in a.get("concepts", [])
        )
        concepts_html = (
            f'<div class="tag-block concepts"><div class="tb-label">🏷 关键概念</div>'
            f'<div class="ctags">{concept_tags}</div></div>'
            if concept_tags else ""
        )

        sep = "&" if "?" in metadata["url"] else "?"
        yt_link = html.escape(f'{metadata["url"]}{sep}t={ts_sec}s')

        cards.append(f"""
<section id="sec-{i+1}" class="sec-card">
  <div class="sec-head">
    <span class="sec-num">{i+1:02d}</span>
    <div class="sec-titles">
      <h2>{t_cn}</h2>
      <div class="sec-en">{t_en}</div>
    </div>
    <a class="ts-link" href="{yt_link}" target="_blank">{start_str} → {end_str}</a>
  </div>
  <p class="sec-summary">{summary}</p>
  {quotes_html}
  {research_html}
  {examples_html}
  {concepts_html}
</section>""")

    sections_html = "\n".join(cards)

    overall_text = html.escape(overall.get("overall_summary", "")).replace("\n", "<br>")
    themes_html = "".join(f"<li>{html.escape(t)}</li>" for t in overall.get("main_themes", []))
    takeway_html = "".join(f"<li>{html.escape(t)}</li>" for t in overall.get("key_takeaways", []))
    all_research = "".join(f"<li>{html.escape(r)}</li>" for r in overall.get("all_research", []))
    all_examples = "".join(f"<li>{html.escape(e)}</li>" for e in overall.get("all_examples", []))
    all_concepts = "".join(f'<span class="ctag">{html.escape(c)}</span>' for c in overall.get("all_concepts", []))

    research_card = f'<div class="ov-card full"><div class="ov-card-title">研究 / 数据</div><ul>{all_research}</ul></div>' if all_research else ""
    examples_card = f'<div class="ov-card full"><div class="ov-card-title">例子 / 故事</div><ul>{all_examples}</ul></div>' if all_examples else ""
    concepts_card = f'<div class="ov-card full"><div class="ov-card-title">关键概念</div><div class="ctags">{all_concepts}</div></div>' if all_concepts else ""

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
:root{{
  --navy:#1a3a5c;--blue:#2563eb;--blue-lt:#3b82f6;
  --bg:#f0f4f8;--card:#fff;--border:#dde4ed;
  --text:#1e293b;--muted:#64748b;
  --research-bg:#eff6ff;--example-bg:#f0fdf4;
}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;background:var(--bg);color:var(--text);line-height:1.7}}
a{{color:var(--blue-lt);text-decoration:none}}
a:hover{{text-decoration:underline}}
.layout{{display:flex;min-height:100vh}}
.sidebar{{
  width:256px;min-width:256px;background:var(--navy);color:#fff;
  position:sticky;top:0;height:100vh;overflow-y:auto;
  display:flex;flex-direction:column;scrollbar-width:thin;
  scrollbar-color:rgba(255,255,255,.2) transparent
}}
.sb-head{{padding:22px 18px 14px;border-bottom:1px solid rgba(255,255,255,.1)}}
.sb-head h1{{font-size:13.5px;font-weight:600;line-height:1.5;color:#fff}}
.sb-meta{{font-size:11px;color:rgba(255,255,255,.5);margin-top:6px}}
.toc-lbl{{font-size:10px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:rgba(255,255,255,.35);padding:14px 18px 6px}}
.toc ol{{list-style:none;padding:0 0 20px}}
.toc ol li a{{display:block;padding:6px 18px;color:rgba(255,255,255,.65);font-size:12.5px;transition:.15s}}
.toc ol li a:hover,.toc ol li a.active{{background:rgba(255,255,255,.12);color:#fff;text-decoration:none}}
.toc-ts{{font-family:monospace;font-size:10px;color:var(--blue-lt);margin-right:5px}}
.main{{flex:1;padding:40px 48px;max-width:860px}}
.hero{{margin-bottom:36px;padding-bottom:28px;border-bottom:2px solid var(--border)}}
.hero h1{{font-size:24px;font-weight:700;color:var(--navy);line-height:1.35;margin-bottom:12px}}
.hero-meta{{display:flex;flex-wrap:wrap;gap:14px;font-size:13px;color:var(--muted)}}
.ov-grid{{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:36px}}
.ov-card{{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:18px 22px}}
.ov-card.full{{grid-column:span 2}}
.ov-card-title{{font-size:11px;font-weight:700;letter-spacing:.07em;text-transform:uppercase;color:var(--navy);margin-bottom:10px}}
.ov-card p{{font-size:14.5px;line-height:1.75}}
.ov-card ul{{list-style:none;padding:0}}
.ov-card ul li{{font-size:13.5px;padding:4px 0 4px 14px;position:relative}}
.ov-card ul li::before{{content:"→";position:absolute;left:0;color:var(--blue-lt);font-size:11px}}
.ctags{{display:flex;flex-wrap:wrap;gap:6px}}
.ctag{{background:var(--navy);color:#fff;font-size:12px;padding:3px 10px;border-radius:20px}}
.sec-divider{{font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin-bottom:20px;padding-bottom:10px;border-bottom:1px solid var(--border)}}
.sec-card{{background:var(--card);border:1px solid var(--border);border-left:4px solid var(--blue);border-radius:10px;padding:24px 28px;margin-bottom:16px}}
.sec-head{{display:flex;align-items:flex-start;gap:14px;margin-bottom:14px}}
.sec-num{{font-family:monospace;font-size:20px;font-weight:700;color:var(--navy);min-width:28px;padding-top:2px}}
.sec-titles{{flex:1}}
.sec-titles h2{{font-size:17px;font-weight:600;color:var(--navy)}}
.sec-en{{font-size:12.5px;color:var(--muted);margin-top:2px}}
.ts-link{{font-family:monospace;font-size:11.5px;color:var(--blue-lt);white-space:nowrap;padding:4px 10px;background:#eff6ff;border-radius:6px;transition:.15s}}
.ts-link:hover{{background:var(--blue);color:#fff;text-decoration:none}}
.sec-summary{{font-size:14.5px;line-height:1.8;margin-bottom:12px}}
blockquote.quote{{border-left:3px solid var(--blue-lt);padding:8px 14px;background:#f8faff;border-radius:0 8px 8px 0;font-size:13.5px;font-style:italic;color:var(--muted);margin:10px 0}}
.tag-block{{margin-top:12px;padding:12px 16px;border-radius:8px}}
.tag-block ul{{list-style:none;padding:0}}
.tag-block ul li{{font-size:13px;padding:2px 0 2px 12px;position:relative}}
.tag-block ul li::before{{content:"•";position:absolute;left:0}}
.tb-label{{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;margin-bottom:7px}}
.tag-block.research{{background:var(--research-bg);border:1px solid #bfdbfe}}
.tag-block.research .tb-label{{color:#1d4ed8}}
.tag-block.examples{{background:var(--example-bg);border:1px solid #bbf7d0}}
.tag-block.examples .tb-label{{color:#15803d}}
.tag-block.concepts .tb-label{{color:var(--navy)}}
</style>
</head>
<body>
<div class="layout">
  <nav class="sidebar">
    <div class="sb-head">
      <h1>{title}</h1>
      <div class="sb-meta">{channel} · {dur} · {date}</div>
    </div>
    <div class="toc-lbl">目录</div>
    <nav class="toc"><ol>{toc_html}</ol></nav>
  </nav>
  <main class="main">
    <div class="hero">
      <h1>{title}</h1>
      <div class="hero-meta">
        <span>📺 {channel}</span>
        <span>⏱ {dur}</span>
        <span>📅 {date}</span>
        <span><a href="{video_url}" target="_blank">▶ 观看原视频</a></span>
      </div>
    </div>
    <div class="ov-grid">
      <div class="ov-card full">
        <div class="ov-card-title">整体概述</div>
        <p>{overall_text}</p>
      </div>
      <div class="ov-card">
        <div class="ov-card-title">主要话题</div>
        <ul>{themes_html}</ul>
      </div>
      <div class="ov-card">
        <div class="ov-card-title">核心要点</div>
        <ul>{takeway_html}</ul>
      </div>
      {research_card}
      {examples_card}
      {concepts_card}
    </div>
    <div class="sec-divider">分段详解 · {len(sections)} 个片段</div>
    {sections_html}
  </main>
</div>
<script>
const secs=document.querySelectorAll('.sec-card');
const links=document.querySelectorAll('.toc a');
const obs=new IntersectionObserver(entries=>{{
  entries.forEach(e=>{{
    if(e.isIntersecting){{
      links.forEach(l=>l.classList.remove('active'));
      const a=document.querySelector('.toc a[href="#'+e.target.id+'"]');
      if(a)a.classList.add('active');
    }}
  }});
}},{{threshold:0.4}});
secs.forEach(s=>obs.observe(s));
</script>
</body>
</html>"""


# ── File I/O ──────────────────────────────────────────────────────────────────

def save_to_obsidian(markdown_content, metadata, config):
    raw_path = _strip_quotes(str(config["obsidian"]["vault_path"]))
    vault = Path(os.path.expanduser(raw_path))
    folder = config["obsidian"].get("folder", "YouTube笔记")
    target = vault / folder
    target.mkdir(parents=True, exist_ok=True)
    filepath = target / (safe_filename(metadata["title"]) + ".md")
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(markdown_content)
    return str(filepath)


def save_html_file(html_content, metadata, config):
    output_dir = Path(config["output"].get("html_dir", "./output"))
    output_dir.mkdir(parents=True, exist_ok=True)
    prefix = datetime.now().strftime("%Y%m%d")
    filename = f"{prefix}_{safe_filename(metadata['title'])}.html"
    filepath = output_dir / filename
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html_content)
    return str(filepath), filename


# ── Main pipeline ─────────────────────────────────────────────────────────────

def process_video(url, config, progress_cb=None):
    def log(msg, level="info"):
        if progress_cb:
            progress_cb({"type": level, "message": msg})

    log("获取视频信息...")
    video_id = extract_video_id(url)
    if not video_id:
        raise ValueError(f"无法解析视频 ID：{url}")

    metadata = get_video_metadata(url)
    log(f'✅ 视频：{metadata["title"]}')
    log(f'   频道：{metadata["channel"]}，时长：{format_time(metadata["duration"])}')

    log("获取字幕...")
    transcript, lang = get_transcript(video_id)
    log(f"✅ 字幕获取成功（{lang}），共 {len(transcript)} 条")

    chunk_mins = config["processing"].get("chunk_minutes", 10)
    chunks = chunk_transcript(transcript, chunk_seconds=chunk_mins * 60)
    log(f"✅ 切分完成，共 {len(chunks)} 段（每段约 {chunk_mins} 分钟）")

    client = OpenAI(
        api_key=config["deepseek"]["api_key"],
        base_url=config["deepseek"]["base_url"],
    )
    model = config["deepseek"]["model"]

    sections = []
    for i, chunk in enumerate(chunks):
        log(f"分析第 {i+1}/{len(chunks)} 段（{format_time(chunk['start'])} — {format_time(chunk['end'])}）...")
        analysis = analyze_chunk(client, model, chunk, metadata["title"], i, len(chunks))
        sections.append({"chunk": chunk, "analysis": analysis})

    log("生成整体总结...")
    overall = generate_overall_summary(client, model, metadata["title"], sections)
    log("✅ 整体总结完成")

    log("生成输出文件...")
    md_content = generate_markdown(metadata, sections, overall)
    html_content = generate_html(metadata, sections, overall)

    md_path = None
    try:
        md_path = save_to_obsidian(md_content, metadata, config)
        log(f"✅ Obsidian 笔记：{md_path}")
    except Exception as e:
        log(f"⚠️  Obsidian 保存失败：{e}", "warn")

    html_path, html_filename = save_html_file(html_content, metadata, config)
    log(f"✅ HTML 报告：{html_path}")

    result = {
        "title": metadata["title"],
        "channel": metadata["channel"],
        "duration": format_time(metadata["duration"]),
        "sections": len(sections),
        "md_path": md_path,
        "html_path": html_path,
        "html_filename": html_filename,
        "url": url,
        "date": datetime.now().strftime("%Y-%m-%d"),
        "summary": overall.get("overall_summary", "")[:200],
    }

    # Append to library index
    _append_library_index(result, config)

    return result


def _append_library_index(result, config):
    """Maintain a persistent index of all processed videos."""
    output_dir = Path(config["output"].get("html_dir", "./output"))
    index_path = output_dir / "index.json"
    try:
        entries = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else []
    except Exception:
        entries = []

    # Deduplicate by url — update if already exists
    entries = [e for e in entries if e.get("url") != result["url"]]
    entries.insert(0, {
        "title": result["title"],
        "channel": result["channel"],
        "duration": result["duration"],
        "sections": result["sections"],
        "html_filename": result["html_filename"],
        "url": result["url"],
        "date": result["date"],
        "summary": result["summary"],
    })

    index_path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
