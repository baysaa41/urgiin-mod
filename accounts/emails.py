"""Урилгын имэйл — хүний засах хуудасны «Урилга илгээх» дуудна.

Имэйл нь цорын ганц зам биш: холбоос нь дэлгэц дээр ч харагддаг тул SMTP
тохируулаагүй орчинд (жишээ нь локал хөгжүүлэлт) ч урилга ажиллана.
"""

import logging

from django.conf import settings
from django.core.mail import send_mail

logger = logging.getLogger(__name__)


def send_invite_email(invite, link):
    """Урилгын линкийг илгээнэ. Алдаа гарвал зөвхөн лог бичнэ — урсгалыг зогсоохгүй."""
    try:
        send_mail(
            subject="Ургийн мод — урилга",
            message=(
                "Сайн байна уу?\n\n"
                "Танд ургийн модны системд нэвтрэх урилга ирлээ.\n"
                "Доорх холбоосоор орж нууц үгээ тавина уу "
                f"({settings.INVITE_EXPIRY_DAYS} хоногийн дотор):\n\n{link}\n"
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[invite.email],
            fail_silently=True,
        )
    except Exception:                       # pragma: no cover - сүлжээний алдаа
        # `fail_silently=True` ч гэсэн холболтын алдаа гарч болно. Чимээгүй
        # өнгөрөх нь имэйл яваагүйг мэдэхгүй байх аюултай тул лог бичнэ.
        logger.exception("Урилгын имэйл илгээж чадсангүй: %s", invite.email)
