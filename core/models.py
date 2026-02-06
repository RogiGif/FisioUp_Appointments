from decimal import Decimal, ROUND_HALF_UP
from django.db import models
from django.core.exceptions import ValidationError
from django.contrib.auth.models import User
from django.conf import settings
from django.utils import timezone
from django.utils.text import slugify



class Service(models.Model):
    name = models.CharField(max_length=100)
    duration_minutes = models.PositiveIntegerField(default=30)
    price = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    PRICING_MODE_CHOICES = (
        ("single", "Preço único"),
        ("first_followup", "1ª consulta / seguintes"),
    )
    pricing_mode = models.CharField(
        max_length=20,
        choices=PRICING_MODE_CHOICES,
        default="single",
    )
    price_first = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    price_followup = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    SERVICE_TYPE_CHOICES = (
        ("one_to_one", "Consulta"),
        ("group", "Turma"),
    )
    service_type = models.CharField(
        max_length=20,
        choices=SERVICE_TYPE_CHOICES,
        default="one_to_one",
    )
    capacity = models.PositiveIntegerField(null=True, blank=True)

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        if self.pricing_mode == "first_followup":
            # Não existe preço base neste modo
            self.price = Decimal("0.00")
            if self.price_first is None:
                raise ValidationError({"price_first": "Indica o preço da 1ª consulta."})
            if self.price_followup is None:
                raise ValidationError({"price_followup": "Indica o preço das seguintes."})


class Partner(models.Model):
    name = models.CharField(max_length=120, unique=True)
    active = models.BooleanField(default=True)
    notes = models.TextField(blank=True, default="")
    DISCOUNT_CHOICES = [
        ("none", "Sem desconto"),
        ("percent", "Percentagem"),
        ("fixed", "Valor fixo"),
    ]
    discount_type = models.CharField(max_length=20, choices=DISCOUNT_CHOICES, default="none")
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    discount_label = models.CharField(max_length=120, blank=True)

    class Meta:
        verbose_name = "Parceria"
        verbose_name_plural = "Parcerias"

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        if self.discount_type == "percent":
            if self.discount_percent is None:
                raise ValidationError({"discount_percent": "Indica a percentagem de desconto."})
            if self.discount_percent < 0 or self.discount_percent > 100:
                raise ValidationError({"discount_percent": "Percentagem inválida (0-100)."})
            self.discount_amount = None
        elif self.discount_type == "fixed":
            if self.discount_amount is None:
                raise ValidationError({"discount_amount": "Indica o valor fixo de desconto."})
            if self.discount_amount < 0:
                raise ValidationError({"discount_amount": "O valor deve ser positivo."})
            self.discount_percent = None
        else:
            self.discount_percent = None
            self.discount_amount = None


class PartnerServicePrice(models.Model):
    partner = models.ForeignKey(
        "Partner",
        on_delete=models.CASCADE,
        related_name="service_prices",
    )
    service = models.ForeignKey(
        "Service",
        on_delete=models.CASCADE,
        related_name="partner_prices",
    )
    price = models.DecimalField(max_digits=10, decimal_places=2)
    pricing_mode = models.CharField(
        max_length=20,
        choices=Service.PRICING_MODE_CHOICES,
        default="single",
    )
    price_first = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    price_followup = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)

    class Meta:
        unique_together = ("partner", "service")

    def __str__(self):
        return f"{self.partner} - {self.service} - {self.price}"

    def clean(self):
        super().clean()
        if self.pricing_mode == "first_followup":
            if self.price_first is None:
                raise ValidationError({"price_first": "Indica o preço da 1ª consulta."})
            if self.price_followup is None:
                raise ValidationError({"price_followup": "Indica o preço das seguintes."})


class Professional(models.Model):
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="professional"
    )
    speciality = models.CharField(max_length=100, blank=True)
    profile_photo = models.ImageField(upload_to="profiles/professionals/", blank=True, null=True)
    GENDER_CHOICES = [
        ("masculino", "Masculino"),
        ("feminino", "Feminino"),
    ]
    gender = models.CharField(max_length=20, blank=True, choices=GENDER_CHOICES)
    phone = models.CharField(max_length=30, blank=True)

    # ✅ NOVO: serviços que este profissional realiza
    services = models.ManyToManyField(
        "Service",
        related_name="professionals",
        blank=True
    )

    def __str__(self):
        return self.user.get_full_name() or self.user.username


