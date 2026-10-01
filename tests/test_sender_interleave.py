"""实时播放交错保持帧集合，并把连续损失分散到 FEC 组。"""
from collections import Counter
from pathlib import Path

from playwright.sync_api import sync_playwright


def test_interleaved_sequence_preserves_frames_and_limits_burst_loss():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto((Path(__file__).resolve().parents[1] / "sender.html").as_uri())
        for total in (1, 31, 32, 33, 2048, 4466):
            seq = page.evaluate("""total => {
                __sender.state.totalFrames = total;
                return __sender.buildPlaybackSequence(64);
            }""", total)
            assert sorted(x for x in seq if x >= 0) == list(range(total))
            assert sum(x == -1 for x in seq) == (total + 99) // 100
            assert sorted(-x - 2 for x in seq if x < -1) == list(
                range(total, total + 2 * ((total + 31) // 32)))
            assert seq[0] == -1
            if total == 2048:
                for start in range(len(seq) - 109):
                    groups = Counter(x // 32 if x >= 0 else (-x - 2 - total) // 2
                                     for x in seq[start:start + 110] if x != -1)
                    assert max(groups.values()) <= 2
        browser.close()
