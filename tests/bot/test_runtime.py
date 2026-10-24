"""The standalone bot on a fake Discord: who may act, where, typed and slash commands, follow-ups, gates."""
import asyncio
from unittest import mock

import pytest

from corpus import fake_discord
from print_buddy.bot import commands, recent_links, runtime
from print_buddy.bot.adapter import parse_custom_id
from print_buddy.bot.router import Router
from print_buddy.core import prompts
from print_buddy.hermes import print_stop


def make(cfg, auto=False, router=None):
    discord = fake_discord.FakeDiscord(auto=auto, chat_id=int(cfg["discord"]["chat_id"]),
                                       operator_id=int(cfg["discord"]["allowed_user_ids"]))
    core = runtime.BotCore(discord.client, cfg, router=router)
    return discord, core


def run(coro):
    return asyncio.run(coro)


def printing(state="printing"):
    return {"state": state, "filename": "a.gcode", "layer": None, "total_layer": None,
            "progress": None, "print_duration": None}


async def press(discord, label):
    """Wait for the question with this button, then press it as the operator."""
    for _ in range(200):
        message = discord.last_question()
        if message is not None:
            for text, custom_id in discord.prompt_buttons(message):
                if text == label and prompts.is_open(parse_custom_id(custom_id)[0]):
                    return await discord.press(message, custom_id)
        await asyncio.sleep(0.01)
    raise AssertionError(f"no open question with {label!r}: {[m.content for m in discord.messages]}")


def test_router_runs_the_plugin_handlers_in_plugin_order_without_the_agent_receipt():
    router = Router()
    names = router.handler_names()
    assert names[:5] == ["maintenance_dispatch", "coldpull_dispatch", "typed_quantity_dispatch",
                         "upload_wait_dispatch", "model_link_dispatch"]
    assert "receive_ack" not in names and len(names) == 13
    assert router.platform_handlers["discord"], "listener button handlers were not registered"


def test_unmatched_text_without_an_llm_gets_the_command_list_and_starts_nothing(cfg):
    discord, core = make(cfg)
    with mock.patch("print_buddy.hermes.print_next.start") as start, \
            mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")):
        out = run(core.handle_message(discord.message("can you make my printer go brrr")))
    assert out == {"action": "skip", "reason": "command list"}
    assert discord.sent == [commands.help_text(cfg["discord"]["bot_name"])]
    start.assert_not_called()


@pytest.mark.parametrize("who", ["stranger", "bot", "elsewhere"])
def test_messages_from_strangers_bots_and_other_channels_are_ignored(cfg, who):
    discord, core = make(cfg)
    author = discord.operator
    channel = discord.channel
    if who == "stranger":
        author = fake_discord.FakeUser(999, "stranger")
    elif who == "bot":
        author = fake_discord.FakeUser(998, "other bot", bot=True)
    else:
        channel = discord.add_channel(555)
    assert run(core.handle_message(discord.message("/print-help", author=author, channel=channel))) is None
    assert discord.messages == []


def test_empty_allow_list_refuses_everyone(cfg):
    cfg["discord"]["allowed_user_ids"] = "200000000000000002"
    discord, core = make(cfg)
    core.adapter.allowed = set()
    assert run(core.handle_message(discord.message("/print-help"))) is None


def test_thread_in_the_print_channel_replies_in_the_thread(cfg):
    discord, core = make(cfg)
    thread = discord.add_channel(777, parent_id=discord.channel.id)
    run(core.handle_message(discord.message("/print-help", channel=thread)))
    assert discord.messages and all(m.channel is thread for m in discord.messages)


def test_typed_stop_asks_first_and_sends_one_cancel_only_after_the_button(cfg):
    discord, core = make(cfg)

    async def flow():
        task = asyncio.create_task(core.handle_message(discord.message("/print-stop")))
        await press(discord, print_stop.KEEP_LABEL)
        assert await task == {"action": "skip", "reason": "deterministic print stop handled"}
        cancel.assert_not_called()
        task = asyncio.create_task(core.handle_message(discord.message("/print-stop")))
        await press(discord, print_stop.STOP_LABEL)
        await task

    with mock.patch.object(print_stop, "status", return_value=printing()), \
            mock.patch.object(print_stop, "cancel", return_value=(True, "Stopped.")) as cancel:
        run(flow())
    cancel.assert_called_once_with("a.gcode")
    assert discord.sent[-1] == "Stopped." and "Kept printing. Nothing sent." in discord.sent


