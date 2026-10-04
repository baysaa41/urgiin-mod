from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("accounts.urls")),
    path("", include("genealogy.urls")),
]

# Медиаг ЗӨВХӨН DEBUG үед шууд үйлчилнэ (хөгжүүлэлтэд). Prod-д
# genealogy.views.serve_media (эрх шалгадаг) ашиглана.
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

admin.site.site_header = "Ургийн мод — удирдлага"
admin.site.site_title = "Ургийн мод"
admin.site.index_title = "Мэдээллийн сан"
