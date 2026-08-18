"""
Synthetic data for a working demo, generated as JSON before anything touches
the database.

**Why two stages.** Generating and loading are different jobs with different
failure modes. A generator that writes JSON can be run, read, diffed and
committed; you can see exactly what a seed will do before it does it, and when
a load fails you can tell whether the data or the loading was wrong. Doing both
in one pass hides that seam, and the only place the mistake shows up is a
half-populated database.

The JSON is also the fixture: `--from-file` loads one somebody edited by hand,
which is how you build a case that reproduces a specific bug.

**Everything here is deterministic.** One `Random` seeded from a fixed value,
so the same seed gives byte-identical JSON. A demo database that differs run to
run is one you cannot describe to anyone else.

**What it covers.** Not a happy path — the states that are awkward to reach by
hand are exactly the ones worth having on tap:

* universities from the shipped list, with real affiliations;
* institutes that are approved, pending and rejected-with-a-reason;
* disciplines both affiliated and autonomous on the same institute;
* departments active and archived, one of them sitting in a discipline the
  institute does not hold — a **revoked department full of active students**,
  which is the case that exposed the status/revocation split: the count read
  the revoked label and reported zero while the department was full;
* every value of `status` — ACTIVE, INVITED and ARCHIVED — on people and on
  rows, so a status filter has something to find in each position;
* attendance across a term, with present, absent and teacher-marked records;
* absence reasons approved, rejected with a remark, and still pending;
* planned absences, including one decided per subject and one cancelled;
* a university-owned subject pushed into its affiliated institutes, so the
  read-only path has data behind it.
"""
import random
from datetime import date, timedelta

# --------------------------------------------------------------------------- #
#  Name pools. Small on purpose: recognisable in a screenshot, and the
#  combinations are plenty for a few thousand rows.
# --------------------------------------------------------------------------- #
FIRST_NAMES = [
    "Aarav", "Aditi", "Ananya", "Arjun", "Bhavna", "Chirag", "Deepa", "Devansh",
    "Farhan", "Gauri", "Harsh", "Ishita", "Kabir", "Kavya", "Manish", "Meera",
    "Nikhil", "Nisha", "Pooja", "Rahul", "Riya", "Rohan", "Sanjana", "Siddharth",
    "Sneha", "Tanvi", "Uday", "Varun", "Yash", "Zoya",
]
LAST_NAMES = [
    "Agarwal", "Banerjee", "Chatterjee", "Desai", "Gupta", "Iyer", "Joshi",
    "Kapoor", "Menon", "Mehta", "Nair", "Patel", "Rao", "Reddy", "Sharma",
    "Singh", "Verma", "Yadav",
]

# (code, name, discipline) — the discipline is what decides who governs it.
DEPARTMENT_POOL = [
    ("CSE", "Computer Science & Engineering", "ENGG"),
    ("ECE", "Electronics & Communication", "ENGG"),
    ("MECH", "Mechanical Engineering", "ENGG"),
    ("PHARM", "Pharmaceutical Sciences", "PHARMACY"),
    ("COM", "Commerce & Management", "GENERAL"),
    ("ARTS", "Arts & Humanities", "GENERAL"),
]

SUBJECT_POOL = {
    "CSE": [("DSA", "Data Structures & Algorithms", "THEORY"),
            ("DBMS", "Database Management Systems", "THEORY"),
            ("OSL", "Operating Systems Lab", "PRACTICAL"),
            ("AI", "Artificial Intelligence", "THEORY")],
    "ECE": [("SIG", "Signals & Systems", "THEORY"),
            ("VLSI", "VLSI Design", "THEORY"),
            ("ECL", "Electronics Lab", "PRACTICAL")],
    "MECH": [("THERM", "Thermodynamics", "THEORY"),
             ("FLUID", "Fluid Mechanics", "THEORY"),
             ("WSL", "Workshop Lab", "PRACTICAL")],
    "PHARM": [("PCG", "Pharmacognosy", "THEORY"),
              ("PCL", "Pharmaceutics Lab", "PRACTICAL")],
    "COM": [("ACC", "Financial Accounting", "THEORY"),
            ("ECO", "Microeconomics", "THEORY")],
    "ARTS": [("ENG", "English Literature", "THEORY"),
             ("HIS", "Modern History", "THEORY")],
    # The revoked department gets subjects too, so the revoked case covers
    # subjects and batches and not only students. Without these it held people
    # and nothing else, and the read-only path had no revoked subject to show.
    "AGRI": [("AGR", "Agronomy", "THEORY"),
             ("SOIL", "Soil Science", "THEORY"),
             ("AGL", "Agriculture Lab", "PRACTICAL")],
}

