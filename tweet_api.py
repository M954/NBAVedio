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
from collections import deque
from typing import Optional
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from agents.tweet_video_agent import TweetVideoAgent
from agents.ai_assistant import get_assistant

app = FastAPI(
    title="NBA Tweet Video Generator API",
    description="将球星推特截图 + 中文翻译合成竖屏短视频（含AI增强+配音配乐）",
    version="3.0.0",
)

# 上传临时目录
UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

agent = TweetVideoAgent()

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


def _cleanup_intermediates(request_id, output_dir, audio_dir):
    """清理视频生成的中间产物：迭代版本、TTS音频、合成BGM、临时帧。"""
    import glob
    cleaned = 0
    # 删除迭代版本 tweet_{id}_v1.mp4 等
    for f in glob.glob(os.path.join(output_dir, f"tweet_{request_id}_v*.mp4")):
        try:
            os.remove(f)
            cleaned += 1
        except Exception:
            pass
    # 删除 TTS 和合成 BGM（audio 目录下的临时文件）
    for pattern in ["tts_*.mp3", "tts_*.wav", "bgm_*.wav"]:
        for f in glob.glob(os.path.join(audio_dir, pattern)):
            try:
                os.remove(f)
                cleaned += 1
            except Exception:
                pass
    # 删除临时帧
    for f in glob.glob(os.path.join(output_dir, "frame_*.png")):
        try:
            os.remove(f)
            cleaned += 1
        except Exception:
            pass
    for f in glob.glob(os.path.join(output_dir, "sub_*.png")):
        try:
            os.remove(f)
            cleaned += 1
        except Exception:
            pass
    # 清理 uploads
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
        _vlog(f"[cleanup] 已清理 {cleaned} 个中间文件")


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
    max_rounds: Optional[int] = Form(3, description="最大迭代轮数（1-5）"),
    backend: Optional[str] = Form(None, description="AI后端: claude/gpt（默认读 AI_BACKEND 环境变量）"),
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
    if max_rounds < 1 or max_rounds > 5:
        max_rounds = 3

    saved_paths = []
    saved_video_path = None
    request_id = uuid.uuid4().hex[:8]
    try:
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

        # 把重活放到线程池，不阻塞 uvicorn event loop（health/logs 可正常响应）
        import asyncio
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            _do_generate_ai,
            saved_paths, saved_video_path, trans_list, author_list, orig_list,
            duration, max_rounds, ai, request_id,
        )
        return JSONResponse(content=result)

    except Exception as e:
        _vlog(f"视频生成失败: {e}", "error")
        raise HTTPException(status_code=500, detail=f"视频生成失败: {str(e)}")
    finally:
        _cleanup_intermediates(request_id, agent.output_dir, agent.audio_dir)
        for p in saved_paths:
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass
        if saved_video_path and os.path.exists(saved_video_path):
            try:
                os.remove(saved_video_path)
            except Exception:
                pass


def _do_generate_ai(saved_paths, saved_video_path, trans_list, author_list, orig_list,
                    duration, max_rounds, ai, request_id):
    """同步执行视频生成全流程（在线程池中运行）。"""
    result = None
    try:
        result = _do_generate_ai_inner(saved_paths, saved_video_path, trans_list,
                                        author_list, orig_list, duration, max_rounds, ai, request_id)
    finally:
        _cleanup_intermediates(request_id, agent.output_dir, agent.audio_dir)
        for p in saved_paths:
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass
        if saved_video_path and os.path.exists(saved_video_path):
            try:
                os.remove(saved_video_path)
            except Exception:
                pass
    return result


