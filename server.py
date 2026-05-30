import asyncio
import json
import uuid
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import StreamingResponse, HTMLResponse, FileResponse
from pydantic import BaseModel
from typing import List

from processor import process_video, load_config, save_config

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
        }
    except Exception as e:
        return {"error": str(e)}


class ConfigUpdate(BaseModel):
    api_key: str = ""
    vault_path: str = ""
    folder: str = "YouTube笔记"
    chunk_minutes: int = 10


@app.post("/config")
async def update_config(body: ConfigUpdate):
    try:
        updates = {
            "api_key": body.api_key,
            "vault_path": body.vault_path,
            "chunk_minutes": body.chunk_minutes,
        }
        save_config(updates, str(BASE_DIR / "config.yaml"))
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


class ProcessRequest(BaseModel):
    urls: List[str]


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

        for url in req.urls:
            url = url.strip()
            if not url:
                continue

            await queue.put({"type": "start", "url": url, "message": f"▶ 开始处理：{url}"})

            def progress_cb(event, _loop=loop, _q=queue):
                _loop.call_soon_threadsafe(_q.put_nowait, event)

            try:
                result = await loop.run_in_executor(
                    executor,
                    lambda u=url, cb=progress_cb: process_video(u, config, cb),
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
