import re
from decimal import Decimal
from django import forms
from django.forms import inlineformset_factory, BaseInlineFormSet
from django.db import models
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.forms import PasswordResetForm, SetPasswordForm
from django.core.exceptions import ValidationError
from django.utils import timezone
from .models import (
    ClientProfile,
    Professional,
    Service,
    Partner,
    ContentPost,
    PartnerServicePrice,
    ClinicSettings,
    WeeklySchedule,
    WeeklyWorkingBlock,
    WeeklyBreakBlock,
    CashSession,
    CashMovement,
    Appointment,
    GroupMonthlyCharge,
    ClientPayment,
)
from .models import Product, ProductCategory
from .models import TreatmentRecord

class TreatmentRecordForm(forms.ModelForm):
    class Meta:
        model = TreatmentRecord
        fields = ["service_name", "date", "time", "notes"]

def _validate_nif_value(nif: str, *, exclude_profile_id=None) -> str:
    nif = (nif or "").strip()
    if not nif:
        raise ValidationError("O NIF é obrigatório.")
    if not nif.isdigit():
        raise ValidationError("O NIF deve conter apenas números.")
    if len(nif) != 9:
        raise ValidationError("O NIF deve ter exatamente 9 dígitos.")

    digits = [int(d) for d in nif]
    total = sum(d * (9 - i) for i, d in enumerate(digits[:8]))
    check = 11 - (total % 11)
    if check >= 10:
        check = 0
    if digits[8] != check:
        raise ValidationError("O NIF indicado não é válido.")

    profiles = ClientProfile.objects.filter(nif=nif)
    if exclude_profile_id:
        profiles = profiles.exclude(pk=exclude_profile_id)
    if profiles.exists():
        raise ValidationError("Já existe um utente com este NIF.")
    return nif