def test_a_stranger_cannot_press_the_operators_button(cfg):
    discord, core = make(cfg)
    stranger = fake_discord.FakeUser(999, "stranger")

    async def flow():
        task = asyncio.create_task(core.handle_message(discord.message("/print-stop")))
        for _ in range(200):
            if discord.last_question():
                break
            await asyncio.sleep(0.01)
        message = discord.last_question()
        stop_id = next(cid for label, cid in message.buttons() if label == print_stop.STOP_LABEL)
        clicked = await discord.press(message, stop_id, user=stranger)
        assert clicked.replies == [{"content": "Not allowed. Nothing changed.", "ephemeral": True}]
        assert prompts.is_open(parse_custom_id(stop_id)[0])
        await press(discord, print_stop.KEEP_LABEL)
        await task

    with mock.patch.object(print_stop, "status", return_value=printing()), \
            mock.patch.object(print_stop, "cancel") as cancel:
        run(flow())
    cancel.assert_not_called()


def test_a_second_click_on_an_answered_question_changes_nothing(cfg):
    discord, core = make(cfg)

    async def flow():
        task = asyncio.create_task(core.handle_message(discord.message("/print-stop")))
        first = await press(discord, print_stop.KEEP_LABEL)
        await task
        message = first.message
        again = await discord.press(message, f"pbq:{parse_custom_id(first.data['custom_id'])[0]}:0")
        return again

    with mock.patch.object(print_stop, "status", return_value=printing()), \
            mock.patch.object(print_stop, "cancel") as cancel:
        again = run(flow())
    assert again.replies[-1]["content"] == "That question already closed. Nothing changed."
    cancel.assert_not_called()


def test_slash_command_runs_as_its_typed_text(cfg):
    discord, core = make(cfg)
    interaction = fake_discord.FakeInteraction(discord, discord.operator, channel=discord.channel)
    with mock.patch("print_buddy.hermes.spool_sync.list_text", return_value="Slot 1: #7 black PLA"):
        run(core.run_slash(interaction, "spool", {"args": "list"}))
    assert interaction.replies[0] == {"content": "`/spool list`", "ephemeral": False}
    assert discord.sent[-1] == "Slot 1: #7 black PLA"


def test_slash_command_from_a_stranger_is_refused(cfg):
    discord, core = make(cfg)
    stranger = fake_discord.FakeUser(999, "stranger")
    interaction = fake_discord.FakeInteraction(discord, stranger, channel=discord.channel)
    with mock.patch("print_buddy.hermes.spool_sync.list_text") as listing:
        run(core.run_slash(interaction, "spool", {"args": "list"}))
    assert interaction.replies == [{"content": "Not allowed. Nothing changed.", "ephemeral": True}]
    listing.assert_not_called()


def test_typed_text_answers_only_the_askers_open_question(cfg):
    discord, core = make(cfg)
    asker = runtime.make_source(discord.operator.id, discord.channel.id)

    async def flow():
        cid = "typed-1"
        prompts.register(cid, runtime.session_key(asker), "How many copies?", ["1", "2"])
        await core.adapter.send_clarify(chat_id=str(discord.channel.id), question="How many copies?",
                                        choices=["1", "2"], clarify_id=cid, session_key="flow")
        other = runtime.make_source(424242, discord.channel.id)
        assert not await core.adapter.answer_typed(runtime.session_key(other), "2")
        out = await core.process(runtime.make_event(asker, "2", 1))
        assert out == {"action": "skip", "reason": "typed answer to an open question"}
        return await asyncio.to_thread(prompts.wait_for_response, cid, 1)

    assert run(flow()) == "2"


def test_print_it_follow_up_reuses_the_last_link_from_print_buddy_state(cfg):
    discord, core = make(cfg)
    url = "https://makerworld.com/en/models/931302-holder"
    seen = []

    async def fake_prepare(event, adapter, state, root, reuse=None, **kwargs):
        seen.append(event.text)
        return {"action": "skip", "reason": "test intake"}

    with mock.patch("print_buddy.core.prepared_intake.prepare_link", fake_prepare), \
            mock.patch("print_buddy.core.printer_state.load", return_value={}):
        run(core.handle_message(discord.message(url)))
        assert recent_links.recall(str(discord.channel.id)) == url
        run(core.handle_message(discord.message("print it")))
    assert seen[0] == url and seen[1].startswith(url) and seen[1].endswith("print it")


