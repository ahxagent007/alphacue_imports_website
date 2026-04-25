"""
affiliate/registration_views.py
---------------------------------
User registration — clean form, black & gold design.
After registration, user is logged in and redirected to apply as affiliate.
"""

from django import forms
from django.contrib.auth import get_user_model, login, authenticate
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.shortcuts import render, redirect
from django.contrib import messages

User = get_user_model()


class UserRegisterForm(forms.Form):
    first_name = forms.CharField(
        max_length=50,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'First name',
            'autofocus': True,
        }),
        label='First Name',
    )
    last_name = forms.CharField(
        max_length=50,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'Last name',
        }),
        label='Last Name',
    )
    username = forms.CharField(
        max_length=150,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'Choose a username',
        }),
        label='Username',
    )
    email = forms.EmailField(
        required=False,
        widget=forms.EmailInput(attrs={
            'class': 'form-control',
            'placeholder': 'email@example.com (optional)',
        }),
        label='Email Address',
    )
    password1 = forms.CharField(
        widget=forms.PasswordInput(attrs={
            'class': 'form-control',
            'placeholder': 'Create a password',
        }),
        label='Password',
    )
    password2 = forms.CharField(
        widget=forms.PasswordInput(attrs={
            'class': 'form-control',
            'placeholder': 'Confirm your password',
        }),
        label='Confirm Password',
    )

    def clean_username(self):
        username = self.cleaned_data['username'].strip()
        if User.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError("This username is already taken. Please choose another.")
        return username

    def clean_email(self):
        email = self.cleaned_data.get('email', '').strip()
        if email and User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("An account with this email already exists.")
        return email

    def clean_password1(self):
        password = self.cleaned_data.get('password1', '')
        try:
            validate_password(password)
        except ValidationError as e:
            raise forms.ValidationError(list(e.messages))
        return password

    def clean(self):
        cleaned = super().clean()
        p1 = cleaned.get('password1', '')
        p2 = cleaned.get('password2', '')
        if p1 and p2 and p1 != p2:
            self.add_error('password2', "Passwords do not match.")
        return cleaned

    def save(self):
        data = self.cleaned_data
        user = User.objects.create_user(
            username   = data['username'],
            email      = data.get('email', ''),
            password   = data['password1'],
            first_name = data['first_name'],
            last_name  = data['last_name'],
        )
        return user


def register(request):
    """
    GET  /accounts/register/  — show registration form
    POST /accounts/register/  — create account, log in, redirect
    """
    # Already logged in
    if request.user.is_authenticated:
        return redirect('store:homepage')

    next_url = request.GET.get('next', '') or request.POST.get('next', '')

    if request.method == 'POST':
        form = UserRegisterForm(request.POST)
        if form.is_valid():
            user = form.save()
            # Auto-login after registration
            login(request, user)
            messages.success(
                request,
                f"Welcome, {user.first_name or user.username}! Your account has been created."
            )
            # Redirect to next URL or affiliate apply
            return redirect(next_url or 'affiliate:affiliate_apply')
    else:
        form = UserRegisterForm()

    return render(request, 'affiliate/register.html', {
        'form': form,
        'next': next_url,
    })