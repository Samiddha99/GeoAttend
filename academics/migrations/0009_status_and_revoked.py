"""
Split the derived state into two stored fields.

The backfill is the interesting part. Both values are reconstructed from what
the application was already computing, so the screens read the same the moment
this lands:

* `status` — ARCHIVED where `is_active` is false, otherwise ACTIVE. Students
  and teachers additionally become INVITED when their account was never
  completed, which is the state those tables were already showing.
* `is_revoked` — true where the department's discipline is not on the
  institute's `InstituteAffiliation` list, which is exactly the query
  `revoked_department_ids` was running per page.

A department with no discipline recorded is not revoked: unset has always meant
"governed by nobody", and every department predating the discipline column is
in that state.
"""
from django.db import migrations, models

from core.enums import revoked_field, status_field


def backfill(apps, schema_editor):
    Department = apps.get_model("academics", "Department")
    Subject = apps.get_model("academics", "Subject")
    Batch = apps.get_model("academics", "Batch")
    StudentProfile = apps.get_model("academics", "StudentProfile")
    InstituteAffiliation = apps.get_model("accounts", "InstituteAffiliation")
    User = apps.get_model("accounts", "User")

    for model in (Department, Subject, Batch, StudentProfile):
        model.objects.filter(is_active=False).update(status="ARCHIVED")
        model.objects.filter(is_active=True).update(status="ACTIVE")

    # A student whose account was never completed is "invited", which is the
    # word those tables already used for them.
    #
    # **The ids are fetched first.** `filter(user__registration_completed=...)`
    # walks from the profile collection into the user collection, and
    # django_mongodb_backend cannot express a cross-collection lookup inside an
    # `update()` — it refuses with "Cannot use QuerySet.update() when querying
    # across multiple collections". Reading the ids turns it into two
    # single-collection queries, which is the only shape that backend can run.
    invited = list(User.objects.filter(registration_completed=False)
                   .values_list("id", flat=True))
    if invited:
        StudentProfile.objects.filter(user_id__in=invited).update(status="INVITED")
    User.objects.filter(is_active=False).update(status="ARCHIVED")
    User.objects.filter(is_active=True,
                        registration_completed=True).update(status="ACTIVE")
    User.objects.filter(is_active=True,
                        registration_completed=False).update(status="INVITED")

    held = {(a.institute_id, a.discipline)
            for a in InstituteAffiliation.objects.all()}
    revoked = [d.pk for d in Department.objects.exclude(discipline="")
               if (d.institute_id, d.discipline) not in held]

    # Cleared first, then set. The column starts False on a fresh migration so
    # the reset is redundant there — but this runs again on any database where
    # the migration is replayed or the flag was set by hand, and a backfill
    # that only ever adds cannot correct anything. Idempotent is cheap here and
    # the alternative is a value that drifts one direction only.
    for model in (Department, Subject, Batch, StudentProfile):
        model.objects.filter(is_revoked=True).update(is_revoked=False)
    User.objects.filter(is_revoked=True).update(is_revoked=False)
    if not revoked:
        return
    Department.objects.filter(pk__in=revoked).update(is_revoked=True)
    Subject.objects.filter(department_id__in=revoked).update(is_revoked=True)
    Batch.objects.filter(department_id__in=revoked).update(is_revoked=True)
    StudentProfile.objects.filter(department_id__in=revoked).update(is_revoked=True)
    User.objects.filter(department_id__in=revoked,
                        role="TEACHER").update(is_revoked=True)


def unbackfill(apps, schema_editor):
    """Nothing to undo — `is_active` was never stopped being written."""


class Migration(migrations.Migration):

    dependencies = [
        ("academics", "0008_archived_with_discipline"),
        ("accounts", "0006_user_status_and_revoked"),
    ]

    operations = [
        *[migrations.AddField(model_name=model, name=name, field=field())
          for model in ("department", "subject", "batch", "studentprofile")
          for name, field in (("status", status_field),
                              ("is_revoked", revoked_field))],
        migrations.RunPython(backfill, unbackfill),
    ]
