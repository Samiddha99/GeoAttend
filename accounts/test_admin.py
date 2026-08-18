"""
Every admin page opens, with a row in it.

Django's system checks catch a misspelled field name but not a `list_display`
callable that raises, an `autocomplete_fields` target whose search returns
nothing, or an inline whose `fk_name` is wrong on a model with two foreign keys
to the same parent. Those only surface when a page is rendered against real
data — which is what this does.
"""
import datetime as dt

from django.contrib import admin
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Institute, University, User


class AdminSmokeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.superuser = User.objects.create_superuser(
            email="root@example.com", password="Str0ngPass!23")

    def setUp(self):
        self.client.force_login(self.superuser)

    def test_every_registered_model_has_a_working_changelist(self):
        broken = []
        for model in admin.site._registry:
            if model._meta.app_label in ("auth", "contenttypes", "sessions"):
                continue
            url = reverse(f"admin:{model._meta.app_label}_"
                          f"{model._meta.model_name}_changelist")
            response = self.client.get(url)
            if response.status_code != 200:
                broken.append(f"{model.__name__} -> {response.status_code}")
        self.assertEqual(broken, [])

    def test_every_add_form_that_exists_renders(self):
        """
        Skips the read-only ones on purpose: `has_add_permission = False` is a
        deliberate 403, not a fault.
        """
        broken = []
        for model, model_admin in admin.site._registry.items():
            if model._meta.app_label in ("auth", "contenttypes", "sessions"):
                continue
            url = reverse(f"admin:{model._meta.app_label}_"
                          f"{model._meta.model_name}_add")
            response = self.client.get(url)
            if response.status_code not in (200, 403):
                broken.append(f"{model.__name__} -> {response.status_code}")
        self.assertEqual(broken, [])

    def test_the_new_university_pages_render_with_data(self):
        """
        A changelist over an empty table proves very little — the computed
        columns are never called. These have rows.
        """
        university = University.objects.create(
            name="Anna University", code="ANNA", email="a@u.edu")
        institute = Institute.objects.create(
            name="Acme", code="ACME", email="acme@i.edu",
            state="Kerala", district="Ernakulam", invited_by=university)
        from accounts.models import Discipline, InstituteAffiliation, UniversityDiscipline

        UniversityDiscipline.objects.create(university=university,
                                            discipline=Discipline.ENGG)
        InstituteAffiliation.objects.create(institute=institute,
                                            discipline=Discipline.ENGG,
                                            university=university)

        for name in ("university", "institute", "instituteaffiliation",
                     "universitydiscipline"):
            with self.subTest(model=name):
                response = self.client.get(
                    reverse(f"admin:accounts_{name}_changelist"))
                self.assertEqual(response.status_code, 200)
        # The computed column actually ran.
        page = self.client.get(reverse("admin:accounts_university_changelist"))
        self.assertContains(page, "Anna University")

    def test_a_template_changelist_renders_both_kinds_of_owner(self):
        """
        `owner_label` is computed because the two owner FKs are mutually
        exclusive. If it raised on either kind, the page would 500.
        """
        from notifications.models import WhatsAppTemplate

        university = University.objects.create(
            name="U", code="U", email="u@u.edu")
        institute = Institute.objects.create(
            name="I", code="I", email="i@i.edu")
        WhatsAppTemplate.objects.create(
            institute=institute, name="Theirs", twilio_name="theirs",
            audience=WhatsAppTemplate.Audience.STUDENT, body="x")
        WhatsAppTemplate.objects.create(
            university=university, name="Ours", twilio_name="ours",
            audience=WhatsAppTemplate.Audience.STUDENT, body="x")

        response = self.client.get(
            reverse("admin:notifications_whatsapptemplate_changelist"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Theirs")
        self.assertContains(response, "Ours")

    def test_the_attachment_inlines_pick_the_right_foreign_key(self):
        """
        AbsenceAttachment has two FKs to two different parents, and an inline
        on either has to name which one. Getting it wrong is a system-check
        error on one side and a silently empty inline on the other.
        """
        from attendance.admin import PlannedAttachmentInline, ReasonAttachmentInline

        self.assertEqual(ReasonAttachmentInline.fk_name, "reason")
        self.assertEqual(PlannedAttachmentInline.fk_name, "planned")

    def test_read_only_models_refuse_edits(self):
        """
        One-time codes, face vectors and verification tickets are evidence.
        The admin is for reading them, never for changing them.
        """
        from accounts.models import FaceSample, PhoneOTP
        from attendance.models import FaceVerifyTicket
        from feedback.models import FeedbackResponse

        for model in (PhoneOTP, FaceSample, FaceVerifyTicket, FeedbackResponse):
            with self.subTest(model=model.__name__):
                model_admin = admin.site._registry[model]
                request = type("R", (), {"user": self.superuser})()
                self.assertFalse(model_admin.has_add_permission(request))
                self.assertFalse(model_admin.has_change_permission(request))

    def test_a_feedback_response_cannot_be_searched_by_student(self):
        """
        The privacy line. Responses are stored against a student so the student
        can read their own back; making that searchable in the admin would turn
        a deliberate design into a lookup service.
        """
        from feedback.models import FeedbackResponse

        model_admin = admin.site._registry[FeedbackResponse]
        joined = " ".join(model_admin.search_fields)
        self.assertNotIn("student", joined)
        self.assertNotIn("student", " ".join(model_admin.list_display))
