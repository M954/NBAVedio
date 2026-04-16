"""ScriptWriter Agent - 编剧：将新闻数据转化为播报稿"""


class ScriptWriter:
    """将节目大纲转化为自然的中文播报脚本"""

    def generate_intro_script(self, show_title, version=1):
        if version >= 4:
            return (
                f"大家好，欢迎收看{show_title}！"
                f"我是你们的 AI 主播。"
                f"今天我们为您带来最新的 NBA 篮球资讯，精彩内容马上开始！"
            )
        elif version >= 2:
            return (
                f"大家好，欢迎收看{show_title}！"
                f"今天为您带来最新的 NBA 篮球资讯。"
            )
        else:
            return f"欢迎收看{show_title}，以下是今日要闻。"

    def generate_news_script(self, segment, version=1):
        title = segment["title_cn"]
        summary = segment["summary_cn"]
        idx = segment["index"]

        if version >= 4:
            return (
                f"接下来是第{idx}条新闻。"
                f"{title}。"
                f"{summary}"
                f"这条消息来源于{segment.get('source', 'NBA官方')}。"
            )
        elif version >= 2:
            return (
                f"第{idx}条，{title}。"
                f"{summary}"
            )
        else:
            return f"第{idx}条新闻：{title}。{summary}"

    def generate_outro_script(self, version=1):
        if version >= 4:
            return (
                "以上就是今天的全部内容。"
                "感谢您的收看！"
                "如果喜欢我们的节目，请点赞关注，我们下期再见！"
            )
        elif version >= 2:
            return "以上就是今天的 NBA 快报，感谢收看，下期再见！"
        else:
            return "感谢收看，再见。"

    def run(self, show_plan, version=1):
        scripts = []
        for seg in show_plan["segments"]:
            if seg["type"] == "intro":
                text = self.generate_intro_script(show_plan["show_title"], version)
            elif seg["type"] == "news":
                text = self.generate_news_script(seg, version)
            elif seg["type"] == "outro":
                text = self.generate_outro_script(version)
            else:
                continue
            scripts.append({
                "type": seg["type"],
                "text": text,
                "segment": seg,
            })
        print(f"[ScriptWriter] 生成了 {len(scripts)} 段脚本 (v{version})")
        return scripts
