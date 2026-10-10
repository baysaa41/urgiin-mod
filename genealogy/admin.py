"""Django admin — бөөнөөр мэдээлэл оруулах үндсэн зам.

Бизнес логик models/services дотор байгаа тул admin болон захиалгат форм
хоёулаа нэг дүрмийг дагана (давхардуулахгүй).
"""

from django.contrib import admin, messages
from django.contrib.contenttypes.admin import GenericStackedInline, GenericTabularInline
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from .models import (
    AuditLog,
    ChildLink,
    Citation,
    Event,
    EventParticipant,
    Family,
    FlexDate,
    Media,
    MediaLink,
    Person,
    PersonEditRequest,
    PersonName,
    Place,
    Source,
)
from .forms import AdminEventForm, AdminFamilyForm, AdminPersonForm
from .services import merge_persons

admin.site.register(FlexDate)


class PersonNameInline(admin.TabularInline):
    model = PersonName
    extra = 0


class ChildLinkInline(admin.TabularInline):
    """Энэ хүн хүүхэд болж байгаа гэр бүлүүд (өөрөөр хэлбэл эцэг эх)."""

    model = ChildLink
    fk_name = "child"
    extra = 0
    autocomplete_fields = ["family"]
    verbose_name = "Эцэг эхийн холбоо"
    verbose_name_plural = "Эцэг эхийн холбоо"


class EventParticipantInline(admin.TabularInline):
    model = EventParticipant
    extra = 0
    autocomplete_fields = ["person"]


class MediaLinkInline(GenericTabularInline):
    model = MediaLink
    extra = 0
    autocomplete_fields = ["media"]


class CitationInline(GenericStackedInline):
    model = Citation
    extra = 0
    autocomplete_fields = ["source"]


@admin.register(Person)
class PersonAdmin(admin.ModelAdmin):
    form = AdminPersonForm
    list_display = ("display_name", "sex", "lifespan", "life_badge", "visibility",
                    "is_open_for_edit", "updated_at")
    list_filter = ("sex", "visibility", "living_override", "is_open_for_edit")
    search_fields = ("clan_name", "patronymic", "surname", "given_name", "names__given_name")
    autocomplete_fields = ["birth_place", "death_place"]
    readonly_fields = ("search_text", "created_at", "updated_at", "merged_into")
    inlines = [PersonNameInline, ChildLinkInline, EventParticipantInline, MediaLinkInline, CitationInline]
    fieldsets = (
        ("Нэр", {"fields": ("clan_name", "patronymic", "surname", "given_name", "sex")}),
        ("Огноо ба газар", {"fields": ("birth_date", "birth_place", "death_date", "death_place")}),
        ("Харагдах байдал ба эрх",
         {"fields": ("living_override", "visibility", "is_open_for_edit", "merged_into")}),
        ("Тэмдэглэл", {"fields": ("bio", "notes")}),
        ("Техник", {"fields": ("gedcom_xref", "search_text", "created_by", "created_at", "updated_at"),
                    "classes": ("collapse",)}),
    )
    actions = ["merge_selected"]

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("birth_date", "death_date")

    @admin.display(description="Төлөв")
    def life_badge(self, obj):
        """Нас барсан хүнийг secondary (саарал) өнгөөр тэмдэглэнэ.

        Амьд хүнд ямар ч тэмдэг тавихгүй — нэмэлт биш, хасах зарчим.

        АНХААР: format_html()-ыг аргументгүй дуудаж БОЛОХГҮЙ
        (TypeError: args or kwargs must be provided). Орлуулах утга
        байхгүй тул mark_safe ашиглана.
        """
        if obj.is_deceased:
            return mark_safe('<span style="color:#6c757d">нас барсан</span>')
        return ""

    @admin.action(description="Сонгосон хүмүүсийг нэгтгэх (эхнийх рүү)")
    def merge_selected(self, request, queryset):
        people = list(queryset)
        if len(people) != 2:
            self.message_user(request, "Яг 2 хүн сонгоно уу (эхнийх рүү нэгтгэнэ).", messages.ERROR)
            return
        keep, duplicate = people
        try:
            merge_persons(keep, duplicate, user=request.user)
        except ValueError as exc:
            self.message_user(request, str(exc), messages.ERROR)
        else:
            self.message_user(request, f"{duplicate} → {keep} нэгтгэгдлээ.", messages.SUCCESS)


