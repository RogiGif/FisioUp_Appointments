from datetime import time, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from django.urls import reverse

from core.models import (
    Appointment,
    ClientProfile,
    Professional,
    Service,
    WeeklySchedule,
    WeeklyWorkingBlock,
    WeeklyBreakBlock,
)
from core.views.common import _get_slots
from core.session_timeout import get_session_timeout_config


User = get_user_model()


def _next_weekday(start_date, weekday):
    delta = (weekday - start_date.weekday()) % 7
    if delta == 0:
        delta = 7
    return start_date + timedelta(days=delta)


class WeeklyScheduleSlotsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="prof1", password="pass")
        self.prof = Professional.objects.create(user=self.user)

    def test_weekly_schedule_with_breaks_generates_slots(self):
        schedule = WeeklySchedule.objects.create(professional=self.prof, timezone="Europe/Lisbon", is_active=True)
        WeeklyWorkingBlock.objects.create(
            weekly_schedule=schedule,
            weekday=0,
            start_time=time(9, 0),
            end_time=time(17, 0),
        )
        WeeklyBreakBlock.objects.create(
            weekly_schedule=schedule,
            weekday=0,
            start_time=time(13, 0),
            end_time=time(14, 0),
        )

        target_date = _next_weekday(timezone.localdate(), 0)
        slots = _get_slots(self.prof, target_date, step_minutes=60)
        self.assertEqual(
            slots,
            ["09:00", "10:00", "11:00", "12:00", "14:00", "15:00", "16:00"],
        )

    def test_sunday_without_blocks_has_no_slots(self):
        schedule = WeeklySchedule.objects.create(professional=self.prof, timezone="Europe/Lisbon", is_active=True)
        WeeklyWorkingBlock.objects.create(
            weekly_schedule=schedule,
            weekday=0,
            start_time=time(9, 0),
            end_time=time(12, 0),
        )
        target_date = _next_weekday(timezone.localdate(), 6)
        slots = _get_slots(self.prof, target_date, step_minutes=60)
        self.assertEqual(slots, [])

    def test_no_schedule_has_no_slots(self):
        target_date = _next_weekday(timezone.localdate(), 2)
        slots = _get_slots(self.prof, target_date, step_minutes=60)
        self.assertEqual(slots, [])


class SeriesBookingStatusTests(TestCase):
    def setUp(self):
        self.service = Service.objects.create(
            name="Fisioterapia",
            duration_minutes=60,
            price="50.00",
            service_type="one_to_one",
        )
        self.target_date = _next_weekday(timezone.localdate(), 0)

        self.prof_user = User.objects.create_user(username="prof_series", password="pass")
        self.prof = Professional.objects.create(user=self.prof_user)
        self.prof.services.add(self.service)

        schedule = WeeklySchedule.objects.create(professional=self.prof, timezone="Europe/Lisbon", is_active=True)
        WeeklyWorkingBlock.objects.create(
            weekly_schedule=schedule,
            weekday=0,
            start_time=time(9, 0),
            end_time=time(12, 0),
        )

    def _series_confirm_payload(self):
        return {
            "mode": "serie",
            "action": "confirm",
            "service_id": str(self.service.id),
            "freq": "weekly",
            "session_date": [self.target_date.isoformat()],
            "session_professional_id": [str(self.prof.id)],
            "session_time": ["09:00"],
        }

    def test_client_series_booking_creates_pending_appointment(self):
        client_user = User.objects.create_user(username="client_series", password="pass")
        ClientProfile.objects.create(
            user=client_user,
            full_name="Cliente Série",
            phone="+351912345678",
            address_line1="Rua A",
            postal_code="1000-100",
            city="Lisboa",
        )

        self.client.force_login(client_user)
        response = self.client.post("/marcar/", data=self._series_confirm_payload())

        self.assertEqual(response.status_code, 302)
        appt = Appointment.objects.get(client=client_user, service=self.service)
        self.assertEqual(appt.status, Appointment.STATUS_PENDING)

    def test_professional_series_booking_creates_scheduled_appointment(self):
        client_user = User.objects.create_user(username="target_client_series", password="pass")
        client_profile = ClientProfile.objects.create(
            user=client_user,
            full_name="Cliente Alvo",
            phone="+351923456789",
            address_line1="Rua B",
            postal_code="2000-200",
            city="Porto",
        )

        payload = self._series_confirm_payload()
        payload["client_profile_id"] = str(client_profile.id)
        payload["client_id"] = str(client_user.id)

        self.client.force_login(self.prof_user)
        response = self.client.post("/prof/marcar/", data=payload)

        self.assertEqual(response.status_code, 302)
        appt = Appointment.objects.get(client=client_user, service=self.service)
        self.assertEqual(appt.status, Appointment.STATUS_SCHEDULED)


