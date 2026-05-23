# NBA Tweet Video Generator

AI-powered system that turns NBA player tweets into short vertical videos (9:16) with Chinese narration, dynamic subtitles, AI voice, and mood-matched background music. Designed around **multi-LLM collaboration** and **anti-hallucination defenses**.

---

## Highlights

- **4 LLMs working together** — Claude / GPT (text), Gemini (video), Whisper (audio transcription); each handles what it does best
- **ResearchAgent** — SerpAPI Google search + LLM fact extraction with `confidence` field; feeds factual background to the narration writer so the LLM doesn't fabricate player/team/event details
- **Dual-Agent Video Review** — Gemini watches the full video, Claude inspects 8 sampled frames vs Whisper-transcribed audio; cross-validation catches subtitle/narration mismatches and fabrications
- **Iterative Quality Loop** — 6-dimension 100-point scoring; below grade A (≥90) the system rewrites narration and reselects BGM, up to 3 rounds by default
- **ffmpeg filter_complex fast path** — single-command composition (subtitles + TTS + BGM), 3-5× faster than MoviePy
- **GPU encoder auto-detection** — probes NVENC / QSV / AMF → falls back to libx264 ultrafast; ~10× faster than CPU-only
- **Three-tier TTS fallback** — Azure Speech (paid, reliable HTTPS) → edge-tts (free WebSocket, 4 voices × 3 retries) → silent WAV (so ffmpeg never crashes)
- **SerpAPI three-tier quota gating** — 250/month budget; soft gate at ≤50 remaining (only high-value tweets pass), hard floor at ≤10
- **FastAPI service with subprocess isolation** — ProcessPool jobs, `/cancel` force-kills the worker and rebuilds the executor
- **Per-video ≤10 MB size cap** — tuned for real publishing platform constraints

---

## Pipeline (8 steps)

Orchestrated by `agents/pipeline_core.py::run_pipeline`:

```
1. Translation refinement        (LLM polishes the machine translation)
2. Video analysis + smart trim   (Gemini decides when source > 50s)
3. Background research           (ResearchAgent — only triggered when needed)
4. Narration generation          (with research brief, fabrication-resistant)
5. BGM recommendation            (mood matching + local BGM library)
6. Highlight detection           (optional — Gemini picks original-audio segments)
7. Composition + AI review       (loop up to N rounds)
8. Grade-A output                (score ≥ 90 → auto-stop, copy final mp4)
```

**Iteration**: review feedback (`content_issues`, `subtitle_mismatches`, `suggestions`) is fed back per-item to the ScriptWriter and BGM picker — not a generic rewrite. Typical convergence: 2-3 rounds.

---

## Agents

| Agent | File | Role |
|---|---|---|
| **pipeline_core** | `agents/pipeline_core.py` | The 8-step orchestrator; owns iteration, scoring, fallback logic |
| **AIAssistant** | `agents/ai_assistant.py` | Multi-backend: ClaudeAssistant / GptAssistant; also wraps Gemini for video and Whisper for audio |
| **ResearchAgent** | `agents/research_agent.py` | SerpAPI search + LLM fact extraction with confidence gating + local cache + quota gating |
| **TweetVideoAgent** | `agents/tweet_video_agent.py` | Vertical video assembly; GPU encoder probing; subtitle PNG rendering |
| **ffmpeg_renderer** | `agents/ffmpeg_renderer.py` | Fast-path single-command composition via `filter_complex` |
| **VoiceActor** | `agents/voice_actor.py` | Azure Speech → edge-tts → silent-WAV fallback chain; voice locking on first success |
| **VisualDesigner** | `agents/visual_designer.py` | 9:16 frame design with NBA-styled layout |
| **VideoEditor** | `agents/video_editor.py` | MoviePy compositor (compatibility path) |
| **QualityEvaluator** | `agents/quality_evaluator.py` | 6-dimension scoring rubric (A/B/C/D/F) |
| **ScriptWriter** | `agents/script_writer.py` | Versioned narration generation, anti-fabrication prompt |
| **MusicSearcher / MusicProvider** | `agents/music_*.py` | Local BGM library → YouTube via yt-dlp → synthesized fallback |
| **Producer** | `agents/producer.py` | (Daily-report path only) News selection |

