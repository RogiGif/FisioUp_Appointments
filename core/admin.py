from django.contrib import admin
from django.utils.html import format_html
from django.utils import timezone

from .forms import BackofficeServiceForm
from .models import (
    Service,
    Partner,
    PartnerServicePrice,
    Professional,
    WeeklySchedule,
    WeeklyWorkingBlock,
    WeeklyBreakBlock,
    Appointment,
    SubcontractorPaymentLine,
    ClientProfile,
    ClinicalRecord,
    TreatmentRecord,
    BlockedSlot,
    ClinicSettings,
    EmailLog,
    GroupSchedule,
    GroupSession,
    GroupEnrollment,
    ProductCategory,
    Product,
    StockLocation,
    StockMovement,
    AppointmentConsumption,
    ContentPost,
    AuditLog,
    CashSession,
    CashMovement,
)
from .views.common import ensure_group_sessions_for_schedules
from .permissions import can_access_backoffice, is_admin_role

def _can_hard_delete(user):
    return user.is_superuser or is_admin_role(user)


class BackofficeAccessAdminMixin:
    def _has_backoffice_access(self, request):
        return can_access_backoffice(request.user) or is_admin_role(request.user)

    def has_module_permission(self, request):
        if self._has_backoffice_access(request):
            return True
        return super().has_module_permission(request)

    def has_view_permission(self, request, obj=None):
        if self._has_backoffice_access(request):
            return True
        return super().has_view_permission(request, obj=obj)

    def has_add_permission(self, request):
        if self._has_backoffice_access(request):
            return True
        return super().has_add_permission(request)

    def has_change_permission(self, request, obj=None):
        if self._has_backoffice_access(request):
            return True
        return super().has_change_permission(request, obj=obj)

    def has_delete_permission(self, request, obj=None):
        if self._has_backoffice_access(request):
            return True
        return super().has_delete_permission(request, obj=obj)


# --------- INLINES (para reduzir cliques) ---------

class WeeklyWorkingBlockInline(admin.TabularInline):
    model = WeeklyWorkingBlock
    extra = 0
    fields = ("weekday", "start_time", "end_time", "location")
    ordering = ("weekday", "start_time")


class WeeklyBreakBlockInline(admin.TabularInline):
    model = WeeklyBreakBlock
    extra = 0
    fields = ("weekday", "start_time", "end_time")
    ordering = ("weekday", "start_time")


class TreatmentInline(admin.TabularInline):
    model = TreatmentRecord
    extra = 0
    fields = ("date", "time", "service_name", "professional", "notes")
    readonly_fields = ()
    ordering = ("-date", "-time")


class GroupEnrollmentInline(admin.TabularInline):
    model = GroupEnrollment
    extra = 0
    fields = ("client", "status", "created_at")
    readonly_fields = ("created_at",)
    autocomplete_fields = ("client",)


# --------- PARCERIAS (INLINE PREÇOS POR SERVIÇO) ---------

class PartnerServicePriceInline(admin.TabularInline):
    model = PartnerServicePrice
    extra = 0
    fields = ("service", "pricing_mode", "price", "price_first", "price_followup")
    autocomplete_fields = ("service",)


# --------- SERVIÇOS ---------

@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    form = BackofficeServiceForm
    list_display = ("name", "service_type", "pricing_mode", "price", "price_first", "price_followup", "duration_minutes", "capacity")
    search_fields = ("name",)
    ordering = ("name",)

    class Media:
        js = ("core/js/admin_service_pricing.js",)

    def has_delete_permission(self, request, obj=None):
        # produção: evitar apagar serviços sem querer
        return _can_hard_delete(request.user)


# --------- PROFISSIONAIS ---------

