"""
Django settings for the GeoAttend student-attendance platform.

Everything that varies between environments is read from the environment
(optionally via a .env file next to manage.py).
"""
from pathlib import Path
import os
import sys
import mongoengine
import django_mongodb_backend
from django.core.exceptions import ImproperlyConfigured
import certifi

BASE_DIR = Path(__file__).resolve().parent.parent

# --------------------------------------------------------------------------- #
#  .env loading (optional dependency)
# --------------------------------------------------------------------------- #
try:  # pragma: no cover
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env")
except Exception:  # pragma: no cover
    pass


def env(key, default=None):
    return os.environ.get(key, default)


def env_bool(key, default=False):
    val = os.environ.get(key)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def env_int(key, default=0):
    try:
        return int(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
#  Core
# --------------------------------------------------------------------------- #
SECRET_KEY = env("DJANGO_SECRET_KEY", "django-insecure-dev-key-change-me-!v3ry-s3cr3t")
DEBUG = env_bool("DJANGO_DEBUG", True)
DEPLOY = env_bool("DJANGO_DEPLOY", False)
ALLOWED_HOSTS = [h.strip() for h in env("DJANGO_ALLOWED_HOSTS", "*").split(",") if h.strip()]
SITE_URL = env("SITE_URL", "http://127.0.0.1:8000").rstrip("/")
SITE_NAME = env("SITE_NAME", "GeoAttend")

CSRF_TRUSTED_ORIGINS = [
    o.strip()
    for o in env("CSRF_TRUSTED_ORIGINS", SITE_URL).split(",")
    if o.strip().startswith("http")
]

INSTALLED_APPS = [
    # "django.contrib.admin",
    "config.apps.MongoAdminConfig",
    # "django.contrib.auth",
    "config.apps.MongoAuthConfig",
    # "django.contrib.contenttypes",
    "config.apps.MongoContentTypesConfig",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    # "sslserver",
    "django_extensions",
    # local
    "accounts.apps.AccountsConfig",
    "academics.apps.AcademicsConfig",
    "attendance.apps.AttendanceConfig",
    "dashboard.apps.DashboardConfig",
    "notifications.apps.NotificationsConfig",
    "core.apps.CoreConfig",
    "feedback.apps.FeedbackConfig",
    # Serves the face-matching WebSocket. No channel layer is configured
    # because none is needed: each socket talks only to itself, so there is
    # nothing to broadcast and no Redis to run.
    "channels",
    # 'boto',
    'cloud_storages',
    'sri', #Subresource Integrity
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    'whitenoise.middleware.WhiteNoiseMiddleware',
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # local
    # Ahead of the app middleware so it sees every response, including the
    # redirects those middlewares issue.
    "core.middleware.NoStoreMiddleware",
    "accounts.middleware.ForceProfileCompletionMiddleware",
    # After profile completion, never before: a student without a password has
    # an earlier step to finish, and two gates redirecting at each other would
    # trap them in a loop.
    "accounts.middleware.ForceFaceEnrolmentMiddleware",
    # After the two gates above, which only ever act on students: a guardian
    # passes straight through both, and this resolves which child they are
    # looking at before any view runs.
    "accounts.middleware.GuardianChildMiddleware",
    "accounts.middleware.ActivityTrackingMiddleware",
    "core.middleware.AjaxExceptionMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context_processors.site",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# --------------------------------------------------------------------------- #
# #  Database
# # --------------------------------------------------------------------------- #
# if env("DB_ENGINE"):
#     DATABASES = {
#         "default": {
#             "ENGINE": env("DB_ENGINE"),
#             "NAME": env("DB_NAME"),
#             "USER": env("DB_USER"),
#             "PASSWORD": env("DB_PASSWORD"),
#             "HOST": env("DB_HOST", "127.0.0.1"),
#             "PORT": env("DB_PORT", "5432"),
#             "CONN_MAX_AGE": 60,
#         }
#     }
# else:
#     DATABASES = {
#         "default": {
#             "ENGINE": "django.db.backends.sqlite3",
#             "NAME": env("DB_NAME") or (BASE_DIR / "db.sqlite3"),
#             "OPTIONS": {"timeout": 20},
#         }
#     }

# The connection string carries the database password, so it comes from the
# environment — locally from .env, on the server from the platform's secret
# store. It used to be a literal here, which put the production password into
# every clone of the repository and into the image.
MONGO_URI = env("MONGODB_URI") or env("MONGO_URI")
if not MONGO_URI:
    if DEPLOY:
        raise ImproperlyConfigured(
            "MONGODB_URI is not set. On Fly: "
            "fly secrets set MONGODB_URI='mongodb+srv://…/geo_attend?retryWrites=true&w=majority'")
    # Build- and dev-time fallback. A plain host:port URI parses without the
    # DNS lookup an SRV URI needs, so `collectstatic` runs inside a Docker
    # build with no credentials present and nothing to leak into a layer.
    MONGO_URI = "mongodb://localhost:27017/geo_attend"

mongoengine.connect(host=MONGO_URI)
DATABASES = {
  "default": django_mongodb_backend.parse_uri(MONGO_URI),
}

# DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
DEFAULT_AUTO_FIELD = "django_mongodb_backend.fields.ObjectIdAutoField"


MIGRATION_MODULES = {
    'admin': 'mongo_migrations.admin',
    'auth': 'mongo_migrations.auth',
    'contenttypes': 'mongo_migrations.contenttypes',
    'sessions': 'mongo_migrations.sessions',
    'account': 'mongo_migrations.account',
}

# --------------------------------------------------------------------------- #
#  Auth
# --------------------------------------------------------------------------- #
AUTH_USER_MODEL = "accounts.User"
AUTHENTICATION_BACKENDS = ["accounts.backends.EmailBackend"]

LOGIN_URL = "/auth/login/"
LOGIN_REDIRECT_URL = "/app/"
LOGOUT_REDIRECT_URL = "/auth/login/"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
     "OPTIONS": {"min_length": 8}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

SESSION_COOKIE_AGE = 60 * 60 * 12
SESSION_SAVE_EVERY_REQUEST = True
SESSION_EXPIRE_AT_BROWSER_CLOSE = False

# --------------------------------------------------------------------------- #
#  I18N / static / media
# --------------------------------------------------------------------------- #
LANGUAGE_CODE = "en-us"
TIME_ZONE = env("TIME_ZONE", "Asia/Kolkata")
USE_I18N = True
USE_TZ = True

DROPBOX_PERMANENT_LINK = False
OVERWRITE_FILE = True
CLOUD_STORAGE_CREATE_NEW_IF_SAME_CONTENT = True
DROPBOX_OAUTH2_ACCESS_TOKEN = env('DROPBOX_OAUTH2_ACCESS_TOKEN')
DROPBOX_OAUTH2_REFRESH_TOKEN = env('DROPBOX_OAUTH2_REFRESH_TOKEN')
DROPBOX_APP_KEY = env('DROPBOX_APP_KEY')
DROPBOX_APP_SECRET = env('DROPBOX_APP_SECRET')
DROPBOX_ROOT_PATH = "/Apps/GeoAttend/media"

STORAGES = {
    "default": {
        "BACKEND": "cloud_storages.backends.dropbox.DropBoxStorage",
        # "BACKEND": "storages.backends.s3boto3.S3Boto3Storage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
        # "BACKEND": "storages.backends.s3boto3.S3Boto3Storage"
    },
}

STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

# --------------------------------------------------------------------------- #
#  Email
# --------------------------------------------------------------------------- #
# For Mail Sending
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL")
EMAIL_SENDER_NAME = env("EMAIL_SENDER_NAME")
SERVER_EMAIL = env("SERVER_EMAIL")

ADMINS = [
    (env("EMAIL_SENDER_NAME"), env("ADMIN_EMAIL")),
]  #send error to this mail
MANAGERS = ADMINS

EMAIL_BACKEND = env("EMAIL_BACKEND")
EMAIL_HOST = env("EMAIL_HOST")
EMAIL_USE_TLS = True

# EMAIL_USE_SSL = True
EMAIL_PORT = env("EMAIL_TLS_PORT1")
MY_EMAIL_ID = env("MY_EMAIL_ID")
EMAIL_HOST_NAME = env("EMAIL_SENDER_NAME")
EMAIL_HOST_USER = env("EMAIL_HOST_USERNAME")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD")

# --------------------------------------------------------------------------- #
#  Outbound mail — every message in the project goes through
#  notifications.mailer.send_mail(), which picks a transport from here.
#
#  EMAIL_PROVIDER picks the transport:
#     sendgrid            → SendGrid v3 REST API        (SENDGRID_API_KEY)
#     mailchimp/mandrill  → Mailchimp Transactional     (MAILCHIMP_API_KEY)
#     django              → EMAIL_BACKEND above (console in dev, locmem in tests)
#
#  Each provider has its own key setting, so both can stay configured and
#  switching between them is a one-word change.
#
#  Normalised here rather than in mailer.py so that "SendGrid", " mandrill "
#  and similar don't quietly fall through to the console backend — a send that
#  only printed to stdout looks successful and is the worst failure mode.
# --------------------------------------------------------------------------- #
# `or "django"` after stripping, so a present-but-blank EMAIL_PROVIDER= line in
# .env reads as "unset" rather than crashing the process.
EMAIL_PROVIDER = (env("EMAIL_PROVIDER", "") or "").strip().lower() or "django"
if EMAIL_PROVIDER == "mandrill":            # Mandrill is Mailchimp's old name
    EMAIL_PROVIDER = "mailchimp"
if EMAIL_PROVIDER not in ("sendgrid", "mailchimp", "django"):
    raise ImproperlyConfigured(
        f"EMAIL_PROVIDER must be sendgrid, mailchimp or django — got {EMAIL_PROVIDER!r}."
    )
SENDGRID_API_KEY = env("SENDGRID_API_KEY", "")
MAILCHIMP_API_KEY = env("MAILCHIMP_TRANSACTIONAL_API_KEY", "")
EMAIL_TIMEOUT = env_int("EMAIL_TIMEOUT", 20)
MAILCHIMP_API_URL = env("MAILCHIMP_API_URL", "https://mandrillapp.com/api/1.0")
SENDGRID_SANDBOX_MODE = env_bool("SENDGRID_SANDBOX_MODE", False)
SENDGRID_TIMEOUT = env_int("SENDGRID_TIMEOUT", 20)
# Send off the request thread. 0 = auto (CPU count - 2, floor 1, ceiling 32).
EMAIL_ASYNC = env_bool("EMAIL_ASYNC", True)
EMAIL_MAX_WORKERS = env_int("EMAIL_MAX_WORKERS", 0)

# --------------------------------------------------------------------------- #
#  Domain policy (all enforced server-side)
# --------------------------------------------------------------------------- #
ATTENDANCE = {
    # geo-fence radius in metres between teacher and student
    "DEFAULT_RADIUS_M": env_int("ATTENDANCE_DEFAULT_RADIUS_M", 20),
    "MIN_RADIUS_M": env_int("ATTENDANCE_MIN_RADIUS_M", 5),
    # A ceiling, not a suggestion — the server refuses anything larger. 50 m is
    # about a large lecture hall. Beyond that the fence stops distinguishing
    # "in the room" from "in the corridor", which is the only thing it does.
    "MAX_RADIUS_M": env_int("ATTENDANCE_MAX_RADIUS_M", 20),
    # link validity
    "DEFAULT_EXPIRY_MIN": env_int("ATTENDANCE_DEFAULT_EXPIRY_MIN", 5),
    "MIN_EXPIRY_MIN": env_int("ATTENDANCE_MIN_EXPIRY_MIN", 5),
    # Also a hard ceiling. A link that outlives the lesson is a link a student
    # can use from the car park, so the window is bounded to something close to
    # a class period rather than left to whoever fills the box.
    "MAX_EXPIRY_MIN": env_int("ATTENDANCE_MAX_EXPIRY_MIN", 30),
    # Reject GPS fixes too fuzzy to be trusted. Keep this at or below the fence
    # radius: a ±200 m fix inside a 50 m fence cannot tell the classroom from
    # the car park, so raising it does not make marking work, it just stops the
    # geo-fence meaning anything.
    #
    # Laptops and desktops have no GPS chip — the browser geolocates from
    # WiFi/IP and typically reports 100–3000 m, so the mark page will refuse to
    # submit. Test on a phone. If you must use a laptop, set
    # ATTENDANCE_MAX_GPS_ACCURACY_M high *in your local .env only*;
    # `manage.py check` will warn if a loose value is still in place.
    "MAX_GPS_ACCURACY_M": env_int("ATTENDANCE_MAX_GPS_ACCURACY_M", 20),
    # one device per student (anti proxy-attendance) — blocks *marking*
    "ENFORCE_DEVICE_LOCK": env_bool("ATTENDANCE_ENFORCE_DEVICE_LOCK", True),
    # ...and blocks *signing in* from an unrecognised device.  Stricter, and the
    # lockout hurts more: a student on a new phone cannot even see their record
    # until staff unlink them.  Applies to students only, never to staff.
    "ENFORCE_LOGIN_DEVICE_LOCK": env_bool("ATTENDANCE_ENFORCE_LOGIN_DEVICE_LOCK", True),
    # block two different students marking from the same device in one session
    "BLOCK_SHARED_DEVICE": env_bool("ATTENDANCE_BLOCK_SHARED_DEVICE", True),
    "LOW_ATTENDANCE_THRESHOLD": env_int("LOW_ATTENDANCE_THRESHOLD", 75),
    # Whether a student may release their own device binding.  Off by default:
    # a self-service reset would let anyone defeat the one-device rule on demand,
    # which is exactly what the binding exists to prevent.  Staff unlink instead,
    # from Manage → Students.
    "ALLOW_STUDENT_SELF_DEVICE_RESET": env_bool("ALLOW_STUDENT_SELF_DEVICE_RESET", False),
    # How long a student has to explain an absence, counted in whole days from
    # the date of the class. 0 turns the feature off entirely.
    "ABSENCE_REASON_DAYS": env_int("ABSENCE_REASON_DAYS", 10),
    # Evidence a student may attach to an absence request. Optional — a reason
    # with no attachment is still a valid reason. 0 files turns it off.
    "ATTACHMENT_MAX_FILES": env_int("ABSENCE_ATTACHMENT_MAX_FILES", 5),
    "ATTACHMENT_MAX_TOTAL_MB": env_int("ABSENCE_ATTACHMENT_MAX_TOTAL_MB", 20),
    # How long after the link is created a teacher may still mark someone
    # present by hand. Counted from `created_at`, not from expiry: the point is
    # that the teacher is still in the room and can see who is in front of them.
    # Once the class has moved on, "mark present" is a claim about the past that
    # nobody can check.
    #
    # Deliberately longer than the link itself (5 minutes by default) so that a
    # phone with no signal, a flat battery or a failed face match can still be
    # sorted out during the lesson. 0 turns manual marking off entirely.
    "MANUAL_MARK_MINUTES": env_int("ATTENDANCE_MANUAL_MARK_MINUTES", 30),
}

# --------------------------------------------------------------------------- #
#  Class feedback
# --------------------------------------------------------------------------- #
FEEDBACK = {
    # How far back a teacher may reach when asking for feedback. Beyond this a
    # student is being asked to rate a class they no longer remember.
    "MAX_SESSION_AGE_DAYS": env_int("FEEDBACK_MAX_SESSION_AGE_DAYS", 360),
    "OPEN_HOURS": env_int("FEEDBACK_OPEN_HOURS", 24),
    # Individual answers and remarks stay hidden until this many students have
    # replied. In a class of six, a remark is effectively signed.
    "MIN_RESPONSES_TO_REVEAL": env_int("FEEDBACK_MIN_RESPONSES_TO_REVEAL", 5),
}

# --------------------------------------------------------------------------- #
#  Face enrolment
# --------------------------------------------------------------------------- #
# Thresholds worth tuning on your own students rather than trusting these.
# Capture a set of genuine and impostor pairs from real classroom conditions,
# look at where they fall, and move the numbers to suit — a value copied from
# a benchmark says nothing about your cameras or your lighting.
FACE = {
    "ENABLED": env_bool("FACE_ENROLMENT_ENABLED", True),
    # "buffalo_s" is the small pack: lighter on a shared CPU. "buffalo_l" is
    # more accurate and worth it on better hardware.
    "MODEL_PACK": env("FACE_MODEL_PACK", "buffalo_s"),
    "MAX_IMAGE_SIDE": env_int("FACE_MAX_IMAGE_SIDE", 1600),
    "MAX_IMAGE_MB": env_int("FACE_MAX_IMAGE_MB", 5),
    # Detector confidence and how much of the frame the face must fill.
    "MIN_DETECT_SCORE": float(env("FACE_MIN_DETECT_SCORE", "0.6")),
    "MIN_FACE_PX": env_int("FACE_MIN_FACE_PX", 110),
    # How far the head must turn for a "left"/"right" capture to count, and how
    # straight "front" has to be.
    # The capture page holds a tighter window than these. That is deliberate:
    # the page decides what a good capture looks like, and the server only has
    # to agree that the head is plausibly turned the right way. Matching the
    # two exactly would mean any small disagreement between the two measuring
    # methods rejected a capture the student had already got right.
    "FRONT_MAX_YAW": env_int("FACE_FRONT_MAX_YAW", 14),
    "TURN_MIN_YAW": env_int("FACE_TURN_MIN_YAW", 12),
    "TURN_MAX_YAW": env_int("FACE_TURN_MAX_YAW", 55),
    # Occlusion heuristics — see accounts/face.py for what these actually
    # measure. Lower means more permissive.
    "MIN_EYE_ENERGY": float(env("FACE_MIN_EYE_ENERGY", "0.55")),
    "MIN_MOUTH_ENERGY": float(env("FACE_MIN_MOUTH_ENERGY", "0.45")),
    # All three captures must be the same person.
    "SAME_PERSON_MIN": float(env("FACE_SAME_PERSON_MIN", "0.45")),

    # --- live verification while marking attendance ------------------------ #
    "LIVE_ENABLED": env_bool("FACE_LIVE_ENABLED", True),
    # Smaller detector input than enrolment: the browser crops to the face
    # before sending, and this number is the main dial on how many frames a
    # second the server can keep up with.
    "LIVE_DET_SIZE": env_int("FACE_LIVE_DET_SIZE", 320),
    "LIVE_MIN_DETECT_SCORE": float(env("FACE_LIVE_MIN_DETECT_SCORE", "0.5")),
    "LIVE_MIN_FACE_PX": env_int("FACE_LIVE_MIN_FACE_PX", 80),
    # Cosine similarity against the best of the three enrolment vectors.
    # Measure this on your own students before trusting it: too low lets a
    # sibling through, too high locks out anyone who grew a beard.
    "MATCH_MIN": float(env("FACE_MATCH_MIN", "0.42")),
    # How long a student may keep trying before the teacher fallback appears,
    # and how long the ticket that authorises the attempt stays valid.
    "LIVE_TICKET_SECONDS": env_int("FACE_LIVE_TICKET_SECONDS", 180),
    "LIVE_FALLBACK_AFTER_SEC": env_int("FACE_LIVE_FALLBACK_AFTER_SEC", 45),
    "LIVE_MAX_FRAMES": env_int("FACE_LIVE_MAX_FRAMES", 400),
    # How many frames the whole process will work on at once. The bottleneck is
    # CPU, not the network: past this, extra sockets are told to wait rather
    # than queueing work nobody will still be waiting for.
    "LIVE_MAX_CONCURRENT": env_int("FACE_LIVE_MAX_CONCURRENT", 2),
    "LIVE_MAX_FRAME_BYTES": env_int("FACE_LIVE_MAX_FRAME_BYTES", 400 * 1024),

    # --- passive anti-spoofing -------------------------------------------- #
    # Path to an ONNX liveness model (MiniFASNet and friends). Not bundled:
    # these carry their own licences and their own input conventions, so the
    # size and which output means "real" are settings too.
    #
    # With no model configured, liveness is UNKNOWN, not "fine" — and
    # ANTISPOOF_REQUIRED decides whether marking proceeds anyway. Leaving it
    # required and unconfigured stops face marking outright, which is the
    # honest default: a photograph passes every other check in the pipeline.
    "ANTISPOOF_MODEL": env("FACE_ANTISPOOF_MODEL", ""),
    "ANTISPOOF_REQUIRED": env_bool("FACE_ANTISPOOF_REQUIRED", False),
    "ANTISPOOF_INPUT": env_int("FACE_ANTISPOOF_INPUT", 80),
    "ANTISPOOF_PAD": float(env("FACE_ANTISPOOF_PAD", "0.4")),
    "ANTISPOOF_REAL_INDEX": env_int("FACE_ANTISPOOF_REAL_INDEX", 1),
    "ANTISPOOF_MIN": float(env("FACE_ANTISPOOF_MIN", "0.6")),
}

# --------------------------------------------------------------------------- #
#  WhatsApp delivery for alerts (Twilio)
#
#  Leave ACCOUNT_SID blank and the module runs in console mode: messages are
#  logged instead of sent, so the whole flow works without an account.
#
#  NOTE: WhatsApp only allows free-form text inside a 24-hour window opened by
#  the recipient messaging you first. For business-initiated alerts set
#  CONTENT_SID to a pre-approved Content Template. See notifications/whatsapp.py.
# --------------------------------------------------------------------------- #
WHATSAPP = {
    "ENABLED": env_bool("WHATSAPP_ENABLED", True),
    "DEFAULT_COUNTRY_CODE": env("WHATSAPP_DEFAULT_COUNTRY_CODE", "91"),
    "ACCOUNT_SID": env("TWILIO_ACCOUNT_SID", ""),
    "AUTH_TOKEN": env("TWILIO_AUTH_TOKEN", ""),
    # Your WhatsApp-enabled Twilio sender, e.g. +14155238886 (sandbox).
    "FROM_NUMBER": env("TWILIO_WHATSAPP_FROM", ""),
    # Approved Content Template SID (HX…) used for every alert when set.
    "CONTENT_SID": env("TWILIO_CONTENT_SID", ""),
    "STATUS_CALLBACK": env("TWILIO_STATUS_CALLBACK", ""),
    # Seconds before an HTTP call to Twilio is abandoned.
    "TIMEOUT": env_int("TWILIO_TIMEOUT", 20),

    # Opening the WhatsApp templates or Low-attendance alerts screen refreshes
    # any template still awaiting a verdict, so an overnight approval is not
    # invisible until somebody presses Refresh.
    #
    # Two things keep that honest. Templates already decided are never polled,
    # so the steady state costs nothing; and a page-triggered sync uses a short
    # timeout, because a slow Twilio must not hold the page open for the full
    # TIMEOUT above.
    "AUTOSYNC": env_bool("WHATSAPP_AUTOSYNC", True),
    "AUTOSYNC_THROTTLE_SEC": env_int("WHATSAPP_AUTOSYNC_THROTTLE_SEC", 120),
    "AUTOSYNC_TIMEOUT": env_int("WHATSAPP_AUTOSYNC_TIMEOUT", 6),
}

OTP_TTL_MINUTES = env_int("OTP_TTL_MINUTES", 10)
OTP_MAX_ATTEMPTS = env_int("OTP_MAX_ATTEMPTS", 5)
OTP_RESEND_COOLDOWN_SEC = env_int("OTP_RESEND_COOLDOWN_SEC", 60)
INVITE_TTL_DAYS = env_int("INVITE_TTL_DAYS", 7)

# Guardian sign-in codes, sent over WhatsApp. Separate knobs from the email OTP
# above because the threat model is different: this code is the *entire*
# credential for a guardian, where an email code lands in a mailbox that has a
# password of its own. Shorter life, and a hard ceiling on how many messages
# one number can be made to receive.
PHONE_OTP_TTL_MINUTES = env_int("PHONE_OTP_TTL_MINUTES", 5)
PHONE_OTP_RESEND_SECONDS = env_int("PHONE_OTP_RESEND_SECONDS", 60)
PHONE_OTP_MAX_SENDS = env_int("PHONE_OTP_MAX_SENDS", 5)

MESSAGE_STORAGE = "django.contrib.messages.storage.session.SessionStorage"

# --------------------------------------------------------------------------- #
#  Security (tightened automatically when DEBUG is off)
# --------------------------------------------------------------------------- #
X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
if DEPLOY:
    # These assume you are behind HTTPS. `runserver` only speaks HTTP, so with
    # DEBUG=False it will 301 you to https:// and then fail to answer — set
    # SECURE_SSL_REDIRECT=False (and HSTS to 0) while testing locally.
    SESSION_COOKIE_SECURE = env_bool("SESSION_COOKIE_SECURE", True)
    CSRF_COOKIE_SECURE = env_bool("CSRF_COOKIE_SECURE", True)
    SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT", True)
    SECURE_HSTS_SECONDS = env_int("SECURE_HSTS_SECONDS", 60 * 60 * 24 * 30)
    SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool("SECURE_HSTS_INCLUDE_SUBDOMAINS", True)
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    # The platform's health check hits the container directly over plain HTTP,
    # with no X-Forwarded-Proto to say otherwise. Without this exemption it is
    # answered with a 301 to https, read as a failure, and the machine is
    # killed and replaced on a loop.
    SECURE_REDIRECT_EXEMPT = [r"^health/$"]
else:
    SECURE_SSL_REDIRECT = False
    SESSION_COOKIE_SECURE = False
    CSRF_COOKIE_SECURE = False

# --------------------------------------------------------------------------- #
#  Test-run tweaks: PBKDF2 hashing dominates the runtime of a suite that creates
#  hundreds of users, and console WhatsApp echo makes the output unreadable.
# --------------------------------------------------------------------------- #
if "test" in sys.argv:
    PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
    WHATSAPP["CONSOLE_ECHO"] = False
    EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    # Send inline and never touch a real provider, so mail.outbox assertions are
    # stable. Individual tests opt back in with override_settings.
    EMAIL_ASYNC = False
    EMAIL_PROVIDER = "django"
    SENDGRID_API_KEY = ""
    MAILCHIMP_API_KEY = ""

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"simple": {"format": "[{levelname}] {asctime} {name}: {message}", "style": "{"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "simple"}},
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "geoattend": {"handlers": ["console"], "level": "DEBUG", "propagate": False},
    },
}
