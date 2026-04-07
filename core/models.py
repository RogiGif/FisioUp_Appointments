from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timedelta
from django.db import models
from django.core.exceptions import ValidationError
from django.contrib.auth.models import User
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.conf import settings
from django.utils import timezone
from django.utils.text import slugify



class Service(models.Model):
    name = models.CharField(max_length=100)
    duration_minutes = models.PositiveIntegerField(default=30)
    slot_interval_minutes = models.PositiveIntegerField(null=True, blank=True)
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
    allow_waitlist = models.BooleanField(default=False)

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        if self.slot_interval_minutes:
            if self.slot_interval_minutes < 5:
                raise ValidationError({"slot_interval_minutes": "O intervalo deve ser no mínimo 5 minutos."})
            if self.slot_interval_minutes > self.duration_minutes:
                raise ValidationError({"slot_interval_minutes": "O intervalo não pode ser maior do que a duração."})
        if self.pricing_mode == "first_followup":
            # Não existe preço base neste modo
            self.price = Decimal("0.00")
            if self.price_first is None:
                raise ValidationError({"price_first": "Indica o preço da 1ª consulta."})
            if self.price_followup is None:
                raise ValidationError({"price_followup": "Indica o preço das seguintes."})


class Partner(models.Model):
    name = models.CharField(max_length=120, unique=True)
    logo = models.ImageField(upload_to="partners/", blank=True, null=True, verbose_name="Logo")
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
    DISCOUNT_CHOICES = [
        ("none", "Sem desconto"),
        ("percent", "Percentagem"),
        ("fixed", "Valor fixo"),
    ]

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
    discount_type = models.CharField(
        max_length=20,
        choices=DISCOUNT_CHOICES,
        default="none",
    )
    is_enabled = models.BooleanField(
        default=True,
        help_text="Quando desativado, esta parceria não tem efeito neste serviço.",
    )
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)

    class Meta:
        unique_together = ("partner", "service")

    def __str__(self):
        return f"{self.partner} - {self.service} - {self.price}"

    def clean(self):
        super().clean()
        if not self.is_enabled:
            self.discount_type = "none"
            self.discount_percent = None
            self.discount_amount = None
            return

        if self.discount_type == "percent":
            if self.discount_percent is None:
                raise ValidationError({"discount_percent": "Indica a percentagem de desconto."})
            self.discount_amount = None
        elif self.discount_type == "fixed":
            if self.discount_amount is None:
                raise ValidationError({"discount_amount": "Indica o valor fixo de desconto."})
            self.discount_percent = None
        else:
            self.discount_percent = None
            self.discount_amount = None

        if self.pricing_mode == "first_followup" and self.discount_type == "none":
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
    is_independent = models.BooleanField(
        default=False,
        verbose_name="Subcontratado",
    )
    subcontract_percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name="Percentagem subcontrato",
    )
    hourly_rate = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name="Valor por marcação",
    )

    # ✅ NOVO: serviços que este profissional realiza
    services = models.ManyToManyField(
        "Service",
        related_name="professionals",
        blank=True
    )

    def __str__(self):
        return self.user.get_full_name() or self.user.username

    def clean(self):
        super().clean()
        if self.is_independent:
            if self.subcontract_percentage is None:
                raise ValidationError({"subcontract_percentage": "Indica a percentagem de comissionamento."})
            if self.subcontract_percentage < 0 or self.subcontract_percentage > 100:
                raise ValidationError({"subcontract_percentage": "Percentagem inválida (0-100)."})


class WeeklySchedule(models.Model):
    professional = models.OneToOneField(
        Professional,
        on_delete=models.CASCADE,
        related_name="weekly_schedule",
    )
    timezone = models.CharField(max_length=50, default="Europe/Lisbon")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        name = self.professional.user.get_full_name() or self.professional.user.username
        return f"Horário semanal · {name}"


class WeeklyWorkingBlock(models.Model):
    WEEKDAYS = [
        (0, "Monday"),
        (1, "Tuesday"),
        (2, "Wednesday"),
        (3, "Thursday"),
        (4, "Friday"),
        (5, "Saturday"),
        (6, "Sunday"),
    ]

    weekly_schedule = models.ForeignKey(
        WeeklySchedule,
        on_delete=models.CASCADE,
        related_name="blocks",
    )
    weekday = models.IntegerField(choices=WEEKDAYS)
    start_time = models.TimeField()
    end_time = models.TimeField()
    location = models.CharField(max_length=120, blank=True)

    class Meta:
        ordering = ["weekly_schedule", "weekday", "start_time"]

    def __str__(self):
        return f"{self.weekly_schedule.professional} · {self.get_weekday_display()} ({self.start_time}-{self.end_time})"

    def clean(self):
        super().clean()
        if self.start_time and self.end_time and self.end_time <= self.start_time:
            raise ValidationError({"end_time": "A hora de fim deve ser posterior à hora de início."})
        if not self.weekly_schedule_id or self.weekday is None or not self.start_time or not self.end_time:
            return
        overlaps = WeeklyWorkingBlock.objects.filter(
            weekly_schedule_id=self.weekly_schedule_id,
            weekday=self.weekday,
        ).exclude(id=self.id)
        for block in overlaps:
            if self.start_time < block.end_time and self.end_time > block.start_time:
                raise ValidationError("Este bloco de trabalho sobrepõe-se a outro bloco no mesmo dia.")


