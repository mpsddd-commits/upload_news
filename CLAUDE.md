# CLAUDE.md

이 파일은 Claude Code가 세션 시작 시 자동으로 읽는 프로젝트 컨텍스트입니다.
작업 전에 반드시 이 문서 전체를 파악하고, 아래 제약을 위반하지 마세요.

---

## 1. 프로젝트 목적

매일 정해진 시각에 뉴스 RSS를 수집해 유튜브 숏츠 **초안**을 만들고,
유튜브에 **비공개(private)로 업로드**한 뒤 검수 요청 이슈를 생성하는 파이프라인.

**공개 전환은 반드시 사람이 유튜브 스튜디오에서 수동으로 한다.**
이건 편의상의 선택이 아니라 정책 대응이다. 유튜브 채널 수익 창출 정책은
"대량 생산되거나 반복적인 콘텐츠"를 수익화 대상에서 제외하며,
2025년 7월 정책 명확화 이후 "유사한 포맷과 AI 보이스를 반복 사용하는 뉴스 요약 영상"이
비진정성(inauthentic) 콘텐츠의 대표 사례로 명시됐다.
완전 무인 업로드는 YPP 심사 반려 또는 수익화 취소로 직결된다.

---

## 2. 절대 하지 말 것

이 목록은 협상 불가다. 리팩터링·최적화·"더 편하게" 같은 이유로도 어기지 않는다.

| 금지 | 이유 |
|---|---|
| `privacyStatus`를 `"public"` / `"unlisted"` 로 설정 | 수동 검수 원칙 위반. 항상 `"private"` |
| 기사 본문을 그대로 복사해 대본에 사용 | 저작권 침해. RSS 제목·요약만 참고해 **완전히 새로 작성** |
| 언론사 사진·영상·로고를 영상에 삽입 | 저작권 침해 |
| API 키·토큰을 코드나 config에 하드코딩 | 유출 시 채널 탈취. 반드시 `os.environ` |
| `out/`, `.venv/`, `client_secret.json` 커밋 | `.gitignore` 확인 |
| `youtube.search.list` 호출 | 1회 100유닛. 쿼터 낭비, 이 프로젝트에 불필요 |
| `requirements.txt` 생성 | uv 프로젝트다. `uv add` 를 쓴다 |
| 한 실행에서 5건 초과 업로드 | `MAX_UPLOADS_PER_RUN` 상수를 지킨다 |

---

## 3. 실행 환경

- 파이썬 3.12, **uv** 로 의존성 관리 (`uv sync`, `uv add`, `uv run`)
- 실행은 항상 리포 루트에서 `uv run python -m src.<모듈명>`
- 운영 환경은 GitHub Actions `ubuntu-latest` (ffmpeg, fonts-nanum 설치됨)
- 상태 저장소는 `state/posted.json` 하나뿐. DB 없음, 서버 없음
- 웹 서버(uvicorn/FastAPI) 불필요. HTTP 요청을 받는 구성 요소가 없다

### 환경변수 (모두 GitHub Secrets)

대본 생성 제공자는 `config.yml` 의 `script_gen.provider` 로 고른다.
고른 쪽의 키만 있으면 되고, 나머지는 비워둬도 된다.

| 이름 | 용도 |
|---|---|
| `GEMINI_API_KEY` | 대본 생성 (`provider: gemini`, 무료 티어) |
| `ANTHROPIC_API_KEY` | 대본 생성 (`provider: anthropic`, 유료) |
| `YT_CLIENT_ID` | 유튜브 OAuth |
| `YT_CLIENT_SECRET` | 유튜브 OAuth |
| `YT_REFRESH_TOKEN` | 유튜브 OAuth |
| `GH_TOKEN` | Actions가 자동 주입 (`secrets.GITHUB_TOKEN`) |

---

## 4. 디렉터리 구조

```
.github/workflows/daily.yml   스케줄러 (완료)
src/
  __init__.py                 빈 파일. python -m 을 위해 필수
  yt_auth.py                  유튜브 OAuth 공용        (완료)
  upload.py                   비공개 업로드            (완료)
  collect.py                  1) RSS 수집
  script_gen.py               2) 대본 생성
  tts.py                      3) 음성 합성
  render.py                   4) 영상 렌더링
  notify.py                   6) 검수 이슈 생성
scripts/
  get_refresh_token.py        로컬 1회 실행            (완료)
  check_auth.py               로컬 검증                (완료)
assets/{fonts,bg,bgm}/        렌더링 리소스
state/posted.json             업로드 이력 (커밋 대상)
out/                          산출물 (gitignore)
config.yml                    피드 목록·렌더 설정
```

