"""
Evidence attached to an absence request.

The rule worth protecting is the one that is easy to break by accident: a
student gets one attempt per class, so a request refused because of its files
must leave no reason behind — otherwise the refusal costs them the attempt.
"""
import datetime as dt

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from academics.models import Batch, Department, Enrollment, StudentProfile, Subject, TeacherAssignment
from accounts.models import Institute, User
from attendance.models import AbsenceAttachment, AbsenceReason, AttendanceSession
from attendance.services import (
    AttendanceError,
    submit_absence_reason,
    submit_planned_absence,
    validate_attachments,
)

# Real signatures — the validator reads the file, so a placeholder like b"x"
# would be rejected for the right reason but prove nothing about the wrong one.
PDF = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 8
HEIC = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 12


def upload(name, head=PDF, size=None):
    """One uploaded file, padded to `size` bytes if a size is asked for."""
    body = head
    if size:
        body = head + b"\x00" * max(size - len(head), 0)
    return SimpleUploadedFile(name, body, content_type="application/octet-stream")


class AttachmentValidationTests(SimpleTestCase):
    """The limits, with no database in the way."""

    def test_no_files_is_fine(self):
        """Evidence is optional — a reason with nothing attached is normal."""
        self.assertEqual(validate_attachments([]), [])
        self.assertEqual(validate_attachments(None), [])

    def test_five_files_are_accepted(self):
        files = [upload(f"note{i}.pdf") for i in range(5)]
        self.assertEqual(len(validate_attachments(files)), 5)

    def test_a_sixth_file_is_refused(self):
        with self.assertRaises(AttendanceError) as ctx:
            validate_attachments([upload(f"note{i}.pdf") for i in range(6)])
        self.assertEqual(ctx.exception.code, "TOO_MANY_FILES")

    def test_twenty_megabytes_in_total_is_accepted(self):
        mb = 1024 * 1024
        files = [upload(f"scan{i}.pdf", size=4 * mb) for i in range(5)]
        self.assertEqual(len(validate_attachments(files)), 5)

    def test_one_byte_over_the_total_is_refused(self):
        """The cap is on the total, not on any single file."""
        mb = 1024 * 1024
        files = [upload("a.pdf", size=10 * mb), upload("b.pdf", size=10 * mb + 1)]
        with self.assertRaises(AttendanceError) as ctx:
            validate_attachments(files)
        self.assertEqual(ctx.exception.code, "FILES_TOO_LARGE")

    def test_every_promised_format_is_recognised(self):
        cases = [
            ("note.pdf", PDF, "application/pdf"),
            ("photo.png", PNG, "image/png"),
            ("photo.jpg", JPEG, "image/jpeg"),
            ("photo.jpeg", JPEG, "image/jpeg"),
            ("photo.webp", WEBP, "image/webp"),
            ("photo.heic", HEIC, "image/heic"),
        ]
        for name, head, expected in cases:
            with self.subTest(name=name):
                checked = validate_attachments([upload(name, head)])
                self.assertEqual(checked[0][1], expected)

    def test_something_that_is_not_a_document_is_refused(self):
        with self.assertRaises(AttendanceError) as ctx:
            validate_attachments([upload("payload.pdf", b"MZ\x90\x00" + b"\x00" * 16)])
        self.assertEqual(ctx.exception.code, "BAD_FILE_TYPE")

    def test_the_contents_must_match_the_extension(self):
        """
        A PDF named .jpg is harmless in itself. A mismatch is refused because
        it is the shape of an attempt to have something served as a type it is
        not — and because the type we record is the one the download sends.
        """
        with self.assertRaises(AttendanceError) as ctx:
            validate_attachments([upload("certificate.jpg", PDF)])
        self.assertEqual(ctx.exception.code, "EXTENSION_MISMATCH")

    def test_the_file_is_left_rewound_for_saving(self):
        """Validation reads the head; storage must still see the whole file."""
        f = upload("note.pdf")
        validate_attachments([f])
        self.assertEqual(f.read(), PDF)

    def test_limits_come_from_settings(self):
        from django.conf import settings

        conf = {**settings.ATTENDANCE, "ATTACHMENT_MAX_FILES": 2}
        with override_settings(ATTENDANCE=conf):
            with self.assertRaises(AttendanceError) as ctx:
                validate_attachments([upload(f"n{i}.pdf") for i in range(3)])
        self.assertEqual(ctx.exception.code, "TOO_MANY_FILES")

    def test_zero_files_turns_the_feature_off(self):
        from django.conf import settings

        conf = {**settings.ATTENDANCE, "ATTACHMENT_MAX_FILES": 0}
        with override_settings(ATTENDANCE=conf):
            with self.assertRaises(AttendanceError) as ctx:
                validate_attachments([upload("note.pdf")])
        self.assertEqual(ctx.exception.code, "ATTACHMENTS_DISABLED")


