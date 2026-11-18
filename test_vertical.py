# -*- coding: utf-8 -*-
"""Вертикальный формат 9:16: геометрия баннера + реальная перекодировка."""

import os
import subprocess
import tempfile

import pytest

import vertical
from banner import banner_geometry
from utils import ffprobe_meta


def _sample_video(workdir, seconds=6, w=1280, h=720):
    """Тестовый «стрим» 16:9 с движущейся картинкой и звуком."""
    path = os.path.join(workdir, "sample.ts")
    rc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-nostdin",
         "-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate=30",
         "-f", "lavfi", "-i", "sine=frequency=440",
         "-t", str(seconds),
         "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest",
         path], capture_output=True).returncode
    assert rc == 0
    return path


def test_banner_geometry_vertical_area():
    """Баннер в вертикальном клипе занимает заданную долю площади (>=25%)."""
    bw, bh, real = banner_geometry(1080, 1920, 1920, 1080, area=0.30)
    assert bw <= 1080
    assert real >= 0.25


def test_blur_and_crop_graphs():
    g = vertical.blur_graph(1080, 1920)
    assert "overlay=x=(W-w)/2:y=(H-h)/2" in g
    c = vertical.crop_graph(1080, 1920)
    assert "crop=" in c and "scale=1080:1920" in c


@pytest.mark.parametrize("mode", ["blur", "crop"])
def test_make_vertical_real_encode(tmp_path, mode):
    src = _sample_video(str(tmp_path))
    out = vertical.make_vertical(src, "vert.mp4", str(tmp_path), mode=mode)
    meta = ffprobe_meta(out)
    assert int(meta["width"]) == 1080
    assert int(meta["height"]) == 1920
    assert abs(float(meta["duration"]) - 6.0) < 1.2
    # вертикальный клип тяжелее по высоте
    assert int(meta["height"]) > int(meta["width"])
