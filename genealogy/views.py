"""Харагдацууд.

Эрхийн зарчим: унших үед `Person.objects.visible_to(user)` (query түвшинд шүүнэ),
бичих үед `can_edit` шаардана. Медиа файлыг шууд MEDIA_URL-ээр биш, эрх
шалгадаг `serve_media` view-ээр үйлчилнэ.
"""

import uuid
from datetime import date, timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import Case, CharField, Count, IntegerField, Q, Value, When
from django.db.models.functions import Cast
from django.utils import timezone
from django.utils.http import content_disposition_header
from django.http import Http404, HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, DeleteView, DetailView, ListView, UpdateView

from accounts.emails import (
    send_edit_request_email,
    send_edit_resolved_email,
    send_invite_email,
)
from accounts.middleware import real_user
from accounts.models import Invite, User

from . import services
from .forms import (
    EventForm,
    LinkRelativeForm,
    FamilyForm,
    MediaForm,
    PersonForm,
    PlaceForm,
    QuickAddPersonForm,
    SourceForm,
)
from .models import (
    AuditLog,
    ChildLink,
    Event,
    Family,
    Media,
    MediaLink,
    Person,
    PersonEditRequest,
    PersonName,
    Place,
    Source,
)


class CanEditMixin(LoginRequiredMixin, UserPassesTestMixin):
    """Мэдээлэл оруулах/засах эрх шаардана."""

    def test_func(self):
        return bool(getattr(self.request.user, "can_edit", False))

    def handle_no_permission(self):
        if self.request.user.is_authenticated:
            messages.error(self.request, "Танд засах эрх байхгүй.")
            return redirect("genealogy:home")
        return super().handle_no_permission()


def masked_pks(people, user):
    """Нарийн мэдээлэл (нас, хүйс, газар) нь хаалттай хүмүүсийн pk.

    Template-ууд үүгээр «Хувийн» хүнийг ялгана — нэр нь харагдана, харин
    нас/хүйс/газар болон «нас барсан» саарал өнгө хаалттай.
    """
    return {p.pk for p in people if p.is_masked_for(user)}


def visible_person_or_404(request, pk):
    """Харах эрхгүй бол 404 (байгаа эсэхийг задруулахгүй)."""
    person = get_object_or_404(
        Person.objects.visible_to(request.user).select_related("birth_date", "death_date",
                                                               "birth_place", "death_place"),
        pk=pk,
    )
    return person


def editable_person_or_404(request, pk):
    """ЗАСАХ эрхгүй бол 404.

    Мөр тус бүрийн эрх: засварлагч зөвхөн өөрийн/нээлттэй/эзэнгүй хүнийг засна
    (`Person.can_edit_by`). 404-ийг сонгосон шалтгаан: оршин байгааг
    задруулахгүй, `visible_person_or_404`-той ижил хэв маяг.
    """
    return get_object_or_404(Person.objects.editable_by(request.user), pk=pk)


# ---------------------------------------------------------------------------
# Нүүр ба профайл
# ---------------------------------------------------------------------------


def home(request):
    people = Person.objects.visible_to(request.user)
    stats = {
        "total": people.count(),
        "living": sum(1 for p in people.select_related("death_date", "birth_date")[:2000] if p.is_living),
        "families": Family.objects.count(),
        "sources": Source.objects.count(),
    }
    recent = people.select_related("birth_date", "death_date").order_by("-created_at")[:12]
    recent = list(recent)
    return render(request, "genealogy/home.html",
                  {"stats": stats, "recent": recent,
                   "masked_pks": masked_pks(recent, request.user)})


# Холбооны төрлүүдийг дэлгэц дээр эрэмбэлэх (төрсөн нь үргэлж түрүүнд).
_PEDIGREE_ORDER = {"birth": 0, "adopted": 1, "foster": 2, "step": 3, "unknown": 4}

# Ах/эгч/дүүгийн шошго. «Эцэг эхн ах/эгч/дүү» гэж бичих шаардлагагүй —
# эцэг эхийн төрөл (өргөмөл, хойд, …) нь энэ шошгонд шууд орно.
_SIBLING_LABELS = {
    "birth": "Ах/эгч/дүү",
    "adopted": "Өргөмөл ах/эгч/дүү",
    "foster": "Асран хүмүүжүүлсэн ах/эгч/дүү",
    "step": "Хойд ах/эгч/дүү",
    "unknown": "Ах/эгч/дүү (тодорхойгүй)",
}

# Эцэг эх бүрийн тодруулга: холбооны төрөл × хүйс. «Эцэг эх: Дорж, Санга»
# гэвэл хэн нь эцэг, хэн нь эх нь мэдэгдэхгүй тул хаалтанд бичнэ:
# «Дорж (эцэг), Санга (эх)». Төрсөн холбооноос бусад үед угтвар нэмэгдэнэ:
# «(өргөж авсан эцэг)», «(хойд эх)».
_PARENT_ROLE_PREFIX = {
    "birth": "",
    "adopted": "өргөж авсан",
    "foster": "асран хүмүүжүүлсэн",
    "step": "хойд",
    "unknown": "",
}
_SEX_ROLE = {"M": "эцэг", "F": "эх"}

# Модны анхдагч горим — «Нэгдсэн» (өвөг ба үр хойш хоёулаа).
DEFAULT_TREE_MODE = "both"


def _uuid_or_none(value):
    """GET/POST параметрийг Person-ийн UUID болгоно; хоосон эсвэл буруу бол None.

    Буруу утгыг шууд `pk=`-д өгвөл ValidationError (500) болно — 404 байх ёстой.
    """
    try:
        return uuid.UUID(str(value)) if value else None
    except ValueError:
        return None


def _person_or_404(queryset, value):
    """Параметрээр ирсэн UUID-аар хүнийг олно; буруу/олдохгүй бол 404."""
    pk = _uuid_or_none(value)
    if pk is None:
        raise Http404
    return get_object_or_404(queryset, pk=pk)


