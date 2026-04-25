from django import forms
from .models import AffiliateProfile


class AffiliateApplicationForm(forms.ModelForm):
    """
    Form for logged-in users to apply as affiliates.
    Excludes all system-managed fields — only collects applicant info.
    """

    class Meta:
        model = AffiliateProfile
        fields = [
            'full_name',
            'phone_number',
            'nid_number',
            'how_will_promote',
            'preferred_payment_method',
            'payment_account_number',
        ]
        widgets = {
            'full_name': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Your full name',
            }),
            'phone_number': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': '01XXXXXXXXX',
            }),
            'nid_number': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'National ID number (optional)',
            }),
            'how_will_promote': forms.Textarea(attrs={
                'class': 'form-control',
                'rows': 4,
                'placeholder': 'Describe how you plan to promote AlphaCue Imports...',
            }),
            'preferred_payment_method': forms.Select(attrs={
                'class': 'form-select',
            }),
            'payment_account_number': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'bKash / Nagad mobile number',
            }),
        }
        labels = {
            'full_name': 'Full Name',
            'phone_number': 'Phone Number',
            'nid_number': 'National ID (NID)',
            'how_will_promote': 'How Will You Promote?',
            'preferred_payment_method': 'Preferred Payment Method',
            'payment_account_number': 'Payment Account Number',
        }
        help_texts = {
            'nid_number': 'Optional but recommended for faster approval.',
            'how_will_promote': 'Tell us about your audience, platform, or strategy.',
            'payment_account_number': 'The mobile number registered with your bKash or Nagad account.',
        }

    def clean_phone_number(self):
        phone = self.cleaned_data.get('phone_number', '').strip()
        digits = phone.replace('+', '').replace('-', '').replace(' ', '')
        if not digits.isdigit():
            raise forms.ValidationError("Enter a valid phone number.")
        if len(digits) < 10 or len(digits) > 15:
            raise forms.ValidationError("Phone number must be 10–15 digits.")
        return phone

    def clean_payment_account_number(self):
        account = self.cleaned_data.get('payment_account_number', '').strip()
        if account:
            digits = account.replace('+', '').replace('-', '').replace(' ', '')
            if not digits.isdigit() or len(digits) < 10:
                raise forms.ValidationError("Enter a valid mobile number for payment.")
        return account
