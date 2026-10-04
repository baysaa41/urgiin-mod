"""Тестийн үед `MEDIA_ROOT`-ыг түр хавтас руу шилжүүлнэ.

Асуудал: `MediaForm`-ыг шалгадаг тестүүд файл хадгалдаг бөгөөд тэр нь бодит
`media/uploads/<он>/<сар>/` руу бичигддэг байсан. `Media` мөр нь 0 байсан ч
хавтас дотор 349 хог файл (`test_*.txt`, `z_*.jpg`) хуримтлагдсан — тестийн
гаж нөлөө бодит өгөгдлийн хажууд оршиж байв.

Шийдэл: бүх тестийн ажиллагааны туршид `MEDIA_ROOT`-ыг түр хавтас руу
`override_settings`-ээр сольж, төгсгөлд нь бүхэлд нь устгана. Тест бүрд
`@override_settings` тавих шаардлагагүй — нэг газраас удирдана.

`settings.TEST_RUNNER`-ээр холбоно.
"""

import shutil
import tempfile

from django.test.runner import DiscoverRunner
from django.test.utils import override_settings


class MediaRootDiscoverRunner(DiscoverRunner):
    """`manage.py test`-ийг түр `MEDIA_ROOT`-той ажиллуулна."""

    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        self._media_root = tempfile.mkdtemp(prefix="urgiin-test-media-")
        self._media_override = override_settings(MEDIA_ROOT=self._media_root)
        self._media_override.enable()

    def teardown_test_environment(self, **kwargs):
        self._media_override.disable()
        shutil.rmtree(self._media_root, ignore_errors=True)
        super().teardown_test_environment(**kwargs)
