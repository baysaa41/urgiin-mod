"""Хаана ч холбогдоогүй `FlexDate` мөрүүдийг устгана.

Ийм мөр нь хэрэглэгч форм бөглөж байхдаа өөр талбар дээр алдаа гарснаас
үлддэг (огноо нь форм шалгах үед үүсдэг). Функциональ хор хөнөөлгүй ч
санд хэрэггүй өгөгдөл хуримтлагдана.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from genealogy.models import Event, Family, FlexDate, Media, Person

USES = [
    (Person, ("birth_date_id", "death_date_id")),
    (Family, ("marriage_date_id", "divorce_date_id")),
    (Event, ("date_id",)),
    (Media, ("date_id",)),
]


def referenced_ids():
    used = set()
    for model, fields in USES:
        for field in fields:
            used |= set(model.objects.values_list(field, flat=True))
    used.discard(None)
    return used


class Command(BaseCommand):
    help = "Хаана ч холбогдоогүй FlexDate мөрүүдийг устгана (эсвэл --dry-run-аар харуулна)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Устгахгүй, зөвхөн жагсаана.")

    def handle(self, *args, **options):
        orphans = FlexDate.objects.exclude(pk__in=referenced_ids())
        count = orphans.count()

        if count == 0:
            self.stdout.write(self.style.SUCCESS("Хэрэггүй огноо байхгүй — цэвэр байна."))
            return

        if options["dry_run"]:
            self.stdout.write(f"Устгах {count} мөр:")
        else:
            self.stdout.write(f"{count} хэрэггүй огноог устгаж байна:")
        for obj in orphans:
            self.stdout.write(f"  #{obj.pk}  {obj.raw!r}")

        if not options["dry_run"]:
            with transaction.atomic():
                deleted, _ = orphans.delete()
            self.stdout.write(self.style.SUCCESS(f"Устгагдлаа: {deleted}"))
