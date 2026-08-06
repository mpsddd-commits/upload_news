"""RSS 피드를 수집해 오늘 다룰 기사 후보를 고른다.

입력  : config.yml, state/posted.json
출력  : out/articles.json
쿼터  : 유튜브 API 미사용 (0유닛)

기사 본문은 절대 가져오지 않는다. RSS 가 주는 제목과 요약문만 쓴다.
이 요약문도 영상에 그대로 나가지 않고 script_gen.py 의 입력으로만 쓰인다.
"""

from __future__ import annotations

import argparse
import html
import json
import pathlib
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONFIG_YML = ROOT / "config.yml"
POSTED_JSON = ROOT / "state" / "posted.json"
ARTICLES_JSON = ROOT / "out" / "articles.json"

KST = timezone(timedelta(hours=9))

# ── 클러스터링 임계값 ──────────────────────────────────────────
#
# 형태소 분석기 없이 두 지표를 병행한다.
#   자카드 : 조사를 떼어낸 어절 집합의 겹침. 핵심 명사 공유를 본다.
#   문자열 : 공백·문장부호를 지운 제목의 SequenceMatcher 비율.
#            자카드가 조사·표기 차이로 놓치는 경우를 보완한다.
#            ("현대엔지니어링" vs "현대ENG", "코스피 급락" vs "코스피가 급락했다")
#
# 처음에는 "둘 중 하나만 넘으면 같은 사건"(0.34 / 0.62)으로 뒀는데,
# 실제 피드로 확인해 보니 같은 사건을 다른 매체가 쓴 제목이 대부분
# 그 아래에 깔려 있어 화제성이 심하게 과소평가됐다. 실측 예:
#   0.31/0.53  "6월 경상수지 497.3억달러 흑자…두 달 연속 역대 최대"(연합)
#              "[속보] 반도체 수출 호조…6월 경상수지 497억3000만달러 최대 흑자"(매경)
#   0.27/0.44  "오세훈 '정부 세제개편안, 집값 못잡고 전월세 불안만 키울 것'"(머투)
#              "오세훈 '집값은 잡지도 못하면서 전월세만 올려놨다'"(뉴시스)
#
# 그래서 한쪽이 확실히 높은 경우(STRONG)에 더해
# **양쪽이 동시에 어중간히 높은 구간**(WEAK 조합)을 같은 사건으로 본다.
# 어느 한쪽만 어중간한 것은 묶지 않는다. 아래가 그 구간에서 갈린 실측 쌍이다.
#   묶임   0.25/0.42  "현대엔지니어링, 협력사 안전·품질 인력 장기근속 지원…"(머투)
#                     "현대ENG, 협력사 안전·품질 인재 육성 지원…3년간 25.2억"(뉴시스)
#   안묶임 0.23/0.41  "농협, 폭염·가뭄 대응 총력…농축산농가 현장점검 강화"(뉴시스)
#                     "울산시, 폭염·가뭄 대응 농업인 보험 가입 지원"(연합)
#
# 이 값들은 2026-08-06 자 실제 피드로 맞춘 것이라 경계가 좁다.
# 화제성이 계속 과소·과대평가되면 여기부터 손대면 된다.
# 묶기 실패(재현율)가 묶기 과다(정밀도)보다 해롭다. 잘못 묶이면 기사 1건이
# 후보에서 빠질 뿐이지만, 못 묶으면 그날 최대 이슈의 순위가 통째로 밀린다.
JACCARD_STRONG = 0.30
RATIO_STRONG = 0.55
JACCARD_WEAK = 0.20
RATIO_WEAK = 0.42

# 어절 끝에 붙는 조사·어미. 긴 것부터 검사해 가장 긴 것 하나만 떼어낸다.
# 형태소 분석기를 쓰지 않는 대신의 최소 장치다.
JOSA_SUFFIXES = (
    "으로써", "으로서", "이라고", "이라는", "에서는", "에게서", "라고",
    "으로", "에서", "에게", "까지", "부터", "보다", "마다", "조차",
    "처럼", "이나", "이란", "이라", "와의", "과의", "에는", "은는",
    "의", "가", "이", "은", "는", "을", "를", "에", "와", "과",
    "도", "로", "만", "께", "랑",
)

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")
# "(서울=연합뉴스) 조성흠 기자 = ", "[서울=뉴시스]정진형 기자 = " 같은 바이라인.
# 대본 입력에서는 잡음이므로 떼어낸다.
# 괄호 안에 '=' 가 있을 때만 매칭시켜 머니투데이의 "(상보)" 같은 말머리는 남긴다.
BYLINE_RE = re.compile(
    r"^[(\[][^)\]]{0,40}=[^)\]]{0,40}[)\]]\s*(?:[^=]{0,40}?기자\s*=\s*)?"
)
# 어절 토큰화용. 한글·영문·숫자만 남긴다.
NON_WORD_RE = re.compile(r"[^0-9A-Za-z가-힣\s]+")

