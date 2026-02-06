from django.contrib import admin
from django.utils.html import format_html
from django.utils import timezone

from .forms import BackofficeServiceForm
from .models import (
    Service,
    Partner,
    PartnerServicePrice,
    Professional,
    Availability,
    Appointment,
    ClientProfile,
    ClinicalRecord,
    TreatmentRecord,
    BlockedSlot,
    ClinicSettings,
    EmailLog,
    GroupSession,
    GroupEnrollment,
    ContentPost,
)


# --------- INLINES (para reduzir cliques) ---------

class AvailabilityInline(admin.TabularInline):
    model = Availability
    extra = 0
    fields = ("weekday", "start_time", "end_time")
    ordering = ("weekday", "start_time")


class TreatmentInline(admin.TabularInline):
    model = TreatmentRecord
    extra = 0
    fields = ("date", "time", "service_name", "professional", "notes")
    readonly_fields = ()
    ordering = ("-date", "-time")


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
        return request.user.is_superuser


# --------- PROFISSIONAIS ---------

@admin.register(Professional)
class ProfessionalAdmin(admin.ModelAdmin):
    list_display = ("user", "speciality", "services_list")
    search_fields = ("user__username", "user__first_name", "user__last_name", "speciality")
    ordering = ("user__username",)
    inlines = (AvailabilityInline,)
    autocomplete_fields = ("user",)
    filter_horizontal = ("services",)

    def services_list(self, obj):
        return ", ".join(obj.services.values_list("name", flat=True))

    services_list.short_description = "Serviços"

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser


# --------- DISPONIBILIDADES ---------

@admin.register(Availability)
class AvailabilityAdmin(admin.ModelAdmin):
    list_display = ("professional", "weekday", "start_time", "end_time")
    list_filter = ("professional", "weekday")
    ordering = ("professional", "weekday", "start_time")
    autocomplete_fields = ("professional",)

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser


# --------- CLIENTES ---------

@admin.register(ClientProfile)
class ClientProfileAdmin(admin.ModelAdmin):
    list_display = ("full_name", "phone", "city", "postal_code", "partner", "user", "registration_status")
    list_filter = ("registration_status", "city", "partner")
    search_fields = ("full_name", "phone", "nif", "user__email", "user__username")
    ordering = ("full_name",)
    autocomplete_fields = ("user",)
    inlines = (TreatmentInline,)

    fieldsets = (
        ("Identificação", {"fields": ("user", "full_name", "nif")}),
        ("Contacto", {"fields": ("phone", "city")}),
        ("Parceria / Descontos", {"fields": ("partner", "discount_type", "discount_percent", "discount_amount", "discount_label")}),
        ("Registo", {"fields": ("registration_status", "registration_requested_at", "registration_reviewed_at", "registration_reviewed_by")}),
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
        return request.user.is_superuser


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
        return request.user.is_superuser

    def get_actions(self, request):
        actions = super().get_actions(request)
        # Remove ação de apagar em massa (para não haver acidentes)
        if not request.user.is_superuser and "delete_selected" in actions:
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
        return request.user.is_superuser

    def get_actions(self, request):
        actions = super().get_actions(request)
        # Remove ação "Delete selected" para staff (evita acidentes)
        if not request.user.is_superuser and "delete_selected" in actions:
            del actions["delete_selected"]
        return actions


# --------- MARCAÇÕES ---------

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
class GroupSessionAdmin(admin.ModelAdmin):
    list_display = ("date", "time", "service", "professional", "capacity", "status", "spots_left_admin")
    list_filter = ("status", "service", "professional", "date")
    search_fields = ("service__name", "professional__user__username")
    ordering = ("-date", "-time")
    autocomplete_fields = ("service", "professional")

    def spots_left_admin(self, obj):
        return obj.spots_left

    spots_left_admin.short_description = "Vagas"


@admin.register(GroupEnrollment)
class GroupEnrollmentAdmin(admin.ModelAdmin):
    list_display = ("session", "client", "status", "created_at")
    list_filter = ("status", "session__service", "session__date")
    search_fields = ("client__username", "client__client_profile__full_name", "session__service__name")
    ordering = ("-created_at",)
    autocomplete_fields = ("session", "client")


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
                    "notify_client_on_clinic_changes",
                    "notify_professional_on_new_booking",
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
