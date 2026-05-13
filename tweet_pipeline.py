"""推特短视频生成主流程 - AI 增强版 v2
集成: 全网背景检索 + AI解说词 + 真实歌曲配乐 + TTS配音 + 迭代审阅
"""
import os

from utils.console_encoding import ensure_utf8_console
ensure_utf8_console()


def generate_tweet_video(
    images,
    translations,
    authors=None,
    original_texts=None,
    duration=12.0,
    max_rounds=3,
    target_grade="A",
    tweet_ids=None,
):
    from agents.pipeline_core import run_pipeline, PipelineResult
    from agents.ai_assistant import AIAssistant
    from agents.tweet_video_agent import TweetVideoAgent
    ai = AIAssistant()
    agent = TweetVideoAgent()
    result: PipelineResult = run_pipeline(
        images, translations,
        author_list=authors,
        orig_list=original_texts,
        duration=duration,
        max_rounds=max_rounds,
        tweet_id=(tweet_ids[0] if tweet_ids else ""),
        ai=ai,
        agent=agent,
    )
    return result.video_path, result.full_review


if __name__ == "__main__":
    # 测试用例
    _img = os.environ.get("TWEET_TEST_IMAGE", "")
    if not _img:
        raise SystemExit("Set TWEET_TEST_IMAGE env var to a cover image path before running.")
    video, review = generate_tweet_video(
        images=[_img],
        translations=["继续做你自己 @KingJames！库里转发了CNN关于勒布朗·詹姆斯的报道，詹姆斯表示总统在利用体育运动和运动员来分裂国家，这是他无法认同的事情。"],
        authors=["Stephen Curry @StephenCurry30"],
        original_texts=["Keep doing you @KingJames! 💪🏽"],
        duration=12.0,
        max_rounds=3,
        target_grade="A",
    )