---

## 5. 데이터 계약

모듈 간 인터페이스는 JSON 파일이다. **스키마를 임의로 바꾸지 말 것.**
바꿔야 한다면 소비하는 모듈까지 함께 수정하고 이 문서도 갱신한다.

### `out/articles.json` — collect.py 출력

```json
[
  {
    "id": "20260806-001",
    "title": "원문 기사 제목",
    "summary": "RSS 요약문 원문 (대본 생성 입력용, 영상에는 절대 그대로 쓰지 않음)",
    "source_name": "연합뉴스",
    "source_url": "https://...",
    "published_at": "2026-08-06T07:12:00+09:00",
    "cluster_size": 4
  }
]
```

`id` 규칙: `YYYYMMDD-NNN` (3자리 일련번호). 이게 전 파이프라인의 중복 방지 키다.
`cluster_size`는 몇 개 매체가 같은 사건을 보도했는지. 화제성 정렬에 쓴다.

### `out/scripts.json` — script_gen.py 출력, render/upload가 갱신

```json
[
  {
    "id": "20260806-001",
    "title": "유튜브 업로드용 제목 (100자 이하)",
    "description": "설명란 본문. 출처·해시태그는 upload.py가 자동으로 붙임",
    "tags": ["태그1", "태그2"],
    "script": "TTS로 읽을 대본 전문",
    "captions": ["화면 자막 1", "화면 자막 2"],
    "source_name": "연합뉴스",
    "source_url": "https://...",
    "audio_path": "out/audio/20260806-001.mp3",
    "video_path": "out/video/20260806-001.mp4",
    "thumbnail_path": "out/thumb/20260806-001.jpg"
  }
]
```

- `audio_path`는 tts.py가, `video_path`는 render.py가 채워 넣는다 (같은 파일을 읽어서 갱신)
- 경로는 **리포 루트 기준 상대경로** 문자열

### `state/posted.json` — upload.py가 갱신, 유일한 커밋 대상 상태 파일

```json
{
  "20260806-001": {
    "id": "20260806-001",
    "video_id": "dQw4w9WgXcQ",
    "title": "...",
    "source_url": "https://...",
    "watch_url": "https://www.youtube.com/watch?v=...",
    "studio_url": "https://studio.youtube.com/video/.../edit",
    "uploaded_at": "2026-08-06T07:30:00+00:00",
    "privacy": "private"
  }
}
```

collect.py는 이 파일의 `source_url` 집합을 읽어 이미 다룬 기사를 제외해야 한다.

### `out/uploaded.json` — upload.py 출력, notify.py 입력

이번 실행에서 새로 올라간 항목만 담긴 배열. 스키마는 `posted.json`의 값과 동일.

---

## 6. 코딩 컨벤션

- **주석과 사용자 대면 출력은 한국어.** 변수·함수명은 영어
- 모듈은 전부 `def main() -> int:` 진입점 + `if __name__ == "__main__": sys.exit(main())`
  성공 0, 실패 1. Actions가 종료코드로 파이프라인을 끊는다
- 경로는 항상 `ROOT = pathlib.Path(__file__).resolve().parents[1]` 기준.
  상대경로 `open("out/...")` 금지 (실행 위치에 따라 깨진다)
- `from __future__ import annotations` + 타입힌트 사용
- 외부 API 호출은 예외를 잡아 **재시도(지수 백오프)** 하되, 최대 시도 횟수를 둔다
- 부분 실패를 허용한다. 5건 중 2건이 실패해도 나머지 3건은 진행하고,
  마지막에 성공/실패 건수를 출력한다. 전건 실패일 때만 종료코드 1
- 진행 상황을 stdout에 한국어로 찍는다. Actions 로그가 유일한 디버깅 수단이다
- 새 의존성은 `uv add <pkg>` 로 추가하고 `uv.lock` 을 커밋한다

---

## 7. 쿼터 예산

YouTube Data API 기본 할당량은 프로젝트당 **하루 10,000유닛**.

