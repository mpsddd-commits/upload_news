"""렌더링된 숏츠를 유튜브에 **비공개(private)** 로 업로드한다.

공개 전환은 사람이 유튜브 스튜디오에서 직접 한다. 이 스크립트는 절대
privacyStatus를 public으로 올리지 않는다.

입력  : out/scripts.json, out/video/*.mp4
출력  : out/uploaded.json, state/posted.json 갱신
쿼터  : 영상 1건당 1,600유닛 (+ 썸네일 50유닛)
"""

from __future__ import annotations

import json
import pathlib
import random
import sys
import time
from datetime import datetime, timezone

from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from src.yt_auth import build_youtube

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS_JSON = ROOT / "out" / "scripts.json"
UPLOADED_JSON = ROOT / "out" / "uploaded.json"
POSTED_JSON = ROOT / "state" / "posted.json"

# 하루 기본 쿼터 10,000유닛. 사고로 다 태우는 걸 막는 안전장치.
MAX_UPLOADS_PER_RUN = 5

CATEGORY_NEWS_POLITICS = "25"
CHUNK_SIZE = 4 * 1024 * 1024  # 4MB 단위 resumable 업로드

RETRIABLE_STATUS = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 5


# ── 메타데이터 정리 ────────────────────────────────────────────

def _clean_title(raw: str) -> str:
    """유튜브 제목 제한: 100자, < > 사용 불가."""
    title = raw.replace("<", "(").replace(">", ")").strip()
    if "#Shorts" not in title and "#shorts" not in title:
        # 숏츠 분류를 확실히 하기 위해 붙인다. 길이 초과 시 제목을 자른다.
        suffix = " #Shorts"
        title = title[: 100 - len(suffix)].rstrip() + suffix
    return title[:100]


def _clean_description(raw: str, source_name: str, source_url: str) -> str:
    parts = [raw.strip(), "", f"출처: {source_name}", source_url, "", "#Shorts #뉴스"]
    return "\n".join(parts)[:5000]


def _clean_tags(tags: list[str]) -> list[str]:
    """태그 전체 길이 합이 500자를 넘으면 유튜브가 요청 자체를 거부한다."""
    out, total = [], 0
    for tag in tags:
        tag = tag.strip()[:30]
        if not tag:
            continue
        if total + len(tag) + 1 > 480:
            break
        out.append(tag)
        total += len(tag) + 1
    return out


def _build_body(item: dict) -> dict:
    return {
        "snippet": {
            "title": _clean_title(item["title"]),
            "description": _clean_description(
                item.get("description", ""),
                item.get("source_name", ""),
                item.get("source_url", ""),
            ),
            "tags": _clean_tags(item.get("tags", [])),
            "categoryId": CATEGORY_NEWS_POLITICS,
            "defaultLanguage": "ko",
            "defaultAudioLanguage": "ko",
        },
        "status": {
            # 여기가 이 파이프라인의 핵심. 절대 "public"으로 바꾸지 말 것.
            "privacyStatus": "private",
            # 아동용 여부는 명시적 선언이 필수다. 뉴스 채널이므로 False.
            "selfDeclaredMadeForKids": False,
            # 비공개 업로드라 알림은 안 가지만, 나중 혼선을 막기 위해 꺼둔다.
            "notifySubscribers": False,
        },
    }


# ── 업로드 ────────────────────────────────────────────────────

def _upload_one(youtube, item: dict) -> str:
    video_path = ROOT / item["video_path"]
    if not video_path.exists():
        raise FileNotFoundError(f"영상 파일 없음: {video_path}")

    media = MediaFileUpload(
        str(video_path),
        chunksize=CHUNK_SIZE,
        resumable=True,
        mimetype="video/mp4",
    )
    request = youtube.videos().insert(
        part="snippet,status",
        body=_build_body(item),
        media_body=media,
    )

    response, attempt = None, 0
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                print(f"    {int(status.progress() * 100):3d}%", flush=True)
            attempt = 0
        except HttpError as exc:
            if exc.resp.status not in RETRIABLE_STATUS:
                raise
            attempt += 1
            if attempt >= MAX_ATTEMPTS:
                raise
            # resumable 업로드라 재시도해도 처음부터 다시 올리지 않는다.
            wait = min(2**attempt, 30) + random.random()
            print(f"    HTTP {exc.resp.status} — {wait:.1f}초 후 재시도 ({attempt})")
            time.sleep(wait)
        except (ConnectionError, OSError) as exc:
            attempt += 1
            if attempt >= MAX_ATTEMPTS:
                raise
            wait = min(2**attempt, 30) + random.random()
            print(f"    네트워크 오류 {exc} — {wait:.1f}초 후 재시도 ({attempt})")
            time.sleep(wait)

    return response["id"]