class WeeklyBreakBlock(models.Model):
    WEEKDAYS = WeeklyWorkingBlock.WEEKDAYS

    weekly_schedule = models.ForeignKey(
        WeeklySchedule,
        on_delete=models.CASCADE,
        related_name="breaks",
    )
    weekday = models.IntegerField(choices=WEEKDAYS)
    start_time = models.TimeField()
    end_time = models.TimeField()

    class Meta:
        ordering = ["weekly_schedule", "weekday", "start_time"]

    def __str__(self):
        return f"Pausa · {self.weekly_schedule.professional} · {self.get_weekday_display()} ({self.start_time}-{self.end_time})"

    def clean(self):
        super().clean()
        if self.start_time and self.end_time and self.end_time <= self.start_time:
            raise ValidationError({"end_time": "A hora de fim deve ser posterior à hora de início."})
        if not self.weekly_schedule_id or self.weekday is None or not self.start_time or not self.end_time:
            return
        overlaps = WeeklyBreakBlock.objects.filter(
            weekly_schedule_id=self.weekly_schedule_id,
            weekday=self.weekday,
        ).exclude(id=self.id)
        for block in overlaps:
            if self.start_time < block.end_time and self.end_time > block.start_time:
                raise ValidationError("Esta pausa sobrepõe-se a outra pausa no mesmo dia.")


class Appointment(models.Model):
    STATUS_SCHEDULED = "scheduled"
    STATUS_PENDING = "pending_confirmation"
    STATUS_AWAITING_VALIDATION = "awaiting_validation"
    STATUS_NO_SHOW = "no_show"
    STATUS_COMPLETED = "completed"
    STATUS_IN_DEBT = "in_debt"
    STATUS_CANCELLED = "cancelled"

    STATUS_CHOICES = (
        (STATUS_SCHEDULED, "Agendada"),
        (STATUS_PENDING, "Em confirmação"),
        (STATUS_AWAITING_VALIDATION, "A aguardar validação"),
        (STATUS_NO_SHOW, "Falta"),
        (STATUS_COMPLETED, "Concluída"),
        (STATUS_IN_DEBT, "Em dívida"),
        (STATUS_CANCELLED, "Cancelada"),
    )

    SETTLEMENT_PRICING_MODE_AUTO = "auto"
    SETTLEMENT_PRICING_MODE_WITHOUT_PARTNER = "without_partner"
    SETTLEMENT_PRICING_MODE_MANUAL = "manual"
    SETTLEMENT_PRICING_MODE_CHOICES = (
        (SETTLEMENT_PRICING_MODE_AUTO, "Automático"),
        (SETTLEMENT_PRICING_MODE_WITHOUT_PARTNER, "Sem parceria"),
        (SETTLEMENT_PRICING_MODE_MANUAL, "Manual"),
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
    symptomatology = models.TextField(blank=True)
    summary = models.TextField(blank=True, default="")
    treatment_done = models.TextField(blank=True, default="")
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
    settlement_pricing_mode = models.CharField(
        max_length=20,
        choices=SETTLEMENT_PRICING_MODE_CHOICES,
        default=SETTLEMENT_PRICING_MODE_AUTO,
    )
    settlement_partner = models.ForeignKey(
        "Partner",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="appointment_settlements",
    )
    settlement_discount_type = models.CharField(
        max_length=20,
        choices=(
            ("none", "Sem desconto"),
            ("percent", "Percentagem"),
            ("fixed", "Valor fixo"),
        ),
        default="none",
    )
    settlement_discount_value = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    settlement_final_price = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    settlement_locked_at = models.DateTimeField(null=True, blank=True)
    settlement_notes = models.CharField(max_length=255, blank=True, default="")

    is_paid = models.BooleanField(default=False)
    paid_at = models.DateTimeField(null=True, blank=True)

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

    def get_charge_amount(self):
        if self.settlement_locked_at:
            return self.settlement_final_price or Decimal("0.00")
        return self.final_price or Decimal("0.00")

    def get_paid_amount(self):
        total = (
            self.payment_allocations.filter(payment__status=ClientPayment.STATUS_POSTED)
            .aggregate(total=models.Sum("allocated_amount"))
            .get("total")
        )
        if total:
            return total
        if self.is_paid:
            return self.get_charge_amount()
        return Decimal("0.00")

    def get_outstanding_amount(self):
        remaining = self.get_charge_amount() - self.get_paid_amount()
        return remaining if remaining > 0 else Decimal("0.00")

    @property
    def charge_amount(self):
        return self.get_charge_amount()

    @property
    def paid_amount(self):
        return self.get_paid_amount()

    @property
    def outstanding_amount(self):
        return self.get_outstanding_amount()


class SubcontractorPaymentLine(models.Model):
    STATUS_UNPAID = "unpaid"
    STATUS_PAID = "paid"
    STATUS_VOID = "void"

    STATUS_CHOICES = (
        (STATUS_UNPAID, "Em aberto"),
        (STATUS_PAID, "Pago"),
        (STATUS_VOID, "Anulado"),
    )

    appointment = models.OneToOneField(
        Appointment,
        on_delete=models.CASCADE,
        related_name="subcontract_payment",
    )
    professional = models.ForeignKey(
        Professional,
        on_delete=models.CASCADE,
        related_name="subcontract_payments",
    )
    client = models.ForeignKey(
        "ClientProfile",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="subcontract_payments",
    )
    service = models.ForeignKey(
        "Service",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="subcontract_payments",
    )
    appointment_date = models.DateField()
    appointment_time = models.TimeField()
    gross_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    percentage = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))
    payable_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_UNPAID)
    paid_at = models.DateTimeField(null=True, blank=True)
    paid_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="subcontract_payments_paid",
    )
    payment_reference = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-appointment_date", "-appointment_time")

    def __str__(self):
        return f"{self.professional} · {self.appointment_date} {self.appointment_time} · {self.payable_amount}"

