"""
Face enrolment: the gate, and the rules the server enforces on the frames.

The model itself is not exercised here — loading InsightFace costs hundreds of
megabytes and several seconds, which no test suite should pay. What is tested
is everything around it: the pose rules, the same-person rule, and the gate,
all of which are ours and all of which are easy to break by accident.
"""
from unittest.mock import patch

from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from accounts import face as face_engine
from accounts import face_service
from accounts.face import FaceError
from accounts.middleware import ForceFaceEnrolmentMiddleware
from accounts.models import Institute, User


class PoseRuleTests(SimpleTestCase):
    """The head has to actually be where the instruction asked."""

    def test_straight_ahead_accepts_a_small_wobble(self):
        for yaw in (-11, 0, 11):
            with self.subTest(yaw=yaw):
                face_engine._check_pose(yaw, "FRONT")     # must not raise

    def test_straight_ahead_rejects_a_turned_head(self):
        with self.assertRaises(FaceError) as ctx:
            face_engine._check_pose(25, "FRONT")
        self.assertEqual(ctx.exception.code, "POSE_NOT_FRONT")

    def test_under_and_over_rotation_are_told_apart(self):
        """
        One message for both said nothing about which way to correct — and told
        whoever read the log even less.
        """
        with self.assertRaises(FaceError) as ctx:
            face_engine._check_pose(5, "LEFT")
        self.assertEqual(ctx.exception.code, "POSE_NOT_ENOUGH")

        with self.assertRaises(FaceError) as ctx:
            face_engine._check_pose(80, "LEFT")
        self.assertEqual(ctx.exception.code, "POSE_TOO_FAR")

    def test_turning_the_wrong_way_says_so(self):
        with self.assertRaises(FaceError) as ctx:
            face_engine._check_pose(-25, "LEFT")
        self.assertEqual(ctx.exception.code, "POSE_WRONG_SIDE")
        self.assertIn("wrong way", ctx.exception.message)

    def test_the_measured_angle_reaches_the_log(self):
        """"It keeps refusing" versus "it measured 61 and the ceiling is 55"."""
        with self.assertRaises(FaceError) as ctx:
            face_engine._check_pose(61, "LEFT")
        self.assertEqual(ctx.exception.detail.get("yaw"), 61)

    def test_a_left_turn_must_be_far_enough(self):
        with self.assertRaises(FaceError):
            face_engine._check_pose(8, "LEFT")            # barely moved
        face_engine._check_pose(25, "LEFT")               # good

    def test_a_left_turn_must_not_be_too_far(self):
        """Past the limit one side of the face is hidden and the vector is worse."""
        with self.assertRaises(FaceError):
            face_engine._check_pose(70, "LEFT")

    def test_left_and_right_are_not_interchangeable(self):
        """A student who turns the wrong way must be told, not quietly accepted."""
        with self.assertRaises(FaceError):
            face_engine._check_pose(25, "RIGHT")          # that is a left turn
        face_engine._check_pose(-25, "RIGHT")

    def test_thresholds_come_from_settings(self):
        from django.conf import settings

        conf = {**settings.FACE, "FRONT_MAX_YAW": 3}
        with override_settings(FACE=conf):
            with self.assertRaises(FaceError):
                face_engine._check_pose(8, "FRONT")


