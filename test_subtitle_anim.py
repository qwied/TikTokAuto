"""Караоке-субтитры без анимаций: жёлтое = говорят сейчас, белое = остальное.

Подход «одно событие на словный интервал» (auto-caption, MIT): каждое слово
получает своё Dialogue-событие с интервалом слова — субтитры не могут
появиться раньше речи, подсветка меняется ровно в момент произнесения.

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

YELLOW = "{\\c&H00FFFF&}"
WHITE_END = "{\\c&HFFFFFF&}"


def words_sample():
    return [{"start": 1.0 + i * 0.5, "end": 1.0 + i * 0.5 + 0.45, "word": w}
            for i, w in enumerate(["сделай", "эти", "субтитры", "быстро", "гол",
                                   "момент", "ахахах", "кек"])]


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _events(body):
    """[(start_sec, end_sec, text)] всех Dialogue-событий."""
    out = []
    for m in re.finditer(r"Dialogue: 0,([^,]+),([^,]+),TikTok,,0,0,0,,(.+)", body):
        s, e, text = m.groups()
        h1, m1, s1 = s.split(":")
        h2, m2, s2 = e.split(":")
        out.append((int(h1) * 3600 + int(m1) * 60 + float(s1),
                    int(h2) * 3600 + int(m2) * 60 + float(s2), text))
    return out


def _style_colors(body):
    style = [ln for ln in body.splitlines() if ln.startswith("Style:")][0]
    cols = style.split(",")
    return cols[3], cols[4]          # PrimaryColour, SecondaryColour


def test_karaoke_no_animations():
    """Анимаций НЕТ вообще: ни \\t/\\fad/\\fscx, ни накопительного \\k.

    Подсветка — только смена цвета активного слова (\\c) внутри словного
    интервала. Базовый цвет строки — белый (PrimaryColour), жёлтый —
    инлайн на активном слове.
    """
    ass = os.path.join(OUT_DIR, "subs_anim.ass")
    build_tiktok_ass(words_sample(), ass, width=1080, height=1920,
                     font="DejaVu Sans")
    body = _read(ass)
    assert "\\t(" not in body, "анимации \\t должны быть убраны"
    assert "\\fad" not in body, "fade должен быть убран"
    assert "\\fscx" not in body and "\\fscy" not in body, "масштаб должен быть убран"
    assert "\\k" not in body, "накопительный \\k должен быть заменён словными событиями"
    primary, secondary = _style_colors(body)
    assert primary == "&HFFFFFF&", "базовый цвет строки должен быть БЕЛЫМ"
    assert secondary == "&HFFFFFF&", "SecondaryColour не используется — белый"
    evs = _events(body)
    assert evs, "нет событий субтитров"
    for _s, _e, text in evs:
        assert text.count(YELLOW) == 1, \
            f"ровно одно жёлтое слово на событие: {text}"
        assert text.count(WHITE_END) == 1, \
            f"жёлтая заливка должна сниматься после слова: {text}"
    assert "FA8BA7" not in body, "розовая палитра должна быть убрана"
    print("ok: анимаций нет, жёлтое — только слово, которое говорят сейчас")


def test_karaoke_fill_times():
    """Тайминг события = интервал слова: строка появляется РОВНО с первым
    словом, жёлтое слово сменяется по словам, после последнего — 0.35 c."""
    words = [{"start": 1.0 + i * 0.5, "end": 1.0 + i * 0.5 + 0.45, "word": w}
             for i, w in enumerate(["сделай", "эти", "субтитры", "быстро"])]
    ass = os.path.join(OUT_DIR, "subs_karaoke.ass")
    build_tiktok_ass(words, ass, width=1080, height=1920, font="DejaVu Sans")
    evs = _events(_read(ass))
    assert len(evs) == 4, evs
    (s0, _e0, t0), (_s1, _e1, t1), (_s2, _e2, t2), (_s3, e3, t3) = evs
    assert abs(s0 - 1.0) < 1e-6, f"строка появляется с первым словом: {s0}"
    assert t0.startswith(YELLOW + "СДЕЛАЙ" + WHITE_END), \
        f"первое слово — сразу жёлтое: {t0}"
    assert YELLOW + "ЭТИ" in t1 and YELLOW + "СУБТИТРЫ" in t2, \
        f"жёлтое слово меняется по словам: {t1} / {t2}"
    assert abs(e3 - (2.95 + 0.35)) < 1e-6, \
        f"хвост 0.35 c после последнего слова: {e3}"
    print("ok: события строго по словным интервалам")


def test_silence_no_subtitles():
    """ГЛАВНЫЙ РЕГРЕСС ПОЛЬЗОВАТЕЛЯ («появляются заранее»): пауза > 0.6 c
    рвёт строку — в тишине субтитров НЕТ ни одного события."""
    words = [{"start": 1.0, "end": 1.4, "word": "стоп"},
             {"start": 5.0, "end": 5.5, "word": "поехали"}]
    ass = os.path.join(OUT_DIR, "subs_silence.ass")
    build_tiktok_ass(words, ass, width=1080, height=1920, font="DejaVu Sans")
    evs = _events(_read(ass))
    assert len(evs) == 2, evs
    for s, e, _t in evs:
        assert not (e > 1.75 and s < 5.0), \
            f"событие висит в тишине: {s}-{e}"
    print("ok: в паузе субтитров нет")


def test_lines_do_not_overlap():
    """Ни одно событие не перекрывается другим (хвост обрезается по началу
    следующей строки — иначе libass расталкивает события «стопкой»)."""
    ass = os.path.join(OUT_DIR, "subs_two.ass")
    build_tiktok_ass(words_sample(), ass, width=1080, height=1920,
                     font="DejaVu Sans")
    evs = _events(_read(ass))
    assert evs, "нет событий"
    evs_sorted = sorted(evs, key=lambda x: x[0])
    for a, b in zip(evs_sorted, evs_sorted[1:]):
        assert a[1] <= b[0] + 1e-6, \
            f"события перекрываются: {a[0]:.2f}-{a[1]:.2f} и {b[0]:.2f}-{b[1]:.2f}"
    print(f"ok: {len(evs)} событий, перекрытий нет")


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
    evs = _events(_read(ass))
    assert len(evs) == 1, evs
    s, e, text = evs[0]
    assert abs(s - 0.2) < 1e-6, s
    assert text == YELLOW + "ГОЛ" + WHITE_END, text
    assert abs(e - (1.0 + 0.35)) < 1e-6, f"хвост 0.35 c: {e}"
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
    test_silence_no_subtitles()
    test_lines_do_not_overlap()
    test_balanced_split()
    test_single_word_karaoke()
    test_burn_vertical()
    print("ВСЕ ТЕСТЫ КАРАОКЕ-СУБТИТРОВ ПРОШЛИ")


if __name__ == "__main__":
    main()
