"""Нэвтрэлт ба урилга. Бүртгэл нээлттэй БИШ — зөвхөн урилгаар."""

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_not_required, login_required
from django.contrib.auth.views import LoginView, LogoutView
from django.db import IntegrityError, transaction
from django.http import Http404
from django.utils.decorators import method_decorator
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from genealogy.models import Person

from .forms import EmailAuthenticationForm, InviteAcceptForm
from .middleware import (
    MODE_ADMIN,
    MODE_NORMAL,
    VIEW_MODE_SESSION_KEY,
    VIEW_MODES,
    real_user,
)
from .models import EditRequest, Invite, User


@method_decorator(login_not_required, name="dispatch")
class EmailLoginView(LoginView):
    authentication_form = EmailAuthenticationForm
    template_name = "accounts/login.html"
    redirect_authenticated_user = True


class EmailLogoutView(LogoutView):
    next_page = "genealogy:home"


def _require_admin(request):
    return bool(getattr(request.user, "is_admin", False))


@login_required
def invite_list(request):
    """Илгээсэн урилгуудыг харна (зөвхөн админ).

    Урилга ЗӨВХӨН модны хүний засах хуудаснаас илгээгдэнэ — эндээс чөлөөтэй
    хаяг руу урих боломжгүй. Энэ хуудас нь хяналт: хэн, хэнд, ямар эрхээр
    урилга илгээсэн бэ.
    """
    if not _require_admin(request):
        messages.error(request, "Зөвхөн админ урилгыг харна.")
        return redirect("genealogy:home")

    invites = Invite.objects.select_related("invited_by", "person")[:50]
    return render(request, "accounts/invite_list.html", {"invites": invites})


def _linkable_person(invite):
    """Урилгад заасан хүн холбох боломжтой эсэх.

    `None` буцаана: ердийн (хүнгүй) урилга — хэвийн. `False` буцаана: хүнтэй
    урилга боловч холбох боломжгүй (нас барсан / нэгтгэгдсэн / аль хэдийн
    холбогдсон / устсан).
    """
    if invite.person_id is None:
        return None
    person = Person.objects.filter(pk=invite.person_id).first()
    if (person is None or person.merged_into_id is not None
            or person.linked_user_id is not None or person.is_deceased):
        return False
    return person


@login_not_required
def invite_accept(request, token):
    """Урилга хүлээн авагч нь нууц үгээ тавих хүртэл нэвтрээгүй байдаг тул
    энэ view нь нэвтрэлт шаардахгүй (цорын нэг ийм өгөгдлийн view)."""
    invite = get_object_or_404(Invite.objects.select_related("person"), token=token)
    if not invite.is_valid or _linkable_person(invite) is False:
        return render(request, "accounts/invite_invalid.html", {"invite": invite}, status=410)

    if request.method == "POST":
        form = InviteAcceptForm(user=None, data=request.POST)
        if form.is_valid():
            # Урилга үүсгэсний дараа энэ имэйлээр бүртгэл үүссэн байж болно —
            # тэгэхэд `create_user` нь IntegrityError (500) өгнө. Оронд нь
            # ойлгомжтой мессежтэй 410 буцаана.
            if User.objects.filter(email__iexact=invite.email).exists():
                return render(request, "accounts/invite_invalid.html",
                              {"invite": invite, "email_taken": True}, status=410)
            try:
                with transaction.atomic():
                    user = User.objects.create_user(
                        email=invite.email, password=form.cleaned_data["new_password1"],
                        role=invite.role, full_name=form.cleaned_data.get("full_name", ""),
                        is_staff=invite.role == User.Role.ADMIN,
                    )
                    # Хүнтэй урилга бол холбоосыг тавина. Урилга үүсгэснээс хойш
                    # хүн нас барсан/нэгтгэгдсэн бол `_linkable_person` дээр
                    # шүүгдсэн тул энд хүрэхгүй — гэхдээ давхар шалгалт хор хөнөөлгүй.
                    person = _linkable_person(invite)
                    if person is not None and person is not False:
                        Person.objects.filter(pk=person.pk, linked_user__isnull=True) \
                                      .update(linked_user=user)
                    invite.accepted_at = timezone.now()
                    invite.accepted_by = user
                    invite.save(update_fields=["accepted_at", "accepted_by"])
            except IntegrityError:
                return render(request, "accounts/invite_invalid.html",
                              {"invite": invite, "email_taken": True}, status=410)
            login(request, user, backend="django.contrib.auth.backends.ModelBackend")
            messages.success(request, "Тавтай морил! Нууц үг тавигдлаа.")
            return redirect("genealogy:home")
    else:
        form = InviteAcceptForm(user=None)
    return render(request, "accounts/invite_accept.html", {"form": form, "invite": invite})


