# TASKS.md — 구현 작업 순서

Claude Code에게 한 번에 하나씩 지시한다. 예: **"TASKS.md의 Task 2를 진행해줘"**

각 Task는 수용 기준(검증 명령)이 통과해야 완료다. 통과 못 하면 다음으로 넘어가지 않는다.
`CLAUDE.md` 의 "절대 하지 말 것"은 모든 Task에 적용된다.

---

## 진행 현황 (2026-08-06 기준)

| Task | 상태 | 비고 |
|---|---|---|
| 0 스캐폴딩 | **완료** | 수용 기준 2종 통과 |
| 1 GCP 인증 | **미착수** | 사람이 콘솔에서 수행. 유튜브 업로드의 유일한 선행 조건 |
| 2 `config.yml` + `collect.py` | **완료** | 수용 기준 통과 |
| 3 `script_gen.py` | **구현 완료 / 실호출 미검증** | 파싱·검증·재시도는 검증됨. 실제 Gemini 호출은 아직 |
| 4 `tts.py` | **완료** | 수용 기준 통과 |
| 5 `render.py` | **완료** | 수용 기준 + 한글 자막 육안 확인까지 통과 |
| 6 `notify.py` | **미착수** | 다음 작업 |
| 7 통합 점검 | 미착수 | |

### 내일 바로 할 것

1. **`daily.yml` 미커밋 변경분을 푸시한다.** dry_run 이 유튜브 시크릿 없이도
   돌도록 사전 점검을 고친 것과, 실패 알림의 라벨 지정을 뺀 것 두 가지다.
   푸시 전에는 dry_run 이 시크릿 누락으로 즉시 실패한다.
2. **Actions → `daily-shorts-draft` → Run workflow → `dry_run` 체크, `count`는 2.**
   여기서 Task 3 의 실제 Gemini 호출이 검증된다. 결과 mp4 는 아티팩트로 받는다.
3. 그 다음 **Task 6 (`notify.py`)** 를 구현한다.

### 막혀 있는 것

- **Task 1 (GCP)** — 사람이 콘솔에서 해야 한다. 이게 끝나야 Task 7 의 실제 업로드가 가능하다.
  dry_run 범위(수집~렌더)는 Task 1 없이도 검증된다.
- **로컬에서 Task 3 검증** — GitHub Secrets 는 Actions 안에서만 보인다.
  로컬에서 돌리려면 셸에 `$env:GEMINI_API_KEY` 를 따로 설정해야 한다.

---

## Task 0 — 스캐폴딩 (사람이 먼저 수행)

이미 받은 파일들을 배치하고 프로젝트를 초기화한다.

```bash
# uv 프로젝트 초기화
uv init --bare --name shorts-bot --python 3.12   # 빈 레포면 uv init . 후 rm main.py
uv add feedparser anthropic edge-tts pyyaml \
       google-api-python-client google-auth google-auth-oauthlib

# 디렉터리
mkdir -p .github/workflows src scripts state \
         assets/{fonts,bg,bgm} out/{video,audio,thumb}
touch src/__init__.py assets/{fonts,bg,bgm}/.gitkeep
echo '{}' > state/posted.json
```

배치할 완성 파일:

| 파일 | 위치 |
|---|---|
| `daily.yml` | `.github/workflows/daily.yml` |
| `yt_auth.py` | `src/yt_auth.py` |
| `upload.py` | `src/upload.py` |
| `get_refresh_token.py` | `scripts/get_refresh_token.py` |
| `check_auth.py` | `scripts/check_auth.py` |
| `gitignore.txt` | `.gitignore` (이름 변경) |
| `CLAUDE.md` | 리포 루트 |
| `TASKS.md` | 리포 루트 |

**수용 기준**

```bash
git add -A -n | grep -c '.venv'   # → 0 이어야 함
uv run python -c "import src.yt_auth; print('ok')"
```

---

## Task 1 — GCP 인증 뚫기 (사람이 수행, 코드 작업 아님)

1. GCP 콘솔에서 **YouTube Data API v3** 사용 설정
2. OAuth 동의 화면 생성 → 외부 → **게시 상태를 "프로덕션"으로 전환**
3. OAuth 클라이언트 ID → 애플리케이션 유형 **데스크톱 앱** → JSON 다운로드
4. 리포 루트에 `client_secret.json` 으로 저장

```bash
uv run python scripts/get_refresh_token.py
# 출력된 3개 값을 GitHub Secrets에 등록
export YT_CLIENT_ID=... YT_CLIENT_SECRET=... YT_REFRESH_TOKEN=...
uv run python scripts/check_auth.py
```

**수용 기준**: `check_auth.py` 가 본인 채널명을 정확히 출력한다.

---

## Task 2 — `config.yml` + `src/collect.py`

**선행 결정 필요** (`CLAUDE.md` 10장). 사용자에게 먼저 물어볼 것.

### 요구사항

`config.yml` 에 RSS 피드 목록과 수집 설정을 둔다. 코드에 URL을 박지 않는다.

