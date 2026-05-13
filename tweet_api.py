"""推特短视频生成 API
FastAPI 服务：接收推特截图+内容，生成竖屏短视频（AI 增强版）

启动方式:
    uvicorn tweet_api:app --host 0.0.0.0 --port 8000

API 端点:
    POST /generate       - 生成推特短视频（含配音+配乐）
    POST /generate-ai    - 生成推特短视频（AI增强：解说词+Claude配乐+配音+迭代审阅）
    GET  /video/{filename} - 下载/查看已生成的视频
    GET  /health         - 健康检查
"""
import os
import uuid
import shutil
import time
import threading
import asyncio
import concurrent.futures
import multiprocessing
from contextlib import asynccontextmanager
from collections import deque
from typing import Optional
from dotenv import load_dotenv
load_dotenv()
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from agents.tweet_video_agent import TweetVideoAgent
from agents.ai_assistant import get_assistant

_executor: concurrent.futures.ProcessPoolExecutor | None = None
_log_manager: "multiprocessing.managers.SyncManager | None" = None
_log_queue = None
_log_reader_thread: threading.Thread | None = None
_log_reader_stop = threading.Event()


def _drain_log_queue():
    while not _log_reader_stop.is_set():
        try:
            item = _log_queue.get(timeout=0.5)
        except Exception:
            continue
        if item is None:
            break
        msg, level = item
        _vlog(msg, level)


def _kill_executor_children():
    if _executor is None:
        return
    for p in list(getattr(_executor, "_processes", {}).values()):
        try:
            p.terminate()
        except Exception:
            pass


@asynccontextmanager
async def lifespan(app):
    global _executor, _log_queue, _log_manager, _log_reader_thread
    ctx = multiprocessing.get_context("spawn")
    _log_manager = ctx.Manager()
    _log_queue = _log_manager.Queue()
    _executor = concurrent.futures.ProcessPoolExecutor(max_workers=1, mp_context=ctx)
    _log_reader_stop.clear()
    _log_reader_thread = threading.Thread(target=_drain_log_queue, daemon=True)
    _log_reader_thread.start()
    try:
        yield
    finally:
        _kill_executor_children()
        _executor.shutdown(wait=False, cancel_futures=True)
        _log_reader_stop.set()
        try:
            _log_queue.put_nowait(None)
        except Exception:
            pass
        try:
            _log_manager.shutdown()
        except Exception:
            pass


app = FastAPI(
    title="NBA Tweet Video Generator API",
    description="将球星推特截图 + 中文翻译合成竖屏短视频（含AI增强+配音配乐）",
    version="3.0.0",
    lifespan=lifespan,
)

# 上传临时目录
UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

agent = TweetVideoAgent()

# ── 取消机制 ──────────────────────────────────────────────
_cancel_flags: dict[str, bool] = {}  # request_id -> cancelled
_active_request_id: str = ""  # 当前正在生成的 request_id
_last_request_id: str = ""   # 上一次生成的 request_id（下次生成时清理其中间产物）

# ── 日志收集 ──────────────────────────────────────────────
_logs: deque = deque(maxlen=500)
_logs_lock = threading.Lock()

# 日志文件：固定路径，每次生成视频时覆盖
_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output", "logs", "video.log")
os.makedirs(os.path.dirname(_LOG_FILE), exist_ok=True)


def _reset_log():
    """清空日志文件和内存日志，用于新一轮生成开始时。"""
    with _logs_lock:
        _logs.clear()
    try:
        with open(_LOG_FILE, "w", encoding="utf-8") as f:
            f.write("")
    except Exception:
        pass


