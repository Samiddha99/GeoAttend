"""
Guardian sign-in: a phone number, a WhatsApp code, and read-only access to a
child's record.

Three things make this different from every other login in the app and are
worth stating once, here, rather than rediscovering them in each view.

**The number is the whole credential.** There is no password to get wrong and
no invitation to accept. Whoever controls the WhatsApp number on a student's
record can see that student's attendance. That is the deliberate trade — the
alternative is an onboarding step most guardians never complete — but it means
`guardian_mobile` in the student table is now security-relevant data, not just
a contact detail. Staff editing it are changing who can sign in.

**The account is derived, not registered.** A guardian User row is created the
first time a code is verified and exists only to give Django's session
machinery something to hold. It carries no password (`set_unusable_password`),
and its email is synthetic. Nothing about the account grants access: access is
recomputed from the student table on every request, so removing a number from a
student record removes the guardian's view of that child immediately.

**Access is read-only, and that is enforced on the server.** Hiding buttons is
presentation. `guardian_readonly` is what actually refuses the write.
"""
import datetime as dt

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from notifications.whatsapp import normalise_msisdn

SESSION_CHILD_KEY = "guardian_child_id"

# How the code arrives. Plain text rather than a Twilio content template
# because a login code is time-critical: template approval is per-account and
# a pending template silently sends nothing.
OTP_MESSAGE = (
    "{code} is your {site} sign-in code. It expires in {ttl} minutes.\n"
    "If you did not ask for it, ignore this message — and do not share it "
    "with anyone, including anyone claiming to be from {site}."
)


def children_for_number(mobile):
    """
    Every student whose guardian number matches, newest batch first.

    Matched on the normalised form so that "98765 43210", "+919876543210" and
    "09876543210" are one number rather than three. Archived batches and
    deactivated students are excluded — the same rule the rest of the app
    applies, so a guardian's list cannot contain a child staff can no longer
    see.
    """
    from academics.models import StudentProfile

    number, error = normalise_msisdn(mobile)
    if error:
        return StudentProfile.objects.none()
    return (
        StudentProfile.objects
        .filter(guardian_mobile_e164=number, is_active=True,
                batch__is_active=True, user__is_active=True)
        .select_related("user", "batch", "department", "department__institute")
        .order_by("-batch__start_year", "user__full_name")
    )


def account_for_number(number, *, institute=None, name=""):
    """
    The guardian User for this number, created on first successful sign-in.

    The email is synthetic: `email` is the USERNAME_FIELD and is unique, and a
    guardian has no email we can rely on — `guardian_email` is blank on most
    student rows. Nothing is ever sent to it.
    """
    User = get_user_model()
    user = User.objects.filter(guardian_mobile=number).first()
    if user is not None:
        # The institute can change if the child transfers; the name can change
        # if staff correct the record. Neither is worth a second sign-in.
        fields = []
        if institute and user.institute_id != institute.id:
            user.institute = institute
            fields.append("institute")
        if name and user.full_name != name:
            user.full_name = name
            fields.append("full_name")
        if fields:
            user.save(update_fields=fields)
        return user

    with transaction.atomic():
        user = User(
            email=f"guardian.{number.lstrip('+')}@guardian.invalid",
            full_name=name or "Guardian",
            phone=number,
            guardian_mobile=number,
            role=User.Role.GUARDIAN,
            institute=institute,
            # True so the app does not push them into "finish your profile":
            # there is nothing for a guardian to finish.
            registration_completed=True,
            email_verified=False,
        )
        # No password, ever. Not a blank one — an unusable one, so that
        # authenticate() can never succeed against this row whatever is posted.
        user.set_unusable_password()
        user.save()
    return user


def send_login_code(number, code):
    """Deliver the code, returning the whatsapp Result."""
    from notifications.whatsapp import send_whatsapp

    ttl = getattr(settings, "PHONE_OTP_TTL_MINUTES", settings.OTP_TTL_MINUTES)
    return send_whatsapp(number, OTP_MESSAGE.format(
        code=code, site=settings.SITE_NAME, ttl=ttl))


# --------------------------------------------------------------------------- #
#  Which child is being viewed
# --------------------------------------------------------------------------- #
def active_child(request):
    """
    The child this guardian is currently looking at, or None.

    Recomputed from the student table every time rather than trusted from the
    session: the session says which child was chosen, and this decides whether
    that is still allowed. A number removed from a student record stops working
    on the next request, without needing the session to be torn down.
    """
    user = getattr(request, "user", None)
    if not getattr(user, "is_authenticated", False) or not user.is_guardian:
        return None
    children = list(children_for_number(user.guardian_mobile))
    if not children:
        return None
    chosen = request.session.get(SESSION_CHILD_KEY)
    for child in children:
        if str(child.id) == str(chosen):
            return child
    # Either nothing was chosen yet or the chosen child is no longer reachable.
    # Falling back to the first is what makes a single-child guardian never see
    # a picker at all.
    request.session[SESSION_CHILD_KEY] = str(children[0].id)
    return children[0]


def acting_profile(user):
    """
    The student record a request is *about*.

    For a student that is their own profile. For a guardian it is the child
    currently selected, which the middleware has already resolved and hung on
    the user object.

    This exists so the read paths — the dashboard, attendance history, absence
    records, feedback — can be written once and serve both. It deliberately
    does not exist on the write paths: those stay keyed to `student_profile`,
    so a guardian has no profile there and nothing to act on even if a gate
    were somehow missed.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    if getattr(user, "is_guardian", False):
        # Set by GuardianChildMiddleware. Absent means no reachable child.
        return getattr(user, "_acting_child", None)
    return getattr(user, "student_profile", None)


def choose_child(request, student_id):
    """Point the session at one of this guardian's children. True if allowed."""
    for child in children_for_number(request.user.guardian_mobile):
        if str(child.id) == str(student_id):
            request.session[SESSION_CHILD_KEY] = str(child.id)
            return True
    return False


def child_options(request):
    """Rows for the topbar switcher — empty for a guardian with one child."""
    user = getattr(request, "user", None)
    if not getattr(user, "is_authenticated", False) or not user.is_guardian:
        return []
    children = list(children_for_number(user.guardian_mobile))
    if len(children) < 2:
        return []
    current = active_child(request)
    return [{
        "id": str(child.id),
        "name": child.name,
        "detail": f"{child.batch.label} · {child.department.code}",
        "current": current is not None and str(child.id) == str(current.id),
    } for child in children]
