# Lessons Learned — Anti-Hallucination & Multi-LLM Pipeline

Working journal of real bugs and the principles distilled from them.
**Read this before changing any prompt / agent / cache schema.**

---

## 🪤 Real Bugs (with file references + log evidence)

Every entry below was a real bug seen in production logs. Format: **Symptom → Root cause → Fix → Lesson**.

### Bug A: Reaction-type tweets bypassed research → empty narration
- **Symptom**: Tweet `"Wemby may actually be an Alien 👽"` → `needs_research` returned False → narration could only chant "dominance" / "season's loaded" with zero actual game facts.
- **Where**: `agents/ai_assistant.py::needs_research`, original prompt only covered "short/vague tweets" and "implied external events" — missed *"reaction tweets that are literally complete but factually empty"*.
- **Fix**: A+B defense
  - **A**: prompt 4th rule explicitly listing reaction patterns
  - **B**: heuristic fallback — short text + reaction keyword dict (`alien/insane/goat/he's him/外星人/无解...`) forces `need=True` even when LLM says False
- **Files**: `agents/ai_assistant.py::_REACTION_KEYWORDS`, `_looks_like_reaction`, `needs_research`
- **Lesson**: **"LLM can understand X" ≠ "LLM can produce good content from X"**. Design decision criteria around what the downstream needs, not around what's literally readable.

### Bug B-series: Fact extraction lost critical context (4 iterations)
- **Symptom**: SerpAPI returned snippets containing `WCF Game 1 vs OKC`, but after fact extraction the brief only had `"recent game (Monday)"`. Lost playoffs/conference-finals/opponent context. Narration came out as "this season is taking off" — regular-season tone applied to a playoff game.
- **Iteration history (this IS the cost of skipping eval)**:
  - **B**: Added `event_type` + `stakes` fields. Brief improved but generic ("playoffs key game").
  - **B-2**: Added strict tone-by-event_type rules in ScriptWriter. Still imprecise.
  - **B-3**: Split `event_type` into 9 granular levels (first_round / semifinals / WCF / finals ...), added `opponent` / `series_score` + anti-hallucination rules. LLM **still** hallucinated opponent as "Timberwolves" instead of OKC.
  - **B-4 (root-cause fix)**: **Killed the 13-field fact extraction layer entirely**. Replaced with 3 fields: `summary` + `raw_snippets_relevant` + `confidence`. Let downstream ScriptWriter read raw SerpAPI snippets directly. **One less LLM = one less hallucination chance.**
- **Files**: `agents/research_agent.py::_extract_facts` (rewritten in B-4)
- **Lessons**:
  1. **Each LLM transformation = another hallucination opportunity.** Adding extraction fields just adds more places to hallucinate.
  2. **If downstream can read the raw source, don't extract first.** `raw → LLM → 13 fields → LLM → narration` is worse than `raw → LLM → narration`.
  3. **Adding fields is patching. Cutting layers is fixing.**

### Bug C: LLM treated training cutoff as "today"
- **Symptom**: Review LLM saw tweet screenshot dated `May 19, 2026` → flagged "future date = fabricated material". Brandon Clarke memorial video was flagged "player isn't dead, video is fake".
- **Root cause**: No prompt had a `today_date` anchor. Claude/Gemini training cutoff is 2024-early 2025; seeing 2026 dates → assumes fake data.
- **Fix**: 4 prompts now include `今天日期: YYYY-MM-DD` header:
  - `needs_research` (`ai_assistant.py`)
  - `generate_commentary` (`ai_assistant.py`)
  - `_extract_facts` (`research_agent.py`)
  - `review_video` — both Gemini and Claude prompts (`ai_assistant.py`)
- **Lesson**: **LLMs don't know what day it is**. Any prompt involving time perception must inject the date explicitly, or the model uses its training-data worldview to judge facts.

