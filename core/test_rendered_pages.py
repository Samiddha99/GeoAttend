"""
Checks that need a *rendered page* rather than a JSON payload.

Written after a real bug: the suspend modal was placed inside the template's
`{% if can_manage %}` block, and `can_manage` is HEAD-or-HOD. A university —
the only role that opens that modal — never received the markup, while the
script that did `new bootstrap.Modal("#suspend-modal")` ran regardless. The
page threw on load.

Nothing in the existing suite could see it. The service layer was tested, the
API payload was tested, the template parsed. The gap was that no test had ever
asked a browser-shaped question: *does the element this script reaches for
actually exist for this role?*

So that is what this file asks, generically, for every management page and
every role — not just for the one that broke.
"""
import re

from django.test import TestCase
from django.urls import reverse

from academics.models import Department
from accounts.models import (
    Discipline,
    Institute,
    InstituteAffiliation,
    University,
    UniversityDiscipline,
    User,
)

#: `new bootstrap.Modal("#thing")` — the constructors that *throw* on a missing
#: element rather than quietly doing nothing. `$("#thing")` is not checked:
#: jQuery on an empty set is a no-op, and the selector is far too common.
MODAL = re.compile(
    r"""new\s+bootstrap\.(Modal|Offcanvas|Tooltip|Collapse)\s*\(\s*["']#([\w-]+)["']""")

#: A constructor guarded on the same line — `CAN_MANAGE ? new bootstrap.Modal(…)
#: : null` — has already dealt with the element being absent, so it is not a
#: finding. This is a text check on rendered output, not a JS interpreter: it
#: cannot see a guard several lines up, and would rather stay quiet than cry
#: wolf. A bare `const m = new bootstrap.Modal("#x")` is still caught, which is
#: the shape the bug took.
GUARDED = re.compile(r"[?&|]\s*$")


def _guarded(line, position):
    """Is this constructor call conditional on something on the same line?"""
    return bool(GUARDED.search(line[:position].rstrip()))


class PageFixture(TestCase):
    def setUp(self):
        self.university = University.objects.create(
            name="Engineering University", code="ENGGU", short_name="ENGGU",
            email="e@u.edu", grants_affiliation=True)
        UniversityDiscipline.objects.create(university=self.university,
                                            discipline=Discipline.ENGG)
        self.admin = User.objects.create_user(
            email="admin@u.edu", password="Str0ngPass!23",
            role=User.Role.UNIVERSITY, university=self.university,
            registration_completed=True)

        self.institute = Institute.objects.create(
            name="Acme", code="ACME", email="office@acme.edu",
            status=Institute.Status.APPROVED)
        InstituteAffiliation.objects.create(
            institute=self.institute, discipline=Discipline.ENGG,
            university=self.university)
        self.head = User.objects.create_user(
            email="head@acme.edu", password="Str0ngPass!23",
            role=User.Role.HEAD, institute=self.institute,
            registration_completed=True)
        self.department = Department.objects.create(
            institute=self.institute, code="CSE", name="Computer Science",
            discipline=Discipline.ENGG)
        self.hod = User.objects.create_user(
            email="hod@acme.edu", password="Str0ngPass!23", role=User.Role.HOD,
            institute=self.institute, department=self.department,
            registration_completed=True)
        self.teacher = User.objects.create_user(
            email="teacher@acme.edu", password="Str0ngPass!23",
            role=User.Role.TEACHER, institute=self.institute,
            department=self.department, full_name="Asha Rao",
            registration_completed=True)

    def page(self, user, name):
        self.client.force_login(user)
        response = self.client.get(reverse(name))
        self.assertEqual(response.status_code, 200, f"{name} for {user.role}")
        return response.content.decode()


class WidgetTargetsExistTests(PageFixture):
    """
    Every id a page builds a Bootstrap widget from must be in that same page.

    Bootstrap's constructors do not tolerate a missing element — they throw
    while reading `.backdrop` off `undefined`, which kills the rest of the
    script and leaves the page half-wired with no visible cause.
    """

    PAGES = [
        ("academics:teachers", ["admin", "head", "hod", "teacher"]),
        ("academics:subjects", ["admin", "head", "hod"]),
        ("academics:batches", ["admin", "head", "hod"]),
        ("academics:departments", ["head"]),
        ("academics:students", ["admin", "head", "hod", "teacher"]),
        ("academics:catalogue_departments", ["admin"]),
        ("academics:catalogue_batches", ["admin"]),
        ("academics:catalogue_subjects", ["admin"]),
        ("accounts:profile", ["admin", "head", "hod", "teacher"]),
        ("accounts:institutes", ["admin"]),
    ]

    def test_every_widget_target_is_present_for_every_role(self):
        missing = []
        for name, roles in self.PAGES:
            for role in roles:
                html = self.page(getattr(self, role), name)
                for line in html.splitlines():
                    for match in MODAL.finditer(line):
                        element_id = match.group(2)
                        if _guarded(line, match.start()):
                            continue
                        if f'id="{element_id}"' not in html:
                            missing.append(f"{name} as {role}: #{element_id}")
        self.assertEqual(missing, [], "widgets built from ids that are not in "
                                      "the page:\n  " + "\n  ".join(missing))


class TeacherSuspensionMarkupTests(PageFixture):
    """The specific case, kept as its own test so a failure names itself."""

    def test_the_university_gets_the_suspend_modal(self):
        html = self.page(self.admin, "academics:teachers")
        self.assertIn('id="suspend-modal"', html)
        self.assertIn('id="suspend-form"', html)

    def test_the_university_does_not_get_the_invite_modal_it_cannot_use(self):
        html = self.page(self.admin, "academics:teachers")
        self.assertNotIn('id="form"', html)

    def test_the_head_gets_the_invite_modal_and_not_the_suspend_one(self):
        html = self.page(self.head, "academics:teachers")
        self.assertIn('id="form"', html)
        self.assertNotIn('id="suspend-modal"', html)

    def test_a_teacher_gets_neither(self):
        html = self.page(self.teacher, "academics:teachers")
        self.assertNotIn('id="suspend-modal"', html)
