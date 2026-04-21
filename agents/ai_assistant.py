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
            f"你是NBA短视频的口播解说员，风格像聊天一样自然。根据以下推特内容写一段口播稿。\n\n"
            f"核心规则：\n"
            f"1. 简洁！控制在40-55字，像发微博一样精炼\n"
            f"2. 不要重复说同一个意思，每句话必须有新信息\n"
            f"3. 开头直接说事，不要'大家好'或'我们来看看'\n"
            f"4. 用一句话点明推文行为+态度，例如：\n"
            f"   - '库里转发詹姆斯的推文，直接喊话继续做你自己，力挺味儿拉满'\n"
            f"   - '字母哥晒出总冠军戒指，配文就一个字，冠军'\n"
            f"5. 再用一句话加点背景或点评，不要啰嗦：\n"
            f"   - '这对总决赛老冤家，场下反而最惺惺相惜'\n"
            f"   - '从这条推文能看出，他对这事儿态度很明确'\n"
            f"6. 禁止出现：'公开表态''隔空致意''以此表达了''认可与致敬'这些套话\n"
            f"7. 语气口语化，像跟哥们儿聊球\n\n"
            f"作者: {author}\n"
            f"原文: {original_text}\n"
            f"翻译参考: {translation}"
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

    def review_video(self, video_info):
        """审阅推特短视频质量"""
        prompt = (
            f"你是一个严格的短视频审阅员。请审阅以下推特短视频：\n\n"
            f"解说词: {video_info.get('commentary', '')}\n"
            f"翻译文本: {video_info.get('translation', '')}\n"
            f"作者: {video_info.get('author', '')}\n"
            f"背景音乐: {video_info.get('bgm_song', '合成音乐')}\n"
            f"配乐氛围: {video_info.get('mood', '')}\n"
            f"有配音: {video_info.get('has_narration', False)}\n"
            f"时长: {video_info.get('duration', 0)}秒\n"
            f"分辨率: {video_info.get('resolution', '')}\n"
            f"有音频: {video_info.get('has_audio', False)}\n"
            f"文件大小: {video_info.get('file_size_mb', 0)}MB\n\n"
            f"严格评分标准（满分100，90分以上才算A级合格）：\n"
            f"1. 解说质量（30分）：\n"
            f"   - 是否像解说员而非简单念翻译（10分）\n"
            f"   - 是否解读了推文行为（转发/引用/回复/原创）和态度（支持/反对/调侃）（10分）\n"
            f"   - 是否补充了背景信息（球员关系、事件背景）（10分）\n"
            f"   ※ 如果解说词只是翻译的简单改写没有解读，最高只给10分\n"
            f"2. 配乐质量（25分）：\n"
            f"   - 是否使用了真实歌曲而非合成音（15分）\n"
            f"   - 歌曲风格是否与推文情绪匹配（10分）\n"
            f"   ※ 使用合成音乐（sine wave）最高只给5分\n"
            f"3. 配音效果（15分）：语音自然流畅，配音和配乐是否分层清晰\n"
            f"4. 页面简洁（10分）：无多余文字/标签，排版干净\n"
            f"5. 内容趣味（15分）：是否让人想看完，有没有信息增量\n"
            f"6. 技术质量（5分）：时长合理、分辨率、音画同步\n\n"
            f"请严格按以下JSON格式返回（不要添加markdown或其他内容）：\n"
            f'{{"score": 85, "grade": "B", '
            f'"details": {{"解说质量": 20, "配乐质量": 15, "配音效果": 12, "页面简洁": 8, "内容趣味": 13, "技术质量": 5}}, '
            f'"suggestions": ["建议1", "建议2"]}}'
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
