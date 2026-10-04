from django.urls import path

from . import views

app_name = "genealogy"

urlpatterns = [
    path("", views.home, name="home"),

    # Хүн
    path("person/new/", views.PersonCreateView.as_view(), name="person_create"),
    path("person/<int:pk>/", views.person_detail, name="person_detail"),
    path("person/<int:pk>/edit/", views.PersonUpdateView.as_view(), name="person_edit"),
    path("person/<int:pk>/delete/", views.PersonDeleteView.as_view(), name="person_delete"),
    path("person/<int:pk>/quick-add/<str:relation>/", views.quick_add, name="quick_add"),
    path("person/<int:pk>/link/<str:relation>/", views.link_relative, name="link_relative"),
    path("person/picker/", views.person_picker, name="person_picker"),
    # Хүн ↔ бүртгэл (хэрэглэгч) холбоос — засах хуудаснаас
    path("person/<int:pk>/account/link/", views.person_account_link,
         name="person_account_link"),
    path("person/<int:pk>/account/unlink/", views.person_account_unlink,
         name="person_account_unlink"),
    path("person/<int:pk>/account/invite/", views.person_account_invite,
         name="person_account_invite"),

    # Гэр бүл ба үйл явдал
    path("family/<int:pk>/edit/", views.FamilyUpdateView.as_view(), name="family_edit"),
    path("events/new/", views.EventCreateView.as_view(), name="event_create"),

    # Мод
    path("tree/", views.tree_view, name="tree"),
    path("tree/<int:pk>/", views.tree_view, name="tree_person"),
    path("tree/<int:pk>/data/", views.tree_data, name="tree_data"),

    # Хайлт
    path("search/", views.search, name="search"),
    path("lookup/", views.person_lookup, name="person_lookup"),

    # Газар
    path("places/new/", views.place_create, name="place_create"),

    # Эх сурвалж ба медиа
    path("sources/", views.SourceListView.as_view(), name="source_list"),
    path("sources/new/", views.SourceCreateView.as_view(), name="source_create"),
    path("media/upload/", views.MediaUploadView.as_view(), name="media_upload"),
    path("media/<int:pk>/serve/", views.serve_media, name="media_serve"),

    # Админ хэрэгсэл
    path("manage/duplicates/", views.duplicates, name="duplicates"),
    path("manage/merge/", views.merge_view, name="merge"),
    path("manage/audit/", views.audit_list, name="audit"),
]
