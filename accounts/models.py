import datetime as dt

from django.conf import settings
from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.db import models
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver
from django.utils import timezone

from core.enums import RowStatus, revoked_field, status_field
from core.utils import normalise_email, numeric_otp, random_token, sha256

from .managers import UserManager


class Discipline(models.TextChoices):
    """
    The broad fields an institute teaches and a university grants affiliation
    for.

    Declared alphabetically by label, which is also the order they are offered
    in. The codes are short because they end up in query strings and in a
    per-discipline affiliation row for every institute.
    """

    AGRI = "AGRI", "Agriculture, Veterinary & Allied Sciences"
    DIPLOMA = "DIPLOMA", "Diploma (Polytechnic & ITI)"
    ENGG = "ENGG", "Engineering, Technology & Management"
    GENERAL = "GENERAL", "General Courses (Arts, Science, Commerce)"
    MEDICAL = "MEDICAL", "Medical, Health Sciences, Ayush, Nursing & Paramedical"
    PHARMACY = "PHARMACY", "Pharmacy"


class University(models.Model):
    """
    An affiliating university or examination board.

    Holds its own account, so it is a tenant in its own right rather than a
    lookup row. Two flags are worth separating:

    * `grants_affiliation` — whether institutes may name it as their
      affiliating body. A university that only wants to invite its own
      institutes (a private university, say) sets this False and never appears
      in the institute signup dropdown.
    * `is_active` — whether the account works at all.

    Disciplines are a many-to-many in effect: several bodies in the shipped
    list grant affiliation for more than one field, and an institute chooses
    an affiliating body *per discipline*.
    """

    name = models.CharField(max_length=200, unique=True)
    short_name = models.CharField(max_length=40, blank=True)
    code = models.SlugField(max_length=30, unique=True)
    email = models.EmailField(unique=True, help_text="Official university email")
    phone = models.CharField(max_length=20, blank=True)
    website = models.URLField(blank=True)
    address = models.TextField(blank=True)
    state = models.CharField(max_length=60, blank=True)
    district = models.CharField(max_length=80, blank=True)
    logo = models.ImageField(upload_to="university/", blank=True, null=True)

    grants_affiliation = models.BooleanField(
        default=True,
        help_text="Institutes may name this body as their affiliating "
                  "university. Turn off for a university that only takes the "
                  "institutes it invites.")
    is_active = models.BooleanField(default=True)
    # True for the ~112 bodies shipped with the app. A seeded row exists before
    # anyone claims it, so signup matches an existing name instead of creating
    # a near-duplicate — "Anna University" and "Anna Univ." would otherwise
    # both exist and split their institutes between them.
    is_seeded = models.BooleanField(default=False)
    claimed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "Universities"

    def __str__(self):
        return self.short_name or self.name

    @property
    def is_claimed(self):
        return self.claimed_at is not None


class UniversityDiscipline(models.Model):
    """
    One discipline a university covers.

    A join table rather than a comma-separated column because the institute
    signup dropdown is "affiliating bodies for *this* discipline", which is a
    query, not a string search.
    """

    university = models.ForeignKey(University, on_delete=models.CASCADE,
                                   related_name="disciplines")
    discipline = models.CharField(max_length=12, choices=Discipline.choices,
                                  db_index=True)

    class Meta:
        ordering = ["discipline"]
        constraints = [
            models.UniqueConstraint(fields=["university", "discipline"],
                                    name="uniq_university_discipline"),
        ]

    def __str__(self):
        return f"{self.university} · {self.get_discipline_display()}"


