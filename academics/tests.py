from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from academics.importer import import_students, read_rows
from academics.models import Batch, Department, Enrollment, StudentProfile, Subject
from accounts.models import Institute, User


HEADER = ("Name,Mobile Number,Email,Batch,Subjects Enrolled,"
          "Guardian Mobile,Guardian Name,Roll Number")


def make_csv(rows, header=HEADER):
    body = "\n".join([header] + rows)
    return SimpleUploadedFile("roster.csv", body.encode(), content_type="text/csv")


class ImporterTests(TestCase):
    def setUp(self):
        self.institute = Institute.objects.create(name="I", code="I", email="i@i.edu")
        self.dept = Department.objects.create(institute=self.institute, name="CSE", code="CSE")
        self.hod = User.objects.create_user(
            email="hod@i.edu", password="Str0ngPass!23", role="HOD",
            institute=self.institute, department=self.dept, registration_completed=True,
        )
        self.dept.hod = self.hod
        self.dept.save()
        for code, name in [("DSA", "Data Structures"), ("DBMS", "Databases"), ("AI", "Artificial Intelligence")]:
            Subject.objects.create(department=self.dept, code=code, name=name)
        # Batches are no longer conjured out of the spreadsheet, so the ones a
        # roster refers to have to exist first — as they would in real use.
        self.batch = Batch.objects.create(department=self.dept, label="2022-26",
                                          start_year=2022, end_year=2026)

    def test_import_creates_students_batches_and_enrollments(self):
        upload = make_csv([
            "Ananya Sharma,9876543210,ananya@i.edu,2022-26,\"DSA, DBMS, AI\",+919812345670,Mr. Sharma,CSE001",
            "Rahul Verma,9876500011,rahul@i.edu,2022-26,\"DSA, DBMS\",09812345671,Mrs. Verma,CSE002",
        ])
        rows, err = read_rows(upload)
        self.assertIsNone(err)
        job = import_students(rows, self.dept, self.hod, send_invites=False)
        self.assertEqual(job.created_count, 2)
        self.assertEqual(job.error_count, 0)
        self.assertTrue(Batch.objects.filter(department=self.dept, label="2022-26").exists())
        self.assertEqual(Batch.objects.filter(department=self.dept).count(), 1)
        ananya = StudentProfile.objects.get(user__email="ananya@i.edu")
        self.assertEqual(ananya.enrollments.count(), 3)
        self.assertEqual(StudentProfile.objects.get(user__email="rahul@i.edu").enrollments.count(), 2)
        self.assertFalse(ananya.user.registration_completed)   # awaits invite acceptance
        self.assertEqual(ananya.guardian_mobile, "+919812345670")
        self.assertEqual(ananya.guardian_name, "Mr. Sharma")

    def test_a_batch_that_does_not_exist_is_an_error_not_a_new_batch(self):
        """
        Regression: unknown batches used to be created silently, so a typo
        (2022-27 for 2022-26) produced a second cohort and split a class in
        two — invisible until the attendance figures stopped adding up.
        """
        upload = make_csv([
            "Typo Student,1,typo@i.edu,2022-27,\"DSA\",+919812345670,G,X9",
        ])
        rows, err = read_rows(upload)
        job = import_students(rows, self.dept, self.hod, send_invites=False)
        self.assertEqual(job.error_count, 1)
        self.assertEqual(job.created_count, 0)
        self.assertIn("does not exist", job.report["rows"][0]["messages"][0])
        self.assertEqual(Batch.objects.filter(department=self.dept).count(), 1)
        self.assertFalse(StudentProfile.objects.filter(user__email="typo@i.edu").exists())

    def test_it_still_creates_batches_when_explicitly_asked(self):
        """The old behaviour is available, just no longer the default."""
        upload = make_csv([
            "New Cohort,1,cohort@i.edu,2023-27,\"DSA\",+919812345670,G,X8",
        ])
        rows, err = read_rows(upload)
        job = import_students(rows, self.dept, self.hod, send_invites=False,
                              create_missing_batches=True)
        self.assertEqual(job.error_count, 0)
        self.assertTrue(Batch.objects.filter(label="2023-27").exists())

    def test_only_new_students_are_emailed_an_invitation(self):
        """
        A sheet of email + guardian mobile is a routine bulk edit. It used to
        re-send an activation link to every student who had not yet activated,
        every time it was uploaded.
        """
        from unittest.mock import patch

        rows, _ = read_rows(make_csv([
            "Ananya Sharma,1,ananya@i.edu,2022-26,\"DSA\",+919812345670,G,C1",
        ]))
        with patch("academics.importer.send_invitation") as sent:
            import_students(rows, self.dept, self.hod, send_invites=True)
        self.assertEqual(sent.call_count, 1)          # new account

        rows, _ = read_rows(make_csv([
            "Ananya Sharma,1,ananya@i.edu,2022-26,\"DSA\",+919899999999,G,C1",
        ]))
        with patch("academics.importer.send_invitation") as sent:
            job = import_students(rows, self.dept, self.hod, send_invites=True)
        self.assertEqual(sent.call_count, 0)          # same person, updated
        self.assertEqual(job.updated_count, 1)
        self.assertFalse(job.report["rows"][0]["invited"])

    def test_a_changed_email_counts_as_a_new_student_and_is_invited(self):
        from unittest.mock import patch

        rows, _ = read_rows(make_csv([
            "Ananya Sharma,1,old@i.edu,2022-26,\"DSA\",+919812345670,G,C1",
        ]))
        import_students(rows, self.dept, self.hod, send_invites=False)

        rows, _ = read_rows(make_csv([
            "Ananya Sharma,1,new@i.edu,2022-26,\"DSA\",+919812345670,G,C1",
        ]))
        with patch("academics.importer.send_invitation") as sent:
            job = import_students(rows, self.dept, self.hod, send_invites=True)
        self.assertEqual(sent.call_count, 1)
        self.assertTrue(job.report["rows"][0]["invited"])

    def test_a_preview_never_emails_anyone(self):
        from unittest.mock import patch

        rows, _ = read_rows(make_csv([
            "Ananya Sharma,1,ananya@i.edu,2022-26,\"DSA\",+919812345670,G,C1",
        ]))
        with patch("academics.importer.send_invitation") as sent:
            import_students(rows, self.dept, self.hod, send_invites=False)
        self.assertEqual(sent.call_count, 0)

    def test_unknown_subject_and_bad_batch_are_reported(self):
        upload = make_csv([
            "Bad Subject,1,bad@i.edu,2022-26,\"DSA, QUANTUM\",+919812345670,G,X1",
            "Bad Batch,1,batch@i.edu,twenty22,\"DSA\",+919812345670,G,X2",
            "No Email,1,,2022-26,\"DSA\",+919812345670,G,X3",
        ])
        rows, err = read_rows(upload)
        self.assertIsNone(err)
        job = import_students(rows, self.dept, self.hod, send_invites=False)
        self.assertEqual(job.error_count, 3)
        self.assertEqual(job.created_count, 0)
        messages = " ".join(m for r in job.report["rows"] for m in r["messages"])
        self.assertIn("QUANTUM", messages)
        self.assertIn("2022-26", messages)

    def test_a_sheet_without_an_email_column_is_rejected(self):
        """Email is the key the upsert matches on, so it is the one hard header."""
        upload = make_csv(["x,y"], header="Name,Batch")
        rows, err = read_rows(upload)
        self.assertEqual(rows, [])
        self.assertIn("Missing required column", err)

    def test_guardian_mobile_is_required_per_row_not_per_header(self):
        """
        The header check used to reject a sheet with no guardian-mobile column.
        It no longer can: a partial update sheet legitimately omits most
        columns. The protection moved to the row, and only for new students —
        so verify a new student without one is still refused.
        """
        upload = make_csv(
            ["A,1,a@i.edu,2022-26,\"DSA\",R1"],
            header="Name,Mobile Number,Email,Batch,Subjects Enrolled,Class Roll",
        )
        rows, err = read_rows(upload)
        self.assertIsNone(err)                     # header alone is acceptable now
        job = import_students(rows, self.dept, self.hod, send_invites=False)
        self.assertEqual(job.error_count, 1)
        self.assertIn("guardian mobile",
                      " ".join(job.report["rows"][0]["messages"]))
        self.assertFalse(StudentProfile.objects.filter(user__email="a@i.edu").exists())

    def test_blank_or_invalid_guardian_mobile_is_a_row_error(self):
        upload = make_csv([
            "No Guardian,1,ng@i.edu,2022-26,\"DSA\",,G,R1",
            "Bad Guardian,1,bg@i.edu,2022-26,\"DSA\",12ab,G,R2",
            "Good,1,good@i.edu,2022-26,\"DSA\",98765 43210,G,R3",
        ])
        rows, _ = read_rows(upload)
        job = import_students(rows, self.dept, self.hod, send_invites=False)
        self.assertEqual(job.error_count, 2)
        self.assertEqual(job.created_count, 1)
        messages = " ".join(m for r in job.report["rows"] for m in r["messages"])
        self.assertIn("guardian mobile is blank", messages)
        self.assertIn("not a valid phone number", messages)
        # formatting is stripped on the way in
        self.assertEqual(
            StudentProfile.objects.get(user__email="good@i.edu").guardian_mobile, "9876543210"
        )

    def test_reimport_updates_instead_of_duplicating(self):
        upload = make_csv(["A,1,a@i.edu,2022-26,\"DSA\",+919812345670,G,R1"])
        rows, _ = read_rows(upload)
        import_students(rows, self.dept, self.hod, send_invites=False)
        upload2 = make_csv(["A,1,a@i.edu,2022-26,\"DSA, DBMS\",+919812345699,G,R1"])
        rows2, _ = read_rows(upload2)
        job = import_students(rows2, self.dept, self.hod, send_invites=False)
        self.assertEqual(StudentProfile.objects.count(), 1)
        self.assertEqual(job.error_count, 0)
        self.assertEqual(
            Enrollment.objects.filter(student__user__email="a@i.edu", is_active=True).count(), 2
        )
        self.assertEqual(
            StudentProfile.objects.get(user__email="a@i.edu").guardian_mobile, "+919812345699"
        )


