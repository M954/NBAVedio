"""VisualDesigner Agent - 视觉设计师：生成配图"""
import json
import os
import re
import urllib.parse
import urllib.request
from PIL import Image, ImageDraw, ImageFont
import math


# NBA球队颜色
TEAM_COLORS = {
    "Lakers": {"primary": (85, 37, 130), "secondary": (253, 185, 39)},
    "Warriors": {"primary": (29, 66, 138), "secondary": (255, 199, 44)},
    "Heat": {"primary": (152, 0, 46), "secondary": (249, 160, 27)},
    "Celtics": {"primary": (0, 122, 51), "secondary": (255, 255, 255)},
    "Knicks": {"primary": (0, 107, 182), "secondary": (245, 132, 38)},
    "Cavaliers": {"primary": (134, 0, 56), "secondary": (253, 187, 48)},
    "Bucks": {"primary": (0, 71, 27), "secondary": (240, 235, 210)},
    "Kings": {"primary": (91, 43, 130), "secondary": (99, 113, 122)},
    "Suns": {"primary": (29, 17, 96), "secondary": (229, 95, 32)},
    "Magic": {"primary": (0, 125, 197), "secondary": (196, 206, 211)},
    "Bulls": {"primary": (206, 17, 65), "secondary": (6, 25, 34)},
    "Pacers": {"primary": (0, 45, 98), "secondary": (253, 187, 48)},
    "Raptors": {"primary": (206, 17, 65), "secondary": (6, 25, 34)},
    "Nets": {"primary": (0, 0, 0), "secondary": (255, 255, 255)},
    "Clippers": {"primary": (200, 16, 46), "secondary": (29, 66, 148)},
    "Hawks": {"primary": (200, 16, 46), "secondary": (196, 214, 0)},
    "Mavericks": {"primary": (0, 83, 188), "secondary": (0, 43, 92)},
    "default": {"primary": (30, 60, 114), "secondary": (255, 165, 0)},
}


def find_team_color(text):
    for team, colors in TEAM_COLORS.items():
        if team == "default":
            continue
        if team.lower() in text.lower():
            return colors
    return TEAM_COLORS["default"]


def get_font(size, bold=False):
    """尝试加载中文字体"""
    font_paths = [
        "C:/Windows/Fonts/msyh.ttc",       # 微软雅黑
        "C:/Windows/Fonts/msyhbd.ttc",      # 微软雅黑粗体
        "C:/Windows/Fonts/simhei.ttf",      # 黑体
        "C:/Windows/Fonts/simsun.ttc",      # 宋体
    ]
    if bold:
        font_paths.insert(0, "C:/Windows/Fonts/msyhbd.ttc")
    for fp in font_paths:
        if os.path.exists(fp):
            try:
                return ImageFont.truetype(fp, size)
            except Exception:
                continue
    return ImageFont.load_default()


def draw_gradient_bg(draw, width, height, color1, color2):
    """绘制垂直渐变背景"""
    for y in range(height):
        ratio = y / height
        r = int(color1[0] * (1 - ratio) + color2[0] * ratio)
        g = int(color1[1] * (1 - ratio) + color2[1] * ratio)
        b = int(color1[2] * (1 - ratio) + color2[2] * ratio)
        draw.line([(0, y), (width, y)], fill=(r, g, b))


def draw_rounded_rect(draw, xy, radius, fill):
    x0, y0, x1, y1 = xy
    draw.rectangle([x0 + radius, y0, x1 - radius, y1], fill=fill)
    draw.rectangle([x0, y0 + radius, x1, y1 - radius], fill=fill)
    draw.pieslice([x0, y0, x0 + 2*radius, y0 + 2*radius], 180, 270, fill=fill)
    draw.pieslice([x1 - 2*radius, y0, x1, y0 + 2*radius], 270, 360, fill=fill)
    draw.pieslice([x0, y1 - 2*radius, x0 + 2*radius, y1], 90, 180, fill=fill)
    draw.pieslice([x1 - 2*radius, y1 - 2*radius, x1, y1], 0, 90, fill=fill)