class AppointmentLog(models.Model):
    ACTION_CREATED = "created"
    ACTION_RESCHEDULED = "rescheduled"
    ACTION_CANCELLED = "cancelled"
    ACTION_COMPLETED = "completed"
    ACTION_NOTES_UPDATED = "notes_updated"
    ACTION_STATUS_UPDATED = "status_updated"

    ACTION_CHOICES = [
        (ACTION_CREATED, "Criada"),
        (ACTION_RESCHEDULED, "Reagendada"),
        (ACTION_CANCELLED, "Cancelada"),
        (ACTION_COMPLETED, "Concluída"),
        (ACTION_NOTES_UPDATED, "Notas atualizadas"),
        (ACTION_STATUS_UPDATED, "Estado atualizado"),
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


class AuditLog(models.Model):
    category = models.CharField(max_length=64, db_index=True)
    action = models.CharField(max_length=64, db_index=True)
    source = models.CharField(max_length=64, blank=True, default="", db_index=True)

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_logs",
    )
    actor_display = models.CharField(max_length=255, blank=True, default="")
    actor_email = models.EmailField(blank=True, default="")
    actor_role = models.CharField(max_length=64, blank=True, default="", db_index=True)

    content_type = models.ForeignKey(
        ContentType,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_logs",
    )
    object_id = models.PositiveBigIntegerField(null=True, blank=True)
    content_object = GenericForeignKey("content_type", "object_id")
    object_repr = models.CharField(max_length=255, blank=True, default="")

    message = models.CharField(max_length=255, blank=True, default="")
    before = models.JSONField(default=dict, blank=True)
    after = models.JSONField(default=dict, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True, default="")
    request_path = models.CharField(max_length=255, blank=True, default="")
    request_method = models.CharField(max_length=12, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Log de auditoria"
        verbose_name_plural = "Logs de auditoria"
        indexes = [
            models.Index(fields=["category", "action", "-created_at"]),
            models.Index(fields=["source", "-created_at"]),
            models.Index(fields=["actor_role", "-created_at"]),
        ]

    def __str__(self):
        label = self.object_repr or "-"
        return f"{self.category}:{self.action} · {label} · {self.created_at:%Y-%m-%d %H:%M}"


class CashSession(models.Model):
    STATUS_OPEN = "open"
    STATUS_CLOSED = "closed"
    STATUS_CHOICES = (
        (STATUS_OPEN, "Aberta"),
        (STATUS_CLOSED, "Fechada"),
    )

    session_date = models.DateField(default=timezone.localdate, db_index=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_OPEN, db_index=True)
    opening_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    opening_notes = models.TextField(blank=True, default="")
    opened_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="cash_sessions_opened",
    )
    opened_at = models.DateTimeField(auto_now_add=True)
    expected_cash_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    counted_cash_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    difference_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    closing_notes = models.TextField(blank=True, default="")
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="cash_sessions_closed",
    )
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-session_date", "-opened_at", "-id")
        verbose_name = "Sessão de caixa"
        verbose_name_plural = "Sessões de caixa"

    def __str__(self):
        return f"Caixa {self.session_date:%d/%m/%Y} · {self.get_status_display()}"