def person_detail(request, pk):
    person = visible_person_or_404(request, pk)

    # Эцэг эх — холбооны төрлөөр нь бүлэглэнэ (төрсөн / өргөмөл / …)
    parent_groups = []
    for link in person.parent_links.select_related("family__partner1", "family__partner2"):
        people = [p for p in (link.family.partner1, link.family.partner2) if p]
        if people:
            parent_groups.append({"label": link.parent_label, "pedigree": link.pedigree,
                                  "is_birth": link.is_birth, "people": people})
    # Ах/эгч/дүү — НЭГ эцэг/эх хуваалцсан хүмүүс. Нэг хос хэд хэдэн `Family`
    # мөрөнд бүртгэгдсэн байж болзошгүй (хожим хүүхэд нэмэхэд шинэ Family
    # үүсдэг) тул зөвхөн өөрийн Family-ийн хүүхдүүдийг биш, эцэг/эх бүрийн
    # оролцсон БҮХ гэр бүлийн хүүхдүүдийг хамруулна. Ингэснээр эцэг/эхийн
    # өөр гэрлэлтээс төрсөн эцэг/эх нэгтэй ах дүү ч харагдана.
    sibling_groups = []
    seen_siblings = {person.pk}
    for link in person.parent_links.select_related("family__partner1", "family__partner2"):
        sibs = []
        for parent in link.family.partners:
            for fam in Family.objects.filter(Q(partner1=parent) | Q(partner2=parent)):
                for child_link in fam.children.select_related(
                        "child__birth_date", "child__death_date"):
                    child = child_link.child
                    if child.pk in seen_siblings:
                        continue
                    seen_siblings.add(child.pk)
                    sibs.append(child)
        if sibs:
            sibling_groups.append({"label": link.parent_label, "pedigree": link.pedigree,
                                   "sibling_label": _SIBLING_LABELS.get(
                                       link.pedigree, _SIBLING_LABELS["birth"]),
                                   "people": sibs})

    # Хосууд ба хүүхдүүд — хүүхдүүд нь төрлөөрөө бүлэглэгдэнэ
    own_families = []
    for fam in person.families:
        groups = {}
        for link in fam.children.select_related("child__birth_date", "child__death_date"):
            entry = groups.setdefault(link.pedigree, {"pedigree": link.pedigree,
                                                      "label": link.child_label, "people": []})
            entry["people"].append(link.child)
        own_families.append({
            "family": fam,
            "partner": fam.partner2 if fam.partner1_id == person.pk else fam.partner1,
            "child_groups": sorted(groups.values(),
                                   key=lambda g: _PEDIGREE_ORDER.get(g["pedigree"], 9)),
            # Гэр бүл засах эрх нь оролцогч бүрээс хамаарна — «засах» линкийг
            # зөвхөн эрхтэй үед харуулна. Нэмэлт query гарахгүй: fam болон
            # хүүхдүүд аль хэдийн ачаалагдсан.
            "can_edit": fam.can_edit_by(request.user),
        })

    parent_groups.sort(key=lambda g: _PEDIGREE_ORDER.get(g["pedigree"], 9))
    sibling_groups.sort(key=lambda g: _PEDIGREE_ORDER.get(g["pedigree"], 9))

    # -- харах эрхгүй хамаатныг МАСКАРНА ---------------------------------
    # «Хувийн» хүний нэр харагдана, харин нас/хүйс зэрэг нарийн мэдээлэл
    # хаалттай — тэр хүнийг `masked_pks`-д бөглөж, template-д «нас барсан»
    # саарал өнгө ч хэрэглэхгүй (тэр нь өөрөө мэдээлэл). «Хаалттай» хүнийг
    # `•••` placeholder болгоно — нэр ч харагдахгүй.
    #
    # Нэг л query: `visible_to` нь `can_view`-тай ижил дүрэм (устгагдсан/
    # нэгтгэгдсэн хүн ч зөв шүүгдэнэ).
    relatives = [p for group in parent_groups for p in group["people"]]
    relatives += [p for group in sibling_groups for p in group["people"]]
    for entry in own_families:
        if entry["partner"]:
            relatives.append(entry["partner"])
        for group in entry["child_groups"]:
            relatives += group["people"]

    visible_pks = set(
        Person.objects.visible_to(request.user)
        .filter(pk__in=[p.pk for p in relatives])
        .values_list("pk", flat=True)
    )
    masked_pks = {p.pk for p in relatives if p.is_masked_for(request.user)}

    # Ах/эгч/дүү ба хүүхдүүдийг ӨНДӨР НАСТАЙГААС нь (төрсөн оноор өсөхөд)
    # эрэмбэлнэ. Огноо нь тодорхойгүй (`FlexDate.sort_key == date.min`) болон
    # нарийн мэдээлэл нь хаалттай хүмүүс төгсгөлдөө — «хаалттай хүний насыг
    # эрэмбээр ч задруулахгүй» зарчим (`services.build_graph._birth_year`-тай
    # ижил). `_place`-ээс ӨМНӨ ажиллана: тэр нь жинхэнэ объектыг placeholder
    # болгодог тул `birth_date`/`pk` аль хэдийн ачаалагдсан байх ёстой.
    def _age_key(p):
        if p.pk in masked_pks or p.birth_date is None:
            return (1, date.max, p.display_name)
        sort_key = p.birth_date.sort_key
        if sort_key in (None, date.min):
            return (1, date.max, p.display_name)
        return (0, sort_key, p.display_name)

    for group in sibling_groups:
        group["people"].sort(key=_age_key)
    for entry in own_families:
        for group in entry["child_groups"]:
            group["people"].sort(key=_age_key)

    def _place(p):
        """Харах эрхгүй бол placeholder, эс бөгөөс хүн өөрөө.

        Шалтгааныг нь хамт өгнө: «Хаалттай» бол «(хаалттай)» гэж бичнэ,
        нэгтгэгдсэн/устсан бол зөвхөн `•••` (`p` аль хэдийн ачаалагдсан тул
        нэмэлт query гарахгүй).
        """
        if p.pk in visible_pks:
            return p
        return services.HiddenPerson(closed=p.visibility == Person.Visibility.CLOSED)

    for group in parent_groups + sibling_groups:
        group["people"] = [_place(p) for p in group["people"]]
    for entry in own_families:
        entry["partner"] = _place(entry["partner"]) if entry["partner"] else None
        for group in entry["child_groups"]:
            group["people"] = [_place(p) for p in group["people"]]

    # -- эцэг эх бүрд тодруулга ------------------------------------------
    # «Эцэг эх: Дорж, Санга» гэвэл хэн нь эцэг, хэн нь эх нь мэдэгдэхгүй.
    # Хүйс тодорхойгүйг хамтрагчаас нь таана (`effective_sex_map`).
    # «Хувийн» хүний хүйс хаалттай тул тодруулга өгөхгүй — `_person_link`
    # түүнд «(хувийн)» гэж харуулах ба «Хаалттай» нь «••• (хаалттай)».
    known = [p for group in parent_groups for p in group["people"]
             if not p.is_hidden and p.pk not in masked_pks]
    parent_sexes = services.effective_sex_map({p.pk: p for p in known})
    for group in parent_groups:
        prefix = _PARENT_ROLE_PREFIX.get(group["pedigree"], "")
        for p in group["people"]:
            if p.is_hidden or p.pk in masked_pks:
                p.role_tag = ""
                continue
            role = _SEX_ROLE.get(parent_sexes.get(p.pk, p.sex), "эцэг эх")
            p.role_tag = f"{prefix} {role}".strip()
    # Энэ хүнийг засах эрх. Template нь аргументтай метод дуудаж чадахгүй тул
    # энд бодно. `missing_links` ба `audit` ч үүнээс хамаарна — эрхгүй хүнд
    # засварын урилга харуулахгүй.
    can_edit_person = person.can_edit_by(request.user)

    # «Хувийн» хүн: нэр нь харагдана, харин нас/хүйс/газар/намтар/үйл явдал
    # бүгд хаалттай. Холбооны бүтэц (хэн хэнийх) нь модон дээр ч харагддаг
    # тул үлдэнэ — харин нэрийн холбоос нь `•••` болно (`masked_pks`).
    person_masked = person.is_masked_for(request.user)

    context = {
        "person": person,
        "can_edit_person": can_edit_person,
        "person_masked": person_masked,
        "masked_pks": masked_pks,
        "parent_groups": parent_groups,
        "sibling_groups": sibling_groups,
        "own_families": own_families,
        "events": person.event_participations.select_related("event__date", "event__place").all(),
        "citations": person.citations.select_related("source").all(),
        "media": person.media_links.select_related("media").all(),
        "names": person.names.all(),
        "rel_choices": [
            ("father", "Эцэг"), ("mother", "Эх"), ("partner", "Эхнэр/нөхөр"),
            ("child", "Хүүхэд"), ("sibling", "Ах/эгч/дүү"),
        ],
        # Дутуу эцэг/эх + кандидатууд. Зөвхөн засварлагчид — харагчид query
        # огт ажиллахгүй (`suggest_parents` нь бүх амьд хүнийг уншдаг).
        # Зөвхөн хамаатан холбох эрхтэй хүнд — холбогдсон үзэгч санал харна уу
        # гэвэл бүх амьд хүнийг уншдаг query дэмий ажиллана.
        "missing_links": (
            services.suggest_parents(person, request.user)
            if can_edit_person and getattr(request.user, "can_edit", False) else []
        ),
        "audit": AuditLog.objects.filter(object_id=person.pk).select_related("user")[:15]
        if getattr(request.user, "can_edit", False) else [],
        **_pending_edit_context(request, person, can_edit_person),
    }
    return render(request, "genealogy/person_detail.html", context)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