class Institute(models.Model):
    """A college.  Created by the Head of the Institute, or invited by a university."""

    class Status(models.TextChoices):
        """
        Where an institute sits in the approval flow.

        An institute that names an affiliating university starts PENDING and
        that university decides. One that is autonomous in every discipline, or
        that a university invited directly, is APPROVED from the start —
        there is nobody left to ask.
        """

        PENDING = "PENDING", "Awaiting approval"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"

    name = models.CharField(max_length=200)
    code = models.SlugField(max_length=30, unique=True)
    email = models.EmailField(unique=True, help_text="Official institute email")
    phone = models.CharField(max_length=20, blank=True)
    website = models.URLField(blank=True)
    address = models.TextField(blank=True)
    # Set at signup and not editable afterwards by the institute: an institute
    # that could move itself between states could move out from under the
    # university that approved it.
    state = models.CharField(max_length=60, blank=True, db_index=True)
    district = models.CharField(max_length=80, blank=True, db_index=True)
    logo = models.ImageField(upload_to="institute/", blank=True, null=True)

    status = models.CharField(max_length=10, choices=Status.choices,
                              default=Status.APPROVED, db_index=True)
    # Free text, shown to the institute verbatim in the rejection email. Kept
    # rather than cleared on a later approval, so the history stays readable.
    rejection_reason = models.TextField(blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        blank=True, related_name="institute_decisions")
    # Set when a university created this institute rather than the institute
    # registering itself. Distinct from affiliation: a university may invite an
    # institute it does not affiliate.
    invited_by = models.ForeignKey(
        University, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="invited_institutes")

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def is_approved(self):
        return self.status == self.Status.APPROVED

    @property
    def affiliating_universities(self):
        """Every university that affiliates this institute, without repeats."""
        return University.objects.filter(
            affiliated_institutes__institute=self).distinct()


class InstituteAffiliation(models.Model):
    """
    One discipline an institute teaches, and who affiliates it for that.

    Per discipline because that is how affiliation actually works: an institute
    with an engineering wing and a pharmacy wing answers to two different
    bodies, and a single `institute.university` would force it to mis-file one
    of them.

    `university = NULL` means autonomous *for this discipline* — which is a
    real state, not a missing value, and is why the column is nullable rather
    than the row being absent.
    """

    institute = models.ForeignKey(Institute, on_delete=models.CASCADE,
                                  related_name="affiliations")
    discipline = models.CharField(max_length=12, choices=Discipline.choices,
                                  db_index=True)
    university = models.ForeignKey(
        University, on_delete=models.PROTECT, null=True, blank=True,
        related_name="affiliated_institutes",
        help_text="Blank means autonomous for this discipline.")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["discipline"]
        constraints = [
            models.UniqueConstraint(fields=["institute", "discipline"],
                                    name="uniq_institute_discipline"),
        ]

    def __str__(self):
        where = self.university or "Autonomous"
        return f"{self.institute} · {self.get_discipline_display()} · {where}"

    @property
    def is_autonomous(self):
        return self.university_id is None


@receiver([post_save, post_delete], sender=InstituteAffiliation)
def _resync_revoked(sender, instance, **kwargs):
    """
    Keep `is_revoked` true to the affiliation table.

    A stored denormalised flag is only worth having if nothing can change the
    thing it summarises without it noticing. The services call `sync_revoked`
    themselves, but affiliations are also created directly — by the demo
    seeder, by the admin, by a shell session, by every test fixture — and each
    of those left the flag stale and the screens wrong.

    A signal is the one place that cannot be bypassed. It is a handful of bulk
    updates on an action that already reshapes the institute, so the cost is
    nothing next to being reliably correct.
    """
    from academics.curriculum import sync_revoked

    if instance.institute_id:
        sync_revoked(instance.institute)


