"""
A teacher's PAN and date of birth.

**Why not unique.** The rule is "one holder per PAN among rows that are not
archived", which is a partial index over a column that changes every time
somebody is archived or restored. `django_mongodb_backend` will not express
that, so the rule lives in `accounts/pan.py` and this column carries a plain
index for the lookup it does. The race that leaves open is documented there
rather than papered over here.

**Why blank rather than required.** Every teacher already on file predates
this. Making it mandatory would have meant inventing a PAN for each of them,
and an invented national identifier is worse than an absent one — so existing
rows keep an empty value, `accounts.pan` treats "no PAN" as "nothing to check",
and the edit form is the one place a real one can be filled in later.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [("accounts", "0007_user_suspension")]

    operations = [
        migrations.AddField(
            model_name="user",
            name="pan_number",
            field=models.CharField(
                blank=True, db_index=True, max_length=10,
                help_text="Permanent Account Number. Fixed once saved."),
        ),
        migrations.AddField(
            model_name="user",
            name="date_of_birth",
            field=models.DateField(
                blank=True, null=True,
                help_text="Checked against the PAN. Fixed once saved."),
        ),
    ]