```yaml
channel:
  topic: "<사용자가 정한 주제>"
  language: ko

feeds:
  - name: 연합뉴스
    url: https://...
  # 3~6개

collect:
  ranking: cluster        # cluster | recent | keyword
  lookback_hours: 24
  min_title_length: 10
  keywords: []            # ranking이 keyword일 때만 사용
```

`src/collect.py` 동작:

1. `config.yml` 의 모든 피드를 `feedparser` 로 파싱 (피드 하나가 죽어도 계속 진행)
2. `lookback_hours` 이내 항목만 남긴다
3. `state/posted.json` 의 `source_url` 집합과 대조해 이미 다룬 기사 제외
4. 제목 유사도로 클러스터링해 같은 사건을 묶는다
   (외부 라이브러리 없이 `difflib.SequenceMatcher` + 형태소 없는 어절 자카드 정도면 충분.
   임계값은 상수로 빼고 주석으로 근거를 남길 것)
5. `ranking` 기준으로 정렬 후 `--count` 만큼 선택
6. `out/articles.json` 으로 저장

**인자**: `--count N` (기본 5), `--dry-run` (파일 저장 없이 결과만 출력)

### 수용 기준

```bash
uv run python -m src.collect --count 5 --dry-run   # 5건 출력, 중복 없음
uv run python -m src.collect --count 5
python -c "
import json; a=json.load(open('out/articles.json'))
assert len(a)<=5
ids={x['id'] for x in a}; urls={x['source_url'] for x in a}
assert len(ids)==len(a) and len(urls)==len(a), '중복 있음'
assert all(k in a[0] for k in ['id','title','summary','source_name','source_url','published_at','cluster_size'])
print('통과', len(a))
"
```

피드 하나를 일부러 잘못된 URL로 바꿔도 나머지로 동작해야 한다.

---

## Task 3 — `src/script_gen.py`

### 요구사항

`out/articles.json` 을 읽어 Anthropic API로 대본을 만들고 `out/scripts.json` 을 쓴다.

- 제공자는 `config.yml` 의 `script_gen.provider` 로 고른다. **기본값은 `gemini`** (무료 티어).
  (초안은 Anthropic 전용이었으나, 비용 0원으로 시작하되 언제든 갈아끼울 수 있게 바꿨다)
  - `gemini`    — `from google import genai` / 모델 `gemini-2.5-flash` / `GEMINI_API_KEY`
  - `anthropic` — `from anthropic import Anthropic` / 모델 `claude-sonnet-5` / `ANTHROPIC_API_KEY`
- **기사 요약문을 그대로 옮기지 말고 자기 문장으로 다시 쓰도록** 프롬프트에 명시.
  이건 저작권 요건이다. 생성 후 원문과의 유사도까지 검사하고, `script_gen.max_similarity`
  (기본 0.80) 이상이면 다시 생성한다. 조사만 바꾼 표절은 문자열 비교로는 안 잡힌다
- **요약문에 없는 수치·인명·날짜를 만들어내지 못하게** 프롬프트에 명시.
  경제·금융 채널이라 숫자 하나가 틀리면 치명적이다
- 숏츠 분량: 한국어 기준 **45초 내외, 약 300~330자**
  (초안의 "200~250자"는 실측과 맞지 않아 정정. ko-KR-SunHiNeural + `rate "+10%"` 의
  발화 속도가 **7.46자/초** 라 200~250자는 27~34초에 그치고, Task 4의 "각 30~60초"
  수용 기준을 통과하지 못한다. 실제 수치는 `config.yml` 의 `script_gen` 주석 참조)