### Bug D: Stale cache hid prompt fixes
- **Symptom**: Fixed Bug B, reran Wemby tweet, brief had no new fields → assumed fix didn't apply. Actually `ResearchAgent` hit old cache and returned the pre-fix payload.
- **Root cause**: `_load_cache` only checked file existence, **not schema version**.
- **Fix**:
  - `_CACHE_SCHEMA_VERSION` constant — bump on any breaking schema change
  - `_save_cache` stamps the version
  - `_load_cache` rejects stale-version or missing-key payloads → forces re-extract
- **Files**: `agents/research_agent.py::_load_cache`, `_save_cache`, `_CACHE_SCHEMA_VERSION`
- **Lesson**: **Any cached LLM output needs a schema version field**. Otherwise: change prompt → run → no change → suspect the wrong thing → waste an hour debugging.

### Bug E: Log panel couldn't be selected / forced auto-scroll
- **Symptom**: Log panel polled every 3s; selecting text → selection vanished on next tick. Scrolling up to read old logs → yanked back to bottom 3s later.
- **Root cause**: `updateLogPanel` always ran `el.innerHTML = ...` → DOM nodes rebuilt → selection lost. `scrollTop = scrollHeight` overrode user scroll intent.
- **Fix**:
  - `_isUserSelectingIn(el)` — skip refresh while user is actively selecting inside the panel
  - Removed auto-scroll-to-bottom entirely; pure manual scroll
- **Files**: `NBACrawler/web/templates/index.html::updateLogPanel`, `pollVideoLogs`
- **Lesson**: **Polling + innerHTML rewrite is a UX killer**. Any polling refresh must check "is user interacting?" and skip if yes.

### Bug F: ProcessPool spawn mode doesn't hot-reload code
- **Symptom**: Changed Python code but didn't restart FastAPI → still running old logic.
- **Root cause**: `ProcessPoolExecutor(mp_context="spawn")` workers load **a snapshot of code at startup time**. Source changes don't propagate.
- **Fix**: **Restart FastAPI after any change under `agents/`**. Long-term option: add file watcher → SIGTERM executor → rebuild on change.
- **Lesson**: **Multiprocess + spawn = restart required**. Silent footgun — first time you hit it, you'll think your fix is broken.

### Bug G: Review LLM misattributes problems across dimensions
- **Symptom**: A high-quality narration ("Wemby G1 vs OKC, 41+24, double-OT 122-115, joined Wilt Chamberlain as the only 40+20 player") scored only 72/C. Two deductions in `content_issues`:
  1. "41+24 / double-OT 122-115 — no on-screen footage to back it up" (user only provided the tweet screenshot; there *is* no game footage to show — this is an input limitation, not a content error)
  2. "TTS pronunciation of '封神了' unclear (Whisper heard '瘋什麼')" — a **voice quality** issue mis-classified as **content accuracy**
- **Root cause** (3 layers):
  1. Review prompt's "content accuracy" definition is too narrow ("是否忠实于推文原文"), but the LLM in practice expanded it to swallow visual + audio issues
  2. Review prompt had no awareness of **user input constraints** — didn't know the user only provided one screenshot, so flagged "no footage" as a defect
  3. Review prompt didn't receive the **ResearchAgent brief** — couldn't know that "41+24 double-OT" had ESPN as an authoritative source, so flagged it as "unsupported on-screen"
- **Fix** (B + C combined):
  - **B**: Added two hard blocks to both Gemini and Claude review prompts:
    - `input_block` — declares whether user provided a source video; if not, explicitly says "do not deduct content-accuracy points for 'monotonous frames' / 'no B-roll' / '8 frames all show the tweet screenshot' — that's an input limit, not the narrator's fault"
    - `dimension_rules` — explicit mapping table: visual monotony → 「视觉效果」, TTS clarity → 「配音效果」, BGM → 「配乐质量」, narration style → 「解说风格」, **only fact-vs-source mismatch → 「内容准确性」**. Specifically calls out "TTS unclear / monotonous visuals being deducted under content accuracy" as a known common mistake.
  - **C**: Threaded `context_brief` from `run_pipeline` → `_iterate_with_review` → `review_video::video_info` → both review prompts. Each prompt now has a `brief_block`: *"hard facts the narration cites (player stats, opponent, score, series stage) that can be found verbatim in this brief = authoritative source; do not flag as fabricated or 'unsupported'."*