class Availability(models.Model):
    WEEKDAYS = [
        (0, "Monday"),
        (1, "Tuesday"),
        (2, "Wednesday"),
        (3, "Thursday"),
        (4, "Friday"),
        (5, "Saturday"),
        (6, "Sunday"),
    ]

    professional = models.ForeignKey(
        Professional,
        on_delete=models.CASCADE,
        related_name="availabilities",   # ✅ adiciona isto
    )
    weekday = models.IntegerField(choices=WEEKDAYS)
    start_time = models.TimeField()
    end_time = models.TimeField()

    class Meta:
        ordering = ["professional", "weekday", "start_time"]

    def __str__(self):
        return f"{self.professional} - {self.get_weekday_display()} ({self.start_time}-{self.end_time})"


class Appointment(models.Model):
    STATUS_SCHEDULED = "scheduled"
    STATUS_COMPLETED = "completed"
    STATUS_CANCELLED = "cancelled"

    STATUS_CHOICES = (
        (STATUS_SCHEDULED, "Agendada"),
        (STATUS_COMPLETED, "Concluída"),
        (STATUS_CANCELLED, "Cancelada"),
    )

    client = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="appointments",
    )
    professional = models.ForeignKey(
        "Professional",
        on_delete=models.CASCADE,
        related_name="appointments",
    )
    service = models.ForeignKey(
        "Service",
        on_delete=models.CASCADE,
        related_name="appointments",
    )

    date = models.DateField()
    time = models.TimeField()
    notes = models.TextField(blank=True)
    series_id = models.UUIDField(null=True, blank=True, db_index=True)

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_SCHEDULED,
    )

    base_price = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    partner = models.ForeignKey(
        "Partner",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="appointments",
    )
    partner_price = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    discount_type = models.CharField(
        max_length=20,
        choices=(
            ("none", "Sem desconto"),
            ("percent", "Percentagem"),
            ("fixed", "Valor fixo"),
        ),
        default="none",
    )
    discount_value = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    final_price = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    session_index = models.PositiveIntegerField(default=1)
    pricing_tier = models.CharField(
        max_length=20,
        choices=(
            ("single", "single"),
            ("first", "first"),
            ("followup", "followup"),
        ),
        default="single",
    )
    base_price_applied = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    partner_price_applied = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    discount_applied = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))

    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="completed_appointments",
    )
    completed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("date", "time")
        permissions = [
            ("can_view_all_calendar", "Pode ver calendário global"),
            ("can_book_for_any_professional", "Pode marcar para qualquer profissional"),
            ("can_access_backoffice", "Pode aceder ao backoffice"),
        ]

    def __str__(self):
        return f"{self.client} – {self.date} {self.time}"

class AppointmentLog(models.Model):
    ACTION_CREATED = "created"
    ACTION_RESCHEDULED = "rescheduled"
    ACTION_CANCELLED = "cancelled"
    ACTION_COMPLETED = "completed"
    ACTION_NOTES_UPDATED = "notes_updated"

    ACTION_CHOICES = [
        (ACTION_CREATED, "Criada"),
        (ACTION_RESCHEDULED, "Reagendada"),
        (ACTION_CANCELLED, "Cancelada"),
        (ACTION_COMPLETED, "Concluída"),
        (ACTION_NOTES_UPDATED, "Notas atualizadas"),
    ]

    appointment = models.ForeignKey("Appointment", on_delete=models.CASCADE, related_name="logs")
    action = models.CharField(max_length=32, choices=ACTION_CHOICES)

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="appointment_logs",
    )

    # para saber “o que mudou”
    old_date = models.DateField(null=True, blank=True)
    old_time = models.TimeField(null=True, blank=True)
    new_date = models.DateField(null=True, blank=True)
    new_time = models.TimeField(null=True, blank=True)

    old_status = models.CharField(max_length=32, null=True, blank=True)
    new_status = models.CharField(max_length=32, null=True, blank=True)

    note = models.CharField(max_length=255, blank=True, default="")  # opcional (“motivo”, etc.)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.action} · appt #{self.appointment_id} · {self.created_at:%Y-%m-%d %H:%M}"

