"""VoiceActor Agent - 配音师：TTS语音合成"""
import asyncio
import os
import re
import shutil
import subprocess
import time
import wave
import edge_tts

# Edge TTS 需要通过代理访问 speech.platform.bing.com
# 自动从系统代理设置中读取，设置到环境变量供 aiohttp 使用
if not os.environ.get("HTTPS_PROXY") and not os.environ.get("https_proxy"):
    try:
        import urllib.request as _ur
        _sys_proxies = _ur.getproxies()
        if "https" in _sys_proxies:
            os.environ["HTTPS_PROXY"] = _sys_proxies["https"]
            os.environ["https_proxy"] = _sys_proxies["https"]
    except Exception:
        pass


class VoiceActor:
    """使用 edge-tts 将文本转为语音"""

    def __init__(self, output_dir, voice="zh-CN-YunxiNeural"):
        self.output_dir = output_dir
        self.voice = voice
        self._resolved_voice = None
        self._loop = None
        self._proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or None
        os.makedirs(output_dir, exist_ok=True)

    def _get_voice_candidates(self):
        """返回可选 voice，固定使用 YunxiNeural（最接近参考视频风格）。"""
        return [self.voice]

    def _get_loop(self):
        """获取或创建一个可复用的事件循环，避免反复 asyncio.run() 导致冲突"""
        if self._loop is None or self._loop.is_closed():
            self._loop = asyncio.new_event_loop()
            # Windows 上 ProactorEventLoop 关闭连接时会触发无害的 ConnectionResetError
            self._loop.set_exception_handler(self._suppress_connection_reset)
        return self._loop

    @staticmethod
    def _suppress_connection_reset(loop, context):
        exc = context.get("exception")
        if isinstance(exc, (ConnectionResetError, OSError)):
            return  # 静默忽略 TTS 连接关闭时的无害错误
        loop.default_exception_handler(context)

    def _run_async(self, coro):
        """在事件循环中执行协程"""
        loop = self._get_loop()
        return loop.run_until_complete(coro)

    @staticmethod
    def _sanitize_for_tts(text):
        """清理文本，移除 edge-tts 不支持的字符和格式。同时压短句间停顿：
        - 句末 `。` 替换为 `，` 让 TTS 用短停顿
        - 连续逗号去重避免叠加停顿
        """
        # 移除 emoji（补充平面字符）
        text = re.sub(r'[\U00010000-\U0010ffff]', '', text)
        # 替换可能导致 edge-tts 失败的标点
        text = text.replace('：', '，').replace('"', '').replace('"', '')
        text = text.replace('"', '').replace("'", '').replace("'", '').replace("'", '')
        text = text.replace('【', '').replace('】', '').replace('《', '').replace('》', '')
        text = text.replace('！', '。').replace('？', '。')
        # 压短句间停顿：句号当短停顿处理
        text = text.replace('。', '，')
        # 移除 @ 和 # 标签
        text = re.sub(r'[@#]\S+', '', text)
        # 合并连续逗号/空白
        text = re.sub(r'[，,]\s*[，,]+', '，', text)
        text = re.sub(r'\s+', ' ', text).strip()
        # 去掉首尾多余逗号
        text = text.strip('，,')
        return text

    @staticmethod
    def _trim_silences(path, max_silence_ms=200, silence_thresh_db=-35):
        """用 ffmpeg silenceremove 把所有 >max_silence_ms 的静音段压到 max_silence_ms。"""
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return
        if not os.path.exists(path) or os.path.getsize(path) < 1000:
            return
        tmp_path = path + ".trim.mp3"
        stop_dur = max_silence_ms / 1000.0
        # silenceremove: 检测 stop_duration 以上的静音段，压到 stop_duration
        # stop_periods=-1 处理所有段；stop_silence 让保留段长度=stop_duration
        af = (
            f"silenceremove=stop_periods=-1:stop_duration={stop_dur}:"
            f"stop_threshold={silence_thresh_db}dB:stop_silence={stop_dur}"
        )
        try:
            result = subprocess.run(
                [ffmpeg, "-y", "-i", path, "-af", af, "-c:a", "libmp3lame", "-q:a", "4", tmp_path],
                capture_output=True, timeout=30,
            )
            if result.returncode == 0 and os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 1000:
                os.replace(tmp_path, path)
            else:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
        except Exception:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

    async def _synthesize(self, text, output_path, rate="+0%", volume="+0%", pitch="+0Hz"):
        # 首次成功后锁定 voice，保证整条视频音色一致
        voices = [self._resolved_voice] if self._resolved_voice else self._get_voice_candidates()
        last_error = None
        for voice in voices:
            try:
                communicate = edge_tts.Communicate(
                    text, voice, rate=rate, volume=volume, pitch=pitch,
                    proxy=self._proxy,
                )
                await communicate.save(output_path)
                self._resolved_voice = voice
                return voice
            except Exception as exc:
                last_error = exc
                if self._resolved_voice:
                    break
                continue
        # 全部失败，抛出异常
        raise RuntimeError(f"所有语音均合成失败: {last_error}")

    def _validate_mp3(self, path):
        """验证 MP3 文件是否有效"""
        if not os.path.exists(path):
            return False
        size = os.path.getsize(path)
        if size < 1000:
            return False
        with open(path, "rb") as f:
            header = f.read(3)
            # MP3 files start with ID3 tag or MPEG sync bytes (0xFF 0xFB/0xF3/0xF2)
            if header[:2] == b"ID" or (header[0] == 0xFF and header[1] >= 0xE0):
                return True
        return False

    def _generate_silent_wav(self, output_path, duration_sec=2):
        """生成兜底静音 wav，避免无效 mp3 导致后续合成失败"""
        n_channels = 1
        sample_width = 2
        frame_rate = 16000
        n_frames = int(frame_rate * duration_sec)

        with wave.open(output_path, "wb") as wav_file:
            wav_file.setnchannels(n_channels)
            wav_file.setsampwidth(sample_width)
            wav_file.setframerate(frame_rate)
            silence_frame = (0).to_bytes(2, byteorder="little", signed=True)
            wav_file.writeframes(silence_frame * n_frames)

    def synthesize_segment(self, text, filename, version=1, retries=3):
        rate = "+0%"
        volume = "+0%"
        pitch = "+0Hz"

        if version >= 3:
            rate = "+10%"
            volume = "+5%"
            pitch = "-1Hz"  # 略低沉，更自然
        if version >= 5:
            rate = "+15%"
            pitch = "-1Hz"

        output_path = os.path.join(self.output_dir, filename)

        # 清理文本：移除可能导致问题的字符
        clean_text = self._sanitize_for_tts(text)
        if not clean_text:
            clean_text = "无内容。"

        # 删除旧文件防止冲突
        if os.path.exists(output_path):
            os.remove(output_path)

        for attempt in range(retries):
            try:
                self._run_async(self._synthesize(
                    clean_text, output_path, rate=rate, volume=volume, pitch=pitch
                ))
                if self._validate_mp3(output_path):
                    self._trim_silences(output_path)
                    return output_path
                else:
                    print(f"  [VoiceActor] 音频验证失败，重试中...")
                    if os.path.exists(output_path):
                        os.remove(output_path)
            except Exception as e:
                print(f"  [VoiceActor] 尝试 {attempt+1}/{retries} 失败: {e}")
                if os.path.exists(output_path):
                    os.remove(output_path)
                # 如果非默认参数失败，fallback 到默认参数重试
                if rate != "+0%" or volume != "+0%":
                    rate, volume = "+0%", "+0%"
            if attempt < retries - 1:
                time.sleep(3)  # 增加重试间隔避免速率限制

        # 最终失败时使用备用文本
        print(f"  [VoiceActor] 使用备用语音...")
        try:
            self._run_async(self._synthesize(
                "请关注详细内容。", output_path, rate="+0%", volume="+0%"
            ))
            if self._validate_mp3(output_path):
                return output_path
        except Exception:
            pass

        # 最终兜底：输出有效 wav，避免 ffmpeg 读取异常
        wav_path = output_path.replace(".mp3", ".wav")
        self._generate_silent_wav(wav_path, duration_sec=2)
        return wav_path

    def run(self, scripts, version=1):
        audio_paths = []
        for i, script in enumerate(scripts):
            seg_type = script["type"]
            filename = f"{seg_type}_{i}.mp3"
            text = script["text"]

            path = self.synthesize_segment(text, filename, version)
            audio_paths.append({
                "type": seg_type,
                "path": path,
                "script": script,
            })
            print(f"  [VoiceActor] 生成音频: {filename}")
        print(f"[VoiceActor] 共生成 {len(audio_paths)} 段音频 (v{version})")
        return audio_paths