@admin.register(Professional)
class ProfessionalAdmin(admin.ModelAdmin):
    list_display = ("user", "speciality", "is_independent", "subcontract_percentage", "services_list")
    search_fields = ("user__username", "user__first_name", "user__last_name", "speciality")
    ordering = ("user__username",)
    inlines = ()
    autocomplete_fields = ("user",)
    filter_horizontal = ("services",)
    list_filter = ("is_independent",)

    def services_list(self, obj):
        return ", ".join(obj.services.values_list("name", flat=True))

    services_list.short_description = "Serviços"

    def has_delete_permission(self, request, obj=None):
        return _can_hard_delete(request.user)


@admin.register(WeeklySchedule)
class WeeklyScheduleAdmin(BackofficeAccessAdminMixin, admin.ModelAdmin):
    list_display = ("professional", "timezone", "is_active", "updated_at")
    list_filter = ("is_active", "timezone")
    ordering = ("professional__user__username",)
    autocomplete_fields = ("professional",)
    inlines = (WeeklyWorkingBlockInline, WeeklyBreakBlockInline)

    def has_delete_permission(self, request, obj=None):
        return _can_hard_delete(request.user)


# --------- CLIENTES ---------

@admin.register(ClientProfile)
class ClientProfileAdmin(admin.ModelAdmin):
    list_display = ("full_name", "phone", "city", "postal_code", "partner", "user", "registration_status", "accepted_terms_at")
    list_filter = ("registration_status", "city", "partner")
    search_fields = ("full_name", "phone", "nif", "user__email", "user__username")
    ordering = ("full_name",)
    autocomplete_fields = ("user",)
    inlines = (TreatmentInline,)
    readonly_fields = ("accepted_terms_at", "accepted_terms_ip", "accepted_terms_user_agent")

    fieldsets = (
        ("Identificação", {"fields": ("user", "full_name", "nif")}),
        ("Contacto", {"fields": ("phone", "city")}),
        ("Parceria / Descontos", {"fields": ("partner", "discount_type", "discount_percent", "discount_amount", "discount_label")}),
        ("Registo", {"fields": ("registration_status", "registration_requested_at", "registration_reviewed_at", "registration_reviewed_by")}),
        ("Consentimento GDPR", {"fields": ("accepted_terms_at", "accepted_terms_ip", "accepted_terms_user_agent"), "classes": ("collapse",)}),
        ("Morada (opcional)", {"fields": ("address_line1", "postal_code"), "classes": ("collapse",)}),
    )

    actions = ("approve_registrations", "reject_registrations")

    @admin.action(description="Aprovar registos selecionados")
    def approve_registrations(self, request, queryset):
        now = timezone.now()
        for profile in queryset:
            profile.registration_status = "approved"
            profile.registration_reviewed_at = now
            profile.registration_reviewed_by = request.user
            profile.save(update_fields=["registration_status", "registration_reviewed_at", "registration_reviewed_by"])
            if profile.user:
                profile.user.is_active = True
                profile.user.save(update_fields=["is_active"])

    @admin.action(description="Rejeitar registos selecionados")
    def reject_registrations(self, request, queryset):
        now = timezone.now()
        for profile in queryset:
            profile.registration_status = "rejected"
            profile.registration_reviewed_at = now
            profile.registration_reviewed_by = request.user
            profile.save(update_fields=["registration_status", "registration_reviewed_at", "registration_reviewed_by"])
            if profile.user:
                profile.user.is_active = False
                profile.user.save(update_fields=["is_active"])

    def has_delete_permission(self, request, obj=None):
        # apagar cliente apaga histórico (cascade). Só superuser.
        return _can_hard_delete(request.user)


# --------- FICHA CLÍNICA (RESUMO) ---------

@admin.register(ClinicalRecord)
class ClinicalRecordAdmin(admin.ModelAdmin):
    list_display = ("client", "updated_at", "updated_by")
    search_fields = ("client__full_name",)
    ordering = ("-updated_at",)
    autocomplete_fields = ("client", "updated_by")
    readonly_fields = ("updated_at",)

    fieldsets = (
        (None, {"fields": ("client",)}),
        ("Resumo", {"fields": ("allergies", "conditions", "notes")}),
        ("Auditoria", {"fields": ("updated_by", "updated_at"), "classes": ("collapse",)}),
    )

    def has_delete_permission(self, request, obj=None):
        # produção: não apagar ficha clínica
        return _can_hard_delete(request.user)

    def get_actions(self, request):
        actions = super().get_actions(request)
        # Remove ação de apagar em massa (para não haver acidentes)
        if not _can_hard_delete(request.user) and "delete_selected" in actions:
            del actions["delete_selected"]
        return actions


