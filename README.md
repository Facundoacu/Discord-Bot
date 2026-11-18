# Discord Music Bot (Python)

Bot de música para Discord. Un solo proceso, sin servicios en el medio: lo que
pide el usuario va directo a `yt-dlp` y el audio directo al canal de voz.

## Comandos

| Comando | Qué hace |
|---|---|
| `/play <búsqueda o link>` | Reproduce un tema, álbum o playlist |
| `/skip` · `/pause` · `/resume` · `/stop` | Control básico |
| `/queue [página]` · `/nowplaying` | Ver la cola y el tema actual |
| `/shuffle` · `/remove <pos>` · `/clear` | Administrar la cola |
| `/loop <off/canción/cola>` | Repetir |
| `/volume <0-100>` · `/leave` | Volumen y salir del canal |
| `/autoplay [activar]` | Al terminarse la cola, seguir con temas parecidos (sin opción, lo alterna) |

Además, cada tema que empieza trae botones (⏯️ ⏭️ 🔁 🔀 ⏹️, y 🎲 🔉 🔊 para el autoplay y para bajar o subir el volumen de a 10 %) para no tener que tipear.
Los comandos de control solo los puede usar quien esté en el mismo canal de voz que el bot.

## Autoplay

Con el autoplay activado (por defecto; `AUTOPLAY=false` en el `.env` lo apaga),
cuando suena el último tema de la cola el bot busca en segundo plano temas
parecidos en el *Mix* de YouTube de ese tema y elige 3 al azar entre los más
cercanos, salteando los que ya sonaron y los compilados largos. Lo que pida
alguien con `/play` pasa adelante de esos temas, y `/stop` corta el autoplay.

## Qué se puede reproducir

- **Búsqueda por texto** → YouTube.
- **Links de YouTube**: video suelto o playlist (`/playlist?list=...`). Un link
  `watch?v=...&list=...` se toma como el video, no como toda la lista.
- **Cualquier sitio que soporte yt-dlp** (SoundCloud, Bandcamp, etc.): pegá el link.
- **Spotify** (tema, álbum, playlist): Spotify solo da metadata, no audio. El bot
  lee título y artista y busca ese tema en YouTube.

### Limitación importante de Spotify

