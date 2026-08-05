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

---

# FINANCE MODULE — DEPLOYMENT & BACKUP

The finance module records real money. Everything below matters more than the
rest of this file.

## 14. CRITICAL — check the database engine

Before this module existed, `settings.py` declared `DATABASES` twice and the
SQLite block silently overwrote the MySQL one. Production was writing to
`db.sqlite3` regardless of what was in `.env`.

That is fixed, but the switch is now explicit. On the server, `.env` must have:

    DB_ENGINE=mysql

Leaving it out also gives MySQL — `sqlite` is the only value that changes it.

**If the live site has been running for a while, data already recorded on the
server lives in `db.sqlite3`, not MySQL.** Check before you assume MySQL is
empty by accident:

    python manage.py shell -c "from store.models import Order; print(Order.objects.count())"

Run that with `DB_ENGINE=sqlite` and again with `mysql` to see which one holds
the real history, and migrate the data across before going live if needed.

## 15. First-time finance setup

    python manage.py migrate
    python manage.py seed_chart_of_accounts
    python manage.py check_deployment

`check_deployment` is the pre-flight check. It refuses to pass if you are on
SQLite with DEBUG off, the chart of accounts is incomplete, the ledger does not
balance, or `SECRET_KEY` is still a development value. It exits non-zero, so a
deploy script can gate on it.

Then, in the finance panel:

1. **Accounts → each money account → Set opening balance.** What was actually
   in cash, the bank, bKash and Nagad on the day you start.
2. **Stock → Opening stock.** What was on the shelf, and what it cost you.
   Without this, FIFO has nothing to consume and early sales post no cost.
3. **Clients** and **Investors**, if you have dues or capital already in place.
4. **Integrations → Backfill** to post affiliate commissions and payouts that
   happened before this module existed. Preview first.

## 16. Finance settings in .env

    FINANCE_POST_AFFILIATE=True             # commissions and payouts hit the ledger
    FINANCE_AUTO_INVOICE_ON_DELIVERY=False  # auto-issue invoices on delivery
    FINANCE_REQUIRED_GROUP=                 # blank = any staff user
    FINANCE_LOW_STOCK_THRESHOLD=5

Set `FINANCE_REQUIRED_GROUP` to a Django group name as soon as more than one
person has admin access. Someone packing orders does not need to see the bank
balance or what each investor is owed.

## 17. BACKUP — do this before you need it

Accounting data cannot be reconstructed from anywhere else. A lost `store`
table is annoying; a lost ledger is the business's financial history gone.

### Database backup (cPanel)

Manual, before any deploy:

    cPanel → Backup → Download a MySQL Database Backup

Automatic, via cPanel → Cron Jobs, daily at 2am:

    0 2 * * * /usr/bin/mysqldump -u DBUSER -p'DBPASS' DBNAME | gzip > ~/backups/alphacue-$(date +\%F).sql.gz

Then prune anything older than 30 days:

    0 3 * * * find ~/backups -name 'alphacue-*.sql.gz' -mtime +30 -delete

Create `~/backups` first, and keep it outside `public_html` so it is not
downloadable over the web.

### Media backup

Product images live in `media/`. Weekly is enough:

    0 4 * * 0 tar -czf ~/backups/media-$(date +\%F).tar.gz -C ~/alphacue_imports media

### Test the restore

A backup you have never restored is a guess. Once, on a spare database:

    gunzip < ~/backups/alphacue-2026-08-05.sql.gz | mysql -u DBUSER -p'DBPASS' TEST_DBNAME

Then point `.env` at `TEST_DBNAME`, run `python manage.py check_deployment`,
and confirm the trial balance still comes to zero.

### Off-server copies

cPanel backups live on the same machine as the site. Download a copy monthly to
somewhere else — a laptop, Google Drive, anywhere that is not this server.

## 18. Routine health checks

    python manage.py check_deployment     # everything, before/after a deploy
    python manage.py reconcile_stock      # report stock drift
    python manage.py reconcile_stock --fix

The **Trial Balance** page is the single most useful check: it must always read
`৳0.00 ✓`. If it does not, something wrote to the ledger without going through
`post_transaction()` — investigate before trusting any figure in the system.

## 19. Upgrading an existing install

    python manage.py check_deployment     # snapshot the current state
    # ... take a database backup ...
    git pull
    pip install -r requirements.txt
    python manage.py migrate
    python manage.py collectstatic --noinput
    python manage.py check_deployment     # confirm nothing broke
    # Restart the app: cPanel → Setup Python App → Restart

Static files are fingerprinted by WhiteNoise, so `collectstatic` must run on
every deploy or the finance panel will load without its styling.
