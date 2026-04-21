"""AI Assistant - 双后端（Claude CLI / Azure OpenAI GPT）
用于翻译优化、解说词生成、配乐推荐、视频内容审阅

选择后端：
  - 环境变量 AI_BACKEND=claude 或 AI_BACKEND=gpt（默认 claude）
  - API 调用时可通过 backend 参数覆盖
"""
import json
import os
import re
import subprocess
import urllib.request


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

    @staticmethod
    def _analyze_video_frames(video_path):
        """从视频中提取关键帧并分析，生成文字描述供 AI 评分。"""
        try:
            from moviepy import VideoFileClip
            from PIL import Image
            import numpy as np

            clip = VideoFileClip(video_path)
            dur = clip.duration
            analysis = []
            analysis.append(f"视频总时长: {dur:.1f}秒, 分辨率: {clip.size[0]}x{clip.size[1]}, FPS: {clip.fps}")

            # 抽取5个时间点的帧
            times = [0.5, dur * 0.25, dur * 0.5, dur * 0.75, max(dur - 1, 0.5)]
            for i, t in enumerate(times):
                if t >= dur:
                    continue
                frame = clip.get_frame(min(t, dur - 0.1))
                img = Image.fromarray(frame)
                w, h = img.size

                # 分析帧特征
                arr = np.array(img)
                brightness = int(arr.mean())
                # 检查上半部分和下半部分的亮度差（判断是否有字幕区域）
                top_brightness = int(arr[:h//2].mean())
                bottom_brightness = int(arr[h//2:].mean())
                # 检查是否主要是暗色背景
                dark_ratio = int((arr < 40).mean() * 100)

                desc = f"帧{i+1} ({t:.1f}s): 亮度={brightness}"
                if dark_ratio > 50:
                    desc += ", 暗色背景为主"
                if abs(top_brightness - bottom_brightness) > 30:
                    desc += f", 上下亮度差={top_brightness - bottom_brightness}(可能有字幕)"
                if t < 3:
                    desc += ", 开头画面"
                elif t > dur - 2:
                    desc += ", 结尾画面"

                analysis.append(desc)

            # 音频分析
            if clip.audio:
                analysis.append(f"有音频轨道, 音频时长: {clip.audio.duration:.1f}秒")
            else:
                analysis.append("无音频轨道")

            clip.close()
            return "\n".join(analysis)
        except Exception as e:
            return f"视频分析失败: {e}"

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

    def generate_commentary(self, original_text, translation, author=""):
        """生成解说词（不是简单翻译，而是有解说感的旁白）"""
        prompt = (
            f"你是一个NBA短视频博主，用抖音/B站体育区的风格解说球星推特动态。\n\n"
            f"要求：\n"
            f"1. 总共30-50字，2-3个短句\n"
            f"2. 第一句直接点明球星做了什么（发推/转发/晒照片），带上球星名字\n"
            f"3. 第二句用大白话解读这条推文的意思或背景\n"
            f"4. 可以加一句简短的个人看法或吐槽，像跟朋友聊天\n"
            f"5. 语气要像说话不像写文章，可以用'哥们''直接''拉满''懂的都懂'这种口语\n"
            f"6. 绝对不要用：'公开表态''隔空致意''以此表达''认可与致敬''展现了''彰显了'这类书面套话\n"
            f"7. 只返回解说词本身，不要加引号、标题、解释\n\n"
            f"球星: {author}\n"
            f"原文: {original_text}\n"
            f"翻译: {translation}"
        )
        result = self._call(prompt)
        return result.strip().strip('"').strip("'") if result else translation

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

    def review_video(self, video_info, video_path=None):
        """审阅推特短视频质量。如果提供 video_path，会分析视频帧。"""
        # 从视频中提取详细信息
        frame_analysis = ""
        if video_path and os.path.exists(video_path):
            frame_analysis = self._analyze_video_frames(video_path)

        prompt = (
            f"你是一个严格的短视频审阅员。请审阅以下推特短视频：\n\n"
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
        if frame_analysis:
            prompt += f"=== 视频帧分析 ===\n{frame_analysis}\n\n"

        prompt += (
            f"严格评分标准（满分100，90分以上才算A级合格）：\n"
            f"1. 解说质量（30分）：是否像博主聊天而非念稿，是否解读了推文含义，是否有背景补充\n"
            f"2. 配乐质量（20分）：是否用了真实歌曲（合成音最高5分），风格是否匹配\n"
            f"3. 配音效果（15分）：语音自然度，与配乐分层是否清晰\n"
            f"4. 视觉效果（20分）：画面清晰度，截图→视频过渡是否流畅，字幕位置是否合理\n"
            f"5. 内容趣味（15分）：是否让人想看完，有没有信息增量\n\n"
            f"请严格按以下JSON格式返回：\n"
            f'{{"score": 85, "grade": "B", '
            f'"details": {{"解说质量": 20, "配乐质量": 15, "配音效果": 12, "视觉效果": 16, "内容趣味": 13}}, '
            f'"suggestions": ["具体建议1", "具体建议2"]}}'
        )
        system = "你是专业短视频审阅员。必须以纯JSON格式返回结果，不要包含markdown代码块标记。"
        result = self._call(prompt, system=system)

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
    """Claude CLI 后端"""

    CLAUDE_MODEL = "claude-opus-4-6"

    def __init__(self):
        # 查找 claude 可执行文件路径
        import shutil
        self._claude_cmd = shutil.which("claude") or shutil.which("claude.cmd")
        if not self._claude_cmd:
            # 常见 npm 全局安装路径
            npm_path = os.path.join(os.environ.get("APPDATA", ""), "npm", "claude.cmd")
            if os.path.exists(npm_path):
                self._claude_cmd = npm_path
            else:
                self._claude_cmd = "claude"
        self._log(f"Claude CLI 路径: {self._claude_cmd}")

    def _call(self, prompt, system="你是一个专业的NBA篮球内容编辑和翻译。"):
        full_prompt = f"{system}\n\n{prompt}"
        if len(full_prompt) > 4000:
            full_prompt = full_prompt[:4000]
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        import time as _time
        for attempt in range(3):
            try:
                _time.sleep(2)
                result = subprocess.run(
                    [self._claude_cmd, "--bare",
                     "--model", self.CLAUDE_MODEL],
                    input=full_prompt,
                    capture_output=True, text=True, timeout=60,
                    encoding="utf-8", env=env,
                )
                if result.returncode == 0 and result.stdout.strip():
                    return result.stdout.strip()
                if result.returncode != 0:
                    self._log(f"Claude CLI 尝试{attempt+1}/3 返回码={result.returncode}", "warn")
                else:
                    self._log(f"Claude CLI 尝试{attempt+1}/3 返回为空", "warn")
            except subprocess.TimeoutExpired:
                self._log(f"Claude CLI 尝试{attempt+1}/3 超时(60s)", "warn")
            except Exception as e:
                self._log(f"Claude CLI 尝试{attempt+1}/3 失败: {e}", "error")
                break
            import time as _time
            _time.sleep(1)
        self._log("Claude CLI 3次尝试均失败", "error")
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