# ---------------------------------------------------------------------------
# Editor хүсэлт
# ---------------------------------------------------------------------------


@login_required
def editor_request(request):
    """Хэрэглэгч өөрөө «editor болох» хүсэлт гаргана."""
    if getattr(request.user, "can_edit", False):
        messages.info(request, "Танд аль хэдийн засах эрх бий.")
        return redirect("genealogy:home")

    if request.method == "POST":
        EditRequest.objects.get_or_create(
            user=request.user, status=EditRequest.Status.PENDING,
            defaults={"note": (request.POST.get("note") or "").strip()[:2000]},
        )
        messages.success(request, "Хүсэлт илгээгдлээ. Админ шийдвэрлэхэд мэдэгдэнэ.")
        return redirect("accounts:editor_request")

    return render(request, "accounts/editor_request.html",
                  {"current": EditRequest.objects.filter(user_id=request.user.pk).first()})


@login_required
def editor_request_list(request):
    """Админ: хүсэлтүүдийг харж, шийдвэрлэнэ."""
    if not _require_admin(request):
        messages.error(request, "Зөвхөн админ хүсэлт харна.")
        return redirect("genealogy:home")

    qs = list(EditRequest.objects.select_related("user", "resolved_by"))
    return render(request, "accounts/editor_request_list.html", {
        "pending": [r for r in qs if r.is_pending],
        "decided": [r for r in qs if not r.is_pending][:50],
    })


@login_required
@require_POST
def editor_request_decide(request, pk, action):
    """Хүсэлтийг зөвшөөрөх / татгалзах. Зөвхөн админ."""
    if not _require_admin(request):
        messages.error(request, "Зөвхөн админ шийдвэрлэнэ.")
        return redirect("genealogy:home")
    if action not in {"approve", "reject"}:
        raise Http404

    with transaction.atomic():
        req = get_object_or_404(
            EditRequest.objects.select_for_update().select_related("user"), pk=pk)
        if not req.is_pending:
            messages.error(request, "Энэ хүсэлт аль хэдийн шийдэгдсэн.")
            return redirect("accounts:editor_request_list")

        req.status = (EditRequest.Status.APPROVED if action == "approve"
                      else EditRequest.Status.REJECTED)
        req.resolved_at = timezone.now()
        req.resolved_by = request.user
        req.save(update_fields=["status", "resolved_at", "resolved_by"])

        if action == "approve" and req.user.role == User.Role.VIEWER:
            # Зөвхөн VIEWER → EDITOR. ADMIN эрх, is_staff, is_superuser-ыг
            # ХЭЗЭЭ Ч хөндөхгүй — эс бөгөөс энэ нь эрх өргөжүүлэх зам болно.
            req.user.role = User.Role.EDITOR
            req.user.save(update_fields=["role"])

    messages.success(request, "Хүсэлт шийдэгдлээ.")
    return redirect("accounts:editor_request_list")


# ---------------------------------------------------------------------------
# Харагдацын горим (админ)
# ---------------------------------------------------------------------------


def _safe_next(request):
    """Зөвхөн дотоод зам руу буцаана (open redirect-ээс сэргийлнэ)."""
    target = request.POST.get("next") or ""
    if target.startswith("/") and not target.startswith("//"):
        return target
    return reverse("genealogy:home")


@require_POST
@login_required
def set_view_mode(request):
    """Админы харагдацыг солино: «Админ» ↔ «Энгийн».

    `request.user` нь «энгийн харагдац»-д бүрхүүл болдог тул жинхэнэ
    хэрэглэгчээр (`real_user`) шалгана — эс бөгөөс админ буцаж асаахгүй.
    """
    user = real_user(request)
    if not getattr(user, "is_admin", False):
        raise Http404
    if "mode" in request.POST:
        mode = request.POST.get("mode")
        if mode not in VIEW_MODES:
            raise Http404
    else:
        # Модны хуудасны «Энгийн view» чекбокс — тэмдэглээгүй бол админ харагдац.
        mode = MODE_NORMAL if request.POST.get("normal") else MODE_ADMIN
    request.session[VIEW_MODE_SESSION_KEY] = mode
    if mode == MODE_NORMAL:
        messages.info(request, "Энгийн харагдац — сайт гишүүнд хэрхэн харагдахыг үзүүлж байна.")
    return redirect(_safe_next(request))
