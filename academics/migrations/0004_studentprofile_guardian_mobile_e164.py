"""
Store the guardian's number in E.164 alongside the number staff typed.

Additive, then backfilled. The backfill runs in Python rather than as an update
expression because normalising a phone number is a hundred lines of rules
(trunk zeros, country codes, punctuation) that already exist in
notifications.whatsapp and should not be reimplemented in SQL.

Numbers that will not parse are left blank on purpose. Blank means "no
guardian can sign in against this student", which is the safe reading of "we
could not make sense of this number".
"""
from django.db import migrations, models


def fill_e164(apps, schema_editor):
    from notifications.whatsapp import normalise_msisdn

    StudentProfile = apps.get_model("academics", "StudentProfile")
    batch, unparsed = [], 0
    for profile in StudentProfile.objects.exclude(guardian_mobile="").iterator():
        number, error = normalise_msisdn(profile.guardian_mobile)
        if error:
            unparsed += 1
            continue
        profile.guardian_mobile_e164 = number
        batch.append(profile)
        if len(batch) >= 500:
            StudentProfile.objects.bulk_update(batch, ["guardian_mobile_e164"])
            batch = []
    if batch:
        StudentProfile.objects.bulk_update(batch, ["guardian_mobile_e164"])
    if unparsed:
        print(f"  {unparsed} guardian number(s) could not be parsed and were "
              "left blank; those guardians cannot sign in until the number is "
              "corrected.")


def clear_e164(apps, schema_editor):
    apps.get_model("academics", "StudentProfile").objects.update(
        guardian_mobile_e164="")


class Migration(migrations.Migration):

    dependencies = [
        ("academics", "0003_subject_subject_type"),
    ]

    operations = [
        migrations.AddField(
            model_name="studentprofile",
            name="guardian_mobile_e164",
            field=models.CharField(blank=True, db_index=True, editable=False,
                                   max_length=20),
        ),
        migrations.AlterField(
            model_name="studentprofile",
            name="guardian_mobile",
            field=models.CharField(
                blank=True,
                help_text="WhatsApp number used for alerts and for guardian sign-in.",
                max_length=20),
        ),
        migrations.RunPython(fill_e164, clear_e164),
    ]