@admin.register(Family)
class FamilyAdmin(admin.ModelAdmin):
    form = AdminFamilyForm
    list_display = ("__str__", "family_type", "marriage_date", "divorced")
    search_fields = ("partner1__given_name", "partner2__given_name", "partner1__patronymic",
                     "partner2__patronymic")
    autocomplete_fields = ["partner1", "partner2", "marriage_place"]

    @admin.display(boolean=True, description="Салсан")
    def divorced(self, obj):
        return obj.divorce_date_id is not None


@admin.register(ChildLink)
class ChildLinkAdmin(admin.ModelAdmin):
    list_display = ("child", "family", "pedigree", "child_order")
    list_filter = ("pedigree",)
    search_fields = ("child__given_name", "child__patronymic")
    autocomplete_fields = ["family", "child"]


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    form = AdminEventForm
    list_display = ("event_type", "date", "place", "description_short")
    list_filter = ("event_type",)
    search_fields = ("description", "place__name")
    autocomplete_fields = ["place", "family"]
    inlines = [EventParticipantInline, CitationInline]

    @admin.display(description="Тайлбар")
    def description_short(self, obj):
        return (obj.description[:60] + "…") if len(obj.description) > 60 else obj.description


@admin.register(Place)
class PlaceAdmin(admin.ModelAdmin):
    list_display = ("name", "place_type", "parent", "full_name_display")
    list_filter = ("place_type",)
    search_fields = ("name",)
    autocomplete_fields = ["parent"]

    @admin.display(description="Бүтэн нэр")
    def full_name_display(self, obj):
        return obj.full_name


@admin.register(Source)
class SourceAdmin(admin.ModelAdmin):
    list_display = ("title", "source_type", "author", "repository")
    list_filter = ("source_type",)
    search_fields = ("title", "author", "repository")
    inlines = [CitationInline]


@admin.register(Citation)
class CitationAdmin(admin.ModelAdmin):
    list_display = ("source", "content_object", "quality", "page")
    list_filter = ("quality",)
    autocomplete_fields = ["source"]


@admin.register(Media)
class MediaAdmin(admin.ModelAdmin):
    list_display = ("title", "kind", "display_thumb", "created_at")
    list_filter = ("kind",)
    search_fields = ("title", "caption")
    autocomplete_fields = ["source"]

    @admin.display(description="Жижиг")
    def display_thumb(self, obj):
        if obj.thumbnail:
            return format_html('<img src="{}" style="height:48px">', obj.thumbnail.url)
        return "—"


@admin.register(PersonName)
class PersonNameAdmin(admin.ModelAdmin):
    list_display = ("__str__", "person", "name_type")
    search_fields = ("given_name", "patronymic", "surname")
    autocomplete_fields = ["person"]


@admin.register(PersonEditRequest)
class PersonEditRequestAdmin(admin.ModelAdmin):
    """Зөвхөн уншина — шийдвэрийг апп дотроос (`/manage/person-edits/`) гаргана."""

    list_display = ("created_at", "person", "submitted_by", "status", "changed_fields")
    list_filter = ("status",)
    search_fields = ("person__given_name", "person__patronymic", "submitted_by__email")
    readonly_fields = [f.name for f in PersonEditRequest._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "user", "action", "object_repr")
    list_filter = ("action",)
    search_fields = ("object_repr",)
    readonly_fields = [f.name for f in AuditLog._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
