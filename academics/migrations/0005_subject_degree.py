"""
Add Subject.degree.

Purely additive: one column with a default and an index, no constraint and no
data rewrite. That matters on MongoDB, where a migration that fails part-way
leaves behind whatever it already did — there is nothing here to leave behind.

Existing subjects become Bachelor. That is a real assertion, not a placeholder:
every subject in the system predates the distinction. Any diploma or masters
papers already recorded will need retyping, which is why the degree is a
visible column on the Subjects screen rather than buried in the edit form.

The column is indexed because it is a filter on nine screens, unlike
`subject_type`, which is mostly used for grouping a list already in memory.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("academics", "0004_studentprofile_guardian_mobile_e164"),
    ]

    operations = [
        migrations.AddField(
            model_name="subject",
            name="degree",
            field=models.CharField(
                choices=[("DIPLOMA", "Diploma"), ("BACHELOR", "Bachelor"),
                         ("MASTERS", "Masters")],
                db_index=True, default="BACHELOR", max_length=12,
                verbose_name="Degree",
            ),
        ),
    ]
