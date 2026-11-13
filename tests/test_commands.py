"""Raw commands and resource lists in MixerClient (FakeMixer, replies injected)."""

import asyncio


async def reply_later(mixer, line, delay=0.02):
    await asyncio.sleep(delay)
    mixer.inject(line)


async def test_send_command(mixer):
    mixer.send_command("MEDIA_PLAY")
    assert mixer.sent == ["MEDIA_PLAY"]


async def test_request_list_resolves_on_reply(mixer):
    asyncio.create_task(reply_later(mixer, "SNAPSHOTLIST^Default^* Init *^Oooo^test"))
    items = await mixer.request_list("SNAPSHOTLIST^Default", "SNAPSHOTLIST^Default")
    assert items == ["* Init *", "Oooo", "test"]
    assert mixer.sent == ["SNAPSHOTLIST^Default"]
    assert mixer.lists["SNAPSHOTLIST^Default"] == items


async def test_request_list_empty_reply_and_timeout(mixer):
    asyncio.create_task(reply_later(mixer, "PLISTS^"))
    assert await mixer.request_list("MEDIA_GET_PLISTS", "PLISTS") == []
    # the real mixer stays silent for an empty cue list
    assert await mixer.request_list("CUELIST^Default", "CUELIST^Default", timeout=0.1) == []


async def test_list_replies_are_not_state(mixer):
    mixer.inject("SHOWLIST^Default^cal")
    assert mixer.lists["SHOWLIST"] == ["Default", "cal"]
    assert "SHOWLIST" not in mixer.state