class User(AbstractBaseUser, PermissionsMixin):
    class Role(models.TextChoices):
        HEAD = "HEAD", "Head of Institute"
        HOD = "HOD", "Head of Department"
        TEACHER = "TEACHER", "Teacher"
        STUDENT = "STUDENT", "Student"
        GUARDIAN = "GUARDIAN", "Guardian"
        UNIVERSITY = "UNIVERSITY", "University"

    email = models.EmailField(unique=True)
    full_name = models.CharField(max_length=150, blank=True)
    phone = models.CharField(max_length=20, blank=True)
    # 12, not 10: "UNIVERSITY" is exactly 10 and would fit, but a column with
    # no headroom turns the next role into a migration nobody expected.
    role = models.CharField(max_length=12, choices=Role.choices, db_index=True)

    institute = models.ForeignKey(
        Institute, on_delete=models.CASCADE, null=True, blank=True, related_name="users"
    )
    # Set only on UNIVERSITY accounts, and mutually exclusive with `institute`
    # in practice: a university user belongs to no single institute, which is
    # the whole point of the role.
    university = models.ForeignKey(
        "accounts.University", on_delete=models.CASCADE, null=True, blank=True,
        related_name="users")
    department = models.ForeignKey(
        "academics.Department",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="members",
    )

    is_active = models.BooleanField(default=True)
    # Lifecycle and revocation, the same two fields every scoped row carries.
    # For a person INVITED is a real state: the account exists because somebody
    # was invited and has not finished signing up.
    status = status_field()
    is_revoked = revoked_field()
    # A teacher switched off by a discipline removal — see the identical field
    # on academics.Department for why the memory is needed. Only ever set for
    # teachers; nothing else is touched by that operation.
    archived_with_discipline = models.BooleanField(default=False, editable=False)

    # ---- identity, for teachers -------------------------------------------- #
    #
    # A teacher's PAN is the one identifier that is the *same person* across
    # institutes. Email is not: somebody changes jobs and gets a new one, and
    # two colleges would each hold a different account for one teacher with no
    # way to know. So the rule that only one college may run a teacher at a
    # time is keyed on this, not on the login.
    #
    # Blank for every other role and for the rows that predate this, which is
    # why it is `blank=True` rather than required at the column: making it
    # mandatory would have needed a value invented for every existing teacher,
    # and an invented PAN is worse than an absent one.
    #
    # Not unique at the database level. The rule is "one *non-archived* holder",
    # which is a condition over a column that changes — see accounts/pan.py for
    # how it is enforced and for the race it cannot close on its own.
    pan_number = models.CharField(
        max_length=10, blank=True, db_index=True,
        help_text="Permanent Account Number. Fixed once saved.")
    date_of_birth = models.DateField(
        null=True, blank=True,
        help_text="Checked against the PAN. Fixed once saved.")

    # ---- suspension, by the affiliating university ------------------------ #
    #
    # **A fourth orthogonal fact, not a fourth status.** `status` says what the
    # institute has done with this account (active, invited, archived);
    # `is_revoked` says the discipline underneath it is gone; this says the
    # affiliating university has barred the person. Three different parties,
    # three different facts, and folding any of them into the others is the
    # mistake recorded at the top of core/enums.py — a suspension written over
    # `status` would leave nothing to restore when it was lifted, and would
    # make "we archived them" and "they were suspended" the same row.
    #
    # Only the university that affiliates the teacher's department may set or
    # clear it. That is the whole point: an institute able to lift it would
    # make it a note rather than a sanction.
    is_suspended = models.BooleanField(
        default=False, db_index=True,
        help_text="Barred by the affiliating university. Independent of "
                  "status: a suspended teacher keeps whatever status they had.")
    suspension_reason = models.TextField(blank=True)
    suspended_at = models.DateTimeField(null=True, blank=True)
    # The university rather than the person who clicked: administrators come
    # and go, and the question "may this account lift the suspension" is about
    # the body, not the individual.
    suspended_by = models.ForeignKey(
        "accounts.University", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="suspended_teachers")
    is_staff = models.BooleanField(default=False)
    email_verified = models.BooleanField(default=False)
    registration_completed = models.BooleanField(
        default=False, help_text="False until the invitee sets their password."
    )
    # Denormalised from FaceEnrolment on purpose: the gate middleware reads it
    # on every request, and a join per request to answer "has this student
    # enrolled" is a query the app should not be making.
    face_enrolled = models.BooleanField(
        default=False,
        help_text="Student has captured the three enrolment images.",
    )

    # A guardian signs in with this number and a WhatsApp code — there is no
    # password. Unique so one number is one account however many children it
    # covers, and NULL (not "") for everyone else, because a unique column
    # cannot hold two empty strings.
    guardian_mobile = models.CharField(
        max_length=20, null=True, blank=True, unique=True, db_index=True,
        help_text="Set only on guardian accounts. The number is the login.",
    )

    # anti proxy-attendance: a student may only mark from their bound device
    device_id = models.CharField(max_length=64, blank=True, db_index=True)
    device_bound_at = models.DateTimeField(null=True, blank=True)

    date_joined = models.DateTimeField(default=timezone.now)
    last_seen = models.DateTimeField(null=True, blank=True)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    class Meta:
        ordering = ["full_name", "email"]
        indexes = [
            models.Index(fields=["role", "institute"]),
            models.Index(fields=["role", "department"]),
        ]

    def __str__(self):
        return self.full_name or self.email

    def save(self, *args, **kwargs):
        self.email = normalise_email(self.email)
        # The same one-directional sync every scoped model uses: `is_active`
        # decides running-vs-archived, and for a person `registration_completed`
        # supplies the third state. Without this an account created with
        # `is_active=False` kept status ACTIVE and was counted as staff.
        if not self.is_active:
            self.status = RowStatus.ARCHIVED
        elif not self.registration_completed:
            self.status = RowStatus.INVITED
        elif self.status != RowStatus.ACTIVE:
            self.status = RowStatus.ACTIVE
        # Revocation is the department's fact; a teacher carries a copy so the
        # staff list can filter on it without a join.
        if self.department_id and self.role == self.Role.TEACHER:
            self.is_revoked = self.department.is_revoked
        super().save(*args, **kwargs)

    # ---- convenience ------------------------------------------------------ #
    @property
    def is_head(self):
        return self.role == self.Role.HEAD

    @property
    def is_hod(self):
        return self.role == self.Role.HOD

    @property
    def is_teacher(self):
        return self.role == self.Role.TEACHER

    @property
    def is_student(self):
        return self.role == self.Role.STUDENT

    @property
    def is_guardian(self):
        return self.role == self.Role.GUARDIAN

    @property
    def is_university(self):
        return self.role == self.Role.UNIVERSITY

    @property
    def is_staff_role(self):
        """The roles that administer anything, at either tier."""
        return self.role in (self.Role.HEAD, self.Role.HOD, self.Role.TEACHER,
                             self.Role.UNIVERSITY)

    @property
    def is_institute_admin(self):
        """
        Head-of-institute authority, whoever is exercising it.

        A university has the same read and write reach over an institute as its
        head — that is the requirement — so the two answer this the same way.
        The *scope* they may exercise it over still differs, and that is
        decided by the selectors, never by this flag.
        """
        return self.role in (self.Role.HEAD, self.Role.UNIVERSITY)

    @property
    def short_name(self):
        return (self.full_name or self.email).split(" ")[0]

    @property
    def initials(self):
        parts = [p for p in (self.full_name or self.email).replace(".", " ").split(" ") if p]
        return "".join(p[0] for p in parts[:2]).upper() or "U"

    def get_full_name(self):
        return self.full_name or self.email

    def get_short_name(self):
        return self.short_name

    def bind_device(self, fingerprint):
        if fingerprint and not self.device_id:
            self.device_id = fingerprint
            self.device_bound_at = timezone.now()
            self.save(update_fields=["device_id", "device_bound_at"])


