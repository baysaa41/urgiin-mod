from django import forms
from django.contrib.auth.forms import AuthenticationForm, SetPasswordForm

from .models import Invite, User


class EmailAuthenticationForm(AuthenticationForm):
    """Нэвтрэх нэр нь имэйл."""

    username = forms.EmailField(
        label="Имэйл",
        widget=forms.EmailInput(attrs={"autofocus": True, "autocomplete": "email",
                                       "class": "form-control"}),
    )
    password = forms.CharField(
        label="Нууц үг",
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password",
                                          "class": "form-control"}),
    )


class InviteAcceptForm(SetPasswordForm):
    """Урилга хүлээн авагч нууц үгээ тавих форм."""

    full_name = forms.CharField(
        label="Бүтэн нэр", max_length=150, required=False,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