class PersonCreateView(CanEditMixin, CreateView):
    """Модноос ХОЛБООГҮЙ шинэ хүн — зөвхөн админ.

    Засварлагч хүнийг зөвхөн хамаатан нэмэх (quick-add) замаар, модонд
    холбогдсон байдлаар нэмнэ — эс бөгөөс өнчин бичлэг хуримтлагдана.
    """

    model = Person
    form_class = PersonForm
    template_name = "genealogy/person_form.html"

    def test_func(self):
        return bool(getattr(self.request.user, "is_admin", False))

    def handle_no_permission(self):
        if self.request.user.is_authenticated:
            messages.error(self.request, "Шинэ хүнийг зөвхөн админ нэмнэ — "
                                         "хамаатныг модноос «+»-ээр нэмнэ үү.")
            return redirect("genealogy:home")
        return super().handle_no_permission()

    def form_valid(self, form):
        form.instance.created_by = self.request.user
        response = super().form_valid(form)
        services.log_action(self.request.user, AuditLog.Action.CREATE, self.object,
                            ip_address=services.client_ip(self.request))
        messages.success(self.request, f"{self.object.display_name} нэмэгдлээ.")
        return response

    def get_success_url(self):
        return reverse("genealogy:person_detail", args=[self.object.pk])


class PersonEditMixin(CanEditMixin):
    """`PersonUpdateView`-д зориулсан сулруулсан хаалт.

    Мөр тус бүрийн эрх нь `can_edit`-ээс давамгайлна: холбогдсон хэрэглэгч
    (үзэгч эрхтэй ч) ЗӨВХӨН өөрийн бичлэгээ засна. `CanEditMixin`-ийг
    бүхэлд нь сулруулахгүй — устгах, гэр бүл засах нь хатуу хэвээр.
    """

    def test_func(self):
        if bool(getattr(self.request.user, "can_edit", False)):
            return True
        return bool(getattr(self.request.user, "person", None) is not None)


class PersonUpdateView(PersonEditMixin, UpdateView):
    model = Person
    form_class = PersonForm
    template_name = "genealogy/person_form.html"

    def get_queryset(self):
        # Мөр тус бүрийн эрх: засварлагч зөвхөн өөрийн/нээлттэй/эзэнгүй хүнийг
        # засна. Эрхгүй бол 404 — оршин байгааг алдагдуулахгүй.
        return Person.objects.alive().editable_by(self.request.user)

    def get_context_data(self, **kwargs):
        """Бүртгэлийн хэсэгт хэрэгтэй өгөгдөл (`_person_account_panel.html`)."""
        context = super().get_context_data(**kwargs)
        person = self.object
        linked = person.linked_user
        pending = _pending_invite(person)
        # ГЛОБАЛ `can_edit` ЗААВАЛ — `can_edit_by` нь холбогдсон үзэгчийг
        # зөвшөөрдөг тул зөвхөн түүнээр шалгавал тэр өөрөө холбоосоо солино.
        can_manage = (bool(getattr(self.request.user, "can_edit", False))
                      and person.can_edit_by(self.request.user))
        is_admin = bool(getattr(self.request.user, "is_admin", False))

        linkable_users = []
        if is_admin and can_manage and linked is None and pending is None:
            taken = (Person.objects.exclude(linked_user=None)
                     .values_list("linked_user_id", flat=True))
            linkable_users = (get_user_model().objects.exclude(pk__in=taken)
                              .order_by("email"))

        context.update({
            "person": person,
            "account_user": linked,
            "pending_invite": pending,
            "can_manage_account": can_manage,
            "can_link_existing": is_admin,
            "linkable_users": linkable_users,
            "pending_edit": _pending_edit(person),
        })
        return context

    def form_valid(self, form):
        """Засварыг хадгална. Эрхгүй (үзэгч) хүний засвар батлуулахаар очно.

        Засвар нь `Person` мөрөнд ШУУД бичигдэнэ — гэхдээ «батлагдаагүй»
        төлөвтэй; татгалзвал `old_values`-ээс сэргээнэ.
        """
        user = self.request.user
        # Editor/admin өөрийн бичлэгээ шууд засна — батламж шаардахгүй.
        privileged = bool(getattr(user, "can_edit", False))
        # «Энгийн харагдац»-д `self.request.user` нь `User` биш бүрхүүл тул
        # FK бичихдээ жинхэнэ хэрэглэгчийг авна.
        actor = real_user(self.request)

        # АНХААР: `form.is_valid()` дотор `construct_instance` нь `self.object`-ыг
        # аль хэдийн шинэ утгаар дарсан байдаг тул хуучин утгыг ЗӨВХӨН сангаас
        # шинэ query-ээр авна.
        old_values = None
        if not privileged:
            old = Person.objects.select_related(
                "birth_date", "death_date", "birth_place", "death_place",
            ).get(pk=self.object.pk)
            old_values = services.person_snapshot(old)

        # Нээлттэй байсан бичлэг нь засварлагчийн нэр дээр шилжинэ.
        # `user`-ыг (бүрхүүлчлээгүй!) дамжуулна: «энгийн харагдац»-ын админ
        # нь үзэгч мэтээр батлуулж байгаа тул эзэмших ёсгүй.
        services.claim_open_person(self.object, user)

        response = super().form_valid(form)

        # Хуучирсан хүсэлт editor-ийн шинэ утгатай зөрчилдөхгүйн тулд
        # шууд засвар хийгдмэгц хүлээгдэж буй хүсэлтийг хаана.
        if privileged:
            _cancel_pending_edit(self.object, actor, "Шууд засвар хийгдсэн")

        services.log_action(actor, AuditLog.Action.UPDATE, self.object,
                            {"fields": list(form.changed_data)},
                            ip_address=services.client_ip(self.request))

        if privileged:
            messages.success(self.request, "Өөрчлөлт хадгалагдлаа.")
            return response

        new_values = services.person_snapshot(self.object)
        changed = services.changed_field_names(old_values, new_values)
        if not changed:
            messages.info(self.request, "Өөрчлөлт оруулаагүй.")
            return response

        req, _ = services.record_person_edit_request(
            self.object, actor, old_values, new_values, changed)

        diff_text = services.render_diff_text(req)
        link = f"{settings.SITE_URL}{reverse('genealogy:person_edit_request_list')}"
        for approver in services.person_approvers(self.object, exclude_user=user):
            send_edit_request_email(approver.email, self.object.named_display,
                                    actor.email, diff_text, link)

        messages.success(self.request,
                         "Засвар хадгалагдлаа. Админ эсвэл editor батлах хүртэл "
                         "«батлагдаагүй» төлөвтэй байна.")
        return response

    def get_success_url(self):
        return reverse("genealogy:person_detail", args=[self.object.pk])


