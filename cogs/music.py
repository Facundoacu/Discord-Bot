"""Capa de Discord: comandos, botones y conexión de voz.

Toda la lógica de reproducción vive en `player/` (testeada sin Discord). Acá
solo se traduce entre eso y discord.py.
"""
from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

import config
from player.engine import PlayerEngine
from player.errors import SourceError
from player.queue import LoopMode
from player.sources import build_tracks, ensure_resolved
from player.text import LOOP_LABELS, escape_md, format_duration, format_queue, truncate
from player.track import Track

log = logging.getLogger("music")

FFMPEG_BEFORE_OPTIONS = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FFMPEG_OPTIONS = "-vn"


def _label(track: Track) -> str:
    return escape_md(truncate(track.title))


def track_embed(track: Track, header: str = "Sonando ahora") -> discord.Embed:
    embed = discord.Embed(
        title=truncate(track.title, 256),
        url=track.webpage_url or None,
        color=discord.Color.blurple(),
    )
    embed.set_author(name=header)
    embed.add_field(name="Duración", value=format_duration(track.duration))
    embed.set_footer(text=f"Pedido por {truncate(track.requester, 40)}")
    return embed


class DiscordBackend:
    """Adapta el VoiceClient de discord.py a la interfaz que usa el motor."""

    def __init__(self, guild: discord.Guild):
        self._guild = guild

    @property
    def _vc(self) -> discord.VoiceClient | None:
        return self._guild.voice_client  # type: ignore[return-value]

    def is_connected(self) -> bool:
        vc = self._vc
        return vc is not None and vc.is_connected()

    def is_playing(self) -> bool:
        vc = self._vc
        return vc is not None and vc.is_playing()

    def is_paused(self) -> bool:
        vc = self._vc
        return vc is not None and vc.is_paused()

    def play(self, track: Track, volume: float, after) -> None:
        source = discord.FFmpegPCMAudio(
            track.stream_url,
            before_options=FFMPEG_BEFORE_OPTIONS,
            options=FFMPEG_OPTIONS,
        )
        self._vc.play(discord.PCMVolumeTransformer(source, volume=volume), after=after)

    def stop(self) -> None:
        if self._vc is not None:
            self._vc.stop()

    def pause(self) -> None:
        if self._vc is not None:
            self._vc.pause()

    def resume(self) -> None:
        if self._vc is not None:
            self._vc.resume()

    def set_volume(self, volume: float) -> None:
        vc = self._vc
        if vc is not None and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = volume


