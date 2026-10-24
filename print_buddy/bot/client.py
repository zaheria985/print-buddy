"""The discord.py client for the standalone bot: gateway events, slash commands, heartbeat."""
import asyncio
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands as ext_commands

from . import commands, health
from .runtime import BotCore

logger = logging.getLogger(__name__)

HEARTBEAT_S = 15


def _slash(core_ref, command):
    """One app command whose callback hands its options to BotCore.run_slash."""
    names = [o.name for o in command.options]

    async def run(interaction, values):
        await core_ref().run_slash(interaction, command.name, values)

    if not names:
        async def callback(interaction: discord.Interaction):
            await run(interaction, {})
    elif len(names) == 1:
        async def callback(interaction: discord.Interaction, first: Optional[str] = None):
            await run(interaction, {names[0]: first})
    else:
        async def callback(interaction: discord.Interaction, first: Optional[str] = None,
                           second: Optional[str] = None):
            await run(interaction, {names[0]: first, names[1]: second})
    params = ["first", "second"][:len(names)]
    if params:
        callback = app_commands.rename(**dict(zip(params, names)))(callback)
        callback = app_commands.describe(**{p: o.description[:100]
                                            for p, o in zip(params, command.options)})(callback)
        for param, option in zip(params, command.options):
            if option.choices:
                callback = app_commands.choices(**{param: [app_commands.Choice(name=c, value=c)
                                                           for c in option.choices]})(callback)
    return app_commands.Command(name=command.name, description=command.description[:100], callback=callback)


class PrintBuddyBot(ext_commands.Bot):
    def __init__(self, cfg):
        intents = discord.Intents.default()
        intents.message_content = True  # typed replies, links and "print it" follow-ups
        super().__init__(command_prefix=ext_commands.when_mentioned, intents=intents, help_command=None)
        self.cfg = cfg
        self.core = None
        self._background = []
        self.connected = False  # gateway session up: set on ready/resume, cleared on disconnect

    async def setup_hook(self):
        self.core = BotCore(self, self.cfg)
        for command in commands.COMMANDS:
            self.tree.add_command(_slash(lambda: self.core, command))
        if self.cfg["bot"].get("sync_commands", True):
            try:
                synced = await self.tree.sync()
                logger.info("registered %d slash commands", len(synced))
            except discord.HTTPException as exc:
                logger.warning("slash command registration failed (typed commands still work): %r", exc)
        self._background.append(asyncio.create_task(self._heartbeat()))
        port = int(self.cfg["bot"].get("health_port") or 0)
        if port:
            server = await health.serve(port, lambda: self.core.status(self.connected))
            self._background.append(server)
            logger.info("health check on :%d/healthz", port)

    async def _heartbeat(self):
        while not self.is_closed():
            try:
                health.write(self.core.status(self.connected))
            except Exception:
                logger.exception("heartbeat write failed")
            await asyncio.sleep(HEARTBEAT_S)

    async def on_disconnect(self):
        self.connected = False

    async def on_resumed(self):
        self.connected = True

    async def on_ready(self):
        self.connected = True
        logger.info("logged in as %s; print channel %s; %d allowed user(s); handlers: %s",
                    self.user, self.core.chat_id, len(self.core.adapter.allowed),
                    ", ".join(self.core.router.handler_names()))
        health.write(self.core.status(True))

    async def on_message(self, message):
        try:
            await self.core.handle_message(message)
        except Exception:
            logger.exception("message handling failed")

    async def close(self):
        for item in self._background:
            if isinstance(item, asyncio.Task):
                item.cancel()
            else:
                item.close()
        tasks = [item for item in self._background if isinstance(item, asyncio.Task)]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for item in self._background:
            if not isinstance(item, asyncio.Task):
                await item.wait_closed()
        self._background.clear()
        self.connected = False
        try:
            if self.core is not None:
                health.write(self.core.status(False))
        except Exception:
            pass
        await super().close()
