"""
The university tier: reference data, and the shape of affiliation.

Mostly about the reference data being *coherent* — a district that belongs to
no state, or an affiliating body listed under a discipline that does not exist,
would surface as an empty dropdown on a signup page long after anyone
remembered editing the file.
"""
from django.test import SimpleTestCase, TestCase

from academics import reference
from accounts.models import (
    Discipline,
    Institute,
    InstituteAffiliation,
    University,
    UniversityDiscipline,
    User,
)


class StateReferenceTests(SimpleTestCase):
    def test_states_are_split_into_states_and_union_territories(self):
        groups = reference.states_grouped()
        self.assertEqual([g["label"] for g in groups],
                         ["States", "Union Territories"])
        self.assertIn("Kerala", groups[0]["states"])
        self.assertIn("Lakshadweep", groups[1]["states"])

    def test_every_state_appears_exactly_once(self):
        names = reference.all_states()
        self.assertEqual(len(names), len(set(names)))
        grouped = [s for g in reference.states_grouped() for s in g["states"]]
        self.assertCountEqual(names, grouped)

    def test_every_state_has_at_least_one_district(self):
        empty = [s for s in reference.all_states()
                 if not reference.districts_for(s)]
        self.assertEqual(empty, [])

    def test_districts_are_looked_up_by_state(self):
        self.assertIn("Ernakulam", reference.districts_for("Kerala"))
        self.assertNotIn("Ernakulam", reference.districts_for("Bihar"))

    def test_an_unknown_state_yields_no_districts_rather_than_raising(self):
        self.assertEqual(reference.districts_for("Atlantis"), [])
        self.assertEqual(reference.districts_for(""), [])
        self.assertEqual(reference.districts_for(None), [])

    def test_a_district_is_only_valid_inside_its_own_state(self):
        """
        Validating the two separately would accept "Kerala / Bhopal", which is
        the whole reason this check takes both.
        """
        self.assertTrue(reference.is_valid_place("Kerala", "Ernakulam"))
        self.assertFalse(reference.is_valid_place("Kerala", "Bhopal"))
        self.assertFalse(reference.is_valid_place("", "Ernakulam"))
        self.assertFalse(reference.is_valid_place("Kerala", ""))

    def test_the_browser_payload_covers_every_state(self):
        payload = reference.districts_payload()
        self.assertCountEqual(payload.keys(), reference.all_states())


class SeedListTests(SimpleTestCase):
    def test_every_seeded_discipline_is_a_real_discipline(self):
        """
        A body filed under a discipline the model does not know would never
        appear in any dropdown, and nothing would say so.
        """
        known = set(Discipline.values)
        self.assertTrue(set(reference.seed_universities()).issubset(known),
                        set(reference.seed_universities()) - known)

    def test_every_discipline_has_at_least_one_affiliating_body(self):
        seeded = reference.seed_universities()
        missing = [d for d in Discipline.values if not seeded.get(d)]
        self.assertEqual(missing, [], f"no bodies listed for {missing}")

    def test_names_do_not_repeat_within_a_discipline(self):
        for discipline, names in reference.seed_universities().items():
            with self.subTest(discipline=discipline):
                self.assertEqual(len(names), len(set(names)))