def _vlog(msg, level="info"):
    """记录日志：内存 + 文件 + 原始stdout。"""
    ts = time.strftime("%H:%M:%S")
    entry = {"time": ts, "message": str(msg), "level": level}
    with _logs_lock:
        _logs.append(entry)
    line = f"[{ts}] [{level}] {msg}\n"
    try:
        _orig_stdout.write(line)
    except UnicodeEncodeError:
        enc = getattr(_orig_stdout, "encoding", "utf-8") or "utf-8"
        _orig_stdout.write(line.encode(enc, errors="replace").decode(enc, errors="replace"))
    _orig_stdout.flush()
    try:
        with open(_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


# 劫持 stdout/stderr，所有 print() 输出自动进入 _vlog
import sys
_orig_stdout = sys.stdout
_orig_stderr = sys.stderr


class _LogCapture:
    def __init__(self, level="info"):
        self._level = level
        self._buf = ""

    def write(self, s):
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.strip()
            if not line:
                continue
            # 过滤下载进度刷屏，只保留完成行
            if line.startswith("[download]") and "100%" not in line and "in 00:" not in line:
                continue
            # 根据内容自动判断 level
            level = self._level
            if "失败" in line or "FAIL" in line or "ERR" in line or "Error" in line:
                level = "warn"
            elif "成功" in line or "完成" in line or "OK" in line:
                level = "success"
            _vlog(line, level)

    def flush(self):
        if self._buf.strip():
            _vlog(self._buf.strip(), self._level)
            self._buf = ""

    def isatty(self):
        return False


sys.stdout = _LogCapture("info")
sys.stderr = _LogCapture("error")


@app.get("/logs")
def get_logs(limit: int = 200):
    """返回最近的日志。"""
    with _logs_lock:
        return list(_logs)[-limit:]


@app.post("/cancel")
def cancel_generation():
    """取消当前正在进行的视频生成（强杀子进程）。"""
    global _executor, _active_request_id
    if not _active_request_id:
        return {"status": "no_active_task"}
    rid = _active_request_id
    _cancel_flags[rid] = True
    _vlog(f"[cancel] 强杀子进程，停止 {rid}", "warn")
    _kill_executor_children()
    # 重建 executor，否则下一次请求会失败
    ctx = multiprocessing.get_context("spawn")
    try:
        _executor.shutdown(wait=False, cancel_futures=True)
    except Exception:
        pass
    _executor = concurrent.futures.ProcessPoolExecutor(max_workers=1, mp_context=ctx)
    _active_request_id = ""
    return {"status": "cancelled", "request_id": rid}


@app.get("/status")
def get_status():
    """返回当前生成状态。"""
    return {
        "active_request_id": _active_request_id,
        "is_generating": bool(_active_request_id and _active_request_id not in _cancel_flags),
    }


def _cleanup_intermediates(output_dir, audio_dir):
    """清理历史所有中间产物。在每次新生成开始时调用。
    保留：最终成片（无 _v 后缀的 tweet_*.mp4）、reference_videos、
    audio_dir 下非 tts_/bgm_ 前缀的文件。"""
    import glob
    cleaned = 0
    patterns = [
        os.path.join(output_dir, "tweet_*_v*.mp4"),       # 迭代版本
        os.path.join(output_dir, "frame_*.png"),          # 临时帧
        os.path.join(output_dir, "sub_*.png"),            # TTS 字幕帧
        os.path.join(output_dir, "hl_*.png"),             # 高光字幕帧
        os.path.join(audio_dir, "tts_*.mp3"),
        os.path.join(audio_dir, "tts_*.wav"),
        os.path.join(audio_dir, "bgm_*.wav"),
    ]
    for pat in patterns:
        for f in glob.glob(pat):
            try:
                os.remove(f)
                cleaned += 1
            except Exception:
                pass
    # 清空 uploads
    uploads_dir = os.path.join(output_dir, "uploads")
    if os.path.isdir(uploads_dir):
        for f in os.listdir(uploads_dir):
            fp = os.path.join(uploads_dir, f)
            if os.path.isfile(fp):
                try:
                    os.remove(fp)
                    cleaned += 1
                except Exception:
                    pass
    if cleaned:
        _vlog(f"[cleanup] 已清理 {cleaned} 个历史中间文件")


@app.get("/health")
def health():
    """健康检查"""
    return {"status": "ok", "service": "tweet-video-generator"}


@app.get("/backends")
def list_backends():
    """可用的 AI 后端列表"""
    from agents.ai_assistant import _DEFAULT_BACKEND
    return {
        "backends": ["claude", "gpt"],
        "default": _DEFAULT_BACKEND,
        "description": {
            "claude": "Claude Opus 4.6 (本地 CLI，无需 API key)",
            "gpt": "Azure OpenAI GPT (需要 AZURE_OPENAI_API_KEY)",
        },
    }


@app.post("/generate")
async def generate_video(
    images: list[UploadFile] = File(..., description="推特截图文件列表"),
    translations: str = Form(..., description="中文翻译列表，用 | 分隔"),
    authors: Optional[str] = Form(None, description="作者列表，用 | 分隔"),
    original_texts: Optional[str] = Form(None, description="原始英文推文，用 | 分隔"),
    mood: Optional[str] = Form("chill", description="背景音乐氛围: chill/hype/emotional"),
    duration: Optional[float] = Form(12.0, description="视频时长（秒）"),
    backend: Optional[str] = Form(None, description="AI后端: claude/gpt（默认读 AI_BACKEND 环境变量）"),
):
    """
    生成推特短视频（含配音+配乐）

    **请求参数**:
    - images: 推特截图文件（支持多张）
    - translations: 对应的中文翻译，多条用 `|` 分隔
    - authors: 对应的作者名，多条用 `|` 分隔（可选）
    - original_texts: 原始英文推文，多条用 `|` 分隔（可选，用于生成解说词）
    - mood: 背景音乐氛围，可选 chill/hype/emotional（默认 chill）
    - duration: 视频总时长秒数（默认 12）

    **返回**:
    ```json
    {
        "video_url": "/video/tweet_xxxxx.mp4",
        "video_path": "...",
        "duration": 12.0,
        "resolution": "1080x1920",
        "images_count": 1,
        "has_narration": true,
        "commentary": "...",
        "recommended_song": "..."
    }
    ```
    """
    # 验证输入
    if not images:
        raise HTTPException(status_code=400, detail="至少需要上传一张截图")

    ai = get_assistant(backend, logger=_vlog)
    _reset_log()
    _vlog(f"[generate] 开始生成, 后端={backend or 'default'}, 图片={len(images)}")
    trans_list = [t.strip() for t in translations.split("|")]
    author_list = [a.strip() for a in authors.split("|")] if authors else None
    orig_list = [t.strip() for t in original_texts.split("|")] if original_texts else None

    if mood not in ("chill", "hype", "emotional"):
        mood = "chill"
    if duration < 5 or duration > 60:
        duration = 12.0

    # 保存上传的图片到临时目录
    saved_paths = []
    request_id = uuid.uuid4().hex[:8]
    try:
        for i, img_file in enumerate(images):
            # 安全文件名
            ext = os.path.splitext(img_file.filename or "img.jpg")[1] or ".jpg"
            if ext.lower() not in (".jpg", ".jpeg", ".png", ".webp"):
                ext = ".jpg"
            safe_name = f"{request_id}_{i}{ext}"
            save_path = os.path.join(UPLOAD_DIR, safe_name)
            with open(save_path, "wb") as f:
                content = await img_file.read()
                f.write(content)
            saved_paths.append(save_path)

        # 生成解说词（如果提供了原始文本）
        commentaries = None
        if orig_list:
            commentaries = []
            for i, trans in enumerate(trans_list):
                orig = orig_list[i] if i < len(orig_list) else ""
                author = author_list[i] if author_list and i < len(author_list) else ""
                try:
                    c = ai.generate_commentary(orig, trans, author)
                    commentaries.append(c)
                except Exception:
                    commentaries.append(trans)

        # Claude 推荐歌曲
        song_query = None
        try:
            content = trans_list[0]
            author = author_list[0] if author_list else ""
            song_query = ai.recommend_music_claude(content, author)
        except Exception:
            pass

        # 生成视频
        output_name = f"tweet_{request_id}.mp4"
        video_path = agent.generate(
            images=saved_paths,
            translations=trans_list,
            authors=author_list,
            mood=mood,
            duration=duration,
            output_name=output_name,
            commentary=commentaries,
            song_query=song_query,
        )

        return JSONResponse(content={
            "video_url": f"/video/{output_name}",
            "video_path": video_path,
            "duration": duration,
            "resolution": "1080x1920",
            "images_count": len(saved_paths),
            "has_narration": commentaries is not None,
            "commentary": commentaries[0] if commentaries else None,
            "recommended_song": song_query,
        })

    except Exception as e:
        _vlog(f"视频生成失败: {e}", "error")
        raise HTTPException(status_code=500, detail=f"视频生成失败: {str(e)}")
    finally:
        # 清理上传的临时文件
        for p in saved_paths:
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass


@app.get("/video/{filename}")
def get_video(filename: str):
    """下载/查看已生成的视频"""
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="非法文件名")
    path = os.path.join(agent.output_dir, filename)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="视频未找到")
    return FileResponse(path, media_type="video/mp4", filename=filename)


