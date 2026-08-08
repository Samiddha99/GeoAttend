"""
Add Subject.subject_type.

Purely additive: a new column with a default, no index, no constraint. That
matters on MongoDB, where a migration that fails part-way leaves whatever it
already did behind — there is nothing here to leave behind.

Existing subjects become Theory. That is a real assertion about the data, not
a placeholder: every subject in the system predates the distinction and was
recorded as a lecture course. Any labs already in there will need retyping by
hand, which is why the type is visible as a column rather than buried in the
edit form.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("academics", "0002_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="subject",
            name="subject_type",
            field=models.CharField(
                choices=[("THEORY", "Theory"), ("PRACTICAL", "Practical"),
                         ("OTHER", "Other")],
                default="THEORY", max_length=12, verbose_name="Subject type",
            ),
        ),
    ]
