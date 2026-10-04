"""Ургийн овог хоосон хүмүүст эцгийн овгийг нөхнө.

Овог нь эцгээсээ дамжддаг тул гараар бичих шаардлагагүй. Шинэ бичлэг ба
шинэ холбоонд дүрэм автоматаар ажилладаг (`Person.inherit_clan_name`,
`services._link`); энэ команд нь хуучин бичлэгүүдийг нэг удаа нөхнө.

Анхдагчаар ЗӨВХӨН ХАРУУЛНА — бичихийн тулд `--apply` нэмнэ.
"""

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q

from genealogy.models import Person


class Command(BaseCommand):
    help = "Ургийн овог хоосон хүмүүст эцгийн овгийг нөхнө (анхдагчаар зөвхөн харна)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true",
                            help="Үнэхээр бичих (эс бөгөөс зөвхөн жагсаана).")

    def handle(self, *args, **options):
        # Овог нь хоосон, гэхдээ эцэг нь овогтой хүмүүс.
        candidates = (Person.objects.alive()
                      .filter(Q(clan_name="") | Q(clan_name__isnull=True))
                      .select_related("birth_date", "death_date"))
        plans = [(p, p.father) for p in candidates
                 if p.father is not None and p.father.clan_name]

        if not plans:
            self.stdout.write(self.style.SUCCESS("Нөхөх овог байхгүй — бүгд бүрэн байна."))
            return

        self.stdout.write(f"{len(plans)} хүний ургийн овгийг нөхнө:")
        for person, father in plans:
            self.stdout.write(
                f"  #{str(person.pk)[:8]} {person.display_name:<30} "
                f"← {father.clan_name}   (эцэг: #{father.pk} {father.display_name})")

        if not options["apply"]:
            self.stdout.write(self.style.WARNING(
                "Бичээгүй — `--apply` нэмж ажиллуулна уу."))
            return

        with transaction.atomic():
            changed = sum(1 for person, _ in plans if person.inherit_clan_name())
        self.stdout.write(self.style.SUCCESS(f"Нөхөгдлөө: {changed} хүн."))