@app.get("/videos")
def list_videos():
    """列出所有已生成的视频文件。"""
    videos = []
    for f in sorted(os.listdir(agent.output_dir), reverse=True):
        if f.endswith(".mp4") and not f.startswith("tweet_") or f.startswith("tweet_"):
            fp = os.path.join(agent.output_dir, f)
            if f.endswith(".mp4") and os.path.isfile(fp):
                stat = os.stat(fp)
                videos.append({
                    "filename": f,
                    "url": f"/video/{f}",
                    "size_mb": round(stat.st_size / 1024 / 1024, 2),
                    "created": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
                })
    return videos


@app.delete("/video/{filename}")
def delete_video(filename: str):
    """删除单个视频文件。"""
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="非法文件名")
    path = os.path.join(agent.output_dir, filename)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="视频未找到")
    os.remove(path)
    _vlog(f"已删除视频: {filename}", "warn")
    return {"message": f"已删除 {filename}"}


@app.delete("/videos")
def delete_all_videos():
    """删除所有生成的视频文件。"""
    count = 0
    for f in os.listdir(agent.output_dir):
        if f.endswith(".mp4") and os.path.isfile(os.path.join(agent.output_dir, f)):
            os.remove(os.path.join(agent.output_dir, f))
            count += 1
    _vlog(f"已删除全部 {count} 个视频", "warn")
    return {"message": f"已删除 {count} 个视频"}


