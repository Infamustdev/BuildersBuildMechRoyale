# Build from this folder: docker build -t my-bot .
# The match runs it with no network, a read-only root (writable /tmp), 1 CPU, 512 MB, and the map at /arena.
# Standard library only: nothing to install, so nothing to pin.
FROM python:3.13-slim
WORKDIR /bot
COPY . .
ENTRYPOINT ["python3", "-u", "/bot/bot.py"]
