"""
alphacue_imports/settings.py
─────────────────────────────
All sensitive values are read from a .env file in the project root.

Install dependencies:
    pip install python-dotenv mysqlclient whitenoise Pillow django-ckeditor
"""

from pathlib import Path
from dotenv import load_dotenv
import os

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / '.env')


# ─── Core ──────────────────────────────────────────────────────────────────────

SECRET_KEY = os.environ['SECRET_KEY']

DEBUG = os.getenv('DEBUG', 'False').strip().lower() in ('true', '1', 'yes')

ALLOWED_HOSTS = [
    h.strip()
    for h in os.getenv('ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',')
    if h.strip()
]


# ─── Apps ──────────────────────────────────────────────────────────────────────

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'affiliate',
    'store',
    'ckeditor',
    'ckeditor_uploader',
]


# ─── Middleware ────────────────────────────────────────────────────────────────
# WhiteNoise must be immediately after SecurityMiddleware

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',         # ← WhiteNoise
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'affiliate.middleware.AffiliateReferralMiddleware',
]

ROOT_URLCONF = 'alphacue_imports.urls'


# ─── Templates ────────────────────────────────────────────────────────────────

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'store.context_processors.cart',
                'store.context_processors.google_analytics',
            ],
        },
    },
]

WSGI_APPLICATION = 'alphacue_imports.wsgi.application'


# ─── Database — MySQL ──────────────────────────────────────────────────────────

DATABASES = {
    'default': {
        'ENGINE':   'django.db.backends.mysql',
        'NAME':     os.environ['DB_NAME'],
        'USER':     os.environ['DB_USER'],
        'PASSWORD': os.environ['DB_PASSWORD'],
        'HOST':     os.getenv('DB_HOST', 'localhost'),
        'PORT':     os.getenv('DB_PORT', '3306'),
        'OPTIONS': {
            'charset': 'utf8mb4',
            'init_command': "SET sql_mode='STRICT_TRANS_TABLES'",
        },
    }
}


# ─── Password Validation ──────────────────────────────────────────────────────

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]


# ─── Localisation ─────────────────────────────────────────────────────────────

LANGUAGE_CODE = 'en-us'
TIME_ZONE     = 'Asia/Dhaka'
USE_I18N      = True
USE_TZ        = False  # Disabled — MySQL on cPanel often lacks timezone tables


# ─── Static & Media ───────────────────────────────────────────────────────────

STATIC_URL  = '/static/'
MEDIA_URL   = '/media/'

_static = os.getenv('STATIC_ROOT', '').strip()
STATIC_ROOT = Path(_static) if _static else BASE_DIR / 'staticfiles'

_media = os.getenv('MEDIA_ROOT', '').strip()
MEDIA_ROOT = Path(_media) if _media else BASE_DIR / 'media'

# WhiteNoise — compressed + cached static files for production
STORAGES = {
    'default': {
        'BACKEND': 'django.core.files.storage.FileSystemStorage',
    },
    'staticfiles': {
        'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage',
    },
}


# ─── Sessions ─────────────────────────────────────────────────────────────────

SESSION_ENGINE             = 'django.contrib.sessions.backends.db'
SESSION_COOKIE_AGE         = 60 * 60 * 24 * 30
SESSION_SAVE_EVERY_REQUEST = False


# ─── Auth ─────────────────────────────────────────────────────────────────────

LOGIN_URL           = '/accounts/login/'
LOGIN_REDIRECT_URL  = '/affiliate/application-status/'
LOGOUT_REDIRECT_URL = '/'


# ─── Affiliate ────────────────────────────────────────────────────────────────

AFFILIATE_COOKIE_NAME    = os.getenv('AFFILIATE_COOKIE_NAME',    'alphacue_ref')
AFFILIATE_COOKIE_MAX_AGE = int(os.getenv('AFFILIATE_COOKIE_MAX_AGE', str(60 * 60 * 24 * 30)))
AFFILIATE_SESSION_KEY    = os.getenv('AFFILIATE_SESSION_KEY',    'affiliate_referral_code')
AFFILIATE_WEBHOOK_SECRET = os.getenv('AFFILIATE_WEBHOOK_SECRET', '')

# Google Analytics
GOOGLE_ANALYTICS_ID = os.getenv('GOOGLE_ANALYTICS_ID', '')


# ─── Security headers (production only) ───────────────────────────────────────

if not DEBUG:
    SECURE_BROWSER_XSS_FILTER    = True
    SECURE_CONTENT_TYPE_NOSNIFF  = True
    X_FRAME_OPTIONS               = 'DENY'
    SESSION_COOKIE_SECURE         = True
    CSRF_COOKIE_SECURE            = True
    SECURE_PROXY_SSL_HEADER       = ('HTTP_X_FORWARDED_PROTO', 'https')
    # Uncomment once HTTPS is confirmed working:
    # SECURE_SSL_REDIRECT            = True
    # SECURE_HSTS_SECONDS            = 31536000
    # SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    # SECURE_HSTS_PRELOAD            = True


# ─── CKEditor ────────────────────────────────────────────────────────────────

CKEDITOR_UPLOAD_PATH   = 'uploads/ckeditor/'
CKEDITOR_IMAGE_BACKEND = 'pillow'
CKEDITOR_CONFIGS = {
    'default': {
        'toolbar': 'Custom',
        'toolbar_Custom': [
            ['Bold', 'Italic', 'Underline', 'Strike'],
            ['NumberedList', 'BulletedList', '-', 'Outdent', 'Indent'],
            ['JustifyLeft', 'JustifyCenter', 'JustifyRight'],
            ['Link', 'Unlink'],
            ['Image', 'Table', 'HorizontalRule'],
            ['TextColor', 'BGColor'],
            ['Format', 'FontSize'],
            ['RemoveFormat', 'Source'],
        ],
        'height': 400,
        'width': '100%',
        'removePlugins': 'stylesheetparser',
        'extraPlugins': 'uploadimage',
        'filebrowserUploadUrl': '/ckeditor/upload/',
        'filebrowserBrowseUrl': '/ckeditor/browse/',
    },
    'basic': {
        'toolbar': 'Basic',
        'toolbar_Basic': [
            ['Bold', 'Italic', 'Underline'],
            ['NumberedList', 'BulletedList'],
            ['Link', 'Unlink'],
            ['RemoveFormat'],
        ],
        'height': 250,
        'width': '100%',
    },
}

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'