- **Files**:
  - `agents/pipeline_core.py::_iterate_with_review` (added `context_brief` parameter; threaded into `info` dict)
  - `agents/pipeline_core.py::run_pipeline` (passes `context_brief` to `_iterate_with_review`)
  - `agents/ai_assistant.py::review_video` (built `input_block`, `brief_block`, `dimension_rules`; injected into both Gemini and Claude prompts)
- **Lesson**: **LLM-as-judge inherits the same problems as the LLMs it judges** — vague rubrics get interpreted creatively, missing context gets filled with bad assumptions. Judge prompts need (1) input boundaries the judge can see, (2) authoritative context the judged work was based on, (3) explicit anti-mistake reminders for known common errors. **A scoring rubric is itself a prompt that needs all the same anti-hallucination defenses as the generation prompt.**

### Bug H: Heuristic referenced wrong class — silent NameError in subclass path
- **Symptom**: First-ever eval run crashed: `AttributeError: 'function' object has no attribute '_REACTION_KEYWORDS'` on `_looks_like_reaction`. Production never hit it because `needs_research` wraps `self._call` in `except Exception` → the AttributeError was swallowed, `need_llm` silently became `False`, and the heuristic branch was never reached. **The reaction-tweet defense was dead code in production.**
- **Root cause**: `_looks_like_reaction` referenced `AIAssistant._REACTION_KEYWORDS` but the constant lives on `_BaseAssistant`, and `AIAssistant` here is the Claude subclass — Python resolves the name at call time, finds nothing, raises. The bug was masked because the calling site catches `Exception`.
- **Fix**: Reference `_BaseAssistant._REACTION_KEYWORDS` directly (`agents/ai_assistant.py::_looks_like_reaction`).
- **How it was found**: The eval framework's first invocation. Manual production runs had been getting "lucky" — the LLM was answering `need=True` often enough that the broken fallback never showed up. Eval forced every case, including ones designed to test the fallback.
- **Lessons**:
  1. **Broad `except Exception` hides class-of-bug failures.** If a defensive fallback is wrapped in catch-all, you can never tell if it's working — write the fallback so its own errors surface, or assert it ran.
  2. **Eval finds bugs that manual testing structurally cannot.** This bug existed for the entire life of the heuristic and was invisible until a non-LLM-driven harness exercised the code path.

---

## 🧪 Eval Framework

Lives in `NBAVedio/evals/`. Designed around two hard rules:

1. **Paid APIs must never be re-billed by eval runs.** Default mode = cache-only; cache miss raises. Live calls require explicit `--record`.
2. **Every cached call's input + output is committed to the repo.** So CI / teammates re-run for free, and prompt diffs show up as cache-key churn.

### Files
- `dataset.jsonl` — hand-labeled ground truth (15 seed cases covering reaction / breaking / playoff / off-topic / injury / milestone).
- `_cache.py` — sha256-keyed on-disk cache. `CacheMiss` raises unless `record_mode=True`.
- `run_needs_research.py` — first eval target: `needs_research` decision accuracy. Reports precision / recall / F1 + per-category breakdown + per-case mismatches.

### Baseline (recorded 2026-05-24, post Bug-H fix)
```
cases: 15   accuracy=80.00%   precision=81.82%   recall=90.00%   F1=85.71%
mismatches: drake_goat (FP, heuristic), injury_jokic (FN, LLM), free_agency_open (FP, LLM)
```

### Why this exists
LESSONS_LEARNED Bug B took **4 iterations** of prompt tweaking against a single case. Trigger to build eval: "you've changed the same module 3+ times." We hit it; we built it.

---

## 🛠️ Required Workflow for Any Future Improvement

Stop. Read this. Don't skip steps.