---

## AI Backends

| Backend | Model | Used for | Source |
|---|---|---|---|
| **Claude** (default) | `claude-opus-4.7-1m-internal` (configurable) | Text generation + frame-by-frame analysis | `ai_assistant.py:1103-1148` |
| **GPT** (swappable) | `gpt-5.4-mini` (Azure OpenAI, configurable) | Same as Claude when `AI_BACKEND=gpt` | `ai_assistant.py:1151-1196` |
| **Gemini** | `gemini-2.5-flash-lite` | Direct video ingestion: long-video trim plan, highlight detection, full-video review | `ai_assistant.py:43-100` |
| **Whisper** | `faster-whisper` (`small`/`base`/`medium`) | Local audio transcription, feeds Claude for subtitle alignment | `ai_assistant.py:177-224` |

Backend selection: `AI_BACKEND=claude|gpt`. Per-request override supported via API.

---

## Anti-Hallucination Design

Hallucination is the central problem when a 20-character tweet has to become a 60-second narration. Three layers of defense:

**1. Inject facts before generation**
ResearchAgent (`research_agent.py`) runs Google search via SerpAPI, then a second LLM pass extracts structured facts with explicit `confidence`:

```json
{
  "subject": "Brandon Clarke (Grizzlies forward)",
  "identity": "...",
  "when": "...",
  "what": "...",
  "confidence": "high|medium|low",
  "warnings": "..."
}
```

When `confidence: "low"`, the brief returned to ScriptWriter explicitly says *"research confidence is low, do not introduce additional background"* — so the LLM stays conservative instead of inventing.

**2. ScriptWriter prompt forbids fabrication**
Narration prompts include hard rules that facts must come from the brief; no inventing relationships, ages, or events.

**3. Cross-validate after generation**
Dual-agent review (`ai_assistant.py:882-1100`):
- Gemini ingests the full video file → judges content/voice/music
- Claude sees 8 sampled frames + Whisper transcription → catches subtitle vs narration mismatches
Both produce a 6-dim score; content accuracy is a hard-fail dimension.

---

## Quick Start

### Prerequisites
- Python 3.10+
- ffmpeg (bundled via `imageio-ffmpeg`)
- yt-dlp (optional, for YouTube BGM)
- Chinese fonts on the host (paths configurable, see Environment Variables)

### Install
```bash
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install yt-dlp                  # optional, for real BGM downloads
pip install fastapi uvicorn python-multipart  # optional, for HTTP service
```

### Environment Variables

**AI backends**
```bash
AI_BACKEND=claude                   # claude (default) | gpt

# Claude endpoint (internal proxy by default)
CLAUDE_API_ENDPOINT=http://localhost:23333/api/anthropic/v1/messages

# GPT (Azure OpenAI)
AZURE_OPENAI_API_KEY=...
AZURE_OPENAI_ENDPOINT=...
AZURE_OPENAI_MODEL=gpt-5.4-mini
AZURE_OPENAI_API_VERSION=...

# Gemini (for video analysis)
GEMINI_API_KEY=...

# Whisper
WHISPER_MODEL_SIZE=small            # small | base | medium
```

**TTS**
```bash
AZURE_SPEECH_KEY=...                # enables Azure Speech (preferred)
AZURE_SPEECH_REGION=...
HTTPS_PROXY=...                     # optional, for edge-tts in restricted regions
```

**Research**
```bash
SERPAPI_KEY=...                     # enables ResearchAgent; without it, the pipeline skips research
```

**Rendering / assets**
```bash
USE_FFMPEG_RENDER=1                 # 1 (default) = filter_complex fast path; 0 = MoviePy
KEEP_RENDER_ARTIFACTS=0             # 1 keeps intermediate PNG/WAV for debugging
FONT_PATH_BOLD=...
FONT_PATH_REGULAR=...
FONT_PATH_LIGHT=...
NBAVEDIO_DEFAULT_JSON=...           # default tweet input file
TWEET_TEST_IMAGE=...                # default cover image for testing
```

### Run

