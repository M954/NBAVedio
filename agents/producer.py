"""Producer Agent - 制片人：选择新闻、规划节目流程"""
import json


class Producer:
    """从新闻数据中选择最佳内容并规划节目流程"""

    def __init__(self, max_news=5):
        self.max_news = max_news

    def load_news(self, json_path):
        with open(json_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def select_news(self, all_news):
        """选择最有价值的新闻，优先选择有实质性内容的"""
        scored = []
        for news in all_news:
            score = 0
            title = news.get("title_cn", "") or news.get("title", "")
            summary = news.get("summary_cn", "") or news.get("summary", "")
            # 有实际新闻内容的优先
            if len(summary) > 20:
                score += 3
            # 有作者的更可信
            if news.get("author"):
                score += 1
            # 排除纯赔率/投注类
            skip_keywords = ["赔率", "投注", "盘口", "精选推荐", "预测、精选"]
            if any(kw in title for kw in skip_keywords):
                score -= 5
            # 排除"在哪里观看"类
            if "哪里可以观看" in title or "在哪里观看" in title:
                score -= 5
            scored.append((score, news))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [item[1] for item in scored[: self.max_news]]

    def plan_show(self, selected_news):
        """生成节目大纲"""
        segments = []
        segments.append({
            "type": "intro",
            "title": "NBA 每日快报",
            "description": "开场白",
            "duration_hint": 5,
        })
        for i, news in enumerate(selected_news):
            segments.append({
                "type": "news",
                "index": i + 1,
                "title_cn": news.get("title_cn", news.get("title", "")),
                "title_en": news.get("title", ""),
                "summary_cn": news.get("summary_cn", news.get("summary", "")),
                "summary_en": news.get("summary", ""),
                "source": news.get("source", ""),
                "duration_hint": 8,
            })
        segments.append({
            "type": "outro",
            "title": "感谢收看",
            "description": "结尾致谢",
            "duration_hint": 4,
        })
        return {"show_title": "NBA 每日快报", "segments": segments}

    def run(self, json_path):
        all_news = self.load_news(json_path)
        selected = self.select_news(all_news)
        show_plan = self.plan_show(selected)
        print(f"[Producer] 选择了 {len(selected)} 条新闻，共 {len(show_plan['segments'])} 个片段")
        return show_plan
