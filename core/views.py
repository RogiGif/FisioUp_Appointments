from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from datetime import datetime, timedelta, time as dtime
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.shortcuts import render, redirect
from .models import Professional, Availability, Appointment, Service


def _time_range(start: dtime, end: dtime, step_minutes: int):
    current = datetime.combine(datetime.today().date(), start)
    end_dt = datetime.combine(datetime.today().date(), end)
    step = timedelta(minutes=step_minutes)
    while current < end_dt:
        yield current.time().replace(second=0, microsecond=0)
        current += step


def _get_slots(prof: Professional, date_obj, step_minutes: int):
    weekday = date_obj.weekday()
    avails = Availability.objects.filter(professional=prof, weekday=weekday)

    taken = set(
        Appointment.objects.filter(professional=prof, date=date_obj)
        .values_list("time", flat=True)
    )

    slots = []
    for a in avails:
        for t in _time_range(a.start_time, a.end_time, step_minutes=step_minutes):
            if t not in taken:
                slots.append(t.strftime("%H:%M"))
    return slots

def login_view(request):
    message = ""
    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "").strip()

        user = authenticate(request, username=username, password=password)
        if user is not None:
            login(request, user)
            return redirect("/marcar/")
        message = "Credenciais inválidas."

    return render(request, "core/login.html", {"message": message})


def logout_view(request):
    logout(request)
    return redirect("/login/")


@login_required(login_url="/login/")
def book_view(request):
    professionals = Professional.objects.select_related("user").all().order_by("user__username")
    services = Service.objects.all().order_by("name")
    message = ""

    selected_service_id = request.GET.get("service_id") or ""
    selected_professional_id = request.GET.get("professional_id") or ""
    selected_date = request.GET.get("date") or ""

    slots = []

    # GET: mostrar horários
    if selected_service_id and selected_professional_id and selected_date:
        service = Service.objects.get(id=selected_service_id)
        prof = Professional.objects.get(id=selected_professional_id)
        date_obj = datetime.strptime(selected_date, "%Y-%m-%d").date()
        slots = _get_slots(prof, date_obj, step_minutes=service.duration_minutes)

    # POST: criar marcação
    if request.method == "POST":
        service_id = request.POST.get("service_id")
        professional_id = request.POST.get("professional_id")
        date_str = request.POST.get("date")
        time_str = request.POST.get("time")
        notes = request.POST.get("notes", "").strip()

        if not (service_id and professional_id and date_str and time_str):
            message = "Dados incompletos."
        else:
            service = Service.objects.get(id=service_id)
            prof = Professional.objects.get(id=professional_id)
            date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
            time_obj = datetime.strptime(time_str, "%H:%M").time()

            client = request.user


            slots_now = _get_slots(prof, date_obj, step_minutes=service.duration_minutes)
            if time_str not in slots_now:
                message = "Esse horário já não está disponível. Atualiza a página."
                selected_service_id = str(service.id)
                selected_professional_id = str(prof.id)
                selected_date = date_str
                slots = slots_now
            else:
                try:
                    with transaction.atomic():
                        Appointment.objects.create(
                            client=client,
                            professional=prof,
                            service=service,
                            date=date_obj,
                            time=time_obj,
                            notes=notes,
                        )
                    return redirect(
                        f"/marcar/?service_id={service.id}&professional_id={prof.id}&date={date_str}"
                    )
                except IntegrityError:
                    message = "Esse horário acabou de ser reservado por outra pessoa. Escolhe outro."
                    selected_service_id = str(service.id)
                    selected_professional_id = str(prof.id)
                    selected_date = date_str
                    slots = _get_slots(prof, date_obj, step_minutes=service.duration_minutes)

    return render(
        request,
        "core/book.html",
        {
            "professionals": professionals,
            "services": services,
            "selected_service_id": selected_service_id,
            "selected_professional_id": selected_professional_id,
            "selected_date": selected_date,
            "slots": slots,
            "message": message,
        },
    )