def wrap_text(text, font, max_width):
    """中文文本自动换行"""
    lines = []
    current_line = ""
    for char in text:
        test_line = current_line + char
        bbox = font.getbbox(test_line)
        w = bbox[2] - bbox[0]
        if w > max_width and current_line:
            lines.append(current_line)
            current_line = char
        else:
            current_line = test_line
    if current_line:
        lines.append(current_line)
    return lines


class VisualDesigner:
    """为每个片段生成配图"""

    def __init__(self, output_dir, width=1920, height=1080):
        self.output_dir = output_dir
        self.width = width
        self.height = height
        project_root = os.path.dirname(os.path.dirname(output_dir))
        self.cache_dir = os.path.join(project_root, "assets", "media_cache")
        os.makedirs(output_dir, exist_ok=True)
        os.makedirs(self.cache_dir, exist_ok=True)

    def _safe_name(self, text):
        return re.sub(r"[^a-zA-Z0-9_\-]", "_", text)[:80]

    def _extract_keywords(self, segment):
        """抽取新闻关键词，用于抓取实拍图（精准主题匹配 + 球队识别）"""
        title_en = segment.get("title_en", "")
        summary_en = segment.get("summary_en", "")
        text = f"{title_en} {summary_en}"

        keywords = []

        # 1. 抽取球员姓名（英文双词姓名）- 优先级最高
        name_pattern = r"\b([A-Z][a-z]+(?:\s[A-Z][a-zA-Z'.-]+)+)\b"
        for name in re.findall(name_pattern, text):
            parts = name.split()
            if len(parts) <= 3 and name not in keywords:
                # 跳过常见非人名短语
                skip = {"Trail Blazers", "All Star", "Los Angeles", "New York",
                        "San Antonio", "Golden State", "Oklahoma City"}
                if name not in skip:
                    keywords.append(name)

        # 2. 识别 NBA 球队并添加球队相关关键词
        teams = {
            "Lakers": "Los Angeles Lakers basketball", "Warriors": "Golden State Warriors basketball",
            "Heat": "Miami Heat basketball", "Celtics": "Boston Celtics basketball",
            "Knicks": "New York Knicks basketball", "Bulls": "Chicago Bulls basketball",
            "Cavaliers": "Cleveland Cavaliers basketball", "Bucks": "Milwaukee Bucks basketball",
            "Kings": "Sacramento Kings basketball", "Suns": "Phoenix Suns basketball",
            "Magic": "Orlando Magic basketball", "Clippers": "LA Clippers basketball",
            "Trail Blazers": "Portland Trail Blazers basketball",
            "Pacers": "Indiana Pacers basketball", "Raptors": "Toronto Raptors basketball",
            "Nets": "Brooklyn Nets basketball", "Hawks": "Atlanta Hawks basketball",
            "Mavericks": "Dallas Mavericks basketball", "Nuggets": "Denver Nuggets basketball",
            "76ers": "Philadelphia 76ers basketball", "Spurs": "San Antonio Spurs basketball",
            "Rockets": "Houston Rockets basketball", "Grizzlies": "Memphis Grizzlies basketball",
            "Pelicans": "New Orleans Pelicans basketball", "Timberwolves": "Minnesota Timberwolves basketball",
            "Thunder": "Oklahoma City Thunder basketball", "Jazz": "Utah Jazz basketball",
            "Pistons": "Detroit Pistons basketball", "Hornets": "Charlotte Hornets basketball",
            "Wizards": "Washington Wizards basketball",
        }
        for team_name, team_kw in teams.items():
            if team_name.lower() in text.lower() and team_kw not in keywords:
                keywords.append(team_kw)

        # 3. 主题映射
        topic_map = {
            "MVP": ["NBA MVP award", "NBA MVP trophy"],
            "Rookie": ["NBA Rookie of the Year award"],
            "trade": ["NBA trade deadline"],
            "waive": ["NBA player waived"],
            "playoff": ["NBA Playoffs basketball", "NBA playoff game"],
            "All-Star": ["NBA All-Star Game"],
            "triple-double": ["NBA triple-double"],
            "scoring": ["NBA scoring leader"],
            "draft": ["NBA Draft"],
            "foul": ["NBA basketball foul"],
            "tanking": ["NBA tanking team"],
            "seeding": ["NBA playoff seeding"],
            "fantasy": ["NBA fantasy basketball"],
        }
        for k, v_list in topic_map.items():
            if k.lower() in text.lower():
                for v in v_list:
                    if v not in keywords:
                        keywords.append(v)

        # 4. 保底关键词（高质量通用 NBA 横图）
        fallback = ["NBA basketball game action", "NBA arena court", "NBA basketball highlights"]
        for item in fallback:
            if item not in keywords:
                keywords.append(item)

        return keywords[:10]

    def _wikimedia_candidates(self, search_term, skip_urls=None):
        """从 Wikimedia Commons 搜索并返回候选横版图片列表"""
        if skip_urls is None:
            skip_urls = set()
        query = {
            "action": "query",
            "generator": "search",
            "gsrsearch": search_term,
            "gsrnamespace": "6",
            "gsrlimit": "30",
            "prop": "imageinfo",
            "iiprop": "url|mime|size",
            "iiurlwidth": "1920",
            "format": "json",
        }
        url = (
            "https://commons.wikimedia.org/w/api.php?"
            + urllib.parse.urlencode(query)
        )
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "NBA-Video-Agent/1.0 (Windows; Python urllib)"},
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception:
            return []

        candidates = []
        pages = data.get("query", {}).get("pages", {})
        for _, page in pages.items():
            infos = page.get("imageinfo", [])
            if not infos:
                continue
            info = infos[0]
            mime = info.get("mime", "")
            if mime not in ("image/jpeg", "image/png", "image/webp"):
                continue
            img_url = info.get("url")
            img_width = info.get("width", 0)
            img_height = info.get("height", 0)
            if not img_url or img_width < 400 or img_url in skip_urls:
                continue

            aspect = img_width / max(img_height, 1)
            if aspect < 0.9:
                continue
            landscape_bonus = 0
            if aspect >= 1.2:
                landscape_bonus = 5000
            if 1.5 <= aspect <= 2.0:
                landscape_bonus = 10000
            score = img_width * img_height + landscape_bonus
            candidates.append((score, img_url))

        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates

    def _search_wikimedia_image(self, keyword, skip_urls=None):
        """从 Wikimedia Commons 搜索单张最佳横版图片"""
        candidates = self._wikimedia_candidates(f"{keyword} basketball", skip_urls)
        if candidates:
            return candidates[0][1]
        return None

    def _fetch_wikipedia_thumbnail(self, keyword):
        """从 Wikipedia 获取高清原图（而非低清缩略图）"""
        title = keyword.strip().replace(" ", "_")
        url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(title)}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "NBA-Video-Agent/1.0 (Windows; Python urllib)"},
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception:
            return None

        # 优先用 originalimage（高清），其次 thumbnail
        orig = data.get("originalimage", {})
        thumb = data.get("thumbnail", {})

        # 获取原图信息
        orig_src = orig.get("source")
        orig_w = orig.get("width", 0)
        orig_h = orig.get("height", 0)
        thumb_src = thumb.get("source")
        thumb_w = thumb.get("width", 0)
        thumb_h = thumb.get("height", 0)

        # 选定源
        source = orig_src or thumb_src
        w = orig_w or thumb_w
        h = orig_h or thumb_h
        if not source:
            return None

        low = source.lower()
        if not any(ext in low for ext in (".jpg", ".jpeg", ".png")):
            return None

        # 严格检查宽高比：跳过竖图（如人物全身照竖版）
        if w > 0 and h > 0:
            aspect = w / h
            if aspect < 0.8:  # 明显竖图，不适合横版视频
                return None

        return source

    def _download_image(self, url, local_path):
        if os.path.exists(local_path):
            return local_path
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "NBA-Video-Agent/1.0 (Windows; Python urllib)"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp, open(local_path, "wb") as out:
                out.write(resp.read())
            return local_path
        except Exception:
            return None

    def _is_usable_image(self, img):
        """检查图片是否适合用于横版视频（质量审查）"""
        w, h = img.size
        if w < 300 or h < 200:
            return False  # 太小的图片
        aspect = w / h
        if aspect < 0.7:
            return False  # 严重竖图
        return True

    def _fit_cover(self, img):
        """智能裁剪为 1920x1080（改进：避免无意义裁剪，竖图用模糊背景填充）"""
        src_w, src_h = img.size
        target_ratio = self.width / self.height  # 16:9 ≈ 1.78
        src_ratio = src_w / src_h

        # 情况1：接近横图（宽高比 >= 1.0），直接裁剪中心区域
        if src_ratio >= 1.0:
            if src_ratio > target_ratio:
                # 比 16:9 更宽 → 裁两侧
                new_w = int(src_h * target_ratio)
                x0 = (src_w - new_w) // 2
                img = img.crop((x0, 0, x0 + new_w, src_h))
            else:
                # 比 16:9 更窄但还是横的 → 裁上下
                new_h = int(src_w / target_ratio)
                # 人物照片通常重点在上半部分，裁剪偏上
                y0 = max(0, int((src_h - new_h) * 0.35))
                img = img.crop((0, y0, src_w, y0 + new_h))
            return img.resize((self.width, self.height), Image.Resampling.LANCZOS)

        # 情况2：竖图（宽高比 < 1.0）→ 不强行裁剪，用模糊背景填充
        # 创建模糊背景
        bg = img.copy()
        bg = bg.resize((self.width, self.height), Image.Resampling.LANCZOS)
        try:
            from PIL import ImageFilter
            bg = bg.filter(ImageFilter.GaussianBlur(radius=30))
        except Exception:
            pass
        # 在上面叠加半透明暗层
        dark = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 120))
        bg = Image.alpha_composite(bg.convert("RGBA"), dark).convert("RGB")

        # 将原图按比例缩放放在中央
        scale = min(self.width * 0.7 / src_w, self.height * 0.9 / src_h)
        new_w = int(src_w * scale)
        new_h = int(src_h * scale)
        resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
        x_offset = (self.width - new_w) // 2
        y_offset = (self.height - new_h) // 2
        bg.paste(resized, (x_offset, y_offset))
        return bg

    def _create_photo_slide(self, segment, photo_path, shot_idx):
        """将实拍图加工成可播报的新闻画面（改进：去掉标签，专注高质量图片）"""
        try:
            raw = Image.open(photo_path).convert("RGB")
            img = self._fit_cover(raw)
        except Exception:
            return self.create_news_image(segment, version=5)

        draw = ImageDraw.Draw(img)
        overlay = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))
        ov = ImageDraw.Draw(overlay)

        # 仅在底部添加半透明遮罩，避免遮挡主要内容
        ov.rectangle((0, self.height - 200, self.width, self.height), fill=(0, 0, 0, 140))
        img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
        draw = ImageDraw.Draw(img)

        title_font = get_font(44, bold=True)
        sum_font = get_font(28)
        title_cn = segment.get("title_cn", "")
        summary_cn = segment.get("summary_cn", "")
        lines = wrap_text(title_cn, title_font, self.width - 120)

        # 不添加"实拍图1"标签 - 让图片更干净
        # 直接显示标题
        for i, line in enumerate(lines[:2]):
            y_pos = self.height - 180 + i * 48
            draw.text((60, y_pos), line, font=title_font, fill=(255, 255, 255))

        # 显示摘要
        sum_lines = wrap_text(summary_cn, sum_font, self.width - 120)
        if sum_lines:
            draw.text((60, self.height - 68), sum_lines[0], font=sum_font, fill=(200, 200, 200))

        out = os.path.join(self.output_dir, f"news_{segment['index']}_{shot_idx}.png")
        img.save(out, quality=95)
        return out

    def create_news_story_images(self, segment, version=1):
        """每条新闻生成至少3张高质量横版实拍图"""
        min_images = 3 if version >= 5 else 1
        keywords = self._extract_keywords(segment)
        downloaded = []
        used_urls = set()

        def _try_download(url, tag):
            """尝试下载并验证图片质量"""
            if not url or url in used_urls:
                return False
            used_urls.add(url)
            ext = ".jpg"
            if ".png" in url.lower():
                ext = ".png"
            elif ".webp" in url.lower():
                ext = ".webp"
            cache_name = self._safe_name(f"{segment['index']}_{tag}") + ext
            local_path = os.path.join(self.cache_dir, cache_name)
            saved = self._download_image(url, local_path)
            if not saved:
                return False
            try:
                check_img = Image.open(saved)
                if not self._is_usable_image(check_img):
                    return False
            except Exception:
                return False
            if os.path.getsize(saved) > 5000:
                downloaded.append(saved)
                return True
            return False

        # 第1轮：用提取的关键词搜索
        for kw in keywords:
            if len(downloaded) >= min_images:
                break
            url = self._fetch_wikipedia_thumbnail(kw)
            if url:
                _try_download(url, kw)
            if len(downloaded) >= min_images:
                break
            url = self._search_wikimedia_image(kw, skip_urls=used_urls)
            if url:
                _try_download(url, kw)

        # 第2轮：如果不够，用 Wikimedia 批量搜索获取多张不同的图
        if len(downloaded) < min_images:
            search_terms = [
                f"{segment.get('title_en', '')} NBA basketball",
                "NBA game basketball court",
                "NBA basketball arena crowd",
                "NBA dunk shot basketball",
                "NBA championship celebration",
            ]
            for term in search_terms:
                if len(downloaded) >= min_images:
                    break
                candidates = self._wikimedia_candidates(term, skip_urls=used_urls)
                for _, cand_url in candidates:
                    if len(downloaded) >= min_images:
                        break
                    tag = f"extra_{len(downloaded)}"
                    _try_download(cand_url, tag)

        slides = []
        for idx, photo in enumerate(downloaded[:min_images], start=1):
            slides.append(self._create_photo_slide(segment, photo, idx))

        while len(slides) < min_images:
            fallback = self.create_news_image(segment, version=max(version, 4))
            fallback_out = os.path.join(
                self.output_dir, f"news_{segment['index']}_fallback_{len(slides) + 1}.png"
            )
            Image.open(fallback).save(fallback_out, quality=95)
            slides.append(fallback_out)

        return slides

    def create_intro_image(self, title, version=1):
        img = Image.new("RGB", (self.width, self.height))
        draw = ImageDraw.Draw(img)

        if version >= 2:
            draw_gradient_bg(draw, self.width, self.height, (10, 10, 40), (30, 60, 114))
        else:
            draw_gradient_bg(draw, self.width, self.height, (20, 20, 60), (40, 40, 100))

        # 装饰元素
        if version >= 3:
            for i in range(5):
                x = 100 + i * 380
                draw.ellipse([x, 80, x + 60, 140], fill=(255, 165, 0, 80), outline=(255, 165, 0))
            # 底部装饰线
            draw.rectangle([0, self.height - 8, self.width, self.height], fill=(255, 165, 0))

        if version >= 4:
            # 顶部品牌条
            draw_rounded_rect(draw, (60, 30, self.width - 60, 100), 15, (255, 165, 0))
            brand_font = get_font(32, bold=True)
            draw.text((90, 45), "🏀 NBA DAILY REPORT", font=brand_font, fill=(10, 10, 40))

        # 主标题
        title_font = get_font(96 if version >= 2 else 72, bold=True)
        bbox = title_font.getbbox(title)
        tw = bbox[2] - bbox[0]
        x = (self.width - tw) // 2
        y = self.height // 2 - 80

        if version >= 3:
            # 文字阴影
            draw.text((x + 4, y + 4), title, font=title_font, fill=(0, 0, 0))
        draw.text((x, y), title, font=title_font, fill=(255, 255, 255))

        # 副标题
        if version >= 2:
            sub_font = get_font(36)
            sub_text = "最新篮球资讯 · AI 智能播报"
            bbox2 = sub_font.getbbox(sub_text)
            sw = bbox2[2] - bbox2[0]
            draw.text(((self.width - sw) // 2, y + 130), sub_text, font=sub_font, fill=(200, 200, 200))

        # 底部日期
        if version >= 4:
            date_font = get_font(28)
            draw.text((self.width - 350, self.height - 60), "2026年4月10日", font=date_font, fill=(180, 180, 180))

        path = os.path.join(self.output_dir, "intro.png")
        img.save(path, quality=95)
        return path

    def create_news_image(self, segment, version=1):
        img = Image.new("RGB", (self.width, self.height))
        draw = ImageDraw.Draw(img)
        idx = segment["index"]
        title_en = segment.get("title_en", "")
        colors = find_team_color(title_en)

        # 背景
        dark1 = tuple(max(0, c - 20) for c in colors["primary"])
        dark2 = tuple(min(255, c + 30) for c in colors["primary"])
        draw_gradient_bg(draw, self.width, self.height, dark1, dark2)

        if version >= 3:
            # 装饰图形
            accent = colors["secondary"]
            draw.rectangle([0, 0, self.width, 6], fill=accent)
            draw.rectangle([0, self.height - 6, self.width, self.height], fill=accent)
            # 侧边装饰
            draw.rectangle([0, 0, 8, self.height], fill=accent)

        # 新闻编号标签
        if version >= 2:
            tag_w, tag_h = 180, 70
            draw_rounded_rect(draw, (60, 60, 60 + tag_w, 60 + tag_h), 12, colors["secondary"])
            num_font = get_font(36, bold=True)
            draw.text((85, 72), f"NEWS #{idx}", font=num_font, fill=colors["primary"])
        else:
            num_font = get_font(48, bold=True)
            draw.text((80, 60), f"#{idx}", font=num_font, fill=colors["secondary"])

        # 标题
        title_cn = segment["title_cn"]
        title_font = get_font(56 if version >= 2 else 48, bold=True)
        margin = 80
        max_w = self.width - margin * 2
        lines = wrap_text(title_cn, title_font, max_w)

        y_start = 180 if version >= 2 else 160
        for i, line in enumerate(lines[:3]):
            y = y_start + i * 75
            if version >= 3:
                draw.text((margin + 3, y + 3), line, font=title_font, fill=(0, 0, 0))
            draw.text((margin, y), line, font=title_font, fill=(255, 255, 255))

        # 分隔线
        sep_y = y_start + len(lines[:3]) * 75 + 30
        if version >= 2:
            draw.rectangle([margin, sep_y, self.width - margin, sep_y + 3], fill=colors["secondary"])
        else:
            draw.line([(margin, sep_y), (self.width - margin, sep_y)], fill=(200, 200, 200), width=2)

        # 摘要
        summary_cn = segment["summary_cn"]
        sum_font = get_font(38 if version >= 2 else 32)
        sum_lines = wrap_text(summary_cn, sum_font, max_w - 40)
        sum_start = sep_y + 40

        if version >= 3:
            # 摘要背景框
            box_h = len(sum_lines[:5]) * 55 + 40
            draw_rounded_rect(draw,
                (margin - 10, sum_start - 15, self.width - margin + 10, sum_start + box_h),
                15, (0, 0, 0, 60)
            )
            # 半透明黑色背景
            overlay = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))
            ov_draw = ImageDraw.Draw(overlay)
            draw_rounded_rect(ov_draw,
                (margin - 10, sum_start - 15, self.width - margin + 10, sum_start + box_h),
                15, (0, 0, 0, 100)
            )
            img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
            draw = ImageDraw.Draw(img)

        for i, line in enumerate(sum_lines[:5]):
            draw.text((margin + 20, sum_start + i * 55), line, font=sum_font, fill=(230, 230, 230))

        # 来源
        if version >= 2:
            src_font = get_font(24)
            source = segment.get("source", "")
            draw.text((margin, self.height - 70), f"来源: {source}", font=src_font, fill=(160, 160, 160))

        # 篮球图标装饰 (v4+)
        if version >= 4:
            icon_font = get_font(60)
            draw.text((self.width - 140, self.height - 100), "🏀", font=icon_font, fill=(255, 255, 255))

        path = os.path.join(self.output_dir, f"news_{idx}.png")
        img.save(path, quality=95)
        return path

    def create_outro_image(self, version=1):
        img = Image.new("RGB", (self.width, self.height))
        draw = ImageDraw.Draw(img)

        if version >= 2:
            draw_gradient_bg(draw, self.width, self.height, (10, 10, 40), (30, 30, 80))
        else:
            draw_gradient_bg(draw, self.width, self.height, (30, 30, 60), (10, 10, 30))

        if version >= 3:
            draw.rectangle([0, self.height - 8, self.width, self.height], fill=(255, 165, 0))
            # 装饰圆
            for i in range(8):
                x = 200 + i * 200
                r = 20 + (i % 3) * 10
                draw.ellipse([x - r, 200 - r, x + r, 200 + r], outline=(255, 165, 0, 100), width=2)

        title_font = get_font(80 if version >= 2 else 64, bold=True)
        text = "感谢收看"
        bbox = title_font.getbbox(text)
        tw = bbox[2] - bbox[0]
        x = (self.width - tw) // 2
        y = self.height // 2 - 60

        if version >= 3:
            draw.text((x + 3, y + 3), text, font=title_font, fill=(0, 0, 0))
        draw.text((x, y), text, font=title_font, fill=(255, 255, 255))

        if version >= 2:
            sub_font = get_font(36)
            sub = "NBA 每日快报 · 下期再见"
            bbox2 = sub_font.getbbox(sub)
            sw = bbox2[2] - bbox2[0]
            draw.text(((self.width - sw) // 2, y + 110), sub, font=sub_font, fill=(180, 180, 180))

        if version >= 4:
            cta_font = get_font(32)
            cta = "点赞 · 关注 · 分享"
            bbox3 = cta_font.getbbox(cta)
            cw = bbox3[2] - bbox3[0]
            draw_rounded_rect(draw,
                ((self.width - cw) // 2 - 30, y + 180, (self.width + cw) // 2 + 30, y + 230),
                10, (255, 165, 0)
            )
            draw.text(((self.width - cw) // 2, y + 188), cta, font=cta_font, fill=(10, 10, 40))

        path = os.path.join(self.output_dir, "outro.png")
        img.save(path, quality=95)
        return path

    def run(self, scripts, version=1):
        image_paths = []
        for script in scripts:
            seg = script["segment"]
            if seg["type"] == "intro":
                path = self.create_intro_image(seg["title"], version)
                image_paths.append({"type": seg["type"], "path": path, "script": script})
            elif seg["type"] == "news":
                story_paths = self.create_news_story_images(seg, version)
                image_paths.append({
                    "type": seg["type"],
                    "paths": story_paths,
                    "path": story_paths[0],
                    "script": script,
                })
            elif seg["type"] == "outro":
                path = self.create_outro_image(version)
                image_paths.append({"type": seg["type"], "path": path, "script": script})
            else:
                continue
        print(f"[VisualDesigner] 生成了 {len(image_paths)} 张图片 (v{version})")
        return image_paths