class YawSignTests(SimpleTestCase):
    """
    Which way is left.

    Regression: the capture page read the wrong element of MediaPipe's
    transformation matrix and came out inverted, so "turn left" only accepted a
    student who turned right. Both sides now derive the angle from the same
    geometry, and these pin the direction so it cannot quietly flip again.
    """

    def _face(self, nose_x, eye_a=100.0, eye_b=200.0):
        """A stand-in detection: two eye keypoints and a nose, in image x."""
        return type("F", (), {"kps": [(eye_a, 0), (eye_b, 0), (nose_x, 0)]})()

    def test_a_centred_nose_is_straight_ahead(self):
        self.assertAlmostEqual(face_engine.yaw_of(self._face(150.0)), 0.0)

    def test_a_nose_toward_larger_x_is_a_turn_to_the_subjects_left(self):
        """
        In the image frame the subject's left is the larger-x side, so this is
        the direction the LEFT step must accept.
        """
        yaw = face_engine.yaw_of(self._face(175.0))
        self.assertGreater(yaw, 0)
        face_engine._check_pose(yaw, "LEFT")            # accepted
        with self.assertRaises(FaceError):
            face_engine._check_pose(yaw, "RIGHT")       # and not as a right turn

    def test_a_nose_toward_smaller_x_is_a_turn_to_the_subjects_right(self):
        yaw = face_engine.yaw_of(self._face(125.0))
        self.assertLess(yaw, 0)
        face_engine._check_pose(yaw, "RIGHT")
        with self.assertRaises(FaceError):
            face_engine._check_pose(yaw, "LEFT")

    def test_the_keypoint_order_does_not_change_the_answer(self):
        """
        The detector's two eye keypoints may arrive either way round. The sign
        must rest on where the nose is, not on which keypoint is whose eye.
        """
        a = face_engine.yaw_of(self._face(175.0, eye_a=100.0, eye_b=200.0))
        b = face_engine.yaw_of(self._face(175.0, eye_a=200.0, eye_b=100.0))
        self.assertAlmostEqual(a, b)

    def test_a_pose_attribute_from_the_model_is_ignored(self):
        """
        Some model packs expose their own pose estimate with its own sign
        convention. Trusting it would flip the meaning of "left" the day
        someone changed packs.
        """
        face = self._face(175.0)
        face.pose = (0.0, -30.0, 0.0)                   # opposite sign
        self.assertGreater(face_engine.yaw_of(face), 0)

    def test_the_extremes_are_clamped_not_wrapped(self):
        """A nose far outside the eye span must not read as a small angle."""
        self.assertAlmostEqual(face_engine.yaw_of(self._face(900.0)), 45.0)
        self.assertAlmostEqual(face_engine.yaw_of(self._face(-900.0)), -45.0)


class OcclusionRuleTests(SimpleTestCase):
    def test_a_covered_mouth_is_refused(self):
        with self.assertRaises(FaceError) as ctx:
            face_engine._check_occlusion({"eye_energy": 1.0, "mouth_energy": 0.1})
        self.assertEqual(ctx.exception.code, "FACE_COVERED")

    def test_covered_eyes_are_refused(self):
        with self.assertRaises(FaceError) as ctx:
            face_engine._check_occlusion({"eye_energy": 0.1, "mouth_energy": 1.0})
        self.assertEqual(ctx.exception.code, "EYES_COVERED")

    def test_an_ordinary_face_passes(self):
        face_engine._check_occlusion({"eye_energy": 1.1, "mouth_energy": 0.9})

    def test_no_report_is_not_a_failure(self):
        """A face too small to sample regions from is caught by another rule."""
        face_engine._check_occlusion({})


class SamePersonTests(SimpleTestCase):
    """Three valid faces at three valid angles can still be three people."""

    def _result(self, vector):
        return {"embedding": vector}

    def test_three_captures_of_one_person_pass(self):
        base = [1.0, 0.0, 0.0]
        near = [0.95, 0.31, 0.0]
        face_engine.check_same_person({
            "FRONT": self._result(base),
            "LEFT": self._result(near),
            "RIGHT": self._result(base),
        })

    def test_a_different_person_in_one_frame_is_caught(self):
        with self.assertRaises(FaceError) as ctx:
            face_engine.check_same_person({
                "FRONT": self._result([1.0, 0.0, 0.0]),
                "LEFT": self._result([0.0, 1.0, 0.0]),      # someone else
                "RIGHT": self._result([1.0, 0.0, 0.0]),
            })
        self.assertEqual(ctx.exception.code, "DIFFERENT_PEOPLE")

    def test_cosine_is_orientation_not_magnitude(self):
        self.assertAlmostEqual(face_engine.cosine([2, 0], [9, 0]), 1.0)
        self.assertAlmostEqual(face_engine.cosine([1, 0], [0, 1]), 0.0)