class PersonDeleteView(CanEditMixin, DeleteView):
    model = Person
    template_name = "genealogy/confirm_delete.html"
    success_url = "/"

    def get_queryset(self):
        return Person.objects.alive().editable_by(self.request.user)

    def form_valid(self, form):
        person = self.get_object()
        services.log_action(self.request.user, AuditLog.Action.DELETE, person,
                            {"name": person.display_name}, ip_address=services.client_ip(self.request))
        messages.warning(self.request, f"{person.display_name} устгагдлаа.")
        return super().form_valid(form)


class FamilyUpdateView(CanEditMixin, UpdateView):
    model = Family
    form_class = FamilyForm
    template_name = "genealogy/family_form.html"

    def get_object(self, queryset=None):
        # Гэр бүл хэд хэдэн хүнийг холбодог тул оролцогч БҮР (partner ба хүүхэд)
        # засах эрхтэй байх ёстой — эс бөгөөс бусдын хүнийг салгаж/сольж болно.
        family = super().get_object(queryset)
        if not family.can_edit_by(self.request.user):
            raise Http404
        return family

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), "user": self.request.user}

    def form_valid(self, form):
        response = super().form_valid(form)
        # Гэр бүл засах нь оролцогч БҮРИЙН (partner ба хүүхэд) холбоог хөнддөг
        # тул нээлттэй байсан оролцогчид эзэмшигдэнэ.
        services.claim_open_members(self.object, self.request.user)
        services.log_action(self.request.user, AuditLog.Action.UPDATE, self.object, {},
                            ip_address=services.client_ip(self.request))
        messages.success(self.request, "Гэр бүлийн мэдээлэл хадгалагдлаа.")
        return response

    def get_success_url(self):
        person = self.object.partner1 or self.object.partner2
        if person:
            return reverse("genealogy:person_detail", args=[person.pk])
        return reverse("genealogy:home")


class EventCreateView(CanEditMixin, CreateView):
    model = Event
    form_class = EventForm
    template_name = "genealogy/event_form.html"

    def get_initial(self):
        initial = super().get_initial()
        person_id = self.request.GET.get("person")
        if person_id:
            initial["event_type"] = Event.EventType.OCCUPATION
        return initial

    def form_valid(self, form):
        response = super().form_valid(form)
        person_id = _uuid_or_none(self.request.POST.get("person") or self.request.GET.get("person"))
        if person_id:
            from .models import EventParticipant

            EventParticipant.objects.get_or_create(
                event=self.object, person_id=person_id, role=EventParticipant.Role.PRINCIPAL
            )
        services.log_action(self.request.user, AuditLog.Action.CREATE, self.object, {},
                            ip_address=services.client_ip(self.request))
        messages.success(self.request, "Үйл явдал нэмэгдлээ.")
        return response

    def get_success_url(self):
        person_id = _uuid_or_none(self.request.POST.get("person") or self.request.GET.get("person"))
        if person_id:
            return reverse("genealogy:person_detail", args=[person_id])
        return reverse("genealogy:home")


class SourceCreateView(CanEditMixin, CreateView):
    model = Source
    form_class = SourceForm
    template_name = "genealogy/source_form.html"
    success_url = "/sources/"


class SourceListView(LoginRequiredMixin, ListView):
    model = Source
    template_name = "genealogy/source_list.html"
    context_object_name = "sources"
    paginate_by = 50


# ---------------------------------------------------------------------------
# Хурдан нэмэх (HTMX)
# ---------------------------------------------------------------------------


@login_required
def quick_add(request, pk, relation):
    """Модны зангилаанаас хамаатан нэмэх. GET → modal, POST → шинэ зангилаа."""
    if not getattr(request.user, "can_edit", False):
        return HttpResponse("Эрх байхгүй", status=403)
    # Хамаатан нэмэх нь anchor-ын гэр бүлийг өөрчилдөг тул түүнийг засах эрхтэй
    # байх ёстой.
    anchor = get_object_or_404(Person.objects.alive().editable_by(request.user), pk=pk)

    if request.method == "POST":
        form = QuickAddPersonForm(request.POST, anchor=anchor)
        if form.is_valid():
            data = form.cleaned_data
            try:
                person = services.create_relative(
                    anchor, data["relation"],
                    given_name=data["given_name"], patronymic=data.get("patronymic", ""),
                    clan_name=data.get("clan_name", ""),
                    sex=data.get("sex") or Person.Sex.UNKNOWN,
                    birth_date=data.get("birth_date"), user=request.user,
                    pedigree=data.get("pedigree") or "birth",
                )
            except ValueError as exc:
                form.add_error(None, str(exc))
            else:
                # Хамаатан нэмэх нь anchor-ыг хөндсөн засвар тул нээлттэй
                # байсан бол эзэмшигдэнэ.
                services.claim_open_person(anchor, request.user)
                response = render(request, "genealogy/partials/_quick_add_success.html",
                                  {"person": person, "anchor": anchor})
                # D3 тал нь энэ event-ээр модоо шинэчилнэ.
                response["HX-Trigger"] = '{"tree:refresh": {"root": %d}}' % anchor.pk
                return response
    else:
        form = QuickAddPersonForm(initial={"relation": relation}, anchor=anchor)

    return render(request, "genealogy/partials/_quick_add_modal.html",
                  {"form": form, "anchor": anchor, "relation": relation})


# ---------------------------------------------------------------------------
# Мод
# ---------------------------------------------------------------------------


def tree_view(request, pk=None):
    people = Person.objects.visible_to(request.user)
    if pk:
        root = get_object_or_404(people, pk=pk)
    else:
        # pk заагаагүй бол («Мод харах», navbar-ын «Мод») нэвтэрсэн
        # хэрэглэгчтэй холбогдсон хүнийг төвд авчирна — өөрийн мод хамгийн
        # ойлгомжтой. Холбоос байхгүй бол хамгийн эрт бүртгэгдсэн хүн.
        root = (people.filter(linked_user_id=getattr(request.user, "pk", None)).first()
                or people.order_by("created_at", "pk").first())
    if root is None:
        messages.info(request, "Эхлээд хүн бүртгэнэ үү.")
        return redirect("genealogy:home")
    mode = request.GET.get("mode", DEFAULT_TREE_MODE)
    depth = min(int(request.GET.get("depth", 4) or 4), 10)
    bloodline = request.GET.get("bloodline") == "1"
    return render(request, "genealogy/tree.html",
                  {"root": root, "mode": mode, "depth": depth, "bloodline": bloodline,
                   # «Хувийн» хүн: модны толгойд нэр л харагдана.
                   "root_masked": root.is_masked_for(request.user),
                   # Гол хүнийг засах эрх (мөр тус бүрийн дүрмээр) — линк харуулах эсэх
                   "can_edit_root": root.can_edit_by(request.user)})


