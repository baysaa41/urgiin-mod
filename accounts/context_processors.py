"""Template-ийн нийтлэг хувьсагчид."""

from .middleware import is_normal_view, real_user


def view_mode(request):
    """Админы харагдацын төлөв — navbar-т зориулж.

    `user` нь «энгийн харагдац»-д бүрхүүл болсон байдаг тул navbar-т
    `is_admin_user` (жинхэнэ эрх) тусад нь хэрэгтэй — эс бөгөөс админ
    унтраалгаа буцааж асаах товчоо алдана.
    """
    return {
        "is_admin_user": bool(getattr(real_user(request), "is_admin", False)),
        "normal_view": is_normal_view(request),
    }