# --------- TRATAMENTOS ---------

@admin.register(TreatmentRecord)
class TreatmentRecordAdmin(admin.ModelAdmin):
    list_display = ("client", "date", "time", "service_name", "professional", "updated_at")
    list_filter = ("professional", "date")
    search_fields = ("client__full_name", "service_name", "professional__user__username")
    ordering = ("-date", "-time", "-id")
    autocomplete_fields = ("client", "professional", "created_by", "updated_by", "appointment")
    readonly_fields = ("created_at", "updated_at")

    fieldsets = (
        (None, {"fields": ("client", "professional", "appointment")}),
        ("Sessão", {"fields": ("service_name", "date", "time", "notes")}),
        ("Auditoria", {"fields": ("created_by", "updated_by", "created_at", "updated_at"), "classes": ("collapse",)}),
    )

    def has_delete_permission(self, request, obj=None):
        # produção: não apagar histórico de tratamentos
        return _can_hard_delete(request.user)

    def get_actions(self, request):
        actions = super().get_actions(request)
        # Remove ação "Delete selected" para staff (evita acidentes)
        if not _can_hard_delete(request.user) and "delete_selected" in actions:
            del actions["delete_selected"]
        return actions


# --------- MARCAÇÕES ---------

class SubcontractorPaymentInline(admin.StackedInline):
    model = SubcontractorPaymentLine
    extra = 0
    can_delete = False
    readonly_fields = (
        "professional",
        "client",
        "service",
        "appointment_date",
        "appointment_time",
        "gross_amount",
        "percentage",
        "payable_amount",
        "status",
        "paid_at",
        "paid_by",
        "payment_reference",
    )
    fields = readonly_fields


@admin.register(Appointment)
class AppointmentAdmin(admin.ModelAdmin):
    list_display = ("date", "time", "client", "professional", "service", "status", "session_index", "pricing_tier", "final_price", "open_client_record")
    list_filter = ("status", "service", "professional", "date", "partner", "pricing_tier")
    search_fields = (
        "client__username",
        "client__email",
        "client__client_profile__full_name",
        "professional__user__username",
        "service__name",
    )
    ordering = ("-date", "-time")
    date_hierarchy = "date"

    autocomplete_fields = ("client", "professional", "service")
    readonly_fields = ("open_client_record",)
    inlines = (SubcontractorPaymentInline,)

    fieldsets = (
        (None, {"fields": ("client", "professional", "service", "date", "time", "status")}),
        ("Financeiro", {"fields": ("base_price", "partner", "partner_price", "discount_type", "discount_value", "base_price_applied", "partner_price_applied", "discount_applied", "final_price", "session_index", "pricing_tier")}),
        ("Ações rápidas", {"fields": ("open_client_record",)}),
    )

    def open_client_record(self, obj):
        if not obj or not hasattr(obj, "client"):
            return "-"

        try:
            profile = obj.client.client_profile
        except Exception:
            return "-"

        if not profile:
            return "-"

        return format_html(
            '<a class="button" href="{}">Abrir ficha</a>',
            f"/admin/core/clientprofile/{profile.id}/change/"
        )

    def has_delete_permission(self, request, obj=None):
        return _can_hard_delete(request.user)


# --------- SUBCONTRATADOS ---------