@app.post("/generate-ai")
async def generate_video_ai(
    images: list[UploadFile] = File(..., description="推特截图文件列表"),
    translations: str = Form(..., description="中文翻译列表，用 | 分隔"),
    authors: Optional[str] = Form(None, description="作者列表，用 | 分隔"),
    original_texts: Optional[str] = Form(None, description="原始英文推文，用 | 分隔"),
    duration: Optional[float] = Form(12.0, description="视频时长（秒）"),
    max_rounds: Optional[int] = Form(3, description="最大迭代轮数（1-3）"),
    backend: Optional[str] = Form(None, description="AI后端: claude/gpt（默认读 AI_BACKEND 环境变量）"),
    highlight: Optional[str] = Form(None, description="是否对原视频识别高光段并保留原音 (1/true/yes 启用)"),
    tweet_id: Optional[str] = Form(None, description="推文ID（用于背景检索缓存与触发）"),
    video: Optional[UploadFile] = File(None, description="推文自带视频文件（可选）"),
):
    """
    AI增强版 v3：解说词 + 配乐 + 真实歌曲 + 配音 + 迭代审阅

    支持 backend 参数选择 AI 后端（claude / gpt）。

    流程：
    1. AI 优化翻译（字幕显示用）
    2. AI 生成解说词（有解说感的旁白，非简单翻译）
    3. Claude CLI 推荐最适合的歌曲 → 搜索下载 → 截取高潮段
    4. TTS 配音解说词
    5. 混合：配音(前景) + 歌曲配乐(背景20%)
    6. AI 严格审阅（90+ 分 = A级合格）
    7. 未达A级则改进解说词/歌曲，重新生成（最多 max_rounds 轮）
    """
    if not images:
        raise HTTPException(status_code=400, detail="至少需要上传一张截图")

    ai = get_assistant(backend, logger=_vlog)

    trans_list = [t.strip() for t in translations.split("|")]
    author_list = [a.strip() for a in authors.split("|")] if authors else None
    orig_list = [t.strip() for t in original_texts.split("|")] if original_texts else None

    if duration < 5 or duration > 60:
        duration = 12.0
    if max_rounds < 1 or max_rounds > 3:
        max_rounds = 3

    saved_paths = []
    saved_video_path = None
    request_id = uuid.uuid4().hex[:8]
    try:
        # 在保存新一轮 uploads 之前，先清理历史所有中间产物
        _output_dir = os.path.dirname(UPLOAD_DIR)
        _audio_dir = os.path.join(_output_dir, "audio")
        _cleanup_intermediates(_output_dir, _audio_dir)

        for i, img_file in enumerate(images):
            ext = os.path.splitext(img_file.filename or "img.jpg")[1] or ".jpg"
            if ext.lower() not in (".jpg", ".jpeg", ".png", ".webp"):
                ext = ".jpg"
            safe_name = f"{request_id}_{i}{ext}"
            save_path = os.path.join(UPLOAD_DIR, safe_name)
            with open(save_path, "wb") as f:
                content = await img_file.read()
                f.write(content)
            saved_paths.append(save_path)

        # 保存推文自带视频（如有）
        if video and video.filename:
            vext = os.path.splitext(video.filename)[1] or ".mp4"
            video_save = os.path.join(UPLOAD_DIR, f"{request_id}_video{vext}")
            with open(video_save, "wb") as f:
                f.write(await video.read())
            saved_video_path = video_save
            _vlog(f"[generate-ai] 收到推文视频: {video.filename}")

        # 把重活放到独立子进程，Ctrl+C / /cancel 可以直接 terminate
        loop = asyncio.get_event_loop()
        _active_request_id_set(request_id)
        _highlight_flag = str(highlight or "").lower() in ("1", "true", "yes", "on")
        try:
            result = await loop.run_in_executor(
                _executor,
                _do_generate_ai_subprocess,
                saved_paths, saved_video_path, trans_list, author_list, orig_list,
                duration, max_rounds, backend, request_id, _log_queue, _highlight_flag,
                (tweet_id or "").strip(),
            )
        finally:
            _active_request_id_clear(request_id)
        return JSONResponse(content=result)

    except Exception as e:
        _vlog(f"视频生成失败: {e}", "error")
        raise HTTPException(status_code=500, detail=f"视频生成失败: {str(e)}")