class AttachmentStorageTests(TestCase):
    """Attaching, and who may read what was attached."""

    def setUp(self):
        self.institute = Institute.objects.create(name="I", code="I", email="i@i.edu")
        self.cse = Department.objects.create(institute=self.institute, name="CSE", code="CSE")
        self.dsa = Subject.objects.create(department=self.cse, code="DSA", name="DS")
        self.batch = Batch.objects.create(department=self.cse, label="2022-26",
                                          start_year=2022, end_year=2026)

        def user(email, role, dept=None):
            return User.objects.create_user(
                email=email, password="Str0ngPass!23", role=role, institute=self.institute,
                department=dept, registration_completed=True, full_name=email)

        self.teacher = user("t@i.edu", "TEACHER", self.cse)
        self.other_teacher = user("t2@i.edu", "TEACHER", self.cse)
        self.hod = user("hod@i.edu", "HOD", self.cse)
        TeacherAssignment.objects.create(teacher=self.teacher, subject=self.dsa,
                                         batch=self.batch)

        self.student_user = user("s@i.edu", "STUDENT", self.cse)
        self.student = StudentProfile.objects.create(
            user=self.student_user, department=self.cse, batch=self.batch, class_roll="01")
        Enrollment.objects.create(student=self.student, subject=self.dsa)

    def _session(self, days_ago=1):
        return AttendanceSession.objects.create(
            teacher=self.teacher, subject=self.dsa, batch=self.batch,
            session_date=timezone.localdate() - dt.timedelta(days=days_ago),
            latitude=22.5, longitude=88.3,
            expires_at=timezone.now() + dt.timedelta(minutes=5), expected_count=1)

    # ------------------------------------------------------------- storing
    def test_a_reason_keeps_what_was_attached_to_it(self):
        reason = submit_absence_reason(
            student=self.student, session=self._session(), text="Fever",
            files=[upload("certificate.pdf"), upload("ticket.png", PNG)])
        names = sorted(a.original_name for a in reason.attachments.all())
        self.assertEqual(names, ["certificate.pdf", "ticket.png"])
        self.assertEqual(
            {a.content_type for a in reason.attachments.all()},
            {"application/pdf", "image/png"})

    def test_a_refused_upload_leaves_no_reason_behind(self):
        """
        The one that matters. There is one attempt per class, so a reason saved
        alongside a rejected file would burn that attempt and leave the student
        unable to try again with a smaller scan.
        """
        session = self._session()
        with self.assertRaises(AttendanceError):
            submit_absence_reason(
                student=self.student, session=session, text="Fever",
                files=[upload(f"n{i}.pdf") for i in range(6)])
        self.assertFalse(
            AbsenceReason.objects.filter(session=session, student=self.student).exists())
        self.assertEqual(AbsenceAttachment.objects.count(), 0)

    def test_a_planned_absence_takes_evidence_too(self):
        planned = submit_planned_absence(
            student=self.student,
            from_date=timezone.localdate() + dt.timedelta(days=3),
            to_date=timezone.localdate() + dt.timedelta(days=4),
            text="Wedding", files=[upload("invitation.pdf")])
        self.assertEqual(
            [a.original_name for a in planned.attachments.all()], ["invitation.pdf"])

    def test_an_attachment_belongs_to_exactly_one_request(self):
        from django.core.exceptions import ValidationError

        with self.assertRaises(ValidationError):
            AbsenceAttachment.objects.create(original_name="x.pdf")

    # -------------------------------------------------------------- access
    def _attachment(self):
        reason = submit_absence_reason(
            student=self.student, session=self._session(), text="Fever",
            files=[upload("certificate.pdf")])
        return reason.attachments.first()

    def _get(self, user, attachment):
        client = self.client_class()
        client.force_login(user)
        return client.get(reverse("attendance:api_attachment_download",
                                  args=[attachment.pk]))

    def test_the_student_who_uploaded_it_can_open_it(self):
        attachment = self._attachment()
        self.assertEqual(self._get(self.student_user, attachment).status_code, 200)

    def test_the_teacher_who_took_the_class_can_open_it(self):
        attachment = self._attachment()
        self.assertEqual(self._get(self.teacher, attachment).status_code, 200)

    def test_the_hod_can_open_it(self):
        attachment = self._attachment()
        self.assertEqual(self._get(self.hod, attachment).status_code, 200)

    def test_a_teacher_who_did_not_take_the_class_cannot(self):
        """
        404 rather than 403: whether a given attachment exists is itself
        something the asker is not entitled to learn.
        """
        attachment = self._attachment()
        self.assertEqual(self._get(self.other_teacher, attachment).status_code, 404)

    def test_another_student_cannot(self):
        other_user = User.objects.create_user(
            email="s2@i.edu", password="Str0ngPass!23", role="STUDENT",
            institute=self.institute, department=self.cse,
            registration_completed=True, full_name="Other")
        StudentProfile.objects.create(
            user=other_user, department=self.cse, batch=self.batch, class_roll="02")
        attachment = self._attachment()
        self.assertEqual(self._get(other_user, attachment).status_code, 404)

    def test_the_download_is_never_rendered_in_the_page(self):
        """Nothing a student uploads gets to execute on our origin."""
        attachment = self._attachment()
        response = self._get(self.student_user, attachment)
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        # The recorded type comes from the file's own bytes, not from what the
        # uploader claimed — the upload above declared octet-stream.
        self.assertEqual(response["Content-Type"], "application/pdf")

    def test_the_row_offers_metadata_but_no_file_url(self):
        self._attachment()
        client = self.client_class()
        client.force_login(self.teacher)
        rows = client.get(reverse("attendance:api_absence_reasons")).json()["data"]["rows"]
        attachment = rows[0]["items"][0]["attachments"][0]
        self.assertEqual(attachment["name"], "certificate.pdf")
        self.assertNotIn("url", attachment)
        self.assertNotIn("file", attachment)