@admin.register(SubcontractorPaymentLine)
class SubcontractorPaymentLineAdmin(BackofficeAccessAdminMixin, admin.ModelAdmin):
    list_display = (
        "appointment_date",
        "appointment_time",
        "professional",
        "client",
        "service",
        "gross_amount",
        "percentage",
        "payable_amount",
        "status",
        "paid_at",
    )
    list_filter = ("status", "professional", "service", "appointment_date")
    search_fields = (
        "professional__user__username",
        "professional__user__first_name",
        "professional__user__last_name",
        "client__full_name",
        "service__name",
    )
    ordering = ("-appointment_date", "-appointment_time")
    autocomplete_fields = ("appointment", "professional", "client", "service")
    readonly_fields = ("created_at", "updated_at")
    actions = ("mark_paid", "mark_unpaid")

    @admin.action(description="Marcar como pago")
    def mark_paid(self, request, queryset):
        now = timezone.now()
        queryset.update(status=SubcontractorPaymentLine.STATUS_PAID, paid_at=now, paid_by=request.user)

    @admin.action(description="Marcar como em aberto")
    def mark_unpaid(self, request, queryset):
        queryset.update(status=SubcontractorPaymentLine.STATUS_UNPAID, paid_at=None, paid_by=None)

# --------- CONTEÚDOS ---------

@admin.register(ContentPost)
class ContentPostAdmin(admin.ModelAdmin):
    list_display = ("title", "kind", "status", "is_featured", "published_at", "updated_at")
    list_filter = ("kind", "status", "is_featured")
    search_fields = ("title", "excerpt", "body")
    prepopulated_fields = {"slug": ("title",)}
    ordering = ("-is_featured", "-published_at", "-created_at")

    fieldsets = (
        ("Conteúdo", {"fields": ("title", "slug", "kind", "excerpt", "body")}),
        ("Media", {"fields": ("cover_image",)}),
        ("Publicação", {"fields": ("status", "published_at", "is_featured")}),
        ("Meta", {"fields": ("author", "created_at", "updated_at"), "classes": ("collapse",)}),
    )
    readonly_fields = ("created_at", "updated_at")

    def save_model(self, request, obj, form, change):
        if not obj.author_id:
            obj.author = request.user
        super().save_model(request, obj, form, change)


@admin.register(Partner)
class PartnerAdmin(admin.ModelAdmin):
    list_display = ("name", "active", "discount_type", "discount_percent", "discount_amount")
    list_filter = ("active",)
    search_fields = ("name",)
    ordering = ("name",)
    inlines = (PartnerServicePriceInline,)

@admin.register(GroupSession)
class GroupSessionAdmin(BackofficeAccessAdminMixin, admin.ModelAdmin):
    list_display = ("date", "time", "name", "service", "professional", "capacity", "status", "spots_left_admin", "schedule")
    list_filter = ("status", "service", "professional", "date")
    search_fields = ("name", "service__name", "professional__user__username", "professional__user__first_name", "professional__user__last_name")
    ordering = ("-date", "-time")
    autocomplete_fields = ("service", "professional", "schedule")
    inlines = (GroupEnrollmentInline,)
    readonly_fields = ("spots_left_admin", "created_at", "updated_at")

    fieldsets = (
        ("Turma", {"fields": ("name", "service", "professional", "schedule")}),
        ("Sessão", {"fields": ("date", "time", "duration_minutes", "capacity", "status", "spots_left_admin")}),
        ("Notas", {"fields": ("notes",)}),
        ("Auditoria", {"fields": ("created_at", "updated_at"), "classes": ("collapse",)}),
    )

    def spots_left_admin(self, obj):
        return obj.spots_left

    spots_left_admin.short_description = "Vagas"

    @admin.action(description="Cancelar sessões selecionadas")
    def cancel_sessions(self, request, queryset):
        queryset.update(status=GroupSession.STATUS_CANCELLED)

    @admin.action(description="Marcar sessões como concluídas")
    def complete_sessions(self, request, queryset):
        queryset.update(status=GroupSession.STATUS_COMPLETED)

    actions = ("cancel_sessions", "complete_sessions")


