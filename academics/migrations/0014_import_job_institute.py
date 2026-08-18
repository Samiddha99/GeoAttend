"""
An import job belongs to an institute, not to a department.

The roster used to be uploaded one department at a time, chosen from a dropdown
on the modal. It carries a Department column now, so one file can span the whole
college — and recording a single department on the job would mean picking a
winner among the six a file touched. The departments a file actually reached are
listed in `report["departments"]` instead.

`department` is kept, and made nullable, rather than dropped: for every job
uploaded before this change it is a real fact, and the Imports screen still has
it to show. Nothing writes it any more.

The backfill fills `institute` from the department each old job already points
at, so the history stays visible on a screen that now scopes by institute.
"""
import django.db.models.deletion
from django.db import migrations, models


def fill_institute(apps, schema_editor):
    """
    Point every existing job at its department's institute.

    Iterated rather than done with one `update()`. A queryset update that walks
    `department__institute` is a cross-collection lookup, and
    `django_mongodb_backend` cannot express one — Atlas rejects the pipeline.
    sqlite runs it happily, which is exactly how this shape reaches production
    unnoticed. There are a handful of these rows; the loop is free.
    """
    ImportJob = apps.get_model("academics", "ImportJob")
    for job in ImportJob.objects.filter(
            institute__isnull=True, department__isnull=False
    ).select_related("department"):
        job.institute_id = job.department.institute_id
        job.save(update_fields=["institute"])


def unfill(apps, schema_editor):
    """Reversing leaves `institute` empty; `department` was never cleared."""
    ImportJob = apps.get_model("academics", "ImportJob")
    for job in ImportJob.objects.filter(institute__isnull=False):
        job.institute_id = None
        job.save(update_fields=["institute"])


class Migration(migrations.Migration):

    dependencies = [
        ("academics", "0013_section"),
        ("accounts", "0008_teacher_pan"),
    ]

    operations = [
        migrations.AddField(
            model_name="importjob",
            name="institute",
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="imports", to="accounts.institute"),
        ),
        migrations.AlterField(
            model_name="importjob",
            name="department",
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="imports", to="academics.department"),
        ),
        migrations.RunPython(fill_institute, unfill),
    ]
