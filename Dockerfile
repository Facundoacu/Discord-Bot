FROM python:3.12-slim

# ffmpeg: transcodifica el audio para el canal de voz.
# curl + unzip: solo para instalar Deno (abajo).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl unzip ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Deno: yt-dlp lo usa para resolver los desafíos de JavaScript de YouTube
# (es el único runtime que yt-dlp habilita por defecto). El script instala
# el binario correcto para la arquitectura, así que sirve igual en arm64 (t4g).
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh \
    && deno --version \
    && apt-get purge -y --auto-remove curl unzip

RUN useradd --create-home --uid 1000 bot
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=bot:bot . .
USER bot

CMD ["python", "-u", "bot.py"]