class EmailOTP(models.Model):
    """One-time codes for institute registration & password reset."""

    class Purpose(models.TextChoices):
        INSTITUTE_SIGNUP = "SIGNUP", "Institute signup"
        PASSWORD_RESET = "RESET", "Password reset"
        EMAIL_CHANGE = "CHANGE", "Email change"

    email = models.EmailField(db_index=True)
    purpose = models.CharField(max_length=10, choices=Purpose.choices)
    code_hash = models.CharField(max_length=64)
    payload = models.JSONField(default=dict, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    is_used = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["email", "purpose", "is_used"])]

    def __str__(self):
        return f"{self.email} · {self.purpose}"

    # ---- factory & validation -------------------------------------------- #
    @classmethod
    def issue(cls, email, purpose, payload=None, ttl_minutes=None):
        email = normalise_email(email)
        cls.objects.filter(email=email, purpose=purpose, is_used=False).update(is_used=True)
        code = numeric_otp(6)
        ttl = ttl_minutes or settings.OTP_TTL_MINUTES
        otp = cls.objects.create(
            email=email,
            purpose=purpose,
            code_hash=sha256(code),
            payload=payload or {},
            expires_at=timezone.now() + dt.timedelta(minutes=ttl),
        )
        return otp, code

    @property
    def is_expired(self):
        return timezone.now() > self.expires_at

    def verify(self, code):
        """Returns (ok, message)."""
        if self.is_used:
            return False, "This code has already been used."
        if self.is_expired:
            return False, "This code has expired. Please request a new one."
        if self.attempts >= settings.OTP_MAX_ATTEMPTS:
            return False, "Too many incorrect attempts. Please request a new code."
        if sha256(str(code).strip()) != self.code_hash:
            self.attempts += 1
            self.save(update_fields=["attempts"])
            left = max(settings.OTP_MAX_ATTEMPTS - self.attempts, 0)
            return False, f"Incorrect code. {left} attempt(s) left."
        self.is_used = True
        self.save(update_fields=["is_used"])
        return True, "Verified."


