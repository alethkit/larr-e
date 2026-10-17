"""Losing the Pioreactor must reach a person.

The MQTT broker runs on the Pi, so when the Pi goes away the listener -> broker -> Slack alert path
goes with it. The Slack bot watches its own connection to the Pi and posts directly to Slack.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from linkwatch import LinkWatch  # noqa: E402


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def make(grace: float = 20.0):
    clock, events = Clock(), []
    watch = LinkWatch(grace_s=grace, on_down=lambda s: events.append(("down", round(s))),
                      on_up=lambda s: events.append(("up", round(s))), clock=clock)
    return watch, clock, events


def test_no_alert_before_first_connection_or_while_connected():
    watch, clock, events = make()
    clock.t += 300
    watch.check()  # never connected yet: startup, not an outage
    watch.connected()
    clock.t += 300
    watch.check()
    assert events == []


def test_outage_longer_than_grace_alerts_once_then_recovers():
    watch, clock, events = make()
    watch.connected()
    watch.disconnected()
    clock.t += 10
    watch.check()
    assert events == []  # still inside the grace period
    clock.t += 15
    watch.check()
    clock.t += 60
    watch.check()  # no repeat while still down
    assert events == [("down", 25)]
    watch.connected()
    assert events == [("down", 25), ("up", 85)]


def test_blip_inside_grace_stays_quiet():
    watch, clock, events = make()
    watch.connected()
    watch.disconnected()
    clock.t += 5
    watch.connected()
    clock.t += 100
    watch.check()
    assert events == []


def test_repeated_disconnect_callbacks_keep_the_first_time():
    watch, clock, events = make()
    watch.connected()
    watch.disconnected()
    clock.t += 15
    watch.disconnected()  # paho reports every failed reconnect attempt
    clock.t += 10
    watch.check()
    assert events == [("down", 25)]


# ------------------------------------------------------------------ the Slack bot posts it


class FakeSlack:
    def __init__(self) -> None:
        self.posts: list[dict] = []

    def chat_postMessage(self, **kw):
        self.posts.append(kw)
        return {"ts": "1.1", "channel": kw.get("channel")}


def make_bot(tmp_path, channel="CALERTS"):
    import dataclasses
    from slack_bolt import App
    from config import Settings
    import slack_bot

    base = Settings()
    cfg = dataclasses.replace(
        base,
        slack=dataclasses.replace(base.slack, alert_channel=channel),
        output=dataclasses.replace(base.output, runtime_dir=tmp_path, state_path=tmp_path / "s.json",
                                   alerts_path=tmp_path / "a.jsonl", audit_path=tmp_path / "audit.jsonl",
                                   plots_dir=tmp_path / "plots"),
    )
    bot = slack_bot.LarreBot(cfg, app=App(token="xoxb-test", token_verification_enabled=False), bot_user_id="UBOT")
    bot.client = FakeSlack()
    return bot


def test_bot_posts_lost_contact_and_recovery_to_the_alert_channel(tmp_path):
    bot = make_bot(tmp_path)
    bot.post_link_down(25)
    bot.post_link_up(130)
    down, up = bot.client.posts
    assert down["channel"] == up["channel"] == "CALERTS"
    assert "Lost contact" in down["text"] and "25 s" in down["text"]
    assert "Back in contact" in up["text"] and "2 min" in up["text"]
    assert "weren't recorded" in up["text"]  # the listener doesn't backfill the gap


def test_bot_without_alert_channel_posts_nothing(tmp_path):
    bot = make_bot(tmp_path, channel=None)
    bot.post_link_down(25)
    assert bot.client.posts == []
