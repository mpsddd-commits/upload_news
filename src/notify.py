"""업로드된 숏츠의 검수 요청 Issue 를 만든다.

입력  : out/uploaded.json
출력  : GitHub Issue 1건
쿼터  : 유튜브 API 미사용 (0유닛)

gh CLI 대신 stdlib urllib 로 REST API 를 직접 부른다.
Actions 러너에는 gh 가 깔려 있지만 로컬 윈도우에는 없어서, 같은 코드로
양쪽에서 돌게 하려면 이쪽이 낫다. 의존성도 늘지 않는다.

이 이슈는 "공개로 바꿔라"가 아니라 "확인하고 사람이 직접 바꿔라"를 위한 것이다.
자동 공개 전환은 이 프로젝트의 존재 이유를 무너뜨린다 (CLAUDE.md 1장).
"""

from __future__ import annotations

import json
import os
import pathlib
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
UPLOADED_JSON = ROOT / "out" / "uploaded.json"

API_ROOT = "https://api.github.com"
KST = timezone(timedelta(hours=9))

RETRIABLE = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 3

# 검수자가 매번 같은 순서로 보게 고정한다.
# 경제·금융 채널이라 숫자 확인이 첫 줄이다 (CLAUDE.md 10장).
CHECKLIST = """## 검수 체크리스트

영상마다 아래를 확인하세요.

1. **사실 오류가 없는지 — 특히 숫자·인명·날짜.** 금리·시세·지수를 잘못 읽으면 치명적입니다
2. 자막에 오타나 깨진 글자(네모)가 없는지
3. 제목이 낚시성이 아닌지 — 내용과 다른 자극적 표현 금지
4. 설명란에 출처가 표기됐는지
5. 영상이 세로로 나오고 숏츠로 인식되는지"""

BANNER = """> [!IMPORTANT]
> **공개 전환은 이 이슈에서 하지 않습니다.**
> 아래를 확인한 뒤 유튜브 스튜디오에서 **직접** 공개로 바꾸세요.
> 자동 공개는 채널 수익 창출 심사에 직결되는 문제라 의도적으로 막아 두었습니다."""


class NotifyError(RuntimeError):
    pass


def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise NotifyError(
            f"환경변수 {name} 가 비어 있습니다. "
            "Actions 는 secrets.GITHUB_TOKEN 이 자동 주입되고, "
            "로컬에서는 Issues 쓰기 권한이 있는 토큰을 직접 넣어야 합니다."
        )
    return value


def _resolve_repo() -> str:
    """'owner/repo'. Actions 는 GH_REPO 를 주입하고, 로컬은 git 리모트에서 캔다."""
    repo = os.environ.get("GH_REPO", "").strip()
    if repo:
        return repo

    try:
        url = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=ROOT, capture_output=True, text=True, timeout=15, check=True,
        ).stdout.strip()
    except (subprocess.SubprocessError, FileNotFoundError) as exc:
        raise NotifyError(f"리포지토리를 알 수 없습니다 (GH_REPO 미설정, git 조회 실패): {exc}")

    # https://github.com/owner/repo.git 과 git@github.com:owner/repo.git 둘 다 받는다
    match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", url)
    if not match:
        raise NotifyError(f"github 리모트 URL 을 해석하지 못했습니다: {url}")
    return match.group(1)


def _build_body(items: list[dict], today: str) -> str:
    lines = [BANNER, "", CHECKLIST, "", "---", "", f"## 대상 {len(items)}건"]
    for item in items:
        title = item.get("title", "(제목 없음)")
        studio = item.get("studio_url", "")
        watch = item.get("watch_url", "")
        source_name = item.get("source_name", "출처")
        source_url = item.get("source_url", "")

        lines.append("")
        lines.append(f"- [ ] **{title}**")
        if studio:
            lines.append(f"  - 스튜디오(공개 전환): {studio}")
        if watch:
            lines.append(f"  - 미리보기: {watch}")
        if source_url:
            lines.append(f"  - 원문: [{source_name}]({source_url})")
        lines.append(f"  - id: `{item.get('id', '')}` / 현재 공개상태: `{item.get('privacy', '?')}`")

    lines += [
        "",
        "---",
        "",
        f"자동 생성 ({today} KST). 검수를 마치면 이 이슈를 닫아 주세요.",
    ]
    return "\n".join(lines)


def _post_issue(repo: str, token: str, title: str, body: str) -> dict:
    """실패하면 재시도. 재시도해도 소용없는 오류는 바로 올린다."""
    request = urllib.request.Request(
        f"{API_ROOT}/repos/{repo}/issues",
        data=json.dumps({"title": title, "body": body}).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "shorts-bot",
        },
    )

    last = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = (exc.read().decode("utf-8", "replace") or "")[:200]
            if exc.code == 401:
                raise NotifyError(f"토큰이 유효하지 않습니다 (401). {detail}")
            if exc.code == 403:
                raise NotifyError(
                    "권한이 없습니다 (403). 워크플로에 permissions: issues: write 가 "
                    f"있는지, 토큰 범위에 Issues 쓰기가 있는지 확인하세요. {detail}"
                )
            if exc.code == 404:
                raise NotifyError(
                    f"리포지토리를 찾을 수 없습니다 (404): {repo}. "
                    f"토큰이 이 리포에 접근 가능한지 확인하세요. {detail}"
                )
            if exc.code == 410:
                raise NotifyError(
                    "이 리포지토리에서 Issues 기능이 꺼져 있습니다 (410). "
                    "Settings > General > Features > Issues 를 켜세요."
                )
            if exc.code not in RETRIABLE:
                raise NotifyError(f"HTTP {exc.code}: {detail}")
            last = f"HTTP {exc.code}: {detail}"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = f"{type(exc).__name__}: {exc}"

        if attempt < MAX_ATTEMPTS:
            wait = 2**attempt + random.random()
            print(f"  재시도 {attempt}/{MAX_ATTEMPTS - 1} — {last} ({wait:.1f}초 대기)")
            time.sleep(wait)

    raise NotifyError(last or "알 수 없는 오류")


def main() -> int:
    if not UPLOADED_JSON.exists():
        print(f"[!] {UPLOADED_JSON} 가 없습니다. src.upload 를 먼저 실행하세요.")
        return 1

    items = json.loads(UPLOADED_JSON.read_text(encoding="utf-8"))
    if not items:
        # 업로드가 0건이면 검수할 것도 없다. 빈 이슈를 만들지 않는다.
        print("이번 실행에서 새로 업로드된 영상이 없습니다. 이슈를 만들지 않습니다.")
        return 0

    try:
        token = _env("GH_TOKEN")
        repo = _resolve_repo()
    except NotifyError as exc:
        print(f"[!] {exc}")
        return 1

    today = datetime.now(KST).strftime("%Y-%m-%d")
    title = f"[검수] 숏츠 {len(items)}건 — {today}"
    body = _build_body(items, today)

    print(f"검수 이슈 생성: {repo}")
    print(f"  제목: {title}")
    print(f"  대상: {len(items)}건")

    try:
        issue = _post_issue(repo, token, title, body)
    except NotifyError as exc:
        print(f"[!] 이슈 생성 실패: {exc}")
        return 1

    print(f"\n완료 — #{issue['number']} {issue['html_url']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
