"""
The teacher half of the discipline-archive snapshot.

Its twin lives in academics/0008. Kept separate only because `User` is in this
app — the field means exactly the same thing and is set and cleared by the same
two functions in `accounts.affiliations`.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0004_university_tier"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="archived_with_discipline",
            field=models.BooleanField(default=False, editable=False),
        ),
    ]
