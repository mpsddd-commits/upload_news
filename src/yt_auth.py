"""유튜브 API 인증 공용 모듈.

refresh token 하나로 access token을 매번 새로 발급받아 서비스 객체를 만든다.
GitHub Actions에는 refresh token만 시크릿으로 넣으면 되고,
브라우저 인증은 최초 1회 로컬에서만 한다.
"""

from __future__ import annotations

import os

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

TOKEN_URI = "https://oauth2.googleapis.com/token"

SCOPES = [
    # 영상 업로드 + 썸네일 설정
    "https://www.googleapis.com/auth/youtube.upload",
    # 채널 확인, 업로드 후 처리 상태 조회
    "https://www.googleapis.com/auth/youtube.readonly",
]


class MissingCredentials(RuntimeError):
    pass


def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise MissingCredentials(
            f"환경변수 {name} 가 비어 있습니다. "
            "로컬이면 .env 를, Actions면 리포지토리 시크릿을 확인하세요."
        )
    return value


def build_credentials() -> Credentials:
    """환경변수의 refresh token으로 자격증명을 만들고 즉시 갱신한다."""
    creds = Credentials(
        token=None,
        refresh_token=_env("YT_REFRESH_TOKEN"),
        client_id=_env("YT_CLIENT_ID"),
        client_secret=_env("YT_CLIENT_SECRET"),
        token_uri=TOKEN_URI,
        scopes=SCOPES,
    )
    # access token은 항상 없는 상태로 시작하므로 여기서 한 번 발급받는다.
    # 이 시점에서 실패하면 refresh token이 만료·폐기된 것이다.
    creds.refresh(Request())
    return creds


def build_youtube():
    """YouTube Data API v3 서비스 객체."""
    return build(
        "youtube",
        "v3",
        credentials=build_credentials(),
        cache_discovery=False,
    )
