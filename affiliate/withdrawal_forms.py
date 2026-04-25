from django import forms
from .models import WithdrawalRequest


class WithdrawalRequestForm(forms.ModelForm):
    class Meta:
        model  = WithdrawalRequest
        fields = ['amount', 'payment_method', 'payment_account']
        widgets = {
            'amount': forms.NumberInput(attrs={
                'class': 'form-input',
                'placeholder': 'e.g. 500',
                'step': '0.01',
                'min': '1',
            }),
            'payment_method': forms.Select(attrs={
                'class': 'form-input',
            }),
            'payment_account': forms.TextInput(attrs={
                'class': 'form-input',
                'placeholder': '01XXXXXXXXX',
            }),
        }
        labels = {
            'amount':         'Withdrawal Amount (৳)',
            'payment_method': 'Payment Method',
            'payment_account':'Account Number',
        }

    def __init__(self, *args, affiliate=None, min_amount=None, max_amount=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.affiliate   = affiliate
        self.min_amount  = min_amount
        self.max_amount  = max_amount

        # Pre-fill account from affiliate profile
        if affiliate and affiliate.payment_account_number:
            self.fields['payment_account'].initial = affiliate.payment_account_number
        if affiliate:
            self.fields['payment_method'].initial = affiliate.preferred_payment_method

    def clean_amount(self):
        amount = self.cleaned_data.get('amount')
        if amount is None:
            raise forms.ValidationError("Please enter an amount.")
        if self.min_amount and amount < self.min_amount:
            raise forms.ValidationError(
                f"Minimum withdrawal amount is ৳{self.min_amount:,.0f}."
            )
        if self.max_amount and amount > self.max_amount:
            raise forms.ValidationError(
                f"You can only withdraw up to ৳{self.max_amount:,.0f} (your available balance)."
            )
        return amount

    def clean_payment_account(self):
        account = self.cleaned_data.get('payment_account', '').strip()
        digits  = account.replace('+', '').replace('-', '').replace(' ', '')
        if not digits.isdigit() or len(digits) < 10:
            raise forms.ValidationError("Enter a valid mobile number.")
        return account