def tree_data(request, pk):
    person = visible_person_or_404(request, pk)
    mode = request.GET.get("mode", DEFAULT_TREE_MODE)
    try:
        depth = min(int(request.GET.get("depth", 4)), 10)
    except (TypeError, ValueError):
        depth = 4
    data = services.build_graph(
        person, mode=mode, max_depth=depth, user=request.user,
        bloodline_only=request.GET.get("bloodline") == "1",
    )
    return JsonResponse(data)


# ---------------------------------------------------------------------------
# Хайлт
# ---------------------------------------------------------------------------


def search(request):
    query = (request.GET.get("q") or "").strip()
    results = list(Person.objects.visible_to(request.user).search(query)[:40]) if query else []
    template = ("genealogy/partials/_search_results.html"
                if request.headers.get("HX-Request") else "genealogy/search.html")
    return render(request, template, {"query": query, "results": results,
                                      "masked_pks": masked_pks(results, request.user)})


def person_lookup(request):
    """autocomplete — нэрээр хайж id/нэр буцаана."""
    query = (request.GET.get("q") or "").strip()
    people = list(Person.objects.visible_to(request.user).search(query)[:20]) if query else []
    hidden = masked_pks(people, request.user)
    return JsonResponse({
        # «Хувийн» хүний он жилийг autocomplete-аас ч задруулахгүй.
        "results": [{"id": p.pk,
                     "text": (p.display_name if p.pk in hidden
                              else f"{p.display_name} {p.lifespan}".strip())}
                    for p in people]
    })


# ---------------------------------------------------------------------------
# Медиа
# ---------------------------------------------------------------------------


def _make_thumbnail(media):
    """Pillow-оор 400px thumbnail үүсгэнэ. Алдаа гарвал чимээгүй өнгөрнө."""
    from io import BytesIO

    from django.core.files.base import ContentFile
    from PIL import Image, ImageOps

    try:
        media.file.open("rb")
        img = Image.open(media.file)
        img = ImageOps.exif_transpose(img)  # утасны зургийн эргэлтийг засна
        media.width, media.height = img.size
        img.thumbnail((400, 400))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        buffer = BytesIO()
        img.save(buffer, format="JPEG", quality=85)
        name = f"{media.pk or 'new'}.jpg"
        media.thumbnail.save(name, ContentFile(buffer.getvalue()), save=False)
    except Exception:
        pass
    finally:
        try:
            media.file.close()
        except Exception:
            pass


class MediaUploadView(CanEditMixin, CreateView):
    model = Media
    form_class = MediaForm
    template_name = "genealogy/media_form.html"

    def form_valid(self, form):
        form.instance.uploaded_by = self.request.user
        response = super().form_valid(form)
        _make_thumbnail(self.object)
        self.object.save()
        person_id = _uuid_or_none(self.request.POST.get("person"))
        if person_id:
            MediaLink.objects.get_or_create(
                media=self.object,
                content_type=self._person_ctype(),
                object_id=str(person_id),
                defaults={"is_primary": not MediaLink.objects.filter(
                    content_type=self._person_ctype(), object_id=str(person_id)
                ).exists()},
            )
        messages.success(self.request, "Файл хадгалагдлаа.")
        return response

    @staticmethod
    def _person_ctype():
        from django.contrib.contenttypes.models import ContentType

        return ContentType.objects.get_for_model(Person)

    def get_success_url(self):
        person_id = _uuid_or_none(self.request.POST.get("person"))
        if person_id:
            return reverse("genealogy:person_detail", args=[person_id])
        return reverse("genealogy:home")


def serve_media(request, pk):
    """Медиа файлыг эрх шалгаж үйлчилнэ.

    Нээлттэй MEDIA_URL-ээр үйлчилбэл амьд хүний зураг нууцлалыг тойрч
    харагдана. Тиймээс: зөвхөн нэвтэрсэн, холбогдсон объектуудын ядаж нэгийг
    харах эрхтэй хэрэглэгчид.
    """
    media = get_object_or_404(Media, pk=pk)
    if not request.user.is_authenticated:
        raise Http404
    if not getattr(request.user, "can_edit", False):
        allowed = False
        for link in media.links.all():
            target = link.content_object
            if isinstance(target, Person) and target.can_see_details(request.user):
                allowed = True
                break
            if isinstance(target, (Event, Family, Source)):
                allowed = True
                break
        if not allowed:
            raise Http404

    if not media.file:
        raise Http404

    # S3: файлыг Django-гоор дамжуулахгүй, эрх шалгасны дараа богино хугацаатай
    # presigned URL руу шилжүүлнэ (бакет хувийн тул шууд холбоос ажиллахгүй).
    if settings.USE_S3_MEDIA:
        if request.GET.get("thumb") and media.thumbnail:
            url = media.thumbnail.storage.url(
                media.thumbnail.name, parameters={"ResponseContentType": "image/jpeg"}
            )
        else:
            filename = media.file.name.rsplit("/", 1)[-1]
            url = media.file.storage.url(
                media.file.name,
                parameters={
                    "ResponseContentType": media.mime_type or "application/octet-stream",
                    # Кирилл нэртэй файлд RFC 5987 (filename*=UTF-8'') хэлбэр хэрэгтэй.
                    "ResponseContentDisposition": content_disposition_header(False, filename),
                },
            )
        response = HttpResponseRedirect(url)
        response["Cache-Control"] = "private, no-store"
        return response

    # ?thumb=1 — жагсаалт/карт дээр хөнгөн хувилбарыг үзүүлнэ.
    if request.GET.get("thumb") and media.thumbnail:
        response = HttpResponse(content_type="image/jpeg")
        with media.thumbnail.open("rb") as fh:
            response.write(fh.read())
        return response

    response = HttpResponse(content_type=media.mime_type or "application/octet-stream")
    filename = media.file.name.rsplit("/", 1)[-1]
    response["Content-Disposition"] = f'inline; filename="{filename}"'
    with media.file.open("rb") as fh:
        response.write(fh.read())
    return response


# ---------------------------------------------------------------------------
# Байгаа хүмүүсийг холбох
# ---------------------------------------------------------------------------


