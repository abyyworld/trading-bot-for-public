# The bot in a container: the same code and commands as on any computer, kept running.
#
#   docker compose up -d --build       start the loop (see docker-compose.yml)
#   docker compose logs -f             watch it
#   docker compose run --rm bot doctor one command instead of the loop
#
# Without docker-compose.yml's folder mount, the ledger and the paper account live inside
# the container and are lost with it; use the compose file.

FROM python:3.12-slim

# Print as it happens, not in blocks, so docker logs keeps up; no .pyc files in the folder.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Standard library only. Slim Debian images may come without the system's time zone
# database, which zoneinfo needs for New York's and London's market hours, so the tzdata
# package is installed whatever requirements.txt says for this platform.
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt tzdata

# Not root. /app belongs to the bot, which writes its ledger and state files there.
RUN useradd --create-home --uid 1000 bot \
    && mkdir /app && chown bot:bot /app
WORKDIR /app
COPY --chown=bot:bot . /app
USER bot

ENTRYPOINT ["python3", "run.py"]
CMD ["loop"]
