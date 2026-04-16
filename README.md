# NBA Tweet Video Generator

AI-powered system that transforms NBA player tweets into short-form vertical videos (9:16) with Chinese narration, dynamic subtitles, and mood-matched background music.

## Features

- **AI Commentary** — GPT generates natural Chinese narration instead of plain translation
- **Text-to-Speech** — Edge TTS with multi-voice fallback (Yunyang → Yunjian → Xiaoxiao)
- **Music Matching** — Claude/GPT recommends songs by tweet mood; downloads from YouTube via yt-dlp
- **Iterative Review** — AI scores each video and iterates to improve quality (target: A grade)
- **Batch Processing** — Generate 15 curated tweet videos in one run

## Architecture

```
┌────────────┐    ┌──────────────┐    ┌────────────────┐
│ AIAssistant│───>│ TweetVideoAgt│───>│   VoiceActor   │
│ (GPT/Claude)   │ (orchestrator)    │ (edge-tts)     │
└────────────┘    └──────┬───────┘    └────────────────┘
                         │
              ┌──────────┼──────────┐
              v          v          v
        MusicSearcher  MusicProvider  Pillow Renderer
        (yt-dlp)       (synth fallback)  (9:16 frames)
```

### Agents

| Agent | File | Role |
|-------|------|------|
| **AIAssistant** | `agents/ai_assistant.py` | Azure OpenAI GPT: translation polish, commentary, music & mood recommendation, video review |
| **TweetVideoAgent** | `agents/tweet_video_agent.py` | Orchestrates vertical video creation: frame → subtitle → TTS → music → composite |
| **VoiceActor** | `agents/voice_actor.py` | Edge TTS synthesis with 3-voice fallback, rate/pitch control, MP3 validation |
| **MusicSearcher** | `agents/music_searcher.py` | YouTube song search & download via yt-dlp, chorus extraction via ffmpeg |
| **MusicProvider** | `agents/music_provider.py` | Synthesized ambient music fallback (sine waves, lo-fi chords) |
| **Producer** | `agents/producer.py` | News selection & show planning for the NBA daily report pipeline |
| **ScriptWriter** | `agents/script_writer.py` | Narration script generation (versioned) |
| **VisualDesigner** | `agents/visual_designer.py` | NBA-styled frame generation with team colors |
| **VideoEditor** | `agents/video_editor.py` | MoviePy video composition with subtitles |
| **QualityEvaluator** | `agents/quality_evaluator.py` | Multi-dimensional quality scoring (A–F grades) |

## Quick Start

### Prerequisites

- Python 3.10+
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) (for music downloads)
- ffmpeg (bundled with moviepy/imageio-ffmpeg)
- Windows (uses system Chinese fonts)

### Setup

```bash
python -m venv .venv
.venv\Scripts\Activate.ps1

pip install -r requirements.txt
pip install yt-dlp        # optional: real music downloads
```

### Environment Variables

```powershell
$env:AZURE_OPENAI_API_KEY = "your-azure-openai-key"

# Optional overrides:
$env:AZURE_OPENAI_ENDPOINT = "https://your-resource.openai.azure.com/openai/responses"
$env:AZURE_OPENAI_MODEL = "gpt-5.4-mini"
```

### Generate Tweet Videos

**Single tweet:**
```bash
python tweet_pipeline.py
```

**Batch (15 curated tweets):**
```bash
python batch_generate.py
```

**NBA daily report (6-agent pipeline):**
```bash
python main.py
```

**FastAPI service:**
```bash
pip install fastapi uvicorn python-multipart
uvicorn tweet_api:app --reload
```

## Project Structure

```
├── main.py                 # NBA daily report pipeline (6-agent orchestrator)
├── tweet_pipeline.py       # Single tweet video with AI enhancement
├── batch_generate.py       # Batch 15-tweet video generation
├── tweet_api.py            # FastAPI REST service
├── config.py               # Global configuration
├── requirements.txt        # Python dependencies
├── agents/
│   ├── ai_assistant.py     # Azure OpenAI GPT integration
│   ├── tweet_video_agent.py# Vertical video generator
│   ├── voice_actor.py      # Edge TTS with fallback
│   ├── music_searcher.py   # YouTube music download
│   ├── music_provider.py   # Synthesized music fallback
│   ├── producer.py         # News selection
│   ├── script_writer.py    # Script generation
│   ├── visual_designer.py  # Frame rendering
│   ├── video_editor.py     # Video composition
│   └── quality_evaluator.py# Quality scoring
├── assets/
│   ├── backgrounds/        # Background images
│   └── fonts/              # Custom fonts
└── output/
    └── tweet_videos/       # Generated videos
```

## Input Data

Tweet data is loaded from JSON files with this structure:

```json
{
  "tweet_id": "2042968119057031218",
  "player_name": "Shams Charania",
  "player_handle": "ShamsCharania",
  "content": "Original tweet text",
  "content_cn": "中文翻译",
  "tweet_type": "original"
}
```

Tweet screenshot images (`.jpg`) are expected in a covers directory, named by `{tweet_id}.jpg`.

## Output

- **Format:** MP4, H.264 + AAC
- **Resolution:** 1080×1920 (9:16 vertical)
- **FPS:** 24
- **Duration:** 12–18s (auto-adjusted to narration length)
- **Audio layers:** TTS narration (foreground) + BGM at 20% volume (background)
