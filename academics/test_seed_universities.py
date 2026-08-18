"""
The university seeder.

Two properties matter more than the row count: it is idempotent — a second run
must not create a 113th "Anna University" or a second login for one — and the
accounts it makes can actually sign in.
"""
import io

from django.contrib.auth import authenticate, get_user_model
from django.core.management import call_command
from django.test import TestCase

from academics.management.commands.seed_universities import (
    DEFAULT_PASSWORD,
    short_name_for,
)
from accounts.models import University, UniversityDiscipline

User = get_user_model()


def seed(*args):
    out = io.StringIO()
    call_command("seed_universities", *args, stdout=out)
    return out.getvalue()


class ShortNameTests(TestCase):
    def test_it_prefers_the_acronym_the_source_already_gives(self):
        self.assertEqual(
            short_name_for("Dr. A.P.J. Abdul Kalam Technical University (AKTU)"),
            "AKTU")

    def test_it_ignores_a_bracketed_aside_that_is_not_an_acronym(self):
        """
        "Univ. of Agricultural Sciences (Bangalore/Dharwad/Raichur)" is a list
        of campuses, not a name.
        """
        short = short_name_for(
            "Univ. of Agricultural Sciences (Bangalore/Dharwad/Raichur)")
        self.assertNotIn("Bangalore", short)

    def test_it_falls_back_to_initials(self):
        self.assertEqual(short_name_for("Acharya N.G. Ranga Agricultural Univ."),
                         "ANGRA")

    def test_a_one_letter_result_becomes_the_whole_name(self):
        """
        "Anna University" initials to "A", which is no use in an email address
        or a pill. The slug is longer but recognisable.
        """
        self.assertEqual(short_name_for("Anna University"), "anna-university")


class SeedTests(TestCase):
    def test_it_creates_every_body_with_a_login(self):
        seed()
        self.assertEqual(University.objects.count(), 112)
        self.assertEqual(User.objects.filter(role=User.Role.UNIVERSITY).count(), 112)
        self.assertEqual(UniversityDiscipline.objects.count(), 134)

    def test_the_accounts_can_sign_in(self):
        seed()
        account = User.objects.filter(role=User.Role.UNIVERSITY).first()
        self.assertIsNotNone(authenticate(username=account.email,
                                          password=DEFAULT_PASSWORD))
        self.assertIsNone(authenticate(username=account.email, password="wrong"))
        self.assertTrue(account.registration_completed)
        self.assertIsNotNone(account.university_id)

    def test_emails_are_derived_from_the_short_name_and_are_unique(self):
        seed()
        emails = list(User.objects.values_list("email", flat=True))
        self.assertEqual(len(emails), len(set(emails)))
        aktu = University.objects.get(name__startswith="Dr. A.P.J. Abdul Kalam Tech")
        account = User.objects.get(university=aktu)
        self.assertTrue(account.email.startswith("aktu@"), account.email)

    def test_short_names_do_not_collide(self):
        """
        Several bodies initial to the same letters. Two universities showing
        the same pill would be indistinguishable on every screen.
        """
        seed()
        shorts = list(University.objects.values_list("short_name", flat=True))
        self.assertEqual(len(shorts), len(set(shorts)))

    def test_running_twice_changes_nothing(self):
        seed()
        seed()
        self.assertEqual(University.objects.count(), 112)
        self.assertEqual(User.objects.filter(role=User.Role.UNIVERSITY).count(), 112)

    def test_a_dry_run_writes_nothing(self):
        output = seed("--dry-run")
        self.assertIn("Would create 112", output)
        self.assertEqual(University.objects.count(), 0)
        self.assertEqual(User.objects.count(), 0)

    def test_no_accounts_makes_rows_only(self):
        seed("--no-accounts")
        self.assertEqual(University.objects.count(), 112)
        self.assertEqual(User.objects.count(), 0)

    def test_a_later_run_backfills_the_logins(self):
        """A university can exist without one if an earlier run skipped them."""
        seed("--no-accounts")
        seed()
        self.assertEqual(User.objects.filter(role=User.Role.UNIVERSITY).count(), 112)
        self.assertEqual(University.objects.count(), 112)

    def test_a_custom_password_is_used(self):
        seed("--password", "An0ther!Secret9")
        account = User.objects.filter(role=User.Role.UNIVERSITY).first()
        self.assertIsNotNone(authenticate(username=account.email,
                                          password="An0ther!Secret9"))

    def test_the_shared_password_is_called_out(self):
        output = seed()
        self.assertIn(DEFAULT_PASSWORD, output)
        self.assertIn("demo or staging", output)
        # And not when the operator chose their own.
        User.objects.all().delete()
        University.objects.all().delete()
        self.assertNotIn("demo or staging", seed("--password", "An0ther!Secret9"))

    def test_seeding_a_login_marks_the_body_claimed(self):
        """
        The signup form offers *unclaimed* bodies. One with a working login
        must not stay on that list, or a stranger could register against it and
        inherit its institutes.
        """
        seed()
        self.assertEqual(University.objects.filter(claimed_at=None).count(), 0)

    def test_rows_seeded_without_logins_stay_claimable(self):
        seed("--no-accounts")
        self.assertEqual(University.objects.exclude(claimed_at=None).count(), 0)

    def test_it_does_not_overwrite_a_body_that_corrected_its_own_details(self):
        seed("--no-accounts")
        anna = University.objects.get(name="Anna University")
        anna.email = "registrar@annauniv.edu"
        anna.short_name = "AU"
        anna.save()

        seed("--no-accounts")
        anna.refresh_from_db()
        self.assertEqual(anna.email, "registrar@annauniv.edu")
        self.assertEqual(anna.short_name, "AU")
