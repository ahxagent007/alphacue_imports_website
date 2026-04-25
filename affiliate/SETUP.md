# ═══════════════════════════════════════════════════════════
#  AlphaCue Affiliate System — Milestone 1 Setup Guide
# ═══════════════════════════════════════════════════════════

## FILES DELIVERED
- affiliate/models.py          ← All 7 database models
- affiliate/admin.py           ← Full admin panel configuration
- affiliate/apps.py            ← App config
- affiliate/settings_additions.py  ← Paste these into your settings.py

## STEP-BY-STEP SETUP

### 1. Place the affiliate/ folder inside your Django project root

### 2. Add to INSTALLED_APPS in settings.py
    'affiliate',

### 3. Paste the affiliate settings into settings.py
    AFFILIATE_COOKIE_NAME = 'alphacue_ref'
    AFFILIATE_COOKIE_MAX_AGE = 2592000
    AFFILIATE_SESSION_KEY = 'affiliate_referral_code'

### 4. Run migrations
    python manage.py makemigrations affiliate
    python manage.py migrate

### 5. Seed a default commission setting (run once)
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

### 6. Login to /admin/ — you will see all 7 models under
    "Affiliate & Reseller System"

## MODELS CREATED
┌──────────────────────┬────────────────────────────────────────────┐
│ Model                │ Purpose                                    │
├──────────────────────┼────────────────────────────────────────────┤
│ AffiliateProfile     │ Core affiliate entity (1:1 with User)      │
│ CommissionSetting    │ Admin-configurable commission rates        │
│ ReferralClick        │ Raw click tracking log                     │
│ Commission           │ Earnings ledger (pending→approved→paid)    │
│ WithdrawalRequest    │ bKash/Nagad payout requests                │
│ ResellerPricing      │ Per-product custom pricing per affiliate   │
│ FraudFlag            │ Fraud investigation log                    │
└──────────────────────┴────────────────────────────────────────────┘

## TESTING CHECKLIST
[ ] python manage.py check          → No errors
[ ] makemigrations + migrate        → All tables created
[ ] /admin/ login                   → All 7 models visible
[ ] Create CommissionSetting        → is_default=True saves correctly
[ ] Create AffiliateProfile         → referral_code auto-generated
[ ] Duplicate Commission attempt    → IntegrityError (UniqueConstraint works)
[ ] Approve affiliate (bulk action) → Status changes to 'approved'
