"""
Remember which rows a discipline removal switched off.

False everywhere to begin with, which is the truthful answer: no removal has
recorded a snapshot yet, so a restore of a department archived *before* this
field existed turns nothing back on by itself. That is the safe direction —
it leaves the rows visibly archived for someone to restore deliberately,
rather than guessing which ones were meant to come back.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("academics", "0007_department_discipline"),
    ]

    operations = [
        migrations.AddField(
            model_name=model,
            name="archived_with_discipline",
            field=models.BooleanField(default=False, editable=False),
        )
        for model in ("department", "subject", "batch", "studentprofile")
    ]
