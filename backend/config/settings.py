from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env()

# Local (non-Docker) runs read the repo-root .env.  In Docker, compose injects the vars
_env_file = BASE_DIR.parent / ".env"
if _env_file.exists():
    environ.Env.read_env_(_env_file)

# No default on purpose: A missing secret must fail loudly, never fall back to a known value.
SECRET_KEY = env("DJANGO_SECRET_KEY")
DEBUG = env.bool("DJANGO_DEBUG", default=False)
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "corsheaders",
    "rest_framework",
    "apps.accounts",
    "apps.pos",
    "apps.cloudmock",
    "apps.api",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("POSTGRES_DB"),
        "USER": env("POSTGRES_USER"),
        "PASSWORD": env("POSTGRES_PASSWORD"),
        "HOST": env("POSTGRES_HOST", default="localhost"),
        "PORT": env("POSTGRES_PORT", default="5432"),
    }
}

AUTH_USER_MODEL = "accounts.User"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

CORS_ALLOWED_ORIGINS = env.list("CORS_ALLOWED_ORIGINS", default=["http://localhost:5173"])

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Africa/Nairobi"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# Whether menu prices already include VAT. Open product decision: change via env, never in code.
POS_PRICES_TAX_INCLUSIVE = env.bool("POS_PRICES_TAX_INCLUSIVE", default=True)

# --- Cloud sync (Outbox drain) ---
CLOUD_SYNC_URL = env("CLOUD_SYNC_URL", default="")  # blank = sync disabled
CLOUD_API_KEY = env("CLOUD_API_KEY", default="")
CLOUD_MOCK_ENABLED = env.bool("CLOUD_MOCK_ENABLED", default=False)  # mock receiver, cloud only
OUTBOX_BATCH_SIZE = env.int("OUTBOX_BATCH_SIZE", default=50)
OUTBOX_POLL_SECONDS = env.int("OUTBOX_POLL_SECONDS", default=5)
OUTBOX_LEASE_SECONDS = env.int("OUTBOX_LEASE_SECONDS", default=60)
OUTBOX_BACKOFF_BASE_SECONDS = env.int("OUTBOX_BACKOFF_BASE_SECONDS", default=2)
OUTBOX_BACKOFF_CAP_SECONDS = env.int("OUTBOX_BACKOFF_CAP_SECONDS", default=60)

# --- Reconciliation ---
RECONCILIATION_WINDOW_MINUTES = env.int("RECONCILIATION_WINDOW_MINUTES", default=30)
RECONCILIATION_GRACE_HOURS = env.int("RECONCILIATION_GRACE_HOURS", default=24)

# --- M-PESA records (reconciliation source) ---
MPESA_RECORDS_URL = env("MPESA_RECORDS_URL", default="")  # blank = reconciliation disabled
RECONCILE_POLL_SECONDS = env.int("RECONCILE_POLL_SECONDS", default=30)

# --- HTTP API ---
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["rest_framework.authentication.SessionAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
    "DEFAULT_THROTTLE_RATES": {"login": "10/min"},
    "EXCEPTION_HANDLER": "apps.api.errors.api_exception_handler",
}
MPESA_PROVIDER = env("MPESA_PROVIDER", default="mock")
STK_TIMEOUT_SECONDS = env.int("STK_TIMEOUT_SECONDS", default=90)
MPESA_CALLBACK_TOKEN = env("MPESA_CALLBACK_TOKEN", default="")
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=["http://localhost:5173"])
SESSION_COOKIE_AGE = env.int("SESSION_COOKIE_AGE_SECONDS", default=28800)  # until idle lock exists
