"""ResearchAgent：用 SerpAPI（Google）给推文搜全网背景，输出一段中文友好的"背景简报"喂给 ScriptWriter。

目的：原推往往很短（如 "RIP B CLARKE"），ScriptWriter 没有上下文容易脑补错。
本 Agent 拉 top-N organic 结果的 title+snippet 作为事实背景，不让 ScriptWriter 直接朗读。

无 SERPAPI_KEY、网络失败、零结果时返回空字符串，调用方应当无背景继续走旧流程。
"""
import json
import os
import re
import httpx

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

_CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output", "research_cache")
_ENDPOINT = "https://serpapi.com/search.json"
_USAGE_PATH = os.path.join(_CACHE_DIR, "_usage.json")
_MONTHLY_QUOTA = 250
# 配额软门控阈值（剩余 ≤ 阈值时收紧/停搜）
_QUOTA_SOFT_LIMIT = 50   # 剩余 ≤50 → 软门控（仅高把握的"明显需要"放行）
_QUOTA_HARD_FLOOR = 10   # 剩余 ≤10 → 硬停（保留余量给最关键场景）

# tweets.json 路径（NBACrawler 是兄弟项目）
_TWEETS_JSON = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "NBACrawler", "output", "tweets.json",
)


def cleanup_orphan_cache():
    """删掉 research_cache 里 tweet_id 已不在 tweets.json 的孤儿文件。
    返回 (removed_count, kept_count)。tweets.json 不存在时跳过、不删任何文件。"""
    if not os.path.exists(_TWEETS_JSON):
        return 0, 0
    try:
        with open(_TWEETS_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
        live_ids = {str(t.get("tweet_id", "")) for t in data if t.get("tweet_id")}
    except Exception:
        return 0, 0
    if not live_ids:
        return 0, 0  # 解析出空集，保险起见不动
    removed = kept = 0
    if not os.path.isdir(_CACHE_DIR):
        return 0, 0
    for fn in os.listdir(_CACHE_DIR):
        if not fn.endswith(".json") or fn.startswith("_"):
            continue
        tid = fn[:-5]
        if tid in live_ids:
            kept += 1
            continue
        try:
            os.remove(os.path.join(_CACHE_DIR, fn))
            removed += 1
        except Exception:
            pass
    return removed, kept


def _bump_usage():
    """累加本月调用次数，返回 (count, quota)。"""
    import datetime
    os.makedirs(_CACHE_DIR, exist_ok=True)
    month = datetime.datetime.now().strftime("%Y-%m")
    data = {}
    if os.path.exists(_USAGE_PATH):
        try:
            with open(_USAGE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    data[month] = int(data.get(month, 0)) + 1
    try:
        with open(_USAGE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return data[month], _MONTHLY_QUOTA


def usage_this_month():
    import datetime
    month = datetime.datetime.now().strftime("%Y-%m")
    if os.path.exists(_USAGE_PATH):
        try:
            with open(_USAGE_PATH, "r", encoding="utf-8") as f:
                return int(json.load(f).get(month, 0)), _MONTHLY_QUOTA
        except Exception:
            pass
    return 0, _MONTHLY_QUOTA


class ResearchAgent:
    def __init__(self, top_n=5, char_limit=1500, timeout=15.0):
        self.top_n = top_n
        self.char_limit = char_limit
        self.timeout = timeout
        self.api_key = os.environ.get("SERPAPI_KEY", "")
        os.makedirs(_CACHE_DIR, exist_ok=True)
        # 启动时清一次孤儿缓存（轻量、idempotent；失败静默）
        try:
            removed, kept = cleanup_orphan_cache()
            if removed:
                print(f"  [ResearchAgent] 清理孤儿缓存: 删除 {removed} 条, 保留 {kept} 条")
        except Exception:
            pass

    def _cache_path(self, tweet_id):
        return os.path.join(_CACHE_DIR, f"{tweet_id}.json")

    def _load_cache(self, tweet_id):
        path = self._cache_path(tweet_id)
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return None
        return None

    def _save_cache(self, tweet_id, payload):
        try:
            with open(self._cache_path(tweet_id), "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    @staticmethod
    def _build_query(tweet_text, author=""):
        text = re.sub(r"https?://\S+", "", tweet_text or "").strip()
        text = re.sub(r"\s+", " ", text)
        if len(text) > 120:
            text = text[:120]
        if author:
            return f"{author} {text}".strip()
        return text

    def _search(self, query):
        params = {
            "engine": "google",
            "q": query,
            "api_key": self.api_key,
            "num": max(self.top_n, 5),
            "hl": "en",
        }
        with httpx.Client(timeout=self.timeout) as client:
            r = client.get(_ENDPOINT, params=params)
            r.raise_for_status()
            return r.json()

    @staticmethod
    def _format_brief(results, char_limit):
        lines = []
        used = 0
        for i, item in enumerate(results, 1):
            title = (item.get("title") or "").strip()
            snippet = (item.get("snippet") or "").strip()
            source = (item.get("source") or item.get("displayed_link") or "").strip()
            piece = f"[{i}] {title}\n    {snippet}"
            if source:
                piece += f"  —— {source}"
            if used + len(piece) > char_limit and lines:
                break
            lines.append(piece)
            used += len(piece)
        return "\n".join(lines)

    @staticmethod
    def _is_high_value(tweet_text, query_override):
        """判断这条推文是不是"明显需要"搜的高把握场景，用于软门控期间筛选。
        启发式：(a) 推文短（≤80 字符，难自洽）；或
                (b) 含关键事件词（RIP/breaking/sources/report/announce/trade）；或
                (c) LLM 给出了明显非空、非泛化的英文 query（包含人名+事件词）。
        """
        t = (tweet_text or "").strip()
        if 0 < len(t) <= 80:
            return True
        kw = ("RIP", "R.I.P", "breaking", "sources", "report", "announce", "announces",
              "trade", "traded", "signs", "signing", "waived", "released",
              "passed away", "dies", "died")
        low = t.lower()
        if any(k.lower() in low for k in kw):
            return True
        q = (query_override or "").strip()
        if len(q.split()) >= 4 and any(k.lower() in q.lower() for k in kw):
            return True
        return False

    def research(self, tweet_id, tweet_text, author="", force=False, query_override=""):
        """返回 dict: {query, results: [{title, snippet, link, source}], context_brief}.
        context_brief 为空字符串表示无可用背景，调用方按"无背景"处理即可。
        query_override 非空则用它替代默认 query（让 LLM 决定查什么更准）。
        """
        if not force:
            cached = self._load_cache(tweet_id)
            if cached:
                return cached

        if not self.api_key:
            return {"query": "", "results": [], "context_brief": ""}

        used, quota = usage_this_month()
        remaining = quota - used
        if remaining <= 0:
            return {"query": "", "results": [], "context_brief": "", "error": f"monthly_quota_exhausted_{used}/{quota}"}
        if remaining <= _QUOTA_HARD_FLOOR:
            print(f"  [ResearchAgent][warn] SerpAPI 剩余 {remaining}/{quota}，硬停搜索保留余量")
            return {"query": "", "results": [], "context_brief": "", "error": f"quota_hard_floor_{used}/{quota}"}
        if remaining <= _QUOTA_SOFT_LIMIT and not self._is_high_value(tweet_text, query_override):
            print(f"  [ResearchAgent][warn] SerpAPI 剩余 {remaining}/{quota}，软门控跳过低把握推文")
            return {"query": "", "results": [], "context_brief": "", "error": f"quota_soft_gate_{used}/{quota}"}

        query = (query_override or "").strip() or self._build_query(tweet_text, author)
        if not query:
            return {"query": "", "results": [], "context_brief": ""}

        try:
            data = self._search(query)
        except Exception as exc:
            return {"query": query, "results": [], "context_brief": "", "error": str(exc)[:200]}

        cnt, q = _bump_usage()
        if q - cnt <= 25:
            print(f"  [ResearchAgent][warn] SerpAPI 本月已用 {cnt}/{q}")

        organic = data.get("organic_results") or []
        slim = []
        for item in organic[: self.top_n]:
            slim.append({
                "title": item.get("title", ""),
                "snippet": item.get("snippet", ""),
                "link": item.get("link", ""),
                "source": item.get("source") or item.get("displayed_link") or "",
            })
        brief = self._format_brief(slim, self.char_limit)
        payload = {"query": query, "results": slim, "context_brief": brief}
        self._save_cache(tweet_id, payload)
        return payload