class ScopingTests(TestCase):
    def setUp(self):
        self.institute = Institute.objects.create(name="I", code="I", email="i@i.edu")
        self.cse = Department.objects.create(institute=self.institute, name="CSE", code="CSE")
        self.ece = Department.objects.create(institute=self.institute, name="ECE", code="ECE")
        Subject.objects.create(department=self.cse, code="DSA", name="DS")
        Subject.objects.create(department=self.ece, code="SS", name="Signals")
        self.hod = User.objects.create_user(
            email="hod.cse@i.edu", password="Str0ngPass!23", role="HOD",
            institute=self.institute, department=self.cse, registration_completed=True,
        )
        self.cse.hod = self.hod
        self.cse.save()

    def test_hod_only_sees_own_department_subjects(self):
        self.client.force_login(self.hod)
        res = self.client.get(reverse("academics:api_subjects"))
        codes = {r["code"] for r in res.json()["data"]["rows"]}
        self.assertEqual(codes, {"DSA"})

    def test_hod_cannot_reach_department_admin(self):
        self.client.force_login(self.hod)
        self.assertEqual(self.client.get(reverse("academics:api_departments")).status_code, 403)


class ContactLinkTests(TestCase):
    """The student list ships ready-made tel: and wa.me targets."""

    def setUp(self):
        from academics.models import Batch, TeacherAssignment
        from accounts.models import User

        self.institute = Institute.objects.create(name="I", code="I", email="i@i.edu")
        self.dept = Department.objects.create(institute=self.institute, name="CSE", code="CSE")
        self.batch = Batch.objects.create(department=self.dept, label="2022-26",
                                          start_year=2022, end_year=2026)
        self.dsa = Subject.objects.create(department=self.dept, code="DSA", name="DS")
        self.hod = User.objects.create_user(
            email="hod@i.edu", password="Str0ngPass!23", role="HOD",
            institute=self.institute, department=self.dept, registration_completed=True)
        self.teacher = User.objects.create_user(
            email="t@i.edu", password="Str0ngPass!23", role="TEACHER",
            institute=self.institute, department=self.dept, registration_completed=True)
        TeacherAssignment.objects.create(teacher=self.teacher, subject=self.dsa,
                                         batch=self.batch)

    def add_student(self, email, mobile, guardian, roll):
        from academics.models import Enrollment, StudentProfile
        from accounts.models import User

        user = User.objects.create_user(
            email=email, password="Str0ngPass!23", full_name="Ana Sharma", role="STUDENT",
            institute=self.institute, department=self.dept, registration_completed=True)
        profile = StudentProfile.objects.create(
            user=user, department=self.dept, batch=self.batch, class_roll=roll,
            mobile=mobile, guardian_mobile=guardian)
        Enrollment.objects.create(student=profile, subject=self.dsa)
        return profile

    def rows(self, user):
        self.client.force_login(user)
        return self.client.get(reverse("academics:api_students")).json()["data"]["rows"]

    def test_links_are_built_for_both_numbers(self):
        self.add_student("a@i.edu", "+919812345670", "+919812345671", "R1")
        row = self.rows(self.hod)[0]
        self.assertEqual(row["mobile_dial"]["tel"], "+919812345670")
        self.assertEqual(row["mobile_dial"]["wa"], "919812345670")   # wa.me drops the +
        self.assertEqual(row["guardian_dial"]["tel"], "+919812345671")
        self.assertEqual(row["guardian_dial"]["wa"], "919812345671")

    def test_a_locally_formatted_number_still_links(self):
        """Rosters often hold '98765 43210'; the link must still work."""
        self.add_student("b@i.edu", "98765 43210", "098765-43211", "R2")
        row = self.rows(self.hod)[0]
        self.assertEqual(row["mobile_dial"]["tel"], "+919876543210")
        self.assertEqual(row["guardian_dial"]["wa"], "919876543211")

    def test_a_missing_number_yields_no_link(self):
        self.add_student("c@i.edu", "", "", "R3")
        row = self.rows(self.hod)[0]
        self.assertEqual(row["mobile_dial"]["tel"], "")
        self.assertEqual(row["guardian_dial"]["tel"], "")
        self.assertIn("no number", row["guardian_dial"]["error"])

    def test_an_unusable_number_is_flagged_not_linked(self):
        self.add_student("d@i.edu", "12", "not a number", "R4")
        row = self.rows(self.hod)[0]
        self.assertEqual(row["mobile_dial"]["tel"], "")
        self.assertTrue(row["mobile_dial"]["error"])
        self.assertEqual(row["mobile_dial"]["raw"], "12")      # still shown to staff
        self.assertTrue(row["guardian_dial"]["error"])

    def test_teachers_see_the_same_links_in_my_students(self):
        self.add_student("e@i.edu", "+919812345670", "+919812345671", "R5")
        row = self.rows(self.teacher)[0]
        self.assertEqual(row["mobile_dial"]["wa"], "919812345670")
        self.assertEqual(row["guardian_dial"]["wa"], "919812345671")