SUMMARY_MAX_CHARS = 600


class FeedError(RuntimeError):
    """피드 하나를 끝내 읽지 못했을 때."""


# ── 텍스트 정리 ────────────────────────────────────────────────

def _clean_text(raw: str) -> str:
    """HTML 태그·엔티티를 걷어내고 공백을 정리한다.

    머니투데이 요약문은 <table><img> 가 통째로 들어 있고,
    제목에는 &quot; &#039; 같은 엔티티가 섞여 온다.
    """
    if not raw:
        return ""
    text = TAG_RE.sub(" ", raw)
    # 엔티티가 두 번 인코딩된 피드가 있어 두 번 푼다 (&amp;quot; → &quot; → ")
    text = html.unescape(html.unescape(text))
    text = TAG_RE.sub(" ", text)
    return WS_RE.sub(" ", text).strip()


def _strip_byline(text: str) -> str:
    return BYLINE_RE.sub("", text, count=1).strip()


def _normalize_title(title: str) -> str:
    """문자열 유사도 비교용. 공백과 문장부호를 전부 없앤다."""
    return NON_WORD_RE.sub("", title).replace(" ", "")


def _tokenize(title: str) -> set[str]:
    """어절 자카드 비교용 토큰 집합."""
    cleaned = NON_WORD_RE.sub(" ", title)
    tokens = set()
    for word in cleaned.split():
        if len(word) < 2:
            # 한 글자 어절은 대부분 조사나 의존명사라 변별력이 없다.
            continue
        for josa in JOSA_SUFFIXES:
            if word.endswith(josa) and len(word) - len(josa) >= 2:
                word = word[: -len(josa)]
                break
        tokens.add(word)
    return tokens


def _canonical_url(url: str) -> str:
    """추적 파라미터를 떼어 같은 기사를 같은 URL로 만든다.

    posted.json 의 source_url 도 이 형태로 저장되므로 대조가 성립한다.
    """
    if not url:
        return ""
    parts = urlsplit(url.strip())
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith(("utm_", "fbclid", "gclid"))
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme, parts.netloc.lower(), path, urlencode(query), ""))


# ── 피드 읽기 ──────────────────────────────────────────────────

def _fetch_feed(url: str, agent: str, retries: int, backoff: float):
    """피드 하나를 재시도하며 읽는다. 끝내 실패하면 FeedError."""
    reason = "알 수 없음"
    for attempt in range(1, retries + 1):
        try:
            parsed = feedparser.parse(url, agent=agent)
        except Exception as exc:  # noqa: BLE001 — 어떤 예외든 다음 피드로 넘어간다
            reason = f"{type(exc).__name__}: {exc}"
        else:
            if parsed.entries:
                return parsed
            status = getattr(parsed, "status", None)
            exc = getattr(parsed, "bozo_exception", None)
            reason = f"status={status}, " + (
                f"{type(exc).__name__}: {exc}" if exc else "엔트리 0건"
            )
        if attempt < retries:
            wait = backoff**attempt
            print(f"    재시도 {attempt}/{retries - 1} — {reason} ({wait:.0f}초 대기)")
            time.sleep(wait)
    raise FeedError(reason)


def _entry_published(entry) -> datetime | None:
    """feedparser 는 시각을 UTC struct_time 으로 normalize 해준다."""
    stamp = getattr(entry, "published_parsed", None) or getattr(
        entry, "updated_parsed", None
    )
    if not stamp:
        return None
    return datetime(*stamp[:6], tzinfo=timezone.utc).astimezone(KST)


def _normalize_entry(entry, source_name: str) -> dict | None:
    title = _clean_text(getattr(entry, "title", ""))
    link = _canonical_url(getattr(entry, "link", ""))
    published = _entry_published(entry)
    if not title or not link or published is None:
        return None

    summary = _strip_byline(
        _clean_text(getattr(entry, "summary", "") or getattr(entry, "description", ""))
    )
    if not summary:
        # 요약이 비어 있는 엔트리가 섞여 있다. 제목만으로도 대본 생성은 가능하다.
        summary = title

    return {
        "title": title,
        "summary": summary[:SUMMARY_MAX_CHARS],
        "source_name": source_name,
        "source_url": link,
        "published": published,
        "_norm": _normalize_title(title),
        "_tokens": _tokenize(title),
    }