class CashMovement(models.Model):
    TYPE_IN = "in"
    TYPE_OUT = "out"
    TYPE_CHOICES = (
        (TYPE_IN, "Entrada"),
        (TYPE_OUT, "Saída"),
    )

    SOURCE_MANUAL = "manual"
    SOURCE_CLIENT_PAYMENT = "client_payment"
    SOURCE_APPOINTMENT = "appointment"
    SOURCE_GROUP_MONTHLY = "group_monthly"
    SOURCE_STOCK_SALE = "stock_sale"
    SOURCE_CHOICES = (
        (SOURCE_MANUAL, "Manual"),
        (SOURCE_CLIENT_PAYMENT, "Pagamento de cliente"),
        (SOURCE_APPOINTMENT, "Marcação"),
        (SOURCE_GROUP_MONTHLY, "Turma"),
        (SOURCE_STOCK_SALE, "Stock"),
    )

    METHOD_CASH = "cash"
    METHOD_CARD = "card"
    METHOD_MBWAY = "mbway"
    METHOD_TRANSFER = "transfer"
    METHOD_OTHER = "other"
    PAYMENT_METHOD_CHOICES = (
        (METHOD_CASH, "Numerário"),
        (METHOD_CARD, "Multibanco"),
        (METHOD_MBWAY, "MB Way"),
        (METHOD_TRANSFER, "Transferência"),
        (METHOD_OTHER, "Outro"),
    )

    session = models.ForeignKey(
        CashSession,
        on_delete=models.CASCADE,
        related_name="movements",
    )
    movement_type = models.CharField(max_length=8, choices=TYPE_CHOICES, db_index=True)
    source_type = models.CharField(max_length=20, choices=SOURCE_CHOICES, default=SOURCE_MANUAL, db_index=True)
    payment_method = models.CharField(max_length=20, choices=PAYMENT_METHOD_CHOICES, default=METHOD_CASH, db_index=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    description = models.CharField(max_length=255)
    notes = models.TextField(blank=True, default="")
    client_profile = models.ForeignKey(
        "ClientProfile",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="cash_movements",
    )
    appointment = models.OneToOneField(
        "Appointment",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="cash_movement",
    )
    group_monthly_charge = models.OneToOneField(
        "GroupMonthlyCharge",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="cash_movement",
    )
    stock_movement = models.OneToOneField(
        "StockMovement",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="cash_movement",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="cash_movements_created",
    )
    is_void = models.BooleanField(default=False, db_index=True)
    void_reason = models.CharField(max_length=255, blank=True, default="")
    voided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="cash_movements_voided",
    )
    voided_at = models.DateTimeField(null=True, blank=True)
    happened_at = models.DateTimeField(default=timezone.now, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-happened_at", "-id")
        verbose_name = "Movimento de caixa"
        verbose_name_plural = "Movimentos de caixa"

    def __str__(self):
        return f"{self.get_movement_type_display()} · {self.amount} · {self.session}"

    def clean(self):
        super().clean()
        if self.amount is None or self.amount <= 0:
            raise ValidationError({"amount": "Indica um valor positivo."})
        if self.source_type == self.SOURCE_APPOINTMENT:
            if not self.appointment_id:
                raise ValidationError({"appointment": "Seleciona a marcação associada."})
            if self.movement_type != self.TYPE_IN:
                raise ValidationError({"movement_type": "Movimentos de marcação só podem ser entradas."})
            if self.group_monthly_charge_id:
                raise ValidationError({"group_monthly_charge": "Não combines marcação e mensalidade no mesmo movimento."})
            if self.client_profile_id:
                raise ValidationError({"client_profile": "O utente é definido pela marcação associada."})
        elif self.source_type == self.SOURCE_GROUP_MONTHLY:
            if not self.group_monthly_charge_id:
                raise ValidationError({"group_monthly_charge": "Seleciona a mensalidade associada."})
            if self.movement_type != self.TYPE_IN:
                raise ValidationError({"movement_type": "Movimentos de turma só podem ser entradas."})
            if self.appointment_id:
                raise ValidationError({"appointment": "Não combines marcação e mensalidade no mesmo movimento."})
            if self.stock_movement_id:
                raise ValidationError({"stock_movement": "Não combines turma e stock no mesmo movimento."})
            if self.client_profile_id:
                raise ValidationError({"client_profile": "O utente é definido pela mensalidade associada."})
        elif self.source_type == self.SOURCE_CLIENT_PAYMENT:
            if self.movement_type != self.TYPE_IN:
                raise ValidationError({"movement_type": "Pagamentos de cliente só podem ser entradas."})
            if self.appointment_id or self.group_monthly_charge_id or self.stock_movement_id:
                raise ValidationError({"source_type": "Pagamentos de cliente não podem combinar outras origens."})
        elif self.source_type == self.SOURCE_STOCK_SALE:
            if not self.stock_movement_id:
                raise ValidationError({"stock_movement": "Seleciona o movimento de stock associado."})
            if self.movement_type != self.TYPE_IN:
                raise ValidationError({"movement_type": "Vendas de stock só podem ser entradas."})
            if self.appointment_id or self.group_monthly_charge_id:
                raise ValidationError({"stock_movement": "Não combines stock com outras origens no mesmo movimento."})
        else:
            if self.appointment_id:
                raise ValidationError({"appointment": "A marcação só pode ser associada a movimentos de marcação."})
            if self.group_monthly_charge_id:
                raise ValidationError({"group_monthly_charge": "A mensalidade só pode ser associada a movimentos de turma."})
            if self.stock_movement_id:
                raise ValidationError({"stock_movement": "O movimento de stock só pode ser associado a vendas de stock."})
        if self.is_void and not self.voided_at:
            raise ValidationError({"voided_at": "Indica quando o movimento foi anulado."})

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
    accepted_terms_at = models.DateTimeField(null=True, blank=True)
    accepted_terms_ip = models.GenericIPAddressField(null=True, blank=True)
    accepted_terms_user_agent = models.TextField(null=True, blank=True)
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
    company_id = models.CharField(max_length=50, blank=True)
    company_name = models.CharField(max_length=255, blank=True)
    customer_payment_method_id = models.PositiveIntegerField(null=True, blank=True)
    customer_document_type_id = models.PositiveIntegerField(null=True, blank=True)
    customer_language_id = models.PositiveIntegerField(null=True, blank=True)
    customer_maturity_date_id = models.PositiveIntegerField(null=True, blank=True)
    customer_country_id = models.PositiveIntegerField(null=True, blank=True)
    customer_delivery_method_id = models.PositiveIntegerField(null=True, blank=True)
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
    name = models.CharField(max_length=150, blank=True, default="")
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
    duration_minutes = models.PositiveIntegerField(null=True, blank=True)
    notes = models.TextField(blank=True, default="")
    schedule = models.ForeignKey(
        "GroupSchedule",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="sessions",
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_SCHEDULED)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("service", "professional", "date", "time")
        ordering = ("date", "time")
        verbose_name = "Sessão de turma"
        verbose_name_plural = "Sessões de turma"

    @property
    def capacity_value(self):
        return self.capacity or self.service.capacity or 0

    @property
    def duration_value(self):
        return self.duration_minutes or getattr(self.service, "duration_minutes", None) or 60

    @property
    def start_datetime(self):
        return datetime.combine(self.date, self.time or datetime.min.time())

    @property
    def end_datetime(self):
        return self.start_datetime + timedelta(minutes=self.duration_value)

    @property
    def spots_left(self):
        taken = self.enrolments.filter(
            status__in=[
                GroupEnrollment.STATUS_BOOKED,
                GroupEnrollment.STATUS_ATTENDED,
                GroupEnrollment.STATUS_NO_SHOW,
            ]
        ).count()
        return max(self.capacity_value - taken, 0)

    def __str__(self):
        label = self.name or self.service.name
        return f"{label} · {self.date} {self.time}"


class GroupSchedule(models.Model):
    WEEKDAY_CHOICES = [
        (0, "Segunda-feira"),
        (1, "Terça-feira"),
        (2, "Quarta-feira"),
        (3, "Quinta-feira"),
        (4, "Sexta-feira"),
        (5, "Sábado"),
        (6, "Domingo"),
    ]

    service = models.ForeignKey(
        "Service",
        on_delete=models.CASCADE,
        related_name="group_schedules",
    )
    name = models.CharField(max_length=150, blank=True, default="")
    professional = models.ForeignKey(
        "Professional",
        on_delete=models.CASCADE,
        related_name="group_schedules",
    )
    weekday = models.PositiveSmallIntegerField(choices=WEEKDAY_CHOICES)
    time = models.TimeField()
    start_date = models.DateField()
    capacity = models.PositiveIntegerField(null=True, blank=True)
    duration_minutes = models.PositiveIntegerField(null=True, blank=True)
    notes = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("professional", "weekday", "time")
        ordering = ("weekday", "time")
        verbose_name = "Recorrência de turma"
        verbose_name_plural = "Recorrências de turma"

    @property
    def capacity_value(self):
        return self.capacity or self.service.capacity or 0

    @property
    def duration_value(self):
        return self.duration_minutes or getattr(self.service, "duration_minutes", None) or 60

    def __str__(self):
        label = self.name or self.service.name
        return f"{label} · {self.get_weekday_display()} {self.time}"


class GroupEnrollment(models.Model):
    STATUS_BOOKED = "booked"
    STATUS_WAITLIST = "waitlist"
    STATUS_CANCELLED = "cancelled"
    STATUS_ATTENDED = "attended"
    STATUS_NO_SHOW = "no_show"

    STATUS_CHOICES = (
        (STATUS_BOOKED, "Confirmada"),
        (STATUS_WAITLIST, "Lista de espera"),
        (STATUS_CANCELLED, "Cancelada"),
        (STATUS_ATTENDED, "Presença"),
        (STATUS_NO_SHOW, "Falta"),
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
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_BOOKED)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("session", "client")
        ordering = ("-created_at",)
        verbose_name = "Inscrição de turma"
        verbose_name_plural = "Inscrições de turma"

    def __str__(self):
        return f"{self.client} · {self.session}"


class GroupMembership(models.Model):
    """
    Plano de inscrição do cliente numa turma recorrente (família).
    Permite personalizar dias e preço mensal por inscrito.
    """

    client = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="group_memberships",
    )
    service = models.ForeignKey(
        "Service",
        on_delete=models.CASCADE,
        related_name="group_memberships",
    )
    professional = models.ForeignKey(
        "Professional",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="group_memberships",
    )
    schedule = models.ForeignKey(
        "GroupSchedule",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="memberships",
    )
    family_key = models.CharField(max_length=255, db_index=True)
    class_name = models.CharField(max_length=150, blank=True, default="")
    weekdays = models.CharField(
        max_length=32,
        blank=True,
        default="",
        help_text="Dias da semana permitidos (0-6), separados por vírgula.",
    )
    monthly_price_override = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Se definido, substitui o preço mensal base do serviço para este inscrito.",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("client", "family_key")
        ordering = ("-updated_at", "-id")
        verbose_name = "Plano de inscrito em turma"
        verbose_name_plural = "Planos de inscritos em turma"

    def weekday_values(self):
        values = []
        for item in (self.weekdays or "").split(","):
            item = item.strip()
            if not item.isdigit():
                continue
            number = int(item)
            if 0 <= number <= 6 and number not in values:
                values.append(number)
        return values

    def __str__(self):
        label = self.class_name or self.service.name
        return f"{label} · {self.client}"