@admin.register(GroupSchedule)
class GroupScheduleAdmin(BackofficeAccessAdminMixin, admin.ModelAdmin):
    list_display = ("name", "service", "professional", "weekday", "time", "start_date", "capacity", "duration_minutes", "is_active")
    list_filter = ("service", "professional", "weekday", "is_active")
    search_fields = ("name", "service__name", "professional__user__username", "professional__user__first_name", "professional__user__last_name")
    ordering = ("service__name", "weekday", "time")
    autocomplete_fields = ("service", "professional")

    @admin.action(description="Gerar próximas sessões")
    def generate_sessions(self, request, queryset):
        ensure_group_sessions_for_schedules(schedules=queryset)

    @admin.action(description="Desativar recorrência")
    def deactivate_schedules(self, request, queryset):
        queryset.update(is_active=False)

    @admin.action(description="Desativar recorrência e cancelar futuras sessões")
    def deactivate_and_cancel(self, request, queryset):
        queryset.update(is_active=False)
        today = timezone.localdate()
        GroupSession.objects.filter(
            schedule__in=queryset,
            date__gte=today,
            status=GroupSession.STATUS_SCHEDULED,
        ).update(status=GroupSession.STATUS_CANCELLED)

    @admin.action(description="Apagar recorrência e TODAS as sessões")
    def delete_with_sessions(self, request, queryset):
        GroupSession.objects.filter(schedule__in=queryset).delete()
        queryset.delete()

    actions = ("generate_sessions", "deactivate_schedules", "deactivate_and_cancel", "delete_with_sessions")

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        ensure_group_sessions_for_schedules(schedules=GroupSchedule.objects.filter(id=obj.id))


@admin.register(GroupEnrollment)
class GroupEnrollmentAdmin(BackofficeAccessAdminMixin, admin.ModelAdmin):
    list_display = ("session", "client", "status", "created_at")
    list_filter = ("status", "session__service", "session__date")
    search_fields = ("client__username", "client__client_profile__full_name", "session__service__name")
    ordering = ("-created_at",)
    autocomplete_fields = ("session", "client")


# --------- STOCK / CONSUMÍVEIS ---------

@admin.register(ProductCategory)
class ProductCategoryAdmin(BackofficeAccessAdminMixin, admin.ModelAdmin):
    list_display = ("name",)
    search_fields = ("name",)
    ordering = ("name",)


@admin.register(Product)
class ProductAdmin(BackofficeAccessAdminMixin, admin.ModelAdmin):
    list_display = ("name", "sku", "category", "unit_base", "min_stock_alert", "is_active")
    list_filter = ("is_active", "unit_base", "category")
    search_fields = ("name", "sku")
    ordering = ("name",)


@admin.register(StockLocation)
class StockLocationAdmin(BackofficeAccessAdminMixin, admin.ModelAdmin):
    list_display = ("name",)
    search_fields = ("name",)
    ordering = ("name",)


@admin.register(StockMovement)
class StockMovementAdmin(BackofficeAccessAdminMixin, admin.ModelAdmin):
    list_display = ("product", "movement_type", "quantity_base", "created_at", "created_by", "is_void")
    list_filter = ("movement_type", "created_at", "product")
    search_fields = ("product__name", "note")
    ordering = ("-created_at",)
    autocomplete_fields = ("product", "appointment", "created_by", "location")

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AppointmentConsumption)
class AppointmentConsumptionAdmin(BackofficeAccessAdminMixin, admin.ModelAdmin):
    list_display = ("appointment", "product", "quantity_base", "created_at", "created_by")
    list_filter = ("product", "created_at")
    search_fields = ("appointment__id", "product__name")
    ordering = ("-created_at",)
    autocomplete_fields = ("appointment", "product", "created_by")


