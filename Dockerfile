FROM python:3.12-slim

# FFmpeg decodifica el audio del sintetizador; libopus lo codifica para
# Discord. Sin estos dos, el bot escribe pero no habla.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg libopus0 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY ft_discord ./ft_discord

CMD ["python", "-m", "ft_discord.bot"]