REJECTION_REASONS = [
    "The affiliation certificate on file has expired.",
    "The head's email domain does not match the institute's.",
    "Duplicate of an existing registration — see AICTE code on record.",
]
ABSENCE_REASONS = [
    "Down with fever, doctor's note attached.",
    "Attending my sister's wedding out of town.",
    "Train delayed by four hours on the return journey.",
    "Represented the college at the inter-university meet.",
    "Family emergency — had to travel home at short notice.",
]
APPROVE_REMARKS = ["Medical certificate seen.", "Cleared by the HoD.",
                   "Official college duty."]
REJECT_REMARKS = ["No supporting document was provided.",
                  "Submitted after the three-day window.",
                  "The same reason was used twice this month."]


def _student_status(index, per_batch):
    """
    Which status the nth student of a cohort gets.

    Fixed positions so every status is present whatever the cohort size: the
    first is mid-signup, the last is deactivated once there are at least three,
    and the rest are running. A modulo rule reads more naturally and silently
    produces a cohort with no archived students when the batch is small.
    """
    if index == 0:
        return "INVITED"
    if per_batch >= 3 and index == per_batch - 1:
        return "ARCHIVED"
    return "ACTIVE"


class Generator:
    """
    Builds the whole fixture in memory, then hands back a plain dict.

    Every id is a string key made up here ("inst-1", "sub-inst-1-CSE-DSA")
    rather than a database id — the JSON has to be loadable into an empty
    database, so it cannot reference rows that do not exist yet. The loader
    keeps a map from these keys to real objects as it goes.
    """

    def __init__(self, *, seed=20260811, institutes=4, students_per_batch=18,
                 weeks=6, today=None):
        self.rng = random.Random(seed)
        self.n_institutes = institutes
        self.students_per_batch = students_per_batch
        self.weeks = weeks
        self.today = today or date.today()
        self.used_emails = set()
        self.data = {
            "meta": {}, "universities": [], "institutes": [], "affiliations": [],
            "departments": [], "batches": [], "subjects": [], "users": [],
            "students": [], "enrolments": [], "assignments": [], "sessions": [],
            "records": [], "absence_reasons": [], "planned_absences": [],
        }

    # ---------------------------------------------------------------- helpers
    def person(self):
        return (f"{self.rng.choice(FIRST_NAMES)} {self.rng.choice(LAST_NAMES)}")

    def email(self, local, domain):
        """A login that is unique across the whole fixture."""
        base = "".join(c for c in local.lower() if c.isalnum() or c in "._-")
        candidate = f"{base}@{domain}"
        n = 2
        while candidate in self.used_emails:
            candidate = f"{base}{n}@{domain}"
            n += 1
        self.used_emails.add(candidate)
        return candidate

    def mobile(self):
        return f"+9198{self.rng.randint(10000000, 99999999)}"

    # ------------------------------------------------------------ universities
    def build_universities(self):
        """
        Three affiliating bodies drawn from the shipped list, so the demo lines
        up with what `seed_universities` would create rather than inventing
        names nobody will recognise.
        """
        from academics import reference

        by_discipline = reference.seed_universities()
        picks = []
        for discipline in ("ENGG", "PHARMACY", "GENERAL"):
            names = sorted(by_discipline.get(discipline, []))
            if names:
                picks.append((discipline, names[self.rng.randrange(len(names))]))

        for i, (discipline, name) in enumerate(picks, start=1):
            key = f"uni-{i}"
            short = "".join(w[0] for w in name.split()
                            if w[0].isupper())[:8] or f"U{i}"
            self.data["universities"].append({
                "key": key,
                "name": name,
                "short_name": short,
                "code": f"UNI{i}",
                "email": f"registrar@{short.lower() or 'uni'}.ac.in",
                "disciplines": [discipline],
                "grants_affiliation": True,
                "admin": {
                    "email": self.email(f"registrar.{short}", "demo.geoattend.in"),
                    "full_name": self.person(),
                    "phone": self.mobile(),
                },
            })
        return [u["key"] for u in self.data["universities"]]

    # --------------------------------------------------------------- institutes
    def build_institutes(self, university_keys):
        """
        A spread of statuses on purpose. One pending and one rejected institute
        mean the university's approval queue and its rejected tab both have
        something in them the first time anyone opens the screen.
        """
        # Two of each kind at minimum. One approved institute would leave every
        # cross-institute screen — the university's dashboard, the Institute
        # column, the institute filter — with nothing to compare, which is
        # exactly what those screens exist to do.
        approved = max(self.n_institutes - 2, 2)
        statuses = ["APPROVED"] * approved + ["PENDING", "REJECTED"]
        self.n_institutes = len(statuses)

        places = [("Kerala", "Ernakulam"), ("Karnataka", "Bengaluru Urban"),
                  ("Maharashtra", "Pune"), ("Tamil Nadu", "Coimbatore"),
                  ("West Bengal", "Kolkata"), ("Gujarat", "Surat")]

        for i, status in enumerate(statuses, start=1):
            key = f"inst-{i}"
            state, district = places[(i - 1) % len(places)]
            name = f"{district} Institute of Technology"
            code = f"INST{i:02d}"
            domain = f"{code.lower()}.demo.geoattend.in"

            self.data["institutes"].append({
                "key": key,
                "name": name,
                "code": code,
                "email": f"office@{domain}",
                "phone": self.mobile(),
                "website": f"https://{domain}",
                "address": f"{self.rng.randint(1, 90)} College Road, {district}",
                "state": state,
                "district": district,
                "status": status,
                "rejection_reason": (self.rng.choice(REJECTION_REASONS)
                                     if status == "REJECTED" else ""),
                "domain": domain,
            })

            # Engineering under a university; one discipline autonomous, so the
            # institute has something of its own to manage. That pairing is the
            # whole point of the per-discipline design and is fiddly to set up
            # by hand.
            self.data["affiliations"].append({
                "institute": key, "discipline": "ENGG",
                "university": university_keys[0] if university_keys else None,
            })
            self.data["affiliations"].append({
                "institute": key, "discipline": "GENERAL", "university": None,
            })
            if i % 2 == 0 and len(university_keys) > 1:
                self.data["affiliations"].append({
                    "institute": key, "discipline": "PHARMACY",
                    "university": university_keys[1],
                })
        return [inst["key"] for inst in self.data["institutes"]]

    # -------------------------------------------------------------- departments
    def build_departments(self, institute_key, index):
        """
        Three or four departments, one of them archived, and on one institute a
        department whose discipline is *not* on file — the only way to see a
        revoked row without performing a removal first.
        """
        # Read the affiliations that were actually generated rather than
        # re-deriving them from the index. The first version computed both from
        # `i % 2` and `index % 2` — off by one from each other — so every
        # institute got a Pharmacy department in a discipline it did not hold
        # and three departments came out accidentally revoked. Deriving from
        # one source cannot drift from itself.
        held = {a["discipline"] for a in self.data["affiliations"]
                if a["institute"] == institute_key}
        chosen = [d for d in DEPARTMENT_POOL if d[2] in held]

        keys = []
        for n, (code, name, discipline) in enumerate(chosen):
            key = f"dept-{institute_key}-{code}"
            archived = (n == len(chosen) - 1 and index == 1)
            self.data["departments"].append({
                "key": key, "institute": institute_key,
                "code": code, "name": name, "discipline": discipline,
                # `status` is the row's own lifecycle. Revocation is a separate
                # fact and is never written here — the loader lets the model
                # derive it from the affiliations, which is the one place that
                # can be right. See core/enums.py.
                "status": "ARCHIVED" if archived else "ACTIVE",
            })
            keys.append(key)

        # The revoked case: a department in a discipline this institute does
        # not hold. Only on the first institute, so the demo has exactly one
        # and it is easy to point at.
        if index == 0:
            # Named for its own discipline, not borrowed from the pool. The
            # first version called it ARTS, which the pool already supplies
            # once GENERAL is held — two departments with one name in one
            # institute, and a unique constraint that says so.
            key = f"dept-{institute_key}-AGRI"
            assert "AGRI" not in held, "AGRI must stay unaffiliated for this"
            self.data["departments"].append({
                "key": key, "institute": institute_key,
                "code": "AGRI", "name": "Agricultural Sciences",
                # Active, and about to be revoked by having no AGRI
                # affiliation. That pairing — a live department nobody
                # affiliates, holding live students — is the exact case that
                # used to report zero.
                # AGRI is deliberately never affiliated, so this department is
                # governed by nobody and every screen reads it as revoked. One
                # institute only, so the example is easy to point at.
                "discipline": "AGRI",
                "status": "ACTIVE",
            })
            keys.append(key)
        return keys

    # ------------------------------------------------------------------ people
    def build_staff(self, institute, department_keys):
        """The head, plus a HoD and two teachers per department."""
        domain = institute["domain"]
        head_key = f"user-{institute['key']}-head"
        self.data["users"].append({
            "key": head_key, "role": "HEAD", "institute": institute["key"],
            "department": None, "full_name": self.person(),
            "email": self.email(f"head.{institute['code']}", domain),
            "phone": self.mobile(), "status": "ACTIVE",
            "registration_completed": True,
        })

        teachers = {}
        for index, department_key in enumerate(department_keys):
            code = department_key.rsplit("-", 1)[-1]
            hod_key = f"user-{department_key}-hod"
            self.data["users"].append({
                "key": hod_key, "role": "HOD", "institute": institute["key"],
                "department": department_key, "full_name": self.person(),
                "email": self.email(f"hod.{code}.{institute['code']}", domain),
                "phone": self.mobile(), "status": "ACTIVE",
                "registration_completed": True,
                "heads_department": department_key,
            })
            teachers[department_key] = []
            for n in range(2):
                teacher_key = f"user-{department_key}-t{n}"
                self.data["users"].append({
                    "key": teacher_key, "role": "TEACHER",
                    "institute": institute["key"], "department": department_key,
                    "full_name": self.person(),
                    "email": self.email(f"{code}.teacher{n + 1}.{institute['code']}",
                                        domain),
                    "phone": self.mobile(),
                    # Keyed on the department's position rather than its
                    # code: an institute whose departments happen not to
                    # include CSE or ECE would otherwise have no invited or
                    # deactivated teacher at all.
                    "status": ("INVITED" if n == 1 and index == 0
                               else "ARCHIVED" if n == 1 and index == 1
                               else "ACTIVE"),
                    "registration_completed": not (n == 1 and index == 0),
                })
                teachers[department_key].append(teacher_key)
        return head_key, teachers

    def build_academics(self, institute, department_keys):
        """Batches, subjects and the students enrolled in them."""
        subjects, batches, students = {}, {}, []
        start_year = self.today.year - 2

        for department_key in department_keys:
            code = department_key.rsplit("-", 1)[-1]

            # Two cohorts: one running, one graduated and archived — so an
            # archived batch exists without anyone having to make one.
            batches[department_key] = []
            for n, (offset, active) in enumerate(((0, True), (-4, False))):
                label = f"{start_year + offset}-{(start_year + offset + 4) % 100:02d}"
                key = f"batch-{department_key}-{n}"
                self.data["batches"].append({
                    "key": key, "department": department_key, "label": label,
                    "start_year": start_year + offset,
                    "end_year": start_year + offset + 4,
                    "status": "ACTIVE" if active else "ARCHIVED",
                })
                batches[department_key].append(key)

            subjects[department_key] = []
            for n, (subject_code, name, kind) in enumerate(
                    SUBJECT_POOL.get(code, [])):
                key = f"sub-{department_key}-{subject_code}"
                self.data["subjects"].append({
                    "key": key, "department": department_key,
                    "code": subject_code, "name": name, "subject_type": kind,
                    "degree": "BACHELOR",
                    "semester": self.rng.randint(1, 8),
                    "credits": 2 if kind == "PRACTICAL" else 4,
                    # The last subject of each department is retired, so the
                    # Archived position of the status filter is never empty.
                    "status": ("ARCHIVED"
                               if n == len(SUBJECT_POOL.get(code, [])) - 1
                               and len(SUBJECT_POOL.get(code, [])) > 2
                               else "ACTIVE"),
                    "owner_university": None,
                })
                subjects[department_key].append(key)

            live_batch = batches[department_key][0]
            for n in range(self.students_per_batch):
                name = self.person()
                key = f"stu-{department_key}-{n}"
                self.data["users"].append({
                    "key": f"user-{key}", "role": "STUDENT",
                    "institute": institute["key"], "department": None,
                    "full_name": name,
                    "email": self.email(
                        f"{name.split()[0]}.{code}{n + 1}.{institute['code']}",
                        institute["domain"]),
                    "phone": self.mobile(),
                    # Fixed positions, not a modulo. `n % 11 == 5` looked
                    # more natural and never fired below twelve students, so a
                    # small seed had no archived students at all and the
                    # Archived filter came up empty on a dataset that claimed
                    # to cover every status.
                    "status": _student_status(n, self.students_per_batch),
                    "registration_completed": _student_status(
                        n, self.students_per_batch) != "INVITED",
                })
                self.data["students"].append({
                    "key": key, "user": f"user-{key}",
                    "department": department_key, "batch": live_batch,
                    "class_roll": f"{n + 1:02d}",
                    "exam_roll": f"{code}{start_year}{n + 1:03d}",
                    "mobile": self.mobile(),
                    "guardian_name": f"{self.rng.choice(['Mr.', 'Mrs.'])} "
                                     f"{name.split()[-1]}",
                    "guardian_mobile": self.mobile(),
                    "guardian_email": "",
                    # Mirrors the account: the profile has to agree with the
                    # user or a status filter and a status column disagree on
                    # screen.
                    "status": _student_status(n, self.students_per_batch),
                })
                students.append(key)
                for subject_key in subjects[department_key]:
                    self.data["enrolments"].append(
                        {"student": key, "subject": subject_key})
        return subjects, batches, students

    # -------------------------------------------------------------- attendance
    def build_attendance(self, institute, department_keys, subjects, batches,
                         teachers, students_by_department):
        """
        A term of classes, with the record mix a real register has: mostly
        present, some absent, a few marked by the teacher afterwards.

        `MANUAL` matters — every "how much of this was typed in" figure on the
        dashboard reads it, and a demo without any would show those as zero and
        look broken.
        """
        for department_key in department_keys:
            department_subjects = subjects.get(department_key) or []
            department_students = students_by_department.get(department_key) or []
            if not department_subjects or not department_students:
                continue
            live_batch = batches[department_key][0]
            teacher_keys = teachers[department_key]

            for week in range(self.weeks):
                for subject_key in department_subjects[:3]:
                    day = self.today - timedelta(days=(self.weeks - week) * 7
                                                 + self.rng.randint(0, 4))
                    session_key = f"ses-{subject_key}-{week}"
                    teacher_key = teacher_keys[week % len(teacher_keys)]
                    self.data["sessions"].append({
                        "key": session_key, "teacher": teacher_key,
                        "subject": subject_key, "batch": live_batch,
                        "session_date": day.isoformat(),
                        "latitude": round(self.rng.uniform(8.4, 27.2), 6),
                        "longitude": round(self.rng.uniform(72.8, 88.4), 6),
                        "radius_m": 50,
                        "status": "CLOSED",
                        "expected_count": len(department_students),
                        "note": f"Week {week + 1}",
                    })

                    for student_key in department_students:
                        roll = self.rng.random()
                        if roll < 0.78:
                            status = "PRESENT"
                        elif roll < 0.88:
                            status = "MANUAL"
                        else:
                            status = "ABSENT"
                        self.data["records"].append({
                            "session": session_key, "student": student_key,
                            "status": status,
                            "marked_by": teacher_key if status == "MANUAL" else None,
                        })

    def build_absences(self, department_keys, students_by_department, teachers,
                       subjects):
        """
        Absence reasons in all three states, and planned absences both decided
        and cancelled.

        Pending ones exist so the review queue and its badge are not empty; the
        rejected ones carry a remark, because a rejection without one is a
        state the UI can render and nobody should ever see.
        """
        # This institute's absences only. `build_absences` runs once per
        # institute, and the first version took the top 60 of *every* absent
        # record each time — so the same (session, student) pair was picked
        # again for the next institute and the unique constraint caught it.
        own_sessions = {s["key"] for s in self.data["sessions"]
                        if any(s["subject"].startswith(f"sub-{d}-")
                               for d in department_keys)}
        absent = [r for r in self.data["records"]
                  if r["status"] == "ABSENT" and r["session"] in own_sessions]
        self.rng.shuffle(absent)

        seen = getattr(self, "_absence_pairs", None)
        if seen is None:
            seen = self._absence_pairs = set()

        taken = 0
        for record in absent:
            pair = (record["session"], record["student"])
            if pair in seen:
                continue
            seen.add(pair)
            status = ("APPROVED", "REJECTED", "PENDING")[taken % 3]
            taken += 1
            self.data["absence_reasons"].append({
                "session": record["session"],
                "student": record["student"],
                "reason": self.rng.choice(ABSENCE_REASONS),
                "status": status,
                "review_remark": (
                    self.rng.choice(APPROVE_REMARKS) if status == "APPROVED"
                    else self.rng.choice(REJECT_REMARKS) if status == "REJECTED"
                    else ""),
            })
            if taken >= 45:
                break

        for department_key in department_keys:
            department_students = students_by_department.get(department_key) or []
            department_subjects = subjects.get(department_key) or []
            if not department_students or not department_subjects:
                continue
            for n in range(2):
                student_key = department_students[n % len(department_students)]
                start = self.today + timedelta(days=self.rng.randint(2, 20))
                self.data["planned_absences"].append({
                    "key": f"plan-{department_key}-{n}",
                    "student": student_key,
                    "from_date": start.isoformat(),
                    "to_date": (start + timedelta(days=2)).isoformat(),
                    "reason": self.rng.choice(ABSENCE_REASONS),
                    "all_subjects": n == 0,
                    # One of the two is cancelled by the student, which is a
                    # state the decision screens have to skip over.
                    "cancelled": n == 1,
                    "decisions": [
                        {"subject": subject_key,
                         "status": ("APPROVED", "REJECTED", "PENDING")[i % 3],
                         "review_remark": (
                             self.rng.choice(REJECT_REMARKS) if i % 3 == 1 else "")}
                        for i, subject_key in enumerate(department_subjects[:3])
                    ] if n == 0 else [],
                })

    # ------------------------------------------------------------------ public
    def build(self):
        university_keys = self.build_universities()
        institute_keys = self.build_institutes(university_keys)

        for index, institute_key in enumerate(institute_keys):
            institute = next(i for i in self.data["institutes"]
                             if i["key"] == institute_key)
            department_keys = self.build_departments(institute_key, index)
            head_key, teachers = self.build_staff(institute, department_keys)
            subjects, batches, _ = self.build_academics(institute, department_keys)

            students_by_department = {
                key: [s["key"] for s in self.data["students"]
                      if s["department"] == key]
                for key in department_keys
            }
            for department_key in department_keys:
                for subject_key in (subjects.get(department_key) or []):
                    for teacher_key in teachers[department_key]:
                        self.data["assignments"].append({
                            "teacher": teacher_key, "subject": subject_key,
                            "batch": batches[department_key][0],
                        })

            # Only approved institutes get a term of history. A pending one
            # has not started teaching, and showing attendance for a college
            # nobody has approved would be a state the app cannot reach.
            if institute["status"] == "APPROVED":
                self.build_attendance(institute, department_keys, subjects,
                                      batches, teachers, students_by_department)
                self.build_absences(department_keys, students_by_department,
                                    teachers, subjects)

        # The shared curriculum: one subject the university owns, pushed into
        # every affiliated institute's CSE department. Gives the read-only
        # path something to show.
        if self.data["universities"]:
            owner = self.data["universities"][0]["key"]
            for department in self.data["departments"]:
                if department["code"] != "CSE":
                    continue
                self.data["subjects"].append({
                    "key": f"sub-{department['key']}-UNIV",
                    "department": department["key"],
                    "code": "SYLB", "name": "University Core Syllabus",
                    "subject_type": "THEORY", "degree": "BACHELOR",
                    "semester": 1, "credits": 4, "status": "ACTIVE",
                    "owner_university": owner,
                })

        self.data["meta"] = {
            "generated_for": self.today.isoformat(),
            "counts": {k: len(v) for k, v in self.data.items()
                       if isinstance(v, list)},
        }
        return self.data
