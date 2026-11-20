FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --system app \
    && useradd --system --gid app --uid 10001 app \
    && mkdir -p /var/lib/hr_bot \
    && chown app:app /var/lib/hr_bot

COPY *.py alembic.ini ./
COPY storage ./storage
COPY hr_api ./hr_api
COPY department_bot ./department_bot
COPY migrations ./migrations
COPY sql ./sql
COPY prompts ./prompts
COPY docker ./docker

USER app
EXPOSE 8000
ENTRYPOINT ["python", "-m", "docker.entrypoint"]
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
