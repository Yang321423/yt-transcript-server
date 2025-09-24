from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from typing import List
import importlib.metadata

from youtube_transcript_api import (
    YouTubeTranscriptApi,
    TranscriptsDisabled,
    NoTranscriptFound,
    VideoUnavailable,
)

app = FastAPI(title="YouTube Captions Proxy")

# 개발 단계에서는 모든 origin 허용 (실서비스에서는 특정 도메인으로 제한 권장)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root():
    """서버 상태 확인"""
    return {"ok": True}


# ---------- 유틸 함수들 ----------
def _format_ts(sec: float) -> str:
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    ms = int(round((sec - int(sec)) * 1000))
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def to_srt(items: List[dict]) -> str:
    """자막 리스트를 SRT 문자열로 변환"""
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


def check_scraping_block(exc: Exception) -> bool:
    """429 / 403 에러 메시지 기반으로 차단 여부 추정"""
    msg = str(exc).lower()
    if "429" in msg or "too many requests" in msg:
        return True
    if "forbidden" in msg or "403" in msg:
        return True
    return False


def _detail(user_msg: str, exc: Exception | None, debug: bool, scrapingBlocked: bool) -> str:
    base = user_msg
    if scrapingBlocked:
        base += " (scrapingBlocked=True)"
    if debug and exc is not None:
        base += f" | {type(exc).__name__}: {exc!s}"
    return base


# ---------- 실제 API 엔드포인트 ----------
@app.get("/v1/transcript")
def get_transcript(
    videoId: str = Query(..., description="YouTube video id"),
    lang: str = Query("en", description="BCP-47 code (예: en, ko, ja). 여러개는 쉼표로 구분"),
    format: str = Query("json", pattern="^(json|srt)$"),
    prefer: str = Query("any", pattern="^(any|manual|generated)$",
                        description="any | manual (업로더 자막) | generated (자동자막)"),
    allowTranslate: bool = Query(True, description="요청 언어가 없을 때 자동 번역 허용 여부"),
    debug: bool = Query(False, description="에러 발생 시 상세 메시지 포함 여부"),
):
    langs = [x.strip() for x in lang.split(",") if x.strip()]
    scrapingBlocked = False

    try:
        # 1) 요청 언어로 직접 자막 시도
        items = YouTubeTranscriptApi.get_transcript(videoId, languages=langs)
        if format == "json":
            return {"videoId": videoId, "lang": langs[0], "items": items, "scrapingBlocked": scrapingBlocked}
        else:
            return Response(content=to_srt(items), media_type="text/plain; charset=utf-8")

    except NoTranscriptFound:
        # 2) 자막 리스트 불러오기 → 수동/자동 선택
        try:
            tl = YouTubeTranscriptApi.list_transcripts(videoId)
        except (TranscriptsDisabled, VideoUnavailable) as e:
            scrapingBlocked = check_scraping_block(e)
            raise HTTPException(status_code=404, detail=_detail("Transcript unavailable.", e, debug, scrapingBlocked))
        except Exception as e:
            scrapingBlocked = check_scraping_block(e)
            raise HTTPException(status_code=500, detail=_detail("Internal error while listing transcripts.", e, debug, scrapingBlocked))

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

        # 3) 번역 폴백
        if not transcript and allowTranslate:
            try:
                manual = [t for t in tl if not t.is_generated]
                base = manual[0] if manual else list(tl)[0]
                transcript = base.translate(langs[0])
            except Exception:
                pass

        if not transcript:
            raise HTTPException(status_code=404, detail=_detail("No transcript in requested languages.", None, debug, scrapingBlocked))

        items = transcript.fetch()
        if format == "json":
            return {
                "videoId": videoId,
                "lang": langs[0],
                "isTranslated": getattr(transcript, "language_code", "") != langs[0],
                "isGenerated": getattr(transcript, "is_generated", False),
                "items": items,
                "scrapingBlocked": scrapingBlocked,
            }
        else:
            return Response(content=to_srt(items), media_type="text/plain; charset=utf-8")

    except Exception as e:
        scrapingBlocked = check_scraping_block(e)
        if scrapingBlocked:
            raise HTTPException(status_code=429, detail=_detail("Rate limited or blocked by YouTube.", e, debug, scrapingBlocked))
        raise HTTPException(status_code=500, detail=_detail("Internal server error.", e, debug, scrapingBlocked))


@app.get("/v1/diag")
def diag():
    """youtube-transcript-api 버전 및 샘플 호출 결과 확인"""
    try:
        ver = importlib.metadata.version("youtube-transcript-api")
    except Exception:
        ver = "UNKNOWN"

    test_id = "5MgBikgcWnY"  # CrashCourse (자막 있음)
    try:
        items = YouTubeTranscriptApi.get_transcript(test_id, languages=["en"])
        return {"ok": True, "yta_version": ver, "sample_items": len(items)}
    except Exception as e:
        blocked = check_scraping_block(e)
        return {
            "ok": False,
            "yta_version": ver,
            "error": f"{type(e).__name__}: {e}",
            "scrapingBlocked": blocked
        }