class RosterUpsertTests(TestCase):
    """
    Re-importing a sheet updates students instead of duplicating them, and a
    blank cell means "leave this alone" so a partial sheet can be used to fix
    one field in bulk without wiping the rest of the row.
    """

    HEADER = ("Email,Name,Class Roll,Exam Roll,Batch,Subjects Enrolled,"
              "Guardian Mobile,Guardian Name,Guardian Email,Mobile Number")

    def setUp(self):
        self.institute = Institute.objects.create(name="I", code="I", email="i@i.edu")
        self.dept = Department.objects.create(institute=self.institute, name="CSE", code="CSE")
        self.hod = User.objects.create_user(
            email="hod@i.edu", password="Str0ngPass!23", role="HOD",
            institute=self.institute, department=self.dept, registration_completed=True,
        )
        for code, name in [("DSA", "Data Structures"), ("DBMS", "Databases")]:
            Subject.objects.create(department=self.dept, code=code, name=name)
        self._import([
            "ana@i.edu,Ana Sharma,01,CSE22001,2022-26,\"DSA, DBMS\","
            "+919812345670,Mr Sharma,parent@i.edu,9876543210",
        ])
        self.ana = StudentProfile.objects.get(user__email="ana@i.edu")

    def _import(self, rows, header=None):
        body = "\n".join([header or self.HEADER] + rows)
        upload = SimpleUploadedFile("r.csv", body.encode(), content_type="text/csv")
        parsed, error = read_rows(upload)
        self.assertIsNone(error, error)
        return import_students(parsed, self.dept, self.hod, send_invites=False)

    # ----------------------------------------------------------------- split
    def test_both_rolls_are_stored_separately(self):
        self.assertEqual(self.ana.class_roll, "01")
        self.assertEqual(self.ana.exam_roll, "CSE22001")

    def test_a_bare_roll_column_still_means_class_roll(self):
        """Old sheets say "Roll Number" and must keep working."""
        self._import(["bo@i.edu,Bo,,,2022-26,DSA,+919812345671,,,"],
                     header="Email,Name,Roll Number,Batch,Subjects Enrolled,"
                            "Guardian Mobile,Guardian Name,Guardian Email,Mobile Number")
        # header has one fewer column, so re-import properly:
        self._import(["cy@i.edu,Cy,07,2022-26,DSA,+919812345672,,,"],
                     header="Email,Name,Roll No,Batch,Subjects Enrolled,"
                            "Guardian Mobile,Guardian Name,Guardian Email,Mobile Number")
        self.assertEqual(StudentProfile.objects.get(user__email="cy@i.edu").class_roll, "07")

    # ---------------------------------------------------------------- upsert
    def test_repeat_email_updates_rather_than_duplicates(self):
        job = self._import([
            "ana@i.edu,Ana Sharma,01,CSE22001,2022-26,\"DSA, DBMS\","
            "+919999999999,Mr Sharma,parent@i.edu,9876543210",
        ])
        self.assertEqual(StudentProfile.objects.filter(user__email="ana@i.edu").count(), 1)
        self.assertEqual(job.created_count, 0)
        self.assertEqual(job.updated_count, 1)
        self.ana.refresh_from_db()
        self.assertEqual(self.ana.guardian_mobile, "+919999999999")

    def test_a_two_column_sheet_updates_only_that_column(self):
        """The whole point: fix guardian mobiles in bulk without re-typing rosters."""
        self._import(["ana@i.edu,+919111111111"], header="Email,Guardian Mobile")
        self.ana.refresh_from_db()
        self.assertEqual(self.ana.guardian_mobile, "+919111111111")
        # everything else survived
        self.assertEqual(self.ana.class_roll, "01")
        self.assertEqual(self.ana.exam_roll, "CSE22001")
        self.assertEqual(self.ana.guardian_name, "Mr Sharma")
        self.assertEqual(self.ana.mobile, "9876543210")
        self.assertEqual(self.ana.user.full_name, "Ana Sharma")
        self.assertEqual(self.ana.batch.label, "2022-26")
        self.assertEqual(self.ana.enrollments.filter(is_active=True).count(), 2)

    def test_blank_subjects_leave_enrolments_alone(self):
        self._import(["ana@i.edu,,,,,,+919812345670,,,"])
        self.assertEqual(self.ana.enrollments.filter(is_active=True).count(), 2)

    def test_supplying_subjects_still_replaces_them(self):
        self._import(["ana@i.edu,,,,,DSA,,,,"])
        active = {e.subject.code for e in self.ana.enrollments.filter(is_active=True)}
        self.assertEqual(active, {"DSA"})

    def test_dash_clears_a_value(self):
        self._import(["ana@i.edu,,,-,,,,,-,"])
        self.ana.refresh_from_db()
        self.assertEqual(self.ana.exam_roll, "")
        self.assertEqual(self.ana.guardian_email, "")
        self.assertEqual(self.ana.class_roll, "01")     # untouched

    # -------------------------------------------------------------- new rows
    def test_a_new_student_still_needs_the_required_columns(self):
        job = self._import(["new@i.edu,,,,,,,,,"])
        self.assertEqual(job.error_count, 1)
        messages = " ".join(job.report["rows"][0]["messages"])
        for field in ("name", "class roll", "batch", "subjects", "guardian mobile"):
            self.assertIn(field, messages)
        self.assertFalse(StudentProfile.objects.filter(user__email="new@i.edu").exists())

    def test_email_is_the_only_mandatory_header(self):
        parsed, error = read_rows(SimpleUploadedFile(
            "r.csv", b"Email,Guardian Mobile\nana@i.edu,+919812345670\n",
            content_type="text/csv"))
        self.assertIsNone(error)
        self.assertEqual(len(parsed), 1)

    def test_a_sheet_without_email_is_rejected(self):
        _, error = read_rows(SimpleUploadedFile(
            "r.csv", b"Name,Batch\nAna,2022-26\n", content_type="text/csv"))
        self.assertIn("email", error.lower())


