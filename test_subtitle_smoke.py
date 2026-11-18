"""Смок-тест субтитров: генерирует тестовое видео, вшивает оба стиля ASS.
Запуск: python tests/test_subtitle_smoke.py (нужен ffmpeg).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import ChatMessage                      # noqa: E402
from subtitle import _chunks, build_chat_ass, build_tiktok_ass, burn_subtitles  # noqa: E402
from utils import ffprobe_meta, run_cmd             # noqa: E402

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "test_out")


def synth_clip(path, seconds=10):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", f"testsrc2=size=1280x720:rate=30:duration={seconds}",
           "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
           "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-shortest", str(path)]
    rc, _out, err = run_cmd(cmd, timeout=180)
    assert rc == 0, "ffmpeg не смог создать тестовое видео: " + err


def test_chunks():
    words = [{"start": i * 0.4, "end": i * 0.4 + 0.38, "word": w}
             for i, w in enumerate(["гол", "это", "же", "ахахах", "лучший", "момент",
                                    "за", "весь", "стрим", "кек"])]
    chunks = _chunks(words)
    assert chunks and all(1 <= len(c) <= 4 for c in chunks)
    assert sum(len(c) for c in chunks) == len(words)
    print(f"ok: разбивка на группы ({len(chunks)} групп)")


def test_style_two_tone_and_skip():
    """Стиль как на образце: белый верх / жёлтый низ; паузы баннера пропускаются."""
    words = [{"start": 1.0 + i * 0.5, "end": 1.0 + i * 0.5 + 0.45, "word": w}
             for i, w in enumerate(["сделай", "эти", "субтитры", "быстро", "гол",
                                    "момент", "ахахах", "кек"])]
    ass = os.path.join(OUT_DIR, "subs_style.ass")
    build_tiktok_ass(words, ass, width=1280, height=720, font="DejaVu Sans")
    with open(ass, "r", encoding="utf-8") as fh:
        body = fh.read()
    assert "\\N" in body, "должны быть две строки"
    assert "&HFFFFFF&" in body and "&H00FFFF&" in body, "нет пары белый/жёлтый"
    assert "СДЕЛАЙ" in body and "СУБТИТРЫ" in body  # верх 2 слова / низ 2 слова

    build_tiktok_ass(words, ass, width=1280, height=720, font="DejaVu Sans",
                     skip_intervals=[(3.5, 4.2)])   # вторая группа целиком в паузе
    with open(ass, "r", encoding="utf-8") as fh:
        body2 = fh.read()
    assert "КЕК" not in body2, "слова из паузы баннера не выброшены"
    assert "СДЕЛАЙ" in body2, "слова до паузы должны остаться"
    print("ok: стиль белый/жёлтый + пропуск паузы баннера")


def test_tiktok_burn():
    os.makedirs(OUT_DIR, exist_ok=True)
    video = os.path.join(OUT_DIR, "raw.mp4")
    synth_clip(video)
    words = [{"start": 0.3 + i * 0.7, "end": 0.3 + i * 0.7 + 0.6, "word": w}
             for i, w in enumerate(["ГОЛ", "ЭТО", "ПРОСТО", "АХАХАХ", "КАКОЙ",
                                    "МОМЕНТ", "ЛУЧШИЙ", "ЗА", "ВСЕ", "ВРЕМЯ"])]
    ass = os.path.join(OUT_DIR, "subs.ass")
    build_tiktok_ass(words, ass, width=1280, height=720, font="DejaVu Sans")
    assert os.path.getsize(ass) > 200, "файл субтитров пустой"
    out = os.path.join(OUT_DIR, "out_tiktok.mp4")
    burn_subtitles(video, ass, out, workdir=OUT_DIR)
    meta = ffprobe_meta(out)
    assert meta.get("height") == 720, f"битая высота: {meta}"
    assert meta.get("duration", 0) > 8, f"битая длительность: {meta}"
    print(f"ok: TikTok-субтитры вшиты -> {out}")


def test_chat_overlay_burn():
    os.makedirs(OUT_DIR, exist_ok=True)
    video = os.path.join(OUT_DIR, "raw.mp4")
    synth_clip(video)
    msgs = [ChatMessage(ts=1.0 + i, user=f"user{i}", text=t, platform="twitch")
            for i, t in enumerate(["АХАХАХАХ", "КЛИП КЛИП", "KEKW", "ЭТО ГОЛ",
                                   "ЛУЧШИЙ МОМЕНТ"])]
    ass = os.path.join(OUT_DIR, "subs_chat.ass")
    built = build_chat_ass(msgs, 10.0, ass, width=1280, height=720, font="DejaVu Sans")
    assert built and os.path.getsize(ass) > 200
    out = os.path.join(OUT_DIR, "out_chat.mp4")
    burn_subtitles(video, ass, out, workdir=OUT_DIR)
    assert os.path.getsize(out) > 10000
    print(f"ok: субтитры из чата вшиты -> {out}")


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    test_chunks()
    test_style_two_tone_and_skip()
    test_tiktok_burn()
    test_chat_overlay_burn()
    print("ВСЕ ТЕСТЫ СУБТИТРОВ ПРОШЛИ")


if __name__ == "__main__":
    main()