def _collect_entries(feeds: list[dict], settings: dict) -> tuple[list[dict], int]:
    """모든 피드를 훑는다. 하나가 죽어도 나머지는 계속한다."""
    agent = settings.get("user_agent", "shorts-bot/0.1")
    retries = int(settings.get("request_retries", 3))
    backoff = float(settings.get("request_backoff", 2.0))

    entries: list[dict] = []
    failed = 0
    for feed in feeds:
        name = feed.get("name", "(이름 없음)")
        url = feed.get("url", "")
        print(f"  {name} — {url}")
        try:
            parsed = _fetch_feed(url, agent, retries, backoff)
        except FeedError as exc:
            failed += 1
            print(f"    실패 (건너뜀): {exc}")
            continue

        picked = 0
        for entry in parsed.entries:
            item = _normalize_entry(entry, name)
            if item:
                entries.append(item)
                picked += 1
        dropped = len(parsed.entries) - picked
        note = f" (제목·링크·날짜 누락 {dropped}건 제외)" if dropped else ""
        print(f"    {picked}건 수집{note}")

    return entries, failed


# ── 선별 ──────────────────────────────────────────────────────

def _filter_entries(
    entries: list[dict], lookback_hours: int, min_title_length: int, posted_urls: set[str]
) -> list[dict]:
    cutoff = datetime.now(KST) - timedelta(hours=lookback_hours)
    kept: list[dict] = []
    seen_urls: set[str] = set()
    counts = {"오래됨": 0, "제목 짧음": 0, "이미 다룸": 0, "피드 중복": 0}

    # 최신순으로 훑어야 같은 URL이 여러 피드에 걸렸을 때 최신 항목이 남는다.
    for item in sorted(entries, key=lambda x: x["published"], reverse=True):
        if item["published"] < cutoff:
            counts["오래됨"] += 1
            continue
        if len(item["title"]) < min_title_length:
            counts["제목 짧음"] += 1
            continue
        if item["source_url"] in posted_urls:
            counts["이미 다룸"] += 1
            continue
        if item["source_url"] in seen_urls:
            counts["피드 중복"] += 1
            continue
        seen_urls.add(item["source_url"])
        kept.append(item)

    detail = ", ".join(f"{k} {v}건" for k, v in counts.items() if v)
    print(f"  {len(kept)}건 남음" + (f" (제외: {detail})" if detail else ""))
    return kept


def _same_event(a: dict, b: dict) -> bool:
    tokens_a, tokens_b = a["_tokens"], b["_tokens"]
    jaccard = 0.0
    if tokens_a and tokens_b:
        jaccard = len(tokens_a & tokens_b) / len(tokens_a | tokens_b)
    if jaccard >= JACCARD_STRONG:
        return True

    ratio = SequenceMatcher(None, a["_norm"], b["_norm"]).ratio()
    if ratio >= RATIO_STRONG:
        return True

    # 어느 한쪽만 어중간한 것은 묶지 않는다. 둘 다 받쳐줘야 같은 사건으로 본다.
    return jaccard >= JACCARD_WEAK and ratio >= RATIO_WEAK


def _cluster(entries: list[dict]) -> list[list[dict]]:
    """제목 유사도로 같은 사건을 묶는다.

    새 기사는 각 클러스터의 **대표 1건** 과만 비교한다.
    모든 구성원과 비교하면(single-link) A~B, B~C 가 이어지면서
    정작 A~C 는 전혀 다른 사건인데도 한 덩어리가 되는 사슬 현상이 생긴다.
    """
    clusters: list[list[dict]] = []
    for item in entries:
        for cluster in clusters:
            if _same_event(item, cluster[0]):
                cluster.append(item)
                break
        else:
            clusters.append([item])
    return clusters


def _representative(cluster: list[dict]) -> dict:
    """클러스터에서 내보낼 1건.

    대본 생성 입력이 되므로 요약문이 가장 충실한 항목을 고른다.
    같으면 먼저 보도한 쪽(가장 이른 시각)을 택한다.
    """
    return sorted(cluster, key=lambda x: (-len(x["summary"]), x["published"]))[0]


def _keyword_score(item: dict, keywords: list[str]) -> int:
    haystack = f"{item['title']} {item['summary']}"
    return sum(1 for kw in keywords if kw and kw in haystack)


def _rank(clusters: list[list[dict]], ranking: str, keywords: list[str]) -> list[dict]:
    """클러스터를 대표 기사 목록으로 펼치고 기준에 맞춰 정렬한다."""
    rows = []
    for cluster in clusters:
        rep = _representative(cluster)
        # 화제성은 피드 수가 아니라 **서로 다른 매체 수** 로 센다.
        # 매일경제 경제/부동산/증권에 같은 기사가 걸려도 3이 되면 안 된다.
        sources = {c["source_name"] for c in cluster}
        rows.append(
            {
                "item": rep,
                "cluster_size": len(sources),
                "latest": max(c["published"] for c in cluster),
            }
        )

    if ranking == "recent":
        rows.sort(key=lambda r: r["latest"], reverse=True)
    elif ranking == "keyword":
        rows.sort(
            key=lambda r: (
                -_keyword_score(r["item"], keywords),
                -r["cluster_size"],
                -r["latest"].timestamp(),
            )
        )
    else:  # cluster (기본)
        rows.sort(key=lambda r: (-r["cluster_size"], -r["latest"].timestamp()))
    return rows