class ClientProfileForm(forms.ModelForm):
    class Meta:
        model = ClientProfile
        fields = [
            "profile_photo",
            "full_name",
            "phone",
            "gender",
            "nif",
            "address_line1",
            "address_line2",
            "postal_code",
            "city",
            "district",
            "county",
            "locality",
            "country",
            "birth_date",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Mesmas validações de registo para campos críticos
        for field in self.fields.values():
            if isinstance(field.widget, forms.Select):
                field.widget.attrs.setdefault("class", "form-select")
            elif isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.setdefault("class", "form-check-input")
            else:
                field.widget.attrs.setdefault("class", "form-control")
        for name in ("full_name", "phone", "nif"):
            if name in self.fields:
                self.fields[name].required = True
                self.fields[name].error_messages["required"] = "Campo de preenchimento obrigatório"
        self._require_complete_profile = bool(
            getattr(self.instance, "require_complete_profile", False)
        )
        if self._require_complete_profile:
            for name in ("address_line1", "postal_code", "district", "county", "locality"):
                if name in self.fields:
                    self.fields[name].required = True
                    self.fields[name].error_messages["required"] = "Campo de preenchimento obrigatório"

    def clean_full_name(self):
        name = (self.cleaned_data.get("full_name") or "").strip()
        if len(name) < 3:
            raise ValidationError("Indica um nome completo válido.")
        if re.search(r"\d", name):
            raise ValidationError("O nome não pode conter números.")
        parts = [p for p in re.split(r"\s+", name) if p]
        if len(parts) < 2:
            raise ValidationError("Indica pelo menos o primeiro e último nome.")
        return name

    def clean_phone(self):
        phone = (self.cleaned_data.get("phone") or "").strip()
        normalized = re.sub(r"[^\d]", "", phone)
        if len(normalized) < 9:
            raise ValidationError("Indica um número de telefone válido.")
        return normalized

    def clean_nif(self):
        nif = (self.cleaned_data.get("nif") or "").strip()
        return _validate_nif_value(nif, exclude_profile_id=self.instance.pk if self.instance else None)

    def clean_postal_code(self):
        postal_code = (self.cleaned_data.get("postal_code") or "").strip()
        if self._require_complete_profile:
            if not postal_code:
                raise ValidationError("Campo de preenchimento obrigatório")
            if not re.match(r"^\d{4}-\d{3}$", postal_code):
                raise ValidationError("Indica um código-postal válido.")
        return postal_code

class ProfessionalProfileForm(forms.Form):
    first_name = forms.CharField(max_length=150, required=False, label="Primeiro nome")
    last_name = forms.CharField(max_length=150, required=False, label="Último nome")
    email = forms.EmailField(required=False, label="Email")
    phone = forms.CharField(max_length=30, required=False, label="Telefone")
    speciality = forms.CharField(max_length=100, required=False, label="Especialidade")
    gender = forms.ChoiceField(
        choices=[("", "— escolher —"), ("masculino", "Masculino"), ("feminino", "Feminino")],
        required=False,
        label="Género",
    )
    profile_photo = forms.ImageField(required=False, label="Foto de perfil")

    def __init__(self, *args, user=None, professional=None, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            if isinstance(field.widget, forms.Select):
                field.widget.attrs.setdefault("class", "form-select")
            else:
                field.widget.attrs.setdefault("class", "form-control")
        if user:
            self.fields["first_name"].initial = user.first_name
            self.fields["last_name"].initial = user.last_name
            self.fields["email"].initial = user.email
        if professional:
            self.fields["speciality"].initial = professional.speciality
            self.fields["gender"].initial = professional.gender
            self.fields["phone"].initial = professional.phone
            if professional.profile_photo:
                self.fields["profile_photo"].initial = professional.profile_photo

    def save(self, *, user, professional):
        user.first_name = self.cleaned_data.get("first_name", "")
        user.last_name = self.cleaned_data.get("last_name", "")
        user.email = self.cleaned_data.get("email", "")
        user.save()

        professional.speciality = self.cleaned_data.get("speciality", "")
        professional.gender = self.cleaned_data.get("gender", "")
        professional.phone = self.cleaned_data.get("phone", "")
        profile_photo = self.cleaned_data.get("profile_photo")
        if profile_photo:
            professional.profile_photo = profile_photo
        professional.save()


User = get_user_model()


class BackofficeServiceForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if "price" in self.fields:
            self.fields["price"].required = False
        if "price_first" in self.fields:
            self.fields["price_first"].required = False
        if "price_followup" in self.fields:
            self.fields["price_followup"].required = False
        for field in self.fields.values():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.setdefault("class", "form-check-input")
            elif isinstance(field.widget, forms.Select):
                field.widget.attrs.setdefault("class", "form-select")
            else:
                field.widget.attrs.setdefault("class", "form-control")

    class Meta:
        model = Service
        fields = [
            "name",
            "duration_minutes",
            "slot_interval_minutes",
            "service_type",
            "capacity",
            "allow_waitlist",
            "pricing_mode",
            "price",
            "price_first",
            "price_followup",
        ]

    def clean_name(self):
        name = (self.cleaned_data.get("name") or "").strip()
        if len(name) < 3:
            raise ValidationError("O nome deve ter pelo menos 3 caracteres.")
        return name

    def clean_duration_minutes(self):
        value = self.cleaned_data.get("duration_minutes")
        if value is None or value < 10:
            raise ValidationError("A duração deve ser no mínimo 10 minutos.")
        return value

    def clean(self):
        cleaned = super().clean()
        service_type = cleaned.get("service_type")
        duration = cleaned.get("duration_minutes")
        slot_interval = cleaned.get("slot_interval_minutes")
        if service_type != "group":
            cleaned["allow_waitlist"] = False
        else:
            cleaned["slot_interval_minutes"] = None
        if slot_interval:
            if slot_interval < 5:
                self.add_error("slot_interval_minutes", "O intervalo deve ser no mínimo 5 minutos.")
            if duration and slot_interval > duration:
                self.add_error("slot_interval_minutes", "O intervalo não pode ser maior do que a duração.")
        pricing_mode = cleaned.get("pricing_mode")
        price = cleaned.get("price")
        price_first = cleaned.get("price_first")
        price_followup = cleaned.get("price_followup")
        if pricing_mode == "first_followup":
            cleaned["price"] = Decimal("0.00")
            if price_first is None:
                self.add_error("price_first", "Indica o preço da 1ª consulta.")
            if price_followup is None:
                self.add_error("price_followup", "Indica o preço das seguintes.")
        else:
            if price is None:
                self.add_error("price", "Indica o preço base.")
            elif price < 0:
                self.add_error("price", "O preço deve ser positivo.")
        return cleaned


class BackofficeProfessionalForm(forms.ModelForm):
    user_email = forms.EmailField(required=False, label="Email")
    new_user_first_name = forms.CharField(max_length=150, required=False, label="Primeiro nome")
    new_user_last_name = forms.CharField(max_length=150, required=False, label="Último nome")
    new_user_email = forms.EmailField(required=False, label="Email")
    new_user_username = forms.CharField(max_length=150, required=False, label="Username")
    new_user_password1 = forms.CharField(
        required=False,
        label="Password",
        widget=forms.PasswordInput,
    )
    new_user_password2 = forms.CharField(
        required=False,
        label="Confirmar password",
        widget=forms.PasswordInput,
    )

    class Meta:
        model = Professional
        fields = [
            "user",
            "profile_photo",
            "speciality",
            "gender",
            "phone",
            "services",
            "is_independent",
            "subcontract_percentage",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        user_qs = User.objects.all().order_by("username")
        if self.instance and self.instance.pk:
            user_qs = user_qs.filter(models.Q(professional__isnull=True) | models.Q(id=self.instance.user_id))
        else:
            user_qs = user_qs.filter(professional__isnull=True)
        self.fields["user"].queryset = user_qs
        self.fields["user"].required = bool(self.instance and self.instance.pk)
        if "services" in self.fields:
            self.fields["services"].queryset = Service.objects.order_by("name")
        if "profile_photo" in self.fields:
            self.fields["profile_photo"].widget.attrs.setdefault("accept", "image/*")
        if self.instance and self.instance.pk and getattr(self.instance, "user_id", None):
            self.fields["user_email"].initial = self.instance.user.email
        for field in self.fields.values():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.setdefault("class", "form-check-input")
            elif isinstance(field.widget, forms.SelectMultiple):
                field.widget.attrs.setdefault("class", "form-select")
                field.widget.attrs.setdefault("multiple", "multiple")
                field.widget.attrs.setdefault("size", "6")
            elif isinstance(field.widget, forms.Select):
                field.widget.attrs.setdefault("class", "form-select")
            else:
                field.widget.attrs.setdefault("class", "form-control")

    def clean_user(self):
        user = self.cleaned_data.get("user")
        if user and (not self.instance or user.id != self.instance.user_id):
            if Professional.objects.filter(user=user).exists():
                raise ValidationError("Este utilizador já está associado a um profissional.")
        return user

    def clean_user_email(self):
        email = (self.cleaned_data.get("user_email") or "").strip().lower()
        if not email:
            return ""
        qs = User.objects.filter(email__iexact=email)
        current_user_id = getattr(self.instance, "user_id", None)
        if current_user_id:
            qs = qs.exclude(pk=current_user_id)
        if qs.exists():
            raise ValidationError("Este email já está registado.")
        return email

    def clean(self):
        cleaned = super().clean()
        user = cleaned.get("user")
        user_email = (cleaned.get("user_email") or "").strip()
        new_username = (cleaned.get("new_user_username") or "").strip()
        new_password1 = cleaned.get("new_user_password1") or ""
        new_password2 = cleaned.get("new_user_password2") or ""
        if not user:
            if not new_username:
                self.add_error("new_user_username", "Indica um username.")
            elif User.objects.filter(username__iexact=new_username).exists():
                self.add_error("new_user_username", "Já existe um utilizador com este username.")
            if not new_password1:
                self.add_error("new_user_password1", "Indica uma password.")
            if not new_password2:
                self.add_error("new_user_password2", "Confirma a password.")
            if new_password1 and new_password2 and new_password1 != new_password2:
                self.add_error("new_user_password2", "As passwords não coincidem.")
        elif not self.instance.pk and not user_email:
            self.add_error("user_email", "Indica um email.")
        is_independent = cleaned.get("is_independent")
        subcontract_percentage = cleaned.get("subcontract_percentage")
        if is_independent:
            if subcontract_percentage is None:
                self.add_error("subcontract_percentage", "Indica a percentagem de comissionamento.")
            elif subcontract_percentage < 0 or subcontract_percentage > 100:
                self.add_error("subcontract_percentage", "Percentagem inválida (0-100).")
        else:
            cleaned["subcontract_percentage"] = None
        return cleaned

    def _create_new_user(self):
        username = (self.cleaned_data.get("new_user_username") or "").strip()
        email = (self.cleaned_data.get("new_user_email") or "").strip()
        password = self.cleaned_data.get("new_user_password1") or ""
        user = User.objects.create_user(username=username, email=email or "", password=password)
        user.first_name = (self.cleaned_data.get("new_user_first_name") or "").strip()
        user.last_name = (self.cleaned_data.get("new_user_last_name") or "").strip()
        user.save()
        return user

    def save(self, commit=True):
        user = self.cleaned_data.get("user")
        if not user:
            user = self._create_new_user()
        else:
            user_email = (self.cleaned_data.get("user_email") or "").strip().lower()
            if user.email != user_email:
                user.email = user_email
                if commit:
                    user.save(update_fields=["email"])
        instance = super().save(commit=False)
        instance.user = user
        if commit:
            instance.save()
            self.save_m2m()
        return instance


class WeeklyScheduleForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.setdefault("class", "form-check-input")
            elif isinstance(field.widget, forms.Select):
                field.widget.attrs.setdefault("class", "form-select")
            else:
                field.widget.attrs.setdefault("class", "form-control")

    class Meta:
        model = WeeklySchedule
        fields = ["is_active"]


SCHEDULE_START_MINUTES = 9 * 60
SCHEDULE_END_MINUTES = 21 * 60
SCHEDULE_STEP_MINUTES = 15


def _time_choices(
    step_minutes=SCHEDULE_STEP_MINUTES,
    start_minutes=SCHEDULE_START_MINUTES,
    end_minutes=SCHEDULE_END_MINUTES,
):
    choices = []
    for total_minutes in range(start_minutes, end_minutes + 1, step_minutes):
        hour = total_minutes // 60
        minute = total_minutes % 60
        label = f"{hour:02d}:{minute:02d}"
        value = f"{label}:00"
        choices.append((value, label))
    return choices


class WeeklyWorkingBlockForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        start_time_choices = _time_choices(end_minutes=SCHEDULE_END_MINUTES - SCHEDULE_STEP_MINUTES)
        end_time_choices = _time_choices(start_minutes=SCHEDULE_START_MINUTES + SCHEDULE_STEP_MINUTES)
        for name, field in self.fields.items():
            if name == "weekday":
                field.widget = forms.HiddenInput()
                continue
            if name == "start_time":
                field.widget = forms.Select(choices=start_time_choices)
            if name == "end_time":
                field.widget = forms.Select(choices=end_time_choices)
            field.widget.attrs.setdefault("class", "form-control")

    class Meta:
        model = WeeklyWorkingBlock
        fields = ["weekday", "start_time", "end_time"]


class WeeklyBreakBlockForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        start_time_choices = _time_choices(end_minutes=SCHEDULE_END_MINUTES - SCHEDULE_STEP_MINUTES)
        end_time_choices = _time_choices(start_minutes=SCHEDULE_START_MINUTES + SCHEDULE_STEP_MINUTES)
        for name, field in self.fields.items():
            if name == "weekday":
                field.widget = forms.HiddenInput()
                continue
            if name == "start_time":
                field.widget = forms.Select(choices=start_time_choices)
            if name == "end_time":
                field.widget = forms.Select(choices=end_time_choices)
            field.widget.attrs.setdefault("class", "form-control")

    class Meta:
        model = WeeklyBreakBlock
        fields = ["weekday", "start_time", "end_time"]


class BaseWeeklyBlockFormSet(BaseInlineFormSet):
    overlap_message = "Existem blocos sobrepostos no mesmo dia."

    def clean(self):
        super().clean()
        blocks_by_day = {}
        for form in self.forms:
            if not hasattr(form, "cleaned_data"):
                continue
            if form.cleaned_data.get("DELETE"):
                continue
            weekday = form.cleaned_data.get("weekday")
            start = form.cleaned_data.get("start_time")
            end = form.cleaned_data.get("end_time")
            if weekday is None or not start or not end:
                continue
            day_blocks = blocks_by_day.setdefault(weekday, [])
            for existing_start, existing_end in day_blocks:
                if start < existing_end and end > existing_start:
                    raise ValidationError(self.overlap_message)
            day_blocks.append((start, end))


class BaseWeeklyBreakBlockFormSet(BaseWeeklyBlockFormSet):
    overlap_message = "Existem pausas sobrepostas no mesmo dia."


WeeklyWorkingBlockFormSet = inlineformset_factory(
    WeeklySchedule,
    WeeklyWorkingBlock,
    form=WeeklyWorkingBlockForm,
    formset=BaseWeeklyBlockFormSet,
    extra=0,
    can_delete=True,
)

WeeklyBreakBlockFormSet = inlineformset_factory(
    WeeklySchedule,
    WeeklyBreakBlock,
    form=WeeklyBreakBlockForm,
    formset=BaseWeeklyBreakBlockFormSet,
    extra=0,
    can_delete=True,
)


class BackofficePartnerForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.setdefault("class", "form-check-input")
            elif isinstance(field.widget, forms.Select):
                field.widget.attrs.setdefault("class", "form-select")
            else:
                field.widget.attrs.setdefault("class", "form-control")

    class Meta:
        model = Partner
        fields = [
            "name",
            "logo",
            "active",
            "notes",
        ]

    def clean_name(self):
        name = (self.cleaned_data.get("name") or "").strip()
        if len(name) < 2:
            raise ValidationError("O nome deve ter pelo menos 2 caracteres.")
        return name


class BackofficeHighlightForm(forms.ModelForm):
    class Meta:
        model = ContentPost
        fields = [
            "title",
            "cover_image",
            "excerpt",
            "body",
            "status",
            "is_featured",
        ]
        widgets = {
            "excerpt": forms.Textarea(attrs={"rows": 3}),
            "body": forms.Textarea(attrs={"rows": 6}),
        }
        labels = {
            "title": "Título",
            "cover_image": "Imagem",
            "excerpt": "Descrição",
            "body": "Descrição completa",
            "status": "Estado",
            "is_featured": "Destaque principal",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["title"].widget.attrs.setdefault("placeholder", "Título do destaque")
        self.fields["excerpt"].widget.attrs.setdefault("placeholder", "Resumo curto para o cartão")
        self.fields["body"].widget.attrs.setdefault("placeholder", "Texto completo do destaque")

        for field in self.fields.values():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.setdefault("class", "form-check-input")
            elif isinstance(field.widget, forms.Select):
                field.widget.attrs.setdefault("class", "form-select")
            else:
                field.widget.attrs.setdefault("class", "form-control")

    def clean_title(self):
        title = (self.cleaned_data.get("title") or "").strip()
        if len(title) < 5:
            raise ValidationError("O título deve ter pelo menos 5 caracteres.")
        return title

    def clean(self):
        cleaned = super().clean()
        excerpt = (cleaned.get("excerpt") or "").strip()
        body = (cleaned.get("body") or "").strip()

        if not excerpt and not body:
            raise ValidationError("Indica pelo menos uma descrição.")
        if not body and excerpt:
            cleaned["body"] = excerpt
        if not excerpt and body:
            cleaned["excerpt"] = body[:180]
        return cleaned


class BackofficeClientProfileForm(forms.ModelForm):
    email = forms.EmailField(required=False, label="Email")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if name == "email":
                field.widget.attrs.setdefault("class", "form-control")
                continue
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.setdefault("class", "form-check-input")
            elif isinstance(field.widget, forms.Select):
                field.widget.attrs.setdefault("class", "form-select")
            else:
                field.widget.attrs.setdefault("class", "form-control")

    class Meta:
        model = ClientProfile
        fields = [
            "full_name",
            "nif",
            "phone",
            "gender",
            "address_line1",
            "address_line2",
            "postal_code",
            "city",
            "district",
            "county",
            "locality",
            "country",
            "partner",
            "discount_type",
            "discount_percent",
            "discount_amount",
            "discount_label",
        ]

    def clean_full_name(self):
        name = (self.cleaned_data.get("full_name") or "").strip()
        if len(name) < 3:
            raise ValidationError("Indica um nome completo válido.")
        if re.search(r"\d", name):
            raise ValidationError("O nome não pode conter números.")
        parts = [p for p in re.split(r"\s+", name) if p]
        if len(parts) < 2:
            raise ValidationError("Indica pelo menos o primeiro e último nome.")
        return name

    def clean_phone(self):
        phone = (self.cleaned_data.get("phone") or "").strip()
        normalized = re.sub(r"[^\d]", "", phone)
        if normalized and len(normalized) < 9:
            raise ValidationError("Indica um número de telefone válido.")
        return normalized

    def clean_nif(self):
        nif = (self.cleaned_data.get("nif") or "").strip()
        return _validate_nif_value(nif, exclude_profile_id=self.instance.pk if self.instance else None)


class EmailPasswordResetForm(PasswordResetForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["email"].widget.attrs["class"] = "field-input"

    def clean_email(self):
        email = (self.cleaned_data.get("email") or "").strip().lower()
        if not email:
            raise ValidationError("Campo de preenchimento obrigatório")
        return email

    def send_mail(self, subject_template_name, email_template_name, context, from_email, to_email, html_email_template_name=None):
        from django.urls import reverse
        from core.emails import send_templated_email, clinic_settings, log_email_skip

        reset_url = f"{context['protocol']}://{context['domain']}{reverse('password_reset_confirm', args=[context['uid'], context['token']])}"
        settings_obj = clinic_settings()
        clinic_name = settings_obj.clinic_name
        subject = f"Recuperação de password — {clinic_name}"
        if not settings_obj.notify_password_reset:
            log_email_skip("password_reset", subject, "Envio desativado nas definições.", to_email)
            return
        send_templated_email(
            to_email,
            subject,
            "emails/password_reset.html",
            "emails/password_reset.txt",
            {"user": context.get("user"), "reset_url": reset_url},
            event="password_reset",
        )


class ResetPasswordConfirmForm(SetPasswordForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["new_password1"].widget.attrs["class"] = "field-input"
        self.fields["new_password2"].widget.attrs["class"] = "field-input"

class RegisterForm(forms.Form):
    full_name = forms.CharField(
        max_length=200,
        label="Nome completo",
        error_messages={"required": "Campo de preenchimento obrigatório"},
    )
    nif = forms.CharField(
        max_length=20,
        label="NIF",
        error_messages={"required": "Campo de preenchimento obrigatório"},
    )
    email = forms.EmailField(
        label="Email",
        error_messages={"required": "Campo de preenchimento obrigatório"},
    )
    phone = forms.CharField(
        max_length=30,
        label="Telefone",
        error_messages={"required": "Campo de preenchimento obrigatório"},
    )
    address_line1 = forms.CharField(
        max_length=255,
        label="Morada",
        error_messages={"required": "Campo de preenchimento obrigatório"},
    )
    district = forms.CharField(
        max_length=120,
        label="Distrito",
        error_messages={"required": "Campo de preenchimento obrigatório"},
    )
    county = forms.CharField(
        max_length=120,
        label="Concelho",
        error_messages={"required": "Campo de preenchimento obrigatório"},
    )
    locality = forms.CharField(
        max_length=120,
        label="Localidade",
        error_messages={"required": "Campo de preenchimento obrigatório"},
    )
    postal_code_1 = forms.CharField(
        max_length=4,
        label="Código postal",
        error_messages={"required": "Campo de preenchimento obrigatório"},
    )
    postal_code_2 = forms.CharField(
        max_length=3,
        label="Código postal",
        error_messages={"required": "Campo de preenchimento obrigatório"},
    )
    password1 = forms.CharField(
        widget=forms.PasswordInput,
        label="Password",
        error_messages={"required": "Campo de preenchimento obrigatório"},
    )
    password2 = forms.CharField(
        widget=forms.PasswordInput,
        label="Confirmar password",
        error_messages={"required": "Campo de preenchimento obrigatório"},
    )
    accepted_terms = forms.BooleanField(
        required=True,
        label="Li e aceito os Termos, a Política de Privacidade e a Política de Cookies.",
        error_messages={
            "required": "Tens de aceitar os Termos, a Política de Privacidade e a Política de Cookies para criares conta."
        },
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in (
            "full_name",
            "nif",
            "email",
            "phone",
            "address_line1",
            "district",
            "county",
            "locality",
            "postal_code_1",
            "postal_code_2",
            "password1",
            "password2",
        ):
            self.fields[name].widget.attrs["class"] = "field-input"
        for name in ("accepted_terms",):
            self.fields[name].widget.attrs["class"] = "field-check"

        self.fields["postal_code_1"].widget.attrs["inputmode"] = "numeric"
        self.fields["postal_code_2"].widget.attrs["inputmode"] = "numeric"

    def clean(self):
        cleaned = super().clean()
        password1 = cleaned.get("password1") or ""
        password2 = cleaned.get("password2") or ""
        if password1 or password2:
            if password1 != password2:
                raise ValidationError("As passwords não coincidem.")
            validate_password(password1)

        cp1 = (cleaned.get("postal_code_1") or "").strip()
        cp2 = (cleaned.get("postal_code_2") or "").strip()
        if cp1 or cp2:
            if not (cp1.isdigit() and cp2.isdigit()):
                self.add_error("postal_code_1", "Indica um código-postal válido.")
            if len(cp1) != 4 or len(cp2) != 3:
                self.add_error("postal_code_1", "Indica um código-postal válido.")
            if not self.errors.get("postal_code_1"):
                cleaned["postal_code"] = f"{cp1}-{cp2}"
        return cleaned

    def clean_full_name(self):
        name = (self.cleaned_data.get("full_name") or "").strip()
        if len(name) < 3:
            raise ValidationError("Indica um nome completo válido.")
        if re.search(r"\d", name):
            raise ValidationError("O nome não pode conter números.")
        parts = [p for p in re.split(r"\s+", name) if p]
        if len(parts) < 2:
            raise ValidationError("Indica pelo menos o primeiro e último nome.")
        return name

    def clean_nif(self):
        return _validate_nif_value(self.cleaned_data.get("nif"))

    def clean_phone(self):
        phone = (self.cleaned_data.get("phone") or "").strip()
        normalized = re.sub(r"[^\d]", "", phone)
        if len(normalized) < 9:
            raise ValidationError("Indica um número de telefone válido.")
        return normalized

    def clean_email(self):
        email = (self.cleaned_data.get("email") or "").strip().lower()
        if not email:
            raise ValidationError("O email é obrigatório.")
        if " " in email:
            raise ValidationError("Indica um email válido.")
        if email in {"nao@tem.com"}:
            raise ValidationError("Indica um email válido.")
        if "@" not in email:
            raise ValidationError("Indica um email válido.")
        local, domain = email.split("@", 1)
        if "." not in domain:
            raise ValidationError("Indica um email válido.")
        blocked_locals = {"test", "fake", "exemplo", "nao", "sememail", "email", "user"}
        blocked_domains = {"example.com", "exemplo.com", "tem.com"}
        if local in blocked_locals or domain in blocked_domains:
            raise ValidationError("Indica um email válido.")
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("Este email já está registado.")
        return email




class StaffClientCreateForm(forms.Form):
    profile_photo = forms.ImageField(required=False, label="Foto")
    full_name = forms.CharField(
        max_length=200,
        label="Nome completo",
        error_messages={"required": "Campo de preenchimento obrigatório"},
    )
    nif = forms.CharField(
        max_length=20,
        label="NIF",
        required=False,
        error_messages={"required": "Campo de preenchimento obrigatório"},
    )
    username = forms.CharField(max_length=150, required=False, label="Username")
    email = forms.EmailField(label="Email", required=False)
    password = forms.CharField(
        widget=forms.PasswordInput,
        label="Password",
        required=False,
    )
    password_confirm = forms.CharField(
        widget=forms.PasswordInput,
        label="Confirmar password",
        required=False,
    )
    phone = forms.CharField(
        max_length=30,
        required=True,
        label="Telefone",
        error_messages={"required": "Campo de preenchimento obrigatório"},
    )
    address_line1 = forms.CharField(
        max_length=255,
        required=False,
        label="Morada",
        error_messages={"required": "Campo de preenchimento obrigatório"},
    )
    address_line2 = forms.CharField(max_length=255, required=False, label="Morada (linha 2)")
    postal_code = forms.CharField(
        max_length=20,
        required=False,
        label="Código postal",
        error_messages={"required": "Campo de preenchimento obrigatório"},
    )
    city = forms.CharField(
        max_length=120,
        required=False,
        label="Cidade",
        error_messages={"required": "Campo de preenchimento obrigatório"},
    )
    district = forms.CharField(max_length=120, required=False, label="Distrito")
    county = forms.CharField(max_length=120, required=False, label="Concelho")
    locality = forms.CharField(max_length=120, required=False, label="Localidade")
    postal_designation = forms.CharField(max_length=120, required=False, label="Designação postal")
    country = forms.CharField(max_length=120, required=False, label="País")
    partner = forms.ModelChoiceField(
        queryset=Partner.objects.none(),
        required=False,
        label="Parceria",
    )
    discount_type = forms.ChoiceField(
        choices=ClientProfile.DISCOUNT_CHOICES,
        required=False,
        label="Tipo de desconto",
        initial="none",
    )
    discount_percent = forms.DecimalField(
        max_digits=5,
        decimal_places=2,
        required=False,
        label="Desconto (%)",
    )
    discount_amount = forms.DecimalField(
        max_digits=10,
        decimal_places=2,
        required=False,
        label="Desconto (valor)",
    )
    discount_label = forms.CharField(max_length=120, required=False, label="Descrição do desconto")
    clinical_allergies = forms.CharField(required=False, label="Alergias")
    clinical_conditions = forms.CharField(required=False, label="Condições clínicas")
    clinical_notes = forms.CharField(required=False, label="Notas clínicas")

    def __init__(self, *args, **kwargs):
        self.existing_user = kwargs.pop("existing_user", None)
        self.existing_profile = kwargs.pop("existing_profile", None)
        super().__init__(*args, **kwargs)
        self.fields["partner"].queryset = Partner.objects.all().order_by("name")
        if self.existing_profile:
            for name in ("full_name", "nif", "phone", "address_line1", "postal_code", "city"):
                if name in self.fields:
                    self.fields[name].required = False
                    self.fields[name].error_messages.pop("required", None)

    def clean_full_name(self):
        name = (self.cleaned_data.get("full_name") or "").strip()
        if self.existing_profile and not name:
            return ""
        if len(name) < 3:
            raise ValidationError("Indica um nome completo válido.")
        if re.search(r"\d", name):
            raise ValidationError("O nome não pode conter números.")
        parts = [p for p in re.split(r"\s+", name) if p]
        if len(parts) < 2:
            raise ValidationError("Indica pelo menos o primeiro e último nome.")
        return name

    def clean_username(self):
        username = (self.cleaned_data.get("username") or "").strip()
        if not username:
            return ""
        if self.existing_user and username.lower() == (self.existing_user.username or "").lower():
            return username
        if User.objects.filter(username__iexact=username).exists():
            raise ValidationError("Este username já está registado.")
        return username

    def clean_nif(self):
        nif = (self.cleaned_data.get("nif") or "").strip()
        if not nif:
            return ""
        exclude_id = self.existing_profile.pk if self.existing_profile else None
        return _validate_nif_value(nif, exclude_profile_id=exclude_id)

    def clean_phone(self):
        phone = (self.cleaned_data.get("phone") or "").strip()
        if self.existing_profile and not phone:
            return ""
        normalized = re.sub(r"[^\d]", "", phone)
        if normalized and len(normalized) < 9:
            raise ValidationError("Indica um número de telefone válido.")
        return normalized

    def clean_postal_code(self):
        postal_code = (self.cleaned_data.get("postal_code") or "").strip()
        if not postal_code:
            return ""
        if not re.match(r"^\d{4}-\d{3}$", postal_code):
            raise ValidationError("Indica um código-postal válido (ex: 1234-567).")
        return postal_code

    def clean_email(self):
        email = (self.cleaned_data.get("email") or "").strip().lower()
        if not email:
            return ""
        if " " in email:
            raise ValidationError("Indica um email válido.")
        if email in {"nao@tem.com"}:
            raise ValidationError("Indica um email válido.")
        if "@" not in email:
            raise ValidationError("Indica um email válido.")
        local, domain = email.split("@", 1)
        if "." not in domain:
            raise ValidationError("Indica um email válido.")
        blocked_locals = {"test", "fake", "exemplo", "nao", "sememail", "email", "user"}
        blocked_domains = {"example.com", "exemplo.com", "tem.com"}
        if local in blocked_locals or domain in blocked_domains:
            raise ValidationError("Indica um email válido.")
        exists = User.objects.filter(email__iexact=email)
        if self.existing_user:
            exists = exists.exclude(pk=self.existing_user.pk)
        if exists.exists():
            raise ValidationError("Este email já está registado.")
        return email

    def clean(self):
        cleaned = super().clean()
        password = cleaned.get("password") or ""
        password_confirm = cleaned.get("password_confirm") or ""
        if password or password_confirm:
            if password != password_confirm:
                self.add_error("password_confirm", "As passwords não coincidem.")
            if password:
                validate_password(password)
        discount_type = cleaned.get("discount_type") or "none"
        discount_percent = cleaned.get("discount_percent")
        discount_amount = cleaned.get("discount_amount")
        if discount_type == "percent":
            if discount_percent is None:
                self.add_error("discount_percent", "Indica a percentagem de desconto.")
            elif discount_percent < 0 or discount_percent > 100:
                self.add_error("discount_percent", "Percentagem inválida (0-100).")
            cleaned["discount_amount"] = None
        elif discount_type == "fixed":
            if discount_amount is None:
                self.add_error("discount_amount", "Indica o valor fixo de desconto.")
            elif discount_amount < 0:
                self.add_error("discount_amount", "O valor deve ser positivo.")
            cleaned["discount_percent"] = None
        else:
            cleaned["discount_percent"] = None
            cleaned["discount_amount"] = None
        return cleaned


class ProductForm(forms.ModelForm):
    class Meta:
        model = Product
        fields = [
            "name",
            "sku",
            "category",
            "is_active",
            "unit_base",
            "min_stock_alert",
            "unit_per_pack",
            "notes",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if isinstance(field.widget, forms.Select):
                field.widget.attrs.setdefault("class", "form-select")
            elif isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.setdefault("class", "form-check-input")
            else:
                field.widget.attrs.setdefault("class", "form-control")


class ProductCategoryForm(forms.ModelForm):
    class Meta:
        model = ProductCategory
        fields = ["name"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if isinstance(field.widget, forms.Select):
                field.widget.attrs.setdefault("class", "form-select")
            elif isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.setdefault("class", "form-check-input")
            else:
                field.widget.attrs.setdefault("class", "form-control")


class ClinicEmailSettingsForm(forms.ModelForm):
    class Meta:
        model = ClinicSettings
        fields = [
            "notify_admin_on_pending_registration",
            "notify_clinic_on_new_booking",
            "notify_clinic_on_client_reschedule",
            "notify_clinic_on_client_cancel",
            "notify_professional_on_new_booking",
            "notify_client_on_new_booking",
            "notify_client_on_clinic_changes",
            "notify_password_reset",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.setdefault("class", "form-check-input")
            else:
                field.widget.attrs.setdefault("class", "form-control")


class StockEntryForm(forms.Form):
    UNIT_BASE = "base"
    UNIT_PACK = "pack"

    UNIT_MODE_CHOICES = (
        (UNIT_BASE, "Unidade base"),
        (UNIT_PACK, "Embalagens"),
    )

    product = forms.ModelChoiceField(queryset=Product.objects.filter(is_active=True), label="Produto")
    quantity = forms.DecimalField(max_digits=12, decimal_places=2, label="Quantidade")
    unit_mode = forms.ChoiceField(choices=UNIT_MODE_CHOICES, label="Modo de quantidade")
    unit_cost = forms.DecimalField(max_digits=12, decimal_places=2, required=False, label="Custo unitário")
    note = forms.CharField(widget=forms.Textarea, required=False, label="Notas")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if isinstance(field.widget, forms.Select):
                field.widget.attrs.setdefault("class", "form-select")
            elif isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.setdefault("class", "form-check-input")
            elif isinstance(field.widget, forms.Textarea):
                field.widget.attrs.setdefault("class", "form-control")
                field.widget.attrs.setdefault("rows", 2)
            else:
                field.widget.attrs.setdefault("class", "form-control")


class StockAdjustmentForm(forms.Form):
    DIRECTION_INCREASE = "increase"
    DIRECTION_DECREASE = "decrease"

    DIRECTION_CHOICES = (
        (DIRECTION_INCREASE, "Entrada"),
        (DIRECTION_DECREASE, "Saída"),
    )

    product = forms.ModelChoiceField(queryset=Product.objects.filter(is_active=True), label="Produto")
    quantity = forms.DecimalField(max_digits=12, decimal_places=2, label="Quantidade")
    direction = forms.ChoiceField(choices=DIRECTION_CHOICES, label="Tipo de ajuste")
    note = forms.CharField(widget=forms.Textarea, required=False, label="Notas")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if isinstance(field.widget, forms.Select):
                field.widget.attrs.setdefault("class", "form-select")
            elif isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.setdefault("class", "form-check-input")
            elif isinstance(field.widget, forms.Textarea):
                field.widget.attrs.setdefault("class", "form-control")
                field.widget.attrs.setdefault("rows", 2)
            else:
                field.widget.attrs.setdefault("class", "form-control")


class CashSessionOpenForm(forms.Form):
    session_date = forms.DateField(
        label="Data",
        input_formats=["%Y-%m-%d"],
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    opening_amount = forms.DecimalField(max_digits=10, decimal_places=2, label="Fundo inicial")
    opening_notes = forms.CharField(widget=forms.Textarea, required=False, label="Observações")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["session_date"].initial = self.initial.get("session_date")
        for field in self.fields.values():
            if isinstance(field.widget, forms.Textarea):
                field.widget.attrs.setdefault("class", "form-control")
                field.widget.attrs.setdefault("rows", 2)
            else:
                field.widget.attrs.setdefault("class", "form-control")


class CashSessionCloseForm(forms.Form):
    counted_cash_amount = forms.DecimalField(max_digits=10, decimal_places=2, label="Numerário contado")
    closing_notes = forms.CharField(widget=forms.Textarea, required=False, label="Observações de fecho")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            if isinstance(field.widget, forms.Textarea):
                field.widget.attrs.setdefault("class", "form-control")
                field.widget.attrs.setdefault("rows", 2)
            else:
                field.widget.attrs.setdefault("class", "form-control")


class CashManualMovementForm(forms.Form):
    movement_type = forms.ChoiceField(choices=CashMovement.TYPE_CHOICES, label="Tipo")
    payment_method = forms.ChoiceField(choices=CashMovement.PAYMENT_METHOD_CHOICES, label="Método")
    amount = forms.DecimalField(max_digits=10, decimal_places=2, label="Valor")
    description = forms.CharField(max_length=255, label="Descrição")
    happened_at = forms.DateTimeField(
        label="Quando",
        input_formats=["%Y-%m-%dT%H:%M"],
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
    )
    notes = forms.CharField(widget=forms.Textarea, required=False, label="Notas")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.initial.get("happened_at"):
            self.fields["happened_at"].initial = timezone.localtime().strftime("%Y-%m-%dT%H:%M")
        for field in self.fields.values():
            if isinstance(field.widget, forms.Select):
                field.widget.attrs.setdefault("class", "form-select")
            elif isinstance(field.widget, forms.Textarea):
                field.widget.attrs.setdefault("class", "form-control")
                field.widget.attrs.setdefault("rows", 2)
            else:
                field.widget.attrs.setdefault("class", "form-control")


class CashAppointmentMovementForm(forms.Form):
    appointment = forms.ModelChoiceField(queryset=Appointment.objects.none(), label="Marcação paga")
    payment_method = forms.ChoiceField(choices=CashMovement.PAYMENT_METHOD_CHOICES, label="Método")
    notes = forms.CharField(widget=forms.Textarea, required=False, label="Notas")

    def __init__(self, *args, appointment_queryset=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["appointment"].queryset = appointment_queryset or Appointment.objects.none()
        self.fields["appointment"].label_from_instance = self._appointment_label
        for field in self.fields.values():
            if isinstance(field.widget, forms.Select):
                field.widget.attrs.setdefault("class", "form-select")
            elif isinstance(field.widget, forms.Textarea):
                field.widget.attrs.setdefault("class", "form-control")
                field.widget.attrs.setdefault("rows", 2)
            else:
                field.widget.attrs.setdefault("class", "form-control")

    @staticmethod
    def _appointment_label(appointment):
        client_name = (
            getattr(getattr(appointment.client, "client_profile", None), "full_name", "")
            or appointment.client.get_full_name()
            or appointment.client.username
        )
        service_name = getattr(appointment.service, "name", "") or "Serviço"
        amount = getattr(appointment, "final_price", None)
        amount_display = f"{amount:.2f} €" if amount is not None else "—"
        return f"{appointment.date:%d/%m/%Y} {appointment.time:%H:%M} · {client_name} · {service_name} · {amount_display}"


class CashClientPaymentMovementForm(forms.Form):
    client_payment = forms.ModelChoiceField(queryset=ClientPayment.objects.none(), label="Pagamento de cliente")
    notes = forms.CharField(widget=forms.Textarea, required=False, label="Notas adicionais")

    def __init__(self, *args, payment_queryset=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["client_payment"].queryset = payment_queryset or ClientPayment.objects.none()
        self.fields["client_payment"].label_from_instance = self._payment_label
        for field in self.fields.values():
            if isinstance(field.widget, forms.Select):
                field.widget.attrs.setdefault("class", "form-select")
            elif isinstance(field.widget, forms.Textarea):
                field.widget.attrs.setdefault("class", "form-control")
                field.widget.attrs.setdefault("rows", 2)
            else:
                field.widget.attrs.setdefault("class", "form-control")

    @staticmethod
    def _payment_label(payment):
        client_name = payment.client_profile.full_name if payment.client_profile else "Cliente"
        amount_display = f"{payment.amount_received:.2f} €"
        method_label = dict(ClientPayment.PAYMENT_METHOD_CHOICES).get(payment.payment_method, payment.payment_method)
        return f"{payment.received_at:%d/%m/%Y %H:%M} · {client_name} · {amount_display} · {method_label}"


class CashGroupMonthlyMovementForm(forms.Form):
    group_monthly_charge = forms.ModelChoiceField(queryset=GroupMonthlyCharge.objects.none(), label="Mensalidade paga")
    payment_method = forms.ChoiceField(choices=CashMovement.PAYMENT_METHOD_CHOICES, label="Método")
    notes = forms.CharField(widget=forms.Textarea, required=False, label="Notas")

    def __init__(self, *args, monthly_charge_queryset=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["group_monthly_charge"].queryset = monthly_charge_queryset or GroupMonthlyCharge.objects.none()
        self.fields["group_monthly_charge"].label_from_instance = self._monthly_label
        for field in self.fields.values():
            if isinstance(field.widget, forms.Select):
                field.widget.attrs.setdefault("class", "form-select")
            elif isinstance(field.widget, forms.Textarea):
                field.widget.attrs.setdefault("class", "form-control")
                field.widget.attrs.setdefault("rows", 2)
            else:
                field.widget.attrs.setdefault("class", "form-control")

    @staticmethod
    def _monthly_label(charge):
        client_name = (
            getattr(getattr(charge.client, "client_profile", None), "full_name", "")
            or charge.client.get_full_name()
            or charge.client.username
        )
        class_name = charge.class_name or getattr(charge.service, "name", "") or "Turma"
        return f"{charge.month:%m/%Y} · {client_name} · {class_name} · {charge.final_price:.2f} €"


class CashStockSaleForm(forms.Form):
    product = forms.ModelChoiceField(queryset=Product.objects.none(), label="Produto")
    client_profile = forms.ModelChoiceField(queryset=ClientProfile.objects.none(), required=False, label="Utente")
    quantity_base = forms.DecimalField(max_digits=12, decimal_places=2, label="Quantidade")
    amount = forms.DecimalField(max_digits=10, decimal_places=2, label="Valor")
    payment_method = forms.ChoiceField(choices=CashMovement.PAYMENT_METHOD_CHOICES, label="Método")
    happened_at = forms.DateTimeField(
        label="Quando",
        input_formats=["%Y-%m-%dT%H:%M"],
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
    )
    notes = forms.CharField(widget=forms.Textarea, required=False, label="Notas")

    def __init__(self, *args, product_queryset=None, client_queryset=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["product"].queryset = product_queryset or Product.objects.none()
        self.fields["client_profile"].queryset = client_queryset or ClientProfile.objects.none()
        self.fields["client_profile"].label_from_instance = lambda obj: obj.full_name
        if not self.initial.get("happened_at"):
            self.fields["happened_at"].initial = timezone.localtime().strftime("%Y-%m-%dT%H:%M")
        for field in self.fields.values():
            if isinstance(field.widget, forms.Select):
                field.widget.attrs.setdefault("class", "form-select")
            elif isinstance(field.widget, forms.Textarea):
                field.widget.attrs.setdefault("class", "form-control")
                field.widget.attrs.setdefault("rows", 2)
            else:
                field.widget.attrs.setdefault("class", "form-control")


class CashVoidMovementForm(forms.Form):
    void_reason = forms.CharField(
        max_length=255,
        label="Motivo da anulação",
        widget=forms.Textarea,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["void_reason"].widget.attrs.setdefault("class", "form-control")
        self.fields["void_reason"].widget.attrs.setdefault("rows", 2)


class MoloniCustomerDefaultsForm(forms.Form):
    payment_method_id = forms.IntegerField(min_value=1, label="Método de pagamento")
    document_type_id = forms.IntegerField(min_value=1, label="Tipo de documento")
    language_id = forms.IntegerField(min_value=1, label="Idioma")
    maturity_date_id = forms.IntegerField(min_value=1, label="Condição de vencimento")
    country_id = forms.IntegerField(min_value=1, label="País")
    delivery_method_id = forms.IntegerField(min_value=1, required=False, label="Método de envio")

    FIELD_HELP_TEXT = {
        "payment_method_id": "Método base usado quando a app cria um cliente novo na Moloni.",
        "document_type_id": "Tipo de documento por defeito associado ao cliente na Moloni.",
        "language_id": "Idioma por defeito do cliente na Moloni.",
        "maturity_date_id": "Condição ou prazo de vencimento por defeito.",
        "country_id": "País por defeito para novos clientes.",
        "delivery_method_id": "Opcional. Método de envio por defeito.",
    }

    def __init__(self, *args, suggestions=None, **kwargs):
        super().__init__(*args, **kwargs)
        suggestions = suggestions or {}
        suggestions_by_field = {
            row.get("field"): row
            for row in suggestions.get("fields", [])
            if row.get("field")
        }

        for field_name, field in self.fields.items():
            field.help_text = self.FIELD_HELP_TEXT.get(field_name, "")
            row = suggestions_by_field.get(field_name) or {}
            options = list(row.get("options") or [])

            if options:
                choices = [("", "— escolher —")]
                if not field.required:
                    choices = [("", "— sem método de envio —")]
                existing_values = set()
                for option in options:
                    value = str(option.get("value") or "").strip()
                    if not value:
                        continue
                    existing_values.add(value)
                    count = int(option.get("count") or 0)
                    sample_names = option.get("sample_names") or []
                    label = value
                    if count:
                        label = f"{label} · usado em {count} cliente{'s' if count != 1 else ''}"
                    if sample_names:
                        label = f"{label} · ex.: {', '.join(sample_names)}"
                    choices.append((value, label))

                current_value = self.data.get(field_name) if self.is_bound else self.initial.get(field_name)
                current_value = str(current_value or "").strip()
                if current_value and current_value not in existing_values:
                    choices.append((current_value, f"{current_value} · valor guardado"))

                field.widget = forms.Select(choices=choices)
                field.widget.attrs.setdefault("class", "form-select")
            else:
                field.widget.attrs.setdefault("class", "form-control")
