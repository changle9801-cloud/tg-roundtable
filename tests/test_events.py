"""Problems the owner should hear about: kept across restarts, repeats merged, downtime noticed."""

from __future__ import annotations

import logging

from roundtable.events import ALERT_EVERY, DOWN_AFTER, EventLog


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def log_with(events: EventLog) -> logging.Logger:
    logger = logging.getLogger("roundtable.room.test")
    logger.handlers[:] = [events]
    logger.propagate = False
    return logger


def test_warnings_are_kept_and_repeats_merged(tmp_path):
    clock = Clock()
    events = EventLog(tmp_path, now=clock)
    logger = log_with(events)
    logger.info("Codex: 早")  # chat lines are not problems
    logger.warning("codex failed: timed out after 180 s")
    clock.t += 60
    logger.warning("codex failed: timed out after 181 s")  # same problem, other numbers
    logger.error("round failed in chat", exc_info=(ValueError, ValueError("bad"), None))
    events.save()

    again = EventLog(tmp_path, now=clock)  # after a restart
    items = again.recent()
    assert [e["level"] for e in items] == ["error", "warning"]
    assert items[1]["count"] == 2 and items[1]["source"] == "room.test"
    assert items[0]["text"].endswith("ValueError: bad")


def test_downtime_is_noticed_at_startup(tmp_path):
    clock = Clock()
    events = EventLog(tmp_path, now=clock)
    events.started()
    clock.t += 60
    events.beat()
    clock.t += DOWN_AFTER - 10  # a quick restart is not downtime
    EventLog(tmp_path, now=clock).started()
    assert EventLog(tmp_path, now=clock).recent() == []

    clock.t += 3600 * 10
    later = EventLog(tmp_path, now=clock)
    later.started()
    [down] = later.recent()
    assert down["level"] == "down" and "没在运行" in down["text"]
    assert later.alert_text() == ""  # back up again: no need to message the owner about it


def test_the_owner_hears_about_errors_or_several_problems(tmp_path):
    clock = Clock()
    events = EventLog(tmp_path, now=clock)
    events.add("warning", "room", "deepseek failed: 503")
    assert events.alert_text() == ""  # one hiccup
    events.add("warning", "life", "生活活动失败")
    events.add("warning", "drawing", "画图失败")
    text = events.alert_text()
    assert "画图失败" in text and "管理页面" in text

    events.mark_alerted()
    clock.t += 1
    events.add("error", "room", "round failed")
    assert events.alert_text() == ""  # not more than every half hour
    clock.t += ALERT_EVERY
    assert "round failed" in events.alert_text()
    events.mark_alerted()
    clock.t += ALERT_EVERY
    assert events.alert_text() == ""  # nothing new since


def test_clear(tmp_path):
    events = EventLog(tmp_path)
    events.add("warning", "room", "x")
    events.clear()
    assert EventLog(tmp_path).recent() == []
