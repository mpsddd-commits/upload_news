"""대본을 음성으로 합성한다.

입력  : out/scripts.json, config.yml
출력  : out/audio/<id>.mp3, out/scripts.json 의 audio_path 갱신
쿼터  : 유튜브 API 미사용 (0유닛)

edge-tts 는 무료이고 API 키가 필요 없다. 마이크로소프트 음성 서비스를 쓰므로
네트워크 오류는 재시도한다.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import random
import shutil
import subprocess
import sys

import edge_tts
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONFIG_YML = ROOT / "config.yml"
SCRIPTS_JSON = ROOT / "out" / "scripts.json"
AUDIO_DIR = ROOT / "out" / "audio"


def _duration_seconds(path: pathlib.Path) -> float | None:
    """ffprobe 로 길이를 잰다. ffprobe 가 없으면 None."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        out = subprocess.run(
            [
                ffprobe, "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=nw=1:nk=1",
                str(path),
            ],
            capture_output=True, text=True, timeout=30, check=True,
        )
        return float(out.stdout.strip())
    except (subprocess.SubprocessError, ValueError):
        return None


async def _synthesize(text: str, target: pathlib.Path, cfg: dict) -> None:
    """음성 1건. 실패하면 재시도하고, 끝내 안 되면 마지막 예외를 올린다."""
    retries = int(cfg.get("retries", 3))
    backoff = float(cfg.get("backoff", 2.0))
    # 중간에 죽어서 잘린 mp3 가 남으면, 다음 실행이 "이미 있음"으로 보고 건너뛴다.
    # 임시 파일에 받아 두고 다 끝난 뒤에만 제자리로 옮긴다.
    partial = target.with_suffix(".part")

    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            communicate = edge_tts.Communicate(text, cfg["voice"], rate=cfg["rate"])
            await communicate.save(str(partial))
            if partial.stat().st_size == 0:
                raise RuntimeError("빈 파일이 생성됨")
        except Exception as exc:  # noqa: BLE001 — 네트워크·서비스 오류 전부 재시도
            last = exc
            partial.unlink(missing_ok=True)
            if attempt < retries:
                wait = backoff**attempt + random.random()
                print(
                    f"    재시도 {attempt}/{retries - 1} — "
                    f"{type(exc).__name__}: {exc} ({wait:.1f}초 대기)"
                )
                await asyncio.sleep(wait)
        else:
            partial.replace(target)
            return

    raise RuntimeError(f"{type(last).__name__}: {last}")


async def _run_all(items: list[dict], cfg: dict) -> tuple[list[str], list[dict]]:
    """순차 처리. 5건뿐이라 동시 요청으로 서비스를 두드릴 이유가 없다."""
    done, failures = [], []
    max_seconds = float(cfg.get("max_seconds", 60))

    for idx, item in enumerate(items, 1):
        target = AUDIO_DIR / f"{item['id']}.mp3"
        rel = target.relative_to(ROOT).as_posix()  # 윈도우에서도 슬래시로 통일
        print(f"[{idx}/{len(items)}] {item['id']} — {item['title'][:32]}")

        if target.exists() and target.stat().st_size > 0:
            item["audio_path"] = rel
            done.append(item["id"])
            print(f"    이미 있음, 건너뜀 ({rel})")
            continue

        try:
            await _synthesize(item["script"], target, cfg)
        except Exception as exc:  # noqa: BLE001
            print(f"    실패 (건너뜀): {exc}")
            failures.append({"id": item["id"], "error": str(exc)})
            continue

        item["audio_path"] = rel
        done.append(item["id"])

        seconds = _duration_seconds(target)
        size_kb = target.stat().st_size / 1024
        if seconds is None:
            print(f"    완료 ({size_kb:.0f}KB) — ffprobe 가 없어 길이는 확인 못 함")
        elif seconds > max_seconds:
            # 숏츠 상한을 넘었다. 죽이지는 않되 눈에 띄게 남긴다.
            print(f"    [경고] {seconds:.1f}초 — 숏츠 상한 {max_seconds:.0f}초 초과")
            print("           대본이 너무 깁니다. script_gen 의 분량 설정을 줄이세요.")
        else:
            print(f"    완료 {seconds:.1f}초 ({size_kb:.0f}KB)")

    return done, failures


def main() -> int:
    if not CONFIG_YML.exists():
        print(f"[!] {CONFIG_YML} 가 없습니다.")
        return 1
    if not SCRIPTS_JSON.exists():
        print(f"[!] {SCRIPTS_JSON} 가 없습니다. src.script_gen 을 먼저 실행하세요.")
        return 1

    config = yaml.safe_load(CONFIG_YML.read_text(encoding="utf-8")) or {}
    cfg = config.get("tts") or {}
    if not cfg.get("voice"):
        print("[!] config.yml 에 tts.voice 가 없습니다.")
        return 1
    cfg.setdefault("rate", "+0%")

    items = json.loads(SCRIPTS_JSON.read_text(encoding="utf-8"))
    if not items:
        print("[!] 대본이 없습니다.")
        return 1

    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    print(f"음성 {len(items)}건 합성 시작 (음성: {cfg['voice']}, 속도: {cfg['rate']})\n")

    done, failures = asyncio.run(_run_all(items, cfg))

    # 같은 파일을 읽어서 갱신한다 (CLAUDE.md 5장 데이터 계약)
    SCRIPTS_JSON.write_text(
        json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print(f"\n성공 {len(done)}건, 실패 {len(failures)}건")
    print(f"{SCRIPTS_JSON.relative_to(ROOT)} 의 audio_path 갱신 완료")
    if failures and not done:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
