import asyncio
import json
import uuid
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, Request, Query
from fastapi.responses import StreamingResponse, HTMLResponse, FileResponse, JSONResponse
from pydantic import BaseModel
from typing import List, Optional

from openai import OpenAI

from processor import process_video, load_config, save_config, format_time, update_meta, delete_entry, list_trash, restore_entry, CATEGORIES

executor = ThreadPoolExecutor(max_workers=3)

# SSE 任务缓冲：键是 job_id，值是 Job（见下）。
jobs: dict[str, "Job"] = {}

PING_INTERVAL = 15   # SSE 心跳间隔（秒）。远小于 WKWebView/系统代理常见的 60s 空闲超时，
                     # 因此即使某一段分析耗时很久、期间没有新进度，连接也不会被掐断。
JOB_GRACE = 300      # 任务完成后事件缓冲再保留多久（秒），让断线重连仍能取回最终结果。
MAX_JOBS = 30        # 缓冲任务数量上限，超出时回收最老的已完成任务，防止内存无限增长。


class Job:
    """一个处理任务的事件缓冲。

    所有进度事件按出现顺序追加进 events，SSE 推送时以其在列表里的下标作为事件 id。
    断线重连时浏览器会自动带上 Last-Event-ID，服务端据此从缓冲里「续传」后续事件——
    不丢已经分析好的段、也不用从头再跑。这样一根长达十几分钟的流被网络抖一下也能自愈，
    而不是像以前那样整单作废。
    """

    def __init__(self):
        self.events: list[dict] = []
        self.done = False
        self._waiters: list[asyncio.Future] = []

    def append(self, event: dict):
        # 必须在事件循环线程调用：run() 里直接调用；worker 线程经 call_soon_threadsafe 调用。
        self.events.append(event)
        if event.get("type") == "done":
            self.done = True
        waiters, self._waiters = self._waiters, []
        for w in waiters:
            if not w.done():
                w.set_result(None)

    async def wait(self, timeout: float):
        """挂起到下一次 append 或超时（超时即用于发心跳）。"""
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._waiters.append(fut)
        try:
            await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            pass
        finally:
            if fut in self._waiters:
                self._waiters.remove(fut)


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
    # 控制内存：超过上限时清掉最老的「已完成」任务（dict 保持插入顺序）。
    if len(jobs) >= MAX_JOBS:
        for jid in [j for j, jb in jobs.items() if jb.done][: len(jobs) - MAX_JOBS + 1]:
            jobs.pop(jid, None)
    job = Job()
    jobs[job_id] = job

    loop = asyncio.get_running_loop()

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
                job.append({"type": "info", "message": "   ⏸ 间隔 5s（避免被 YouTube 限流）…"})
                await asyncio.sleep(5)

            job.append({"type": "start", "url": url, "message": f"▶ 开始处理：{url}"})

            def progress_cb(event, _loop=loop, _job=job):
                _loop.call_soon_threadsafe(_job.append, event)

            try:
                result = await loop.run_in_executor(
                    executor,
                    lambda u=url, cb=progress_cb: process_video(u, config, cb, req.chunk_minutes),
                )
                results.append(result)
                job.append({"type": "video_done", "result": result})
            except Exception as e:
                errors.append({"url": url, "error": str(e)})
                job.append({
                    "type": "video_error",
                    "url": url,
                    "message": f"❌ 处理失败：{e}",
                })

        job.append({"type": "done", "results": results, "errors": errors})
        # 完成后保留一段时间供断线重连取回结果，再回收缓冲。
        loop.call_later(JOB_GRACE, jobs.pop, job_id, None)

    asyncio.create_task(run())
    return {"job_id": job_id}


