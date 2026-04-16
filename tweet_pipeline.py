"""推特短视频生成主流程 - AI 增强版 v2
集成: AI解说词 + 真实歌曲配乐 + TTS配音 + 迭代审阅
"""
import os
from agents.ai_assistant import AIAssistant
from agents.tweet_video_agent import TweetVideoAgent
from moviepy import VideoFileClip


def generate_tweet_video(
    images,
    translations,
    authors=None,
    original_texts=None,
    duration=12.0,
    max_rounds=3,
    target_grade="A",
):
    ai = AIAssistant()
    agent = TweetVideoAgent()

    print("=" * 50)
    print("  🐦 推特短视频 AI 生成 v2")
    print("=" * 50)

    orig0 = original_texts[0] if original_texts else ""
    author0 = authors[0] if authors else ""

    # --- Step 1: AI 优化翻译（字幕用） ---
    print("\n>>> [1/5] AI 优化翻译...")
    polished = []
    for i, trans in enumerate(translations):
        orig = original_texts[i] if original_texts and i < len(original_texts) else ""
        try:
            polished.append(ai.polish_translation(orig, trans))
        except Exception:
            polished.append(trans)
    print(f"  字幕: {polished[0]}")

    # --- Step 2: AI 生成解说词（配音用，有解说感） ---
    print("\n>>> [2/5] AI 生成解说词...")
    commentary = None
    try:
        commentary = ai.generate_commentary(orig0, polished[0], author0)
    except Exception:
        commentary = polished[0]
    print(f"  解说: {commentary}")

    # --- Step 3: Claude 推荐歌曲 ---
    print("\n>>> [3/5] Claude 推荐歌曲...")
    song_query = None
    try:
        song_query = ai.recommend_song(orig0, polished[0], author0)
    except Exception:
        pass
    mood = "chill"
    try:
        mood = ai.recommend_mood(orig0, polished[0])
    except Exception:
        pass
    print(f"  歌曲: {song_query}")
    print(f"  氛围: {mood}")

    # --- Step 4 & 5: 迭代生成 + 审阅 ---
    best_video = None
    best_review = {"score": 0, "grade": "F"}
    cur_commentary = commentary
    cur_song = song_query

    for rnd in range(1, max_rounds + 1):
        print(f"\n>>> [4/5] 第 {rnd} 轮生成...")
        name = f"tweet_v{rnd}.mp4"

        video_path = agent.generate(
            images=images,
            translations=polished,
            authors=authors,
            mood=mood,
            duration=duration,
            output_name=name,
            commentary=[cur_commentary] if cur_commentary else None,
            song_query=cur_song,
        )

        clip = VideoFileClip(video_path)
        fsz = os.path.getsize(video_path) / (1024 * 1024)
        info = {
            "commentary": cur_commentary or "",
            "translation": polished[0],
            "author": author0,
            "bgm_song": cur_song or "合成音乐",
            "mood": mood,
            "has_narration": cur_commentary is not None,
            "duration": round(clip.duration, 1),
            "resolution": f"{clip.size[0]}x{clip.size[1]}",
            "has_audio": clip.audio is not None,
            "file_size_mb": round(fsz, 2),
        }
        clip.close()
        print(f"  视频: {name} ({fsz:.2f}MB, {info['duration']}s)")

        # 审阅
        print(f"\n>>> [5/5] AI 审阅第 {rnd} 轮...")
        try:
            review = ai.review_video(info)
        except Exception as e:
            review = {"score": 70, "grade": "C", "suggestions": [str(e)]}

        score = review.get("score", 0)
        grade = review.get("grade", "F")
        suggestions = review.get("suggestions", [])

        print(f"\n{'─' * 50}")
        print(f"  📊 第 {rnd} 轮: {grade} ({score}/100)")
        for k, v in review.get("details", {}).items():
            print(f"    {k}: {v}")
        for s in suggestions:
            print(f"  💡 {s}")
        print(f"{'─' * 50}")

        if score > best_review["score"]:
            best_video = video_path
            best_review = review

        if score >= 90:
            print(f"\n  ✅ 达到 A 级！")
            break

        if rnd < max_rounds:
            print(f"\n  🔄 根据建议改进...")
            # 改进解说词
            try:
                improved = ai._call(
                    f"当前解说词: {cur_commentary}\n"
                    f"审阅建议: {'; '.join(suggestions)}\n"
                    f"原始推文: {orig0}\n作者: {author0}\n"
                    f"请根据建议重写解说词：\n"
                    f"1. 必须解读推文行为（转发/引用/回复/原创）\n"
                    f"2. 必须说明态度（支持/反对/调侃/感慨）\n"
                    f"3. 必须补充背景信息（球员关系、事件背景等）\n"
                    f"50-80字。只返回解说词。"
                )
                if improved and len(improved.strip()) > 10:
                    cur_commentary = improved.strip().strip('"').strip("'")
                    print(f"  新解说: {cur_commentary}")
            except Exception:
                pass

            # 改进歌曲选择
            for s in suggestions:
                if "配乐" in s or "歌曲" in s or "音乐" in s or "BGM" in s:
                    try:
                        new_song = ai.recommend_song(orig0, polished[0], author0)
                        if new_song and new_song != cur_song:
                            cur_song = new_song
                            print(f"  新歌曲: {cur_song}")
                    except Exception:
                        pass
                    break

    print(f"\n{'=' * 50}")
    print(f"  🎬 最终: {best_review['grade']} ({best_review['score']}/100)")
    print(f"  📁 {best_video}")
    print(f"{'=' * 50}\n")

    return best_video, best_review


if __name__ == "__main__":
    # 测试用例
    video, review = generate_tweet_video(
        images=[r"C:\Users\xuqin\Documents\testssh\output\covers\1025808660637745152.jpg"],
        translations=["继续做你自己 @KingJames！库里转发了CNN关于勒布朗·詹姆斯的报道，詹姆斯表示总统在利用体育运动和运动员来分裂国家，这是他无法认同的事情。"],
        authors=["Stephen Curry @StephenCurry30"],
        original_texts=["Keep doing you @KingJames! 💪🏽"],
        duration=12.0,
        max_rounds=3,
        target_grade="A",
    )
