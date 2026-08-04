"""
Populate a realistic demo institute so the dashboards have something to show.

    python manage.py seed_demo            # ~90 days of history
    python manage.py seed_demo --reset    # wipe demo data first
"""
import datetime as dt
import random

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from academics.models import (
    Batch, Department, Enrollment, StudentProfile, Subject, TeacherAssignment,
)
from accounts.models import Institute, User
from attendance.models import AttendanceRecord, AttendanceSession
from core.utils import haversine_m

PASSWORD = "Passw0rd!23"
CENTRE = (22.572600, 88.363900)  # Kolkata

FIRST = ["Ananya", "Rahul", "Priya", "Arjun", "Sneha", "Vikram", "Ishita", "Rohan", "Meera",
         "Aditya", "Kavya", "Sourav", "Tanvi", "Nikhil", "Riya", "Aakash", "Pooja", "Debjit",
         "Sanjana", "Kunal", "Ritika", "Abhishek", "Neha", "Suman", "Farhan", "Anjali",
         "Manish", "Shreya", "Imran", "Divya"]
LAST = ["Sharma", "Verma", "Das", "Roy", "Ghosh", "Banerjee", "Mukherjee", "Chatterjee",
        "Sen", "Bose", "Dutta", "Nair", "Iyer", "Patel", "Singh"]


