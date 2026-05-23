"""ResearchAgent：用 SerpAPI（Google）给推文搜全网背景，输出一段中文友好的"背景简报"喂给 ScriptWriter。

目的：原推往往很短（如 "RIP B CLARKE"），ScriptWriter 没有上下文容易脑补错。
本 Agent 拉 top-N organic 结果的 title+snippet 作为事实背景，不让 ScriptWriter 直接朗读。

无 SERPAPI_KEY、网络失败、零结果时返回空字符串，调用方应当无背景继续走旧流程。
"""
import datetime
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

# Cache schema 版本——_extract_facts 的输出字段集发生破坏性变更时 +1
# 老 cache 缺关键字段或版本不符时 _load_cache 会自动忽略并重跑事实抽取
_CACHE_SCHEMA_VERSION = 4  # v4: 极简 3 字段（summary + raw_snippets_relevant + confidence），下游直读原文


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
    def __init__(self, top_n=5, char_limit=1500, timeout=15.0, ai=None):
        self.top_n = top_n
        self.char_limit = char_limit
        self.timeout = timeout
        self.api_key = os.environ.get("SERPAPI_KEY", "")
        # 可选 AI 实例：拿到 SerpAPI snippets 后调一次 LLM 抽事实，渲染更可用的 brief。
        # 为 None 时退化到原始 snippet 拼接（保持向后兼容）。
        self.ai = ai
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
                    payload = json.load(f)
            except Exception:
                return None
            # Schema 兼容性检查：版本不符 → 忽略 cache 重跑
            if payload.get("_schema_version") != _CACHE_SCHEMA_VERSION:
                return None
            # v4 极简 schema：facts 高/中置信度时必须含 summary 字段
            facts = payload.get("facts") or {}
            confidence = (facts.get("confidence") or "").lower()
            if confidence and confidence != "low":
                if "summary" not in facts:
                    return None
            return payload
        return None

    def _save_cache(self, tweet_id, payload):
        try:
            payload = dict(payload)
            payload["_schema_version"] = _CACHE_SCHEMA_VERSION
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
        raw_brief = self._format_brief(slim, self.char_limit)
        facts, brief = self._extract_facts(tweet_text, author, slim, raw_brief)
        payload = {
            "query": query,
            "results": slim,
            "context_brief": brief,
            "facts": facts,
            "raw_brief": raw_brief,
        }
        self._save_cache(tweet_id, payload)
        return payload

    def _extract_facts(self, tweet_text, author, slim_results, raw_brief):
        """让 LLM 做轻量"筛选 + 置信度判断"，不再抽硬事实字段。
        硬事实（对手/场次/比分）留给下游 ScriptWriter 直接读 raw snippets ——
        少一层 LLM 转换 = 少一次幻觉机会。

        返回 (facts, brief)，facts 只含三字段：
          - summary: 一句话浓缩相关搜索结果讲的事
          - raw_snippets_relevant: 从原始 snippet 里挑出的最相关原文（保留 link/source）
          - confidence: high/medium/low
        brief 渲染：summary 一行 + 相关 snippet 原样列出，让下游自行核对硬事实。
        """
        if not self.ai or not slim_results:
            return {}, raw_brief
        sources_text = "\n".join(
            f"[{i}] {r.get('title','')} —— {r.get('snippet','')}  ({r.get('source','')})"
            for i, r in enumerate(slim_results, 1)
        )
        today_str = datetime.date.today().isoformat()
        prompt = (
            "你在帮一个NBA短视频博主从全网搜索结果里筛选相关内容。\n"
            "你的任务【只有筛选 + 一句话总结 + 置信度判断】，不要试图抽取结构化字段，"
            "也不要重写事实——硬事实（对手、场次、比分、伤情）下游会直接读原始 snippet。\n\n"
            f"【时间上下文】今天日期: {today_str}\n"
            f"搜索结果里的日期是真实的，不要因为与你训练数据不符就当成伪造。\n\n"
            f"原推文（作者: {author or '未知'}）:\n{tweet_text}\n\n"
            f"全网搜索结果（多来源，质量参差）:\n{sources_text}\n\n"
            "请严格输出一个 JSON 对象（不要 markdown 代码块、不要解释），三个字段：\n"
            "{\n"
            '  "summary": "一句话总结相关搜索结果在讲什么，必须基于原文 snippet，'
            '不要复述推文措辞，不要引入 snippet 没说的细节（人物关系、对手球队等）。'
            '如果多条 snippet 互相矛盾，写明矛盾点而不是任选一个。20-60字。",\n'
            '  "relevant_indices": [挑出与推文最相关的 snippet 编号数组，例 [1,2,5]；'
            '宁缺勿滥，不相关的别选；可以为空数组],\n'
            '  "confidence": "high (snippet 明确讲推文同一件事) / medium (有相关但不完整) / '
            'low (相关度低或互相矛盾)"\n'
            "}\n\n"
            "硬规则：\n"
            "- 只输出 JSON，不要任何前后文字\n"
            "- summary 里不得编造 snippet 中没出现的硬事实（对手球队名、Game几、比分、死因等）\n"
            "- relevant_indices 必须是 snippet 真实编号（1..N），不存在的编号不要写\n"
            "- 多条 snippet 讲不同事件时，只挑与推文最对得上的那些；矛盾就在 summary 里点出来\n"
            "- 不确定就给 medium 或 low，不要硬给 high\n"
        )
        try:
            raw = self.ai._call(prompt, system="你是一个严谨的检索结果筛选器，只输出合法 JSON。")
            if not raw:
                return {}, raw_brief
            m = re.search(r"\{[\s\S]*\}", raw)
            if not m:
                return {}, raw_brief
            parsed = json.loads(m.group(0))
        except Exception:
            return {}, raw_brief

        summary = (parsed.get("summary") or "").strip()
        confidence = (parsed.get("confidence") or "").strip().lower()
        if confidence not in ("high", "medium", "low"):
            confidence = "low"
        # 校验 relevant_indices 并拿到对应的原始 snippet 子集
        raw_indices = parsed.get("relevant_indices") or []
        relevant = []
        if isinstance(raw_indices, list):
            seen = set()
            for v in raw_indices:
                try:
                    idx = int(v)
                except Exception:
                    continue
                if 1 <= idx <= len(slim_results) and idx not in seen:
                    seen.add(idx)
                    relevant.append(slim_results[idx - 1])
        # 没挑出任何相关 snippet 但 confidence>=medium 时降级——避免空腹喂下游
        if not relevant and confidence != "low":
            confidence = "low"

        facts = {
            "summary": summary,
            "raw_snippets_relevant": relevant,
            "confidence": confidence,
        }

        # 低置信度：明确告诉下游"别引入背景"
        if confidence == "low":
            brief = (
                "【背景检索置信度低，搜索结果与推文相关性不强或互相矛盾，"
                "解说词不要引入额外背景，只基于推文本身写】\n"
                + (f"参考摘要：{summary}" if summary else "")
            )
            return facts, brief

        # 高/中置信度：summary 一行打头 + 相关 snippet 原文按编号列出
        lines = []
        if summary:
            lines.append(f"⭐ 检索摘要（置信度 {confidence}）：{summary}")
        lines.append("")
        lines.append("【相关原文 snippet（请直接基于以下原文写解说，硬事实必须在这里找得到字面证据）】")
        for i, r in enumerate(relevant, 1):
            title = (r.get("title") or "").strip()
            snippet = (r.get("snippet") or "").strip()
            source = (r.get("source") or "").strip()
            piece = f"[{i}] {title}\n    {snippet}"
            if source:
                piece += f"  —— {source}"
            lines.append(piece)
        brief = "\n".join(lines)
        return facts, brief