def _active_request_id_set(rid: str):
    global _active_request_id
    _active_request_id = rid


def _active_request_id_clear(rid: str):
    global _active_request_id, _last_request_id
    if _active_request_id == rid:
        _active_request_id = ""
    _cancel_flags.pop(rid, None)
    _last_request_id = rid


def _do_generate_ai_subprocess(saved_paths, saved_video_path, trans_list, author_list, orig_list,
                                duration, max_rounds, backend, request_id, log_queue,
                                highlight=False, tweet_id=""):
    """子进程入口：重新构造 ai/logger，调用原同步流程。"""
    def _qlog(msg, level="info"):
        try:
            log_queue.put_nowait((str(msg), level))
        except Exception:
            pass

    from agents.ai_assistant import get_assistant as _get
    ai = _get(backend, logger=_qlog)
    return _do_generate_ai_inner(saved_paths, saved_video_path, trans_list,
                                  author_list, orig_list, duration, max_rounds,
                                  ai, request_id, logger=_qlog, highlight=highlight,
                                  tweet_id=tweet_id)


def _do_generate_ai(saved_paths, saved_video_path, trans_list, author_list, orig_list,
                    duration, max_rounds, ai, request_id):
    """同步执行视频生成全流程（兼容旧调用）。"""
    global _active_request_id
    global _active_request_id, _last_request_id
    _active_request_id = request_id

    result = None
    try:
        result = _do_generate_ai_inner(saved_paths, saved_video_path, trans_list,
                                        author_list, orig_list, duration, max_rounds, ai, request_id)
    finally:
        _active_request_id = ""
        _cancel_flags.pop(request_id, None)
        _last_request_id = request_id
    return result


def _do_generate_ai_inner(saved_paths, saved_video_path, trans_list, author_list, orig_list,
                          duration, max_rounds, ai, request_id, logger=None, highlight=False,
                          tweet_id=""):
    from agents.pipeline_core import run_pipeline, PipelineResult
    from agents.tweet_video_agent import TweetVideoAgent
    _log = logger or _vlog
    _agent = TweetVideoAgent()
    result: PipelineResult = run_pipeline(
        saved_paths, trans_list,
        saved_video_path=saved_video_path,
        author_list=author_list,
        orig_list=orig_list,
        duration=duration,
        max_rounds=max_rounds,
        request_id=request_id,
        highlight=highlight,
        tweet_id=tweet_id,
        ai=ai,
        agent=_agent,
        logger=_log,
        on_cancel=None,
    )
    return {
        "video_url": f"/video/{result.final_name}",
        "video_path": result.video_path,
        "duration": duration,
        "resolution": "1080x1920",
        "images_count": len(saved_paths),
        "ai_enhanced": {
            "original_translation": result.original_translation,
            "polished_translation": result.polished_translations[0] if result.polished_translations else "",
            "final_commentary": result.final_commentary,
            "recommended_song": result.final_song,
            "recommended_mood": result.recommended_mood,
            "final_review": result.full_review,
            "selected_round": result.selected_round,
            "total_rounds": result.total_rounds,
            "rounds": result.rounds_log,
        },
    }


if __name__ == "__main__":
    import uvicorn, signal, logging

    class _SilenceHealth(logging.Filter):
        def filter(self, record):
            msg = record.getMessage()
            return "/health" not in msg

    logging.getLogger("uvicorn.access").addFilter(_SilenceHealth())

    config = uvicorn.Config(app, host="0.0.0.0", port=8000)
    server = uvicorn.Server(config)

    def _force_exit(*_):
        _kill_executor_children()
        server.should_exit = True
        server.force_exit = True

    signal.signal(signal.SIGINT, _force_exit)
    try:
        signal.signal(signal.SIGTERM, _force_exit)
    except Exception:
        pass
    server.run()