### Step 1: Reproduce + collect evidence (5 min)
- Open `output/logs/video.log`, find the full log for the failing run
- Extract 3 things: **input tweet**, **intermediate artifacts** (brief / commentary), **final output**
- **Don't diagnose from memory** — use logs + files as ground truth
- Commands:
  ```bash
  grep -n "<tweet_id first 8 chars>" output/logs/video.log
  cat output/research_cache/<tweet_id>.json
  ```

### Step 2: Distinguish symptom vs root cause (10 min)
Ask 3 questions:
- Is this a **one-off** (coincidence on this tweet) or a **class bug** (whole category will fail)?
- Will fixing it **break other cases**?
- Am I **adding fields (patching)** or **cutting a layer (fixing)**?
- **Red flag**: if you've changed the same module 3+ times without resolving it, **stop — you're playing whack-a-mole**. Consider architectural change instead of more prompt tweaks (see Bug B's B → B-4 evolution).

### Step 3: Think about cache & versions before editing (2 min)
- Will the code I'm about to change get cached anywhere? (`research_cache` / subprocess code snapshot / browser)
- If yes: bump schema version / restart service / hard-refresh?
- **Skip this → next run shows no change → 1 hour debugging the wrong thing**.

### Step 4: Edit + restart + rerun (10-20 min)
- Edit code
- **Restart FastAPI** (mandatory)
- **Run eval cache-only first**: `cd NBAVedio && python -m evals.run_needs_research` — confirms no regression on the 15 labeled cases, costs zero API quota
- Rerun the same tweet
- **Must verify 4 things**:
  1. New log contains the expected new behavior (new field, new rule trigger)
  2. Final narration / brief reflects the fix
  3. **Other cases aren't affected** (manually check 1-2 unrelated tweets)
  4. No new warnings / errors

### Step 5: Document (5 min — easiest to skip, most important)
- Add to this file's "Real Bugs" section AND `INTERVIEW_NOTES.md` (for interview prep)
- Three things: **symptom / root cause / lesson** — lesson must **prevent the class of problem**, not just describe this instance
- If eval framework exists: **add this case to the eval set** — every future change auto-verifies no regression

---

## 📐 When to Stop and Build Eval

**Trigger: you've changed the same module 3+ times.**

At that point:
- You're doing prompt engineering but only validating "this one case" — can't guarantee other cases didn't regress
- **"Seeing is believing" mode is unsustainable** — next fix might break the previous fix and you won't know
- Freeze manual debugging, build a minimal eval (15-20 case dataset + auto-scoring), then continue

**The actual lesson from B-series**: 4 iterations (B → B-2 → B-3 → B-4) before cutting to the root cause. If we had stopped after B-2 and built eval, we'd have saved 2 wasted rounds AND noticed the architectural issue earlier.

---

## 🧭 Design Principles (distilled)

1. **Anti-hallucination is multi-layer, not single-prompt.**
   - Inject facts before generation (ResearchAgent)
   - Cross-validate after generation (dual-agent review)
   - Hard rules in prompt (`do not fabricate`) are necessary but not sufficient

2. **Prefer fewer LLM transformations.**
   - Each LLM step = chance to hallucinate or lose context
   - Let downstream read raw upstream when possible
   - "Structured extraction" sounds good but often loses information

3. **Every LLM cache needs a schema version.**
   - Bump on breaking changes; let `_load_cache` reject stale entries
   - Saves hours of "why isn't my fix working?"

4. **Every prompt involving time needs an explicit date.**
   - LLM training cutoff ≠ today
   - 4-line `今天日期:` header is non-negotiable

5. **Heuristics > LLM for high-confidence patterns.**
   - Reaction keyword list catches what LLM-as-classifier misses
   - Cheap, deterministic, testable

6. **Multiprocess + spawn = restart required.**
   - No hot reload. Build the restart habit; or add file watcher.

---

## 📋 Active Bug Backlog (not yet fixed)

| ID | Description | Severity |
|---|---|---|
| — | (none open as of last update) | — |

Add new entries here as discovered. Move to "Real Bugs" section once fixed.
**When you fix a bug, add the case to `evals/dataset.jsonl` so the next change auto-verifies no regression.**