class AffiliationShapeTests(TestCase):
    """Per-discipline affiliation, and what autonomous means."""

    def setUp(self):
        self.institute = Institute.objects.create(
            name="Acme Institute", code="ACME", email="acme@i.edu",
            state="Kerala", district="Ernakulam")
        self.engg = University.objects.create(
            name="Engg University", code="ENGGU", email="e@u.edu")
        UniversityDiscipline.objects.create(university=self.engg,
                                            discipline=Discipline.ENGG)
        self.pharm = University.objects.create(
            name="Health University", code="HEALTHU", email="h@u.edu")
        UniversityDiscipline.objects.create(university=self.pharm,
                                            discipline=Discipline.PHARMACY)

    def test_one_institute_can_answer_to_two_universities(self):
        """
        The reason affiliation is per discipline: an engineering wing and a
        pharmacy wing genuinely have different affiliating bodies, and a single
        `institute.university` would force one of them to be mis-filed.
        """
        InstituteAffiliation.objects.create(
            institute=self.institute, discipline=Discipline.ENGG,
            university=self.engg)
        InstituteAffiliation.objects.create(
            institute=self.institute, discipline=Discipline.PHARMACY,
            university=self.pharm)
        self.assertCountEqual(
            [u.name for u in self.institute.affiliating_universities],
            ["Engg University", "Health University"])

    def test_autonomous_is_a_row_with_no_university_not_a_missing_row(self):
        """
        "Autonomous in engineering" is a claim; "we never said" is not. The
        two have to be distinguishable, which is why the column is nullable
        rather than the row being absent.
        """
        row = InstituteAffiliation.objects.create(
            institute=self.institute, discipline=Discipline.ENGG)
        self.assertTrue(row.is_autonomous)
        self.assertEqual(self.institute.affiliations.count(), 1)
        self.assertEqual(self.institute.affiliating_universities.count(), 0)

    def test_a_discipline_cannot_be_claimed_twice_for_one_institute(self):
        from django.db.utils import IntegrityError

        InstituteAffiliation.objects.create(
            institute=self.institute, discipline=Discipline.ENGG,
            university=self.engg)
        with self.assertRaises(IntegrityError):
            InstituteAffiliation.objects.create(
                institute=self.institute, discipline=Discipline.ENGG,
                university=self.pharm)

    def test_a_university_in_use_cannot_be_deleted_out_from_under_an_institute(self):
        """
        PROTECT rather than CASCADE: deleting a university must not silently
        take its institutes' affiliation records with it.
        """
        from django.db.models import ProtectedError

        InstituteAffiliation.objects.create(
            institute=self.institute, discipline=Discipline.ENGG,
            university=self.engg)
        with self.assertRaises(ProtectedError):
            self.engg.delete()


class InstituteStatusTests(TestCase):
    def test_an_institute_is_approved_unless_something_says_otherwise(self):
        """
        The default has to be APPROVED, not PENDING: every institute that
        already existed got in before there was anyone to ask, and defaulting
        the other way would lock all of them out on the day this shipped.
        """
        institute = Institute.objects.create(
            name="Acme", code="ACME2", email="a2@i.edu")
        self.assertEqual(institute.status, Institute.Status.APPROVED)
        self.assertTrue(institute.is_approved)


class UniversityAccountTests(TestCase):
    def test_a_university_user_belongs_to_a_university_not_an_institute(self):
        university = University.objects.create(
            name="U", code="U", email="u@u.edu")
        user = User.objects.create_user(
            email="admin@u.edu", password="Str0ngPass!23",
            role=User.Role.UNIVERSITY, university=university,
            registration_completed=True)
        self.assertTrue(user.is_university)
        self.assertIsNone(user.institute_id)

    def test_a_university_carries_head_of_institute_authority(self):
        """
        The requirement is that a university has the same read and write reach
        over an institute as its head. `is_institute_admin` is where the two
        roles converge; *scope* is decided by the selectors, never by this.
        """
        university = University.objects.create(
            name="U2", code="U2", email="u2@u.edu")
        user = User.objects.create_user(
            email="admin@u2.edu", password="Str0ngPass!23",
            role=User.Role.UNIVERSITY, university=university)
        self.assertTrue(user.is_institute_admin)
        self.assertTrue(user.is_staff_role)
        # But not a head — the two are still distinguishable where it matters.
        self.assertFalse(user.is_head)

    def test_a_seeded_body_is_unclaimed_until_someone_signs_up(self):
        university = University.objects.create(
            name="U3", code="U3", email="u3@unclaimed.invalid", is_seeded=True)
        self.assertFalse(university.is_claimed)
