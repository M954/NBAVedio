"""AI Assistant - 双后端（Claude CLI / Azure OpenAI GPT）
用于翻译优化、解说词生成、配乐推荐、视频内容审阅

选择后端：
  - 环境变量 AI_BACKEND=claude 或 AI_BACKEND=gpt（默认 claude）
  - API 调用时可通过 backend 参数覆盖
"""
import base64
import json
import os
import re
import subprocess
import urllib.request


def _truncate(text, limit):
    """截断长文本，保留首段，附 [+N字省略] 标记。"""
    if not text:
        return text
    s = str(text)
    if len(s) <= limit:
        return s
    return s[:limit] + f"...[+{len(s) - limit}字省略]"


class _BaseAssistant:
    """AI 助手基类 — 定义所有 prompt，子类只需实现 _call()"""

    # 日志回调，可被外部替换（如 tweet_api 的 _vlog）
    _logger = None

    def _log(self, msg, level="info"):
        if self._logger:
            self._logger(msg, level)
        else:
            print(f"  [AI] [{level}] {msg}")

    def _call(self, prompt, system="你是一个专业的NBA篮球内容编辑和翻译。"):
        raise NotImplementedError

    # ── Gemini 视频直传分析 ──────────────────────────────────

    _GEMINI_API_KEY = ""
    _GEMINI_MODEL = os.environ.get("GEMINI_VIDEO_MODEL", "gemini-2.5-flash-lite")
    _GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta"

    @staticmethod
    def _analyze_video_gemini(video_path, prompt):
        """用 Gemini 直传视频文件进行分析，返回文本结果。"""
        api_key = os.environ.get("GEMINI_API_KEY", "") or _BaseAssistant._GEMINI_API_KEY
        if not api_key:
            print("  [Gemini] 未设置 GEMINI_API_KEY，跳过")
            return ""
        file_size_mb = os.path.getsize(video_path) / (1024 * 1024)
        model = _BaseAssistant._GEMINI_MODEL
        print(f"  [Gemini] 开始分析视频 ({file_size_mb:.1f}MB), 模型: {model}")
        with open(video_path, "rb") as f:
            video_b64 = base64.standard_b64encode(f.read()).decode()
        body = json.dumps({
            "contents": [{
                "parts": [
                    {"text": prompt},
                    {"inline_data": {"mime_type": "video/mp4", "data": video_b64}},
                ]
            }]
        }).encode("utf-8")
        import time as _t
        _start = _t.time()
        url = f"{_BaseAssistant._GEMINI_ENDPOINT}/models/{model}:generateContent?key={api_key}"
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                elapsed = _t.time() - _start
                for c in data.get("candidates", []):
                    for p in c.get("content", {}).get("parts", []):
                        if "text" in p:
                            result = p["text"].strip()
                            print(f"  [Gemini] 分析完成，耗时 {elapsed:.1f}s，结果长度: {len(result)} 字符")
                            return result
                print(f"  [Gemini] 返回无文本内容，耗时 {elapsed:.1f}s")
                return ""
            except urllib.error.HTTPError as e:
                elapsed = _t.time() - _start
                err_body = e.read().decode("utf-8")[:200]
                if e.code in (429, 503) and attempt < 2:
                    wait = (attempt + 1) * 15
                    print(f"  [Gemini] HTTP {e.code}，{wait}s 后重试 ({attempt+1}/3)")
                    _t.sleep(wait)
                    # 重建 request（read 后原 request 不可复用）
                    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
                    continue
                print(f"  [Gemini] HTTP {e.code} 错误 ({elapsed:.1f}s): {err_body}")
                raise
            except Exception as e:
                elapsed = _t.time() - _start
                print(f"  [Gemini] 失败 ({elapsed:.1f}s): {e}")
                raise
        return ""

    # ── 抽帧工具 ────────────────────────────────────────────

    @staticmethod
    def _extract_frames_b64(video_path, n=8, frame_times=None):
        """从视频抽取帧，返回 JPEG base64 列表。可指定精确时间点。"""
        import io as _io
        from moviepy import VideoFileClip
        from PIL import Image

        clip = VideoFileClip(video_path)
        dur = clip.duration
        if frame_times:
            times = [t for t in frame_times if 0 <= t < dur]
        else:
            times = [0.5] + [dur * i / n for i in range(1, n)] + [max(dur - 1, 0.5)]
            times = sorted(set(t for t in times if t < dur))[:n]
        images = []
        for t in times:
            frame = clip.get_frame(min(t, dur - 0.1))
            buf = _io.BytesIO()
            img = Image.fromarray(frame).convert("RGB")
            img.thumbnail((768, 768))  # 网关对 >100KB 的 JPEG 会误判为 PNG，必须缩
            img.save(buf, format="JPEG", quality=80)
            images.append(base64.standard_b64encode(buf.getvalue()).decode())
        clip.close()
        return images

    @staticmethod
    def _extract_audio_b64(video_path, max_seconds=None):
        """从视频提取音频，返回 mp3 base64 字符串。无音频返回空字符串。
        默认不截断、不压缩（192kbps 高码率），并对音频做 peak normalize 到 0.95
        以提升模型对低音量音轨的识别率。"""
        import tempfile
        import numpy as np
        from moviepy import VideoFileClip
        from moviepy.audio.AudioClip import AudioArrayClip

        clip = VideoFileClip(video_path)
        if not clip.audio:
            clip.close()
            return ""
        audio = clip.audio
        if max_seconds and audio.duration > max_seconds:
            audio = audio.subclipped(0, max_seconds)

        # 拉到 numpy 做归一化
        fps = audio.fps or 44100
        arr = audio.to_soundarray(fps=fps)
        peak = float(np.max(np.abs(arr))) if arr.size else 0.0
        if peak > 0:
            gain = min(0.95 / peak, 30.0)  # 最多放大 30 倍，防止纯静音段乘出噪点
            arr = np.clip(arr * gain, -1.0, 1.0)
            print(f"  [Audio] peak={peak:.4f} → 归一化增益 {gain:.1f}x")
        norm_clip = AudioArrayClip(arr, fps=fps)

        tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        tmp.close()
        try:
            norm_clip.write_audiofile(tmp.name, logger=None, bitrate="192k")
            with open(tmp.name, "rb") as f:
                result = base64.standard_b64encode(f.read()).decode()
        finally:
            clip.close()
            os.unlink(tmp.name)
        return result

    # ── Claude 抽帧+音频分析 ──────────────────────────────────

    _CLAUDE_VISION_ENDPOINT = os.environ.get(
        "CLAUDE_VISION_ENDPOINT",
        "http://localhost:23333/api/anthropic/v1/messages",
    )
    _CLAUDE_VISION_MODEL = "claude-opus-4.7-1m-internal"
    _CLAUDE_VISION_BATCH = 2  # 每批发送的帧数

    # ── Whisper STT 单例（faster-whisper） ──────────────────────
    _whisper_model = None
    _WHISPER_SIZE = os.environ.get("WHISPER_MODEL_SIZE", "small")

    @staticmethod
    def _get_whisper():
        if _BaseAssistant._whisper_model is None:
            from faster_whisper import WhisperModel
            print(f"  [Whisper] 加载模型 {_BaseAssistant._WHISPER_SIZE} (首次会下载)...")
            import time as _t
            t0 = _t.time()
            _BaseAssistant._whisper_model = WhisperModel(
                _BaseAssistant._WHISPER_SIZE, device="cpu", compute_type="int8"
            )
            print(f"  [Whisper] 模型加载完成，耗时 {_t.time()-t0:.1f}s")
        return _BaseAssistant._whisper_model

    @staticmethod
    def _transcribe_audio(video_path):
        """用 faster-whisper 转写视频音频。返回 (lang, [(start, end, text)...])。
        若视频无音频或转写失败，返回 ("", [])。"""
        import tempfile
        from moviepy import VideoFileClip
        clip = VideoFileClip(video_path)
        if not clip.audio:
            clip.close()
            return "", []
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp.close()
        try:
            clip.audio.write_audiofile(tmp.name, logger=None, fps=16000, nbytes=2)
            clip.close()
            model = _BaseAssistant._get_whisper()
            import time as _t
            t0 = _t.time()
            segments, info = model.transcribe(tmp.name, beam_size=5, vad_filter=True)
            segs = [(s.start, s.end, s.text.strip()) for s in segments]
            print(f"  [Whisper] 转写完成: lang={info.language} ({info.language_probability:.2f}), "
                  f"{len(segs)} 段, 耗时 {_t.time()-t0:.1f}s")
            return info.language, segs
        except Exception as e:
            print(f"  [Whisper] 转写失败: {e}")
            return "", []
        finally:
            try:
                os.unlink(tmp.name)
            except Exception:
                pass

    @staticmethod
    def _format_transcript(lang, segments):
        """把转写片段格式化成可塞入 prompt 的文本。"""
        if not segments:
            return ""
        lines = [f"[{s:.1f}s-{e:.1f}s] {t}" for s, e, t in segments]
        return f"(detected language: {lang})\n" + "\n".join(lines)

    def pick_highlight_segments_gemini(self, video_path,
                                        max_segments=3, max_total_sec=12.0,
                                        max_segment_sec=4.0):
        """让 Gemini 直接看视频选高光段（含画面+原音），返回
        [{start,end,original,translation,reason}]。
        无 GEMINI_API_KEY 或失败时返回 []，调用方据此跳过原音叠加。
        """
        if not video_path or not os.path.exists(video_path):
            return []
        if not (os.environ.get("GEMINI_API_KEY", "")
                or _BaseAssistant._GEMINI_API_KEY):
            self._log("[Highlight-Gemini] 未设置 GEMINI_API_KEY，跳过")
            return []

        prompt = (
            f"你是 NBA 短视频剪辑师。请观看这段视频（含画面与原始音轨），"
            f"挑出最多 {max_segments} 段适合在二次创作成片中"
            f"\"保留原音播放\"的精彩片段。\n\n"
            f"目标：让观众听到现场感（解说、球员/教练原话、关键互动），"
            f"而非只听后期中文配音。\n\n"
            f"挑选准则：\n"
            f"- 优先：解说员高潮、球员/教练金句、采访关键句、现场情绪点、有画面冲击的对话\n"
            f"- 排除：纯背景噪音/欢呼、广告、明显 BGM 歌词、空洞语气词、画外音空白\n"
            f"- 单段时长 1.5-{max_segment_sec}s，所选片段总时长 ≤ {max_total_sec}s，不要重叠\n"
            f"- 时间戳必须精确到 0.1s，对齐人声起止，不要把无声的尾巴算进去\n"
            f"- 宁缺毋滥，但若画面/音频里有像样的人声内容，至少挑 1 段\n\n"
            f"严格返回 JSON 数组（不要 markdown 代码块），每项格式：\n"
            f"{{\n"
            f'  "start": 起始秒（float, 相对视频开头）,\n'
            f'  "end": 结束秒（float）,\n'
            f'  "original": 原音的英文/原文转录,\n'
            f'  "translation": 中文翻译，≤20字，单行字幕用,\n'
            f'  "reason": 为什么值得保留原音，≤25字\n'
            f"}}\n"
            f"若全是噪音/无意义内容，返回空数组 []。"
        )

        try:
            raw = self._analyze_video_gemini(video_path, prompt) or ""
        except Exception as e:
            self._log(f"[Highlight-Gemini] 调用失败: {e}")
            return []

        self._log(f"[Highlight-Gemini raw]\n{_truncate(raw, 300)}")

        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()

        try:
            picks = json.loads(raw)
        except Exception as e:
            self._log(f"[Highlight-Gemini] JSON 解析失败: {e}; 原始: {raw[:300]}")
            return []
        if not isinstance(picks, list):
            self._log(f"[Highlight-Gemini] 返回非数组: {str(picks)[:200]}")
            return []
        if not picks:
            self._log(f"[Highlight-Gemini] 返回空数组")
            return []

        results = []
        total = 0.0
        for p in picks:
            if not isinstance(p, dict):
                continue
            translation = (p.get("translation") or p.get("caption") or "").strip()
            if not translation:
                continue
            try:
                s = float(p["start"]); e = float(p["end"])
            except Exception:
                continue
            if e <= s:
                continue
            dur = e - s
            if dur > max_segment_sec:
                e = s + max_segment_sec
                dur = max_segment_sec
            results.append({
                "start": s,
                "end": e,
                "original": (p.get("original") or "").strip(),
                "translation": translation,
                "reason": (p.get("reason") or "").strip(),
            })
            total += dur
            if len(results) >= max_segments or total >= max_total_sec:
                break

        results.sort(key=lambda x: x["start"])
        cleaned = []
        for r in results:
            if cleaned and r["start"] < cleaned[-1]["end"]:
                continue
            cleaned.append(r)

        self._log(f"[Highlight-Gemini] 挑出 {len(cleaned)} 段，总时长 "
                  f"{sum(r['end']-r['start'] for r in cleaned):.1f}s")
        return cleaned

    @staticmethod
    def _call_claude_vision(prompt, frames_b64, audio_b64="", max_tokens=800):
        """单次 Claude 视觉请求：发送给定的若干帧（+ 可选音频）。
        音频 type 字段必须是 "audio"（不是 "input_audio"，那是 OpenAI schema）。"""
        content = [{"type": "text", "text": prompt}]
        for b64 in frames_b64:
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": b64},
            })
        if audio_b64:
            content.append({
                "type": "audio",
                "source": {"type": "base64", "media_type": "audio/mp3", "data": audio_b64},
            })
        body = json.dumps({
            "model": _BaseAssistant._CLAUDE_VISION_MODEL,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": content}],
        }).encode("utf-8")
        import time as _t
        for attempt in range(3):
            req = urllib.request.Request(
                _BaseAssistant._CLAUDE_VISION_ENDPOINT,
                data=body,
                headers={"Content-Type": "application/json", "anthropic-version": "2023-06-01"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                for block in data.get("content", []):
                    if block.get("type") == "text":
                        return block["text"].strip()
                return ""
            except urllib.error.HTTPError as e:
                err_body = ""
                try:
                    err_body = e.read().decode("utf-8", errors="replace")[:400]
                except Exception:
                    pass
                if attempt < 2:
                    wait = (attempt + 1) * 10
                    print(f"  [Claude Vision] 单批尝试{attempt+1}/3 失败: HTTP {e.code} {err_body}，{wait}s 后重试")
                    _t.sleep(wait)
                else:
                    print(f"  [Claude Vision] 单批3次均失败: HTTP {e.code} {err_body}")
                    raise
            except Exception as e:
                if attempt < 2:
                    wait = (attempt + 1) * 10
                    print(f"  [Claude Vision] 单批尝试{attempt+1}/3 失败: {e}，{wait}s 后重试")
                    _t.sleep(wait)
                else:
                    print(f"  [Claude Vision] 单批3次均失败: {e}")
                    raise

    @staticmethod
    def _analyze_video_claude_frames(video_path, prompt, frame_times=None):
        """抽帧 + Whisper 转写 + Claude 音乐氛围；图像每 2 帧一批，最后合并。"""
        import time as _t
        _start = _t.time()
        n_frames = len(frame_times) if frame_times else 8
        print(f"  [Claude Vision] 抽取 {n_frames} 帧" + (" (指定时间点)" if frame_times else ""))
        frames = _BaseAssistant._extract_frames_b64(video_path, n=n_frames, frame_times=frame_times)

        # 1) Whisper 转写人声
        lang, segs = _BaseAssistant._transcribe_audio(video_path)
        transcript = _BaseAssistant._format_transcript(lang, segs)

        batch_size = _BaseAssistant._CLAUDE_VISION_BATCH
        total = len(frames)
        n_image_batches = (total + batch_size - 1) // batch_size
        print(f"  [Claude Vision] 帧提取完成: {total} 帧, 转写: {len(segs)} 段, "
              f"图像批: {n_image_batches}×{batch_size}帧, "
              f"模型: {_BaseAssistant._CLAUDE_VISION_MODEL}")

        partials = []

        if transcript:
            partials.append(f"[音频转写 (Whisper)]\n{transcript}")

        # 3) 图像批：每批 2 帧，纯图像
        for i in range(0, total, batch_size):
            batch_frames = frames[i:i + batch_size]
            batch_idx = i // batch_size + 1
            batch_prompt = (
                f"{prompt}\n\n"
                f"【本批为分批分析的第 {batch_idx}/{n_image_batches} 图像批，共 {len(batch_frames)} 帧 "
                f"(整段第 {i+1}-{i+len(batch_frames)} 帧)。请只针对本批帧描述。】"
            )
            try:
                t0 = _t.time()
                part = _BaseAssistant._call_claude_vision(batch_prompt, batch_frames, "")
                print(f"  [Claude Vision] 图像批 {batch_idx}/{n_image_batches} 完成，耗时 {_t.time()-t0:.1f}s，"
                      f"结果 {len(part)} 字符")
                if part:
                    partials.append(f"[图像批{batch_idx}] {part}")
            except Exception as e:
                print(f"  [Claude Vision] 图像批 {batch_idx}/{n_image_batches} 失败: {e}")

        if not partials:
            print(f"  [Claude Vision] 全部批次均无结果，总耗时 {_t.time()-_start:.1f}s")
            return ""
        if len(partials) == 1:
            print(f"  [Claude Vision] 单批返回，总耗时 {_t.time()-_start:.1f}s")
            return partials[0].split("] ", 1)[-1] if "] " in partials[0] else partials[0]
        merged = "\n\n".join(partials)
        print(f"  [Claude Vision] 合并 {len(partials)} 批结果，总耗时 {_t.time()-_start:.1f}s")
        return merged

    # ── GPT 抽帧分析 fallback ─────────────────────────────────

    @staticmethod
    def _analyze_video_frames_gpt(video_path, prompt, endpoint_url, headers):
        """抽取 8 帧发送给 GPT-4o 视觉模型进行分析（fallback）。"""
        frames = _BaseAssistant._extract_frames_b64(video_path, n=8)
        content = [{"type": "text", "text": prompt}]
        for b64 in frames:
            content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})

        body = json.dumps({
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": content}],
            "max_tokens": 500,
        }).encode("utf-8")
        req = urllib.request.Request(url=endpoint_url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"].strip()

    def polish_translation(self, original_text, raw_translation):
        """优化翻译，使其更自然流畅、适合短视频展示"""
        prompt = (
            f"请优化以下推特翻译，要求：\n"
            f"1. 简洁有力，适合竖屏短视频字幕展示\n"
            f"2. 保留原意但语言更地道\n"
            f"3. 控制在50字以内\n"
            f"4. 只返回优化后的翻译文本，不要添加任何解释\n\n"
            f"原文: {original_text}\n"
            f"当前翻译: {raw_translation}"
        )
        result = self._call(prompt)
        return result.strip().strip('"').strip("'") if result else raw_translation

    def analyze_video_content(self, video_path, original_text="", author=""):
        """双 Agent 视频分析：Gemini 直传 + Claude 抽帧+音频，最后总结合并。"""
        import concurrent.futures

        ref_info = f"参考信息 — 作者: {author}，推文原文: {original_text}" if (author or original_text) else ""

        gemini_prompt = (
            f"你是专业短视频内容分析师。请分析这个视频讲述的内容和事件：\n"
            f"1. 视频在讲什么事件/新闻/故事？涉及哪些人物？\n"
            f"2. 视频中出现的文字、字幕、旁白说了什么？请转录关键对话。\n"
            f"3. 这个事件的背景和意义是什么？\n"
            f"{ref_info}\n200字以内，用中文，只返回描述。"
        )
        claude_prompt = (
            f"你是专业短视频内容分析师。以下是一个视频的关键帧截图和音频。\n"
            f"请分析：\n"
            f"1. 画面中出现的人物（如能识别身份请指出）、场景、动作\n"
            f"2. 画面中出现的所有文字、字幕、标题、品牌logo\n"
            f"3. 音频中的对话/旁白（请转录原文）\n"
            f"4. 音频的音乐风格和氛围\n"
            f"{ref_info}\n200字以内，用中文。"
        )

        gemini_result = ""
        claude_result = ""

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = {}
            if os.environ.get("GEMINI_API_KEY", "") or self._GEMINI_API_KEY:
                self._log("视频分析: 启动 Gemini 直传视频 agent")
                futures["gemini"] = pool.submit(self._analyze_video_gemini, video_path, gemini_prompt)
            self._log("视频分析: 启动 Claude 抽帧+音频 agent")
            futures["claude"] = pool.submit(self._analyze_video_claude_frames, video_path, claude_prompt)

            for name, f in futures.items():
                try:
                    result = f.result(timeout=150)
                    if name == "gemini":
                        gemini_result = result or ""
                    else:
                        claude_result = result or ""
                    if result:
                        self._log(f"视频分析: {name} agent 完成")
                        _lim = 200 if name == "gemini" else 300
                        self._log(f"[视频分析-{name} raw]\n{_truncate(result, _lim)}")
                    else:
                        self._log(f"视频分析: {name} agent 返回为空", "warn")
                except Exception as e:
                    self._log(f"视频分析: {name} agent 失败: {e}", "warn")

        # 只有一个成功 → 直接返回
        if gemini_result and not claude_result:
            return gemini_result
        if claude_result and not gemini_result:
            return claude_result

        # 两个都成功 → 总结 agent 合并
        if gemini_result and claude_result:
            self._log("视频分析: 启动总结 agent 合并结果")
            summary_prompt = (
                f"你是内容编辑。以下是两个AI对同一视频的分析结果。\n"
                f"Gemini 能听到完整音频和看到连续画面，Claude 的画面细节更精准。\n"
                f"请综合两者，取各自之长，去除重复和矛盾，输出200字以内的最终视频内容描述。\n\n"
                f"=== Gemini 分析（含音频）===\n{gemini_result}\n\n"
                f"=== Claude 分析（画面细节）===\n{claude_result}\n\n"
                f"{ref_info}"
            )
            result = self._call(summary_prompt)
            if result and result.strip():
                self._log(f"[视频分析-summary raw]\n{result.strip()}")
                return result.strip()
            return gemini_result  # 总结失败则返回 Gemini 结果

        # 都失败 → fallback GPT-4o 抽帧
        gpt_endpoint = os.environ.get("GPT_VISION_ENDPOINT", "http://localhost:23333/api/openai/v1/chat/completions")
        fallback_prompt = (
            f"你是专业短视频内容分析师。以下是一个视频的关键帧截图。\n"
            f"请分析视频内容：人物、事件、文字、背景。\n"
            f"{ref_info}\n200字以内，用中文。"
        )
        try:
            self._log("视频分析: 双 agent 均失败，fallback 到 GPT-4o 抽帧")
            return self._analyze_video_frames_gpt(
                video_path, fallback_prompt,
                endpoint_url=gpt_endpoint,
                headers={"Content-Type": "application/json"},
            )
        except Exception as e:
            self._log(f"视频分析: GPT-4o fallback 也失败: {e}", "error")
            return ""

    def generate_commentary(self, original_text, translation, author="", has_video=False, video_description="", target_duration=0):
        """生成解说词（不是简单翻译，而是有解说感的旁白）"""
        try:
            from .style_guide import STYLE_EXAMPLES, COMMENTARY_RULES, PLAYER_NICKNAMES, FORBIDDEN_WORDS
            examples_text = "\n".join(f"范例{i+1}: {ex}" for i, ex in enumerate(STYLE_EXAMPLES[:3]))
            forbidden = "、".join(FORBIDDEN_WORDS)
            nickname_hint = ""
            for eng, cn in PLAYER_NICKNAMES.items():
                if cn and eng.lower() in (author or "").lower():
                    nickname_hint = f"（圈内昵称：{cn}）"
                    break
            nickname_table = "\n".join(
                f"  {eng} = {cn}" for eng, cn in PLAYER_NICKNAMES.items() if cn
            )
        except ImportError:
            examples_text = ""
            forbidden = "公开表态、隔空致意、展现了、彰显了、以此表达、认可与致敬"
            nickname_hint = ""
            nickname_table = ""

        video_hint = ""
        if has_video:
            video_hint = (
                "这条推文带有视频素材，你的解说要像体育评论员解说画面一样，"
                "结合视频内容描述，语气更有现场感。\n"
            )
            if video_description:
                video_hint += f"视频内容分析：{video_description}\n"

        # 根据视频时长计算目标字数（中文 TTS 约 4 字/秒，预留首尾各 1.5s）
        if target_duration > 0:
            available_seconds = max(target_duration - 3, 8)
            target_chars = int(available_seconds * 4)
            min_chars = max(target_chars - 20, 60)
            max_chars = target_chars + 20
            length_rule = f"1. {min_chars}-{max_chars}字（视频时长{target_duration:.0f}秒，解说需要贯穿全程），像跟兄弟聊天\n"
        else:
            length_rule = f"1. 80-150字，4-6个短句，像跟兄弟聊天\n"

        prompt = (
            f"你是篮球邮差Melo风格的NBA短视频博主。请模仿以下范例的解说风格：\n\n"
            f"{examples_text}\n\n"
            f"==========================================\n"
            f"⚠️ 最高优先级：解说词是整个视频的灵魂，必须同时满足【完整】+【顺畅可读】\n"
            f"==========================================\n"
            f"【完整性硬要求】\n"
            f"- 必须有清晰的开头-发展-结尾三段，不能戛然而止、不能半截话、不能开头没引入直接细节\n"
            f"- 推文/视频里的核心事实（人物+事件+关键细节+结果）必须交代完整，不能只说一半就跳走\n"
            f"- 不许出现'……''等等''之类的'这种偷懒省略，每句都要把意思讲完\n"
            f"- 结尾必须真的收住（个人观点/情绪反问/价值判断），不能像没写完一样断在半空\n\n"
            f"【可读性硬要求】\n"
            f"- 念出来必须自然顺口，TTS 朗读不能有奇怪停顿。读不通就是失败\n"
            f"- 短句优先，每句 8-20 字，长句必须拆开。句与句之间逻辑连贯，不能跳跃\n"
            f"- 避免书面/翻译腔结构（'对于...来说''就...而言''在...的情况下'）\n"
            f"- 避免连续叠加形容词或并列长定语，宁可分两句也别堆在一句里\n"
            f"- 写完默念一遍：如果普通人朗读会卡壳/上气不接下气，就重写\n"
            f"==========================================\n\n"
            f"【核心风格规则】\n"
            f"{length_rule}"
            f"2. 开头第一句必须是'XXX今日发推'或'XXX今日转推'（用球星昵称），然后紧接情绪钩子\n"
            f"   例如：'老詹今日发推，直接放出了一段超燃视频。' / '欧文今日转推，力挺好兄弟鲁卡。'\n"
            f"3. 中间引述事件细节，用'他表示''说道'做引用过渡\n"
            f"4. 结尾必须有个人观点/情绪判断/反问，如'真的太善良了''算是得到认可了吗'\n"
            f"5. 事实占45%，个人观点评论占55%\n"
            f"6. 必须使用口语词：真的、太、算是、天啊、好家伙、没得说、直接、拉满\n"
            f"7. 绝对禁用书面套话：{forbidden}\n"
            f"8. 只返回解说词本身，不加引号、标题、解释\n"
            f"9. 【标点格式强制要求】每个短句必须以句号、感叹号或问号结尾。句内停顿用逗号。"
            f"禁止用空格代替标点。禁止省略句末标点。\n"
            f"10. 【球员译名强制规则】凡涉及NBA球员，必须严格使用下表译名，禁止生造、禁止使用表外昵称：\n"
            f"{nickname_table}\n"
            f"   表外球员一律采用国内主流篮球媒体（如腾讯体育/虎扑）通用音译，"
            f"绝不可凭感觉造名（例如不得把Wembanyama叫'华师'）。如不确定，宁可用全名音译也不要生造昵称。\n"
            f"{video_hint}\n"
            f"球星: {author}{nickname_hint}\n"
            f"原文: {original_text}\n"
            f"翻译: {translation}\n\n"
            f"再次提醒：返回前在心里默念一遍解说词，确认【完整、顺口、能让人听懂】才提交。"
        )
        result = self._call(prompt)
        if result:
            # 后处理：修复空格分隔的问题，确保标点规范
            text = result.strip().strip('"').strip("'")
            # 把中文字符之间的空格替换成逗号（AI 有时用空格代替标点）
            text = re.sub(r'([\u4e00-\u9fff])\s+([\u4e00-\u9fff])', r'\1，\2', text)
            # 重复处理（上面的正则一次只替换一对）
            text = re.sub(r'([\u4e00-\u9fff])\s+([\u4e00-\u9fff])', r'\1，\2', text)
            return text
        return translation

    def recommend_music_claude(self, blog_content, author=""):
        """推荐最适合的配乐歌曲（英文 prompt，更适合音乐推荐）"""
        desc = f"{author}: {blog_content}" if author else blog_content
        desc = desc[:200].replace('"', "'").replace("\n", " ")
        prompt = (
            f"provide a most suitable music for this message or blog: {desc}. "
            f"Reply with ONLY the song name and artist in format: Song Name - Artist. Nothing else."
        )
        result = self._call(prompt)
        if result:
            song = result.strip().strip('"').strip("'").split("\n")[0].strip()
            song = re.sub(r'\*+', '', song).strip()
            if song and len(song) > 3:
                self._log(f"推荐歌曲: {song}")
                return song
        return None

    def extract_video_dialogue(self, video_description, original_text="", author=""):
        """从视频分析结果中提取关键对话/旁白并翻译为中文字幕，供静默字幕段使用。"""
        if not video_description:
            return []
        prompt = (
            f"以下是对一个视频的内容分析。请从中提取视频里的关键对话或旁白，翻译成简短的中文字幕。\n\n"
            f"=== 视频分析 ===\n{video_description}\n\n"
            f"要求：\n"
            f"1. 只提取视频中人物实际说的话（对话/旁白），不要提取画面描述\n"
            f"2. 每条字幕控制在20字以内\n"
            f"3. 如果原文是英文，翻译成地道的中文\n"
            f"4. 如果没有对话/旁白，返回空\n"
            f"5. 每行一条字幕，不要编号，不要引号\n"
            f"6. 最多5条"
        )
        result = self._call(prompt)
        if not result or not result.strip():
            return []
        self._log(f"[视频字幕提取 raw]\n{result.strip()}")
        lines = [l.strip() for l in result.strip().split("\n") if l.strip() and len(l.strip()) > 2]
        return lines[:5]

    def select_bgm_from_library(self, tweet_text, translation, author="", bgm_dir=""):
        """从本地 BGM 库中选择最适合推文内容的背景音乐，返回文件名或空字符串。"""
        if not bgm_dir or not os.path.isdir(bgm_dir):
            return ""
        files = [f for f in os.listdir(bgm_dir) if f.endswith((".ogg", ".mp3", ".wav"))]
        if not files:
            return ""
        file_list = "\n".join(f"- {f}" for f in files)
        prompt = (
            f"你是短视频配乐师。根据以下推文内容，从BGM库中选择最合适的一首背景音乐。\n\n"
            f"推文作者: {author}\n"
            f"推文原文: {tweet_text}\n"
            f"翻译: {translation}\n\n"
            f"可选BGM文件：\n{file_list}\n\n"
            f"选择标准：根据推文的情绪（励志/感伤/热血/轻松/怀旧等）匹配歌曲风格。\n"
            f"只返回文件名，不要解释。"
        )
        result = self._call(prompt).strip().strip('"').strip("'")
        # 验证返回的文件名在列表中
        for f in files:
            if f in result or os.path.splitext(f)[0] in result:
                self._log(f"BGM 库选曲: {f}")
                return f
        return ""

    def recommend_song(self, tweet_text, translation, author=""):
        """推荐一首具体的适合作为背景音乐的歌曲"""
        content = translation or tweet_text
        result = self.recommend_music_claude(content, author)
        if result and " - " in result:
            return result

        prompt = (
            f"为以下NBA球星推特短视频推荐一首背景歌曲。\n\n"
            f"要求：\n"
            f"1. 必须是在 SoundCloud 或 YouTube 上能搜到的知名歌曲\n"
            f"2. 节奏感强，适合10-15秒短视频\n"
            f"3. 根据推文情绪选择合适风格\n"
            f"4. 格式：歌名 - 歌手（只返回一行）\n\n"
            f"作者: {author}\n"
            f"推文: {tweet_text}\n"
            f"翻译: {translation}"
        )
        result = self._call(prompt).strip().strip('"').strip("'")
        if result and " - " in result:
            return result
        return "Unstoppable - Sia"

    def recommend_mood(self, tweet_text, translation):
        """根据推文内容推荐配乐氛围"""
        prompt = (
            f"根据以下推特内容，推荐最合适的背景音乐氛围。\n"
            f"只能从这三个选项中选一个: chill, hype, emotional\n"
            f"只返回一个单词，不要解释。\n\n"
            f"推文: {tweet_text}\n"
            f"翻译: {translation}"
        )
        result = self._call(prompt).strip().lower()
        if result in ("chill", "hype", "emotional"):
            return result
        return "chill"

    def review_video(self, video_info, video_path=None, subtitle_timeline=None):
        """审阅推特短视频质量，以参考视频风格为标杆。"""
        # 双 agent 分析视频内容
        video_analysis = ""
        subtitle_check = ""
        if video_path and os.path.exists(video_path):
            import concurrent.futures

            # Gemini review prompt（针对内容质量评审）
            review_prompt = (
                f"你是专业短视频审阅员。请从以下维度审阅这个视频：\n\n"
                f"=== 预期内容 ===\n"
                f"解说词: {video_info.get('commentary', '')}\n"
                f"推文原文: {video_info.get('original_text', '')}\n"
                f"翻译: {video_info.get('translation', '')}\n"
                f"作者: {video_info.get('author', '')}\n"
                f"视频内容分析: {video_info.get('video_description', '')}\n\n"
                f"请严格评估：\n"
                f"1. 内容准确性【最重要】：解说词是否忠实于推文原文的事实？有无添油加醋、张冠李戴、编造细节？\n"
                f"2. 视频匹配度：如果有原始推文视频，解说词是否与视频画面内容一致？有无描述了视频中不存在的内容？\n"
                f"3. 配音内容：旁白说的话是否和解说词一致？有无漏读、错读？\n"
                f"4. 配音风格：语气是否像跟朋友聊天？还是像念稿？是否有情绪起伏？\n"
                f"5. 配乐配合：BGM风格是否匹配内容情绪？音量是否合适？\n"
                f"6. 整体节奏：视频节奏是否流畅？有无尴尬的空白或太赶的部分？\n"
                f"7. 如果发现事实错误或与推文/视频不符的地方，请明确指出并给出修改建议。\n"
                f"200字以内，用中文，具体指出问题。"
            )

            # Claude 精确字幕校对 + 内容评审：按字幕时间点抽帧
            if subtitle_timeline:
                frame_times = [start + dur / 2 for _, start, dur in subtitle_timeline]
                checklist = "\n".join(
                    f"帧{i+1} ({frame_times[i]:.1f}s): 预期字幕「{text}」"
                    for i, (text, _, _) in enumerate(subtitle_timeline)
                )
                claude_prompt = (
                    f"你是短视频审阅员。以下是一个视频的关键帧截图（每帧对应一条字幕）和音频。\n\n"
                    f"=== 预期内容 ===\n"
                    f"解说词: {video_info.get('commentary', '')}\n"
                    f"推文原文: {video_info.get('original_text', '')}\n"
                    f"翻译: {video_info.get('translation', '')}\n"
                    f"作者: {video_info.get('author', '')}\n\n"
                    f"=== 字幕校对清单 ===\n{checklist}\n\n"
                    f"请完成两项任务：\n\n"
                    f"【任务1: 字幕校对】\n"
                    f"逐帧对比画面中实际显示的字幕和预期字幕，报告不匹配。\n\n"
                    f"【任务2: 内容评审】\n"
                    f"1. 画面中的人物、场景是否与解说词描述一致？\n"
                    f"2. 解说词有无编造画面中不存在的内容？\n"
                    f"3. 解说词是否忠实于推文原文的事实？\n"
                    f"4. 字幕位置、大小、可读性如何？\n\n"
                    f"请分两部分回答：先字幕校对结果，再内容评审意见。"
                )
            else:
                frame_times = None
                claude_prompt = review_prompt

            gemini_result = ""
            claude_result = ""
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                futures = {}
                if os.environ.get("GEMINI_API_KEY", "") or self._GEMINI_API_KEY:
                    futures["gemini"] = pool.submit(self._analyze_video_gemini, video_path, review_prompt)
                futures["claude"] = pool.submit(
                    self._analyze_video_claude_frames, video_path, claude_prompt,
                    frame_times)
                for name, f in futures.items():
                    try:
                        r = f.result(timeout=150)
                        if name == "gemini":
                            gemini_result = r or ""
                        else:
                            claude_result = r or ""
                        if r:
                            self._log(f"[Review-{name} raw]\n{_truncate(r, 300)}")
                    except Exception:
                        pass

            if subtitle_timeline:
                subtitle_check = claude_result
                video_analysis = gemini_result
            elif gemini_result and claude_result:
                video_analysis = f"[Gemini] {gemini_result}\n[Claude] {claude_result}"
            else:
                video_analysis = gemini_result or claude_result

        # 加载风格参考
        try:
            from .style_guide import STYLE_EXAMPLES, FORBIDDEN_WORDS
            ref_examples = "\n".join(f"  范例{i+1}: {ex[:80]}..." for i, ex in enumerate(STYLE_EXAMPLES[:3]))
            forbidden = "、".join(FORBIDDEN_WORDS)
        except ImportError:
            ref_examples = ""
            forbidden = "公开表态、隔空致意、展现了、彰显了"

        prompt = (
            f"你是一个严格的短视频审阅员。以篮球邮差Melo的风格为标杆审阅以下推特短视频。\n\n"
            f"=== 参考标杆风格 ===\n{ref_examples}\n\n"
            f"=== 视频元信息 ===\n"
            f"解说词: {video_info.get('commentary', '')}\n"
            f"翻译文本: {video_info.get('translation', '')}\n"
            f"作者: {video_info.get('author', '')}\n"
            f"背景音乐: {video_info.get('bgm_song', '合成音乐')}\n"
            f"配乐氛围: {video_info.get('mood', '')}\n"
            f"有配音: {video_info.get('has_narration', False)}\n"
            f"时长: {video_info.get('duration', 0)}秒\n"
            f"分辨率: {video_info.get('resolution', '')}\n"
            f"有音频: {video_info.get('has_audio', False)}\n"
            f"文件大小: {video_info.get('file_size_mb', 0)}MB\n"
            f"有原始推文视频: {video_info.get('has_source_video', False)}\n\n"
        )
        if video_analysis:
            prompt += f"=== 视频内容分析 ===\n{video_analysis}\n\n"
        if subtitle_check:
            prompt += f"=== 字幕校对结果（Claude 逐帧比对）===\n{subtitle_check}\n\n"

        prompt += (
            f"严格评分标准（满分100，90分以上才算A级合格，大部分视频应在60-80分）：\n"
            f"【评分纪律】不要给人情分。没有明确证据表现优秀的项目，给中等分而非高分。\n"
            f"只有各项都真正出色才能达到90+。首轮生成几乎不可能达到A级。\n\n"
            f"⚠️ 解说词是整个视频的灵魂，【完整性】+【可读性】是最高优先级，"
            f"任何半截话/戛然而止/翻译腔/朗读卡壳的情况都要在评论里明确指出，并在内容准确性和解说风格里重扣。\n\n"
            f"1. 内容准确性（25分）【一票否决项】：\n"
            f"   - 解说词是否忠实于推文原文？有无编造、夸大、张冠李戴？（10分）\n"
            f"   - 【完整性】开头-发展-结尾是否齐全？事实是否交代完？结尾是否真的收住、没有半截话？（5分）\n"
            f"   - 如果有视频，解说词是否与视频画面一致？（5分）\n"
            f"   - 字幕与解说词是否完全匹配？（5分）\n"
            f"   - 存在事实错误或解说词不完整则此项直接0分\n"
            f"2. 解说风格匹配度（25分）：\n"
            f"   - 是否用球星昵称开头+情绪钩子？（6分）\n"
            f"   - 是否有个人观点/吐槽/反问结尾？（6分）\n"
            f"   - 【可读性】念出来是否自然顺口？短句为主、无翻译腔、TTS 朗读不卡壳？（5分）\n"
            f"   - 事实vs评论比例是否约45:55？（3分）\n"
            f"   - 是否使用口语词（真的、太、算是、天啊等）？（3分）\n"
            f"   - 是否避免了书面套话（{forbidden}）？（2分）\n"
            f"3. 配乐质量（15分）：风格是否匹配内容情绪，音量比例是否合适\n"
            f"4. 配音效果（15分）：语音自然度，与配乐分层是否清晰\n"
            f"5. 视觉效果（10分）：画面清晰度，截图→视频过渡，字幕位置和可读性\n"
            f"6. 内容趣味（10分）：是否有信息增量、让人想看完，像跟朋友聊天不像念稿\n\n"
            f"请严格按以下JSON格式返回：\n"
            f'{{"score": 68, "grade": "C", '
            f'"details": {{"内容准确性": 18, "解说风格": 16, "配乐质量": 10, "配音效果": 10, "视觉效果": 7, "内容趣味": 7}}, '
            f'"subtitle_mismatches": ["第2句字幕显示XXX但解说词是YYY"], '
            f'"content_issues": ["解说词提到XXX但推文原文并未提及"], '
            f'"suggestions": ["具体建议1", "具体建议2"]}}'
        )

        # 最终评分 agent 也抽 8 帧+音频看视频；audio 用 Whisper 转写
        if video_path and os.path.exists(video_path):
            frames = self._extract_frames_b64(video_path, n=8)
            lang, segs = self._transcribe_audio(video_path)
            transcript = self._format_transcript(lang, segs)
            batch_size = self._CLAUDE_VISION_BATCH
            n_image_batches = (len(frames) + batch_size - 1) // batch_size
            partials = []

            if transcript:
                partials.append(f"[音频转写 (Whisper)]\n{transcript}")

            for i in range(0, len(frames), batch_size):
                batch_frames = frames[i:i + batch_size]
                batch_idx = i // batch_size + 1
                batch_prompt = (
                    f"{prompt}\n\n"
                    f"【本批为分批评审的第 {batch_idx}/{n_image_batches} 图像批，共 {len(batch_frames)} 帧 "
                    f"(整段第 {i+1}-{i+len(batch_frames)} 帧)。请只针对本批帧给出观察，"
                    f"不要返回最终 JSON，最终评分会在合并阶段统一输出。】"
                )
                try:
                    part = self._call_claude_vision(batch_prompt, batch_frames, "", max_tokens=800)
                    if part:
                        partials.append(f"[图像批{batch_idx}观察] {part}")
                        self._log(f"[Review-Claude图像批{batch_idx} raw]\n{_truncate(part, 200)}")
                except Exception:
                    pass
            system_msg = "你是专业短视频审阅员。必须以纯JSON格式返回结果，不要包含markdown代码块标记。评分要严格，不要给人情分。"
            if partials:
                merge_prompt = (
                    prompt
                    + "\n\n=== 各批观察（已分批分析） ===\n"
                    + "\n\n".join(partials)
                    + "\n\n请综合上述观察按要求 JSON 格式输出最终评分结果。"
                )
                result = self._call(merge_prompt, system=system_msg)
            else:
                result = self._call(prompt, system=system_msg)
        else:
            system_msg = "你是专业短视频审阅员。必须以纯JSON格式返回结果，不要包含markdown代码块标记。评分要严格，不要给人情分。"
            result = self._call(prompt, system=system_msg)

        self._log(f"[Review-最终评分 raw]\n{result}")

        try:
            cleaned = result.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[-1]
            if cleaned.endswith("```"):
                cleaned = cleaned.rsplit("```", 1)[0]
            cleaned = cleaned.strip()

            review = json.loads(cleaned)
            score = review.get("score", 0)
            if score >= 90:
                review["grade"] = "A"
            elif score >= 75:
                review["grade"] = "B"
            elif score >= 60:
                review["grade"] = "C"
            else:
                review["grade"] = "D"
            return review
        except (json.JSONDecodeError, ValueError):
            return {
                "score": 0,
                "grade": "F",
                "details": {},
                "suggestions": ["AI审阅解析失败，请重试"],
                "raw": result,
            }


class ClaudeAssistant(_BaseAssistant):
    """Claude 后端 — 通过 Agent Maestro Anthropic 端点调用"""

    CLAUDE_MODEL = "claude-opus-4.7-1m-internal"

    def __init__(self):
        self._endpoint = os.environ.get(
            "CLAUDE_API_ENDPOINT",
            "http://localhost:23333/api/anthropic/v1/messages",
        )
        self._model = self.CLAUDE_MODEL
        self._log(f"Claude API 端点: {self._endpoint}")

    def _call(self, prompt, system="你是一个专业的NBA篮球内容编辑和翻译。"):
        body = json.dumps({
            "model": self._model,
            "max_tokens": 1024,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        }).encode("utf-8")

        for attempt in range(3):
            req = urllib.request.Request(
                self._endpoint,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "anthropic-version": "2023-06-01",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                for block in data.get("content", []):
                    if block.get("type") == "text":
                        return block["text"].strip()
                return ""
            except Exception as e:
                wait = (attempt + 1) * 10  # 10s, 20s, 30s
                self._log(f"Claude API 尝试{attempt+1}/3 失败: {e}，{wait}s 后重试", "warn")
                if attempt < 2:
                    import time
                    time.sleep(wait)
        self._log("Claude API 3次尝试均失败", "error")
        return ""


class GptAssistant(_BaseAssistant):
    """Azure OpenAI GPT 后端"""

    def __init__(self):
        self.endpoint = os.environ.get(
            "AZURE_OPENAI_ENDPOINT",
            "https://ravensai.openai.azure.com/openai/responses",
        )
        self.api_version = os.environ.get("AZURE_OPENAI_API_VERSION", "2025-04-01-preview")
        self.api_key = os.environ.get("AZURE_OPENAI_API_KEY", "")
        self.model = os.environ.get("AZURE_OPENAI_MODEL", "gpt-5.4-mini")
        if not self.api_key:
            raise RuntimeError(
                "请设置环境变量 AZURE_OPENAI_API_KEY，例如：\n"
                "  $env:AZURE_OPENAI_API_KEY = 'your-key-here'"
            )

    def _call(self, prompt, system="你是一个专业的NBA篮球内容编辑和翻译。"):
        url = f"{self.endpoint}?api-version={self.api_version}"
        body = json.dumps({
            "model": self.model,
            "input": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        }).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "api-key": self.api_key,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        output = data.get("output", [])
        for item in output:
            if item.get("type") == "message":
                for c in item.get("content", []):
                    if c.get("type") == "output_text":
                        return c["text"]
        return ""


# ── 工厂函数 ──────────────────────────────────────────────

_DEFAULT_BACKEND = os.environ.get("AI_BACKEND", "claude").lower()


def get_assistant(backend=None, logger=None):
    """获取 AI 助手实例。backend: "claude" | "gpt"，默认读 AI_BACKEND 环境变量。"""
    choice = (backend or _DEFAULT_BACKEND).lower()
    if choice == "gpt":
        inst = GptAssistant()
    else:
        inst = ClaudeAssistant()
    if logger:
        inst._logger = logger
    return inst


# 向后兼容：直接 import AIAssistant 仍然可用
AIAssistant = get_assistant