def _do_generate_ai_inner(saved_paths, saved_video_path, trans_list, author_list, orig_list,
                          duration, max_rounds, ai, request_id):
    orig0 = orig_list[0] if orig_list else ""
    author0 = author_list[0] if author_list else ""

    # 1. AI 优化翻译（用于字幕显示）
    _reset_log()
    _vlog("[generate-ai] 步骤1: 优化翻译")
    polished = []
    for i, trans in enumerate(trans_list):
        orig = orig_list[i] if orig_list and i < len(orig_list) else ""
        try:
            polished.append(ai.polish_translation(orig, trans))
        except Exception:
            polished.append(trans)

    # 2. AI 生成解说词（用于配音，有解说感）
    _vlog("[generate-ai] 步骤2: 生成解说词")
    commentaries = []
    for i, trans in enumerate(polished):
        orig = orig_list[i] if orig_list and i < len(orig_list) else ""
        author = author_list[i] if author_list and i < len(author_list) else ""
        try:
            c = ai.generate_commentary(orig, trans, author)
            commentaries.append(c)
        except Exception:
            commentaries.append(trans)

    # 3. Claude 推荐歌曲 + AI 氛围
    _vlog("[generate-ai] 步骤3: 推荐配乐")
    song_query = None
    try:
        song_query = ai.recommend_song(orig0, polished[0], author0)
    except Exception:
        pass

    try:
        mood = ai.recommend_mood(orig0, polished[0])
    except Exception:
        mood = "chill"

    # 4-7. 迭代生成 + 审阅
    _vlog(f"[generate-ai] 步骤4-7: 开始迭代生成 (最多{max_rounds}轮)")
    best_video = None
    best_review = {"score": 0, "grade": "F"}
    cur_commentary = commentaries[0] if commentaries else polished[0]
    cur_song = song_query
    rounds_log = []

    for rnd in range(1, max_rounds + 1):
        _vlog(f"[generate-ai] 第{rnd}轮生成中...")
        output_name = f"tweet_{request_id}_v{rnd}.mp4"
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
        )

        # 审阅
        from moviepy import VideoFileClip
        clip = VideoFileClip(video_path)
        info = {
            "commentary": cur_commentary,
            "translation": polished[0] if polished else "",
            "author": author0,
            "bgm_song": cur_song or "合成音乐",
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
            review = ai.review_video(info, video_path=video_path)
        except Exception as e:
            review = {"score": 70, "grade": "C", "suggestions": [str(e)]}

        score = review.get("score", 0)
        grade = review.get("grade", "F")
        suggestions = review.get("suggestions", [])
        _vlog(f"[generate-ai] 第{rnd}轮评分: {score}分 ({grade}级)")

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

        if score >= 90:
            _vlog("[generate-ai] A级达标，停止迭代", "success")
            break

        if rnd < max_rounds:
            try:
                improved = ai._call(
                    f"当前解说词: {cur_commentary}\n"
                    f"审阅建议: {'; '.join(suggestions)}\n"
                    f"原始推文: {orig0}\n作者: {author0}\n"
                    f"请根据建议重写解说词：\n"
                    f"1. 必须解读推文行为（转发/引用/回复/原创）\n"
                    f"2. 必须说明态度（支持/反对/调侃/感慨）\n"
                    f"3. 必须补充背景信息\n"
                    f"50-80字。只返回解说词。"
                )
                if improved and len(improved.strip()) > 10:
                    cur_commentary = improved.strip().strip('"').strip("'")
            except Exception:
                pass

            for s in suggestions:
                if "配乐" in s or "歌曲" in s or "音乐" in s or "BGM" in s or "合成" in s:
                    try:
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
    _vlog(f"[generate-ai] 完成! 最终评分: {best_review.get('score',0)}分 ({best_review.get('grade','?')}级)", "success")

    return {
        "video_url": f"/video/{final_name}",
        "video_path": final_path,
        "duration": duration,
        "resolution": "1080x1920",
        "images_count": len(saved_paths),
        "ai_enhanced": {
            "original_translation": trans_list[0] if trans_list else "",
            "polished_translation": polished[0] if polished else "",
            "final_commentary": cur_commentary,
            "recommended_song": cur_song,
            "recommended_mood": mood,
            "final_review": best_review,
            "total_rounds": len(rounds_log),
            "rounds": rounds_log,
        },
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
