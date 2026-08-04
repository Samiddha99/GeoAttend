"""Bootstrap an institute + head account from the command line (no OTP)."""
from django.core.management.base import BaseCommand, CommandError

from accounts.models import Institute, User
from accounts.services import create_institute_and_head


class Command(BaseCommand):
    help = "Create an institute and its head-of-institute account."

    def add_arguments(self, parser):
        parser.add_argument("--name", required=True)
        parser.add_argument("--code", required=True)
        parser.add_argument("--institute-email", required=True)
        parser.add_argument("--head-name", required=True)
        parser.add_argument("--head-email", required=True)
        parser.add_argument("--password", required=True)

    def handle(self, *args, **o):
        if Institute.objects.filter(code=o["code"].upper()).exists():
            raise CommandError("An institute with that code already exists.")
        if User.objects.filter(email=o["head_email"].lower()).exists():
            raise CommandError("That head email is already registered.")
        institute, head = create_institute_and_head({
            "institute_name": o["name"], "institute_code": o["code"].upper(),
            "institute_email": o["institute_email"], "head_name": o["head_name"],
            "head_email": o["head_email"], "password": o["password"],
        })
        self.stdout.write(self.style.SUCCESS(f"Created {institute.name} · head {head.email}"))
