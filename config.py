"""
NBA篮球资讯视频生成器 - 全局配置
"""
import os

# === 路径配置 ===
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
ASSETS_DIR = os.path.join(PROJECT_ROOT, "assets")
FONTS_DIR = os.path.join(ASSETS_DIR, "fonts")
BACKGROUNDS_DIR = os.path.join(ASSETS_DIR, "backgrounds")
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "output")
TEMP_DIR = os.path.join(PROJECT_ROOT, "temp")

# 默认数据源（可用 NBAVEDIO_DEFAULT_JSON 覆盖）
DEFAULT_JSON_PATH = os.getenv(
    "NBAVEDIO_DEFAULT_JSON",
    os.path.join(OUTPUT_DIR, "demo_results.json"),
)

# === 视频配置 ===
VIDEO_WIDTH = 1080
VIDEO_HEIGHT = 1920  # 竖屏 9:16 (适合短视频平台)
VIDEO_FPS = 30
VIDEO_FORMAT = "mp4"
VIDEO_CODEC = "libx264"
AUDIO_CODEC = "aac"

# === 颜色方案 (NBA 风格) ===
BG_COLOR = (15, 23, 42)           # 深蓝黑背景
TITLE_COLOR = (255, 255, 255)     # 白色标题
SUBTITLE_COLOR = (148, 163, 184)  # 灰蓝色副标题
ACCENT_COLOR = (239, 68, 68)      # NBA红色强调
HIGHLIGHT_COLOR = (59, 130, 246)  # 蓝色高亮
TEXT_COLOR = (226, 232, 240)      # 浅灰正文
BORDER_COLOR = (239, 68, 68)      # 红色边框

# === 字体配置 ===
# 解析顺序：环境变量 → 项目自带 assets/fonts/ → 系统字体目录 → PIL 默认字体
# 任何环节缺失都不抛错，由消费方在 ImageFont.truetype 处兜底。
def _resolve_font(env_key: str, filename: str) -> str:
    override = os.getenv(env_key, "").strip()
    if override:
        return override
    bundled = os.path.join(FONTS_DIR, filename)
    if os.path.exists(bundled):
        return bundled
    candidates = [
        os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", filename),
        f"/usr/share/fonts/truetype/{filename}",
        f"/Library/Fonts/{filename}",
        f"/System/Library/Fonts/{filename}",
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return bundled  # 不存在也返回，消费方报错时路径有意义

FONT_PATH_BOLD = _resolve_font("FONT_PATH_BOLD", "msyhbd.ttc")
FONT_PATH_REGULAR = _resolve_font("FONT_PATH_REGULAR", "msyh.ttc")
FONT_PATH_LIGHT = _resolve_font("FONT_PATH_LIGHT", "msyhl.ttc")

TITLE_FONT_SIZE = 56
SUBTITLE_FONT_SIZE = 36
BODY_FONT_SIZE = 32
CAPTION_FONT_SIZE = 24

# === TTS 配置 ===
TTS_VOICE = "zh-CN-YunxiNeural"       # 中文男声（云希）
TTS_VOICE_FEMALE = "zh-CN-XiaoxiaoNeural"  # 中文女声（晓晓）
TTS_RATE = "+0%"                        # 语速调整
TTS_VOLUME = "+0%"                      # 音量调整
TTS_PITCH = "+0Hz"                      # 音调调整

# === Azure Speech TTS（首选后端，避开 edge-tts WebSocket 403）===
# 同时设置以下两个环境变量即可启用 Azure；未设置则自动回退到 edge-tts
AZURE_SPEECH_KEY = os.getenv("AZURE_SPEECH_KEY", "")
AZURE_SPEECH_REGION = os.getenv("AZURE_SPEECH_REGION", "")

# === 视频画面时间配置 (秒) ===
INTRO_DURATION = 2.0        # 开场动画时长
TITLE_DISPLAY_TIME = 1.5    # 标题额外显示时间（在语音之外）
TRANSITION_DURATION = 0.5   # 转场时长
OUTRO_DURATION = 2.0        # 结尾画面时长

# === 画面布局 (相对于视频尺寸的比例) ===
PADDING_X = 60              # 水平内边距
PADDING_TOP = 200           # 顶部内边距
TITLE_Y = 350               # 标题 Y 坐标
DIVIDER_Y = 480             # 分隔线 Y 坐标
BODY_Y = 530                # 正文起始 Y 坐标
SOURCE_Y = 1700             # 来源信息 Y 坐标

# === 质量评估阈值 ===
MIN_VIDEO_DURATION = 5.0    # 最短视频时长(秒)
MAX_VIDEO_DURATION = 120.0  # 最长视频时长(秒)
MAX_SOURCE_VIDEO_DURATION = 50.0  # 推文源视频上限(秒)，超过则触发 Gemini 智能剪辑
MIN_AUDIO_QUALITY = 0.7     # 最低音频质量评分
MIN_VISUAL_QUALITY = 0.7    # 最低画面质量评分