class ClientProfile(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="client_profile",
        null=True,
        blank=True,
    )

    full_name = models.CharField(max_length=200)
    phone = models.CharField(max_length=30, blank=True)
    GENDER_CHOICES = [
        ("masculino", "Masculino"),
        ("feminino", "Feminino"),
    ]
    gender = models.CharField(max_length=20, blank=True, choices=GENDER_CHOICES)
    terms_accepted = models.BooleanField(default=False)
    rgpd_accepted = models.BooleanField(default=False)
    REG_STATUS_CHOICES = [
        ("approved", "Aprovado"),
        ("pending", "Pendente"),
        ("rejected", "Rejeitado"),
    ]
    registration_status = models.CharField(
        max_length=20,
        choices=REG_STATUS_CHOICES,
        default="approved",
    )
    registration_requested_at = models.DateTimeField(null=True, blank=True)
    registration_reviewed_at = models.DateTimeField(null=True, blank=True)
    registration_reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reviewed_clients",
    )
    profile_photo = models.ImageField(upload_to="profiles/clients/", blank=True, null=True)

    nif = models.CharField(max_length=20, blank=True)
    moloni_customer_id = models.CharField(max_length=50, blank=True)
    partner = models.ForeignKey(
        "Partner",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="clients",
    )
    DISCOUNT_CHOICES = [
        ("none", "Sem desconto"),
        ("percent", "Percentagem"),
        ("fixed", "Valor fixo"),
    ]
    discount_type = models.CharField(max_length=20, choices=DISCOUNT_CHOICES, default="none")
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    discount_label = models.CharField(max_length=120, blank=True)
    address_line1 = models.CharField(max_length=255, blank=True)
    address_line2 = models.CharField(max_length=255, blank=True)
    postal_code = models.CharField(max_length=20, blank=True)
    postal_designation = models.CharField(max_length=120, blank=True)
    city = models.CharField(max_length=120, blank=True)
    country = models.CharField(max_length=120, blank=True)
    district = models.CharField(max_length=120, blank=True)
    county = models.CharField(max_length=120, blank=True)
    locality = models.CharField(max_length=120, blank=True)

    birth_date = models.DateField(null=True, blank=True)
    require_complete_profile = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="created_clients"
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="updated_clients"
    )

    def __str__(self):
        return self.full_name

    def clean(self):
        super().clean()
        if self.discount_type == "percent":
            if self.discount_percent is None:
                raise ValidationError({"discount_percent": "Indica a percentagem de desconto."})
            if self.discount_percent < 0 or self.discount_percent > 100:
                raise ValidationError({"discount_percent": "Percentagem inválida (0-100)."})
            self.discount_amount = None
        elif self.discount_type == "fixed":
            if self.discount_amount is None:
                raise ValidationError({"discount_amount": "Indica o valor fixo de desconto."})
            if self.discount_amount < 0:
                raise ValidationError({"discount_amount": "O valor deve ser positivo."})
            self.discount_percent = None
        else:
            self.discount_percent = None
            self.discount_amount = None


