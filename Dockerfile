FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt ./
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir -r requirements.txt && useradd --create-home notes
COPY --chown=notes:notes app ./app
COPY --chown=notes:notes migrations ./migrations
COPY --chown=notes:notes alembic.ini ./
RUN mkdir -p /app/data/audio && chown -R notes:notes /app/data && chmod 700 /app/data/audio
USER notes
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
