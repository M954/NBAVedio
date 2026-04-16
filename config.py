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

# 默认数据源
DEFAULT_JSON_PATH = r"C:\Users\xuqin\Documents\testssh\output\demo_results.json"

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
# Windows 系统中文字体路径
FONT_PATH_BOLD = r"C:\Windows\Fonts\msyhbd.ttc"   # 微软雅黑粗体
FONT_PATH_REGULAR = r"C:\Windows\Fonts\msyh.ttc"  # 微软雅黑常规
FONT_PATH_LIGHT = r"C:\Windows\Fonts\msyhl.ttc"   # 微软雅黑细体

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
MIN_AUDIO_QUALITY = 0.7     # 最低音频质量评分
MIN_VISUAL_QUALITY = 0.7    # 最低画面质量评分