def _assign_ids(rows: list[dict], posted: dict) -> list[dict]:
    """YYYYMMDD-NNN. 전 파이프라인의 중복 방지 키다.

    같은 날 두 번 돌려도 이미 업로드된 번호를 다시 쓰지 않는다.
    (upload.py 가 id 로 중복을 거르기 때문에 번호가 겹치면 새 기사가 조용히 누락된다)
    """
    date_str = datetime.now(KST).strftime("%Y%m%d")
    used = {k for k in posted if k.startswith(f"{date_str}-")}

    articles, seq = [], 1
    for row in rows:
        while f"{date_str}-{seq:03d}" in used:
            seq += 1
        article_id = f"{date_str}-{seq:03d}"
        used.add(article_id)
        seq += 1

        item = row["item"]
        articles.append(
            {
                "id": article_id,
                "title": item["title"],
                "summary": item["summary"],
                "source_name": item["source_name"],
                "source_url": item["source_url"],
                "published_at": item["published"].isoformat(timespec="seconds"),
                "cluster_size": row["cluster_size"],
            }
        )
    return articles


# ── 진입점 ────────────────────────────────────────────────────

def _load_posted() -> dict:
    if POSTED_JSON.exists():
        return json.loads(POSTED_JSON.read_text(encoding="utf-8") or "{}")
    return {}


def main() -> int:
    parser = argparse.ArgumentParser(description="뉴스 RSS 수집")
    parser.add_argument("--count", type=int, default=5, help="선택할 기사 수 (기본 5)")
    parser.add_argument(
        "--dry-run", action="store_true", help="파일로 저장하지 않고 결과만 출력"
    )
    args = parser.parse_args()

    if not CONFIG_YML.exists():
        print(f"[!] {CONFIG_YML} 가 없습니다.")
        return 1

    config = yaml.safe_load(CONFIG_YML.read_text(encoding="utf-8")) or {}
    feeds = config.get("feeds") or []
    if not feeds:
        print("[!] config.yml 에 feeds 가 비어 있습니다.")
        return 1

    settings = config.get("collect") or {}
    ranking = settings.get("ranking", "cluster")
    if ranking not in ("cluster", "recent", "keyword"):
        print(f"[!] collect.ranking 값이 잘못됐습니다: {ranking}")
        return 1
    keywords = settings.get("keywords") or []
    if ranking == "keyword" and not keywords:
        print("[!] ranking 이 keyword 인데 keywords 가 비어 있습니다.")
        return 1

    posted = _load_posted()
    posted_urls = {
        _canonical_url(v.get("source_url", ""))
        for v in posted.values()
        if v.get("source_url")
    }

    print(f"피드 {len(feeds)}개 수집 시작 (정렬 기준: {ranking})")
    entries, failed = _collect_entries(feeds, settings)
    if failed:
        print(f"  피드 {failed}/{len(feeds)}개 실패")
    if not entries:
        print("[!] 어떤 피드에서도 기사를 읽지 못했습니다.")
        return 1

    print(f"\n총 {len(entries)}건 → 필터링")
    kept = _filter_entries(
        entries,
        int(settings.get("lookback_hours", 24)),
        int(settings.get("min_title_length", 10)),
        posted_urls,
    )
    if not kept:
        print("[!] 조건을 만족하는 기사가 없습니다.")
        return 1

    clusters = _cluster(kept)
    print(f"\n{len(clusters)}개 사건으로 묶음")

    rows = _rank(clusters, ranking, keywords)[: args.count]
    articles = _assign_ids(rows, posted)

    print(f"\n상위 {len(articles)}건 선정")
    print("=" * 70)
    for art in articles:
        print(f"[{art['id']}] {art['title']}")
        print(
            f"    {art['source_name']} | 매체 {art['cluster_size']}곳 | "
            f"{art['published_at']}"
        )
        print(f"    {art['source_url']}")
        print(f"    요약: {art['summary'][:80]}")
    print("=" * 70)

    if args.dry_run:
        print("dry-run 이므로 저장하지 않습니다.")
        return 0

    ARTICLES_JSON.parent.mkdir(parents=True, exist_ok=True)
    ARTICLES_JSON.write_text(
        json.dumps(articles, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"{ARTICLES_JSON.relative_to(ROOT)} 에 {len(articles)}건 저장")
    return 0


if __name__ == "__main__":
    sys.exit(main())
