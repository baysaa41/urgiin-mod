"""Медиа файлыг бичлэгтэй нь хамт цэвэрлэнэ.

Django-гийн FileField нь бичлэг устгах/файл солиход хуучин файлыг storage-аас
устгадаггүй — S3 дээр «өнчин» хувийн зураг үүрд үлддэг. Устгалыг
`transaction.on_commit`-оор хийнэ: transaction буцаагдвал файл хэвээр үлдэнэ.
"""

import logging

from django.db import transaction
from django.db.models.signals import post_delete, pre_save
from django.dispatch import receiver

from .models import Media

logger = logging.getLogger(__name__)

_FILE_FIELDS = ("file", "thumbnail")


def _delete_after_commit(storage, name):
    def _delete():
        try:
            storage.delete(name)
        except Exception:
            # Файл устгаж чадаагүй нь хэрэглэгчийн үйлдлийг унагах ёсгүй.
            logger.exception("Медиа файл устгаж чадсангүй: %s", name)

    transaction.on_commit(_delete)


@receiver(post_delete, sender=Media)
def delete_media_files(sender, instance, **kwargs):
    for field in _FILE_FIELDS:
        f = getattr(instance, field)
        if f and f.name:
            _delete_after_commit(f.storage, f.name)


@receiver(pre_save, sender=Media)
def delete_replaced_media_files(sender, instance, **kwargs):
    """Файл/thumbnail солигдвол хуучныг нь устгана."""
    if not instance.pk:
        return
    old = Media.objects.filter(pk=instance.pk).only(*_FILE_FIELDS).first()
    if old is None:
        return
    for field in _FILE_FIELDS:
        old_file = getattr(old, field)
        new_name = getattr(getattr(instance, field), "name", None)
        if old_file and old_file.name and old_file.name != new_name:
            _delete_after_commit(old_file.storage, old_file.name)