@app.get("/stream/{job_id}")
async def stream(job_id: str, request: Request, from_: int = Query(0, alias="from")):
    # 续传游标：EventSource 自动重连时会带上 Last-Event-ID（上次收到事件的下标），
    # 据此只发它还没收到的事件；首连没有该头则从 ?from=（默认 0）开始。
    leid = request.headers.get("last-event-id")
    if leid is not None and leid.strip().isdigit():
        start = int(leid) + 1
    else:
        start = from_

    async def event_gen():
        job = jobs.get(job_id)
        if job is None:
            yield f"data: {json.dumps({'type': 'error', 'message': '任务不存在或已过期，请重新开始'}, ensure_ascii=False)}\n\n"
            return
        # 告诉浏览器断线后 3s 自动重连（默认更长）。
        yield "retry: 3000\n\n"
        cursor = max(0, min(start, len(job.events)))
        while True:
            # 先把缓冲里已积累、客户端尚未收到的事件一次性补齐。
            while cursor < len(job.events):
                ev = job.events[cursor]
                yield f"id: {cursor}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"
                cursor += 1
                if ev.get("type") in ("done", "error"):
                    return
            if job.done:
                return
            # 没有新事件就等一小会儿；到点仍无新事件则发心跳，维持连接不被掐。
            await job.wait(PING_INTERVAL)
            if cursor >= len(job.events) and not job.done:
                yield f"data: {json.dumps({'type': 'ping'})}\n\n"

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
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


@app.get("/categories")
async def get_categories():
    return CATEGORIES


class MetaUpdate(BaseModel):
    url: str
    category: str = ""
    tags: List[str] = []


@app.post("/update-meta")
async def update_meta_endpoint(body: MetaUpdate):
    try:
        config = load_config(str(BASE_DIR / "config.yaml"))
        config["output"]["html_dir"] = str(output_dir)
        ok, msg = update_meta(body.url, body.category, body.tags, config)
        return JSONResponse({"ok": ok, "message": msg})
    except Exception as e:
        return JSONResponse({"ok": False, "message": str(e)}, status_code=500)


class DeleteRequest(BaseModel):
    url: str


@app.post("/delete-entry")
async def delete_entry_endpoint(body: DeleteRequest):
    """软删除：把报告 HTML 和 Obsidian 笔记移入各自的 _trash/，并从文章库索引移除。"""
    try:
        config = load_config(str(BASE_DIR / "config.yaml"))
        config["output"]["html_dir"] = str(output_dir)
        ok, msg = delete_entry(body.url, config)
        return JSONResponse({"ok": ok, "message": msg})
    except Exception as e:
        return JSONResponse({"ok": False, "message": str(e)}, status_code=500)


@app.get("/trash")
async def get_trash():
    """列出回收站里的笔记。"""
    try:
        config = load_config(str(BASE_DIR / "config.yaml"))
        config["output"]["html_dir"] = str(output_dir)
        return list_trash(config)
    except Exception:
        return []


class RestoreRequest(BaseModel):
    id: str


@app.post("/restore-entry")
async def restore_entry_endpoint(body: RestoreRequest):
    """从回收站恢复一篇笔记到文章库。"""
    try:
        config = load_config(str(BASE_DIR / "config.yaml"))
        config["output"]["html_dir"] = str(output_dir)
        ok, msg = restore_entry(body.id, config)
        return JSONResponse({"ok": ok, "message": msg})
    except Exception as e:
        return JSONResponse({"ok": False, "message": str(e)}, status_code=500)


@app.post("/reveal-trash")
async def reveal_trash():
    """在访达里打开回收站文件夹（output/_trash）。"""
    trash = (output_dir / "_trash").resolve()
    try:
        trash.mkdir(parents=True, exist_ok=True)
        subprocess.run(["open", str(trash)], check=False)
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "message": str(e)}, status_code=500)


class RevealRequest(BaseModel):
    filename: str


@app.post("/reveal")
async def reveal(body: RevealRequest):
    """在访达里定位 HTML 报告文件，方便分享（微信发给朋友手机浏览器打开）。"""
    filepath = (output_dir / body.filename).resolve()
    try:
        if filepath.parent != output_dir.resolve() or not filepath.exists():
            return JSONResponse({"ok": False, "message": "文件不存在"}, status_code=404)
        subprocess.run(["open", "-R", str(filepath)], check=False)
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "message": str(e)}, status_code=500)


@app.post("/open-dashboard")
async def open_dashboard():
    """打开笔记中心总览页。"""
    dash = (BASE_DIR.parent / "dashboard" / "index.html").resolve()
    try:
        if not dash.exists():
            return JSONResponse({"ok": False, "message": "未找到笔记中心首页"}, status_code=404)
        subprocess.run(["open", str(dash)], check=False)
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "message": str(e)}, status_code=500)


if __name__ == "__main__":
    import uvicorn
    print("🎬 YouTube Digest 启动中...")
    print("   打开浏览器访问: http://localhost:8000")
    uvicorn.run(app, host="127.0.0.1", port=8000, reload=False)
