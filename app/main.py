from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from typing import List
import importlib.metadata

# youtube-transcript-api 1.2.x 기준: 함수들을 직접 import
from youtube_transcript_api import (
    get_transcript,
    list_transcripts,
    TranscriptsDisabled,
    NoTranscriptFound,
    VideoUnavailable,
)

app = FastAPI(title="YouTube Captions Proxy")

# 개발 단계에서는 * 허용, 운영에서는 실제 도메인으로 제한 권장
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root():
    """헬스체크"""
    return {"ok": True}


# ---------- 유틸 ----------
def _format_ts(sec: float) -> str:
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    ms = int(round((sec - int(sec)) * 1000))
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def to_srt(items: List[dict]) -> str:
    """[{text,start,duration}, ...] -> SRT 문자열"""
    lines = []
    for i, it in enumerate(items, start=1):
        start = it.get("start", 0.0)
        dur = it.get("duration", 0.0)
        end = start + dur
        text = (it.get("text", "") or "").replace("\n", " ").strip() or " "
        lines.append(str(i))
        lines.append(f"{_format_ts(start)} --> {_format_ts(end)}")
        lines.append(text)
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def check_scraping_block(exc: Exception) -> bool:
    """
    유튜브 차단(추정) 신호 간단 감지: 429/403 키워드 기반
    """
    msg = str(exc).lower()
    if "429" in msg or "too many requests" in msg:
        return True
    if "403" in msg or "forbidden" in msg:
        return True
    return False


def _detail(user_msg: str, exc: Exception | None, debug: bool, scrapingBlocked: bool) -> str:
    base = user_msg
    if scrapingBlocked:
        base += " (scrapingBlocked=True)"
    if debug and exc is not None:
        base += f" | {type(exc).__name__}: {exc!s}"
    return base


# ---------- API ----------
@app.get("/v1/transcript")
def api_transcript(
    videoId: str = Query(..., description="YouTube video id"),
    lang: str = Query("en", description="BCP-47 코드, 여러 개는 콤마로(예: en,ko)"),
    format: str = Query("json", pattern="^(json|srt)$"),
    prefer: str = Query("any", pattern="^(any|manual|generated)$",
                        description="any | manual(업로더 자막) | generated(자동 자막)"),
    allowTranslate: bool = Query(True, description="요청 언어가 없으면 번역 폴백 허용"),
    debug: bool = Query(False, description="에러 발생 시 상세 메시지 노출"),
):
    langs = [x.strip() for x in lang.split(",") if x.strip()]
    scrapingBlocked = False

    # 1) 요청 언어로 직접 시도
    try:
        items = get_transcript(videoId, languages=langs)
        if format == "json":
            return {"videoId": videoId, "lang": langs[0], "items": items, "scrapingBlocked": scrapingBlocked}
        else:
            return Response(content=to_srt(items), media_type="text/plain; charset=utf-8")

    except NoTranscriptFound:
        # 2) 목록 열람 후 수동/자동 우선순위 선택
        try:
            tl = list_transcripts(videoId)  # Transcripts 객체
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
        # 기타 예외: 차단 추정 시 429로 매핑
        scrapingBlocked = check_scraping_block(e)
        if scrapingBlocked:
            raise HTTPException(status_code=429, detail=_detail("Rate limited or blocked by YouTube.", e, debug, scrapingBlocked))
        raise HTTPException(status_code=500, detail=_detail("Internal server error.", e, debug, scrapingBlocked))


@app.get("/v1/diag")
def api_diag():
    """
    youtube-transcript-api 버전 및 샘플 호출 결과 확인
    - 설치 버전: yta_version
    - 샘플 영상 호출 결과: ok / error / scrapingBlocked
    """
    try:
        ver = importlib.metadata.version("youtube-transcript-api")
    except Exception:
        ver = "UNKNOWN"

    test_id = "5MgBikgcWnY"  # CrashCourse (자막 있음으로 알려진 영상)
    try:
        items = get_transcript(test_id, languages=["en"])
        return {"ok": True, "yta_version": ver, "sample_items": len(items)}
    except Exception as e:
        blocked = check_scraping_block(e)
        return {
            "ok": False,
            "yta_version": ver,
            "error": f"{type(e).__name__}: {e}",
            "scrapingBlocked": blocked,
        }
