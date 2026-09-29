FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && useradd --create-home notes
COPY --chown=notes:notes app ./app
COPY --chown=notes:notes migrations ./migrations
COPY --chown=notes:notes alembic.ini ./
RUN mkdir -p /app/data && chown notes:notes /app/data
USER notes
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