def _blocked_for_relation(anchor, relation):
    """Тухайн хамаатан төрөлд сонгох боломжгүй хүмүүсийн id.

    Цикл нь зөвхөн нэг чиглэлд үүсдэг тул шүүлт нь төрөл бүрд өөр байна:

      * эцэг/эх болгох  → зөвхөн ҮР ХОЙЧИС хориглоно (хүн өөрийн үр хойшсын
        эцэг эх болж болохгүй);
      * хүүхэд/ах дүү    → зөвхөн ӨВӨГ хориглоно;
      * хос болгох       → шууд өвөг ба үр хойч хоёулаа (үеэл гэрлэх нь хэвийн).

    Үйлчилгээний (`services._link`) шалгалттай яг ижил байх ёстой — эс бөгөөс
    сонгож болох ч алдаа өгдөг сонголт гарч ирнэ.
    """
    up = set(services.ancestors(anchor, max_depth=services.CYCLE_SEARCH_LIMIT))
    down = set(services.descendants(anchor, max_depth=services.CYCLE_SEARCH_LIMIT))
    if relation in {"father", "mother", "parent"}:
        return down
    if relation == "partner":
        return up | down
    return up


@login_required
def person_picker(request):
    """Хайлтын үр дүн — радио товчнууд (форм дотор, JS шаардахгүй)."""
    # Зөвхөн хамаатан холбох эрхтэй хүн. (Холбогдсон үзэгч `editable_by`-д
    # багтдаг ч хамаатан холбох эрхгүй — тэр нь `can_edit` шаардана.)
    if not getattr(request.user, "can_edit", False):
        return HttpResponse("Эрх байхгүй", status=403)
    anchor = _person_or_404(Person.objects.editable_by(request.user),
                            request.GET.get("anchor"))
    relation = request.GET.get("relation", "child")
    query = (request.GET.get("q") or "").strip()
    selected = _uuid_or_none(request.GET.get("selected"))

    # Зөвхөн засах эрхтэй хүмүүсийг санал болгоно — эс бөгөөс засварлагч
    # бусдын хүнийг удмын модонд холбож чадна.
    qs = (
        Person.objects.editable_by(request.user)
        .exclude(pk=anchor.pk)
        .exclude(pk__in=_blocked_for_relation(anchor, relation))
    )
    # Хүүхэд/ах дүү болгох бол тухайн хүн өөр төрсөн гэр бүлгүй байх ёстой
    # (DB constraint: нэг хүүхэд зөвхөн НЭГ төрсөн гэр бүлтэй).
    if relation in {"child", "son", "daughter", "sibling", "brother", "sister"}:
        qs = qs.exclude(parent_links__pedigree=ChildLink.Pedigree.BIRTH)
    if query:
        qs = qs.search(query)

    order = ["clan_name", "patronymic", "given_name"]
    if selected is not None:
        # Урьдчилан сонгосон хүн эхний 25-д ЗААВАЛ багтах ёстой — эс бөгөөс
        # түүний radio огт зурагдахгүй.
        qs = qs.annotate(_preselected=Case(
            When(pk=selected, then=Value(0)), default=Value(1),
            output_field=IntegerField()))
        order.insert(0, "_preselected")

    return render(request, "genealogy/partials/_person_picker.html",
                  {"people": qs.order_by(*order)[:25], "query": query,
                   "relation": relation, "selected": selected})


@login_required
def link_relative(request, pk, relation):
    """Байгаа хүнийг `pk`-тай холбох. GET → modal, POST → холбоно."""
    if not getattr(request.user, "can_edit", False):
        return HttpResponse("Эрх байхгүй", status=403)
    anchor = get_object_or_404(Person.objects.alive().editable_by(request.user), pk=pk)

    if request.method == "POST":
        form = LinkRelativeForm(request.POST, user=request.user)
        if form.is_valid():
            data = form.cleaned_data
            try:
                linked = services.link_existing(
                    anchor, data["relation"], data["person"], user=request.user,
                    pedigree=data.get("pedigree") or "birth",
                )
            except ValueError as exc:
                form.add_error(None, str(exc))
            else:
                # Холбох нь anchor-ыг хөндсөн засвар тул нээлттэй байсан бол
                # эзэмшигдэнэ.
                services.claim_open_person(anchor, request.user)
                response = render(request, "genealogy/partials/_quick_add_success.html",
                                  {"person": linked, "anchor": anchor, "linked": True})
                response["HX-Trigger"] = '{"tree:refresh": {"root": %d}}' % anchor.pk
                return response
    else:
        form = LinkRelativeForm(initial={"relation": relation}, user=request.user)

    # `?person=<pk>` — «Дутуу холбоос»-ын санал болгосон хүнийг урьдчилан сонгоно.
    return render(request, "genealogy/partials/_link_modal.html",
                  {"form": form, "anchor": anchor, "relation": relation,
                   "selected": _uuid_or_none(request.GET.get("person"))})



# ---------------------------------------------------------------------------
# Хүн ↔ бүртгэл (хэрэглэгч) холбоос
# ---------------------------------------------------------------------------


def _manageable_person_or_404(request, pk):
    """Бүртгэлийн холбоосыг удирдах эрхтэй хүн.

    ГЛОБАЛ `can_edit` ЗААВАЛ шаардана: `can_edit_by` нь холбогдсон үзэгчийг
    (зөвхөн өөрийн бичлэгт) зөвшөөрдөг тул глобал хаалтгүй бол тэр хүн өөрийн
    холбоосыг өөр хүн рүү шилжүүлж, эрхээ бусдад өгч чадна.
    """
    if not getattr(request.user, "can_edit", False):
        raise Http404
    return get_object_or_404(Person.objects.alive().editable_by(request.user), pk=pk)


def _pending_invite(person):
    """Хүчингүй болоогүй, хүлээгдэж буй урилга."""
    return person.invites.filter(
        accepted_at__isnull=True, expires_at__gt=timezone.now()
    ).order_by("-created_at").first()


def _pending_edit(person):
    """Хүлээгдэж буй өөрийн засварын хүсэлт (нэг л байна — DB хязгаарлана)."""
    return person.edit_requests.filter(
        status=PersonEditRequest.Status.PENDING
    ).select_related("submitted_by").first()


def _pending_edit_context(request, person, can_edit_person):
    """«Батлагдаагүй» тэмдэг ба диффийг ХЭНД харуулахыг шийднэ.

    Зөвхөн батлах эрхтэй хүн (глобал `can_edit` ба `can_edit_by`) болон
    хүсэлт гаргасан эзэнд — бусад нь шинэ утгыг тэмдэггүйгээр харна.
    """
    req = _pending_edit(person)
    if req is None:
        return {"pending_edit": None, "show_pending_badge": False, "pending_diff": []}
    user = request.user
    is_submitter = (getattr(user, "pk", None) is not None
                    and req.submitted_by_id == user.pk)
    can_approve = (bool(getattr(user, "can_edit", False))
                   and can_edit_person and not is_submitter)
    if not (is_submitter or can_approve):
        return {"pending_edit": None, "show_pending_badge": False, "pending_diff": []}
    return {
        "pending_edit": req,
        "show_pending_badge": True,
        "is_submitter": is_submitter,
        "pending_diff": services.describe_person_diff(req),
    }