class MoloniIntegration(models.Model):
    access_token = models.TextField(blank=True)
    refresh_token = models.TextField(blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    last_sync_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Integração Moloni"
        verbose_name_plural = "Integração Moloni"

    @classmethod
    def get_solo(cls):
        obj = cls.objects.first()
        if obj:
            return obj
        return cls.objects.create()

    def __str__(self):
        return "Moloni"


class ClientImportLog(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="client_import_logs",
    )
    file_name = models.CharField(max_length=255, blank=True)
    created_count = models.IntegerField(default=0)
    updated_count = models.IntegerField(default=0)
    skipped_count = models.IntegerField(default=0)
    error_count = models.IntegerField(default=0)
    summary = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Importação de clientes"
        verbose_name_plural = "Importações de clientes"

    def __str__(self):
        return f"Importação {self.created_at:%Y-%m-%d %H:%M}"


class ClientImportBatch(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="client_import_batches",
    )
    original_filename = models.CharField(max_length=255, blank=True)
    validate_nif = models.BooleanField(default=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Lote de importação (clientes)"
        verbose_name_plural = "Lotes de importação (clientes)"

    def __str__(self):
        return f"Lote {self.created_at:%Y-%m-%d %H:%M}"


class ClientImportRow(models.Model):
    batch = models.ForeignKey(
        "ClientImportBatch",
        on_delete=models.CASCADE,
        related_name="rows",
    )
    row_key = models.PositiveIntegerField()
    full_name = models.CharField(max_length=255, blank=True)
    nif = models.CharField(max_length=20, blank=True)
    phone = models.CharField(max_length=30, blank=True)
    email = models.CharField(max_length=255, blank=True)
    address_line1 = models.CharField(max_length=255, blank=True)
    postal_code = models.CharField(max_length=20, blank=True)
    city = models.CharField(max_length=120, blank=True)
    county = models.CharField(max_length=120, blank=True)
    district = models.CharField(max_length=120, blank=True)
    valid_vat = models.BooleanField(default=False)
    missing_email = models.BooleanField(default=True)
    duplicate_in_file = models.BooleanField(default=False)
    exists_in_db = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["row_key"]
        unique_together = ("batch", "row_key")
        verbose_name = "Linha de importação (clientes)"
        verbose_name_plural = "Linhas de importação (clientes)"

    def __str__(self):
        return f"Linha {self.row_key} ({self.nif})"


class BlockedSlot(models.Model):
    professional = models.ForeignKey(
        "Professional",
        on_delete=models.CASCADE,
        related_name="blocked_slots",
    )
    date = models.DateField()
    time = models.TimeField()
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_blocked_slots",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("professional", "date", "time")
        ordering = ["-date", "-time"]

    def __str__(self):
        return f"{self.professional} · {self.date} {self.time}"


class GroupSession(models.Model):
    STATUS_SCHEDULED = "scheduled"
    STATUS_COMPLETED = "completed"
    STATUS_CANCELLED = "cancelled"

    STATUS_CHOICES = (
        (STATUS_SCHEDULED, "Agendada"),
        (STATUS_COMPLETED, "Concluída"),
        (STATUS_CANCELLED, "Cancelada"),
    )

    service = models.ForeignKey(
        "Service",
        on_delete=models.CASCADE,
        related_name="group_sessions",
    )
    professional = models.ForeignKey(
        "Professional",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="group_sessions",
    )
    date = models.DateField()
    time = models.TimeField()
    capacity = models.PositiveIntegerField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_SCHEDULED)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("service", "professional", "date", "time")
        ordering = ("date", "time")

    @property
    def capacity_value(self):
        return self.capacity or self.service.capacity or 0

    @property
    def spots_left(self):
        taken = self.enrolments.filter(status="active").count()
        return max(self.capacity_value - taken, 0)

    def __str__(self):
        return f"{self.service.name} · {self.date} {self.time}"


class GroupEnrollment(models.Model):
    STATUS_ACTIVE = "active"
    STATUS_CANCELLED = "cancelled"

    STATUS_CHOICES = (
        (STATUS_ACTIVE, "Ativa"),
        (STATUS_CANCELLED, "Cancelada"),
    )

    session = models.ForeignKey(
        "GroupSession",
        on_delete=models.CASCADE,
        related_name="enrolments",
    )
    client = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="group_enrolments",
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("session", "client")
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.client} · {self.session}"


class ClinicSettings(models.Model):
    clinic_name = models.CharField(max_length=120, default="FisioUp", verbose_name="Nome da clínica")
    clinic_email = models.EmailField(blank=True, verbose_name="Email da clínica")
    notification_emails = models.TextField(
        blank=True,
        verbose_name="Emails de notificação",
        help_text="Emails separados por vírgulas ou por linha. Ex: rececao@..., gerente@...",
    )
    from_email = models.EmailField(blank=True, verbose_name="Email de envio")
    reply_to_email = models.EmailField(blank=True, verbose_name="Email de resposta")
    footer_text = models.TextField(blank=True, verbose_name="Texto de rodapé")
    signature_text = models.TextField(blank=True, verbose_name="Assinatura")
    logo = models.ImageField(upload_to="clinic/", blank=True, null=True, verbose_name="Logo")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Atualizado em")

    notify_admin_on_pending_registration = models.BooleanField(default=True, verbose_name="Notificar pedido de registo pendente")
    notify_clinic_on_new_booking = models.BooleanField(default=True, verbose_name="Notificar nova marcação")
    notify_clinic_on_client_reschedule = models.BooleanField(default=True, verbose_name="Notificar reagendamento pelo utente")
    notify_clinic_on_client_cancel = models.BooleanField(default=True, verbose_name="Notificar cancelamento pelo utente")
    notify_client_on_clinic_changes = models.BooleanField(default=True, verbose_name="Notificar utente quando a clínica altera")
    notify_professional_on_new_booking = models.BooleanField(default=True, verbose_name="Notificar profissional em nova marcação")

    class Meta:
        verbose_name = "Configuração da clínica"
        verbose_name_plural = "Configuração da clínica"

    def save(self, *args, **kwargs):
        if not self.pk and ClinicSettings.objects.exists():
            raise ValueError("Só pode existir uma configuração da clínica.")
        return super().save(*args, **kwargs)

    @classmethod
    def get_solo(cls):
        obj = cls.objects.first()
        if obj:
            return obj
        return cls.objects.create()

    def __str__(self):
        return self.clinic_name