@admin.register(AuditLog)
class AuditLogAdmin(BackofficeAccessAdminMixin, admin.ModelAdmin):
    list_display = (
        "created_at",
        "category",
        "action",
        "source",
        "object_repr",
        "actor_display",
        "actor_role",
        "ip_address",
    )
    list_filter = ("category", "action", "source", "actor_role", "created_at")
    search_fields = ("object_repr", "message", "actor_display", "actor_email", "request_path")
    ordering = ("-created_at",)
    readonly_fields = (
        "created_at",
        "category",
        "action",
        "source",
        "actor",
        "actor_display",
        "actor_email",
        "actor_role",
        "content_type",
        "object_id",
        "object_repr",
        "message",
        "before",
        "after",
        "metadata",
        "ip_address",
        "user_agent",
        "request_path",
        "request_method",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(CashSession)
class CashSessionAdmin(BackofficeAccessAdminMixin, admin.ModelAdmin):
    list_display = (
        "session_date",
        "status",
        "opening_amount",
        "expected_cash_amount",
        "counted_cash_amount",
        "difference_amount",
        "opened_by",
        "closed_by",
    )
    list_filter = ("status", "session_date")
    search_fields = ("opened_by__username", "opened_by__first_name", "opened_by__last_name", "closed_by__username")
    ordering = ("-session_date", "-opened_at")
    autocomplete_fields = ("opened_by", "closed_by")


@admin.register(CashMovement)
class CashMovementAdmin(BackofficeAccessAdminMixin, admin.ModelAdmin):
    list_display = ("happened_at", "session", "movement_type", "source_type", "payment_method", "amount", "description", "client_profile", "is_void", "created_by")
    list_filter = ("movement_type", "source_type", "payment_method", "is_void", "session__session_date")
    search_fields = ("description", "notes", "appointment__id", "client_profile__full_name", "client_profile__nif")
    ordering = ("-happened_at", "-id")
    autocomplete_fields = ("session", "appointment", "client_profile", "created_by", "voided_by")


# --------- BLOQUEIOS ---------

@admin.register(BlockedSlot)
class BlockedSlotAdmin(admin.ModelAdmin):
    list_display = ("professional", "date", "time", "created_by", "created_at")
    list_filter = ("professional", "date")
    ordering = ("-date", "-time")
    autocomplete_fields = ("professional", "created_by")


# --------- CONFIGURAÇÃO CLÍNICA (SINGLETON) ---------

@admin.register(ClinicSettings)
class ClinicSettingsAdmin(admin.ModelAdmin):
    fieldsets = (
        ("Identidade da clínica", {"fields": ("clinic_name", "logo")}),
        (
            "Emails",
            {
                "fields": ("clinic_email", "notification_emails", "from_email", "reply_to_email"),
                "description": "Emails separados por vírgulas ou por linha. Ex: rececao@..., gerente@...",
            },
        ),
        (
            "Assinatura",
            {
                "fields": ("footer_text", "signature_text"),
            },
        ),
        (
            "Notificações",
            {
                "fields": (
                    "notify_admin_on_pending_registration",
                    "notify_clinic_on_new_booking",
                    "notify_clinic_on_client_reschedule",
                    "notify_clinic_on_client_cancel",
                    "notify_client_on_new_booking",
                    "notify_client_on_clinic_changes",
                    "notify_professional_on_new_booking",
                    "notify_password_reset",
                )
            },
        ),
    )

    def has_add_permission(self, request):
        if ClinicSettings.objects.exists():
            return False
        return True

    def changelist_view(self, request, extra_context=None):
        if ClinicSettings.objects.exists():
            obj = ClinicSettings.objects.first()
            return self.change_view(request, str(obj.pk))
        return super().changelist_view(request, extra_context=extra_context)


# --------- LOGS DE EMAIL ---------

@admin.register(EmailLog)
class EmailLogAdmin(admin.ModelAdmin):
    list_display = ("event", "subject", "to", "status", "created_at")
    list_filter = ("event", "status", "created_at")
    search_fields = ("subject", "to", "body_text", "body_html")
    ordering = ("-created_at",)
    readonly_fields = ("event", "to", "subject", "body_text", "body_html", "status", "error", "created_at")

    def has_module_permission(self, request):
        return request.user.is_superuser or request.user.groups.filter(name="ADMIN").exists()

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser or request.user.groups.filter(name="ADMIN").exists()

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser or request.user.groups.filter(name="ADMIN").exists()

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
