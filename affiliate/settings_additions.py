# ─────────────────────────────────────────────────────────────
# AlphaCue Affiliate System — settings.py additions
# Add these values into your existing Django settings.py
# ─────────────────────────────────────────────────────────────

# 1. Add 'affiliate' to INSTALLED_APPS
# INSTALLED_APPS = [
#     ...existing apps...
#     'affiliate',
# ]

# 2. Affiliate cookie + session configuration
AFFILIATE_COOKIE_NAME = 'alphacue_ref'
AFFILIATE_COOKIE_MAX_AGE = 60 * 60 * 24 * 30   # 30 days in seconds
AFFILIATE_SESSION_KEY = 'affiliate_referral_code'

# 3. Database (MySQL — shared cPanel hosting)
# DATABASES = {
#     'default': {
#         'ENGINE': 'django.db.backends.mysql',
#         'NAME': 'your_db_name',
#         'USER': 'your_db_user',
#         'PASSWORD': 'your_db_password',
#         'HOST': 'localhost',
#         'PORT': '3306',
#         'OPTIONS': {
#             'charset': 'utf8mb4',
#             'init_command': "SET sql_mode='STRICT_TRANS_TABLES'",
#         },
#     }
# }