class GroupMonthlyCharge(models.Model):
    STATUS_UNPAID = "unpaid"
    STATUS_PAID = "paid"
    STATUS_VOID = "void"

    STATUS_CHOICES = (
        (STATUS_UNPAID, "Em dívida"),
        (STATUS_PAID, "Paga"),
        (STATUS_VOID, "Anulada"),
    )

    DISCOUNT_CHOICES = (
        ("none", "Sem desconto"),
        ("percent", "Percentagem"),
        ("fixed", "Valor fixo"),
    )

    client = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="group_monthly_charges",
    )
    service = models.ForeignKey(
        "Service",
        on_delete=models.CASCADE,
        related_name="group_monthly_charges",
    )
    professional = models.ForeignKey(
        "Professional",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="group_monthly_charges",
    )
    schedule = models.ForeignKey(
        "GroupSchedule",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="monthly_charges",
    )
    family_key = models.CharField(max_length=255, db_index=True)
    class_name = models.CharField(max_length=150, blank=True, default="")
    month = models.DateField(help_text="Primeiro dia do mês de referência.")

    base_price = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    partner = models.ForeignKey(
        "Partner",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="group_monthly_charges",
    )
    partner_price = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    discount_type = models.CharField(max_length=20, choices=DISCOUNT_CHOICES, default="none")
    discount_value = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    discount_applied = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    final_price = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_UNPAID)
    paid_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("client", "family_key", "month")
        ordering = ("-month", "-id")
        verbose_name = "Mensalidade de turma"
        verbose_name_plural = "Mensalidades de turma"

    def __str__(self):
        label = self.class_name or self.service.name
        return f"{label} · {self.client} · {self.month:%Y-%m}"

    def get_charge_amount(self):
        return self.final_price or Decimal("0.00")

    def get_paid_amount(self):
        total = (
            self.payment_allocations.filter(payment__status=ClientPayment.STATUS_POSTED)
            .aggregate(total=models.Sum("allocated_amount"))
            .get("total")
        )
        if total:
            return total
        if self.status == self.STATUS_PAID:
            return self.get_charge_amount()
        return Decimal("0.00")

    def get_outstanding_amount(self):
        remaining = self.get_charge_amount() - self.get_paid_amount()
        return remaining if remaining > 0 else Decimal("0.00")

    @property
    def charge_amount(self):
        return self.get_charge_amount()

    @property
    def paid_amount(self):
        return self.get_paid_amount()

    @property
    def outstanding_amount(self):
        return self.get_outstanding_amount()


