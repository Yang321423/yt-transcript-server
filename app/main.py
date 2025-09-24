from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from typing import List
import math

from youtube_transcript_api import (
    YouTubeTranscriptApi,
    TranscriptsDisabled,
    NoTranscriptFound,
    VideoUnavailable,
)

app = FastAPI(title="YouTube Captions Proxy")

# CORS: allow all during development; restrict to specific origins in production.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def root():
    return {"ok": True}

def _format_ts(sec: float) -> str:
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    ms = int(round((sec - int(sec)) * 1000))
    return f"{h:02}:{m:02}:{s:02},{ms:03}"

def to_srt(items: List[dict]) -> str:
    lines = []
    for i, it in enumerate(items, start=1):
        start = it.get("start", 0.0)
        dur = it.get("duration", 0.0)
        end = start + dur
        text = it.get("text", "").replace("\n", " ").strip() or " "
        lines.append(str(i))
        lines.append(f"{_format_ts(start)} --> {_format_ts(end)}")
        lines.append(text)
        lines.append("")
    return "\n".join(lines).strip() + "\n"

@app.get("/v1/transcript")
def get_transcript(
    videoId: str = Query(..., description="YouTube video id"),
    lang: str = Query("en", description="BCP-47 code (e.g., en, ko, ja). Multiple with comma"),
    format: str = Query("json", pattern="^(json|srt)$"),
    prefer: str = Query("any", pattern="^(any|manual|generated)$",
                        description="Priority: any | manual (uploader captions) | generated (auto)"),
    allowTranslate: bool = Query(True, description="Translate if requested lang not found"),
):
    langs = [x.strip() for x in lang.split(",") if x.strip()]

    try:
        # 1) Try to get transcript directly in requested languages
        items = YouTubeTranscriptApi.get_transcript(videoId, languages=langs)
        if format == "json":
            return {"videoId": videoId, "lang": langs[0], "items": items}
        else:
            return Response(content=to_srt(items), media_type="text/plain; charset=utf-8")

    except NoTranscriptFound:
        # 2) Fallback: inspect transcript list and honor preference
        try:
            tl = YouTubeTranscriptApi.list_transcripts(videoId)
        except TranscriptsDisabled:
            raise HTTPException(status_code=404, detail="Transcripts disabled for this video.")
        except VideoUnavailable:
            raise HTTPException(status_code=404, detail="Video unavailable.")
        except Exception as e:
            msg = str(e)
            if "429" in msg or "TooManyRequests" in msg:
                raise HTTPException(status_code=429, detail="Rate limited by YouTube. Try again later.")
            raise HTTPException(status_code=500, detail=msg)

        transcript = None

        def pick_transcript():
            if prefer in ("manual", "any"):
                try:
                    return tl.find_manually_created_transcript(langs)
                except Exception:
                    pass
            if prefer in ("generated", "any"):
                try:
                    return tl.find_generated_transcript(langs)
                except Exception:
                    pass
            return None

        transcript = pick_transcript()

        # 3) Translate fallback
        if not transcript and allowTranslate:
            try:
                manual = [t for t in tl if not t.is_generated]
                base = manual[0] if manual else list(tl)[0]
                transcript = base.translate(langs[0])
            except Exception:
                pass

        if not transcript:
            raise HTTPException(status_code=404, detail="No transcript in requested languages.")

        items = transcript.fetch()
        if format == "json":
            return {
                "videoId": videoId,
                "lang": langs[0],
                "isTranslated": getattr(transcript, "language_code", "") != langs[0],
                "isGenerated": getattr(transcript, "is_generated", False),
                "items": items,
            }
        else:
            return Response(content=to_srt(items), media_type="text/plain; charset=utf-8")

    except TranscriptsDisabled:
        raise HTTPException(status_code=404, detail="Transcripts disabled for this video.")
    except VideoUnavailable:
        raise HTTPException(status_code=404, detail="Video unavailable.")
    except Exception as e:
        # Treat 429 or textual "TooManyRequests" as rate limit
        msg = str(e)
        if "429" in msg or "TooManyRequests" in msg:
            raise HTTPException(status_code=429, detail="Rate limited by YouTube. Try again later.")
        raise HTTPException(status_code=500, detail=msg)
