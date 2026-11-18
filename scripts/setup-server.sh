#!/usr/bin/env bash
# Instala y levanta el bot en un servidor Ubuntu/Debian (Oracle Cloud, VPS...).
#
# Uso, desde la carpeta del repo ya clonado:
#   bash scripts/setup-server.sh
#
# Hace, en orden:
#   1. instala Docker si no está;
#   2. exige que exista el .env y lo deja legible solo por tu usuario;
#   3. construye la imagen y levanta el bot (se reinicia solo para siempre);
#   4. prueba si YouTube deja bajar audio desde la IP de este servidor.
#
# Se puede correr de nuevo para actualizar: hace git pull y reconstruye.
set -euo pipefail

cd "$(dirname "$0")/.."

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m    ✔ %s\033[0m\n' "$*"; }
fail() { printf '\033[1;31m    ✘ %s\033[0m\n' "$*"; }

# --- 1. Docker -------------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
    say "Instalando Docker"
    curl -fsSL https://get.docker.com | sudo sh
    sudo usermod -aG docker "$USER"
    ok "Docker instalado"
fi
# En la primera instalación el grupo docker no aplica a esta sesión todavía.
DOCKER="docker"
if ! docker info >/dev/null 2>&1; then
    DOCKER="sudo docker"
fi

# --- 2. .env ----------------------------------------------------------------
say "Revisando el .env"
if [ ! -f .env ]; then
    fail "No existe el archivo .env en $(pwd)"
    echo "    Copialo desde tu PC (ver README, sección 'Deploy en Oracle Cloud') y volvé a correr este script."
    exit 1
fi
if ! grep -qE '^DISCORD_TOKEN=.+' .env; then
    fail "El .env no tiene DISCORD_TOKEN"
    exit 1
fi
chmod 600 .env
ok ".env encontrado y protegido (solo tu usuario puede leerlo)"

# --- 3. Bot -----------------------------------------------------------------
if [ -d .git ]; then
    say "Actualizando el código"
    git pull --ff-only || echo "    (no pude hacer git pull; sigo con lo que hay)"
fi

say "Construyendo y levantando el bot (la primera vez tarda unos minutos)"
$DOCKER compose up -d --build

say "Esperando a que el bot se conecte a Discord"
for _ in $(seq 1 60); do
    if $DOCKER compose logs --since 5m 2>&1 | grep -q "Conectado como"; then
        ok "$($DOCKER compose logs --since 5m 2>&1 | grep -o 'Conectado como.*' | tail -1)"
        break
    fi
    if $DOCKER compose logs --since 5m 2>&1 | grep -qE "Improper token|Falta DISCORD_TOKEN"; then
        fail "Discord rechazó el token: revisá DISCORD_TOKEN en el .env"
        exit 1
    fi
    sleep 3
done

# --- 4. Prueba de YouTube -----------------------------------------------------
say "Probando si YouTube deja bajar audio desde este servidor"
if $DOCKER compose exec -T bot sh -c \
    'cd /tmp && python -m yt_dlp -q --no-warnings -f bestaudio --test -o yt-test.webm "ytsearch1:daft punk one more time" && rm -f yt-test.webm' \
    > /tmp/yt-test.log 2>&1; then
    ok "YouTube funciona desde esta IP: el bot debería sonar normal"
else
    fail "YouTube no dejó bajar audio desde esta IP"
    if grep -qi "confirm you" /tmp/yt-test.log; then
        echo "    YouTube marcó la IP del servidor como bot (\"Sign in to confirm you're not a bot\")."
        echo "    Opciones: otra región o cuenta de Oracle, o cookies de una cuenta secundaria (ver README)."
    else
        echo "    Detalle:"; tail -5 /tmp/yt-test.log | sed 's/^/      /'
    fi
fi

say "Listo"
echo "    Ver logs en vivo:   $DOCKER compose logs -f"
echo "    Actualizar el bot:  bash scripts/setup-server.sh"
