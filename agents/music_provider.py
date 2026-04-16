"""MusicProvider - 生成氛围背景音乐（纯 Python 合成，无需外部素材）"""
import os
import struct
import math
import random


class MusicProvider:
    """用正弦波合成简单的氛围背景音乐（Lo-fi / Chill 风格）"""

    SAMPLE_RATE = 44100

    # 和弦进行（C大调 lo-fi 风格）
    CHORD_PROGRESSIONS = {
        "chill": [
            [261.63, 329.63, 392.00],   # Cmaj
            [220.00, 277.18, 329.63],   # Am
            [174.61, 220.00, 261.63],   # Fmaj
            [196.00, 246.94, 293.66],   # Gmaj
        ],
        "hype": [
            [329.63, 415.30, 493.88],   # Em (高八度)
            [293.66, 369.99, 440.00],   # Dm
            [261.63, 329.63, 392.00],   # Cmaj
            [349.23, 440.00, 523.25],   # Fmaj (高)
        ],
        "emotional": [
            [220.00, 277.18, 329.63],   # Am
            [174.61, 220.00, 261.63],   # Fmaj
            [261.63, 329.63, 392.00],   # Cmaj
            [196.00, 246.94, 293.66],   # Gmaj
        ],
    }

    def __init__(self, output_dir="d:/vedio/output/audio"):
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

    def _sine_wave(self, freq, duration, volume=0.15):
        """生成单个正弦波"""
        samples = []
        n = int(self.SAMPLE_RATE * duration)
        for i in range(n):
            t = i / self.SAMPLE_RATE
            # 带淡入淡出的正弦波
            envelope = 1.0
            fade = int(self.SAMPLE_RATE * 0.05)
            if i < fade:
                envelope = i / fade
            elif i > n - fade:
                envelope = (n - i) / fade
            val = volume * envelope * math.sin(2 * math.pi * freq * t)
            samples.append(val)
        return samples

    def _mix(self, *tracks):
        """混合多个音轨"""
        length = max(len(t) for t in tracks)
        mixed = [0.0] * length
        for track in tracks:
            for i, val in enumerate(track):
                mixed[i] += val
        # 归一化
        peak = max(abs(v) for v in mixed) if mixed else 1.0
        if peak > 0.95:
            mixed = [v * 0.9 / peak for v in mixed]
        return mixed

    def _add_bass(self, chord_freqs, duration, volume=0.12):
        """添加低音线"""
        bass_freq = chord_freqs[0] / 2  # 低一个八度
        return self._sine_wave(bass_freq, duration, volume)

    def _write_wav(self, samples, filepath):
        """写 WAV 文件（16bit 立体声）"""
        n = len(samples)
        with open(filepath, "wb") as f:
            # WAV header
            data_size = n * 4  # 16bit * 2 channels
            f.write(b"RIFF")
            f.write(struct.pack("<I", 36 + data_size))
            f.write(b"WAVE")
            f.write(b"fmt ")
            f.write(struct.pack("<I", 16))       # chunk size
            f.write(struct.pack("<H", 1))        # PCM
            f.write(struct.pack("<H", 2))        # stereo
            f.write(struct.pack("<I", self.SAMPLE_RATE))
            f.write(struct.pack("<I", self.SAMPLE_RATE * 4))  # byte rate
            f.write(struct.pack("<H", 4))        # block align
            f.write(struct.pack("<H", 16))       # bits per sample
            f.write(b"data")
            f.write(struct.pack("<I", data_size))
            for s in samples:
                val = max(-1.0, min(1.0, s))
                sample_int = int(val * 32767)
                # 写两个通道（立体声）
                f.write(struct.pack("<h", sample_int))
                f.write(struct.pack("<h", sample_int))
        return filepath

    def generate(self, duration=12.0, mood="chill", output_name="bgm.wav"):
        """
        生成氛围背景音乐
        
        Args:
            duration: 音乐时长（秒）
            mood: 氛围类型 ("chill", "hype", "emotional")
            output_name: 输出文件名
        
        Returns:
            str: 生成的 WAV 文件路径
        """
        chords = self.CHORD_PROGRESSIONS.get(mood, self.CHORD_PROGRESSIONS["chill"])
        chord_duration = duration / len(chords)
        
        all_samples = []
        for chord_freqs in chords:
            # 合成和弦（多个正弦波叠加）
            chord_tracks = []
            for freq in chord_freqs:
                chord_tracks.append(self._sine_wave(freq, chord_duration, volume=0.10))
            # 添加低音
            chord_tracks.append(self._add_bass(chord_freqs, chord_duration, volume=0.08))
            
            # 添加轻微的高频泛音
            for freq in chord_freqs[:2]:
                chord_tracks.append(self._sine_wave(freq * 2, chord_duration, volume=0.03))
            
            mixed = self._mix(*chord_tracks)
            all_samples.extend(mixed)

        # 全局淡入淡出
        fade_samples = int(self.SAMPLE_RATE * 1.0)  # 1秒淡入淡出
        for i in range(min(fade_samples, len(all_samples))):
            all_samples[i] *= i / fade_samples
        for i in range(min(fade_samples, len(all_samples))):
            idx = len(all_samples) - 1 - i
            all_samples[idx] *= i / fade_samples

        filepath = os.path.join(self.output_dir, output_name)
        self._write_wav(all_samples, filepath)
        return filepath