| 작업 | 유닛 | 하루 사용량 |
|---|---|---|
| `videos.insert` | 1,600 | 5건 = 8,000 |
| `thumbnails.set` | 50 | 5건 = 250 |
| `channels.list` | 1 | 검증 시 1 |
| **합계** | | **약 8,251 / 10,000** |

여유가 250유닛 남짓뿐이다. 새 API 호출을 추가하기 전에 반드시 유닛 비용을 확인할 것.

### 대본 생성 비용

기본값은 **Gemini 무료 티어**다 (`config.yml` 의 `script_gen.provider: gemini`).
2.5 Flash 무료 한도는 10 RPM / 250 RPD 이고 이 파이프라인은 하루 5건이라 50배 여유가 있다.
단, 무료 티어의 입출력은 구글 제품 개선에 사용되고 한도는 예고 없이 줄어든다
(2025-12-07에 50~80% 삭감된 전례가 있다). 어느 쪽도 공개 기사 요약이라 문제는 없지만,
조용히 429로 죽는 날이 오면 provider를 바꾸면 된다.

유료로 돌릴 때 하루 5건(1건당 입력 ~1,000토큰 / 출력 ~800토큰) 기준:

| 모델 | 입력/출력 (100만 토큰) | 한 달 |
|---|---|---|
| `claude-sonnet-5` | $3 / $15 | 약 3,200원 |
| `claude-haiku-4-5` | $1 / $5 | 약 1,000원 |

Anthropic API에는 무료 티어가 없다. 선불 크레딧 종량제다.

---

## 8. 알려진 함정

작업 중 이 증상이 나오면 아래를 먼저 의심할 것.

- **refresh token이 7일 만에 만료** → GCP OAuth 동의 화면이 "테스트" 상태.
  "프로덕션"으로 게시해야 한다
- **재인증 시 `refresh_token`이 None** → `prompt="consent"` 를 빠뜨림
- **cron이 엉뚱한 시각에 실행** → GitHub Actions cron은 UTC 고정.
  `0 21 * * *` = KST 06:00. 그리고 정시 근처는 수십 분 지연될 수 있다
- **영상 자막이 네모(두부)로 깨짐** → 한글 폰트 미지정.
  ffmpeg `drawtext` 에 `fontfile=/usr/share/fonts/truetype/nanum/NanumGothic.ttf` 명시
- **업로드는 됐는데 숏츠 선반에 안 뜸** → 세로 9:16, 3분 이하, 제목/설명에 `#Shorts` 필요
- **썸네일 403** → 채널 전화 인증 미완료. 실패해도 파이프라인을 죽이지 말 것
- **커스텀 썸네일 없이도 동작해야 함** → `thumbnail_path` 는 선택 필드

---

## 9. 테스트 방법

전체 파이프라인을 유튜브 쿼터 없이 돌려보는 방법:

```bash
# 1) 렌더링까지만 (업로드·커밋·이슈 생성 건너뜀)
uv run python -m src.collect --count 5
uv run python -m src.script_gen
uv run python -m src.tts
uv run python -m src.render

# 2) 결과 확인
ls -la out/video/
ffprobe -v error -show_entries stream=width,height -show_entries format=duration \
        -of default=nw=1 out/video/*.mp4
```

Actions에서는 `workflow_dispatch` 의 `dry_run` 체크박스를 켜면 같은 범위까지만 돈다.

인증 검증:

```bash
export YT_CLIENT_ID=... YT_CLIENT_SECRET=... YT_REFRESH_TOKEN=...
uv run python scripts/check_auth.py   # 1유닛만 소모
```

---

## 10. 아직 정해지지 않은 것

작업 시작 전 사용자에게 확인할 것. 임의로 정하지 말 것.

- **채널 주제** — 종합 뉴스인지, 특정 분야(반도체/AI, 부동산, 국제 등)로 좁힐지.
  좁힐수록 YPP 심사에 유리하다
- **"상위 5개" 판정 기준** — 다매체 동시 보도(화제성) / 최신순 / 키워드 매칭
- **TTS 음성** — `edge-tts` 무료 한국어 음성 vs 유료 TTS
- **영상 스타일** — 단색 배경 + 자막인지, 배경 이미지를 쓸지

`config.yml` 은 이 결정들이 나온 뒤에 작성한다.
