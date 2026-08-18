"""
Load the shipped list of affiliating universities and boards, and give each one
a login.

Idempotent: matches on name, creates what is missing, adds any discipline a
body has gained, and creates an account only for a university that has none.
It never deletes and never overwrites an existing row's details — the file is a
starting list, not the authority. Once a university has corrected its own name
or email, re-running this must not undo that.

    manage.py seed_universities                     # create, update, add logins
    manage.py seed_universities --dry-run           # report, write nothing
    manage.py seed_universities --no-accounts       # rows only, no logins
    manage.py seed_universities --password '…'      # something other than the default

**On the shared password.** Every account this creates has the same one. That
is fine for a demo or a staging load and is a liability anywhere real: one
leaked password is 112 universities, each of which can read every institute
affiliated to it. The command says so on every run, and `--password` is there
so a real load can use something else.

**On claiming.** Creating a login marks the university claimed. The signup
form offers *unclaimed* seeded bodies so that "Anna University" ends up as one
account rather than two; leaving a row unclaimed while it already had a working
login would let a stranger register against it and inherit its institutes.
"""
import re

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from academics.reference import seed_universities
from accounts.models import University, UniversityDiscipline

DEFAULT_PASSWORD = "Passw0rd!23"
DEFAULT_DOMAIN = "university.geoattend.local"

# Bracketed asides that are descriptions, not acronyms. Without this,
# "Univ. of Agricultural Sciences (Bangalore/Dharwad/Raichur)" would call
# itself "Bangalore/Dharwad/Raichur".
NOT_AN_ACRONYM = re.compile(
    r"^(also|bangalore|vet|fisheries|polytechnic|sbtet|sbte)\b", re.I)
SKIP_WORDS = {"of", "and", "the", "for", "in", "university", "univ", "institute"}


def short_name_for(name):
    """
    A usable short name, in three fallbacks.

    1. The acronym the source list already puts in brackets — "…(AKTU)" → AKTU.
       Best, because it is what the university actually calls itself.
    2. Initials of the significant words, if that yields something a person
       could recognise.
    3. A slug of the whole name. "Anna University" initials to "A", which is no
       use to anyone, so it becomes "anna-university" instead.
    """
    for token in re.findall(r"\(([^)]*)\)", name):
        token = token.strip()
        if NOT_AN_ACRONYM.match(token):
            continue
        if re.fullmatch(r"[A-Za-z0-9&.\- ]{2,20}", token) and any(
                c.isupper() for c in token):
            return token

    words = re.sub(r"[^\w\s]", " ", name).split()
    initials = "".join(w[0] for w in words if w.lower() not in SKIP_WORDS).upper()
    if len(initials) >= 3:
        return initials[:12]
    return slugify(name)[:30]


def unique_value(base, taken, limit=30, fallback="university"):
    """A slug-safe value not already used, suffixed only if it has to be."""
    base = slugify(base)[:limit].strip("-") or fallback
    value = base
    n = 2
    while value in taken:
        suffix = f"-{n}"
        value = f"{base[:limit - len(suffix)]}{suffix}"
        n += 1
    taken.add(value)
    return value


def placeholder_email(code):
    """
    The university's *contact* address before anyone sets a real one.

    On `.invalid`, reserved by RFC 2606, so a seeded row can never be mistaken
    for one with a working mailbox and nothing is ever delivered to it. This is
    not the login — see `login_email`.
    """
    return f"{code}@unclaimed.invalid"


def login_email(short_name, domain, taken):
    """The account address, derived from the short name and deduped."""
    local = unique_value(short_name, taken, limit=40, fallback="university")
    return f"{local}@{domain}"