class ClientPayment(models.Model):
    STATUS_POSTED = "posted"
    STATUS_VOID = "void"
    STATUS_CHOICES = (
        (STATUS_POSTED, "Registado"),
        (STATUS_VOID, "Anulado"),
    )

    METHOD_CASH = "cash"
    METHOD_CARD = "card"
    METHOD_MBWAY = "mbway"
    METHOD_TRANSFER = "transfer"
    METHOD_OTHER = "other"
    PAYMENT_METHOD_CHOICES = (
        (METHOD_CASH, "Numerário"),
        (METHOD_CARD, "Multibanco"),
        (METHOD_MBWAY, "MB Way"),
        (METHOD_TRANSFER, "Transferência"),
        (METHOD_OTHER, "Outro"),
    )

    MOLONI_SYNC_PENDING = "pending"
    MOLONI_SYNC_SYNCED = "synced"
    MOLONI_SYNC_ERROR = "error"
    MOLONI_SYNC_SKIPPED = "skipped"
    MOLONI_SYNC_CHOICES = (
        (MOLONI_SYNC_PENDING, "Pendente"),
        (MOLONI_SYNC_SYNCED, "Sincronizado"),
        (MOLONI_SYNC_ERROR, "Erro"),
        (MOLONI_SYNC_SKIPPED, "Ignorado"),
    )

    client_profile = models.ForeignKey(
        "ClientProfile",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="client_payments",
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_POSTED, db_index=True)
    payment_method = models.CharField(max_length=20, choices=PAYMENT_METHOD_CHOICES, default=METHOD_CASH, db_index=True)
    amount_received = models.DecimalField(max_digits=10, decimal_places=2)
    received_at = models.DateTimeField(default=timezone.now, db_index=True)
    reference = models.CharField(max_length=255, blank=True, default="")
    notes = models.TextField(blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="client_payments_created",
    )
    cash_movement = models.OneToOneField(
        "CashMovement",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="client_payment",
    )
    void_reason = models.CharField(max_length=255, blank=True, default="")
    voided_at = models.DateTimeField(null=True, blank=True)
    voided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="client_payments_voided",
    )
    moloni_sync_status = models.CharField(
        max_length=20,
        choices=MOLONI_SYNC_CHOICES,
        default=MOLONI_SYNC_PENDING,
        db_index=True,
    )
    moloni_document_id = models.CharField(max_length=50, blank=True, default="")
    moloni_document_number = models.CharField(max_length=100, blank=True, default="")
    moloni_sync_error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-received_at", "-id")
        verbose_name = "Pagamento de cliente"
        verbose_name_plural = "Pagamentos de clientes"

    def __str__(self):
        client_name = self.client_profile.full_name if self.client_profile else "Sem cliente"
        return f"{client_name} · {self.amount_received} · {self.received_at:%d/%m/%Y %H:%M}"

    def clean(self):
        super().clean()
        if self.amount_received is None or self.amount_received <= 0:
            raise ValidationError({"amount_received": "Indica um valor positivo."})
        if self.status == self.STATUS_VOID and not self.voided_at:
            raise ValidationError({"voided_at": "Indica quando o pagamento foi anulado."})
        if self.cash_movement_id and self.cash_movement.movement_type != CashMovement.TYPE_IN:
            raise ValidationError({"cash_movement": "O movimento de caixa associado ao pagamento tem de ser uma entrada."})

    def get_allocated_amount(self):
        total = self.allocations.aggregate(total=models.Sum("allocated_amount")).get("total")
        return total or Decimal("0.00")

    def get_unallocated_amount(self):
        remaining = (self.amount_received or Decimal("0.00")) - self.get_allocated_amount()
        return remaining if remaining > 0 else Decimal("0.00")

    @property
    def allocated_amount(self):
        return self.get_allocated_amount()

    @property
    def unallocated_amount(self):
        return self.get_unallocated_amount()