@override_settings(
    INTERNAL_SESSION_TIMEOUT_SECONDS=8,
    CLIENT_SESSION_TIMEOUT_SECONDS=4,
    INTERNAL_SESSION_WARNING_SECONDS=3,
    CLIENT_SESSION_WARNING_SECONDS=2,
    SESSION_KEEPALIVE_INTERVAL_SECONDS=2,
)
class SessionTimeoutTests(TestCase):
    def setUp(self):
        self.internal_user = User.objects.create_superuser(
            username="admin_timeout",
            email="admin@example.com",
            password="pass",
        )
        self.client_user = User.objects.create_user(
            username="client_timeout",
            email="client@example.com",
            password="pass",
        )
        ClientProfile.objects.create(
            user=self.client_user,
            full_name="Cliente Timeout",
            phone="+351900000000",
            address_line1="Rua C",
            postal_code="3000-300",
            city="Coimbra",
        )

    def test_internal_timeout_config_is_4_hours_family_with_warning(self):
        config = get_session_timeout_config(self.internal_user)
        self.assertTrue(config["enabled"])
        self.assertTrue(config["is_internal"])
        self.assertEqual(config["timeout_seconds"], 8)
        self.assertEqual(config["warning_seconds"], 3)

    def test_client_timeout_config_is_1_hour_family_with_warning(self):
        config = get_session_timeout_config(self.client_user)
        self.assertTrue(config["enabled"])
        self.assertFalse(config["is_internal"])
        self.assertEqual(config["timeout_seconds"], 4)
        self.assertEqual(config["warning_seconds"], 2)

    def test_keepalive_endpoint_requires_login_and_refreshes_session(self):
        self.client.force_login(self.client_user)
        response = self.client.post(reverse("session_keepalive"))
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["timeout_seconds"], 4)
        self.assertEqual(payload["warning_seconds"], 2)
        self.assertEqual(payload["keepalive_interval_seconds"], 2)

        session = self.client.session
        self.assertIn("_last_activity_ts", session)

    def test_expired_client_session_redirects_to_login(self):
        self.client.force_login(self.client_user)
        session = self.client.session
        session["_last_activity_ts"] = int(timezone.now().timestamp()) - 10
        session.save()

        response = self.client.get(reverse("profile"))

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/?next=%2Fperfil%2F", response.url)

    def test_expired_ajax_request_returns_401_with_login_url(self):
        self.client.force_login(self.client_user)
        session = self.client.session
        session["_last_activity_ts"] = int(timezone.now().timestamp()) - 10
        session.save()

        response = self.client.get(
            reverse("profile"),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 401)
        payload = response.json()
        self.assertTrue(payload["expired"])
        self.assertIn("/login/?next=%2Fperfil%2F", payload["login_url"])

    def test_authenticated_page_injects_warning_config(self):
        self.client.force_login(self.client_user)
        response = self.client.get(reverse("profile"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "session-timeout-config")
        self.assertContains(response, '"warning_seconds": 2')
