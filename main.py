"""NBA 每日快报 - 视频自动生成系统
Agent Group 主编排器：协调各 Agent 完成 5 轮迭代
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agents.producer import Producer
from agents.script_writer import ScriptWriter
from agents.visual_designer import VisualDesigner
from agents.voice_actor import VoiceActor
from agents.video_editor import VideoEditor
from agents.quality_evaluator import QualityEvaluator

# === 配置 ===
NEWS_JSON = r"C:\Users\xuqin\Documents\testssh\output\demo_results.json"
BASE_OUTPUT = r"d:\vedio\output"
TARGET_GRADE = "A"
MAX_ROUNDS = 5


def run_pipeline(version):
    """运行一轮完整的视频生成管道"""
    print(f"\n{'#' * 60}")
    print(f"  🎬 第 {version} 轮迭代开始")
    print(f"{'#' * 60}\n")

    img_dir = os.path.join(BASE_OUTPUT, "images")
    aud_dir = os.path.join(BASE_OUTPUT, "audio")
    vid_dir = os.path.join(BASE_OUTPUT, "video")

    # 清理旧的临时文件
    for d in [img_dir, aud_dir]:
        if os.path.exists(d):
            for f in os.listdir(d):
                os.remove(os.path.join(d, f))

    # 1. Producer: 选择新闻并规划节目
    print(">>> [1/6] Producer 制片人 - 选择新闻...")
    producer = Producer(max_news=5)
    show_plan = producer.run(NEWS_JSON)

    # 2. ScriptWriter: 生成脚本
    print(">>> [2/6] ScriptWriter 编剧 - 生成脚本...")
    writer = ScriptWriter()
    scripts = writer.run(show_plan, version=version)

    # 3. VisualDesigner: 生成图片
    print(">>> [3/6] VisualDesigner 视觉设计师 - 生成配图...")
    designer = VisualDesigner(img_dir)
    image_data = designer.run(scripts, version=version)

    # 4. VoiceActor: 生成语音
    print(">>> [4/6] VoiceActor 配音师 - 合成语音...")
    voice = VoiceActor(aud_dir)
    audio_data = voice.run(scripts, version=version)

    # 5. VideoEditor: 合成视频
    print(">>> [5/6] VideoEditor 视频编辑 - 合成视频...")
    editor = VideoEditor(vid_dir)
    video_path = editor.run(image_data, audio_data, version=version)

    # 6. QualityEvaluator: 评估质量
    print(">>> [6/6] QualityEvaluator 质量评估员 - 评估视频...")
    evaluator = QualityEvaluator()

    # 统计素材质量：每条新闻图片数量、实拍占比、高质量评分
    news_entries = [x for x in image_data if x.get("type") == "news"]
    news_counts = [len(x.get("paths", [x.get("path")])) for x in news_entries]
    fallback_count = 0
    total_news_images = 0
    high_quality_count = 0  # 高质量（非备用）图片数
    
    for x in news_entries:
        paths = x.get("paths", [x.get("path")])
        total_news_images += len(paths)
        for p in paths:
            if p and "fallback" not in p:
                high_quality_count += 1
                # 检查文件大小（高质量图片通常 >200KB）
                if os.path.exists(p) and os.path.getsize(p) > 200000:
                    high_quality_count += 0.2  # 额外加分

            if p and "fallback" in p:
                fallback_count += 1

    real_photo_ratio = 0
    if total_news_images > 0:
        real_photo_ratio = (total_news_images - fallback_count) / total_news_images
    
    # 图片质量评分（0-1）：综合实拍率和文件质量
    image_quality_score = real_photo_ratio * 0.7 + min(high_quality_count / max(total_news_images, 1), 1.0) * 0.3

    media_stats = {
        "news_items": len(news_entries),
        "avg_images_per_news": round(sum(news_counts) / len(news_counts), 2) if news_counts else 0,
        "min_images_per_news": min(news_counts) if news_counts else 0,
        "real_photo_ratio": round(real_photo_ratio, 2),
        "image_quality_score": round(image_quality_score, 2),  # 新增：图片质量综合评分
    }

    report = evaluator.evaluate(video_path, version=version, media_stats=media_stats)
    evaluator.print_report(report)

    return video_path, report


def main():
    print("=" * 60)
    print("  🏀 NBA 每日快报 - 视频自动生成系统")
    print("  Agent Group: Producer, ScriptWriter, VisualDesigner,")
    print("               VoiceActor, VideoEditor, QualityEvaluator")
    print(f"  目标评级: {TARGET_GRADE} | 最大轮次: {MAX_ROUNDS}")
    print("=" * 60)

    final_path = None
    final_report = None

    for version in range(1, MAX_ROUNDS + 1):
        video_path, report = run_pipeline(version)
        final_path = video_path
        final_report = report

        if report["grade"] == TARGET_GRADE:
            print(f"\n✅ 已达到目标评级 {TARGET_GRADE}！在第 {version} 轮完成。")
            break
        else:
            print(f"⏳ 当前评级: {report['grade']}，目标: {TARGET_GRADE}，继续下一轮优化...\n")

    print("\n" + "=" * 60)
    print("  🎉 视频生成完成!")
    print(f"  最终视频: {final_path}")
    print(f"  最终评级: {final_report['grade']} ({final_report['total_score']}/{final_report['max_score']})")
    print("=" * 60)


if __name__ == "__main__":
    main()