class ClientPaymentAllocation(models.Model):
    payment = models.ForeignKey(
        "ClientPayment",
        on_delete=models.CASCADE,
        related_name="allocations",
    )
    appointment = models.ForeignKey(
        "Appointment",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="payment_allocations",
    )
    group_monthly_charge = models.ForeignKey(
        "GroupMonthlyCharge",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="payment_allocations",
    )
    allocated_amount = models.DecimalField(max_digits=10, decimal_places=2)
    notes = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at", "-id")
        verbose_name = "Afetação de pagamento"
        verbose_name_plural = "Afetações de pagamentos"
        unique_together = (
            ("payment", "appointment"),
            ("payment", "group_monthly_charge"),
        )

    def __str__(self):
        target = self.appointment or self.group_monthly_charge
        return f"{self.payment_id} · {target or '-'} · {self.allocated_amount}"

    def clean(self):
        super().clean()
        has_appointment = bool(self.appointment_id)
        has_group_monthly = bool(self.group_monthly_charge_id)
        if has_appointment == has_group_monthly:
            raise ValidationError("Seleciona exatamente um destino para a afetação.")
        if self.allocated_amount is None or self.allocated_amount <= 0:
            raise ValidationError({"allocated_amount": "Indica um valor positivo."})
        if not self.payment_id:
            return

        payment_profile_id = self.payment.client_profile_id
        if has_appointment:
            appointment_profile = getattr(getattr(self.appointment.client, "client_profile", None), "id", None)
            if payment_profile_id and appointment_profile and payment_profile_id != appointment_profile:
                raise ValidationError({"appointment": "A marcação selecionada não pertence ao cliente deste pagamento."})
        elif has_group_monthly:
            monthly_profile = getattr(getattr(self.group_monthly_charge.client, "client_profile", None), "id", None)
            if payment_profile_id and monthly_profile and payment_profile_id != monthly_profile:
                raise ValidationError({"group_monthly_charge": "A mensalidade selecionada não pertence ao cliente deste pagamento."})


