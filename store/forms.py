from django import forms
from .models import Order


class CheckoutForm(forms.ModelForm):
    class Meta:
        model  = Order
        fields = [
            'customer_name',
            'customer_phone',
            'customer_email',
            'address_line',
            'city',
            'delivery_zone',
            'delivery_note',
        ]
        widgets = {
            'customer_name':  forms.TextInput(attrs={
                'class': 'form-input', 'placeholder': 'Your full name',
            }),
            'customer_phone': forms.TextInput(attrs={
                'class': 'form-input', 'placeholder': '01XXXXXXXXX',
            }),
            'customer_email': forms.EmailInput(attrs={
                'class': 'form-input', 'placeholder': 'email@example.com (optional)',
            }),
            'address_line':   forms.TextInput(attrs={
                'class': 'form-input',
                'placeholder': 'House no, Road, Area / Thana',
            }),
            'city':           forms.TextInput(attrs={
                'class': 'form-input', 'placeholder': 'e.g. Dhaka, Chittagong',
            }),
            'delivery_zone':  forms.Select(attrs={'class': 'form-input', 'id': 'id_delivery_zone'}),
            'delivery_note':  forms.Textarea(attrs={
                'class': 'form-input', 'rows': 2,
                'placeholder': 'Any special instruction for delivery (optional)',
            }),
        }
        labels = {
            'customer_name':  'Full Name',
            'customer_phone': 'Phone Number',
            'customer_email': 'Email Address',
            'address_line':   'Delivery Address',
            'city':           'City',
            'delivery_zone':  'Delivery Area',
            'delivery_note':  'Delivery Note',
        }

    def clean_customer_phone(self):
        phone = self.cleaned_data.get('customer_phone', '').strip()
        digits = phone.replace('+', '').replace('-', '').replace(' ', '')
        if not digits.isdigit() or len(digits) < 10:
            raise forms.ValidationError("Enter a valid Bangladeshi phone number.")
        return phone
