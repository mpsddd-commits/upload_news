"""유튜브 인증 검증 — 업로드 전에 자격증명이 살아 있는지 확인한다.

호출하는 API는 channels.list 한 번뿐이고 비용은 1유닛이다.
(CLAUDE.md 7장의 쿼터 예산에서 "검증 시 1"에 해당한다)

실행
    export YT_CLIENT_ID=... YT_CLIENT_SECRET=... YT_REFRESH_TOKEN=...
    uv run python scripts/check_auth.py

본인 채널명이 정확히 출력되면 통과다.
엉뚱한 채널명이 나오면 get_refresh_token.py 실행 시 계정을 잘못 고른 것이므로,
다시 발급받아야 한다.
"""

from __future__ import annotations

import pathlib
import sys

from googleapiclient.errors import HttpError

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from src.yt_auth import MissingCredentials, build_youtube  # noqa: E402


def main() -> int:
    try:
        youtube = build_youtube()
    except MissingCredentials as exc:
        print(f"[!] {exc}")
        return 1
    except Exception as exc:  # refresh 실패 등
        print(f"[!] 자격증명 갱신에 실패했습니다: {exc}")
        print("    refresh token이 만료·폐기됐을 수 있습니다.")
        print("    GCP OAuth 동의 화면이 '테스트' 상태면 7일 만에 만료됩니다.")
        print("    '프로덕션'으로 게시한 뒤 get_refresh_token.py 를 다시 실행하세요.")
        return 1

    # part를 여러 개 넣어도 channels.list 한 번은 1유닛이다.
    try:
        response = (
            youtube.channels()
            .list(part="snippet,status", mine=True)
            .execute()
        )
    except HttpError as exc:
        print(f"[!] channels.list 호출에 실패했습니다: {exc}")
        return 1

    items = response.get("items", [])
    if not items:
        print("[!] 이 자격증명에 연결된 채널이 없습니다.")
        print("    구글 계정에 유튜브 채널이 만들어져 있는지 확인하세요.")
        print("    브랜드 계정을 쓴다면 인증 시 개인 계정이 아니라 채널을 골라야 합니다.")
        return 1

    channel = items[0]
    snippet = channel.get("snippet", {})
    status = channel.get("status", {})

    print("=" * 64)
    print("인증 성공")
    print("=" * 64)
    print(f"채널명    : {snippet.get('title', '(제목 없음)')}")
    print(f"채널 ID   : {channel.get('id', '(알 수 없음)')}")
    print(f"스튜디오  : https://studio.youtube.com/channel/{channel.get('id', '')}")
    print("=" * 64)

    # 전화 인증이 끝나야 15분 초과 업로드와 커스텀 썸네일이 열린다.
    # 여기가 'notAllowed'면 thumbnails.set 이 403으로 실패하는데,
    # 썸네일 실패는 파이프라인을 죽이지 않으므로 경고만 남긴다.
    long_uploads = status.get("longUploadsStatus", "unknown")
    if long_uploads != "allowed":
        print()
        print(f"[경고] longUploadsStatus = {long_uploads}")
        print("       채널 전화 인증이 완료되지 않았을 수 있습니다.")
        print("       이 경우 커스텀 썸네일 설정(thumbnails.set)이 403으로 실패합니다.")
        print("       업로드 자체는 정상 동작하므로 지금 당장 문제는 아닙니다.")

    print()
    print("이 채널명이 본인 채널이 맞습니까? 아니라면 refresh token을 다시 발급하세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
