"""Урилга нь ЗААВАЛ модны хүнтэй холбогдоно.

Хаягаар нь чөлөөтэй урих зам хаагдсан тул `Invite.person` нь NULL байх
боломжгүй болов. Хуучин хүнгүй урилга байхгүй (шалгасан) тул нэг удаагийн
утга (default) шаардлагагүй — гараар бичсэн.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0002_invite_person_editrequest"),
        ("genealogy", "0005_person_linked_user"),
    ]

    operations = [
        migrations.AlterField(
            model_name="invite",
            name="person",
            field=models.ForeignKey(
                help_text="Хүлээн авахад энэ хүнтэй холбогдоно.",
                on_delete=django.db.models.deletion.CASCADE,
                related_name="invites",
                to="genealogy.person",
                verbose_name="Хүн",
            ),
        ),
    ]