class GateTests(TestCase):
    """Who gets held at the capture page, and who gets past it."""

    def setUp(self):
        self.institute = Institute.objects.create(name="I", code="I", email="i@i.edu")
        self.factory = RequestFactory()

    def _user(self, role, **kw):
        return User.objects.create_user(
            email=f"{role.lower()}{User.objects.count()}@i.edu", password="Str0ngPass!23",
            role=role, institute=self.institute, registration_completed=True, **kw)

    def _run(self, user, path="/app/", ajax=False):
        request = self.factory.get(
            path, **({"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"} if ajax else {}))
        request.user = user
        sentinel = object()
        return ForceFaceEnrolmentMiddleware(lambda r: sentinel)(request), sentinel

    def test_a_student_without_a_face_is_redirected(self):
        response, sentinel = self._run(self._user("STUDENT"))
        self.assertIsNot(response, sentinel)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("accounts:face_capture"))

    def test_a_student_with_a_face_passes_through(self):
        response, sentinel = self._run(self._user("STUDENT", face_enrolled=True))
        self.assertIs(response, sentinel)

    def test_staff_are_never_gated(self):
        for role in ("TEACHER", "HOD", "HEAD"):
            with self.subTest(role=role):
                response, sentinel = self._run(self._user(role))
                self.assertIs(response, sentinel)

    def test_a_student_who_has_not_set_a_password_is_left_to_the_other_gate(self):
        """
        Two gates redirecting at each other would trap them in a loop — the
        password step has to finish first.
        """
        user = self._user("STUDENT")
        user.registration_completed = False
        user.save(update_fields=["registration_completed"])
        response, sentinel = self._run(user)
        self.assertIs(response, sentinel)

    def test_the_capture_page_itself_is_never_gated(self):
        """A gate that traps the page it redirects to is a door with no handle."""
        user = self._user("STUDENT")
        for path in ("/auth/face/", "/auth/api/face/enrol/", "/auth/logout/",
                     "/static/js/app.js", "/media/x.jpg"):
            with self.subTest(path=path):
                response, sentinel = self._run(user, path)
                self.assertIs(response, sentinel)

    def test_marking_attendance_is_gated(self):
        """
        Not an oversight: an unenrolled student has no template to check a
        live face against, so letting them mark would defeat the point.
        """
        response, sentinel = self._run(self._user("STUDENT"), "/attendance/mark/abc123/")
        self.assertIsNot(response, sentinel)

    def test_an_ajax_call_gets_json_not_a_redirect(self):
        """The jQuery layer would report a parse error on an HTML redirect."""
        response, sentinel = self._run(self._user("STUDENT"), "/app/api/summary/", ajax=True)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.headers["Content-Type"], "application/json")

    def test_turning_the_feature_off_opens_the_gate(self):
        from django.conf import settings

        conf = {**settings.FACE, "ENABLED": False}
        with override_settings(FACE=conf):
            response, sentinel = self._run(self._user("STUDENT"))
        self.assertIs(response, sentinel)

    def test_an_anonymous_visitor_is_not_gated(self):
        from django.contrib.auth.models import AnonymousUser

        response, sentinel = self._run(AnonymousUser())
        self.assertIs(response, sentinel)