class TeacherStudentScopeTests(TestCase):
    """
    "My students" means the students a teacher actually teaches.

    An allocation is (subject, batch). Filtering on subject alone used to leak
    every student taking that subject in any batch — including batches the
    teacher has never taught.
    """

    def setUp(self):
        from academics.models import Batch, Enrollment, StudentProfile, TeacherAssignment

        self.institute = Institute.objects.create(name="I", code="I", email="i@i.edu")
        self.cse = Department.objects.create(institute=self.institute, name="CSE", code="CSE")
        self.ece = Department.objects.create(institute=self.institute, name="ECE", code="ECE")
        self.dsa = Subject.objects.create(department=self.cse, code="DSA", name="DS")
        self.ai = Subject.objects.create(department=self.cse, code="AI", name="AI")
        self.vlsi = Subject.objects.create(department=self.ece, code="VLSI", name="VLSI")

        self.b22 = Batch.objects.create(department=self.cse, label="2022-26",
                                        start_year=2022, end_year=2026)
        self.b21 = Batch.objects.create(department=self.cse, label="2021-25",
                                        start_year=2021, end_year=2025)
        self.be = Batch.objects.create(department=self.ece, label="2022-26",
                                       start_year=2022, end_year=2026)

        self.teacher = User.objects.create_user(
            email="t@i.edu", password="Str0ngPass!23", role="TEACHER",
            institute=self.institute, department=self.cse, registration_completed=True)
        # Teaches DSA — to the 2022-26 batch only.
        TeacherAssignment.objects.create(teacher=self.teacher, subject=self.dsa, batch=self.b22)

        def student(email, dept, batch, subjects):
            user = User.objects.create_user(
                email=email, password="Str0ngPass!23", role="STUDENT",
                institute=self.institute, department=dept,
                registration_completed=True, full_name=email)
            profile = StudentProfile.objects.create(
                user=user, department=dept, batch=batch, class_roll="1")
            for subject in subjects:
                Enrollment.objects.create(student=profile, subject=subject)
            return profile

        student("taught@i.edu", self.cse, self.b22, [self.dsa])
        student("other-batch@i.edu", self.cse, self.b21, [self.dsa])
        student("other-subject@i.edu", self.cse, self.b22, [self.ai])
        student("other-dept@i.edu", self.ece, self.be, [self.vlsi])

    def _seen(self, user):
        from academics.selectors import students_qs_for

        return {p.user.email for p in students_qs_for(user)}

    def test_a_teacher_sees_only_the_batches_they_teach(self):
        self.assertEqual(self._seen(self.teacher), {"taught@i.edu"})

    def test_a_second_allocation_widens_the_scope_correctly(self):
        from academics.models import TeacherAssignment

        TeacherAssignment.objects.create(teacher=self.teacher, subject=self.dsa, batch=self.b21)
        self.assertEqual(self._seen(self.teacher), {"taught@i.edu", "other-batch@i.edu"})

    def test_a_teacher_with_no_allocations_sees_nobody(self):
        lonely = User.objects.create_user(
            email="new@i.edu", password="Str0ngPass!23", role="TEACHER",
            institute=self.institute, department=self.cse, registration_completed=True)
        self.assertEqual(self._seen(lonely), set())

    def test_the_api_agrees_with_the_selector(self):
        client = self.client_class()
        client.force_login(self.teacher)
        rows = client.get(reverse("academics:api_students")).json()["data"]["rows"]
        self.assertEqual({r["email"] for r in rows}, {"taught@i.edu"})

    def test_staff_scopes_are_unchanged(self):
        hod = User.objects.create_user(
            email="hod@i.edu", password="Str0ngPass!23", role="HOD",
            institute=self.institute, department=self.cse, registration_completed=True)
        head = User.objects.create_user(
            email="head@i.edu", password="Str0ngPass!23", role="HEAD",
            institute=self.institute, registration_completed=True)
        self.assertEqual(self._seen(hod),
                         {"taught@i.edu", "other-batch@i.edu", "other-subject@i.edu"})
        self.assertEqual(len(self._seen(head)), 4)      # whole institute