class ProductCategory(models.Model):
    name = models.CharField(max_length=120, unique=True)

    class Meta:
        verbose_name = "Categoria de produto"
        verbose_name_plural = "Categorias de produto"
        ordering = ("name",)

    def __str__(self):
        return self.name


class Product(models.Model):
    UNIT_UNIT = "unit"
    UNIT_ML = "ml"
    UNIT_G = "g"

    UNIT_CHOICES = (
        (UNIT_UNIT, "Unidades"),
        (UNIT_ML, "ml"),
        (UNIT_G, "g"),
    )

    name = models.CharField(max_length=200)
    sku = models.CharField(max_length=64, blank=True)
    category = models.ForeignKey(
        ProductCategory,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="products",
    )
    is_active = models.BooleanField(default=True)
    unit_base = models.CharField(max_length=10, choices=UNIT_CHOICES, default=UNIT_UNIT)
    min_stock_alert = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    unit_per_pack = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Quantidade na unidade base que existe numa embalagem.",
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("name",)
        verbose_name = "Produto"
        verbose_name_plural = "Produtos"

    def __str__(self):
        return self.name


class StockLocation(models.Model):
    name = models.CharField(max_length=120, unique=True)

    class Meta:
        ordering = ("name",)
        verbose_name = "Local de stock"
        verbose_name_plural = "Locais de stock"

    def __str__(self):
        return self.name


class StockMovement(models.Model):
    TYPE_PURCHASE = "purchase"
    TYPE_CONSUMPTION = "consumption"
    TYPE_ADJUSTMENT = "adjustment"
    TYPE_TRANSFER = "transfer"

    TYPE_CHOICES = (
        (TYPE_PURCHASE, "Entrada"),
        (TYPE_CONSUMPTION, "Consumo"),
        (TYPE_ADJUSTMENT, "Ajuste"),
        (TYPE_TRANSFER, "Transferência"),
    )

    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name="movements",
    )
    location = models.ForeignKey(
        StockLocation,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="movements",
    )
    movement_type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    quantity_base = models.DecimalField(max_digits=12, decimal_places=2)
    unit_cost = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    total_cost = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    appointment = models.ForeignKey(
        "Appointment",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="stock_movements",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="stock_movements",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    note = models.TextField(blank=True)
    is_void = models.BooleanField(default=False)

    class Meta:
        ordering = ("-created_at", "-id")
        verbose_name = "Movimento de stock"
        verbose_name_plural = "Movimentos de stock"

    def save(self, *args, **kwargs):
        if self.unit_cost is not None and self.total_cost is None:
            self.total_cost = (self.unit_cost or Decimal("0.00")) * (self.quantity_base or Decimal("0.00"))
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.product} · {self.movement_type} · {self.quantity_base}"


class AppointmentConsumption(models.Model):
    appointment = models.ForeignKey(
        "Appointment",
        on_delete=models.CASCADE,
        related_name="consumptions",
    )
    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name="consumptions",
    )
    quantity_base = models.DecimalField(max_digits=12, decimal_places=2)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="appointment_consumptions",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        verbose_name = "Consumo em marcação"
        verbose_name_plural = "Consumos em marcações"

    def __str__(self):
        return f"{self.appointment_id} · {self.product} · {self.quantity_base}"


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
    notify_client_on_new_booking = models.BooleanField(default=True, verbose_name="Notificar utente quando a clínica cria marcação")
    notify_password_reset = models.BooleanField(default=True, verbose_name="Enviar emails de recuperação de password")
    group_cancel_hours = models.PositiveIntegerField(default=2, verbose_name="Horas mínimas para cancelamento de turma")

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
