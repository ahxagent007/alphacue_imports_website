# ══════════════════════════════════════════════════════
#  AlphaCue Imports — cPanel Deployment Checklist
# ══════════════════════════════════════════════════════

## 1. Install dependencies
    pip install -r requirements.txt

## 2. Create your .env file
    cp .env.example .env
    # Edit .env with your actual values

## 3. Generate a secret key
    python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
    # Paste into .env SECRET_KEY=...

## 4. Create MySQL database in cPanel
    - cPanel → MySQL Databases
    - Create database, create user, assign ALL privileges
    - Fill DB_NAME, DB_USER, DB_PASSWORD in .env

## 5. Run migrations
    python manage.py migrate

## 6. Collect static files (WhiteNoise serves these)
    python manage.py collectstatic --noinput

## 7. Create superuser
    python manage.py createsuperuser

## 8. Seed default commission setting
    python manage.py shell
    >>> from affiliate.models import CommissionSetting
    >>> CommissionSetting.objects.create(
    ...     name="Default",
    ...     commission_type="percentage",
    ...     commission_value="10.00",
    ...     minimum_withdrawal_amount="500.00",
    ...     cookie_lifetime_days=30,
    ...     is_active=True,
    ...     is_default=True,
    ... )

## 9. Seed site settings
    python manage.py shell
    >>> from store.models import SiteSettings
    >>> SiteSettings.objects.create(
    ...     site_name="AlphaCue Imports",
    ...     delivery_fee_inside_dhaka=60,
    ...     delivery_fee_outside_dhaka=100,
    ... )

## 10. Configure cPanel Python App
    - cPanel → Setup Python App
    - Python version: 3.x
    - Application root: /home/user/alphacue_imports
    - Application URL: your domain
    - Application startup file: passenger_wsgi.py (see below)

## 11. Create passenger_wsgi.py in project root
    import os, sys
    sys.path.insert(0, os.path.dirname(__file__))
    os.environ['DJANGO_SETTINGS_MODULE'] = 'alphacue_imports.settings'
    from django.core.wsgi import get_wsgi_application
    application = get_wsgi_application()

## 12. Add .htaccess to public_html (if needed)
    PassengerEnabled On
    PassengerAppRoot /home/user/alphacue_imports

## 13. Security — after confirming HTTPS works:
    # In settings.py, uncomment:
    # SECURE_SSL_REDIRECT            = True
    # SECURE_HSTS_SECONDS            = 31536000
    # SECURE_HSTS_INCLUDE_SUBDOMAINS = True

## WHY WHITENOISE?
    - cPanel shared hosting cannot run a separate Nginx/Apache for static files
    - WhiteNoise serves static files directly from Django with gzip compression
    - Adds cache headers automatically (1 year for hashed files)
    - Zero configuration — just add to MIDDLEWARE and STORAGES
    - Much faster than Django's default static file serving
    - After collectstatic, all files are compressed + fingerprinted automatically
