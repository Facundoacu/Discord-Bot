import asyncio
import logging
import os

import discord
from discord import app_commands
from discord.ext import commands

import secret_store

secret_store.load()  # antes de importar módulos que lean la config

TOKEN = secret_store.get("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("Falta DISCORD_TOKEN en el archivo .env")

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
secret_store.install_log_redaction()
log = logging.getLogger("bot")


class MusicBot(commands.Bot):
    def __init__(self) -> None:
        # Solo slash commands: no hace falta el intent privilegiado de
        # contenido de mensajes. voice_states ya viene en los intents por defecto.
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=discord.Intents.default(),
            help_command=None,
            # Los títulos vienen de internet: que nunca puedan hacer @everyone.
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def setup_hook(self) -> None:
        await self.load_extension("cogs.music")
        self.tree.on_error = self.on_tree_error

        dev_guild = os.getenv("DEV_GUILD_ID")
        if dev_guild:
            # Sync a un solo servidor: los cambios se ven al instante (para desarrollar).
            guild = discord.Object(id=int(dev_guild))
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
        else:
            synced = await self.tree.sync()
        log.info("Slash commands sincronizados: %d", len(synced))

    async def on_ready(self) -> None:
        log.info("Conectado como %s (id: %s) en %d servidores", self.user, self.user.id, len(self.guilds))

    async def on_tree_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        log.error("Error en /%s", interaction.command.name if interaction.command else "?", exc_info=error)
        message = "Algo salió mal con ese comando."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


async def main() -> None:
    async with MusicBot() as bot:
        await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
