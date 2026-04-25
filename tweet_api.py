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
    _orig_stdout.write(line)
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
                                highlight=False):
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
                                  ai, request_id, logger=_qlog, highlight=highlight)


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
                          duration, max_rounds, ai, request_id, logger=None, highlight=False):
    _vlog = logger or globals()["_vlog"]
    orig0 = orig_list[0] if orig_list else ""
    author0 = author_list[0] if author_list else ""
    import time as _t
    _pipeline_start = _t.time()

    # 1. AI 优化翻译（用于字幕显示）
    _reset_log()
    _step_t = _t.time()
    _vlog("[generate-ai] 步骤1: 优化翻译")
    polished = []
    for i, trans in enumerate(trans_list):
        orig = orig_list[i] if orig_list and i < len(orig_list) else ""
        try:
            result = ai.polish_translation(orig, trans)
            polished.append(result)
            _vlog(f"  翻译优化: {trans} → {result}")
        except Exception as e:
            polished.append(trans)
            _vlog(f"  翻译优化失败: {e}", "warn")
    _vlog(f"[generate-ai] 步骤1完成，耗时 {_t.time()-_step_t:.1f}s")

    # 2. 如果有推文视频，先分析视频内容
    video_description = ""
    video_subtitles = []
    if saved_video_path:
        _step_t = _t.time()
        _vlog("[generate-ai] 步骤2: 分析推文视频内容")
        try:
            video_description = ai.analyze_video_content(saved_video_path, orig0, author0)
        except Exception as e:
            _vlog(f"[generate-ai] 视频分析失败: {e}", "warn")
        # 提取视频中的对话/旁白翻译为字幕
        if video_description:
            try:
                video_subtitles = ai.extract_video_dialogue(video_description, orig0, author0)
                if video_subtitles:
                    _vlog(f"[generate-ai] 提取视频字幕: {video_subtitles}")
            except Exception as e:
                _vlog(f"[generate-ai] 提取视频字幕失败: {e}", "warn")
        _vlog(f"[generate-ai] 步骤2完成，耗时 {_t.time()-_step_t:.1f}s")

    # 计算目标视频时长（用于生成匹配长度的解说词）
    target_video_duration = duration
    if saved_video_path:
        try:
            from moviepy import VideoFileClip as _VFC
            _vc = _VFC(saved_video_path)
            target_video_duration = max(duration, _vc.duration + 5.0)  # 源视频 + 5s 开场
            _vc.close()
            _vlog(f"[generate-ai] 目标视频时长: {target_video_duration:.1f}s (源视频 {target_video_duration-5:.1f}s + 5s开场)")
        except Exception:
            pass

    # 3. AI 生成解说词（用于配音，有解说感）
    _step_t = _t.time()
    _vlog("[generate-ai] 步骤3: 生成解说词")
    commentaries = []
    for i, trans in enumerate(polished):
        orig = orig_list[i] if orig_list and i < len(orig_list) else ""
        author = author_list[i] if author_list and i < len(author_list) else ""
        try:
            c = ai.generate_commentary(orig, trans, author,
                                       has_video=saved_video_path is not None,
                                       video_description=video_description,
                                       target_duration=target_video_duration)
            commentaries.append(c)
            _vlog(f"  解说词: {c}")
        except Exception as e:
            commentaries.append(trans)
            _vlog(f"  解说词生成失败: {e}", "warn")
    _vlog(f"[generate-ai] 步骤3完成，耗时 {_t.time()-_step_t:.1f}s")

    # 4. Claude 推荐歌曲 + AI 氛围
    _step_t = _t.time()
    _vlog("[generate-ai] 步骤4: 推荐配乐")
    song_query = None
    try:
        song_query = ai.recommend_song(orig0, polished[0], author0)
        _vlog(f"  推荐歌曲: {song_query}")
    except Exception:
        pass

    try:
        mood = ai.recommend_mood(orig0, polished[0])
        _vlog(f"  配乐氛围: {mood}")
    except Exception:
        mood = "chill"
    _vlog(f"[generate-ai] 步骤4完成，耗时 {_t.time()-_step_t:.1f}s")

    # 4b. 识别原视频高光段（仅在用户请求时启用）
    highlight_segments = []
    if highlight and saved_video_path and os.path.exists(saved_video_path):
        _hl_t = _t.time()
        _vlog("[generate-ai] 步骤4b: Gemini 识别原视频高光段")
        try:
            highlight_segments = ai.pick_highlight_segments_gemini(saved_video_path)
            for _h in highlight_segments:
                _vlog(f"  高光 [{_h['start']:.1f}-{_h['end']:.1f}s] "
                      f"原: {_h.get('original') or ''} | 译: {_h['translation']}")
            if not highlight_segments:
                _vlog("  Gemini 未挑出高光段")
        except Exception as _he:
            _vlog(f"  高光识别失败: {_he}", "warn")
        _vlog(f"[generate-ai] 步骤4b完成，耗时 {_t.time()-_hl_t:.1f}s")
    elif saved_video_path:
        _vlog("[generate-ai] 步骤4b跳过 (highlight=off)")

    # 5-8. 迭代生成 + 审阅
    _vlog(f"[generate-ai] 步骤5-8: 开始迭代生成 (最多{max_rounds}轮)")
    best_video = None
    best_review = {"score": 0, "grade": "F"}
    best_round = None
    cur_commentary = commentaries[0] if commentaries else polished[0]
    cur_song = song_query
    rounds_log = []

    for rnd in range(1, max_rounds + 1):
        # 检查取消标志
        if _cancel_flags.get(request_id):
            _vlog("[generate-ai] 收到取消请求，停止生成", "warn")
            break
        _rnd_t = _t.time()
        _vlog(f"[generate-ai] 第{rnd}轮生成中...")
        output_name = f"tweet_{request_id}_v{rnd}.mp4"
        _gen_t = _t.time()
        video_path = agent.generate(
            images=saved_paths,
            translations=polished,
            authors=author_list,
            mood=mood,
            duration=duration,
            output_name=output_name,
            commentary=[cur_commentary],
            song_query=cur_song,
            source_video=saved_video_path,
            video_subtitles=video_subtitles,
            highlight_segments=highlight_segments,
        )
        _vlog(f"  视频生成耗时: {_t.time()-_gen_t:.1f}s")

        # 检查取消标志
        if _cancel_flags.get(request_id):
            _vlog("[generate-ai] 收到取消请求，跳过审阅", "warn")
            best_video = video_path
            break

        # 审阅
        _rev_t = _t.time()
        from moviepy import VideoFileClip
        clip = VideoFileClip(video_path)
        info = {
            "commentary": cur_commentary,
            "translation": polished[0] if polished else "",
            "original_text": orig0,
            "author": author0,
            "video_description": video_description,
            "bgm_song": cur_song or "BGM库",
            "mood": mood,
            "has_narration": True,
            "has_source_video": saved_video_path is not None,
            "duration": round(clip.duration, 1),
            "resolution": f"{clip.size[0]}x{clip.size[1]}",
            "has_audio": clip.audio is not None,
            "file_size_mb": round(os.path.getsize(video_path) / (1024 * 1024), 2),
        }
        clip.close()

        try:
            review = ai.review_video(info, video_path=video_path,
                                     subtitle_timeline=agent.last_subtitle_timeline)
        except Exception as e:
            review = {"score": 70, "grade": "C", "suggestions": [str(e)]}
        _vlog(f"  审阅耗时: {_t.time()-_rev_t:.1f}s")

        score = review.get("score", 0)
        grade = review.get("grade", "F")
        suggestions = review.get("suggestions", [])
        details = review.get("details", {})
        content_issues = review.get("content_issues", [])
        subtitle_mismatches = review.get("subtitle_mismatches", [])
        _vlog(f"[generate-ai] 第{rnd}轮评分: {score}分 ({grade}级)")
        if details:
            _vlog(f"  评分明细: {details}")
        if content_issues:
            _vlog(f"  内容问题: {content_issues}", "warn")
        if subtitle_mismatches:
            _vlog(f"  字幕不匹配: {subtitle_mismatches}", "warn")
        if suggestions:
            _vlog(f"  改进建议: {suggestions}")

        rounds_log.append({
            "round": rnd,
            "score": score,
            "grade": grade,
            "commentary": cur_commentary,
            "song": cur_song,
            "suggestions": suggestions,
        })

        if score > best_review.get("score", 0):
            best_video = video_path
            best_review = review
            best_round = {
                "round": rnd,
                "commentary": cur_commentary,
                "song": cur_song,
            }

        if score >= 90:
            _vlog("[generate-ai] A级达标，停止迭代", "success")
            _vlog(f"  第{rnd}轮总耗时: {_t.time()-_rnd_t:.1f}s")
            break

        if rnd < max_rounds:
            _vlog(f"[generate-ai] 第{rnd}轮未达标 (耗时 {_t.time()-_rnd_t:.1f}s)，准备改进...")
            # 把 review 完整反馈传给解说词重写
            _content_issues = review.get("content_issues", []) or []
            _sub_mismatches = review.get("subtitle_mismatches", []) or []
            _suggestions = suggestions or []
            _details = review.get("details", {}) or {}

            # 计算目标字数（与初版 prompt 保持一致：4字/秒，预留首尾 3s）
            _avail = max(target_video_duration - 3, 8)
            _tgt_chars = int(_avail * 4)
            _min_chars = max(_tgt_chars - 20, 60)
            _max_chars = _tgt_chars + 20

            try:
                from agents.style_guide import PLAYER_NICKNAMES as _PN, FORBIDDEN_WORDS as _FW
                _nick_table = "\n".join(f"  {e} = {c}" for e, c in _PN.items() if c)
                _forbid = "、".join(_FW)
            except Exception:
                _nick_table = ""
                _forbid = ""

            try:
                rewrite_prompt = (
                    f"你是篮球邮差Melo风格NBA短视频博主。上一版解说词被审阅 agent 扣分了，"
                    f"请根据反馈彻底修复所有被点名的问题，写出一版高质量的新解说词。\n\n"
                    f"=== 审阅反馈（这是本次重写的核心依据，每一条都必须修）===\n"
                    f"评分明细: {_details}\n"
                    f"内容事实问题: {_content_issues if _content_issues else '无'}\n"
                    f"字幕/画面错位: {_sub_mismatches if _sub_mismatches else '无'}\n"
                    f"改进建议: {_suggestions if _suggestions else '无'}\n\n"
                    f"=== 上一版解说词（仅供参考，可大改可重写，目标是修掉上面所有问题）===\n{cur_commentary}\n\n"
                    f"=== 推文上下文 ===\n"
                    f"作者: {author0}\n"
                    f"原文: {orig0}\n"
                    f"翻译: {polished[0] if polished else ''}\n"
                )
                if video_description:
                    rewrite_prompt += f"视频内容: {video_description}\n"
                rewrite_prompt += (
                    f"\n=== 重写硬规则 ===\n"
                    f"⚠️ 解说词是整个视频的灵魂，必须同时满足【完整】+【顺畅可读】：\n"
                    f"  - 完整：开头-发展-结尾三段齐全，核心事实交代完，结尾真的收住，不能戛然而止\n"
                    f"  - 可读：念出来自然顺口，短句优先(8-20字)，禁止翻译腔/堆砌定语，朗读不卡壳\n\n"
                    f"1. 【绝对优先】审阅反馈里的每一条事实错误、字幕错位、改进建议都必须修掉，不能漏\n"
                    f"2. 必须忠实于推文原文事实；不能添油加醋、张冠李戴、编造细节\n"
                    f"3. 如果有视频，解说词必须与视频画面/对白一致，不得描述画面里没有的东西\n"
                    f"4. 字数严格在 {_min_chars}-{_max_chars} 字之间（视频 {target_video_duration:.0f}s，超长会被截）\n"
                    f"5. 开头第一句必须是'XXX今日发推/转推'+情绪钩子；结尾必须收住（个人观点/反问/价值判断）\n"
                    f"6. 必须使用口语词：真的、太、算是、天啊、好家伙、没得说、直接、拉满\n"
                    f"7. 绝对禁用书面套话：{_forbid}\n"
                    f"8. 标点：每短句以句号/感叹号/问号结尾，句内停顿用逗号，禁止用空格代替标点\n"
                    f"9. 球员译名严格使用下表，禁止生造：\n{_nick_table}\n"
                    f"   表外球员用国内主流篮球媒体通用音译，不确定就用全名音译\n\n"
                    f"只返回修订后的解说词正文，不要前言、不要diff、不要解释。提交前对照审阅反馈逐条核对，"
                    f"确认【所有反馈都修了 + 完整 + 顺口 + 字数达标】才提交。"
                )
                improved = ai._call(rewrite_prompt)
                if improved and len(improved.strip()) > 10:
                    cur_commentary = improved.strip().strip('"').strip("'")
                    _vlog(f"  解说词已重写: {cur_commentary}")
            except Exception as e:
                _vlog(f"  解说词重写失败: {e}", "warn")

            for s in suggestions:
                if "配乐" in s or "歌曲" in s or "音乐" in s or "BGM" in s or "合成" in s:
                    try:
                        _bgm_dir = os.path.join(os.path.dirname(__file__), "reference_videos", "bgm")
                        new_bgm = ai.select_bgm_from_library(
                            orig0, polished[0] if polished else "", author0, _bgm_dir)
                        if new_bgm:
                            cur_song = None  # 走 BGM 库，不走搜索下载
                        else:
                            new_song = ai.recommend_song(orig0, polished[0], author0)
                            if new_song and new_song != cur_song:
                                cur_song = new_song
                    except Exception:
                        pass
                    break

    # 最终文件
    final_name = f"tweet_{request_id}.mp4"
    final_path = os.path.join(agent.output_dir, final_name)
    if best_video and best_video != final_path:
        shutil.copy2(best_video, final_path)
    final_commentary = best_round["commentary"] if best_round else cur_commentary
    final_song = best_round["song"] if best_round else cur_song
    final_round = best_round["round"] if best_round else max_rounds
    _vlog(f"[generate-ai] 采用第{final_round}轮作为最终成片")
    _total = _t.time() - _pipeline_start
    _vlog(f"[generate-ai] 完成! 最终评分: {best_review.get('score',0)}分 ({best_review.get('grade','?')}级), 总耗时: {_total:.1f}s ({_total/60:.1f}min)", "success")

    return {
        "video_url": f"/video/{final_name}",
        "video_path": final_path,
        "duration": duration,
        "resolution": "1080x1920",
        "images_count": len(saved_paths),
        "ai_enhanced": {
            "original_translation": trans_list[0] if trans_list else "",
            "polished_translation": polished[0] if polished else "",
            "final_commentary": final_commentary,
            "recommended_song": final_song,
            "recommended_mood": mood,
            "final_review": best_review,
            "selected_round": final_round,
            "total_rounds": len(rounds_log),
            "rounds": rounds_log,
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
