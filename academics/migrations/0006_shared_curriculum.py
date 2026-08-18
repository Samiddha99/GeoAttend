"""
Feature 10: subjects and batches a university owns.

Nullable with no default, so every existing row keeps saying what it already
said — "this institute made it and owns it". That is exactly right for the
rows that predate the university tier and for every autonomous institute, so
there is nothing to backfill.
"""
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0001_initial"),
        ("academics", "0005_subject_degree"),
    ]

    operations = [
        migrations.AddField(
            model_name="batch",
            name="owner_university",
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="curriculum_batches",
                to="accounts.university",
            ),
        ),
        migrations.AddField(
            model_name="subject",
            name="owner_university",
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="curriculum_subjects",
                to="accounts.university",
            ),
        ),
    ]