def test_dropped_model_file_is_saved_where_the_intake_reads_it(cfg, tmp_path):
    discord, core = make(cfg)
    model = tmp_path / "Bracket.stl"
    model.write_bytes(b"solid x\nendsolid x\n")
    note = tmp_path / "notes.txt"
    note.write_text("ignored")
    message = discord.message(attachments=[fake_discord.FakeAttachment(model), fake_discord.FakeAttachment(note)])
    paths = run(core.save_attachments(message))
    assert len(paths) == 1 and paths[0].endswith("_Bracket.stl")
    from print_buddy.core.prepared_intake import upload_job
    assert upload_job(paths[0])["title"] == "Bracket"
    import os
    stale = core.upload_dir / "doc_000000000000_old.stl"
    stale.write_text("x")
    os.utime(stale, (1, 1))
    run(core.save_attachments(discord.message()))
    assert not stale.exists() and all(os.path.exists(p) for p in paths)


def test_status_reports_no_hermes_modules(cfg):
    discord, core = make(cfg)
    status = core.status(True)
    assert status["mode"] == "standalone" and status["handlers"] == 13
    assert status["chat_id"] == cfg["discord"]["chat_id"] and status["allowed_users"] == 1


def test_listener_alert_buttons_are_wired_and_checked(cfg):
    """A listener alert button (posted over REST) is answered by the plugin's handler through our client."""
    discord, core = make(cfg)
    message = fake_discord.FakeMessage(discord.channel, discord.bot_user, "Paused", None)
    stranger = fake_discord.FakeUser(999, "stranger")
    with mock.patch("print_buddy.hermes.print_alert_buttons.handle_click") as handle_click:
        refused = run(discord.press(message, "pbalert:resume:abcd1234", user=stranger))
        handle_click.assert_not_called()
        assert refused.replies[0]["content"] == "Not allowed. Nothing changed."
        handle_click.return_value = "Resumed."
        with mock.patch("print_buddy.hermes.print_alert_buttons.take", return_value={"kind": "paused"}):
            clicked = run(discord.press(message, "pbalert:resume:abcd1234"))
    handle_click.assert_called_once()
    assert clicked.replies[-1]["content"] == "Resumed."


def test_discord_client_registers_commands_and_reports_connection(cfg):
    """The real discord.py Bot subclass, set up offline: slash commands, handlers, heartbeat, disconnects."""
    from print_buddy.bot import client, health

    async def flow():
        bot = client.PrintBuddyBot(cfg)
        with mock.patch.object(bot.tree, "sync", mock.AsyncMock(return_value=[])) as sync:
            await bot.setup_hook()
        try:
            sync.assert_awaited_once()
            assert sorted(c.name for c in bot.tree.get_commands()) == sorted(c.name for c in commands.COMMANDS)
            assert len(bot.extra_events["on_interaction"]) == 2  # prompt buttons + listener alert buttons
            await bot.on_ready()
            assert health.evaluate(health.read())[0] is True
            await bot.on_disconnect()
            assert bot.core.status(bot.connected)["connected"] is False
            await bot.on_resumed()
            assert bot.connected is True
        finally:
            for task in bot._background:
                task.cancel()

    run(flow())


@pytest.mark.parametrize("name", ["Print Bitch", "Print Buddy"])
def test_custom_name_is_used_in_help_and_wrong_channel_refusal(cfg, name):
    cfg["discord"]["bot_name"] = name
    discord, core = make(cfg)
    run(core.handle_message(discord.message("/print-help")))
    assert discord.sent[-1].startswith(f"**{name}**")
    elsewhere = discord.add_channel(555)
    interaction = fake_discord.FakeInteraction(discord, discord.operator, channel=elsewhere)
    assert run(core.run_slash(interaction, "print-help", {})) is None
    assert interaction.replies == [{"content": f"{name} only works in its print channel.", "ephemeral": True}]