class WideStudentScreenTests(TestCase):
    """
    Two student screens for a teacher:
      "My students" — only the classes they teach
      "Students"    — everyone, read-only except unlinking a device
    """

    def setUp(self):
        from academics.models import Batch, Enrollment, StudentProfile, TeacherAssignment

        self.institute = Institute.objects.create(name="I", code="I", email="i@i.edu")
        self.cse = Department.objects.create(institute=self.institute, name="CSE", code="CSE")
        self.ece = Department.objects.create(institute=self.institute, name="ECE", code="ECE")
        self.dsa = Subject.objects.create(department=self.cse, code="DSA", name="DS")
        self.vlsi = Subject.objects.create(department=self.ece, code="VLSI", name="VLSI")
        self.b22 = Batch.objects.create(department=self.cse, label="2022-26",
                                        start_year=2022, end_year=2026)
        self.be = Batch.objects.create(department=self.ece, label="2022-26",
                                       start_year=2022, end_year=2026)

        self.teacher = User.objects.create_user(
            email="t@i.edu", password="Str0ngPass!23", role="TEACHER",
            institute=self.institute, department=self.cse, registration_completed=True)
        TeacherAssignment.objects.create(teacher=self.teacher, subject=self.dsa, batch=self.b22)

        def student(email, dept, batch, subject):
            user = User.objects.create_user(
                email=email, password="Str0ngPass!23", role="STUDENT",
                institute=self.institute, department=dept,
                registration_completed=True, full_name=email)
            user.device_id = "dev-" + email
            user.save(update_fields=["device_id"])
            profile = StudentProfile.objects.create(
                user=user, department=dept, batch=batch, class_roll="1")
            Enrollment.objects.create(student=profile, subject=subject)
            return profile

        self.mine = student("mine@i.edu", self.cse, self.b22, self.dsa)
        self.theirs = student("theirs@i.edu", self.ece, self.be, self.vlsi)

    def _client(self, user):
        client = self.client_class()
        client.force_login(user)
        return client

    def _emails(self, user, **params):
        res = self._client(user).get(reverse("academics:api_students"), params)
        self.assertEqual(res.status_code, 200)
        return {r["email"] for r in res.json()["data"]["rows"]}

    def test_my_students_stays_narrow(self):
        self.assertEqual(self._emails(self.teacher), {"mine@i.edu"})

    def test_the_wide_screen_shows_every_department(self):
        self.assertEqual(self._emails(self.teacher, scope="all"),
                         {"mine@i.edu", "theirs@i.edu"})

    def test_rows_carry_the_department_for_the_column_and_filter(self):
        rows = self._client(self.teacher).get(
            reverse("academics:api_students"), {"scope": "all"}).json()["data"]["rows"]
        by_email = {r["email"]: r for r in rows}
        self.assertEqual(by_email["theirs@i.edu"]["department"], "ECE")
        self.assertEqual(str(by_email["theirs@i.edu"]["department_id"]), str(self.ece.pk))

    def test_a_student_cannot_reach_the_roster_at_all(self):
        """
        Stronger than scope filtering: the endpoint is closed to students, so
        `scope=all` never even gets the chance to matter. all_students_for()
        also returns nothing for them, so both layers agree.
        """
        from academics.selectors import all_students_for

        res = self._client(self.mine.user).get(
            reverse("academics:api_students"), {"scope": "all"})
        self.assertEqual(res.status_code, 403)
        self.assertEqual(all_students_for(self.mine.user).count(), 0)

    def test_a_teacher_can_unlink_a_device_for_a_student_they_do_not_teach(self):
        # Called directly rather than via reverse(): the sqlite test harness
        # uses integer primary keys, which an <oid:pk> route cannot build.
        from django.test import RequestFactory

        from academics import views

        request = RequestFactory().post("/x/", {"reason": "lost phone"})
        request.user = self.teacher
        request.session = self._client(self.teacher).session
        res = views.api_student_reset_device(request, pk=self.theirs.pk)
        self.assertEqual(res.status_code, 200)
        self.theirs.user.refresh_from_db()
        self.assertEqual(self.theirs.user.device_id, "")

    def test_a_teacher_still_cannot_edit_or_deactivate(self):
        from django.core.exceptions import PermissionDenied
        from django.test import RequestFactory

        from academics import views

        for view in (views.api_student_save, views.api_student_toggle,
                     views.api_student_resend):
            with self.subTest(view=view.__name__):
                request = RequestFactory().post("/x/", {})
                request.user = self.teacher
                with self.assertRaises(PermissionDenied):
                    view(request, pk=self.theirs.pk)
        self.theirs.refresh_from_db()
        self.assertTrue(self.theirs.is_active)

    def test_both_pages_load_for_a_teacher(self):
        client = self._client(self.teacher)
        self.assertEqual(client.get(reverse("academics:students")).status_code, 200)
        self.assertEqual(client.get(reverse("academics:all_students")).status_code, 200)
