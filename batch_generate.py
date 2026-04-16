"""Batch generate 15 videos from selected tweets"""
import json
import os
import sys
from agents.ai_assistant import AIAssistant
from agents.tweet_video_agent import TweetVideoAgent

COVERS = r"C:\Users\xuqin\Documents\testssh\output\covers"
OUTPUT = "d:/vedio/output/tweet_videos"

with open(r"C:\Users\xuqin\Documents\testssh\output\tweets.json", "r", encoding="utf-8") as f:
    data = json.load(f)

# Build lookup by tweet_id
tweets = {str(t["tweet_id"]): t for t in data if t.get("tweet_id")}

# === SELECTED 15 TWEETS (no news accounts, no pure ads, good content) ===
SELECTED = [
    # 1. LeBron watching golf - casual fun
    {"id": "2042968119057031218", "reason": "Shams爆料爵士签约"},
    # 2. Kyrie Irving - mysterious emoji
    {"id": "2042817908460654655", "reason": "欧文神秘emoji暗示"},
    # 3. CJ McCollum - PLAYOFFS BOUND
    {"id": "2042777682664988697", "reason": "CJ确认进季后赛"},
    # 4. LeBron - golf excitement
    {"id": "2042732437931983263", "reason": "LeBron看高尔夫兴奋"},
    # 5. Mitchell - OMG JA!
    {"id": "2041969415281889736", "reason": "米切尔惊叹莫兰特"},
    # 6. Ja Morant - 暖心善举转发
    {"id": "2041857320192204808", "reason": "莫兰特转发暖心善举"},
    # 7. Ja Morant - Netflix推荐
    {"id": "2041852175089865127", "reason": "莫兰特求推荐Netflix"},
    # 8. Mitchell - #LetEmKnow
    {"id": "2041321266381005173", "reason": "米切尔LetEmKnow宣战"},
    # 9. Dame - Oakley签名活动
    {"id": "2041248769631830126", "reason": "利拉德眼镜签名活动"},
    # 10. KD - 杂志转发
    {"id": "2041196727743422530", "reason": "杜兰特转发杂志"},
    # 11. Kyrie - 转发社会评论
    {"id": "2040800075828412659", "reason": "欧文转发社会评论"},
    # 12. Tatum - 扣篮广告
    {"id": "2040508530482880528", "reason": "塔图姆第一次扣篮"},
    # 13. Trae Young - Jay Z quote
    {"id": "2039773635741663447", "reason": "特雷杨引用JayZ歌词"},
    # 14. Giannis - 投资合伙人
    {"id": "2039709086946644226", "reason": "字母哥宣布商业投资"},
    # 15. Westbrook - DeRozan破纪录
    {"id": "2039520551790936564", "reason": "威少转发德罗赞破纪录"},
]

ai = AIAssistant()
agent = TweetVideoAgent()

results = []

for idx, sel in enumerate(SELECTED, 1):
    tid = sel["id"]
    reason = sel["reason"]
    cover = os.path.join(COVERS, f"{tid}.jpg")

    if not os.path.exists(cover):
        print(f"\n[{idx}/15] SKIP - no cover: {tid}")
        continue

    t = tweets.get(tid)
    if not t:
        print(f"\n[{idx}/15] SKIP - no tweet data: {tid}")
        continue

    player = t.get("player_name", "")
    handle = t.get("player_handle", "")
    content = t.get("content", "")
    content_cn = t.get("content_cn", "")
    tweet_type = t.get("tweet_type", "original")

    print(f"\n{'='*60}")
    print(f"[{idx}/15] @{handle} ({player}) - {reason}")
    print(f"  EN: {content[:80]}")
    print(f"{'='*60}")

    # 1. AI commentary
    try:
        commentary = ai.generate_commentary(content, content_cn, f"{player} @{handle}")
    except Exception as e:
        print(f"  Commentary failed: {e}")
        commentary = content_cn[:80] if content_cn else content[:80]
    print(f"  解说: {commentary}")

    # 2. Claude music
    try:
        song = ai.recommend_song(content, content_cn, f"{player} @{handle}")
    except Exception:
        song = None
    print(f"  歌曲: {song}")

    # 3. Mood
    try:
        mood = ai.recommend_mood(content, content_cn)
    except Exception:
        mood = "chill"

    # 4. Generate video (single round, no iteration to save time)
    output_name = f"batch_{idx:02d}_{handle}.mp4"
    try:
        video_path = agent.generate(
            images=[cover],
            translations=[content_cn[:100] if content_cn else content[:100]],
            authors=[f"{player} @{handle}"],
            mood=mood,
            duration=12.0,
            output_name=output_name,
            commentary=[commentary],
            song_query=song,
        )
        print(f"  视频: {video_path}")

        results.append({
            "idx": idx,
            "tweet_id": tid,
            "player": player,
            "handle": handle,
            "reason": reason,
            "commentary": commentary,
            "song": song,
            "video": output_name,
            "video_path": video_path,
        })
    except Exception as e:
        print(f"  生成失败: {e}")
        results.append({
            "idx": idx,
            "tweet_id": tid,
            "player": player,
            "handle": handle,
            "reason": reason,
            "error": str(e),
        })

# Save results
with open(os.path.join(OUTPUT, "batch_results.json"), "w", encoding="utf-8") as f:
    json.dump(results, f, ensure_ascii=False, indent=2)

print(f"\n\n{'='*60}")
print(f"  批量生成完成: {len([r for r in results if 'video' in r])}/15 成功")
print(f"{'='*60}")
for r in results:
    status = "✅" if "video" in r else "❌"
    print(f"  {status} [{r['idx']:2d}] @{r['handle']:20s} - {r['reason']}")
    if "video" in r:
        print(f"       {r['video']}")