class Command(BaseCommand):
    help = "Seed a demo institute with departments, teachers, students and attendance history."

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true", help="Delete the demo institute first")
        parser.add_argument("--days", type=int, default=90, help="Days of attendance history")

    @transaction.atomic
    def handle(self, *args, **opts):
        random.seed(7)
        if opts["reset"]:
            Institute.objects.filter(code="DEMO").delete()
            self.stdout.write(self.style.WARNING("Removed previous demo data."))

        if Institute.objects.filter(code="DEMO").exists():
            self.stdout.write(self.style.WARNING("Demo institute already exists. Use --reset to rebuild."))
            return

        self.stdout.write(self.style.NOTICE("Creating Institute........."))
        institute = Institute.objects.create(
            name="Demo Institute of Technology", code="DEMO",
            email="office@demo.edu", phone="+91 33 4000 0000",
            address="Salt Lake, Kolkata, West Bengal",
        )
        self.stdout.write(self.style.NOTICE("Creating User (Head of Institute)........."))
        User.objects.create_user(
            email="head@demo.edu", password=PASSWORD, full_name="Dr. Arindam Bose",
            role=User.Role.HEAD, institute=institute, registration_completed=True,
            email_verified=True, is_staff=True,
        )

        blueprint = {
            "Computer Science & Engineering": ("CSE", [
                ("DSA", "Data Structures & Algorithms", 3),
                ("DBMS", "Database Management Systems", 4),
                ("AI", "Artificial Intelligence", 5),
                ("CNS", "Computer Networks & Security", 5),
                ("OS", "Operating Systems", 4),
            ]),
            "Electronics & Communication": ("ECE", [
                ("SS", "Signals & Systems", 3),
                ("DE", "Digital Electronics", 3),
                ("EMT", "Electromagnetic Theory", 4),
            ]),
        }

        total_students = 0
        self.stdout.write(self.style.NOTICE("Creating Departments........."))
        for dept_name, (code, subject_defs) in blueprint.items():
            dept = Department.objects.create(institute=institute, name=dept_name, code=code)
            hod = User.objects.create_user(
                email=f"hod.{code.lower()}@demo.edu", password=PASSWORD,
                full_name=f"Prof. {random.choice(LAST)} ({code} HoD)",
                role=User.Role.HOD, institute=institute, department=dept,
                registration_completed=True, email_verified=True,
            )
            dept.hod = hod
            dept.save(update_fields=["hod"])

            self.stdout.write(self.style.NOTICE("Creating Subjects........."))
            subjects = [
                Subject.objects.create(department=dept, code=c, name=n, semester=s, credits=4)
                for c, n, s in subject_defs
            ]
            self.stdout.write(self.style.NOTICE("Creating Batches........."))
            batches = [
                Batch.objects.create(department=dept, label=f"{y}-{str(y + 4)[-2:]}",
                                     start_year=y, end_year=y + 4)
                for y in (2022, 2023)
            ]

            teachers = []
            self.stdout.write(self.style.NOTICE("Creating User (Teachers)........."))
            for i in range(3):
                t = User.objects.create_user(
                    email=f"teacher{i + 1}.{code.lower()}@demo.edu", password=PASSWORD,
                    full_name=f"{random.choice(FIRST)} {random.choice(LAST)}",
                    role=User.Role.TEACHER, institute=institute, department=dept,
                    registration_completed=True, email_verified=True,
                )
                teachers.append(t)

            self.stdout.write(self.style.NOTICE("Creating Teacher Assignment........."))
            for batch in batches:
                for idx, subject in enumerate(subjects):
                    TeacherAssignment.objects.create(
                        teacher=teachers[idx % len(teachers)], subject=subject,
                        batch=batch, assigned_by=hod,
                    )

            self.stdout.write(self.style.NOTICE("Creating User (Students)........."))
            for batch in batches:
                for n in range(1, 26):
                    name = f"{random.choice(FIRST)} {random.choice(LAST)}"
                    email = f"{code.lower()}{batch.start_year}{n:03d}@demo.edu"
                    user = User.objects.create_user(
                        email=email, password=PASSWORD, full_name=name,
                        role=User.Role.STUDENT, institute=institute, department=dept,
                        registration_completed=True, email_verified=True,
                    )
                    guardian = f"{random.choice(['Mr.', 'Mrs.'])} {name.split(' ')[-1]}"
                    profile = StudentProfile.objects.create(
                        user=user, department=dept, batch=batch,
                        class_roll=f"{n:02d}",
                        exam_roll=f"{code}{str(batch.start_year)[-2:]}{n:03d}",
                        mobile=f"+9198{random.randint(10000000, 99999999)}",
                        guardian_name=guardian,
                        # every 20th student is left without one, so the alert
                        # screen has a realistic "missing guardian number" case
                        guardian_mobile=("" if n % 20 == 0
                                         else f"+9197{random.randint(10000000, 99999999)}"),
                    )
                    chosen = random.sample(subjects, k=min(len(subjects), random.choice([3, 4, 4])))
                    for subject in chosen:
                        Enrollment.objects.create(student=profile, subject=subject)
                    total_students += 1

        # ---- attendance history ------------------------------------------ #
        today = timezone.localdate()
        start = today - dt.timedelta(days=opts["days"])
        sessions = records = 0
        assignments = list(
            TeacherAssignment.objects.select_related("subject", "batch", "teacher")
        )
        # each student gets a personal "diligence" factor for realistic spread
        diligence = {
            p.id: random.gauss(0.82, 0.13) for p in StudentProfile.objects.all()
        }

        day = start
        self.stdout.write(self.style.NOTICE("Creating Attendance Record........."))
        while day <= today:
            if day.weekday() < 5:  # weekdays only
                for a in random.sample(assignments, k=max(1, len(assignments) // 4)):
                    roster = list(
                        StudentProfile.objects.filter(
                            batch=a.batch, enrollments__subject=a.subject, enrollments__is_active=True
                        )
                    )
                    if not roster:
                        continue
                    created = timezone.make_aware(
                        dt.datetime.combine(day, dt.time(random.choice([9, 10, 11, 13, 14, 15]), 5))
                    )
                    session = AttendanceSession.objects.create(
                        teacher=a.teacher, subject=a.subject, batch=a.batch,
                        latitude=CENTRE[0] + random.uniform(-0.0004, 0.0004),
                        longitude=CENTRE[1] + random.uniform(-0.0004, 0.0004),
                        accuracy_m=random.uniform(4, 18), radius_m=50,
                        note=random.choice(["B-204", "Lab 3", "A-101", ""]),
                        session_date=day, expected_count=len(roster),
                        status=AttendanceSession.Status.CLOSED,
                        expires_at=created + dt.timedelta(minutes=5),
                        closed_at=created + dt.timedelta(minutes=5),
                    )
                    AttendanceSession.objects.filter(pk=session.pk).update(created_at=created)
                    sessions += 1
                    for student in roster:
                        if random.random() < min(max(diligence[student.id], 0.35), 0.98):
                            lat = float(session.latitude) + random.uniform(-0.00025, 0.00025)
                            lng = float(session.longitude) + random.uniform(-0.00025, 0.00025)
                            AttendanceRecord.objects.create(
                                session=session, student=student,
                                status=AttendanceRecord.Status.PRESENT,
                                marked_at=created + dt.timedelta(seconds=random.randint(20, 280)),
                                latitude=round(lat, 6), longitude=round(lng, 6),
                                accuracy_m=random.uniform(5, 25),
                                distance_m=round(haversine_m(session.latitude, session.longitude, lat, lng), 2),
                                ip="10.0.0.%d" % random.randint(2, 250),
                                device_fingerprint=f"seed{student.id}",
                            )
                            records += 1
            day += dt.timedelta(days=1)

        self.stdout.write(self.style.SUCCESS(
            f"\nSeeded '{institute.name}'\n"
            f"  departments : {Department.objects.filter(institute=institute).count()}\n"
            f"  teachers    : {User.objects.filter(institute=institute, role='TEACHER').count()}\n"
            f"  students    : {total_students}\n"
            f"  sessions    : {sessions}\n"
            f"  marks       : {records}\n"
        ))
        self.stdout.write("Sign in with any of these (password: %s)" % PASSWORD)
        self.stdout.write("  head@demo.edu           — head of institute")
        self.stdout.write("  hod.cse@demo.edu        — head of department")
        self.stdout.write("  teacher1.cse@demo.edu   — teacher")
        self.stdout.write("  cse2022001@demo.edu     — student")
