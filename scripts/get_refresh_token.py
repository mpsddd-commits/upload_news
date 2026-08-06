"""refresh token 발급 — 로컬에서 딱 한 번만 실행한다.

사전 준비
  1. GCP 콘솔 > API 및 서비스 > 라이브러리 에서 "YouTube Data API v3" 사용 설정
  2. OAuth 동의 화면을 만들고 **게시 상태를 '프로덕션'으로 전환**
     (테스트 상태로 두면 refresh token이 7일 만에 만료됩니다)
  3. 사용자 인증 정보 > OAuth 클라이언트 ID > 애플리케이션 유형 = **데스크톱 앱**
  4. JSON 다운로드 후 리포 루트에 client_secret.json 으로 저장
     (.gitignore에 들어있는지 반드시 확인)

실행
    uv run python scripts/get_refresh_token.py

브라우저가 열리면 **업로드할 채널의 구글 계정**으로 로그인하세요.
브랜드 계정을 쓰신다면 계정 선택 화면에서 개인 계정이 아니라
해당 브랜드 채널을 골라야 합니다. 여기서 잘못 고르면 엉뚱한 채널에 올라갑니다.
"""

from __future__ import annotations

import pathlib
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from src.yt_auth import SCOPES  # noqa: E402

CLIENT_SECRET = pathlib.Path("client_secret.json")


def main() -> int:
    if not CLIENT_SECRET.exists():
        print(f"[!] {CLIENT_SECRET} 가 없습니다. 위 주석의 1~4단계를 먼저 진행하세요.")
        return 1

    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)

    creds = flow.run_local_server(
        port=0,
        # offline: refresh token을 함께 발급받기 위해 필수
        access_type="offline",
        # consent: 이미 동의한 적이 있어도 refresh token을 다시 발급하도록 강제.
        #          이게 없으면 두 번째 실행부터 refresh_token이 None으로 온다.
        prompt="consent",
        authorization_prompt_message="브라우저에서 인증을 완료하세요: {url}",
        success_message="인증 완료. 터미널로 돌아가세요.",
    )

    if not creds.refresh_token:
        print("[!] refresh token이 발급되지 않았습니다.")
        print("    GCP 콘솔 > 보안 > 서드파티 앱에서 기존 권한을 해제한 뒤 다시 실행하세요.")
        return 1

    print("\n" + "=" * 64)
    print("아래 세 값을 GitHub 리포지토리 시크릿에 등록하세요.")
    print("Settings > Secrets and variables > Actions > New repository secret")
    print("=" * 64)
    print(f"YT_CLIENT_ID     = {flow.client_config['client_id']}")
    print(f"YT_CLIENT_SECRET = {flow.client_config['client_secret']}")
    print(f"YT_REFRESH_TOKEN = {creds.refresh_token}")
    print("=" * 64)
    print("\n이 출력은 화면에만 남기고 파일로 저장하지 마세요.")
    print("등록이 끝나면 client_secret.json 도 로컬에서 지우는 편이 안전합니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
