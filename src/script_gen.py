"""수집한 기사로 숏츠 대본을 만든다.

입력  : out/articles.json, config.yml
출력  : out/scripts.json
쿼터  : 유튜브 API 미사용 (0유닛)

대본 제공자는 config.yml 의 script_gen.provider 로 고른다.
  gemini    — 무료 티어. GEMINI_API_KEY
  anthropic — 유료.      ANTHROPIC_API_KEY

기사 요약문을 그대로 옮기지 않는다. 저작권 요건이라 생성 후 유사도까지 검사한다.
경제·금융 채널이므로 요약문에 없는 수치를 지어내지 못하게 막는 것도 같은 급으로 중요하다.
"""

from __future__ import annotations

import json
import os
import pathlib
import random
import re
import sys
import time
from difflib import SequenceMatcher

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONFIG_YML = ROOT / "config.yml"
ARTICLES_JSON = ROOT / "out" / "articles.json"
SCRIPTS_JSON = ROOT / "out" / "scripts.json"

# 모델이 JSON 앞뒤에 붙이는 ``` 펜스. 방어적으로 벗겨낸다.
FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)

REQUIRED_FIELDS = ("title", "description", "tags", "script", "captions")


class ScriptGenError(RuntimeError):
    """한 기사에 대해 끝내 쓸만한 대본을 못 얻었을 때."""


# ── 프롬프트 ──────────────────────────────────────────────────

SYSTEM_PROMPT = """당신은 한국어 경제 뉴스 유튜브 숏츠의 대본 작가입니다.

지켜야 할 규칙입니다. 어기면 영상을 쓸 수 없습니다.

1. 제공된 요약문의 문장을 그대로 옮기지 마세요. 사실만 파악한 뒤 완전히 새로운
   문장으로 다시 쓰세요. 표현·어순·문장 구조를 모두 바꿔야 합니다.
2. 요약문과 제목에 없는 정보를 만들어내지 마세요. 특히 금액, 비율, 지수, 날짜,
   인명, 기관명은 주어진 것만 쓰세요. 경제 채널이라 숫자 하나가 틀리면 치명적입니다.
   확실하지 않은 수치는 아예 언급하지 말고 넘어가세요.
3. 추측, 전망, 투자 조언을 덧붙이지 마세요. 보도된 사실만 전달합니다.
4. 낚시성 제목을 쓰지 마세요. 기사 내용과 다른 자극적인 표현을 금지합니다.
5. 구어체로 씁니다. 음성으로 읽히는 대본이므로 문어체 명사 나열을 피하고
   자연스럽게 말하듯 이어지게 쓰세요.

JSON 객체 하나만 출력하세요. 설명이나 코드 펜스를 덧붙이지 마세요."""


def _build_user_prompt(article: dict, cfg: dict) -> str:
    return f"""아래 기사로 숏츠 대본을 만들어 주세요.

[원문 제목]
{article['title']}

[원문 요약]
{article['summary']}

[매체] {article['source_name']}

다음 형식의 JSON 객체 하나만 출력하세요.

{{
  "title": "유튜브 제목. {cfg['title_max_chars']}자 이하. 원문 제목을 그대로 쓰지 말 것",
  "description": "설명란 본문 2~3문장. 출처와 해시태그는 자동으로 붙으니 넣지 말 것",
  "tags": ["검색 태그", "5개 내외", "각 15자 이하"],
  "script": "음성으로 읽을 대본 전문. {cfg['target_chars']}자. 한 덩어리의 자연스러운 구어체",
  "captions": ["화면 자막 4~6개", "각 줄 25자 이하", "대본 내용을 순서대로 나눠 담을 것"]
}}"""


# ── 제공자 ────────────────────────────────────────────────────

def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ScriptGenError(
            f"환경변수 {name} 가 비어 있습니다. "
            "로컬이면 셸에서 export 하고, Actions면 리포지토리 시크릿을 확인하세요."
        )
    return value


def _generate_gemini(cfg: dict, user_prompt: str) -> str:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=_env("GEMINI_API_KEY"))
    response = client.models.generate_content(
        model=cfg["gemini_model"],
        contents=user_prompt,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            # JSON 모드를 켜두면 펜스나 군더더기 없이 본문만 온다.
            # 그래도 파싱은 방어적으로 한다 (모델이 항상 지킨다는 보장은 없다).
            response_mime_type="application/json",
            # Gemini 3 계열은 thinking 토큰이 이 상한에서 함께 차감된다.
            # 2048 으로 두면 사고에 1900여 개를 쓰고 본문에 80개만 남아
            # JSON 이 문자열 중간에서 잘린다 (finish_reason=MAX_TOKENS).
            # 대본 자체는 400 토큰이면 충분하지만 사고 몫까지 얹어 넉넉히 잡는다.
            # thinking 을 끄면 응답이 짧아져 script_min_chars 를 밑돌기 쉽다.
            max_output_tokens=8192,
        ),
    )
    return response.text or ""


def _generate_anthropic(cfg: dict, user_prompt: str) -> str:
    from anthropic import Anthropic

    client = Anthropic(api_key=_env("ANTHROPIC_API_KEY"))
    response = client.messages.create(
        model=cfg["anthropic_model"],
        max_tokens=4000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )
    # content 는 블록 배열이다. 텍스트 블록만 이어붙인다.
    return "".join(b.text for b in response.content if b.type == "text")


