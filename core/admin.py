from django.contrib import admin
from .models import Service, Professional, Availability, Appointment


@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    list_display = ("name", "duration_minutes")
    search_fields = ("name",)


@admin.register(Professional)
class ProfessionalAdmin(admin.ModelAdmin):
    list_display = ("user", "speciality")
    search_fields = ("user__username", "user__first_name", "user__last_name", "speciality")


@admin.register(Availability)
class AvailabilityAdmin(admin.ModelAdmin):
    list_display = ("professional", "weekday", "start_time", "end_time")
    list_filter = ("professional", "weekday")


@admin.register(Appointment)
class AppointmentAdmin(admin.ModelAdmin):
    list_display = ("date", "time", "client", "professional", "service", "status")
    list_filter = ("status", "professional", "service", "date")
    search_fields = ("client__username", "client__first_name", "client__last_name", "notes")


# Register your models here.