class PhoneOTP(models.Model):
    """
    One-time codes sent over WhatsApp. Currently only guardians sign in this
    way, which is why the code is the entire credential.

    Deliberately a separate model from EmailOTP rather than a nullable column
    on it. The two have different threat models: an email code lands in a
    mailbox behind its own password, while this one is the *only* thing between
    a phone number and a child's attendance record. It gets its own resend
    ceiling and its own lockout, and sharing a table would have meant one set
    of limits governing both.
    """

    class Purpose(models.TextChoices):
        GUARDIAN_LOGIN = "GLOGIN", "Guardian sign-in"

    mobile = models.CharField(max_length=20, db_index=True)
    purpose = models.CharField(max_length=10, choices=Purpose.choices,
                               default=Purpose.GUARDIAN_LOGIN)
    code_hash = models.CharField(max_length=64)
    attempts = models.PositiveSmallIntegerField(default=0)
    # Counted so that a number cannot be used to send unlimited WhatsApp
    # messages to whoever owns it. Kept on the record rather than in the
    # session: the session belongs to the sender, who is the problem.
    sends = models.PositiveSmallIntegerField(default=1)
    is_used = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    last_sent_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["mobile", "purpose", "is_used"])]

    def __str__(self):
        return f"{self.mobile} · {self.purpose}"

    @classmethod
    def issue(cls, mobile, purpose=Purpose.GUARDIAN_LOGIN, ttl_minutes=None):
        """Retire any code still outstanding for this number, then mint one."""
        cls.objects.filter(mobile=mobile, purpose=purpose,
                           is_used=False).update(is_used=True)
        code = numeric_otp(6)
        ttl = ttl_minutes or getattr(settings, "PHONE_OTP_TTL_MINUTES",
                                     settings.OTP_TTL_MINUTES)
        otp = cls.objects.create(
            mobile=mobile, purpose=purpose, code_hash=sha256(code),
            expires_at=timezone.now() + dt.timedelta(minutes=ttl),
        )
        return otp, code

    @property
    def is_expired(self):
        return timezone.now() > self.expires_at

    @property
    def max_sends(self):
        return getattr(settings, "PHONE_OTP_MAX_SENDS", 5)

    @property
    def resend_wait(self):
        """Seconds left before another code may be sent, 0 if it may go now."""
        gap = getattr(settings, "PHONE_OTP_RESEND_SECONDS", 60)
        elapsed = (timezone.now() - self.last_sent_at).total_seconds()
        return max(0, int(gap - elapsed))

    def resend(self):
        """
        A fresh code on the same record, so the ceiling still applies.

        Returns (code, error). Minting a *new* record on every resend would
        reset both the send count and the attempt count, which is the whole
        thing the limits exist to prevent.
        """
        if self.sends >= self.max_sends:
            return None, ("Too many codes requested for this number. "
                          "Please try again later.")
        if self.resend_wait:
            return None, (f"Please wait {self.resend_wait} seconds before "
                          "asking for another code.")
        code = numeric_otp(6)
        ttl = getattr(settings, "PHONE_OTP_TTL_MINUTES", settings.OTP_TTL_MINUTES)
        self.code_hash = sha256(code)
        self.sends += 1
        self.attempts = 0
        self.last_sent_at = timezone.now()
        self.expires_at = timezone.now() + dt.timedelta(minutes=ttl)
        self.save(update_fields=["code_hash", "sends", "attempts",
                                 "last_sent_at", "expires_at"])
        return code, None

    def verify(self, code):
        """Returns (ok, message) — the same shape as EmailOTP.verify."""
        if self.is_used:
            return False, "This code has already been used."
        if self.is_expired:
            return False, "This code has expired. Please request a new one."
        if self.attempts >= settings.OTP_MAX_ATTEMPTS:
            return False, "Too many incorrect attempts. Please request a new code."
        if sha256(str(code).strip()) != self.code_hash:
            self.attempts += 1
            self.save(update_fields=["attempts"])
            left = max(settings.OTP_MAX_ATTEMPTS - self.attempts, 0)
            return False, f"Incorrect code. {left} attempt(s) left."
        self.is_used = True
        self.save(update_fields=["is_used"])
        return True, "Verified."