Desde febrero 2026, las apps de Spotify en modo desarrollo **solo pueden leer
playlists propias o colaborativas**, y el dueño de la app necesita Premium
([guía de migración](https://developer.spotify.com/documentation/web-api/tutorials/february-2026-migration-guide)).
En la práctica: links de **tema y álbum** deberían andar; las **playlists de
terceros** probablemente no, y el bot lo avisa con un mensaje claro. Para
playlists, lo más confiable es usar playlists de YouTube.

## Por qué es rápido

- **Todo lo bloqueante corre en el threadpool** (`yt-dlp`, `spotipy`): el event
  loop nunca se congela, así que un `/play` no trababa a los demás servidores.
- **Resolución lazy**: una playlist de 100 temas se encola al instante; el audio
  de cada tema se resuelve recién cuando le toca.
- **Prefetch**: mientras suena un tema, ya se resuelve el siguiente, para que el
  cambio de canción no tenga silencio.
- **Conexión de voz en paralelo con la búsqueda**: los ~1 s de conectarse a voz
  se solapan con `yt-dlp` en vez de sumarse.
- Las URLs de stream que llevan más de 1 h resueltas se vuelven a resolver
  (en YouTube vencen), así que un tema que esperó mucho en la cola no falla.

## Puesta en marcha

### 1. Crear el bot en Discord

En el [Developer Portal](https://discord.com/developers/applications): crear la
app → *Bot* → copiar el token. No hace falta ningún intent privilegiado (el bot
usa solo slash commands). Invitarlo con los scopes `bot` y `applications.commands`
y los permisos *Connect*, *Speak*, *Send Messages* y *Embed Links*.

### 2. (Opcional) Credenciales de Spotify

Crear una app en el [dashboard de Spotify](https://developer.spotify.com/dashboard)
y copiar *Client ID* y *Client Secret* al `.env`. Sin eso, el bot funciona igual,
pero rechaza links de Spotify con un mensaje que lo explica.

### 3. Correrlo

Con Docker (recomendado; la imagen ya trae `ffmpeg` y Deno):

```bash
cp .env.example .env     # completar DISCORD_TOKEN (y Spotify si querés)
docker compose up -d --build
docker compose logs -f
```

Local (necesita Python 3.12+, [ffmpeg](https://ffmpeg.org/) y [Deno](https://deno.com/) instalados):

```bash
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
python bot.py
```

Para desarrollar, poné `DEV_GUILD_ID` en el `.env` con el ID de tu servidor de
pruebas: los slash commands se actualizan al instante.

## Deploy en Oracle Cloud (gratis, 24 h)

Oracle Cloud *Always Free* da un servidor ARM gratis que alcanza de sobra
para unos pocos servidores de Discord. El riesgo: YouTube a veces bloquea IPs
de datacenters (ver abajo); el script de instalación lo prueba solo.

> **Importante:** el mismo token no puede correr en dos lugares a la vez.
> Antes de levantarlo en Oracle, frená el de tu PC con `docker compose down`.

### 1. Crear la cuenta y el servidor

1. Registrate en https://cloud.oracle.com (piden tarjeta solo para verificar
   identidad). La **región principal** no se puede cambiar después: elegí una
   cercana, por ejemplo *Brazil East (São Paulo)* o *Chile (Santiago)*.
2. *Compute → Instances → Create instance*:
   - **Image:** Canonical Ubuntu 24.04.
   - **Shape:** *Ampere* `VM.Standard.A1.Flex`, 1 OCPU y 6 GB de RAM.
   - **SSH keys:** *Generate a key pair* y **descargá la clave privada**
     (`.key`). Sin ella no podés entrar al servidor.
3. *Create*. Cuando figure *Running*, copiá la **Public IP address**.

Si dice *Out of capacity*, reintentá más tarde o probá otro *Availability
domain*. No hace falta abrir puertos: el bot solo hace conexiones salientes.

**Para que Oracle no lo apague por inactividad**, pasá la cuenta a
*Pay As You Go* (sigue siendo gratis dentro de lo *Always Free*) y creá una
alerta de presupuesto en *Billing → Budgets* por si acaso.

### 2. Instalar el bot (desde PowerShell en tu PC)

Guardá la clave en `C:\Users\<vos>\.ssh\oracle.key` y entrá al servidor
(reemplazá `IP` por la IP pública):

```bash
ssh -i $HOME\.ssh\oracle.key ubuntu@IP
```

Ya en el servidor, bajá el código:

```bash
git clone https://github.com/Facundoacu/Discord-Bot.git && cd Discord-Bot
```

En **otra** ventana de PowerShell, desde la carpeta del bot en tu PC, copiá
tu `.env` al servidor (así no tipeás los tokens a mano):

```bash
scp -i $HOME\.ssh\oracle.key .env ubuntu@IP:~/Discord-Bot/.env
```

De vuelta en el servidor, instalá y levantá todo:

```bash
bash scripts/setup-server.sh
```

El script instala Docker, protege el `.env`, levanta el bot (queda corriendo
y se reinicia solo) y **prueba si YouTube deja bajar audio desde esa IP**.
Para actualizar el bot más adelante, volvé a correr el mismo script.

### Si YouTube bloquea la IP

El síntoma: todos los `/play` fallan con "no pude obtener el audio", y el
script muestra *"Sign in to confirm you're not a bot"*. Opciones:
otra región o cuenta de Oracle, cookies de una cuenta de YouTube **secundaria**
(puede ser suspendida), o volver a correrlo en tu PC.

## Mantenimiento

**YouTube cambia seguido y `yt-dlp` hay que mantenerlo al día.** Si de golpe
"no puedo obtener el audio" en todos los temas, casi seguro es eso:

```bash
docker compose build --no-cache && docker compose up -d
```

**Deno es necesario**: yt-dlp lo usa para resolver los desafíos de JavaScript de
YouTube; sin él, YouTube pasa a dar menos formatos y eventualmente puede dejar
de andar. La imagen de Docker ya lo incluye.

Configuración opcional por variables de entorno: ver `.env.example`
(tamaño máximo de cola, volumen inicial, tiempos de inactividad, etc.).

## Estructura

```
bot.py              arranque y sync de slash commands
config.py           variables de entorno
cogs/music.py       capa de Discord: comandos, botones, voz
player/engine.py    motor: avance, loops, skip, prefetch (sin Discord)
player/queue.py     cola y modos de loop
player/sources.py   YouTube / Spotify / URLs → tracks
player/text.py      formato de mensajes
tests/              pytest
```

La lógica de reproducción está separada de discord.py a propósito: el motor
habla con un `PlayerBackend` y en los tests se usa uno falso, lo que permite
probar skip, loops y carreras (por ejemplo `/play` justo cuando termina un
tema) sin conectarse a nada.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

El CI (`.github/workflows/ci.yml`) corre los tests y construye la imagen en
cada push. El deploy queda manual (`git pull && docker compose up -d --build`
en el servidor).
