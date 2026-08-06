"""음성과 자막을 합쳐 세로 숏츠 영상을 만든다.

입력  : out/scripts.json, out/audio/<id>.mp3, config.yml
출력  : out/video/<id>.mp4, out/scripts.json 의 video_path 갱신
쿼터  : 유튜브 API 미사용 (0유닛)

ffmpeg 를 subprocess 로 직접 부른다 (moviepy 는 무겁고 느리다).
자막 문장은 필터 문자열에 넣지 않고 textfile 로 넘긴다. 뉴스 제목에는
따옴표·콜론·퍼센트가 흔한데, 필터 문법과 충돌해 렌더가 통째로 깨진다.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONFIG_YML = ROOT / "config.yml"
SCRIPTS_JSON = ROOT / "out" / "scripts.json"
VIDEO_DIR = ROOT / "out" / "video"
TMP_DIR = ROOT / "out" / "tmp"
BG_DIR = ROOT / "assets" / "bg"

# 설정된 폰트가 없을 때 찾아볼 후보. 운영(ubuntu-latest)은 나눔고딕이 깔려 있고,
# 로컬 확인용으로 윈도우·맥 경로를 함께 둔다.
FONT_FALLBACKS = (
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
    "/usr/share/fonts/truetype/nanum/NanumBarunGothic.ttf",
    "C:/Windows/Fonts/malgun.ttf",
    "C:/Windows/Fonts/NanumGothic.ttf",
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",
    "/Library/Fonts/AppleGothic.ttf",
)

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


def _hex_to_ffmpeg(color: str) -> str:
    """'#0B1B33' → '0x0B1B33'. 필터에서 '#'는 주석으로 오해될 수 있다."""
    c = color.strip()
    return "0x" + c[1:] if c.startswith("#") else c


def _resolve_font(cfg: dict) -> pathlib.Path:
    """설정된 폰트를 쓰되, 없으면 이 OS 의 한글 폰트로 대체한다.

    폰트를 못 찾으면 자막이 두부(□□□)로 나오므로 여기서 끊는다.
    (CLAUDE.md 8장 '영상 자막이 네모로 깨짐' 함정)
    """
    configured = str(cfg.get("font_path", "")).strip()
    if configured and pathlib.Path(configured).exists():
        return pathlib.Path(configured)

    for candidate in FONT_FALLBACKS:
        path = pathlib.Path(candidate)
        if path.exists():
            if configured:
                print(f"  [경고] 설정된 폰트가 없습니다: {configured}")
                print(f"         대체 폰트를 씁니다: {path}")
                print("         운영(ubuntu-latest)에서는 설정값이 그대로 쓰입니다.")
            return path

    raise RuntimeError(
        "한글 폰트를 찾지 못했습니다. config.yml 의 render.font_path 를 확인하세요. "
        "폰트 없이 렌더하면 자막이 전부 네모로 깨집니다."
    )


def _duration_seconds(path: pathlib.Path) -> float:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1",
            str(path),
        ],
        capture_output=True, text=True, timeout=30, check=True,
    )
    return float(out.stdout.strip())


def _wrap(text: str, max_chars: int) -> str:
    """어절 단위로 줄바꿈. 한 줄이 화면 폭을 넘지 않게 한다."""
    words, lines, current = text.split(), [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= max_chars or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return "\n".join(lines)


def _plan_captions(captions: list[str], duration: float) -> list[tuple[str, float, float]]:
    """자막을 음성 길이에 비례 분배한다.

    글자 수에 비례시킨다. 균등 분할하면 긴 자막이 읽기 전에 넘어간다.
    """
    weights = [max(len(c), 1) for c in captions]
    total = sum(weights)
    plan, cursor = [], 0.0
    for idx, (text, weight) in enumerate(zip(captions, weights)):
        span = duration * weight / total
        # 마지막 자막은 반올림 오차와 무관하게 영상 끝까지 유지한다.
        end = duration if idx == len(captions) - 1 else cursor + span
        plan.append((text, cursor, end))
        cursor = end
    return plan


def _background_input(cfg: dict, duration: float) -> tuple[list[str], str]:
    """배경 소스. (ffmpeg 입력 인자, 뒤에 붙일 필터) 를 돌려준다."""
    width, height, fps = cfg["width"], cfg["height"], cfg["fps"]
    mode = str(cfg.get("background", "gradient")).lower()

    if mode == "image":
        images = sorted(p for p in BG_DIR.glob("*") if p.suffix.lower() in IMAGE_EXTS)
        if images:
            src = images[0]
            args = ["-loop", "1", "-t", f"{duration:.3f}", "-i", str(src)]
            # 비율을 유지한 채 화면을 채우고 중앙을 잘라낸다.
            chain = (
                f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},fps={fps},format=yuv420p"
            )
            return args, chain
        print("  [경고] background=image 인데 assets/bg/ 에 이미지가 없습니다. 그라데이션으로 대체합니다.")
        mode = "gradient"

    if mode == "solid":
        color = _hex_to_ffmpeg(cfg.get("solid_color", "#000000"))
        src = f"color=c={color}:s={width}x{height}:r={fps}:d={duration:.3f}"
    else:  # gradient
        c0 = _hex_to_ffmpeg(cfg.get("gradient_from", "#0B1B33"))
        c1 = _hex_to_ffmpeg(cfg.get("gradient_to", "#123A5E"))
        # 좌상단에서 우하단으로 흐르는 대각선 그라데이션.
        src = (
            f"gradients=s={width}x{height}:r={fps}:c0={c0}:c1={c1}"
            f":x0=0:y0=0:x1={width}:y1={height}:duration={duration:.3f}"
        )
    return ["-f", "lavfi", "-i", src], "format=yuv420p"


def _stage_font(font: pathlib.Path) -> str:
    """폰트를 out/tmp 로 복사하고 리포 루트 기준 상대경로를 돌려준다.

    ffmpeg 필터 파서는 백슬래시를 두 번 처리해서, 윈도우 드라이브 콜론
    (C:/...)을 이스케이프하는 방식이 플랫폼마다 다르게 깨진다.
    경로에서 콜론을 아예 없애는 쪽이 확실하다. 자막 textfile 과 같은 이유다.
    """
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    staged = TMP_DIR / f"font{font.suffix.lower() or '.ttf'}"
    shutil.copyfile(font, staged)
    return staged.relative_to(ROOT).as_posix()


def _build_filter(plan, cfg: dict, font_rel: str, tmp: pathlib.Path, bg_chain: str) -> str:
    width, height = cfg["width"], cfg["height"]
    font_size = int(cfg.get("font_size", 64))

    # 숏츠 UI 가 하단을 가린다. 자막은 안전 영역 안에만 그린다.
    safe_top = height * float(cfg.get("safe_top_ratio", 0.18))
    safe_bottom = height * float(cfg.get("safe_bottom_ratio", 0.22))
    safe_height = height - safe_top - safe_bottom

    side_margin = int(width * 0.08)
    max_chars = max(int((width - side_margin * 2) / (font_size * 0.95)), 8)

    parts = [bg_chain]
    for idx, (text, start, end) in enumerate(plan):
        # 텍스트는 파일로 넘긴다. 따옴표·콜론·% 가 섞여도 필터가 깨지지 않는다.
        cap_file = tmp / f"cap_{idx:02d}.txt"
        # newline="\n" 을 반드시 준다. 윈도우에서는 기본값이 CRLF 로 바뀌는데,
        # drawtext 가 \r 을 빈 줄로 그려서 자막 사이가 한 줄씩 벌어진다.
        # 리눅스에서는 재현되지 않아 로컬과 운영이 다르게 보이는 함정이다.
        cap_file.write_text(_wrap(text, max_chars), encoding="utf-8", newline="\n")
        # ROOT 를 작업 디렉터리로 실행하므로 상대경로를 쓴다 (콜론 없음).
        rel = cap_file.relative_to(ROOT).as_posix()
        parts.append(
            "drawtext="
            f"fontfile={font_rel}:"
            f"textfile={rel}:"
            # %{...} 같은 표현식 확장을 끈다. 자막에 % 가 있어도 그대로 찍힌다.
            "expansion=none:"
            f"fontsize={font_size}:"
            "fontcolor=white:"
            "line_spacing=12:"
            # 여러 줄일 때 각 줄을 가운데로 모은다 (기본은 왼쪽 정렬).
            # flags 타입이라 값은 '+' 로 잇는다. 'MC' 처럼 붙여 쓰면 파싱 오류.
            "text_align=M+C:"
            "borderw=4:bordercolor=black@0.85:"
            # 가독성용 반투명 판. 그라데이션 위에서도 글자가 뜬다.
            "box=1:boxcolor=black@0.42:boxborderw=28:"
            "x=(w-text_w)/2:"
            f"y={safe_top:.0f}+({safe_height:.0f}-text_h)/2:"
            f"enable='between(t,{start:.3f},{end:.3f})'"
        )
    return ",".join(parts)


def _render_one(item: dict, cfg: dict, font_rel: str) -> pathlib.Path:
    audio = ROOT / item["audio_path"]
    if not audio.exists():
        raise FileNotFoundError(f"음성 파일 없음: {item['audio_path']}")

    duration = _duration_seconds(audio)
    captions = item.get("captions") or [item["title"]]
    plan = _plan_captions(captions, duration)

    tmp = TMP_DIR / item["id"]
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)

    bg_args, bg_chain = _background_input(cfg, duration)
    filters = _build_filter(plan, cfg, font_rel, tmp, bg_chain)
    target = VIDEO_DIR / f"{item['id']}.mp4"

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        *bg_args,
        "-i", str(audio),
        "-filter_complex", f"[0:v]{filters}[v]",
        "-map", "[v]", "-map", "1:a",
        "-c:v", "libx264", "-preset", "medium", "-crf", "21",
        "-pix_fmt", "yuv420p", "-r", str(cfg["fps"]),
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
        "-movflags", "+faststart",
        "-shortest",
        str(target),
    ]

    result = subprocess.run(
        cmd, cwd=ROOT, capture_output=True, text=True, timeout=600
    )
    shutil.rmtree(tmp, ignore_errors=True)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or "").strip()[:400] or "ffmpeg 실패")
    if not target.exists() or target.stat().st_size == 0:
        raise RuntimeError("출력 파일이 비어 있습니다")
    return target


def main() -> int:
    if not CONFIG_YML.exists():
        print(f"[!] {CONFIG_YML} 가 없습니다.")
        return 1
    if not SCRIPTS_JSON.exists():
        print(f"[!] {SCRIPTS_JSON} 가 없습니다. src.tts 를 먼저 실행하세요.")
        return 1
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        print("[!] ffmpeg / ffprobe 를 찾을 수 없습니다.")
        return 1

    config = yaml.safe_load(CONFIG_YML.read_text(encoding="utf-8")) or {}
    cfg = config.get("render") or {}
    cfg.setdefault("width", 1080)
    cfg.setdefault("height", 1920)
    cfg.setdefault("fps", 30)

    items = json.loads(SCRIPTS_JSON.read_text(encoding="utf-8"))
    todo = [i for i in items if i.get("audio_path")]
    if not todo:
        print("[!] audio_path 가 있는 항목이 없습니다. src.tts 를 먼저 실행하세요.")
        return 1

    try:
        font = _resolve_font(cfg)
    except RuntimeError as exc:
        print(f"[!] {exc}")
        return 1

    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    font_rel = _stage_font(font)
    print(
        f"영상 {len(todo)}건 렌더링 시작 "
        f"({cfg['width']}x{cfg['height']}, {cfg['fps']}fps, 배경: {cfg.get('background')})"
    )
    print(f"폰트: {font}\n")

    done, failures = [], []
    for idx, item in enumerate(todo, 1):
        print(f"[{idx}/{len(todo)}] {item['id']} — {item['title'][:32]}")
        try:
            target = _render_one(item, cfg, font_rel)
        except Exception as exc:  # noqa: BLE001 — 한 건 실패로 나머지를 막지 않는다
            print(f"    실패 (건너뜀): {exc}\n")
            failures.append({"id": item["id"], "error": str(exc)})
            continue

        item["video_path"] = target.relative_to(ROOT).as_posix()
        done.append(item["id"])
        size_mb = target.stat().st_size / (1024 * 1024)
        print(f"    완료 {item['video_path']} ({size_mb:.1f}MB)\n")

    shutil.rmtree(TMP_DIR, ignore_errors=True)

    SCRIPTS_JSON.write_text(
        json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print(f"성공 {len(done)}건, 실패 {len(failures)}건")
    print(f"{SCRIPTS_JSON.relative_to(ROOT)} 의 video_path 갱신 완료")
    if failures and not done:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