class Invitation(models.Model):
    """
    The *only* door into the system for HoDs, teachers and students.
    A person can never self-register: their email must have been added by the
    Head (for HoDs) or by a HoD (for teachers/students).
    """

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        ACCEPTED = "ACCEPTED", "Accepted"
        REVOKED = "REVOKED", "Revoked"
        EXPIRED = "EXPIRED", "Expired"

    email = models.EmailField(db_index=True)
    full_name = models.CharField(max_length=150, blank=True)
    # Widened alongside User.role — the two share a vocabulary, so a role that
    # fits one and not the other is a bug waiting for the right invitation.
    role = models.CharField(max_length=12, choices=User.Role.choices)
    institute = models.ForeignKey(Institute, on_delete=models.CASCADE, related_name="invitations")
    department = models.ForeignKey(
        "academics.Department", on_delete=models.CASCADE, null=True, blank=True,
        related_name="invitations",
    )
    token = models.CharField(max_length=64, unique=True, default=random_token)
    payload = models.JSONField(default=dict, blank=True)  # subjects, batch, roll no...
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="invitations_sent"
    )
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True,
        related_name="invitation",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    sent_count = models.PositiveSmallIntegerField(default=0)
    last_sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["email", "status"])]

    def __str__(self):
        return f"{self.email} → {self.role} ({self.status})"

    def save(self, *args, **kwargs):
        self.email = normalise_email(self.email)
        if not self.expires_at:
            self.expires_at = timezone.now() + dt.timedelta(days=settings.INVITE_TTL_DAYS)
        super().save(*args, **kwargs)

    @property
    def is_expired(self):
        return timezone.now() > self.expires_at

    @property
    def is_usable(self):
        return self.status == self.Status.PENDING and not self.is_expired

    @property
    def accept_url(self):
        return f"{settings.SITE_URL}/auth/invite/{self.token}/"

    def refresh_token(self, extra_days=None):
        self.token = random_token()
        self.expires_at = timezone.now() + dt.timedelta(days=extra_days or settings.INVITE_TTL_DAYS)
        self.status = self.Status.PENDING
        self.save(update_fields=["token", "expires_at", "status"])
        return self

    def accept(self, user):
        self.status = self.Status.ACCEPTED
        self.accepted_at = timezone.now()
        self.user = user
        self.save(update_fields=["status", "accepted_at", "user"])