PROVIDERS = {"gemini": _generate_gemini, "anthropic": _generate_anthropic}
PROVIDER_ENV = {"gemini": "GEMINI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}


# ── 응답 처리 ──────────────────────────────────────────────────

def _parse_json(raw: str) -> dict:
    """앞뒤 펜스를 벗기고 json.loads. 실패하면 예외."""
    text = FENCE_RE.sub("", raw.strip())
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # 앞뒤에 설명이 붙은 경우를 대비해 가장 바깥 중괄호만 잘라 재시도한다.
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise
        data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError(f"JSON 객체가 아닙니다: {type(data).__name__}")
    return data


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def _validate(data: dict, article: dict, cfg: dict) -> list[str]:
    """규격 위반 사유를 모아서 돌려준다. 빈 리스트면 합격."""
    problems = []

    missing = [f for f in REQUIRED_FIELDS if f not in data]
    if missing:
        return [f"필드 누락: {', '.join(missing)}"]

    title = str(data["title"]).strip()
    script = str(data["script"]).strip()
    captions = data["captions"]
    tags = data["tags"]

    if not title:
        problems.append("제목이 비어 있음")
    elif len(title) > 100:
        problems.append(f"제목 {len(title)}자 (100자 초과)")

    length = len(script)
    if not (cfg["script_min_chars"] <= length <= cfg["script_max_chars"]):
        problems.append(
            f"대본 {length}자 "
            f"(허용 {cfg['script_min_chars']}~{cfg['script_max_chars']}자)"
        )

    if not isinstance(captions, list) or not (2 <= len(captions) <= 8):
        problems.append(f"자막 개수 이상: {captions if not isinstance(captions, list) else len(captions)}")
    if not isinstance(tags, list) or not tags:
        problems.append("태그 비어 있음")

    # 저작권 요건. 요약문을 그대로 옮겼는지 본다.
    summary = article["summary"]
    if script == summary:
        problems.append("요약문을 그대로 복사함")
    else:
        ratio = _similarity(script, summary)
        if ratio >= cfg["max_similarity"]:
            problems.append(f"요약문과 유사도 {ratio:.2f} (한계 {cfg['max_similarity']})")

    return problems


def _normalize(data: dict, article: dict) -> dict:
    """데이터 계약(CLAUDE.md 5장)에 맞춘 최종 항목."""
    return {
        "id": article["id"],
        "title": str(data["title"]).strip(),
        "description": str(data["description"]).strip(),
        "tags": [str(t).strip()[:30] for t in data["tags"] if str(t).strip()],
        "script": str(data["script"]).strip(),
        "captions": [str(c).strip() for c in data["captions"] if str(c).strip()],
        "source_name": article["source_name"],
        "source_url": article["source_url"],
    }


def _generate_one(article: dict, cfg: dict) -> dict:
    """기사 1건. 규격을 만족할 때까지 재시도하고, 끝내 안 되면 ScriptGenError."""
    generate = PROVIDERS[cfg["provider"]]
    user_prompt = _build_user_prompt(article, cfg)
    last = "알 수 없음"

    for attempt in range(1, cfg["retries"] + 1):
        try:
            raw = generate(cfg, user_prompt)
            data = _parse_json(raw)
        except ScriptGenError:
            raise  # 자격증명 문제는 재시도해도 소용없다
        except Exception as exc:  # noqa: BLE001 — API 오류든 파싱 오류든 재시도한다
            last = f"{type(exc).__name__}: {exc}"
        else:
            problems = _validate(data, article, cfg)
            if not problems:
                return _normalize(data, article)
            last = "; ".join(problems)

        if attempt < cfg["retries"]:
            wait = cfg["backoff"] ** attempt + random.random()
            print(f"    재시도 {attempt}/{cfg['retries'] - 1} — {last} ({wait:.1f}초 대기)")
            time.sleep(wait)

    raise ScriptGenError(last)


# ── 진입점 ────────────────────────────────────────────────────

def main() -> int:
    if not CONFIG_YML.exists():
        print(f"[!] {CONFIG_YML} 가 없습니다.")
        return 1
    if not ARTICLES_JSON.exists():
        print(f"[!] {ARTICLES_JSON} 가 없습니다. src.collect 를 먼저 실행하세요.")
        return 1

    config = yaml.safe_load(CONFIG_YML.read_text(encoding="utf-8")) or {}
    cfg = config.get("script_gen") or {}
    provider = cfg.get("provider", "gemini")
    if provider not in PROVIDERS:
        print(f"[!] script_gen.provider 값이 잘못됐습니다: {provider}")
        print(f"    사용 가능: {', '.join(PROVIDERS)}")
        return 1
    cfg["provider"] = provider

    articles = json.loads(ARTICLES_JSON.read_text(encoding="utf-8"))
    if not articles:
        print("[!] 기사가 없습니다.")
        return 1

    model_key = "gemini_model" if provider == "gemini" else "anthropic_model"
    print(
        f"대본 {len(articles)}건 생성 시작 "
        f"(제공자: {provider}, 모델: {cfg[model_key]}, 키: {PROVIDER_ENV[provider]})\n"
    )

    results, failures = [], []
    for idx, article in enumerate(articles, 1):
        print(f"[{idx}/{len(articles)}] {article['title'][:40]}")
        try:
            item = _generate_one(article, cfg)
        except ScriptGenError as exc:
            print(f"    실패 (건너뜀): {exc}\n")
            failures.append({"id": article["id"], "error": str(exc)})
            continue

        results.append(item)
        print(f"    제목: {item['title']}")
        print(f"    대본 {len(item['script'])}자, 자막 {len(item['captions'])}개")
        print(f"    {item['script'][:60]}...\n")

    SCRIPTS_JSON.parent.mkdir(parents=True, exist_ok=True)
    SCRIPTS_JSON.write_text(
        json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print(f"성공 {len(results)}건, 실패 {len(failures)}건")
    print(f"{SCRIPTS_JSON.relative_to(ROOT)} 에 저장")
    # 부분 실패는 허용한다. 전건 실패일 때만 파이프라인을 끊는다.
    if failures and not results:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
