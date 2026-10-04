"""Админ/засварлагчийн «энгийн харагдац».

Засах эрхтэй хэрэглэгч (админ, засварлагч) сайт бусдад хэрхэн харагдахыг
шалгах боломжтой байх ёстой:
«Хаалттай» хүн `•••` болж, «Хувийн» хүний нас/хүйс хаалттай, засах товч
алга болно. Сонголт нь session-д хадгалагдана (бүх хуудсанд үйлчилнэ).

Хэрэгжүүлэлт: `request.user`-ыг нимгэн бүрхүүлээр СОЛИНО. Ингэснээр
`can_edit`/`is_admin` шалгадаг БҮХ одоо байгаа код (query, template, mixin)
нэг ч өөрчлөлтгүйгээр зөв ажиллана — дүрэм нэг газраас удирдагдана.
"""

VIEW_MODE_SESSION_KEY = "view_mode"
MODE_ADMIN = "admin"
MODE_NORMAL = "normal"
VIEW_MODES = (MODE_ADMIN, MODE_NORMAL)


class NormalViewUser:
    """«Энгийн харагдац»-ын хэрэглэгч — зөвхөн унших гишүүн мэт.

    Зөвхөн `can_edit` ба `is_admin` нь хаалттай; бусад бүх шинж (pk,
    is_authenticated, email, …) нь жинхэнэ хэрэглэгчээс дамжина.
    """

    can_edit = False
    is_admin = False

    def __init__(self, user):
        self._user = user

    def __getattr__(self, name):
        # Зөвхөн ердийн хайлтаар олдоогүй үед дуудагдана.
        return getattr(self._user, name)

    def __str__(self):
        return str(self._user)

    def __repr__(self):
        return f"<NormalViewUser: {self._user!r}>"


def is_normal_view(request):
    """Хүсэлт «энгийн харагдац»-аар ирсэн эсэх (template/context-д)."""
    return isinstance(getattr(request, "user", None), NormalViewUser)


class AdminViewModeMiddleware:
    """Админ/засварлагч «энгийн харагдац»-ыг сонгосон бол эрхийг нь хаана.

    `AuthenticationMiddleware`-ийн ДАРАА, `LoginRequiredMiddleware`-ийн
    ӨМНӨ байх ёстой — доод бүх middleware аль хэдийн хаалттай хэрэглэгчийг
    харна.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        # Жинхэнэ хэрэглэгчийг тусдаа хадгална: унтраалга нь эрхээ буцааж
        # асаахын тулд «би засах эрхтэй юу» гэдгийг мэдэх ёстой.
        request.real_user = user
        if (user is not None
                and getattr(user, "is_authenticated", False)
                and getattr(user, "can_edit", False)
                and request.session.get(VIEW_MODE_SESSION_KEY) == MODE_NORMAL):
            request.user = NormalViewUser(user)
        return self.get_response(request)


def real_user(request):
    """Бүрхүүлээс үл хамаарах жинхэнэ хэрэглэгч."""
    return getattr(request, "real_user", None) or getattr(request, "user", None)
