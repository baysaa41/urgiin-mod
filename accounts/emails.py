"""Имэйл илгээх туслахууд — урилга ба өөрийн засварын батламж.

Имэйл нь цорын ганц зам биш: холбоос нь дэлгэц дээр ч харагддаг тул SMTP
тохируулаагүй орчинд (жишээ нь локал хөгжүүлэлт) ч урилга ажиллана.
Батламжийн урсгал ч апп дотроо badge/жагсаалттай тул имэйлгүй ч ажиллана.

Хүлээн авагч бүрд ТУСАД НЬ илгээнэ — батлагчид бусдын хаягийг харахгүй.
"""

import logging

from django.conf import settings
from django.core.mail import send_mail
from django.urls import reverse

logger = logging.getLogger(__name__)


def send_invite_email(invite, link):
    """Урилгын линк, товч заавар ба холбоосуудыг илгээнэ.

    Имэйлд ГУРВАН холбоос байна: нууц үг тавих (`link`), дараа нь нэвтрэх
    хуудас, системийн заавар — ингэснээр хүлээн авагч хаанаас орохоо,
    системийг хэрхэн ашиглахаа урьдчилан мэдэж авна. `SITE_URL` нь бүрэн
    хаяг (`https://…`) тул линк нь имэйлд шууд дарагдана.

    Алдаа гарвал зөвхөн лог бичнэ — урсгалыг зогсоохгүй.
    """
    base = settings.SITE_URL.rstrip("/")
    # Батлагдаагүй editor урилга нь эхэндээ зөвхөн viewer эрх өгнө — хүлээн
    # авагч гайхахгүйн тулд урьдчилан хэлнэ.
    extra = ("\n   Анхаар: та эхэндээ энгийн хүний (viewer) эрхээр нэгдэнэ.\n"
             "   Editor эрх нь админы батламж шаардана — батлагдсаны дараа\n"
             "   автоматаар нээгдэнэ.\n"
             if invite.needs_editor_approval else "")
    try:
        send_mail(
            subject="Ургийн мод — урилга",
            message=(
                "Сайн байна уу?\n\n"
                "Танд «Ургийн мод» — гэр бүлийн түүхийн системд нэвтрэх урилга "
                "ирлээ.\n"
                f"Та {invite.person.named_display} хүнтэй холбогдоно.\n\n"
                "1) НУУЦ ҮГЭЭ ТАВИХ\n"
                f"   Доорх холбоосоор орж нууц үгээ тавина уу "
                f"({settings.INVITE_EXPIRY_DAYS} хоногийн дотор):\n\n"
                f"{link}\n\n"
                "2) ДАРАА НЬ НЭВТРЭХ\n"
                f"   Хуудас: {base}{reverse('accounts:login')}\n"
                f"   Нэвтрэх нэр (имэйл): {invite.email}\n\n"
                "3) ЗААВАР\n"
                f"   Системийг хэрхэн ашиглах: {base}{reverse('genealogy:help')}\n"
                "   · Мод, хайлт, профайл — нэвтэрсэн гишүүн бүрд нээлтэй\n"
                "   · Өөрийн бичлэгээ засах боломжтой\n"
                f"{extra}"
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[invite.email],
            fail_silently=True,
        )
    except Exception:                       # pragma: no cover - сүлжээний алдаа
        # `fail_silently=True` ч гэсэн холболтын алдаа гарч болно. Чимээгүй
        # өнгөрөх нь имэйл яваагүйг мэдэхгүй байх аюултай тул лог бичнэ.
        logger.exception("Урилгын имэйл илгээж чадсангүй: %s", invite.email)


def send_edit_request_email(recipient, person_name, submitter_email, diff_text, link):
    """Хүн өөрийн бичлэгээ зассан тухай батлагчид мэдэгдэнэ.

    `diff_text` нь урьдчилан бэлдсэн текст (`services.render_diff_text`) —
    энэ модуль нь `genealogy`-г импортолохгүй, зөвхөн примитив хүлээнэ.
    """
    try:
        send_mail(
            subject=f"Ургийн мод — батлах хүсэлт: {person_name}",
            message=(
                "Сайн байна уу?\n\n"
                f"{submitter_email} дараах хүний мэдээллийг өөрчилсөн байна:\n"
                f"{person_name}\n\n"
                f"Өөрчлөлт:\n{diff_text}\n\n"
                "Өөрчлөлт нь батлагдах хүртэл «батлагдаагүй» төлөвтэй байна.\n"
                f"Доорх холбоосоор орж шийдвэрлэнэ үү:\n\n{link}\n"
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[recipient],
            fail_silently=True,
        )
    except Exception:                       # pragma: no cover - сүлжээний алдаа
        logger.exception("Батлах хүсэлтийн имэйл илгээж чадсангүй: %s", recipient)


def send_edit_resolved_email(recipient, person_name, approved, link):
    """Засвар батлагдсан эсвэл татгалзсаныг хүсэлт гаргасан хүнд мэдэгдэнэ."""
    if approved:
        subject = "Ургийн мод — таны засвар батлагдлаа"
        body = f"{person_name} хүний таны оруулсан засвар батлагдлаа."
    else:
        subject = "Ургийн мод — таны засвар татгалзлаа"
        body = (f"{person_name} хүний таны оруулсан засвар татгалзагдаж, "
                "хуучин мэдээлэл сэргээгдлээ.")
    try:
        send_mail(
            subject=subject,
            message=("Сайн байна уу?\n\n" + body + f"\n\n{link}\n"),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[recipient],
            fail_silently=True,
        )
    except Exception:                       # pragma: no cover - сүлжээний алдаа
        logger.exception("Шийдвэрийн имэйл илгээж чадсангүй: %s", recipient)