class GuildSession:
    """Estado de un servidor: motor de reproducción + mensajes + timers."""

    def __init__(self, cog: Music, guild: discord.Guild):
        self.cog = cog
        self.guild = guild
        self.text_channel: discord.abc.Messageable | None = None
        self._controls_message: discord.Message | None = None
        self._controls_view: discord.ui.View | None = None
        self._idle_task: asyncio.Task | None = None
        self._alone_task: asyncio.Task | None = None
        self.engine = PlayerEngine(
            backend=DiscordBackend(guild),
            resolver=ensure_resolved,
            notify=self._notify,
            on_track_start=self._announce,
            on_idle=self._on_idle,
            max_queue=config.MAX_QUEUE_SIZE,
            volume=config.DEFAULT_VOLUME,
        )

    # --- voz ------------------------------------------------------------

    async def ensure_connected(self, channel: discord.VoiceChannel) -> None:
        vc = self.guild.voice_client
        if vc is not None and not vc.is_connected():
            await vc.disconnect(force=True)  # cliente viejo/colgado
            vc = None
        if vc is None:
            await channel.connect(self_deaf=True)
        elif vc.channel.id != channel.id:
            await vc.move_to(channel)
        self.cancel_idle()
        self.check_alone()

    async def disconnect(self) -> None:
        self.cleanup()
        vc = self.guild.voice_client
        if vc is not None:
            await vc.disconnect()  # también corta el audio

    def cleanup(self) -> None:
        """Libera todo (cola, timers, botones). Idempotente."""
        self.cancel_idle()
        self._cancel(self._alone_task)
        self._alone_task = None
        self.engine.shutdown()
        self._schedule_remove_controls()

    # --- timers ---------------------------------------------------------

    @staticmethod
    def _cancel(task: asyncio.Task | None) -> None:
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()

    def cancel_idle(self) -> None:
        self._cancel(self._idle_task)
        self._idle_task = None

    def schedule_idle_disconnect(self) -> None:
        self.cancel_idle()
        self._idle_task = asyncio.create_task(
            self._disconnect_later(config.IDLE_TIMEOUT_SECONDS, only_if_idle=True)
        )

    def check_alone(self) -> None:
        """Si no quedan personas en el canal, programa la salida; si hay, la cancela."""
        vc = self.guild.voice_client
        if vc is None or not vc.is_connected():
            self._cancel(self._alone_task)
            self._alone_task = None
            return
        if any(not m.bot for m in vc.channel.members):
            self._cancel(self._alone_task)
            self._alone_task = None
        elif self._alone_task is None or self._alone_task.done():
            self._alone_task = asyncio.create_task(
                self._disconnect_later(config.ALONE_TIMEOUT_SECONDS, only_if_idle=False)
            )

    async def _disconnect_later(self, delay: float, *, only_if_idle: bool) -> None:
        await asyncio.sleep(delay)
        if only_if_idle and self.engine.busy:
            return
        log.info("Saliendo del canal de voz (guild %s)", self.guild.id)
        await self.disconnect()

    # --- mensajes -------------------------------------------------------

    async def _notify(self, text: str) -> None:
        if self.text_channel is None:
            return
        try:
            await self.text_channel.send(text)
        except discord.HTTPException:
            log.warning("No pude enviar un aviso al canal de texto", exc_info=True)

    async def _announce(self, track: Track) -> None:
        self.cancel_idle()
        old_message, old_view = self._controls_message, self._controls_view
        self._controls_message = self._controls_view = None
        if self.text_channel is not None:
            view = PlayerControls(self)
            try:
                self._controls_message = await self.text_channel.send(
                    embed=track_embed(track), view=view
                )
                self._controls_view = view
            except discord.HTTPException:
                log.warning("No pude anunciar el tema actual", exc_info=True)
        await self._strip_controls(old_message, old_view)

    async def _on_idle(self) -> None:
        self._schedule_remove_controls()
        self.schedule_idle_disconnect()

    @staticmethod
    async def _strip_controls(message: discord.Message | None, view: discord.ui.View | None) -> None:
        if view is not None:
            view.stop()
        if message is not None:
            try:
                await message.edit(view=None)
            except discord.HTTPException:
                pass  # mensaje borrado o sin permisos: no importa

    def _schedule_remove_controls(self) -> None:
        message, view = self._controls_message, self._controls_view
        self._controls_message = self._controls_view = None
        if message is None and view is None:
            return
        try:
            asyncio.get_running_loop().create_task(self._strip_controls(message, view))
        except RuntimeError:
            pass


