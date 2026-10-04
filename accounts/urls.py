from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("login/", views.EmailLoginView.as_view(), name="login"),
    path("logout/", views.EmailLogoutView.as_view(), name="logout"),
    path("invite/", views.invite_list, name="invite_list"),
    path("invite/<uuid:token>/", views.invite_accept, name="invite_accept"),
    path("editor-request/", views.editor_request, name="editor_request"),
    path("editor-requests/", views.editor_request_list, name="editor_request_list"),
    path("editor-requests/<int:pk>/<str:action>/", views.editor_request_decide,
         name="editor_request_decide"),
    # Админы «энгийн / админ харагдац» унтраалга
    path("view-mode/", views.set_view_mode, name="set_view_mode"),
]
