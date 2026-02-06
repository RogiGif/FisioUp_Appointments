import re
from decimal import Decimal
from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.forms import PasswordResetForm, SetPasswordForm
from django.core.exceptions import ValidationError
from .models import ClientProfile, Professional, Service, Partner, PartnerServicePrice, Availability
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
            "service_type",
            "capacity",
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


class BackofficeAvailabilityForm(forms.ModelForm):
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
        model = Availability
        fields = ["professional", "weekday", "start_time", "end_time"]

    def clean(self):
        cleaned = super().clean()
        start = cleaned.get("start_time")
        end = cleaned.get("end_time")
        if start and end and end <= start:
            self.add_error("end_time", "A hora de fim deve ser posterior à hora de início.")
        return cleaned


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
            "active",
            "discount_type",
            "discount_percent",
            "discount_amount",
            "discount_label",
            "notes",
        ]

    def clean_name(self):
        name = (self.cleaned_data.get("name") or "").strip()
        if len(name) < 2:
            raise ValidationError("O nome deve ter pelo menos 2 caracteres.")
        return name


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
        from core.emails import send_templated_email, clinic_settings

        reset_url = f"{context['protocol']}://{context['domain']}{reverse('password_reset_confirm', args=[context['uid'], context['token']])}"
        clinic_name = clinic_settings().clinic_name
        subject = f"Recuperação de password — {clinic_name}"
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
    terms_accepted = forms.BooleanField(
        required=True,
        label="Li e aceito os Termos e Condicoes",
        error_messages={"required": "Campo de preenchimento obrigatório"},
    )
    rgpd_accepted = forms.BooleanField(
        required=True,
        label="Li e aceito a politica de RGPD",
        error_messages={"required": "Campo de preenchimento obrigatório"},
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
        for name in ("terms_accepted", "rgpd_accepted"):
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
        required=True,
        label="Morada",
        error_messages={"required": "Campo de preenchimento obrigatório"},
    )
    address_line2 = forms.CharField(max_length=255, required=False, label="Morada (linha 2)")
    postal_code = forms.CharField(
        max_length=20,
        required=True,
        label="Código postal",
        error_messages={"required": "Campo de preenchimento obrigatório"},
    )
    city = forms.CharField(
        max_length=120,
        required=True,
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
        super().__init__(*args, **kwargs)
        self.fields["partner"].queryset = Partner.objects.all().order_by("name")

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
        return _validate_nif_value(self.cleaned_data.get("nif"))

    def clean_phone(self):
        phone = (self.cleaned_data.get("phone") or "").strip()
        normalized = re.sub(r"[^\d]", "", phone)
        if normalized and len(normalized) < 9:
            raise ValidationError("Indica um número de telefone válido.")
        return normalized

    def clean_postal_code(self):
        postal_code = (self.cleaned_data.get("postal_code") or "").strip()
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