class Command(BaseCommand):
    help = ("Create or update the shipped affiliating universities and boards, "
            "each with a login.")

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Report what would change and write nothing.")
        parser.add_argument("--no-accounts", action="store_true",
                            help="Create the university rows but no logins.")
        parser.add_argument("--password", default=DEFAULT_PASSWORD,
                            help="Password for every account it creates.")
        parser.add_argument("--email-domain", default=DEFAULT_DOMAIN,
                            help="Domain for the generated login addresses.")

    def handle(self, *args, **options):
        dry = options["dry_run"]
        make_accounts = not options["no_accounts"]
        password = options["password"]
        domain = options["email_domain"].strip().lstrip("@")
        User = get_user_model()

        # One row per distinct name: a body listed under three disciplines is
        # one university with three disciplines, not three universities.
        wanted = {}
        for discipline, names in seed_universities().items():
            for name in names:
                wanted.setdefault(name, set()).add(discipline)

        with transaction.atomic():
            existing = {u.name: u for u in University.objects.all()}
            codes = {u.code for u in existing.values()}
            logins = set(User.objects.values_list("email", flat=True))
            # Short names are not unique in the database, but two universities
            # sharing one would produce two indistinguishable pills on screen,
            # so they are deduped as well.
            shorts = {u.short_name for u in existing.values() if u.short_name}
            has_account = set(
                User.objects.filter(role=User.Role.UNIVERSITY)
                .exclude(university=None).values_list("university_id", flat=True))

            created = updated = links = accounts = 0
            for name, disciplines in sorted(wanted.items()):
                university = existing.get(name)

                if university is None:
                    created += 1
                    code = unique_value(name, codes, limit=26)
                    short = unique_value(short_name_for(name), shorts, limit=40)
                    if dry:
                        links += len(disciplines)
                        if make_accounts:
                            accounts += 1
                            self.stdout.write(
                                f"  + {name}\n      {login_email(short, domain, logins)}")
                        else:
                            self.stdout.write(f"  + {name}")
                        continue
                    university = University.objects.create(
                        name=name, code=code, short_name=short,
                        email=placeholder_email(code),
                        is_seeded=True, grants_affiliation=True)
                    self.stdout.write(f"  + {name}")

                have = set(university.disciplines.values_list("discipline", flat=True))
                missing = disciplines - have
                if missing:
                    links += len(missing)
                    if university.name in existing:
                        updated += 1
                        self.stdout.write(f"  ~ {name}  + {', '.join(sorted(missing))}")
                    if not dry:
                        UniversityDiscipline.objects.bulk_create([
                            UniversityDiscipline(university=university, discipline=d)
                            for d in sorted(missing)])

                if not make_accounts or university.pk in has_account:
                    continue
                # A university may exist without a login if an earlier run used
                # --no-accounts, so this is not folded into the create above.
                short = university.short_name or unique_value(
                    short_name_for(name), shorts, limit=40)
                email = login_email(short, domain, logins)
                accounts += 1
                if dry:
                    self.stdout.write(f"      login {email}")
                    continue
                User.objects.create_user(
                    email=email, password=password,
                    full_name=f"{short} administrator",
                    role=User.Role.UNIVERSITY, university=university,
                    email_verified=True, registration_completed=True)
                # Claimed, because it now has a working login. Leaving it
                # unclaimed would keep it on the signup form's "are you on our
                # list?" dropdown, where a stranger could register against it
                # and inherit its institutes.
                university.claimed_at = university.claimed_at or timezone.now()
                if not university.short_name:
                    university.short_name = short
                university.save(update_fields=["claimed_at", "short_name"])
                self.stdout.write(f"      login {email}")

            verb = "Would create" if dry else "Created"
            self.stdout.write(self.style.SUCCESS(
                f"{verb} {created} universit{'y' if created == 1 else 'ies'}, "
                f"{'would update' if dry else 'updated'} {updated}, "
                f"{links} discipline link{'' if links == 1 else 's'}, "
                f"{accounts} login{'' if accounts == 1 else 's'}."))

            if accounts and password == DEFAULT_PASSWORD:
                self.stdout.write(self.style.WARNING(
                    f"\n  {accounts} accounts share the password "
                    f"'{DEFAULT_PASSWORD}'.\n"
                    "  Each one can read every institute affiliated to that "
                    "university, so this is a demo or staging load only.\n"
                    "  Use --password for anything real, and change them "
                    "before the data is."))
            if dry:
                transaction.set_rollback(True)