class ActivityLog(models.Model):
    """Lightweight audit trail — who did what, from where."""

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="activities"
    )
    institute = models.ForeignKey(
        Institute, on_delete=models.CASCADE, null=True, blank=True, related_name="activities"
    )
    action = models.CharField(max_length=64, db_index=True)
    detail = models.TextField(blank=True)
    meta = models.JSONField(default=dict, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.action}"

    @classmethod
    def log(cls, request=None, actor=None, action="", detail="", **meta):
        from core.http import client_ip

        actor = actor or (getattr(request, "user", None) if request else None)
        if actor is not None and not getattr(actor, "is_authenticated", False):
            actor = None
        ip = client_ip(request) if request else None
        return cls.objects.create(
            actor=actor,
            institute=getattr(actor, "institute", None) if actor else None,
            action=action,
            detail=detail,
            meta=meta or {},
            ip=ip or None,
        )


def face_image_path(instance, filename):
    """
    One enrolment image. Foldered per user so a deletion request is a directory
    operation, and named by pose so a human can tell the three apart. The
    original filename is discarded — it came from a canvas blob and carries no
    information worth keeping.
    """
    return f"faces/{instance.enrolment.user_id}/{instance.pose.lower()}.jpg"


class FaceEnrolment(models.Model):
    """
    A student's face on file: three images from three angles, and the vector
    computed from each.

    One per student. Re-capturing is not self-service — staff clear this first,
    exactly like unlinking a device — because a student who can re-enrol at will
    can enrol whoever they like an hour before class.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="face_enrolment"
    )
    created_at = models.DateTimeField(default=timezone.now)
    # Which model produced the vectors. Embeddings from different models are
    # not comparable, so a stored name is what lets a future upgrade know which
    # rows still need recomputing.
    model_name = models.CharField(max_length=40, blank=True)

    # Cleared and re-armed by staff; kept for the audit trail rather than
    # deleted, so "who let this student re-enrol, and when" has an answer.
    reset_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="face_resets_performed",
    )
    reset_at = models.DateTimeField(null=True, blank=True)
    reset_reason = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Face enrolment · {self.user.email}"

    @property
    def is_complete(self):
        return self.samples.count() >= len(FaceSample.Pose.values)


class FaceSample(models.Model):
    """One captured angle, its image, and the vector derived from it."""

    class Pose(models.TextChoices):
        FRONT = "FRONT", "Looking straight ahead"
        LEFT = "LEFT", "Turned slightly left"
        RIGHT = "RIGHT", "Turned slightly right"

    enrolment = models.ForeignKey(
        FaceEnrolment, on_delete=models.CASCADE, related_name="samples"
    )
    pose = models.CharField(max_length=6, choices=Pose.choices)
    image = models.ImageField(upload_to=face_image_path, max_length=300)
    # A list of floats. Stored rather than recomputed because extracting one is
    # ~a second of CPU, and marking attendance cannot afford three of those per
    # student on top of the live frame.
    embedding = models.JSONField(default=list)
    # What the server measured, not what the browser claimed — kept so a
    # rejected match can be investigated without re-running the model.
    yaw = models.FloatField(default=0)
    detect_score = models.FloatField(default=0)
    captured_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["pose"]
        constraints = [
            models.UniqueConstraint(fields=["enrolment", "pose"],
                                    name="uniq_face_sample_per_pose"),
        ]

    def __str__(self):
        return f"{self.enrolment.user.email} · {self.pose}"
