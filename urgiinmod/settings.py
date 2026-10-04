"""
Ургийн мод — Django тохиргоо.

Нэг файлтай (байшингийн хэв маяг). Нууц утгууд нь .env-ээс уншигдана.
"""

import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")


def _env_bool(name, default):
    """1/0/true/false/yes/no/on/off. Хоосон (DJANGO_DEBUG=) утгыг 'тохируулаагүй'
    гэж үзнэ — .env.example-г хуулж авсан dev box-ыг санамсаргүй унтраахаас сэргийлнэ."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _env_list(name, default=""):
    """Таслалаар тусгаарласан env хувьсагч -> list[str] (хоосон утгууд хасагдана)."""
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


# Зөвхөн DEBUG асаалттай үед хэрэглэгдэнэ — доорх guard-ыг харна уу.
_INSECURE_DEV_SECRET_KEY = "django-insecure-dev-only-urgiin-mod-change-me"

SECRET_KEY = os.environ.get("SECRET_KEY") or _INSECURE_DEV_SECRET_KEY

# Явган DJANGO_DEBUG ямагт ялна. Эс бөгөөс: орчинд SECRET_KEY БАЙХГҮЙ бол локал dev box,
# SECRET_KEY БАЙВАЛ энэ бол deploy — debug унтарна. Ингэснээр prod-д 'мартаж'
# DEBUG=True үлдэх зам хаагдана. deploy/urgiin-mod.service нь DJANGO_DEBUG=0 тавина.
DEBUG = _env_bool("DJANGO_DEBUG", default=not os.environ.get("SECRET_KEY"))

if not DEBUG and SECRET_KEY == _INSECURE_DEV_SECRET_KEY:
    raise ImproperlyConfigured(
        "SECRET_KEY-г орчинд (эсвэл .env-д) тавих ёстой — DJANGO_DEBUG=0 үед заавал."
    )

ALLOWED_HOSTS = _env_list("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost")

CSRF_TRUSTED_ORIGINS = _env_list("DJANGO_CSRF_TRUSTED_ORIGINS")
if DEBUG:
    CSRF_TRUSTED_ORIGINS += ["http://127.0.0.1:8000", "http://localhost:8000"]

# HTTPS / cookie аюулгүй байдал (prod-д nginx SSL termination хийнэ)
if not DEBUG:
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    # Анхаар: nginx нь SSL-ийг terminate хийж, http→https redirect-ийг өөрөө
    # хийдэг (fpl-ийн адил) тул SECURE_SSL_REDIRECT анхдагчаар унтарсан.
    # Хэрэв урд талд redirect хийдэг прокси БАЙХГҮЙ бол 1 болгоно.
    SECURE_SSL_REDIRECT = _env_bool("DJANGO_SECURE_SSL_REDIRECT", default=False)


INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    "accounts",
    "genealogy",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Админы «энгийн харагдац» — доод бүх middleware хаалттай хэрэглэгчийг
    # харахын тулд AuthenticationMiddleware-ийн дараа, LoginRequired-ийн өмнө.
    "accounts.middleware.AdminViewModeMiddleware",
    # HTMX хүсэлт нэвтрэх хуудас руу шилжих үед HX-Redirect болгож хөрвүүлнэ
    # (LoginRequiredMiddleware-ийн өмнө байх ёстой — хариуг нь засдаг).
    "genealogy.middleware.HtmxRedirectToLoginMiddleware",
    # Default-deny: нэвтрээгүй хэрэглэгч ямар ч өгөгдөл харахгүй. Шинэ view
    # нэмэгдэхэд автоматаар хамгаалагдана — view бүрд @login_required
    # нэмэхийг мартах эрсдэлгүй. Зөвшөөрөх view-д @login_not_required тавина.
    "django.contrib.auth.middleware.LoginRequiredMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "urgiinmod.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "genealogy.context_processors.genealogy",
                "accounts.context_processors.view_mode",
            ],
        },
    },
]

WSGI_APPLICATION = "urgiinmod.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("DB_NAME", "urgiin_mod"),
        "USER": os.environ.get("DB_USER", "baysa"),
        "PASSWORD": os.environ.get("DB_PASSWORD", ""),
        "HOST": os.environ.get("DB_HOST", "127.0.0.1"),
        "PORT": os.environ.get("DB_PORT", "5432"),
        "CONN_MAX_AGE": 60,
        # Энэ кластерын template1 нь collation version 2.39-тэй бүртгэгдсэн бол
        # OS нь 2.43 өгдөг тул түүнээс тестийн DB үүсгэх боломжгүй. template0-оос
        # үүсгэнэ — хуваалцсан template1-ийг хөндөхгүй.
        # (Бүх кластерт засахыг хүсвэл: ALTER DATABASE template1 REFRESH COLLATION VERSION;)
        "TEST": {
            "TEMPLATE": "template0",
            "NAME": os.environ.get("DB_TEST_NAME", "test_urgiin_mod"),
        },
    }
}

AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "genealogy:home"
LOGOUT_REDIRECT_URL = "genealogy:home"

LANGUAGE_CODE = "mn"
TIME_ZONE = "Asia/Ulaanbaatar"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
def _running_tests():
    import sys

    return "test" in sys.argv


# Manifest storage нь `collectstatic`-ийн manifest файлыг шаарддаг — dev болон
# тестийн үед байхгүй тул энгийн storage ашиглана (эс бөгөөс {% static %} унана).
_USE_STATIC_MANIFEST = not DEBUG and not _running_tests()

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": (
            "whitenoise.storage.CompressedManifestStaticFilesStorage"
            if _USE_STATIC_MANIFEST
            else "django.contrib.staticfiles.storage.StaticFilesStorage"
        )
    },
}

# Медиа файл нь ЗӨВХӨН хувийн — нээлттэй MEDIA_URL-ээр үйлчлэхгүй.
# Файлыг эрх шалгадаг view (genealogy.views.serve_media) дамжуулж өгнө,
# эс бөгөөс амьд хүний зураг нууцлалыг тойрч нээлттэй болно.
MEDIA_ROOT = BASE_DIR / "media"
MEDIA_URL = "/media/"

# S3: AWS_STORAGE_BUCKET_NAME тавигдсан үед медиа S3-д хадгалагдана. Бакет нь
# ХУВИЙН (public access блоклосон, ACL-гүй) — бусад төслүүдийн public-read
# хэв маягийг энд ХЭРЭГЛЭХГҮЙ. serve_media нь эрх шалгасны дараа богино
# хугацаатай presigned URL руу redirect хийнэ. Тестийн үед үргэлж локал диск.
AWS_STORAGE_BUCKET_NAME = os.environ.get("AWS_STORAGE_BUCKET_NAME", "")
AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")
USE_S3_MEDIA = bool(AWS_STORAGE_BUCKET_NAME) and not _running_tests()
# Presigned URL-ийн хүчинтэй хугацаа (сек). Холбоосыг хуулж бусдад өгөхөд
# удаан ажиллахгүй байх ёстой.
MEDIA_URL_EXPIRE = int(os.environ.get("MEDIA_URL_EXPIRE", "300"))

if USE_S3_MEDIA:
    STORAGES["default"] = {
        "BACKEND": "storages.backends.s3.S3Storage",
        "OPTIONS": {
            "bucket_name": AWS_STORAGE_BUCKET_NAME,
            "region_name": AWS_REGION,
            "location": "media",
            "default_acl": None,
            "querystring_auth": True,
            "querystring_expire": MEDIA_URL_EXPIRE,
            "file_overwrite": False,
            "signature_version": "s3v4",
            "addressing_style": "virtual",
            "object_parameters": {"ServerSideEncryption": "AES256"},
        },
    }

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

MESSAGE_STORAGE = "django.contrib.messages.storage.session.SessionStorage"

# Тестийн үед MEDIA_ROOT-ыг түр хавтас руу шилжүүлнэ — эс бөгөөс тестүүд
# бодит `media/` руу файл бичиж, хог хуримтлуулдаг.
TEST_RUNNER = "urgiinmod.test_runner.MediaRootDiscoverRunner"

# Амьд хүнийг таамаглах насны хязгаар (удмын бичгийн уламжлалт 110 жил).
LIVING_YEARS = int(os.environ.get("LIVING_YEARS", "110"))

# Урилгын линк хэдэн хоног хүчинтэй.
INVITE_EXPIRY_DAYS = int(os.environ.get("INVITE_EXPIRY_DAYS", "7"))

SITE_URL = os.environ.get("SITE_URL", "http://127.0.0.1:8000")

EMAIL_BACKEND = os.environ.get(
    "EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend"
)
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "urgiin-mod@localhost")

# SMTP тохиргоо. Эдгээрийг ЭНД тавихгүй бол `smtp.EmailBackend` руу шилжихэд
# Django-гийн анхдагч (localhost:25) руу явж, урилгын имэйл чимээгүй алдагдана
# (`send_invite_email` нь `fail_silently=True`).
EMAIL_HOST = os.environ.get("EMAIL_HOST", "localhost")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "25"))
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = _env_bool("EMAIL_USE_TLS", default=False)

# Amazon SES (anymail) — EMAIL_BACKEND=anymail.backends.amazon_ses.EmailBackend
# үед. Түлхүүрийг boto3 нь AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY env-ээс
# өөрөө уншина (S3-тэй нэг IAM хэрэглэгч).
ANYMAIL = {
    "AMAZON_SES_CLIENT_PARAMS": {"region_name": AWS_REGION},
}
if os.environ.get("AWS_SES_CONFIGURATION_SET"):
    ANYMAIL["AMAZON_SES_CONFIGURATION_SET_NAME"] = os.environ["AWS_SES_CONFIGURATION_SET"]

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "simple": {"format": "{levelname} {asctime} {name}: {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "simple"},
        "file": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": BASE_DIR / "logs" / "urgiin-mod.log",
            "maxBytes": 5 * 1024 * 1024,
            "backupCount": 5,
            "formatter": "simple",
        },
    },
    "root": {"handlers": ["console", "file"], "level": "INFO"},
    "loggers": {
        "django.request": {"handlers": ["console", "file"], "level": "WARNING", "propagate": False},
    },
}
