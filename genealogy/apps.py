from django.apps import AppConfig


class GenealogyConfig(AppConfig):
    name = 'genealogy'

    def ready(self):
        from . import signals  # noqa: F401 — медиа файлын цэвэрлэгээ