- 프롬프트에서 JSON만 반환하도록 지시하고, 응답 파싱은 방어적으로
  (앞뒤 ```json 펜스 제거 후 `json.loads`, 실패 시 해당 건만 건너뜀)
- 요청할 필드: `title`, `description`, `tags`(5개 내외), `script`, `captions`(4~6개 문장)
- 기사 1건 = API 호출 1회. 실패는 3회까지 재시도
- `articles.json` 의 `id`, `source_name`, `source_url` 을 그대로 이어붙인다

### 수용 기준

```bash
uv run python -m src.script_gen
python -c "
import json; s=json.load(open('out/scripts.json')); a=json.load(open('out/articles.json'))
assert len(s)>0
for x in s:
    assert len(x['title'])<=100, '제목 초과'
    assert 100<=len(x['script'])<=400, f'대본 길이 이상: {len(x[\"script\"])}'
    assert x['source_url'] and x['id']
    orig=next(o for o in a if o['id']==x['id'])
    assert x['script'] != orig['summary'], '요약문 그대로 복사됨'
print('통과', len(s))
"
```

---

## Task 4 — `src/tts.py`

### 요구사항

`out/scripts.json` 의 `script` 를 음성으로 합성해 `out/audio/<id>.mp3` 로 저장하고,
`scripts.json` 의 `audio_path` 를 갱신한다.

- `edge-tts` 사용 (무료, API 키 불필요). 한국어 음성은 `ko-KR-SunHiNeural` 등에서 선택
- 비동기 API이므로 `asyncio.run()` 으로 감싼다
- 속도는 `+10%` 정도로 올려 숏츠 호흡에 맞춘다 (config로 뺄 것)
- 생성 후 `ffprobe` 로 길이를 재서 **60초를 넘으면 경고를 출력**한다
- 이미 `audio_path` 파일이 존재하면 재생성하지 않는다 (재실행 비용 절약)

### 수용 기준

```bash
uv run python -m src.tts
for f in out/audio/*.mp3; do
  ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 "$f"
done   # 각 30~60초 사이
python -c "
import json,pathlib
s=json.load(open('out/scripts.json'))
assert all(pathlib.Path(x['audio_path']).exists() for x in s)
print('통과')
"
```

---

## Task 5 — `src/render.py`

### 요구사항

음성 + 자막을 합쳐 **1080x1920 세로 mp4** 를 만들고 `video_path` 를 갱신한다.

- `subprocess` 로 `ffmpeg` 직접 호출 (moviepy 쓰지 말 것 — 무겁고 느리다)
- 사양: 1080x1920, H.264 (`libx264`), `yuv420p`, AAC 오디오, 30fps
- 자막은 `captions` 를 음성 길이에 비례 분할해 순차 표시.
  `drawtext` 필터 + `fontfile=/usr/share/fonts/truetype/nanum/NanumGothic.ttf`
- 자막 텍스트의 특수문자(`:` `'` `"` `%` `\` `,` `[`) 처리 필수.
  **이스케이프하지 말고 회피한다** — `text=` 대신 `textfile=` 로 파일에서 읽고
  `expansion=none` 을 준다. 필터 파서가 백슬래시를 두 번 처리해서 이스케이프 규칙이
  플랫폼마다 다르게 깨지는데, 뉴스 제목엔 이 문자들이 흔해 한 건만 걸려도 렌더가 통째로 죽는다
- 배경은 `config.yml` 설정에 따라 단색 또는 `assets/bg/` 이미지
- 상하단에 안전 여백. 숏츠 UI가 하단 20% 정도를 가린다
- 렌더 실패한 건은 건너뛰고 나머지 계속

### 수용 기준

```bash
uv run python -m src.render
for f in out/video/*.mp4; do
  ffprobe -v error -select_streams v:0 \
    -show_entries stream=width,height,codec_name -of default=nw=1 "$f"
done   # width=1080 height=1920 codec_name=h264
```

그리고 **실제로 mp4를 열어 한글 자막이 깨지지 않는지 눈으로 확인한다.**
두부(□□□)로 나오면 폰트 경로 문제다.

---

## Task 6 — `src/notify.py`

### 요구사항

`out/uploaded.json` 을 읽어 검수용 GitHub Issue를 생성한다.

- `gh` CLI (`subprocess`) 또는 GitHub REST API 중 편한 쪽. `GH_TOKEN` 사용
- 제목: `[검수] 숏츠 N건 — YYYY-MM-DD`
- 본문은 체크박스 목록. 각 항목에 스튜디오 링크, 원문 링크, 제목
- 본문 상단에 검수 체크리스트를 고정 문구로 넣는다:
  - 사실 오류 없는지 (특히 숫자·인명·날짜)
  - 자막 오타·깨짐 없는지
  - 제목이 낚시성이 아닌지
  - 출처 표기가 설명란에 있는지
  - **공개 전환은 스튜디오에서 직접**

### 수용 기준

```bash
GH_TOKEN=... uv run python -m src.notify
gh issue list --limit 1   # 방금 만든 이슈가 보인다
```

---

## Task 7 — 통합 점검

```bash
# 로컬 전체 실행 (업로드 제외)
rm -rf out && mkdir -p out/{video,audio,thumb}
uv run python -m src.collect --count 3
uv run python -m src.script_gen
uv run python -m src.tts
uv run python -m src.render
```

그다음 Actions에서 `workflow_dispatch` → `dry_run` 체크 → 실행.
아티팩트로 mp4를 받아 확인한 뒤, `dry_run` 해제하고 다시 실행해 실제 업로드까지 확인한다.

**최종 확인**

- 유튜브 스튜디오에 영상이 **비공개**로 올라와 있다
- 세로 화면이고 숏츠로 인식된다
- 검수 이슈가 생성됐다
- `state/posted.json` 이 커밋됐다
- 다음 날 실행 시 같은 기사가 다시 올라오지 않는다

---

## 참고 — 진행 중 막히면

Claude Code에게 이렇게 물어보면 된다.

- `"CLAUDE.md 8장 함정 목록 보고 이 에러 원인 찾아줘"`
- `"Task 5 수용 기준이 실패한다. ffprobe 출력은 이렇다: ..."`
- `"이 변경이 CLAUDE.md 2장 금지 목록을 위반하는지 확인해줘"`