def _cancel_pending_edit(person, user, note, restore=False):
    """Хүлээгдэж буй хүсэлтийг татгалзсан болгоно. Байхгүй бол чимээгүй.

    `restore=False` (анхдагч) үед утгуудыг сэргээхгүй — дуудагч (жишээ нь
    editor-ийн шууд засвар) нь илүү шинэ утгыг аль хэдийн бичсэн байдаг.
    """
    req = _pending_edit(person)
    if req is None:
        return None
    if restore:
        services.restore_person_snapshot(person, req.old_values, req.changed_fields)
    req.status = PersonEditRequest.Status.REJECTED
    req.note = note
    req.resolved_at = timezone.now()
    req.resolved_by = user
    req.save(update_fields=["status", "note", "resolved_at", "resolved_by"])
    return req


@login_required
@require_POST
def person_account_invite(request, pk):
    """Холбоогүй хүнд зориулж имэйлээр урилга илгээнэ (хүлээн авахад холбогдоно)."""
    person = _manageable_person_or_404(request, pk)
    back = redirect("genealogy:person_edit", pk=person.pk)

    if person.is_deceased:
        messages.error(request, "Нас барсан хүнд бүртгэл холбохгүй.")
        return back
    if person.linked_user_id is not None:
        messages.error(request, "Энэ хүн аль хэдийн хэрэглэгчтэй холбогдсон.")
        return back
    pending = _pending_invite(person)
    if pending is not None:
        messages.error(request, f"«{pending.email}» руу урилга аль хэдийн илгээсэн.")
        return back

    email = (request.POST.get("email") or "").strip().lower()
    if "@" not in email:
        messages.error(request, "Зөв имэйл хаяг оруулна уу.")
        return back
    if get_user_model().objects.filter(email=email).exists():
        messages.error(request, "Энэ имэйлээр хэрэглэгч бүртгэлтэй байна — "
                                "«Байгаа хэрэглэгчийг холбох»-ыг ашиглана уу.")
        return back
    # Эрх нь ЗӨВХӨН «энгийн хүн» (VIEWER) эсвэл «editor». Админ эрх урилгаар
    # олгогдохгүй — эс бөгөөс энэ нь эрх өргөжүүлэх зам болно.
    role = request.POST.get("role") or User.Role.VIEWER
    if role not in (User.Role.VIEWER, User.Role.EDITOR):
        messages.error(request, "Эрх буруу байна.")
        return back

    # Editor эрхийг зөвхөн админ ШУУД олгоно. Editor урилга илгээвэл урилга
    # нь эхэндээ зөвхөн viewer эрх өгч, editor эрх нь админы батламж хүлээнэ
    # (`Invite.needs_editor_approval`) — эс бөгөөс editor өөрөө editor
    # үүсгэх, ө.х. эрхээ өргөжүүлэх зам нээгдэнэ.
    auto_approved = role == User.Role.EDITOR and bool(
        getattr(request.user, "is_admin", False))
    invite = Invite.objects.create(
        email=email, role=role, person=person, invited_by=request.user,
        expires_at=timezone.now() + timedelta(days=settings.INVITE_EXPIRY_DAYS),
        approved_at=timezone.now() if auto_approved else None,
        approved_by=request.user if auto_approved else None,
    )
    link = request.build_absolute_uri(
        reverse("accounts:invite_accept", args=[invite.token]))
    send_invite_email(invite, link)
    services.log_action(request.user, AuditLog.Action.UPDATE, person,
                        {"account_invite": email},
                        ip_address=services.client_ip(request))
    if invite.needs_editor_approval:
        messages.success(request, f"Урилга илгээгдлээ: {link} — "
                                  "Editor эрх нь админы батламж хүлээнэ.")
    else:
        messages.success(request, f"Урилга илгээгдлээ: {link}")
    return back


@login_required
@require_POST
def person_account_link(request, pk):
    """БАЙГАА хэрэглэгчийг холбоно. Зөвхөн админ — энэ нь эрх олгож байна."""
    person = _manageable_person_or_404(request, pk)
    back = redirect("genealogy:person_edit", pk=person.pk)

    if not getattr(request.user, "is_admin", False):
        messages.error(request, "Байгаа хэрэглэгчийг зөвхөн админ холбоно.")
        return back
    if person.is_deceased:
        messages.error(request, "Нас барсан хүнд бүртгэл холбохгүй.")
        return back
    if person.linked_user_id is not None:
        messages.error(request, "Энэ хүн аль хэдийн хэрэглэгчтэй холбогдсон.")
        return back

    target = get_user_model().objects.filter(pk=request.POST.get("user") or 0).first()
    if target is None:
        messages.error(request, "Хэрэглэгч сонгоогүй байна.")
        return back
    if getattr(target, "person", None) is not None:
        messages.error(request, f"«{target.email}» өөр хүнтэй холбогдсон байна.")
        return back
    # Нөхцөлт бичилт — зэрэгцээ хүсэлтээс сэргийлнэ (1:1).
    updated = Person.objects.filter(pk=person.pk, linked_user__isnull=True) \
                            .update(linked_user=target)
    if updated:
        services.log_action(request.user, AuditLog.Action.UPDATE, person,
                            {"account_link": target.email},
                            ip_address=services.client_ip(request))
        messages.success(request, f"«{target.email}» холбогдлоо.")
    else:
        messages.error(request, "Холбож чадсангүй — хүн зуур холбогдсон байна.")
    return back


@login_required
@require_POST
def person_account_unlink(request, pk):
    """Холбоосыг салгана. Нас барсан ч зөвшөөрнө — эрх БАГАСГАЖ байна."""
    person = _manageable_person_or_404(request, pk)
    if person.linked_user_id is not None:
        email = person.linked_user.email
        # Салгагдсан хүний батлагдаагүй засвар хүчин төгөлдөр болох ёсгүй —
        # хуучин утгуудыг сэргээж, хүсэлтийг хаана (эс бөгөөс батламжийн
        # хаалт тойрч, шалгагдаагүй мэдээлэл үлдэнэ).
        _cancel_pending_edit(person, request.user, "Холбоос салгагдсан", restore=True)
        person.linked_user = None
        person.save(update_fields=["linked_user"])
        services.log_action(request.user, AuditLog.Action.UPDATE, person,
                            {"account_unlink": email},
                            ip_address=services.client_ip(request))
        messages.success(request, f"«{email}» салгагдлаа.")
    return redirect("genealogy:person_edit", pk=person.pk)


# ---------------------------------------------------------------------------
# Газар
# ---------------------------------------------------------------------------


def _place_rows(selected=None):
    """Сонголтын жагсаалт. select_related нь full_name-ийг query-гүй болгоно."""
    places = Place.objects.select_related("parent__parent__parent").all()
    return [{"pk": p.pk, "label": p.full_name, "selected": p.pk == selected} for p in places]


@login_required
def place_create(request):
    """Газар нэмэх (форм дотороос, HTMX).

    Амжилттай бол тухайн select-ийн <option>-уудыг буцааж, шинэ газрыг
    сонгосон байдлаар тавина — формын бусад талбар хэвээр үлдэнэ.
    """
    if not getattr(request.user, "can_edit", False):
        return HttpResponse("Эрх байхгүй", status=403)

    target = request.POST.get("target") or request.GET.get("target") or ""

    if request.method == "POST":
        form = PlaceForm(request.POST)
        if form.is_valid():
            place = form.save()
            services.log_action(request.user, AuditLog.Action.CREATE, place,
                                ip_address=services.client_ip(request))
            response = render(request, "genealogy/partials/_place_options.html",
                              {"places": _place_rows(place.pk)})
            response["HX-Trigger"] = "place:created"
            return response
    else:
        form = PlaceForm()

    return render(request, "genealogy/partials/_place_modal.html",
                  {"form": form, "target": target})



