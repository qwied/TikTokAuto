"""Тесты быстрого мониторинга: разбор ответов GQL и внутреннего API YouTube.

Запуск: python tests/test_monitor_fast.py  (сеть не нужна — только парсеры)
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import ChannelRef  # noqa: E402
from stream_monitor import (  # noqa: E402
    ChannelState, build_gql_query, parse_gql_response,
    yt_parse_resolve, yt_parse_streams_tab)


def test_gql_query_and_parse():
    q = build_gql_query(["lofigirl", "xqc"])
    assert 'u0: user(login: "lofigirl")' in q, q
    assert 'u1: user(login: "xqc")' in q, q
    assert "stream { type title viewersCount" in q, q

    chans = [ChannelRef(platform="twitch", id="lofigirl"),
             ChannelRef(platform="twitch", id="xqc"),
             ChannelRef(platform="twitch", id="ghost")]     # алиаса нет в ответе
    resp = {"data": {
        "u0": {"login": "lofigirl", "displayName": "Lofi Girl",
               "stream": {"type": "live", "title": "beats to relax",
                          "viewersCount": 315}},
        "u1": {"login": "xqc", "displayName": "xqc", "stream": None},
    }}
    out = parse_gql_response(resp, chans)
    assert out["twitch:lofigirl"] == (True, "beats to relax", 315), out
    assert out["twitch:xqc"] == (False, "", 0), out
    assert "twitch:ghost" not in out, out        # канал без ответа не трогаем
    print("ok: Twitch GQL — сборка запроса и разбор ответа (live/оффлайн/нет ответа)")


def test_gql_bad_viewers():
    chans = [ChannelRef(platform="twitch", id="abc")]
    resp = {"data": {"u0": {"login": "abc",
                            "stream": {"type": "live", "title": "t",
                                       "viewersCount": "мусор"}}}}
    out = parse_gql_response(resp, chans)
    assert out["twitch:abc"][0] is True and out["twitch:abc"][2] == 0, out
    print("ok: Twitch GQL — мусор в viewers не ломает разбор")


def test_yt_lockup_live():
    """Новый формат вкладки «Трансляции»: lockupViewModel + бейдж LIVE."""
    live_lockup = {
        "contentId": "4t4AQ15yj1M",
        "contentType": "LOCKUP_CONTENT_TYPE_VIDEO",
        "contentImage": {"thumbnailViewModel": {"overlays": [
            {"thumbnailBottomOverlayViewModel": {"badges": [
                {"thumbnailBadgeViewModel": {
                    "text": "LIVE",
                    "badgeStyle": "THUMBNAIL_OVERLAY_BADGE_STYLE_LIVE"}}]}},
        ]}},
        "metadata": {"lockupMetadataViewModel": {
            "title": {"content": "christmas lofi radio"}}},
    }
    vod_lockup = {
        "contentId": "DWcJFNfaw9c",
        "contentImage": {"thumbnailViewModel": {"overlays": [
            {"thumbnailBottomOverlayViewModel": {"badges": [
                {"thumbnailBadgeViewModel": {"text": "20,772:51:34",
                                             "badgeStyle": ""}}]}},
        ]}},
        "metadata": {"lockupMetadataViewModel": {
            "title": {"content": "старая запись"}}},
    }
    data = {"contents": [{"richGridRenderer": {"contents": [
        {"richItemRenderer": {"content": {"lockupViewModel": vod_lockup}}},
        {"richItemRenderer": {"content": {"lockupViewModel": live_lockup}}},
    ]}}]}
    vid, title = yt_parse_streams_tab(data)
    assert vid == "4t4AQ15yj1M", (vid, title)
    assert title == "christmas lofi radio", title
    vid2, _ = yt_parse_streams_tab(
        {"x": {"richItemRenderer": {"content": {"lockupViewModel": vod_lockup}}}})
    assert vid2 == "", vid2                    # только VOD — эфира нет
    vid3, _ = yt_parse_streams_tab({})
    assert vid3 == "", vid3                    # пустой ответ
    print("ok: YouTube lockupViewModel — live находится, VOD игнорируется, пусто -> ('', '')")


def test_yt_old_renderer_live():
    """Старые отрисовки: videoRenderer с liveBroadcastContent/badges."""
    data = {"contents": [{"videoRenderer": {
        "videoId": "abc12345678",
        "title": {"runs": [{"text": "стрим идёт"}]},
        "liveBroadcastContent": "live",
        "viewCountText": {"runs": [{"text": "1.2K watching"}]},
    }}]}
    vid, title = yt_parse_streams_tab(data)
    assert vid == "abc12345678" and title == "стрим идёт", (vid, title)
    off = {"contents": [{"videoRenderer": {
        "videoId": "abc12345678", "title": {"simpleText": "запись"},
        "liveBroadcastContent": "none",
        "viewCountText": {"simpleText": "10K views"}}}]}
    vid2, _ = yt_parse_streams_tab(off)
    assert vid2 == "", vid2
    print("ok: YouTube videoRenderer — live распознаётся, запись игнорируется")


def test_yt_resolve():
    data = {"responseContext": {}, "endpoint": {"browseEndpoint": {
        "browseId": "UCSJ4gkVC6NrvII8umztf0Ow"}}}
    assert yt_parse_resolve(data) == "UCSJ4gkVC6NrvII8umztf0Ow"
    assert yt_parse_resolve({}) == ""
    assert yt_parse_resolve({"error": {"code": 404}}) == ""
    print("ok: YouTube resolve_url — id канала извлекается, ошибки -> ''")


def test_channel_state():
    r = ChannelRef(platform="twitch", id="name")
    st = ChannelState.from_ref(r)
    assert st.viewers == 0 and st.is_live is False and not st.is_direct_video
    st.viewers, st.is_live = 777, True
    copy = ChannelState.from_ref(st)
    assert copy.viewers == 777 and copy.is_live is True, copy
    v = ChannelRef(platform="youtube", id="https://www.youtube.com/watch?v=abc12345678",
                   video_id="abc12345678",
                   video_url="https://www.youtube.com/watch?v=abc12345678")
    sv = ChannelState.from_ref(v)
    assert sv.is_direct_video, sv
    ch = ChannelRef(platform="youtube", id="https://www.youtube.com/@name")
    assert not ChannelState.from_ref(ch).is_direct_video
    print("ok: ChannelState — зрители копируются, прямая ссылка на видео определяется")


if __name__ == "__main__":
    test_gql_query_and_parse()
    test_gql_bad_viewers()
    test_yt_lockup_live()
    test_yt_old_renderer_live()
    test_yt_resolve()
    test_channel_state()
    print("ВСЕ ТЕСТЫ БЫСТРОГО МОНИТОРИНГА ПРОЙДЕНЫ")
