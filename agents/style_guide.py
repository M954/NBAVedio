"""
Style guide for AI video narration agents.

Encodes commentary style rules, reference transcripts, player nicknames,
and linguistic constraints derived from analysis of top-performing videos.
"""

# ---------------------------------------------------------------------------
# 1. Representative transcript examples (5 best, diverse topics)
# ---------------------------------------------------------------------------
STYLE_EXAMPLES = [
    # melo_01 — sportsmanship / emotional story
    (
        "老八杰维今日发推，谈到鲁卡受伤真的值得圈粉 在雷霆大胜胡人的赛后 "
        "他表示 我真的为他感到难过 而当时防守的人是我 这感觉我很好受 "
        "当他放下篮球之日 我顺势把球故意碰出界外 想给他点时间缓一缓 "
        "但看着情况很糟糕 祝愿他早日康复 天啊 老八真的太善良了 为他点赞"
    ),
    # melo_02 — legendary rivalry / nostalgia
    (
        "老詹今日发推，再度复刻奥运会与老柯的瞬间 老詹在最新播客中 回忆起了当年防守柯比的瞬间 "
        "他表示那家伙骗了所有人 都以为他要打战术 实则他就是要拉开单打 想弄死我 "
        "老詹与老柯的这一名场面 确实让人记忆深刻 哎 物是人非了"
    ),
    # melo_05 — humor / light news
    (
        "这都能搞错啊 开拓者官方在伤病名单上闹了个乌龙 "
        "他们把下放到发展联盟的杨汉森 标注为双向合同 肖杨只是被下放 "
        "但他的合同还是一份NB标准合同 因此 球迷展开了热议 "
        "其中有部分声音表示 开拓者亚根就不重视和尊重杨汉森 "
        "而且也不给他出场时间 培养价值还不如双向 肖杨真的渐渐失去了信任了吗"
    ),
    # melo_08 — brotherhood / support
    (
        "永远支持自己的好兄弟 近日欧文在接受采访中 "
        "谈到了东西齐渡过了胡人近日的低谷 迎来了状态复苏 "
        "欧文表示道 我很高兴他能打出这样的表现 也重新找回了笑容 "
        "我和他经常保持交流 他也什么都会跟我说 他是我的兄弟 "
        "我会一直支持他 我在达拉斯真的很想念他 "
        "欧文与鲁卡的友谊 真的是超越了篮球 没得说"
    ),
    # melo_09 — greatness / milestone
    (
        "他是另一维度的存在 杜兰特在近期的播客节目中 "
        "谈到生涯总得分超越了乔丹 他表示 和乔丹相比 "
        "数据的本身并没有那么重要 乔丹早已经超越了篮球这项运动的本身 "
        "无论谁在数据上或荣誉上超越他 但都很难真正的赢过他 "
        "他对这项运动 乃至整个文化的影响力太多大 "
        "看得出 阿渡对乔丹只有敬仰了"
    ),
]

# ---------------------------------------------------------------------------
# 2. Commentary style rules
# ---------------------------------------------------------------------------
COMMENTARY_RULES = """\
## Opening
Always lead with the player's nickname followed by an emotional hook or \
shocking statement. Grab attention in the first sentence.

## Structure
Hook -> Context/Event -> Analysis -> Personal Take / Emotional Close.

## Fact vs Editorial ratio
Target roughly 45% factual reporting and 55% editorial commentary / personal \
opinion. The audience expects the narrator's take, not a news anchor read.

## Tone
Casual and conversational, like chatting with a basketball-obsessed friend. \
Be opinionated — take a clear stance and back it up.

## Closings
End with one of:
  - An emotional callback to the opening hook
  - A rhetorical question that lingers
  - An explicit value judgment (praise, criticism, or awe)

## General
- Keep sentences short and punchy for TTS pacing.
- Use spoken-Chinese rhythm; avoid written-Chinese formalism.
- Sprinkle conversational markers naturally (see REQUIRED_MARKERS).
- Never use stiff formal vocabulary (see FORBIDDEN_WORDS).
"""

# ---------------------------------------------------------------------------
# 3. Player nickname mapping (English name -> Chinese nickname)
# ---------------------------------------------------------------------------
PLAYER_NICKNAMES = {
    # players.json 全员（请填写昵称，没有就留空字符串）
    "LeBron James": "老詹",
    "Stephen Curry": "库里",
    "Kevin Durant": "阿杜",
    "Giannis Antetokounmpo": "字母哥",
    "Luka Doncic": "东契奇",
    "Jayson Tatum": "獭兔",
    "Joel Embiid": "大帝",
    "Anthony Edwards": "华子",
    "Shai Gilgeous-Alexander": "SGA",
    "Ja Morant": "莫兰特",
    "Devin Booker": "布克",
    "Damian Lillard": "利指导",
    "Jimmy Butler": "巴特勒",
    "Donovan Mitchell": "米神",
    "Trae Young": "吹杨",
    "Zion Williamson": "胖虎",
    "Paolo Banchero": "班切罗",
    "Victor Wembanyama": "文班",
    "Tyrese Haliburton": "哈里伯顿",
    "De'Aaron Fox": "福克斯",
    "Chet Holmgren": "",
    "Kyrie Irving": "文仔",
    "Paul George": "泡椒哥",
    "Karl-Anthony Towns": "唐斯",
    "Draymond Green": "追梦",
    "Chris Paul": "老炮",
    "Russell Westbrook": "威少",
    "Bradley Beal": "比尔",
    "CJ McCollum": "",
    "DeMar DeRozan": "德罗赞",
}

# ---------------------------------------------------------------------------
# 4. Formal words to avoid — these sound too stiff for the casual style
# ---------------------------------------------------------------------------
FORBIDDEN_WORDS = [
    "公开表态",
    "隔空致意",
    "展现了",
    "彰显了",
    "以此表达",
    "认可与致敬",
    "综上所述",
]

# ---------------------------------------------------------------------------
# 5. Conversational markers to weave into scripts
# ---------------------------------------------------------------------------
REQUIRED_MARKERS = [
    "真的",
    "太",
    "算是",
    "天啊",
    "好家伙",
    "没得说",
    "直接",
    "拉满",
    "懂的都懂",
]