# ---------------------------------------------------------------------------
# Админы хэрэгсэл
# ---------------------------------------------------------------------------


@login_required
def duplicates(request):
    """Ижил нэртэй хүмүүс — нэгтгэхэд туслах."""
    if not getattr(request.user, "can_edit", False):
        messages.error(request, "Эрх байхгүй.")
        return redirect("genealogy:home")
    groups = (
        Person.objects.visible_to(request.user)
        .values("given_name", "patronymic")
        .annotate(n=Count("id"))
        .filter(n__gt=1)
        .order_by("-n")[:50]
    )
    result = []
    for group in groups:
        people = Person.objects.visible_to(request.user).filter(
            given_name=group["given_name"], patronymic=group["patronymic"]
        )
        result.append(people)
    return render(request, "genealogy/duplicates.html",
                  {"groups": result,
                   "masked_pks": {p.pk for g in result for p in g
                                  if p.is_masked_for(request.user)}})


@require_POST
@login_required
def merge_view(request):
    if not getattr(request.user, "can_edit", False):
        return HttpResponse("Эрх байхгүй", status=403)
    # Нэгтгэл нь ХОЁР хүний талбарыг хоёуланг нь өөрчилдөг (`merge_persons`
    # duplicate-ийн холбоог keep рүү шилжүүлж, keep-ийн хоосон талбарыг
    # бөглөнө) тул хоёуланд нь засах эрх шаардана.
    keep = _person_or_404(Person.objects.editable_by(request.user), request.POST.get("keep"))
    duplicate = _person_or_404(Person.objects.editable_by(request.user),
                               request.POST.get("duplicate"))
    try:
        services.merge_persons(keep, duplicate, user=request.user)
        messages.success(request, f"{duplicate.display_name} → {keep.display_name} нэгтгэгдлээ.")
    except ValueError as exc:
        messages.error(request, str(exc))
    return redirect("genealogy:duplicates")


@login_required
def audit_list(request):
    if not getattr(request.user, "can_edit", False):
        return redirect("genealogy:home")
    # «Хаалттай» хүний нэр (`object_repr`) болон талбарын утга (`changes`) нь
    # аудит бүртгэлээр дамжин засварлагчид алдарахгүй — харах эрхтэй хүмүүсийн
    # бүртгэлийг л харуулна.
    person_ct = ContentType.objects.get_for_model(Person)
    # object_id нь varchar, Person.pk нь uuid — subquery-д текст болгон харьцуулна.
    visible_ids = (Person.objects.visible_to(request.user)
                   .annotate(pk_text=Cast("pk", CharField())).values_list("pk_text", flat=True))
    logs = (AuditLog.objects.select_related("user", "content_type")
            .filter(Q(content_type=person_ct, object_id__in=visible_ids)
                    | ~Q(content_type=person_ct))
            .order_by("-created_at")[:200])
    return render(request, "genealogy/audit.html", {"logs": logs})


# ---------------------------------------------------------------------------
# Өөрийн засварын батламж
# ---------------------------------------------------------------------------


def _approvable_edits(user):
    """`user`-ийн БАТАЛЖ болох хүсэлтүүд.

    Мөр тус бүрийн дүрмийг давхарлахгүй — `editable_by`-г нэг л удаа
    дуудаж, түүнийгээ шүүлт болгоно (`Person.can_edit_by`-тай ижил дүрэм).
    Өөрийнхөө хүсэлтийг хасна: хүсэлт гаргагч өөрийгөө батлахгүй, статусаа
    хүний хуудсан дээрээ харна.
    """
    return (PersonEditRequest.objects
            .select_related("person", "submitted_by", "resolved_by")
            .filter(person__in=Person.objects.editable_by(user))
            .exclude(submitted_by=user))


@login_required
def person_edit_request_list(request):
    """Батлах хүсэлтүүд — зөвхөн тухайн хүнийг засах эрхтэй админ/editor."""
    if not getattr(request.user, "can_edit", False):
        messages.error(request, "Зөвхөн админ эсвэл editor хүсэлт харна.")
        return redirect("genealogy:home")

    base = _approvable_edits(request.user)
    pending = list(base.filter(status=PersonEditRequest.Status.PENDING))
    decided = list(base.exclude(status=PersonEditRequest.Status.PENDING)[:50])
    # Диффийг view дээр бодож, мөр бүр дээрээ хавсаргана — template-д
    # аргументтай метод дуудах эсвэл толь бичгээс pk-ээр хайх шаардлагагүй.
    for req in pending:
        req.diff = services.describe_person_diff(req)
    return render(request, "genealogy/person_edit_request_list.html", {
        "pending": pending,
        "decided": decided,
    })


@login_required
@require_POST
def person_edit_request_decide(request, pk, action):
    """Зөвшөөрөх / татгалзах.

    Зөвхөн тухайн хүнийг засах эрхтэй админ/editor, мөн хүсэлт гаргаагүй
    хүн. Зөвшөөрөхөд `Person` мөр аль хэдийн шинэ утгатай байгаа тул дахин
    бичихгүй; татгалзахад `old_values`-ээс сэргээнэ.
    """
    if action not in {"approve", "reject"}:
        raise Http404
    user = request.user
    if not getattr(user, "can_edit", False):
        messages.error(request, "Зөвхөн админ эсвэл editor шийдвэрлэнэ.")
        return redirect("genealogy:home")

    with transaction.atomic():
        req = get_object_or_404(
            PersonEditRequest.objects.select_for_update()
            .select_related("person", "submitted_by"), pk=pk)
        # Объектын түвшний хориг → 404 (оршин байгааг задруулахгүй).
        if not req.person.can_edit_by(user) or req.submitted_by_id == user.pk:
            raise Http404
        if not req.is_pending:
            messages.error(request, "Энэ хүсэлт аль хэдийн шийдэгдсэн.")
            return redirect("genealogy:person_edit_request_list")

        if action == "reject":
            person = Person.objects.select_for_update().get(pk=req.person_id)
            services.restore_person_snapshot(person, req.old_values, req.changed_fields)
            services.log_action(user, AuditLog.Action.UPDATE, person,
                                {"reverted_fields": list(req.changed_fields)},
                                ip_address=services.client_ip(request))
            req.status = PersonEditRequest.Status.REJECTED
        else:
            req.status = PersonEditRequest.Status.APPROVED

        req.resolved_at = timezone.now()
        req.resolved_by = user
        req.save(update_fields=["status", "resolved_at", "resolved_by"])

    link = f"{settings.SITE_URL}{reverse('genealogy:person_detail', args=[req.person_id])}"
    send_edit_resolved_email(req.submitted_by.email, req.person.named_display,
                             action == "approve", link)
    messages.success(request, "Хүсэлт шийдэгдлээ.")
    return redirect("genealogy:person_edit_request_list")