class FaceViewingTests(TestCase):
    """
    Staff looking at what a student enrolled.

    The point of the screen is that a green pill only says three images passed
    the automatic checks — whether they are of the right student is a judgement
    only a person can make, and they cannot make it without seeing them.
    """

    def setUp(self):
        from academics.models import Batch, Department, StudentProfile

        self.institute = Institute.objects.create(name="I", code="I", email="i@i.edu")
        self.other_institute = Institute.objects.create(
            name="J", code="J", email="j@j.edu")
        self.cse = Department.objects.create(institute=self.institute, name="CSE", code="CSE")
        self.batch = Batch.objects.create(department=self.cse, label="2022-26",
                                          start_year=2022, end_year=2026)

        def user(email, role, institute=None, dept=None):
            return User.objects.create_user(
                email=email, password="Str0ngPass!23", role=role,
                institute=institute or self.institute, department=dept,
                registration_completed=True, full_name=email)

        self.hod = user("hod@i.edu", "HOD", dept=self.cse)
        self.outsider = user("head@j.edu", "HEAD", institute=self.other_institute)
        self.student_user = user("s@i.edu", "STUDENT", dept=self.cse)
        self.student = StudentProfile.objects.create(
            user=self.student_user, department=self.cse, batch=self.batch,
            class_roll="01")

    def _url(self, actor=None):
        client = self.client_class()
        client.force_login(actor or self.hod)
        return client, reverse("academics:api_student_face", args=[self.student.pk])

    def test_a_student_with_no_face_reports_an_empty_list(self):
        """Not a 404 — "nothing on file" is a real answer to show."""
        client, url = self._url()
        data = client.get(url).json()["data"]
        self.assertEqual(data["rows"], [])
        self.assertEqual(data["captured_at"], "")

    def test_staff_from_another_institute_cannot_look(self):
        client, url = self._url(self.outsider)
        self.assertEqual(client.get(url).status_code, 404)

    def test_a_student_cannot_reach_the_staff_endpoint(self):
        client, url = self._url(self.student_user)
        self.assertEqual(client.get(url).status_code, 403)

    def test_the_image_route_is_scoped_the_same_way(self):
        client = self.client_class()
        client.force_login(self.outsider)
        response = client.get(reverse("academics:api_student_face_image",
                                      args=[self.student.pk, "front"]))
        self.assertEqual(response.status_code, 404)

    def test_an_unknown_pose_is_not_found(self):
        client = self.client_class()
        client.force_login(self.hod)
        response = client.get(reverse("academics:api_student_face_image",
                                      args=[self.student.pk, "sideways"]))
        self.assertEqual(response.status_code, 404)


class ResetTests(TestCase):
    """Clearing an enrolment is staff's to do, and it re-arms the gate."""

    def setUp(self):
        self.institute = Institute.objects.create(name="I", code="I", email="i@i.edu")
        self.student = User.objects.create_user(
            email="s@i.edu", password="Str0ngPass!23", role="STUDENT",
            institute=self.institute, registration_completed=True, face_enrolled=True)
        self.hod = User.objects.create_user(
            email="hod@i.edu", password="Str0ngPass!23", role="HOD",
            institute=self.institute, registration_completed=True)

    def test_clearing_sends_the_student_back_to_capture(self):
        self.assertTrue(face_service.clear(user=self.student, actor=self.hod))
        self.student.refresh_from_db()
        self.assertFalse(self.student.face_enrolled)
        self.assertTrue(face_service.needs_enrolment(self.student))

    def test_clearing_someone_with_no_face_reports_nothing_to_do(self):
        self.student.face_enrolled = False
        self.student.save(update_fields=["face_enrolled"])
        self.assertFalse(face_service.clear(user=self.student, actor=self.hod))

    def test_a_second_enrolment_is_refused_while_one_is_on_file(self):
        with self.assertRaises(FaceError) as ctx:
            face_service.enrol(user=self.student, uploads={})
        self.assertEqual(ctx.exception.code, "ALREADY_ENROLLED")

    def test_staff_do_not_enrol(self):
        with self.assertRaises(FaceError) as ctx:
            face_service.enrol(user=self.hod, uploads={})
        self.assertEqual(ctx.exception.code, "NOT_A_STUDENT")

    def test_a_rejected_frame_leaves_the_student_unenrolled(self):
        """
        The flag is what the gate trusts, so it must never be set on a face the
        server did not fully validate.
        """
        self.student.face_enrolled = False
        self.student.save(update_fields=["face_enrolled"])
        with patch.object(face_engine, "analyse",
                          side_effect=FaceError("No face found.", "NO_FACE")):
            with self.assertRaises(FaceError):
                face_service.enrol(
                    user=self.student,
                    uploads={p: _fake_upload() for p in face_service.POSES})
        self.student.refresh_from_db()
        self.assertFalse(self.student.face_enrolled)
        self.assertFalse(self.student.__class__.objects.filter(
            pk=self.student.pk, face_enrolled=True).exists())


def _fake_upload(size=1024):
    from django.core.files.uploadedfile import SimpleUploadedFile

    return SimpleUploadedFile("shot.jpg", b"\xff\xd8\xff" + b"\x00" * size,
                              content_type="image/jpeg")