class EmailLog(models.Model):
    EVENT_CHOICES = [
        ("pending_registration", "Pedido de registo"),
        ("new_booking", "Nova marcação"),
        ("reschedule_client", "Reagendamento pelo utente"),
        ("reschedule_clinic", "Reagendamento pela clínica"),
        ("cancel_client", "Cancelamento pelo utente"),
        ("cancel_clinic", "Cancelamento pela clínica"),
        ("password_reset", "Recuperação de password"),
        ("generic", "Genérico"),
    ]

    event = models.CharField(max_length=64, choices=EVENT_CHOICES, default="generic", verbose_name="Evento")
    to = models.TextField(verbose_name="Destinatários")
    subject = models.CharField(max_length=255, verbose_name="Assunto")
    body_text = models.TextField(blank=True, verbose_name="Corpo (texto)")
    body_html = models.TextField(blank=True, verbose_name="Corpo (HTML)")
    status = models.CharField(max_length=20, default="sent", verbose_name="Estado")
    error = models.TextField(blank=True, verbose_name="Erro")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Criado em")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.event} · {self.subject}"

class TreatmentRecord(models.Model):
    client = models.ForeignKey(
        "ClientProfile",
        on_delete=models.CASCADE,
        related_name="treatments",
    )

    professional = models.ForeignKey(
        "Professional",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="treatments",
    )

    appointment = models.OneToOneField(
        "Appointment",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="treatment_record",
    )

    # ✅ Guardar ID + nome do serviço
    service = models.ForeignKey(
        "Service",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="treatment_records",
    )
    service_name = models.CharField(max_length=200)

    date = models.DateField()

    # ✅ Hora obrigatória (sem null/blank)
    time = models.TimeField()

    notes = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_treatments",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="updated_treatments",
    )

    def __str__(self):
        svc = self.service_name or (self.service.name if self.service else "Sem serviço")
        return f"Tratamento {self.client.full_name} - {svc} - {self.date} {self.time}"

class ClinicalRecord(models.Model):
    client = models.OneToOneField(ClientProfile, on_delete=models.CASCADE, related_name="clinical_record")

    allergies = models.TextField(blank=True)
    conditions = models.TextField(blank=True)
    notes = models.TextField(blank=True)

    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="updated_records"
    )

    def __str__(self):
        return f"Ficha clínica — {self.client.full_name}"


class ContentPost(models.Model):
    KIND_CHOICES = (
        ("news", "Notícia"),
        ("promo", "Promoção"),
        ("notice", "Aviso"),
        ("partner", "Parceria"),
        ("project", "Projeto"),
    )
    STATUS_CHOICES = (
        ("draft", "Rascunho"),
        ("published", "Publicado"),
    )

    title = models.CharField(max_length=200)
    slug = models.SlugField(unique=True, blank=True)
    kind = models.CharField(max_length=20, choices=KIND_CHOICES, default="news")
    excerpt = models.TextField(blank=True)
    body = models.TextField()
    cover_image = models.ImageField(upload_to="posts/", blank=True, null=True)
    is_featured = models.BooleanField(default=False)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="draft")
    published_at = models.DateTimeField(blank=True, null=True)
    author = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="posts_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-is_featured", "-published_at", "-created_at"]

    def __str__(self):
        return self.title

    def clean(self):
        super().clean()
        if self.title and len(self.title.strip()) < 5:
            raise ValidationError({"title": "O título deve ter pelo menos 5 caracteres."})

    def _generate_unique_slug(self):
        base_slug = slugify(self.title) or "post"
        slug = base_slug
        counter = 2
        while ContentPost.objects.exclude(pk=self.pk).filter(slug=slug).exists():
            slug = f"{base_slug}-{counter}"
            counter += 1
        return slug

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = self._generate_unique_slug()
        if self.status == "published" and not self.published_at:
            self.published_at = timezone.now()
        super().save(*args, **kwargs)
