from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Appointment, ClientProfile, TreatmentRecord


@receiver(post_save, sender=Appointment)
def create_treatment_record_when_completed(sender, instance: Appointment, created, **kwargs):
    # Só quando fica completed
    if instance.status != "completed":
        return

    # precisa de user (client) e appointment
    if not instance.client_id:
        return

    # garante que o cliente tem profile (sem isto, o admin pode rebentar)
    profile, _ = ClientProfile.objects.get_or_create(user=instance.client)

    # criar/atualizar um TreatmentRecord 1-para-1 com a marcação
    tr, created_tr = TreatmentRecord.objects.get_or_create(
        appointment=instance,
        defaults={
            "client": profile,
            "professional": instance.professional,
            "service": instance.service,
            "service_name": instance.service.name if instance.service else "",
            "date": instance.date,
            "time": instance.time,
            "created_by": None,
            "updated_by": None,
        },
    )

    # se já existia, mantém sincronizado (caso mudem a marcação)
    if not created_tr:
        tr.client = profile
        tr.professional = instance.professional
        tr.service = instance.service
        tr.service_name = instance.service.name if instance.service else tr.service_name
        tr.date = instance.date
        tr.time = instance.time
        tr.save()