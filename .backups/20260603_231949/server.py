import asyncio
import json
import uuid
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import StreamingResponse, HTMLResponse, FileResponse
from pydantic import BaseModel
from typing import List, Optional

from openai import OpenAI

from processor import process_video, load_config, save_config, format_time

executor = ThreadPoolExecutor(max_workers=3)
jobs: dict[str, asyncio.Queue] = {}

app = FastAPI(title="YouTube Digest")

# Resolve paths relative to this script's location so the server can be
# launched from any working directory.
BASE_DIR = Path(__file__).parent
output_dir = BASE_DIR / "output"
output_dir.mkdir(exist_ok=True)
INDEX_HTML = BASE_DIR / "templates" / "index.html"


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(INDEX_HTML.read_text(encoding="utf-8"))


@app.get("/config")
async def get_config():
    try:
        config = load_config(str(BASE_DIR / "config.yaml"))
        api_key = config["deepseek"]["api_key"]
        return {
            "api_key": api_key if api_key != "YOUR_DEEPSEEK_API_KEY" else "",
            "vault_path": config["obsidian"]["vault_path"],
            "folder": config["obsidian"].get("folder", "YouTube笔记"),
            "chunk_minutes": config["processing"].get("chunk_minutes", 10),
            "cookies_browser": config["processing"].get("cookies_browser", ""),
        }
    except Exception as e:
        return {"error": str(e)}


class ConfigUpdate(BaseModel):
    api_key: str = ""
    vault_path: str = ""
    folder: str = "YouTube笔记"
    chunk_minutes: int = 10
    cookies_browser: str = ""


@app.post("/config")
async def update_config(body: ConfigUpdate):
    try:
        updates = {
            "api_key": body.api_key,
            "vault_path": body.vault_path,
            "folder": body.folder,
            "chunk_minutes": body.chunk_minutes,
            "cookies_browser": body.cookies_browser,
        }
        save_config(updates, str(BASE_DIR / "config.yaml"))
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


class ProcessRequest(BaseModel):
    urls: List[str]
    chunk_minutes: Optional[int] = None


@app.post("/process")
async def process(req: ProcessRequest):
    job_id = str(uuid.uuid4())[:8]
    queue: asyncio.Queue = asyncio.Queue()
    jobs[job_id] = queue

    loop = asyncio.get_event_loop()

    async def run():
        config = load_config(str(BASE_DIR / "config.yaml"))
        # Make html_dir absolute so it works regardless of cwd
        config["output"]["html_dir"] = str(output_dir)
        results = []
        errors = []

        urls = [u.strip() for u in req.urls if u.strip()]
        for i, url in enumerate(urls):
            # 视频之间留点间隔，避免一股脑打 YouTube 触发限流/反爬
            if i > 0:
                await queue.put({"type": "info", "message": "   ⏸ 间隔 5s（避免被 YouTube 限流）…"})
                await asyncio.sleep(5)

            await queue.put({"type": "start", "url": url, "message": f"▶ 开始处理：{url}"})

            def progress_cb(event, _loop=loop, _q=queue):
                _loop.call_soon_threadsafe(_q.put_nowait, event)

            try:
                result = await loop.run_in_executor(
                    executor,
                    lambda u=url, cb=progress_cb: process_video(u, config, cb, req.chunk_minutes),
                )
                results.append(result)
                await queue.put({"type": "video_done", "result": result})
            except Exception as e:
                errors.append({"url": url, "error": str(e)})
                await queue.put({
                    "type": "video_error",
                    "url": url,
                    "message": f"❌ 处理失败：{e}",
                })

        await queue.put({"type": "done", "results": results, "errors": errors})

    asyncio.create_task(run())
    return {"job_id": job_id}


@app.get("/stream/{job_id}")
async def stream(job_id: str):
    async def event_gen():
        queue = jobs.get(job_id)
        if not queue:
            yield f"data: {json.dumps({'type': 'error', 'message': 'Job not found'})}\n\n"
            return
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=120)
                except asyncio.TimeoutError:
                    yield f"data: {json.dumps({'type': 'ping'})}\n\n"
                    continue
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                if event.get("type") in ("done", "error"):
                    break
        finally:
            jobs.pop(job_id, None)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class AskRequest(BaseModel):
    video_id: str
    section: int
    question: str


@app.post("/ask")
async def ask(body: AskRequest):
    """基于该视频该段（及相邻段）的字幕原文，让 DeepSeek 回答追问。只复述/解释原片内容。"""
    tpath = output_dir / "transcripts" / f"{body.video_id}.json"
    if not tpath.exists():
        return {"ok": False, "error": "找不到该视频的字幕存档（可能是旧笔记，重跑一次即可启用追问）"}
    try:
        store = json.loads(tpath.read_text(encoding="utf-8"))
    except Exception as e:
        return {"ok": False, "error": f"读取字幕失败：{e}"}

    secs = store.get("sections", [])
    if not secs:
        return {"ok": False, "error": "字幕存档为空"}

    idx = max(0, min(body.section, len(secs) - 1))
    # 取目标段 + 前后各一段作上下文，回答更连贯
    lo, hi = max(0, idx - 1), min(len(secs), idx + 2)
    ctx_parts = []
    for s in secs[lo:hi]:
        marker = "【本段】" if s["idx"] == idx else ""
        ctx_parts.append(
            f"{marker}[{format_time(s['start'])}—{format_time(s['end'])}] {s.get('title_cn','')}\n{s['text']}"
        )
    context = "\n\n".join(ctx_parts)[:9000]

    try:
        config = load_config(str(BASE_DIR / "config.yaml"))
        client = OpenAI(api_key=config["deepseek"]["api_key"], base_url=config["deepseek"]["base_url"])

        def _call():
            resp = client.chat.completions.create(
                model=config["deepseek"]["model"],
                messages=[
                    {"role": "system", "content": (
                        "你在帮用户读懂一个 YouTube 视频。下面给你的是该视频某一段（及相邻段）的字幕原文。"
                        "请只依据这些原文回答用户的问题：可以复述、解释、整理原片说了什么，可在需要时引用英文原话。"
                        "如果原文里没有相关信息，就如实说『这段视频里没有提到』，绝对不要编造视频之外的事实。用中文回答。"
                    )},
                    {"role": "user", "content": f"【视频片段字幕】\n{context}\n\n【我的问题】\n{body.question}"},
                ],
                temperature=0.3,
                max_tokens=1200,
            )
            return resp.choices[0].message.content

        answer = await asyncio.get_event_loop().run_in_executor(executor, _call)
        return {"ok": True, "answer": answer}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/library")
async def get_library():
    index_path = output_dir / "index.json"
    if not index_path.exists():
        return []
    try:
        return json.loads(index_path.read_text(encoding="utf-8"))
    except Exception:
        return []


@app.get("/output/{filename}")
async def serve_output(filename: str):
    filepath = output_dir / filename
    if filepath.exists() and filepath.suffix == ".html":
        return FileResponse(str(filepath), media_type="text/html")
    return HTMLResponse("<h1>File not found</h1>", status_code=404)


if __name__ == "__main__":
    import uvicorn
    print("🎬 YouTube Digest 启动中...")
    print("   打开浏览器访问: http://localhost:8000")
    uvicorn.run(app, host="127.0.0.1", port=8000, reload=False)
