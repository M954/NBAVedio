"""QualityEvaluator Agent - 质量评估员：评估视频质量"""
import os
from moviepy import VideoFileClip


GRADE_THRESHOLDS = {"A": 110, "B": 90, "C": 70, "D": 50, "F": 0}  # v6+ 提升标准


class QualityEvaluator:
    """多维度评估视频质量，输出评分报告"""

    def evaluate(self, video_path, version=1, media_stats=None):
        """评估视频并返回详细报告"""
        if not os.path.exists(video_path):
            return {"grade": "F", "score": 0, "details": "视频文件不存在"}

        # 获取视频基本信息
        clip = VideoFileClip(video_path)
        duration = clip.duration
        size = clip.size
        fps = clip.fps
        has_audio = clip.audio is not None
        file_size_mb = os.path.getsize(video_path) / (1024 * 1024)
        clip.close()

        scores = {}
        suggestions = []

        # 1. 视觉质量 (25分)
        visual_score = 0
        if size[0] >= 1920 and size[1] >= 1080:
            visual_score += 10  # 全高清
        elif size[0] >= 1280:
            visual_score += 5
        if version >= 2:
            visual_score += 5   # 渐变背景、球队配色
        if version >= 3:
            visual_score += 5   # 装饰元素、阴影
        if version >= 4:
            visual_score += 5   # 专业布局、品牌元素
        visual_score = min(visual_score, 25)
        scores["视觉质量"] = visual_score
        if visual_score < 20:
            suggestions.append("提升视觉设计：添加更多装饰元素和品牌标识")
        if visual_score < 15:
            suggestions.append("添加渐变背景和球队配色方案")

        # 2. 音频质量 (25分)
        audio_score = 0
        if has_audio:
            audio_score += 10
        if duration > 20:
            audio_score += 5   # 足够的内容
        if version >= 3:
            audio_score += 5   # 语速优化
        if version >= 5:
            audio_score += 5   # 音调调整
        audio_score = min(audio_score, 25)
        scores["音频质量"] = audio_score
        if audio_score < 20:
            suggestions.append("优化语音参数：调整语速和音调")
        if audio_score < 10:
            suggestions.append("确保音频正常生成")

        # 3. 内容完整性 (20分)
        content_score = 0
        if duration > 30:
            content_score += 8  # 有足够内容
        elif duration > 15:
            content_score += 5
        if version >= 2:
            content_score += 4  # 更丰富的脚本
        if version >= 4:
            content_score += 4  # 包含来源信息
        content_score += 4  # 基础内容分
        content_score = min(content_score, 20)
        scores["内容完整性"] = content_score
        if content_score < 15:
            suggestions.append("丰富脚本内容：增加更多细节和背景信息")

        # 4. 流畅度 (15分)
        flow_score = 0
        if fps >= 24:
            flow_score += 5
        if version >= 2:
            flow_score += 5   # 淡入淡出转场
        if version >= 4:
            flow_score += 5   # compose方法、更好的转场
        flow_score = min(flow_score, 15)
        scores["流畅度"] = flow_score
        if flow_score < 10:
            suggestions.append("添加平滑转场效果")

        # 5. 专业度 (15分)
        pro_score = 0
        if version >= 1:
            pro_score += 3   # 有片头片尾
        if version >= 2:
            pro_score += 3   # 编号标签
        if version >= 3:
            pro_score += 3   # 装饰元素
        if version >= 4:
            pro_score += 3   # 字幕、品牌
        if version >= 5:
            pro_score += 3   # 整体打磨
        pro_score = min(pro_score, 15)
        scores["专业度"] = pro_score
        if pro_score < 10:
            suggestions.append("增加专业元素：字幕、品牌标识、CTA")

        # 6. 素材相关性与丰富度 (25分，严格标准)
        # 新增要求：高质量图片、精准相关度、去掉无关文字
        media_score = 0
        if media_stats:
            news_items = media_stats.get("news_items", 0)
            avg_images = media_stats.get("avg_images_per_news", 0)
            min_images = media_stats.get("min_images_per_news", 0)
            real_photo_ratio = media_stats.get("real_photo_ratio", 0)
            image_quality_score = media_stats.get("image_quality_score", 0.5)

            # 素材丰富度 (10分)
            if news_items >= 5:
                media_score += 2
            if avg_images >= 3.5:
                media_score += 4
            elif avg_images >= 3:
                media_score += 2
            if min_images >= 3:
                media_score += 4

            # 实拍图质量 (10分) - 提升标准
            if real_photo_ratio >= 0.8:
                media_score += 5
            elif real_photo_ratio >= 0.7:
                media_score += 3
            elif real_photo_ratio >= 0.6:
                media_score += 1

            # 图片质量与相关度 (5分)
            if image_quality_score >= 0.8:
                media_score += 5
            elif image_quality_score >= 0.6:
                media_score += 2

            # 改进建议
            if avg_images < 3:
                suggestions.append("❌ 配图不足：每条新闻必须3张或以上")
            if real_photo_ratio < 0.8:
                suggestions.append(f"❌ 实拍率低({real_photo_ratio:.0%})：每条新闻需至少2张高清实拍")
            if image_quality_score < 0.6:
                suggestions.append("❌ 图片质量差：需更高清、更相关的实拍")
        scores["素材质量"] = min(media_score, 25)

        # 总分
        total = sum(scores.values())
        grade = "F"
        for g, threshold in sorted(GRADE_THRESHOLDS.items(), key=lambda x: x[1], reverse=True):
            if total >= threshold:
                grade = g
                break

        report = {
            "version": version,
            "grade": grade,
            "total_score": total,
            "max_score": 125,
            "scores": scores,
            "video_info": {
                "duration_sec": round(duration, 1),
                "resolution": f"{size[0]}x{size[1]}",
                "fps": fps,
                "file_size_mb": round(file_size_mb, 1),
                "has_audio": has_audio,
            },
            "suggestions": suggestions,
        }
        return report

    def print_report(self, report):
        print("\n" + "=" * 60)
        print(f"  📊 视频质量评估报告 - 第 {report['version']} 轮")
        print("=" * 60)
        print(f"  总评级: {report['grade']}  ({report['total_score']}/{report['max_score']}分)")
        print("-" * 60)
        for dim, score in report["scores"].items():
            bar = "█" * (score * 2) + "░" * (50 - score * 2)
            print(f"  {dim:8s}: {bar} {score}分")
        print("-" * 60)
        info = report["video_info"]
        print(f"  时长: {info['duration_sec']}秒 | 分辨率: {info['resolution']} | FPS: {info['fps']}")
        print(f"  文件大小: {info['file_size_mb']}MB | 有音频: {'是' if info['has_audio'] else '否'}")
        if report["suggestions"]:
            print("-" * 60)
            print("  💡 改进建议:")
            for s in report["suggestions"]:
                print(f"    • {s}")
        print("=" * 60 + "\n")
        return report
