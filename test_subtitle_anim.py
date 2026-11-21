"""Караоке-субтитры без анимаций: жёлтое = говорят сейчас, белое = ещё нет.

Запуск: python tests/test_subtitle_anim.py (нужен ffmpeg для burn-теста).
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from subtitle import (_balanced_split, build_tiktok_ass,  # noqa: E402
                              burn_subtitles)
from utils import ffprobe_meta, run_cmd  # noqa: E402

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "test_out")


def words_sample():
    return [{"start": 1.0 + i * 0.5, "end": 1.0 + i * 0.5 + 0.45, "word": w}
            for i, w in enumerate(["сделай", "эти", "субтитры", "быстро", "гол",
                                   "момент", "ахахах", "кек"])]


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _style_colors(body):
    style = [ln for ln in body.splitlines() if ln.startswith("Style:")][0]
    cols = style.split(",")
    return cols[3], cols[4]          # PrimaryColour, SecondaryColour


def test_karaoke_no_animations():
    """Анимаций НЕТ (\\t, \\fad, \\fscx), подсветка только караоке \\k."""
    ass = os.path.join(OUT_DIR, "subs_anim.ass")
    build_tiktok_ass(words_sample(), ass, width=1080, height=1920,
                     font="DejaVu Sans")
    body = _read(ass)
    assert "\\t(" not in body, "анимации \\t должны быть убраны"
    assert "\\fad" not in body, "fade должен быть убран"
    assert "\\fscx" not in body and "\\fscy" not in body, "масштаб должен быть убран"
    assert "\\k" in body, "нет караоке-подсветки \\k"
    primary, secondary = _style_colors(body)
    assert primary == "&H00FFFF&", "PrimaryColour должен быть ЖЁЛТЫМ (сказанное)"
    assert secondary == "&HFFFFFF&", "SecondaryColour должен быть БЕЛЫМ (несказанное)"
    assert "FA8BA7" not in body, "розовая палитра должна быть убрана"
    print("ok: анимаций нет, караоке \\k, палитра бело-жёлтая")


def test_karaoke_fill_times():
    """Слово желтеет ровно в момент произнесения; строка появляется с первым
    словом и держится ~1 c после последнего."""
    words = [{"start": 1.0 + i * 0.5, "end": 1.0 + i * 0.5 + 0.45, "word": w}
             for i, w in enumerate(["сделай", "эти", "субтитры", "быстро"])]
    ass = os.path.join(OUT_DIR, "subs_karaoke.ass")
    build_tiktok_ass(words, ass, width=1080, height=1920, font="DejaVu Sans")
    body = _read(ass)
    m = re.search(r"Dialogue: 0,([^,]+),([^,]+),TikTok,,0,0,0,,(.+)", body)
    assert m, "нет события субтитров"
    t0, t1, text = m.groups()
    assert t0 == "0:00:01.00", f"строка должна появиться с первым словом: {t0}"
    assert text.startswith("{\\k1}СДЕЛАЙ"), f"первое слово — сразу «текущее»: {text}"
    assert "{\\k50}ЭТИ" in text and "{\\k50}СУБТИТРЫ" in text, \
        f"подсветка не по таймингам слов: {text}"
    assert t1 == "0:00:03.95", f"держится 1 c после конца последнего слова: {t1}"
    print("ok: подсветка по таймингам слов, появление/удержание верные")


def test_lines_do_not_overlap():
    """Строки сменяют друг друга без наложения (хвост не залезает на следующую)."""
    ass = os.path.join(OUT_DIR, "subs_two.ass")
    build_tiktok_ass(words_sample(), ass, width=1080, height=1920,
                     font="DejaVu Sans")
    body = _read(ass)
    evs = re.findall(r"Dialogue: 0,([^,]+),([^,]+),TikTok", body)

    def sec(t):
        h, m, s = t.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)

    assert len(evs) == 2, evs
    (a0, a1), (b0, b1) = evs
    assert sec(a1) <= sec(b0) + 1e-6, \
        f"строки перекрываются: {a1} > {b0}"
    print("ok: строки сменяются без наложения")


def test_balanced_split():
    caps = ["ААА", "ББББББББ", "ВВВВВ", "ГГ"]
    k = _balanced_split(caps)
    top = " ".join(caps[:k])
    bot = " ".join(caps[k:])
    # выбран тот разрыв, где разница длин минимальна из всех возможных
    diffs = [abs(len(" ".join(caps[:j])) - len(" ".join(caps[j:])))
             for j in range(1, len(caps))]
    assert abs(len(top) - len(bot)) == min(diffs), (top, bot)
    print(f"ok: ровный перенос («{top}» / «{bot}»)")


def test_single_word_karaoke():
    """Одиночное слово — жёлтое с первого кадра, без анимаций."""
    ass = os.path.join(OUT_DIR, "subs_anim1.ass")
    build_tiktok_ass([{"start": 0.2, "end": 1.0, "word": "ГОЛ"}],
                     ass, width=1080, height=1920, font="DejaVu Sans")
    body = _read(ass)
    m = re.search(r"Dialogue: 0,([^,]+),([^,]+),TikTok,,0,0,0,,(.+)", body)
    assert m, "нет события"
    t0, t1, text = m.groups()
    assert t0 == "0:00:00.20" and text == "{\\k1}ГОЛ", (t0, text)
    assert t1 == "0:00:02.00", f"удержание 1 c: {t1}"
    print("ok: одиночное слово — жёлтое с первого кадра")


def test_burn_vertical():
    os.makedirs(OUT_DIR, exist_ok=True)
    video = os.path.join(OUT_DIR, "raw_v.mp4")
    rc, _o, err = run_cmd(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=1080x1920:rate=30:duration=6",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         str(video)], timeout=180)
    assert rc == 0, err
    ass = os.path.join(OUT_DIR, "subs_v.ass")
    build_tiktok_ass(words_sample(), ass, width=1080, height=1920,
                     font="DejaVu Sans")
    out = os.path.join(OUT_DIR, "out_anim.mp4")
    burn_subtitles(video, ass, out, workdir=OUT_DIR)
    meta = ffprobe_meta(out)
    assert meta.get("width") == 1080 and meta.get("height") == 1920
    print(f"ok: караоке-субтитры вшиты в вертикаль -> {out}")


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    test_karaoke_no_animations()
    test_karaoke_fill_times()
    test_lines_do_not_overlap()
    test_balanced_split()
    test_single_word_karaoke()
    test_burn_vertical()
    print("ВСЕ ТЕСТЫ КАРАОКЕ-СУБТИТРОВ ПРОШЛИ")


if __name__ == "__main__":
    main()
