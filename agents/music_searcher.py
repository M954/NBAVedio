"""MusicSearcher - 根据歌曲名搜索、下载并截取高潮段作为视频配乐"""
import os
import subprocess
import json
import uuid


# 动态获取 ffmpeg 路径
try:
    import imageio_ffmpeg
    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except ImportError:
    FFMPEG = "ffmpeg"


class MusicSearcher:
    """从 YouTube 搜索歌曲并截取高潮段"""

    def __init__(self, cache_dir=None):
        if cache_dir is None:
            cache_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output", "music_cache")
        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)

    def _safe_name(self, text):
        import re
        return re.sub(r"[^a-zA-Z0-9_\-]", "_", text)[:60]

    def search_and_download(self, song_query, duration=30):
        """
        搜索歌曲并下载音频

        Args:
            song_query: 歌曲搜索词，如 "Lose Yourself Eminem"
            duration: 需要的音频时长（秒）

        Returns:
            str: 下载的音频文件路径，失败返回 None
        """
        safe = self._safe_name(song_query)
        cached = os.path.join(self.cache_dir, f"{safe}.wav")
        if os.path.exists(cached) and os.path.getsize(cached) > 10000:
            return cached

        # 用 yt-dlp 搜索并下载
        temp_file = os.path.join(self.cache_dir, f"temp_{uuid.uuid4().hex[:8]}")
        try:
            import yt_dlp
            ydl_opts = {
                "format": "bestaudio/best",
                "outtmpl": temp_file + ".%(ext)s",
                "noplaylist": True,
                "quiet": True,
                "no_warnings": True,
                "extract_flat": False,
                "socket_timeout": 20,
            }
            
            # 尝试多个搜索源（SoundCloud不需要登录）
            search_queries = [
                f"scsearch1:{song_query}",   # SoundCloud
                f"ytsearch1:{song_query}",   # YouTube (fallback)
            ]
            
            info = None
            for sq in search_queries:
                try:
                    opts = dict(ydl_opts)
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        info = ydl.extract_info(sq, download=True)
                    if info:
                        break
                except Exception:
                    continue
            
            if not info:
                return None

            # 找到下载的文件
            downloaded = None
            for ext in [".webm", ".m4a", ".mp3", ".opus", ".ogg", ".wav"]:
                candidate = temp_file + ext
                if os.path.exists(candidate):
                    downloaded = candidate
                    break

            if not downloaded:
                # 搜索目录中匹配的文件
                for f in os.listdir(self.cache_dir):
                    if f.startswith(os.path.basename(temp_file)):
                        downloaded = os.path.join(self.cache_dir, f)
                        break

            if not downloaded:
                return None

            # 获取音频总时长
            total_duration = self._get_duration(downloaded)
            if total_duration <= 0:
                total_duration = 180  # 默认3分钟

            # 截取高潮段（通常在歌曲的 40%-70% 位置）
            chorus_start = max(0, total_duration * 0.45 - duration / 2)
            # 确保不超出结尾
            if chorus_start + duration > total_duration:
                chorus_start = max(0, total_duration - duration)

            # ffmpeg 截取并转为 wav
            cmd = [
                FFMPEG,
                "-y",
                "-ss", str(round(chorus_start, 1)),
                "-i", downloaded,
                "-t", str(duration),
                "-ar", "44100",
                "-ac", "2",
                "-acodec", "pcm_s16le",
                cached,
            ]
            subprocess.run(cmd, capture_output=True, timeout=30)

            # 清理临时文件
            if os.path.exists(downloaded) and downloaded != cached:
                try:
                    os.remove(downloaded)
                except Exception:
                    pass

            if os.path.exists(cached) and os.path.getsize(cached) > 10000:
                return cached

        except Exception as e:
            print(f"  [MusicSearcher] 搜索失败: {e}")

        return None

    def _get_duration(self, filepath):
        """用 ffprobe 获取音频时长"""
        ffprobe = FFMPEG.replace("ffmpeg-", "ffprobe-")
        if not os.path.exists(ffprobe):
            # 回退：用 ffmpeg 获取
            try:
                r = subprocess.run(
                    [FFMPEG, "-i", filepath],
                    capture_output=True, text=True, timeout=10,
                )
                # 从 stderr 中提取 Duration
                import re
                m = re.search(r"Duration:\s*(\d+):(\d+):(\d+)\.(\d+)", r.stderr)
                if m:
                    h, mn, s, ms = int(m[1]), int(m[2]), int(m[3]), int(m[4])
                    return h * 3600 + mn * 60 + s + ms / 100
            except Exception:
                pass
            return 0

        try:
            r = subprocess.run(
                [ffprobe, "-v", "quiet", "-show_entries",
                 "format=duration", "-of",
                 "default=noprint_wrappers=1:nokey=1", filepath],
                capture_output=True, text=True, timeout=10,
            )
            return float(r.stdout.strip())
        except Exception:
            return 0