```bash
# Single tweet → video
python tweet_pipeline.py

# Batch
python batch_generate.py

# Full daily report pipeline
python main.py

# HTTP service
uvicorn tweet_api:app --reload
```

---

## HTTP API (tweet_api.py)

| Route | Method | Purpose |
|---|---|---|
| `/generate` | POST | Synchronous video generation (no AI review iteration) |
| `/generate-ai` | POST | Full pipeline with research + iterative review |
| `/cancel` | POST | Force-kill the running subprocess, rebuild executor |
| `/status` | GET | Current active request id, busy flag |
| `/logs` | GET | Streamed log tail (in-memory ring buffer + file) |
| `/health` | GET | Liveness |
| `/backends` | GET | Available AI backends |
| `/video/{filename}` | GET | Download generated video |

**Concurrency**: `ProcessPoolExecutor(max_workers=1, mp_context="spawn")`. One job at a time per process. The `lifespan` hook builds the pool on startup and force-kills children on shutdown.

**Logging**: `LogCapture` hijacks `sys.stdout/stderr`; every `print()` is mirrored to an in-memory ring buffer and a log file, with auto-classified levels (`info`/`warn`/`success`/`error`). Yt-dlp progress lines are deduped except for 100%.

---

## Output

- **Container/codec**: MP4, H.264 + AAC, 160 kbps audio
- **Resolution**: 1080×1920 (9:16)
- **Frame rate**: 24 fps
- **Duration**: 12–18 s (auto-adjusted to source video length or narration length)
- **Audio mix**: TTS foreground + BGM at low volume + optional highlight original-audio segments
- **Size**: ≤ 10 MB target (encoder params tuned for this constraint)

---

## Input

```json
{
  "tweet_id": "2042968119057031218",
  "player_name": "Shams Charania",
  "player_handle": "ShamsCharania",
  "content": "Original English tweet text",
  "content_cn": "中文翻译（可选，会被翻译优化步骤覆盖）",
  "tweet_type": "original"
}
```

Tweet screenshot images expected at `{covers_dir}/{tweet_id}.jpg`.

---

## Project Structure

```
├── main.py                    # NBA daily-report pipeline
├── tweet_pipeline.py          # Single tweet → video (thin wrapper over pipeline_core)
├── batch_generate.py          # Batch run helper
├── tweet_api.py               # FastAPI service
├── config.py                  # Global config + env loading
├── poc_ffmpeg_render.py       # Standalone POC for filter_complex path
├── requirements.txt
├── agents/
│   ├── pipeline_core.py       # 8-step orchestrator (the heart)
│   ├── ai_assistant.py        # Claude/GPT/Gemini/Whisper wrappers
│   ├── research_agent.py      # SerpAPI + LLM fact extraction + quota
│   ├── tweet_video_agent.py   # Vertical video assembly + GPU probing
│   ├── ffmpeg_renderer.py     # Fast-path filter_complex composition
│   ├── voice_actor.py         # TTS three-layer fallback
│   ├── visual_designer.py
│   ├── video_editor.py        # MoviePy compatibility path
│   ├── quality_evaluator.py   # Scoring rubric
│   ├── script_writer.py
│   ├── style_guide.py
│   ├── music_searcher.py      # YouTube via yt-dlp
│   ├── music_provider.py      # Synthesized fallback
│   └── producer.py            # Daily-report news selection
├── assets/
│   ├── backgrounds/
│   ├── fonts/
│   └── bgm/                   # Local BGM library
└── output/
    ├── tweet_videos/          # Final mp4s
    └── research_cache/        # SerpAPI snapshot per tweet_id
```

---

## Notes

- **Windows GBK consoles**: `ensure_utf8_console()` is invoked globally so emoji and Chinese characters don't crash `print()`.
- **Voice locking**: on the first successful TTS call, the chosen voice is locked for the rest of the video to keep tone consistent.
- **Research cache**: keyed by `tweet_id`; orphan cleanup on startup removes entries no longer present in `tweets.json`.
- **Long videos** (>50 s source): Gemini returns a JSON trim plan `[{start, end}, ...]`; ffmpeg executes the cuts before composition.
