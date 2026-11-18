"""Тесты рекламного баннера: геометрия (>=25% экрана), расписание показов
(раз в минуту) и смоук-сборка клипа с баннером и субтитрами одним проходом.
Запуск: python tests/test_banner.py (нужен ffmpeg).
"""

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from banner import (banner_geometry, compose, has_audio,  # noqa: E402
                            plan_intervals)
from subtitle import build_tiktok_ass     # noqa: E402
from utils import ffprobe_meta, run_cmd   # noqa: E402

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "test_out")


def synth_clip(path, seconds=12, size="1080x1920"):
    """Вертикальный тестовый клип со звуком (как настоящий из буфера)."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=30:duration={seconds}",
           "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
           "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-shortest", str(path)]
    rc, _out, err = run_cmd(cmd, timeout=180)
    assert rc == 0, "ffmpeg не смог создать тестовый клип: " + err


def synth_banner(path, seconds=3):
    """Тестовый баннер 1920x1008 (пропорции как у RubUP) со звуком."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", f"testsrc2=size=1920x1008:rate=30:duration={seconds}",
           "-f", "lavfi", "-i", f"sine=frequency=880:duration={seconds}",
           "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-shortest", str(path)]
    rc, _out, err = run_cmd(cmd, timeout=180)
    assert rc == 0, "ffmpeg не смог создать тестовый баннер: " + err


def test_geometry_rules():
    """Баннер 1920x1008 (RubUP) должен занимать >= 25% экрана в любых кадрах."""
    for w, h in ((1080, 1920), (1280, 720), (1920, 1080)):
        bw, bh, real = banner_geometry(w, h, 1920, 1008, area=0.30)
        assert bw <= w, f"баннер шире кадра: {bw} > {w}"
        assert real >= 0.25, f"площадь {real:.3f} меньше 25% при {w}x{h}"
        assert real <= 0.999, f"баннер закрыл весь экран: {real:.3f}"
        print(f"ok: кадр {w}x{h} -> баннер {bw}x{bh} = {real * 100:.1f}% площади")


def test_intervals_once_per_minute():
    """60-секундный клип — один показ; 130-секундный — три; короткий — целиком."""
    iv = plan_intervals(60.0, 6.1, start=8)
    assert len(iv) == 1, f"должен быть один показ, а не {len(iv)}"
    s, e = iv[0]
    assert abs(e - s - 6.1) < 0.01, "баннер должен проиграться целиком"

    iv = plan_intervals(130.0, 6.1, start=8)
    assert len(iv) == 3, f"для 130 сек нужно 3 показа, а не {len(iv)}"
    starts = [round(s, 1) for s, _e in iv]
    assert starts == [8.0, 68.0, 128.0], f"показы раз в минуту: {starts}"

    iv = plan_intervals(9.0, 6.1, start=8)          # клип короче: сдвиг к концу
    s, e = iv[0]
    assert e <= 9.0 and e - s > 5.5, f"короткий клип: баннер обрезан {iv}"
    print(f"ok: расписание показов (60с -> {plan_intervals(60.0, 6.1, start=8)}, "
          f"130с -> {starts})")


def test_has_audio():
    probe = os.path.join(OUT_DIR, "ban_probe.mp4")
    os.makedirs(OUT_DIR, exist_ok=True)
    synth_banner(probe)
    assert has_audio(probe), "тестовый баннер должен иметь звук"
    print("ok: has_audio определяет звуковую дорожку")


def test_compose_banner_and_subs():
    """Полная сборка: нарезка на паузе во время баннера + субтитры + звук.

    Проверяем три вещи:
      * длительность клипа НЕ меняется (стоп-кадр занимает ровно время баннера);
      * во время показа баннера картинка заморожена (кадры на 10.0 и 10.5 c
        внутри показа баннера 8..11 — одинаковые);
      * после окончания баннера эфир продолжается (кадр на 11.5 c уже другой).
    """
    os.makedirs(OUT_DIR, exist_ok=True)
    clip = os.path.join(OUT_DIR, "raw_vert.mp4")
    ban = os.path.join(OUT_DIR, "ban.mp4")
    if not os.path.exists(clip):
        synth_clip(clip)
    if not os.path.exists(ban):
        synth_banner(ban)

    words = [{"start": 0.3 + i * 0.8, "end": 0.3 + i * 0.8 + 0.7, "word": w}
             for i, w in enumerate(["ГОЛ", "ЭТО", "ПРОСТО", "АХАХАХ", "ЛУЧШИЙ",
                                    "МОМЕНТ", "ЗА", "ВСЁ", "ВРЕМЯ", "КЕК"])]
    ass = os.path.join(OUT_DIR, "subs_banner.ass")
    build_tiktok_ass(words, ass, width=1080, height=1920, font="DejaVu Sans",
                     skip_intervals=[(8.0, 14.1)])

    out = os.path.join(OUT_DIR, "out_banner.mp4")
    compose(clip, ban, ass, out, workdir=OUT_DIR,
            vcodec_args=["-c:v", "libx264", "-preset", "ultrafast", "-crf", "20"],
            cfg={"area": 30, "start": 8, "volume": 1.0})

    src_dur = ffprobe_meta(clip).get("duration") or 0
    meta = ffprobe_meta(out)
    assert meta.get("height") == 1920, f"битая высота: {meta}"
    assert abs((meta.get("duration") or 0) - src_dur) < 0.5, \
        f"длительность изменилась: было {src_dur}, стало {meta.get('duration')}"
    assert os.path.getsize(out) > 50000, "выходной файл подозрительно мал"

    def frame_md5(t):
        fp = os.path.join(OUT_DIR, f"fr_{t}.png")
        rc, _o, err = run_cmd(["ffmpeg", "-hide_banner", "-loglevel", "error",
                               "-y", "-ss", str(t), "-i", out, "-frames:v", "1",
                               "-vf", "crop=iw:ih/6:0:0", fp],
                              timeout=60)
        assert rc == 0, "кадр не извлёкся: " + err
        with open(fp, "rb") as fh:
            import hashlib
            h = hashlib.md5(fh.read()).hexdigest()
        os.remove(fp)
        return h

    h1, h2, h3 = frame_md5(10.0), frame_md5(10.5), frame_md5(11.5)
    assert h1 == h2, "во время баннера эфир должен быть заморожен (кадры разные!)"
    assert h3 != h1, "после баннера эфир должен продолжиться (кадр тот же!)"
    # субтитры, попадающие в паузу баннера, должны быть выброшены
    with open(ass, "r", encoding="utf-8") as fh:
        body = fh.read()
    for t in (8.0, 10.0, 14.0):
        for line in body.splitlines():
            if line.startswith("Dialogue:"):
                st = line.split(",")[1]
                mm, ss = st.split(":")[1:]
                cur = int(mm) * 60 + float(ss)
                if abs(cur - t) < 0.3:
                    raise AssertionError(f"субтитры попали в паузу баннера: {cur:.1f}c")
    print(f"ok: клип с баннером (пауза {8.0}-{14.1}с, кадр заморожен) и субтитрами "
          f"-> {out} ({os.path.getsize(out) // 1024} КБ)")


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    test_geometry_rules()
    test_intervals_once_per_minute()
    test_has_audio()
    test_compose_banner_and_subs()
    print("ВСЕ ТЕСТЫ БАННЕРА ПРОШЛИ")


if __name__ == "__main__":
    main()
