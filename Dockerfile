# Builds the same app the Procfile runs, without Railpack. Nothing here
# downloads build tools from GitHub. Python 3.11 matches runtime.txt.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies first so this layer is reused when only code changes.
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY . .

EXPOSE 8080

# Same start sequence as the Procfile. Migrations and collectstatic run at
# container start, when the Railway variables are present. Railway sets PORT;
# the service is configured for 8080. exec makes gunicorn PID 1 so it receives
# shutdown signals.
CMD ["sh", "-c", "python manage.py migrate --noinput --settings=config.settings.prod && python manage.py collectstatic --noinput --settings=config.settings.prod && exec gunicorn config.wsgi:application --bind 0.0.0.0:${PORT:-8080} --workers ${WEB_CONCURRENCY:-3} --timeout 60 --access-logfile - --error-logfile -"]