class PlayerControls(discord.ui.View):
    """Botones debajo del mensaje "Sonando ahora"."""

    def __init__(self, session: GuildSession):
        super().__init__(timeout=None)
        self.session = session

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self.session.cog.require_session(interaction) is not None

    @discord.ui.button(emoji="⏯️", style=discord.ButtonStyle.secondary)
    async def toggle_pause(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = self.session.engine.toggle_pause()
        text = {True: "Pausado.", False: "Reanudado.", None: "No hay nada sonando."}[state]
        await interaction.response.send_message(text, ephemeral=True)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        skipped = self.session.engine.skip()
        text = f"Salté **{_label(skipped)}**." if skipped else "No hay nada sonando."
        await interaction.response.send_message(text, ephemeral=True)

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary)
    async def loop(self, interaction: discord.Interaction, button: discord.ui.Button):
        mode = self.session.engine.queue.cycle_loop()
        await interaction.response.send_message(f"Loop: {LOOP_LABELS[mode]}.", ephemeral=True)

    @discord.ui.button(emoji="🔀", style=discord.ButtonStyle.secondary)
    async def shuffle(self, interaction: discord.Interaction, button: discord.ui.Button):
        queue = self.session.engine.queue
        if len(queue) < 2:
            await interaction.response.send_message("Hacen falta al menos 2 temas en la cola.", ephemeral=True)
            return
        queue.shuffle()
        await interaction.response.send_message("Cola mezclada.", ephemeral=True)

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger)
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.session.engine.stop()
        await interaction.response.send_message("Detuve la música y vacié la cola.", ephemeral=True)


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.sessions: dict[int, GuildSession] = {}

    def get_session(self, guild: discord.Guild) -> GuildSession:
        session = self.sessions.get(guild.id)
        if session is None:
            session = self.sessions[guild.id] = GuildSession(self, guild)
        return session

    async def cog_unload(self) -> None:
        for session in list(self.sessions.values()):
            await session.disconnect()
        self.sessions.clear()

    async def require_session(self, interaction: discord.Interaction) -> GuildSession | None:
        """Para comandos de control: el bot tiene que estar en voz y el usuario en el mismo canal.
        Si no se cumple, responde al usuario y devuelve None."""
        guild = interaction.guild
        session = self.sessions.get(guild.id) if guild else None
        vc = guild.voice_client if guild else None
        if session is None or vc is None or not vc.is_connected():
            await interaction.response.send_message("No estoy en ningún canal de voz.", ephemeral=True)
            return None
        user_channel = getattr(getattr(interaction.user, "voice", None), "channel", None)
        if user_channel is None or user_channel.id != vc.channel.id:
            await interaction.response.send_message(
                "Tenés que estar en mi mismo canal de voz.", ephemeral=True
            )
            return None
        return session

    # --- comandos -------------------------------------------------------

    @app_commands.command(name="play", description="Reproduce un tema, link o playlist (YouTube, Spotify, SoundCloud...)")
    @app_commands.describe(busqueda="Nombre del tema o un link")
    @app_commands.guild_only()
    async def play(self, interaction: discord.Interaction, busqueda: str):
        member = interaction.user
        channel = member.voice.channel if getattr(member, "voice", None) else None
        if channel is None:
            await interaction.response.send_message("Tenés que estar en un canal de voz.", ephemeral=True)
            return
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.connect and perms.speak):
            await interaction.response.send_message(
                "No tengo permisos para conectarme y hablar en ese canal.", ephemeral=True
            )
            return

        await interaction.response.defer()
        session = self.get_session(interaction.guild)
        session.text_channel = interaction.channel

        # Conectar a voz y resolver la búsqueda en paralelo: la conexión
        # (~1 s) se solapa con yt-dlp en vez de sumarse.
        connect_task = asyncio.create_task(session.ensure_connected(channel))
        try:
            tracks = await build_tracks(
                busqueda, member.display_name, max_tracks=config.MAX_PLAYLIST_TRACKS
            )
        except Exception as exc:
            await asyncio.gather(connect_task, return_exceptions=True)
            if not session.engine.busy:
                session.schedule_idle_disconnect()
            if isinstance(exc, SourceError):
                await interaction.followup.send(str(exc))
            else:
                log.exception("Error inesperado buscando %r", busqueda)
                await interaction.followup.send("Algo salió mal buscando eso. Probá de nuevo.")
            return

        try:
            await connect_task
        except Exception:
            log.exception("No pude conectarme al canal de voz")
            await interaction.followup.send("No pude conectarme al canal de voz.")
            return

        added = session.engine.add(tracks)
        if added == 0:
            await interaction.followup.send(f"La cola está llena ({config.MAX_QUEUE_SIZE} temas).")
        elif len(tracks) == 1:
            suffix = f" (posición {len(session.engine.queue)})" if session.engine.busy else ""
            await interaction.followup.send(f"Agregué **{_label(tracks[0])}**{suffix}.")
        else:
            partial = f" de {len(tracks)}" if added < len(tracks) else ""
            await interaction.followup.send(f"Agregué {added}{partial} temas a la cola.")

        await session.engine.start_if_idle()

    @app_commands.command(name="skip", description="Saltea el tema actual")
    @app_commands.guild_only()
    async def skip(self, interaction: discord.Interaction):
        session = await self.require_session(interaction)
        if session is None:
            return
        skipped = session.engine.skip()
        if skipped is None:
            await interaction.response.send_message("No hay nada sonando.", ephemeral=True)
        else:
            await interaction.response.send_message(f"Salté **{_label(skipped)}**.")

    @app_commands.command(name="pause", description="Pausa la reproducción")
    @app_commands.guild_only()
    async def pause(self, interaction: discord.Interaction):
        session = await self.require_session(interaction)
        if session is None:
            return
        if session.engine.pause():
            await interaction.response.send_message("Pausado.")
        else:
            await interaction.response.send_message("No hay nada sonando.", ephemeral=True)

    @app_commands.command(name="resume", description="Reanuda la reproducción")
    @app_commands.guild_only()
    async def resume(self, interaction: discord.Interaction):
        session = await self.require_session(interaction)
        if session is None:
            return
        if session.engine.resume():
            await interaction.response.send_message("Reanudado.")
        else:
            await interaction.response.send_message("No está en pausa.", ephemeral=True)

    @app_commands.command(name="stop", description="Detiene la música y vacía la cola")
    @app_commands.guild_only()
    async def stop(self, interaction: discord.Interaction):
        session = await self.require_session(interaction)
        if session is None:
            return
        session.engine.stop()
        await interaction.response.send_message("Detuve la música y vacié la cola.")

    @app_commands.command(name="leave", description="Desconecta al bot del canal de voz")
    @app_commands.guild_only()
    async def leave(self, interaction: discord.Interaction):
        session = await self.require_session(interaction)
        if session is None:
            return
        await interaction.response.send_message("Listo, me fui del canal.")
        await session.disconnect()

    @app_commands.command(name="queue", description="Muestra la cola de reproducción")
    @app_commands.describe(pagina="Número de página")
    @app_commands.guild_only()
    async def queue(self, interaction: discord.Interaction, pagina: app_commands.Range[int, 1, 100] = 1):
        session = self.sessions.get(interaction.guild_id)
        if session is None or (session.engine.queue.current is None and len(session.engine.queue) == 0):
            await interaction.response.send_message("La cola está vacía.", ephemeral=True)
            return
        text, _ = format_queue(session.engine.queue, page=pagina)
        await interaction.response.send_message(text)

    @app_commands.command(name="nowplaying", description="Muestra qué está sonando")
    @app_commands.guild_only()
    async def nowplaying(self, interaction: discord.Interaction):
        session = self.sessions.get(interaction.guild_id)
        current = session.engine.queue.current if session else None
        if current is None:
            await interaction.response.send_message("No hay nada sonando.", ephemeral=True)
            return
        await interaction.response.send_message(embed=track_embed(current))

    @app_commands.command(name="shuffle", description="Mezcla la cola")
    @app_commands.guild_only()
    async def shuffle(self, interaction: discord.Interaction):
        session = await self.require_session(interaction)
        if session is None:
            return
        queue = session.engine.queue
        if len(queue) < 2:
            await interaction.response.send_message(
                "Hacen falta al menos 2 temas en la cola para mezclar.", ephemeral=True
            )
            return
        queue.shuffle()
        await interaction.response.send_message(f"Mezclé la cola ({len(queue)} temas).")

    @app_commands.command(name="remove", description="Saca un tema de la cola por su posición")
    @app_commands.describe(posicion="Posición en /queue (1 = el próximo)")
    @app_commands.guild_only()
    async def remove(self, interaction: discord.Interaction, posicion: app_commands.Range[int, 1, 1000]):
        session = await self.require_session(interaction)
        if session is None:
            return
        try:
            removed = session.engine.queue.remove(posicion)
        except IndexError:
            await interaction.response.send_message("No hay ningún tema en esa posición.", ephemeral=True)
            return
        await interaction.response.send_message(f"Saqué **{_label(removed)}** de la cola.")

    @app_commands.command(name="clear", description="Vacía la cola sin cortar el tema actual")
    @app_commands.guild_only()
    async def clear(self, interaction: discord.Interaction):
        session = await self.require_session(interaction)
        if session is None:
            return
        removed = session.engine.queue.clear()
        await interaction.response.send_message(f"Saqué {removed} temas de la cola.")

    @app_commands.command(name="loop", description="Repite el tema actual o toda la cola")
    @app_commands.describe(modo="Qué repetir")
    @app_commands.choices(
        modo=[
            app_commands.Choice(name="Desactivado", value=LoopMode.OFF.value),
            app_commands.Choice(name="Canción", value=LoopMode.TRACK.value),
            app_commands.Choice(name="Cola", value=LoopMode.QUEUE.value),
        ]
    )
    @app_commands.guild_only()
    async def loop(self, interaction: discord.Interaction, modo: app_commands.Choice[str]):
        session = await self.require_session(interaction)
        if session is None:
            return
        session.engine.queue.loop = LoopMode(modo.value)
        await interaction.response.send_message(f"Loop: {LOOP_LABELS[session.engine.queue.loop]}.")

    @app_commands.command(name="volume", description="Ajusta el volumen (0-100)")
    @app_commands.describe(nivel="Volumen de 0 a 100")
    @app_commands.guild_only()
    async def volume(self, interaction: discord.Interaction, nivel: app_commands.Range[int, 0, 100]):
        session = await self.require_session(interaction)
        if session is None:
            return
        session.engine.set_volume(nivel)
        await interaction.response.send_message(f"Volumen al {nivel}%.")

    # --- eventos --------------------------------------------------------

    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        session = self.sessions.get(member.guild.id)
        if session is None:
            return
        if member.id == self.bot.user.id:
            if after.channel is None:
                session.cleanup()  # nos sacaron del canal (o nos desconectamos)
            else:
                session.check_alone()  # nos movieron a otro canal
            return
        session.check_alone()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Music(bot))
