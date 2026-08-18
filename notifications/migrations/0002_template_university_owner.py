"""
Let a university own WhatsApp templates.

The one place a university's access is *narrower* than a head's: it may not use
an institute's approved wording, because that wording is registered against the
institute's own WhatsApp sender and speaks in its voice. So a university needs
templates of its own, and a template needs to say which of the two owns it.

Additive. Every existing row keeps `institute` set and `university` NULL, which
is exactly what "owned by an institute" means.

`institute` becomes nullable so a university-owned row can exist. The
"exactly one owner" rule that replaces the NOT NULL is enforced in
`WhatsAppTemplate.save()`, not by a CheckConstraint: MongoDB has none, and a
constraint that migrates cleanly while enforcing nothing is worse than no
constraint, because it reads like a guarantee.
"""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("notifications", "0001_initial"),
        ("accounts", "0004_university_tier"),
    ]

    operations = [
        migrations.AddField(
            model_name="whatsapptemplate",
            name="university",
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="whatsapp_templates", to="accounts.university"),
        ),
        # Nullable now that a university can own a template instead. Every
        # existing row keeps its institute, so nothing is loosened in practice
        # — the constraint below still insists on exactly one owner.
        migrations.AlterField(
            model_name="whatsapptemplate",
            name="institute",
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="whatsapp_templates", to="accounts.institute"),
        ),
        migrations.AddConstraint(
            model_name="whatsapptemplate",
            constraint=models.UniqueConstraint(
                fields=("university", "twilio_name"),
                name="uniq_wa_template_name_university"),
        ),
    ]
