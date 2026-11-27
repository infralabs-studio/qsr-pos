FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# "dev" includes black/flake8/mypy; use REQUIREMENTS=base for a production image
ARG REQUIREMENTS=dev
COPY requirements/ requirements/
RUN pip install --no-cache-dir --default-timeout=120 --retries=5 -r requirements/${REQUIREMENTS}.txt

COPY . .

EXPOSE 8000
# Dev default. Production should run: gunicorn config.wsgi:application --bind 0.0.0.0:8000
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]