def _set_thumbnail(youtube, video_id: str, path: pathlib.Path) -> None:
    try:
        youtube.thumbnails().set(videoId=video_id, media_body=str(path)).execute()
        print("    썸네일 설정 완료")
    except HttpError as exc:
        # 썸네일 실패로 전체를 죽일 이유는 없다. 채널 인증 전에는 403이 난다.
        print(f"    썸네일 설정 실패 (무시): HTTP {exc.resp.status}")


# ── 이력 관리 ──────────────────────────────────────────────────

def _load_posted() -> dict:
    if POSTED_JSON.exists():
        return json.loads(POSTED_JSON.read_text(encoding="utf-8") or "{}")
    return {}


def _save_posted(posted: dict) -> None:
    POSTED_JSON.parent.mkdir(parents=True, exist_ok=True)
    POSTED_JSON.write_text(
        json.dumps(posted, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


# ── 진입점 ────────────────────────────────────────────────────

def main() -> int:
    if not SCRIPTS_JSON.exists():
        print(f"[!] {SCRIPTS_JSON} 가 없습니다. 앞 단계가 실패했는지 확인하세요.")
        return 1

    items = json.loads(SCRIPTS_JSON.read_text(encoding="utf-8"))
    posted = _load_posted()

    # 렌더가 실패한 건은 video_path 가 없다. render.py 는 부분 실패를 허용하므로
    # 실제로 생기는 상황이다 (CLAUDE.md 6장).
    # 거르지 않으면 두 가지가 터진다.
    #   1) _upload_one 이 KeyError: 'video_path' 로 죽는다. 로그만 봐서는
    #      렌더가 실패했다는 사실을 알 수 없다.
    #   2) 더 나쁜 건, 그 항목이 MAX_UPLOADS_PER_RUN 슬롯을 차지해서
    #      정상 렌더된 뒷순위 기사를 밀어낸다.
    renderable = [i for i in items if i.get("video_path")]
    unrendered = len(items) - len(renderable)
    if unrendered:
        print(f"렌더 결과가 없는 {unrendered}건은 건너뜁니다 (video_path 없음).")

    already = sum(1 for i in renderable if i["id"] in posted)
    if already:
        print(f"이미 업로드된 {already}건은 건너뜁니다.")

    todo = [i for i in renderable if i["id"] not in posted][:MAX_UPLOADS_PER_RUN]
    if not todo:
        print("업로드할 항목이 없습니다.")
        UPLOADED_JSON.write_text("[]\n", encoding="utf-8")
        return 0

    print(f"업로드 {len(todo)}건 시작 (예상 쿼터 {len(todo) * 1600:,}유닛)\n")
    youtube = build_youtube()

    results, failures = [], []
    for idx, item in enumerate(todo, 1):
        print(f"[{idx}/{len(todo)}] {item['title'][:40]}")
        try:
            video_id = _upload_one(youtube, item)
        except Exception as exc:  # noqa: BLE001
            print(f"    실패: {exc}")
            failures.append({"id": item["id"], "error": str(exc)})
            continue

        thumb = item.get("thumbnail_path")
        if thumb and (ROOT / thumb).exists():
            _set_thumbnail(youtube, video_id, ROOT / thumb)

        record = {
            "id": item["id"],
            "video_id": video_id,
            "title": item["title"],
            "source_name": item.get("source_name", ""),
            "source_url": item.get("source_url", ""),
            "watch_url": f"https://www.youtube.com/watch?v={video_id}",
            "studio_url": f"https://studio.youtube.com/video/{video_id}/edit",
            "uploaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "privacy": "private",
        }
        results.append(record)
        posted[item["id"]] = record
        # 중간에 죽어도 이미 올라간 건 다시 안 올리도록 매 건마다 저장한다.
        _save_posted(posted)
        print(f"    완료 → {record['studio_url']}\n")

    UPLOADED_JSON.parent.mkdir(parents=True, exist_ok=True)
    UPLOADED_JSON.write_text(
        json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print(f"성공 {len(results)}건, 실패 {len(failures)}건")
    if failures and not results:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
