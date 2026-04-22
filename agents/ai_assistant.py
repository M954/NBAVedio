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
    def _frame_visual_metrics(img):
        """提取一帧的基础视觉线索，尽量减少只靠推文文案猜内容。"""
        import numpy as np

        hsv = np.array(img.convert("HSV"))
        ycbcr = np.array(img.convert("YCbCr"))
        gray = np.array(img.convert("L"), dtype=np.int16)

        hue = hsv[..., 0].astype(np.int16)
        sat = hsv[..., 1].astype(np.int16)
        val = hsv[..., 2].astype(np.int16)
        cr = ycbcr[..., 1].astype(np.int16)
        cb = ycbcr[..., 2].astype(np.int16)

        return {
            "brightness": int(gray.mean()),
            "contrast": int(gray.std()),
            "dark_ratio": int((val <= 45).mean() * 100),
            "bright_ratio": int((val >= 210).mean() * 100),
            "fog_ratio": int(((sat <= 45) & (val >= 70) & (val <= 220)).mean() * 100),
            "orange_ratio": round(float((((hue >= 8) & (hue <= 30) & (sat >= 90) & (val >= 80)).mean()) * 100), 2),
            "red_ratio": round(float(((((hue <= 8) | (hue >= 245)) & (sat >= 100) & (val >= 80)).mean()) * 100), 2),
            "gold_ratio": round(float((((hue >= 20) & (hue <= 45) & (sat >= 70) & (val >= 90)).mean()) * 100), 2),
            "skin_ratio": round(float((((cr >= 135) & (cr <= 180) & (cb >= 85) & (cb <= 150)).mean()) * 100), 2),
            "edge_strength": round(float((np.abs(np.diff(gray, axis=0)).mean() + np.abs(np.diff(gray, axis=1)).mean()) / 2), 2),
        }

    @staticmethod
    def _frame_visual_cues(metrics, motion_score=0.0):
        """把数值特征转成更容易被语言模型利用的线索。"""
        cues = []
        if metrics["dark_ratio"] >= 55 and metrics["fog_ratio"] >= 15:
            cues.append("暗色烟雾/棚拍布光")
        elif metrics["dark_ratio"] >= 70:
            cues.append("暗场打光为主")

        if metrics["skin_ratio"] >= 6:
            cues.append("人物近景明显")
        elif metrics["skin_ratio"] >= 1:
            cues.append("疑似人物出镜")

        if metrics["orange_ratio"] >= 1.0:
            cues.append("疑似篮球或橙色球体元素")
        if metrics["red_ratio"] >= 1.0:
            cues.append("疑似篮圈/红色道具元素")
        if metrics["gold_ratio"] >= 1.0:
            cues.append("金色球衣/字样元素")

        if metrics["bright_ratio"] >= 12 and metrics["dark_ratio"] >= 20:
            cues.append("明暗反差强")
        if metrics["contrast"] >= 55:
            cues.append("画面对比度高")
        if motion_score >= 18:
            cues.append("动作变化明显")

        return cues

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
            frame_metrics = []
            prev_frame = None
            for i, t in enumerate(times):
                if t >= dur:
                    continue
                frame = clip.get_frame(min(t, dur - 0.1))
                img = Image.fromarray(frame)
                metrics = _BaseAssistant._frame_visual_metrics(img)
                motion_score = 0.0
                if prev_frame is not None:
                    motion_score = float(np.mean(np.abs(frame.astype(np.int16) - prev_frame.astype(np.int16))))
                prev_frame = frame.astype(np.int16)

                frame_metrics.append((t, metrics, motion_score))
                cues = _BaseAssistant._frame_visual_cues(metrics, motion_score)

                desc = (
                    f"帧{i+1} ({t:.1f}s): 亮度={metrics['brightness']}, 对比度={metrics['contrast']}, "
                    f"暗区占比={metrics['dark_ratio']}%, 雾化感={metrics['fog_ratio']}%"
                )
                if cues:
                    desc += ", 线索=" + "、".join(cues)
                if t < 3:
                    desc += ", 开头画面"
                elif t > dur - 2:
                    desc += ", 结尾画面"

                analysis.append(desc)

            if frame_metrics:
                basketball_frames = sum(
                    1 for _, metrics, _ in frame_metrics
                    if metrics["orange_ratio"] >= 1.0 or metrics["red_ratio"] >= 1.0 or metrics["gold_ratio"] >= 1.0
                )
                portrait_frames = sum(1 for _, metrics, _ in frame_metrics if metrics["skin_ratio"] >= 1.0)
                studio_frames = sum(1 for _, metrics, _ in frame_metrics if metrics["dark_ratio"] >= 55 and metrics["fog_ratio"] >= 15)
                dynamic_frames = sum(1 for _, _, motion in frame_metrics if motion >= 18)

                summary_cues = []
                if basketball_frames >= 2:
                    summary_cues.append(f"篮球相关视觉线索出现在 {basketball_frames}/{len(frame_metrics)} 帧")
                if portrait_frames >= 2:
                    summary_cues.append(f"人物主体较明显 ({portrait_frames}/{len(frame_metrics)} 帧)")
                if studio_frames >= 2:
                    summary_cues.append(f"暗色烟雾/棚拍风格明显 ({studio_frames}/{len(frame_metrics)} 帧)")
                if dynamic_frames >= 2:
                    summary_cues.append(f"动作切换较多 ({dynamic_frames}/{len(frame_metrics)} 帧)")
                if studio_frames >= 2 and basketball_frames >= 2:
                    summary_cues.append("整体更像电影感/广告式篮球宣传片，不像比赛直播截取")

                if summary_cues:
                    analysis.append("整体视觉总结: " + "；".join(summary_cues))

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

    def analyze_video_content(self, video_path, original_text="", author=""):
        """分析推文自带视频的内容，返回视频描述供解说词生成参考"""
        frame_analysis = self._analyze_video_frames(video_path)
        if "失败" in frame_analysis:
            return ""
        prompt = (
            f"你是NBA短视频分析师。请优先依据视频画面本身的线索，而不是机械复述推文原文。\n"
            f"如果画面里没有直接证据，不要擅自补出具体电影名、品牌名、合作对象或剧情。\n"
            f"可以参考推文信息理解背景，但输出必须以画面中真正可见的主体、动作、道具、风格为主。\n\n"
            f"=== 视频帧技术分析 ===\n{frame_analysis}\n\n"
            f"=== 推文信息 ===\n"
            f"作者: {author}\n"
            f"推文原文: {original_text}\n\n"
            f"请用中文描述：\n"
            f"1. 先说画面里直接可见的主体、动作、道具、镜头风格\n"
            f"2. 再判断最可能的视频类型（比赛片段、训练、广告、采访、电影感宣传片等）\n"
            f"3. 如果不确定，用'像''疑似''更像'表达，不要装作确定\n"
            f"4. 除非视觉线索足够强，否则不要直接搬用推文里的片名或 IP 名称\n"
            f"5. 50字以内，只返回描述。"
        )
        result = self._call(prompt)
        return result.strip() if result else ""

    def generate_commentary(self, original_text, translation, author="", has_video=False, video_description=""):
        """生成解说词（不是简单翻译，而是有解说感的旁白）"""
        try:
            from .style_guide import STYLE_EXAMPLES, COMMENTARY_RULES, PLAYER_NICKNAMES, FORBIDDEN_WORDS
            examples_text = "\n".join(f"范例{i+1}: {ex}" for i, ex in enumerate(STYLE_EXAMPLES[:3]))
            forbidden = "、".join(FORBIDDEN_WORDS)
            # 查找球星昵称
            nickname_hint = ""
            for eng, cn in PLAYER_NICKNAMES.items():
                if eng.lower() in (author or "").lower():
                    nickname_hint = f"（圈内昵称：{cn}）"
                    break
        except ImportError:
            examples_text = ""
            forbidden = "公开表态、隔空致意、展现了、彰显了、以此表达、认可与致敬"
            nickname_hint = ""

        video_hint = ""
        if has_video:
            video_hint = (
                "这条推文带有视频素材，你的解说要像体育评论员解说画面一样，"
                "结合视频内容描述，语气更有现场感。\n"
            )
            if video_description:
                video_hint += f"视频内容分析：{video_description}\n"

        prompt = (
            f"你是篮球邮差Melo风格的NBA短视频博主。请模仿以下范例的解说风格：\n\n"
            f"{examples_text}\n\n"
            f"【核心风格规则】\n"
            f"1. 80-150字，4-6个短句，像跟兄弟聊天\n"
            f"2. 开头必须用球星昵称+情绪钩子（如'老詹争议言论持续发酵''真没想到'）\n"
            f"3. 中间引述事件细节，用'他表示''说道'做引用过渡\n"
            f"4. 结尾必须有个人观点/情绪判断/反问，如'真的太善良了''算是得到认可了吗'\n"
            f"5. 事实占45%，个人观点评论占55%\n"
            f"6. 必须使用口语词：真的、太、算是、天啊、好家伙、没得说、直接、拉满\n"
            f"7. 绝对禁用书面套话：{forbidden}\n"
            f"8. 只返回解说词本身，不加引号、标题、解释\n"
            f"{video_hint}\n"
            f"球星: {author}{nickname_hint}\n"
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
        """审阅推特短视频质量，以参考视频风格为标杆。"""
        # 从视频中提取详细信息
        frame_analysis = ""
        if video_path and os.path.exists(video_path):
            frame_analysis = self._analyze_video_frames(video_path)

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
        if frame_analysis:
            prompt += f"=== 视频帧分析 ===\n{frame_analysis}\n\n"

        prompt += (
            f"严格评分标准（满分100，90分以上才算A级合格）：\n"
            f"1. 解说风格匹配度（35分）：\n"
            f"   - 是否用球星昵称开头+情绪钩子？（10分）\n"
            f"   - 是否有个人观点/吐槽/反问结尾？（10分）\n"
            f"   - 事实vs评论比例是否约45:55？（5分）\n"
            f"   - 是否使用口语词（真的、太、算是、天啊等）？（5分）\n"
            f"   - 是否避免了书面套话（{forbidden}）？（5分）\n"
            f"2. 配乐质量（20分）：是否用了真实歌曲（合成音最高5分），风格是否匹配\n"
            f"3. 配音效果（15分）：语音自然度，与配乐分层是否清晰\n"
            f"4. 视觉效果（15分）：画面清晰度，截图→视频过渡，字幕位置\n"
            f"5. 内容趣味（15分）：是否有信息增量、让人想看完，像跟朋友聊天不像念稿\n\n"
            f"请严格按以下JSON格式返回：\n"
            f'{{"score": 85, "grade": "B", '
            f'"details": {{"解说风格": 20, "配乐质量": 15, "配音效果": 12, "视觉效果": 16, "内容趣味": 13}}, '
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
