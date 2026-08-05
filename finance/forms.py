"""
finance/forms.py
────────────────
M2 — data entry.

These forms validate what the user typed. They do not write to the ledger —
the views hand clean data to finance.services, which is the only thing that
posts. Keeping it that way means every rule in services.py holds no matter
where the data came from.
"""

from decimal import Decimal

from django import forms
from django.utils import timezone

from .models import (
    Account, Investor, Invoice, InvoiceItem, Loan, LoanInstallment, Party,
    Purchase, PurchaseItem,
)

ZERO = Decimal('0.00')

TEXT_INPUT = {'class': 'form-input'}
DATE_INPUT = {'class': 'form-input', 'type': 'date'}
NUMBER_INPUT = {'class': 'form-input', 'step': '0.01', 'min': '0'}
SELECT = {'class': 'form-input'}
TEXTAREA = {'class': 'form-input', 'rows': 2}


def _money_accounts():
    return Account.objects.filter(
        type__in=Account.MONEY_TYPES, is_active=True,
    ).order_by('code')


def _expense_accounts():
    return Account.objects.filter(
        type=Account.TYPE_EXPENSE, is_active=True,
    ).order_by('code')


# ─── Account management ───────────────────────────────────────────────────────

class AccountForm(forms.ModelForm):
    """
    Create or rename an account. System accounts from the seed keep their code
    and type locked — renaming "bKash" is fine, but repurposing 1030 into an
    expense account would silently rewrite the meaning of past entries.
    """

    class Meta:
        model = Account
        fields = ['code', 'name', 'type', 'description', 'is_active']
        widgets = {
            'code':        forms.TextInput(attrs={**TEXT_INPUT, 'placeholder': 'e.g. 1050'}),
            'name':        forms.TextInput(attrs={**TEXT_INPUT, 'placeholder': 'e.g. City Bank — Current'}),
            'type':        forms.Select(attrs=SELECT),
            'description': forms.TextInput(attrs={**TEXT_INPUT, 'placeholder': 'Optional note'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        instance = kwargs.get('instance') or self.instance
        if instance and instance.pk and instance.is_system:
            self.fields['code'].disabled = True
            self.fields['type'].disabled = True
            self.fields['code'].help_text = 'Locked — this is a standard account.'
            self.fields['type'].help_text = 'Locked — changing it would rewrite the meaning of past entries.'

    def clean_code(self):
        code = (self.cleaned_data.get('code') or '').strip()
        if not code:
            raise forms.ValidationError('A code is required.')
        return code


# ─── Opening balance ──────────────────────────────────────────────────────────

class OpeningBalanceForm(forms.Form):
    """
    What an account already held on the day the books started. Entered the
    natural way — cash you have and money you owe are both positive numbers.
    """

    date = forms.DateField(
        widget=forms.DateInput(attrs=DATE_INPUT),
        initial=timezone.now().date,
        help_text='The date your books start from.',
    )
    amount = forms.DecimalField(
        max_digits=14, decimal_places=2,
        widget=forms.NumberInput(attrs=NUMBER_INPUT),
        help_text='Enter as a positive number.',
    )

    def clean_amount(self):
        amount = self.cleaned_data['amount']
        if amount <= ZERO:
            raise forms.ValidationError('Enter a positive amount.')
        return amount


# ─── Expense ──────────────────────────────────────────────────────────────────

class ExpenseForm(forms.Form):
    """Money spent on a running cost."""

    date = forms.DateField(
        widget=forms.DateInput(attrs=DATE_INPUT),
        initial=timezone.now().date,
    )
    expense_account = forms.ModelChoiceField(
        queryset=Account.objects.none(),
        widget=forms.Select(attrs=SELECT),
        label='Category',
        help_text='Add more categories from Accounts → New account (type: Expense).',
    )
    paid_from = forms.ModelChoiceField(
        queryset=Account.objects.none(),
        widget=forms.Select(attrs=SELECT),
        label='Paid from',
    )
    amount = forms.DecimalField(
        max_digits=14, decimal_places=2,
        widget=forms.NumberInput(attrs=NUMBER_INPUT),
    )
    description = forms.CharField(
        max_length=255,
        widget=forms.TextInput(attrs={**TEXT_INPUT, 'placeholder': 'e.g. August shop rent'}),
    )
    memo = forms.CharField(
        max_length=255, required=False,
        widget=forms.TextInput(attrs={**TEXT_INPUT, 'placeholder': 'Optional — receipt no, payee, reference'}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['expense_account'].queryset = _expense_accounts()
        self.fields['paid_from'].queryset = _money_accounts()

    def clean_amount(self):
        amount = self.cleaned_data['amount']
        if amount <= ZERO:
            raise forms.ValidationError('Enter a positive amount.')
        return amount


# ─── Transfer ─────────────────────────────────────────────────────────────────

class TransferForm(forms.Form):
    """Money moved between accounts you own."""

    date = forms.DateField(
        widget=forms.DateInput(attrs=DATE_INPUT),
        initial=timezone.now().date,
    )
    from_account = forms.ModelChoiceField(
        queryset=Account.objects.none(),
        widget=forms.Select(attrs=SELECT),
        label='From',
    )
    to_account = forms.ModelChoiceField(
        queryset=Account.objects.none(),
        widget=forms.Select(attrs=SELECT),
        label='To',
    )
    amount = forms.DecimalField(
        max_digits=14, decimal_places=2,
        widget=forms.NumberInput(attrs=NUMBER_INPUT),
        label='Amount received',
    )
    fee = forms.DecimalField(
        max_digits=14, decimal_places=2, required=False, initial=ZERO,
        widget=forms.NumberInput(attrs=NUMBER_INPUT),
        label='Charge',
        help_text='bKash/Nagad cash-out or bank fee. Taken from the source account on top of the amount.',
    )
    description = forms.CharField(
        max_length=255, required=False,
        widget=forms.TextInput(attrs={**TEXT_INPUT, 'placeholder': 'Optional — filled in automatically'}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['from_account'].queryset = _money_accounts()
        self.fields['to_account'].queryset = _money_accounts()

    def clean_amount(self):
        amount = self.cleaned_data['amount']
        if amount <= ZERO:
            raise forms.ValidationError('Enter a positive amount.')
        return amount

    def clean_fee(self):
        fee = self.cleaned_data.get('fee') or ZERO
        if fee < ZERO:
            raise forms.ValidationError('A charge cannot be negative.')
        return fee

    def clean(self):
        cleaned = super().clean()
        source = cleaned.get('from_account')
        target = cleaned.get('to_account')
        if source and target and source.pk == target.pk:
            raise forms.ValidationError(
                'Pick two different accounts — money cannot move to where it already is.'
            )
        return cleaned


# ─── Day book filter ──────────────────────────────────────────────────────────

class DaybookFilterForm(forms.Form):
    """Date range and optional single-account filter for the cash book."""

    since = forms.DateField(
        required=False, widget=forms.DateInput(attrs=DATE_INPUT), label='From',
    )
    as_of = forms.DateField(
        required=False, widget=forms.DateInput(attrs=DATE_INPUT), label='To',
    )
    account = forms.ModelChoiceField(
        queryset=Account.objects.none(), required=False,
        widget=forms.Select(attrs=SELECT),
        empty_label='All money accounts',
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['account'].queryset = _money_accounts()

    def clean(self):
        cleaned = super().clean()
        since = cleaned.get('since')
        as_of = cleaned.get('as_of')
        if since and as_of and since > as_of:
            raise forms.ValidationError('The "from" date is after the "to" date.')
        return cleaned


# ─── Reversal ─────────────────────────────────────────────────────────────────

class ReverseTransactionForm(forms.Form):
    """Undo a posted entry. The original stays in the ledger."""

    reason = forms.CharField(
        max_length=200,
        widget=forms.TextInput(attrs={**TEXT_INPUT, 'placeholder': 'e.g. Entered twice by mistake'}),
        help_text='Recorded on the reversal so the history explains itself.',
    )


# ─── M3: Parties ──────────────────────────────────────────────────────────────

class PartyForm(forms.ModelForm):
    """A client, walk-in or supplier."""

    class Meta:
        model = Party
        fields = ['name', 'party_type', 'phone', 'email', 'address',
                  'credit_limit', 'notes', 'is_active']
        widgets = {
            'name':         forms.TextInput(attrs={**TEXT_INPUT, 'placeholder': 'e.g. Rahim Traders'}),
            'party_type':   forms.Select(attrs=SELECT),
            'phone':        forms.TextInput(attrs={**TEXT_INPUT, 'placeholder': '01XXXXXXXXX'}),
            'email':        forms.EmailInput(attrs={**TEXT_INPUT, 'placeholder': 'Optional'}),
            'address':      forms.Textarea(attrs=TEXTAREA),
            'credit_limit': forms.NumberInput(attrs=NUMBER_INPUT),
            'notes':        forms.Textarea(attrs=TEXTAREA),
        }

    def clean_name(self):
        name = (self.cleaned_data.get('name') or '').strip()
        if not name:
            raise forms.ValidationError('A name is required.')
        return name


# ─── M3: Invoices ─────────────────────────────────────────────────────────────

class InvoiceForm(forms.ModelForm):
    """
    The invoice header. Only editable while the invoice is a draft — once
    issued it is on the ledger and the views stop offering this form.
    """

    class Meta:
        model = Invoice
        fields = ['party', 'issue_date', 'payment_terms_days',
                  'discount', 'delivery_charge', 'notes']
        widgets = {
            'party':              forms.Select(attrs=SELECT),
            'issue_date':         forms.DateInput(attrs=DATE_INPUT),
            'payment_terms_days': forms.Select(attrs=SELECT),
            'discount':           forms.NumberInput(attrs=NUMBER_INPUT),
            'delivery_charge':    forms.NumberInput(attrs=NUMBER_INPUT),
            'notes':              forms.Textarea(attrs={**TEXTAREA, 'rows': 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['party'].queryset = Party.objects.filter(
            is_active=True,
        ).exclude(party_type=Party.TYPE_SUPPLIER).order_by('name')
        self.fields['notes'].help_text = (
            'Shown at the bottom of the printed invoice — payment instructions, terms, thanks.'
        )

    def clean_discount(self):
        discount = self.cleaned_data.get('discount') or ZERO
        if discount < ZERO:
            raise forms.ValidationError('A discount cannot be negative.')
        return discount

    def clean_delivery_charge(self):
        charge = self.cleaned_data.get('delivery_charge') or ZERO
        if charge < ZERO:
            raise forms.ValidationError('A delivery charge cannot be negative.')
        return charge


class InvoiceItemForm(forms.ModelForm):
    """
    One line. Blank rows in the formset are ignored.

    A line can either point at a catalogue product — picked through the search
    box, which fills in the description, SKU and price — or be typed freehand,
    which is what you want for a delivery charge, a repair, or anything that
    was never a catalogue item. That is why `variant` stays optional and the
    text fields remain editable after a product is chosen: the invoice keeps a
    snapshot, so renaming or repricing the product later never rewrites a bill
    that was already sent.
    """

    class Meta:
        model = InvoiceItem
        fields = ['variant', 'description', 'sku', 'unit_price', 'quantity', 'discount']
        widgets = {
            'variant':     forms.HiddenInput(),
            'description': forms.TextInput(attrs={**TEXT_INPUT, 'placeholder': 'Item description'}),
            'sku':         forms.TextInput(attrs={**TEXT_INPUT, 'placeholder': 'Optional'}),
            'unit_price':  forms.NumberInput(attrs=NUMBER_INPUT),
            'quantity':    forms.NumberInput(attrs={'class': 'form-input', 'min': '1', 'step': '1'}),
            'discount':    forms.NumberInput(attrs=NUMBER_INPUT),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from store.models import ProductVariant
        self.fields['variant'].queryset = ProductVariant.objects.select_related('product')
        self.fields['variant'].required = False

    @property
    def variant_label(self):
        """
        What the search box shows for a line that already has a product.

        Checks the submitted data first so a form redisplayed with errors keeps
        the product the user picked, rather than blanking the box and making
        them find it again.
        """
        variant = None
        if self.is_bound:
            raw = (self.data.get(self.add_prefix('variant')) or '').strip()
            if raw:
                variant = self.fields['variant'].queryset.filter(pk=raw).first()
        if variant is None:
            variant = getattr(self.instance, 'variant', None)
        return str(variant) if variant else ''

    def has_changed(self):
        """
        Treat a spare row with no description as unused.

        Without this, the quantity and discount fields render pre-filled from
        their model defaults, so Django sees an untouched spare row as "changed"
        and demands a price for it. Existing lines are exempt — clearing the
        description on a saved line should be an error, not a silent skip.
        """
        if self.instance.pk is None:
            description = (self.data.get(self.add_prefix('description'), '') or '').strip()
            if not description:
                return False
        return super().has_changed()

    def clean_unit_price(self):
        price = self.cleaned_data.get('unit_price')
        if price is not None and price < ZERO:
            raise forms.ValidationError('Price cannot be negative.')
        return price

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('DELETE'):
            return cleaned

        discount = cleaned.get('discount') or ZERO
        price = cleaned.get('unit_price')
        quantity = cleaned.get('quantity') or 0

        if price is not None and discount > price * quantity:
            raise forms.ValidationError(
                'The line discount is larger than the line itself.'
            )
        return cleaned


InvoiceItemFormSet = forms.inlineformset_factory(
    Invoice, InvoiceItem,
    form=InvoiceItemForm,
    extra=4,
    can_delete=True,
    min_num=0,
    validate_min=False,
)


class WalkInInvoiceForm(forms.Form):
    """
    Quick invoice for someone with no record — captures just enough to bill
    them, and creates the party behind the scenes.
    """

    customer_name = forms.CharField(
        max_length=150,
        widget=forms.TextInput(attrs={**TEXT_INPUT, 'placeholder': 'Customer name'}),
    )
    phone = forms.CharField(
        max_length=30, required=False,
        widget=forms.TextInput(attrs={**TEXT_INPUT, 'placeholder': 'Optional'}),
    )
    issue_date = forms.DateField(
        widget=forms.DateInput(attrs=DATE_INPUT), initial=timezone.now().date,
    )

    def clean_customer_name(self):
        name = (self.cleaned_data.get('customer_name') or '').strip()
        if not name:
            raise forms.ValidationError('A name is required.')
        return name


class InvoiceCancelForm(forms.Form):
    reason = forms.CharField(
        max_length=200,
        widget=forms.TextInput(attrs={**TEXT_INPUT, 'placeholder': 'e.g. Client cancelled the order'}),
    )


# ─── M4: Payments ─────────────────────────────────────────────────────────────

class PaymentForm(forms.Form):
    """
    Money received from a client.

    Allocation is handled separately: leave it alone and the payment settles
    the oldest invoices first, or tick "choose invoices" to decide by hand.
    """

    party = forms.ModelChoiceField(
        queryset=Party.objects.none(),
        widget=forms.Select(attrs=SELECT),
        label='Client',
    )
    date = forms.DateField(
        widget=forms.DateInput(attrs=DATE_INPUT), initial=timezone.now().date,
    )
    amount = forms.DecimalField(
        max_digits=14, decimal_places=2,
        widget=forms.NumberInput(attrs=NUMBER_INPUT),
        label='Amount received',
    )
    account = forms.ModelChoiceField(
        queryset=Account.objects.none(),
        widget=forms.Select(attrs=SELECT),
        label='Received into',
    )
    reference = forms.CharField(
        max_length=100, required=False,
        widget=forms.TextInput(attrs={**TEXT_INPUT, 'placeholder': 'bKash TrxID, cheque no, bank ref'}),
    )
    notes = forms.CharField(
        max_length=255, required=False,
        widget=forms.TextInput(attrs={**TEXT_INPUT, 'placeholder': 'Optional'}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['party'].queryset = Party.objects.filter(
            is_active=True,
        ).exclude(party_type=Party.TYPE_SUPPLIER).order_by('name')
        self.fields['account'].queryset = _money_accounts()

    def clean_amount(self):
        amount = self.cleaned_data['amount']
        if amount <= ZERO:
            raise forms.ValidationError('Enter a positive amount.')
        return amount


class SupplierPaymentForm(forms.Form):
    """Money paid out to a supplier."""

    party = forms.ModelChoiceField(
        queryset=Party.objects.none(),
        widget=forms.Select(attrs=SELECT),
        label='Supplier',
    )
    date = forms.DateField(
        widget=forms.DateInput(attrs=DATE_INPUT), initial=timezone.now().date,
    )
    amount = forms.DecimalField(
        max_digits=14, decimal_places=2,
        widget=forms.NumberInput(attrs=NUMBER_INPUT),
    )
    paid_from = forms.ModelChoiceField(
        queryset=Account.objects.none(),
        widget=forms.Select(attrs=SELECT),
    )
    reference = forms.CharField(
        max_length=100, required=False,
        widget=forms.TextInput(attrs={**TEXT_INPUT, 'placeholder': 'Optional reference'}),
    )
    notes = forms.CharField(
        max_length=255, required=False,
        widget=forms.TextInput(attrs=TEXT_INPUT),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['party'].queryset = Party.objects.filter(
            party_type=Party.TYPE_SUPPLIER, is_active=True,
        ).order_by('name')
        self.fields['paid_from'].queryset = _money_accounts()

    def clean_amount(self):
        amount = self.cleaned_data['amount']
        if amount <= ZERO:
            raise forms.ValidationError('Enter a positive amount.')
        return amount


class PaymentReverseForm(forms.Form):
    reason = forms.CharField(
        max_length=200,
        widget=forms.TextInput(attrs={**TEXT_INPUT, 'placeholder': 'e.g. Cheque bounced'}),
    )


# ─── Catalogue: products and variants ─────────────────────────────────────────

class ProductForm(forms.ModelForm):
    """
    Add a catalogue product without leaving the finance panel.

    Deliberately narrow: name, category, and the descriptions that show on the
    storefront. Images, SEO fields and the featured flag stay in Django admin —
    someone entering a purchase needs the product to exist, not a full
    merchandising screen.
    """

    class Meta:
        from store.models import Product
        model = Product
        fields = ['name', 'category', 'sku', 'short_description', 'is_active']
        widgets = {
            'name':              forms.TextInput(attrs={**TEXT_INPUT, 'placeholder': 'e.g. USB-C Cable 2m'}),
            'category':          forms.Select(attrs=SELECT),
            'sku':               forms.TextInput(attrs={**TEXT_INPUT, 'placeholder': 'Leave blank to generate one'}),
            'short_description': forms.TextInput(attrs={**TEXT_INPUT, 'placeholder': 'Optional — shown on listing cards'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from store.models import Category
        self.fields['category'].queryset = Category.objects.order_by('sort_order', 'name')
        self.fields['sku'].required = False
        self.fields['category'].help_text = 'Every product needs one.'

    def clean_name(self):
        name = (self.cleaned_data.get('name') or '').strip()
        if not name:
            raise forms.ValidationError('A product name is required.')
        return name


class ProductVariantForm(forms.ModelForm):
    """
    One sellable version of a product — a size, a colour, a wattage.

    A product with no options still needs one variant, because price and stock
    live here rather than on the product.
    """

    class Meta:
        from store.models import ProductVariant
        model = ProductVariant
        fields = ['name', 'sku', 'price', 'compare_price', 'track_stock', 'is_active']
        widgets = {
            'name':          forms.TextInput(attrs={**TEXT_INPUT, 'placeholder': 'e.g. Black, or Standard'}),
            'sku':           forms.TextInput(attrs={**TEXT_INPUT, 'placeholder': 'Auto'}),
            'price':         forms.NumberInput(attrs=NUMBER_INPUT),
            'compare_price': forms.NumberInput(attrs=NUMBER_INPUT),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['sku'].required = False
        self.fields['price'].required = False

    def has_changed(self):
        """A spare row with no name is an unused option, not a mistake."""
        if self.instance.pk is None:
            if not (self.data.get(self.add_prefix('name')) or '').strip():
                return False
        return super().has_changed()

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('DELETE'):
            return cleaned

        name = (cleaned.get('name') or '').strip()
        price = cleaned.get('price')

        if name and price is None:
            self.add_error('price', 'Give this option a selling price.')
        elif name and price <= ZERO:
            self.add_error('price', 'The price must be more than zero.')

        compare = cleaned.get('compare_price')
        if compare is not None and price is not None and compare <= price:
            self.add_error(
                'compare_price',
                'The crossed-out price should be higher than the selling price.',
            )
        return cleaned


class VariantInlineFormSet(forms.BaseInlineFormSet):
    """
    Lets several options be added at once without typing a SKU for each.

    `ProductVariant.save()` generates a SKU when the field is blank, but the
    formset's uniqueness check runs *before* that — so two rows both sitting at
    `''` looked like a duplicate SKU and the whole form was rejected. Blank
    rows get a throwaway placeholder for the duration of the check and are put
    back to blank afterwards, so the model still generates the real codes.
    """

    def validate_unique(self):
        # Django's duplicate check reads `cleaned_data`, not the instance, and
        # skips any field that is absent from it. Dropping the blank SKUs for
        # the duration of the check is therefore enough — the instances still
        # carry '', so ProductVariant.save() generates the real codes.
        removed = []
        for form in self.forms:
            if not hasattr(form, 'cleaned_data'):
                continue
            if not (form.cleaned_data.get('sku') or '').strip():
                removed.append(form)
                form.cleaned_data.pop('sku', None)
        try:
            super().validate_unique()
        finally:
            for form in removed:
                form.cleaned_data['sku'] = ''


def build_variant_formset(extra=3):
    from store.models import Product, ProductVariant
    return forms.inlineformset_factory(
        Product, ProductVariant,
        form=ProductVariantForm,
        formset=VariantInlineFormSet,
        extra=extra, can_delete=True, min_num=0, validate_min=False,
    )


class CategoryForm(forms.ModelForm):
    class Meta:
        from store.models import Category
        model = Category
        fields = ['name', 'description', 'sort_order', 'is_active']
        widgets = {
            'name':        forms.TextInput(attrs={**TEXT_INPUT, 'placeholder': 'e.g. Cables & Adapters'}),
            'description': forms.Textarea(attrs=TEXTAREA),
            'sort_order':  forms.NumberInput(attrs={'class': 'form-input', 'min': '0'}),
        }


# ─── M5: Purchases ────────────────────────────────────────────────────────────

class PurchaseForm(forms.ModelForm):
    """
    The shipment header. RMB fields only matter for imports; the template hides
    them when the type is local.
    """

    class Meta:
        model = Purchase
        fields = ['purchase_type', 'supplier', 'purchase_date',
                  'fx_rate_rmb_to_bdt', 'default_per_kg_charge_bdt',
                  'billed_weight_kg', 'extra_cost_bdt', 'correction_percent',
                  'notes']
        widgets = {
            'purchase_type':             forms.Select(attrs=SELECT),
            'supplier':                  forms.Select(attrs=SELECT),
            'purchase_date':             forms.DateInput(attrs=DATE_INPUT),
            'fx_rate_rmb_to_bdt':        forms.NumberInput(attrs={**NUMBER_INPUT, 'step': '0.0001'}),
            'default_per_kg_charge_bdt': forms.NumberInput(attrs=NUMBER_INPUT),
            'billed_weight_kg':          forms.NumberInput(attrs={**NUMBER_INPUT, 'step': '0.001'}),
            'extra_cost_bdt':            forms.NumberInput(attrs=NUMBER_INPUT),
            'correction_percent':        forms.NumberInput(attrs=NUMBER_INPUT),
            'notes':                     forms.Textarea(attrs=TEXTAREA),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['supplier'].queryset = Party.objects.filter(
            is_active=True,
        ).order_by('name')
        self.fields['billed_weight_kg'].help_text = (
            "The weight the agent charged you for. Leave at 0 until the shipment "
            "lands — the line weights are scaled to match it."
        )

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('purchase_type') == Purchase.TYPE_IMPORT:
            fx = cleaned.get('fx_rate_rmb_to_bdt') or ZERO
            if fx <= ZERO:
                self.add_error(
                    'fx_rate_rmb_to_bdt',
                    'An import purchase needs the RMB exchange rate.',
                )
        return cleaned


class PurchaseItemForm(forms.ModelForm):
    class Meta:
        model = PurchaseItem
        fields = ['variant', 'quantity', 'unit_price_rmb', 'domestic_shipping_rmb',
                  'unit_cost_bdt', 'local_transport_bdt',
                  'entered_weight_kg', 'per_kg_charge_bdt']
        widgets = {
            'variant':               forms.HiddenInput(),
            'quantity':              forms.NumberInput(attrs={'class': 'form-input', 'min': '1', 'step': '1'}),
            'unit_price_rmb':        forms.NumberInput(attrs=NUMBER_INPUT),
            'domestic_shipping_rmb': forms.NumberInput(attrs=NUMBER_INPUT),
            'unit_cost_bdt':         forms.NumberInput(attrs=NUMBER_INPUT),
            'local_transport_bdt':   forms.NumberInput(attrs=NUMBER_INPUT),
            'entered_weight_kg':     forms.NumberInput(attrs={**NUMBER_INPUT, 'step': '0.001'}),
            'per_kg_charge_bdt':     forms.NumberInput(attrs=NUMBER_INPUT),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from store.models import ProductVariant
        # Not filtered to active products: an existing purchase line must keep
        # validating even if the product was retired after the order was placed.
        self.fields['variant'].queryset = ProductVariant.objects.select_related('product')
        self.fields['variant'].required = False

    @property
    def variant_label(self):
        """What the search box shows for a row that already has a product."""
        variant = None
        if self.is_bound:
            raw = (self.data.get(self.add_prefix('variant')) or '').strip()
            if raw:
                variant = self.fields['variant'].queryset.filter(pk=raw).first()
        if variant is None:
            variant = getattr(self.instance, 'variant', None)
        return str(variant) if variant else ''

    def has_changed(self):
        """A spare row with no product picked is unused, whatever else is in it."""
        if self.instance.pk is None:
            if not (self.data.get(self.add_prefix('variant')) or '').strip():
                return False
        return super().has_changed()

    def clean_variant(self):
        variant = self.cleaned_data.get('variant')
        if not variant:
            raise forms.ValidationError('Pick a product for this line.')
        return variant


PurchaseItemFormSet = forms.inlineformset_factory(
    Purchase, PurchaseItem,
    form=PurchaseItemForm,
    extra=4, can_delete=True, min_num=0, validate_min=False,
)


class PurchaseReceiveForm(forms.Form):
    """Stage 2 — confirm the arrival date and put the stock in."""

    received_date = forms.DateField(
        widget=forms.DateInput(attrs=DATE_INPUT), initial=timezone.now().date,
    )


class PurchaseCancelForm(forms.Form):
    reason = forms.CharField(
        max_length=200,
        widget=forms.TextInput(attrs={**TEXT_INPUT, 'placeholder': 'e.g. Supplier could not ship'}),
    )


# ─── M6: Stock ────────────────────────────────────────────────────────────────

class StockAdjustmentForm(forms.Form):
    """
    Correct a stock figure by hand — a stocktake difference, breakage, or units
    that never turned up.
    """

    ADJUST_ADD    = 'add'
    ADJUST_REMOVE = 'remove'
    DIRECTION_CHOICES = [
        (ADJUST_ADD,    'Add stock (found / stocktake gain)'),
        (ADJUST_REMOVE, 'Remove stock (damaged / lost / stocktake loss)'),
    ]

    variant = forms.ModelChoiceField(
        queryset=None, widget=forms.Select(attrs=SELECT), label='Product',
    )
    direction = forms.ChoiceField(
        choices=DIRECTION_CHOICES, widget=forms.Select(attrs=SELECT),
    )
    quantity = forms.IntegerField(
        min_value=1, widget=forms.NumberInput(attrs={'class': 'form-input', 'min': '1'}),
    )
    unit_cost = forms.DecimalField(
        max_digits=12, decimal_places=2, required=False,
        widget=forms.NumberInput(attrs=NUMBER_INPUT),
        help_text='Only used when adding. Leave blank to use the current average cost.',
    )
    date = forms.DateField(
        widget=forms.DateInput(attrs=DATE_INPUT), initial=timezone.now().date,
    )
    note = forms.CharField(
        max_length=200,
        widget=forms.TextInput(attrs={**TEXT_INPUT, 'placeholder': 'Why is this being adjusted?'}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from store.models import ProductVariant
        self.fields['variant'].queryset = ProductVariant.objects.select_related(
            'product',
        ).filter(is_active=True).order_by('product__name', 'sort_order')


class OpeningStockForm(forms.Form):
    """Stock already on the shelf when the system starts."""

    variant = forms.ModelChoiceField(
        queryset=None, widget=forms.Select(attrs=SELECT), label='Product',
    )
    quantity = forms.IntegerField(
        min_value=1, widget=forms.NumberInput(attrs={'class': 'form-input', 'min': '1'}),
    )
    unit_cost = forms.DecimalField(
        max_digits=12, decimal_places=2,
        widget=forms.NumberInput(attrs=NUMBER_INPUT),
        help_text='What one unit cost you, landed.',
    )
    date = forms.DateField(
        widget=forms.DateInput(attrs=DATE_INPUT), initial=timezone.now().date,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from store.models import ProductVariant
        self.fields['variant'].queryset = ProductVariant.objects.select_related(
            'product',
        ).filter(is_active=True).order_by('product__name', 'sort_order')


# ─── M7: Investors ────────────────────────────────────────────────────────────

class InvestorForm(forms.ModelForm):
    class Meta:
        model = Investor
        fields = ['name', 'phone', 'email', 'ownership_percent', 'joined_on',
                  'notes', 'is_active']
        widgets = {
            'name':              forms.TextInput(attrs=TEXT_INPUT),
            'phone':             forms.TextInput(attrs=TEXT_INPUT),
            'email':             forms.EmailInput(attrs=TEXT_INPUT),
            'ownership_percent': forms.NumberInput(attrs={**NUMBER_INPUT, 'step': '0.001'}),
            'joined_on':         forms.DateInput(attrs=DATE_INPUT),
            'notes':             forms.Textarea(attrs=TEXTAREA),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['ownership_percent'].help_text = (
            'Leave blank to work the share out from capital contributed.'
        )


class CapitalForm(forms.Form):
    """Money in or out of an investor's stake."""

    MOVEMENT_IN  = 'in'
    MOVEMENT_OUT = 'out'
    MOVEMENT_CHOICES = [
        (MOVEMENT_IN,  'Capital in — investor puts money in'),
        (MOVEMENT_OUT, 'Drawing — investor takes money out'),
    ]

    movement = forms.ChoiceField(
        choices=MOVEMENT_CHOICES, widget=forms.Select(attrs=SELECT),
    )
    date = forms.DateField(
        widget=forms.DateInput(attrs=DATE_INPUT), initial=timezone.now().date,
    )
    amount = forms.DecimalField(
        max_digits=14, decimal_places=2, widget=forms.NumberInput(attrs=NUMBER_INPUT),
    )
    account = forms.ModelChoiceField(
        queryset=Account.objects.none(), widget=forms.Select(attrs=SELECT),
        label='Money account',
    )
    notes = forms.CharField(
        max_length=200, required=False, widget=forms.TextInput(attrs=TEXT_INPUT),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['account'].queryset = _money_accounts()

    def clean_amount(self):
        amount = self.cleaned_data['amount']
        if amount <= ZERO:
            raise forms.ValidationError('Enter a positive amount.')
        return amount


class ProfitDistributionForm(forms.Form):
    """Share out a period's profit between the investors."""

    period_start = forms.DateField(widget=forms.DateInput(attrs=DATE_INPUT))
    period_end = forms.DateField(widget=forms.DateInput(attrs=DATE_INPUT))
    distribution_date = forms.DateField(
        widget=forms.DateInput(attrs=DATE_INPUT), initial=timezone.now().date,
    )
    amount = forms.DecimalField(
        max_digits=14, decimal_places=2, required=False,
        widget=forms.NumberInput(attrs=NUMBER_INPUT),
        help_text='Leave blank to share out the whole profit for the period.',
    )
    notes = forms.CharField(
        max_length=200, required=False, widget=forms.TextInput(attrs=TEXT_INPUT),
    )

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get('period_start'), cleaned.get('period_end')
        if start and end and start > end:
            raise forms.ValidationError('The period starts after it ends.')
        return cleaned


# ─── M8: Loans ────────────────────────────────────────────────────────────────

class LoanForm(forms.Form):
    """Record a loan and build its repayment schedule."""

    direction = forms.ChoiceField(
        choices=Loan.DIRECTION_CHOICES, widget=forms.Select(attrs=SELECT),
    )
    counterparty_name = forms.CharField(
        max_length=150,
        widget=forms.TextInput(attrs={**TEXT_INPUT, 'placeholder': 'e.g. City Bank, or a relative'}),
        label='Lender / borrower',
    )
    principal = forms.DecimalField(
        max_digits=14, decimal_places=2, widget=forms.NumberInput(attrs=NUMBER_INPUT),
        label='Amount',
    )
    interest_rate = forms.DecimalField(
        max_digits=6, decimal_places=3, initial=Decimal('0.000'),
        widget=forms.NumberInput(attrs={**NUMBER_INPUT, 'step': '0.001'}),
        label='Annual interest rate %',
        help_text='0 for an interest-free loan.',
    )
    method = forms.ChoiceField(
        choices=Loan.METHOD_CHOICES, widget=forms.Select(attrs=SELECT),
    )
    tenure_months = forms.IntegerField(
        min_value=1, max_value=600, initial=12,
        widget=forms.NumberInput(attrs={'class': 'form-input', 'min': '1'}),
        label='Number of monthly installments',
    )
    start_date = forms.DateField(
        widget=forms.DateInput(attrs=DATE_INPUT), initial=timezone.now().date,
    )
    account = forms.ModelChoiceField(
        queryset=Account.objects.none(), widget=forms.Select(attrs=SELECT),
        label='Money account',
        help_text='Where the money landed, or where it was paid from.',
    )
    notes = forms.CharField(
        max_length=200, required=False, widget=forms.TextInput(attrs=TEXT_INPUT),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['account'].queryset = _money_accounts()

    def clean_principal(self):
        principal = self.cleaned_data['principal']
        if principal <= ZERO:
            raise forms.ValidationError('Enter a positive amount.')
        return principal


class LoanPaymentForm(forms.Form):
    """One repayment, split between principal and interest."""

    date = forms.DateField(
        widget=forms.DateInput(attrs=DATE_INPUT), initial=timezone.now().date,
    )
    principal_amount = forms.DecimalField(
        max_digits=14, decimal_places=2, widget=forms.NumberInput(attrs=NUMBER_INPUT),
        label='Principal',
    )
    interest_amount = forms.DecimalField(
        max_digits=14, decimal_places=2, initial=Decimal('0.00'),
        widget=forms.NumberInput(attrs=NUMBER_INPUT), label='Interest',
    )
    account = forms.ModelChoiceField(
        queryset=Account.objects.none(), widget=forms.Select(attrs=SELECT),
        label='Money account',
    )
    installment = forms.ModelChoiceField(
        queryset=LoanInstallment.objects.none(), required=False,
        widget=forms.Select(attrs=SELECT),
        empty_label='Not against a specific installment',
    )
    reference = forms.CharField(
        max_length=100, required=False, widget=forms.TextInput(attrs=TEXT_INPUT),
    )

    def __init__(self, *args, loan=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['account'].queryset = _money_accounts()
        if loan is not None:
            self.fields['installment'].queryset = loan.installments.exclude(
                status=LoanInstallment.STATUS_PAID,
            ).order_by('due_date')

    def clean(self):
        cleaned = super().clean()
        principal = cleaned.get('principal_amount') or ZERO
        interest = cleaned.get('interest_amount') or ZERO
        if principal < ZERO or interest < ZERO:
            raise forms.ValidationError('Amounts cannot be negative.')
        if principal + interest <= ZERO:
            raise forms.ValidationError('Enter a repayment amount.')
        return cleaned


class StatementFilterForm(forms.Form):
    since = forms.DateField(
        required=False, widget=forms.DateInput(attrs=DATE_INPUT), label='From',
    )
    as_of = forms.DateField(
        required=False, widget=forms.DateInput(attrs=DATE_INPUT), label='To',
    )

    def clean(self):
        cleaned = super().clean()
        since, as_of = cleaned.get('since'), cleaned.get('as_of')
        if since and as_of and since > as_of:
            raise forms.ValidationError('The "from" date is after the "to" date.')
        return cleaned
