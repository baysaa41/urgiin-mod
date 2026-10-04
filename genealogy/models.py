"""Ургийн модны домэйн загвар (GEDCOM хэлбэртэй).

Гол зарчим: ургийн холбоо бол DAG (мод биш) — нэг хүн хоёр эцэг эхтэй, нэг өвөг
олон замаар давтагдана. Тиймээс mptt/treebeard ашиглахгүй; холбоог ChildLink
хүснэгтээр илэрхийлж, удмыг WITH RECURSIVE-ээр бодно (services.py).
"""

import re
from datetime import date

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey, GenericRelation
from django.contrib.contenttypes.models import ContentType
from django.contrib.postgres.indexes import GinIndex
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone

# ---------------------------------------------------------------------------
# Огноо
# ---------------------------------------------------------------------------


class FlexDate(models.Model):
    """Бүрэн бус ч гэсэн хадгалагдах огноо.

    Монголын архивт "ойролцоогоор 1920", "1920-04", "1919/1920", "?" гэх мэт
    бичлэг түгээмэл. Тиймээс гурван зүйлийг зэрэг хадгална:
      * raw       — хэрэглэгчийн бичсэн текст (үзүүлэхэд),
      * qualifier — ямар нарийвчлалтай вэ (GEDCOM-ийн ABT/BEF/AFT/EST/CAL),
      * sort_key  — эрэмбэлэх/зураглах/нас тооцоход хэрэглэх нэг огноо.
    """

    class Qualifier(models.TextChoices):
        EXACT = "exact", "Яг"
        ABOUT = "about", "Ойролцоогоор"          # GEDCOM ABT
        ESTIMATED = "estimated", "Тооцоолсон"    # EST / CAL
        BEFORE = "before", "Өмнө"                # BEF
        AFTER = "after", "Дараа"                 # AFT
        RANGE = "range", "Хугацаа"               # BET ... AND ...
        UNKNOWN = "unknown", "Тодорхойгүй"

    class Precision(models.TextChoices):
        DAY = "day", "Өдөр"
        MONTH = "month", "Сар"
        YEAR = "year", "Он"
        DECADE = "decade", "Арван жил"
        UNKNOWN = "unknown", "Тодорхойгүй"

    raw = models.CharField("Бичсэн огноо", max_length=100, blank=True)
    qualifier = models.CharField(
        "Нарийвчлал", max_length=12, choices=Qualifier.choices, default=Qualifier.EXACT
    )
    precision = models.CharField(
        "Нарийвчлалын түвшин", max_length=10, choices=Precision.choices, default=Precision.UNKNOWN
    )
    date_from = models.DateField("Эхлэх", null=True, blank=True)
    date_to = models.DateField("Дуусах", null=True, blank=True)
    # Бүх эрэмбэлэлт, харьцуулалт, насны тооцоо энэ баганад тулгуурлана (null биш).
    sort_key = models.DateField("Эрэмбэлэх огноо", default=date.min)
    gedcom_raw = models.CharField("GEDCOM огноо", max_length=100, blank=True)

    class Meta:
        db_table = "genealogy_flexdate"
        verbose_name = "Огноо"
        verbose_name_plural = "Огноонууд"

    def __str__(self):
        return self.display

    def save(self, *args, **kwargs):
        if self.sort_key in (None, date.min):
            self.sort_key = self._compute_sort_key()
        super().save(*args, **kwargs)

    # -- дэлгэрэнгүй --------------------------------------------------------

    _MONTHS = {
        "01": 1, "02": 2, "03": 3, "04": 4, "05": 5, "06": 6,
        "07": 7, "08": 8, "09": 9, "10": 10, "11": 11, "12": 12,
    }

    # `qualifier` нь бүтэцтэй талбар тул «ойролцоогоор» гэх мэт үгийг `raw`-д
    # давхар бичих шаардлагагүй — дүрслэхдээ эндээс нэмнэ. Хугацаа (`range`)
    # нь өөрөө ойлгомжтой (`1920–1925`) тул угтваргүй.
    # Ойролцоо оны тэмдэг — угтвар биш, АРД нь «?» тавина («1950?»).
    # Угтвар үг («ойролцоогоор 1950») модны зангилаа мэт нарийн зайд
    # багтдаггүй; «?» нь «тодорхойгүй» гэсэн ганц «?»-тайгаа нийцтэй.
    _ABOUT_MARK = "?"
    _QUALIFIER_PREFIX = {
        Qualifier.ESTIMATED: "тооцоолсон",
        Qualifier.BEFORE: "өмнө",
        Qualifier.AFTER: "дараа",
    }

    @property
    def display(self):
        """Хэрэглэгчид харуулах текст.

        `qualifier`-ийн утгыг энд нэмнэ — ингэснээр `raw`-д давхар
        хадгалахгүй, тэмдэг нь цорын ганц эх сурвалж болно.
        «Ойролцоогоор» нь угтвар биш, АРДЫН «?» тэмдгээр илэрнэ («1950?»);
        бусад нь угтвар («тооцоолсон 1950», «өмнө 1950»). Хуучин бичлэгт
        угтвар нь `raw`-д байвал дахин нэмэхгүй.
        """
        text = self.raw or self._text_from_dates() or "?"
        if self.qualifier == self.Qualifier.ABOUT:
            # «ойролцоогоор 1950» / «1950?» / «1945~» → «1950?»
            body = text
            for word in ("ойролцоогоор", "ойролцоо"):
                if body.lower().startswith(word):
                    body = body[len(word):]
                    break
            body = body.strip("?~").strip()
            if not body:
                return text
            return f"{body}{self._ABOUT_MARK}" if self._has_year(body) else body
        prefix = self._QUALIFIER_PREFIX.get(self.qualifier)
        if not prefix:
            return text
        body = text[len(prefix):] if text.startswith(prefix) else text
        body = body.strip("?~").strip()
        return f"{prefix} {body}" if body else text

    @staticmethod
    def _has_year(text):
        """Текст дотор 4 оронтой он байвал `True` — «?» нэмэх эсэхийг шийднэ."""
        return re.search(r"(?<!\d)\d{4}(?!\d)", text) is not None

    def _text_from_dates(self):
        """`raw` хоосон үед бүтэцтэй талбаруудаас бүрдүүлнэ (хуучин зан төлөв)."""
        if self.date_from and self.date_to and self.date_from != self.date_to:
            return f"{self.date_from.isoformat()} – {self.date_to.isoformat()}"
        if self.date_from:
            return self.date_from.isoformat()
        return ""

    @property
    def year(self):
        return self.sort_key.year if self.sort_key and self.sort_key != date.min else None

    def _compute_sort_key(self):
        if self.qualifier == self.Qualifier.RANGE:
            return self.date_from or self.date_to or date.min
        if self.qualifier in {self.Qualifier.BEFORE, self.Qualifier.AFTER}:
            return self.date_from or self.date_to or date.min
        return self.date_to or self.date_from or date.min

    @classmethod
    def parse(cls, text, *, precision_hint=None):
        """Текстийг FlexDate болгоно. Хадгалахгүй — дуудагч тал хадгална.

        Дэмжих хэлбэрүүд:
          1920 · 1920-04 · 1920-04-15 · 15.04.1920 · 1920/04/15
          ойролцоогоор 1920 · approx 1920 · abt 1920 · ~1920
          өмнө 1920 / before 1920 (BEF) · дараа 1920 / after 1920 (AFT)
          1920/1925 эсвэл 1920-1925  (RANGE)
          ? эсвэл хоосон  (UNKNOWN)

        Дээрх хэлбэрүүд таарахгүй ч текст дотор **4 оронтой он** байвал
        түүнийг ШУУД авна — ингэснээр «1950?», «1945~», «1950 он» гэх мэт
        бичлэг он алдахгүй. «?»/«~» нь эргэлзээ илэрхийлдэг тул ABOUT болно.
        """
        raw = (text or "").strip()
        if not raw or raw in {"?", "-", "—"}:
            return cls(raw=raw or "?", qualifier=cls.Qualifier.UNKNOWN,
                       precision=cls.Precision.UNKNOWN, sort_key=date.min)

        lowered = raw.lower()
        qualifier = cls.Qualifier.EXACT
        for prefix, q in (
            ("ойролцоогоор", cls.Qualifier.ABOUT), ("ойролцоо", cls.Qualifier.ABOUT),
            ("approx", cls.Qualifier.ABOUT), ("abt", cls.Qualifier.ABOUT),
            ("тооцоолсон", cls.Qualifier.ESTIMATED), ("est", cls.Qualifier.ESTIMATED),
            ("өмнө", cls.Qualifier.BEFORE), ("before", cls.Qualifier.BEFORE),
            ("bef", cls.Qualifier.BEFORE),
            ("дараа", cls.Qualifier.AFTER), ("after", cls.Qualifier.AFTER),
            ("aft", cls.Qualifier.AFTER),
        ):
            if lowered.startswith(prefix):
                qualifier = q
                lowered = lowered[len(prefix):].strip(" :~")
                break
        if lowered.startswith("~"):
            qualifier = cls.Qualifier.ABOUT
            lowered = lowered[1:].strip()

        # Хугацаа: 1920/1925 — гэхдээ 1920/04/15 нь бүтэн огноо тул тусгайлан шалгана.
        if re.fullmatch(r"\d{4}\s*[/\-–]\s*\d{4}", lowered):
            a, b = re.split(r"\s*[/\-–]\s*", lowered)
            return cls(raw=raw, qualifier=cls.Qualifier.RANGE, precision=cls.Precision.YEAR,
                       date_from=date(int(a), 1, 1), date_to=date(int(b), 12, 31),
                       sort_key=date(int(a), 1, 1))

        day, month, year, precision = cls._parse_components(lowered)
        if year is None:
            # Сүүлийн оролдлого: текст дотроос 4 оронтой ОНЫГ шууд авна.
            # Ингэснээр «1950?», «1945~», «1950 он» гэх мэт бичлэг он
            # алдахгүй — өмнө нь бүхэлдээ UNKNOWN болдог байсан.
            found = next((n for n in re.findall(r"(?<!\d)\d{4}(?!\d)", lowered)
                          if 1000 <= int(n) <= 2099), None)
            if found is None:
                # Танихгүй текст — алдаа өгөхгүй, түүхийг хэвээр хадгална.
                return cls(raw=raw, qualifier=cls.Qualifier.UNKNOWN,
                           precision=cls.Precision.UNKNOWN, sort_key=date.min)
            year = int(found)
            precision = cls.Precision.YEAR
            # «?»/«~» нь «эргэлзээтэй» гэсэн утгатай тул ABOUT.
            if qualifier == cls.Qualifier.EXACT and ("?" in lowered or "~" in lowered):
                qualifier = cls.Qualifier.ABOUT

        first = cls._safe_date(year, month, day)
        last = cls._safe_date(year, month, day)
        if precision == cls.Precision.YEAR:
            first, last = cls._safe_date(year, 1, 1), cls._safe_date(year, 12, 31)
        elif precision == cls.Precision.MONTH:
            first, last = cls._safe_date(year, month, 1), cls._last_day(year, month)

        return cls(
            raw=raw, qualifier=qualifier, precision=precision,
            date_from=first, date_to=last, sort_key=last or first or date.min,
        )

    @classmethod
    def _parse_components(cls, text):
        """(өдөр, сар, он, нарийвчлал) буцаана. Танихгүй бол (None, None, None, UNKNOWN)."""
        # 1920-04-15 / 1920/04/15
        m = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", text)
        if m:
            y, mo, d = (int(x) for x in m.groups())
            return d, mo, y, cls.Precision.DAY
        # 15.04.1920 / 15/04/1920
        m = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", text)
        if m:
            d, mo, y = (int(x) for x in m.groups())
            return d, mo, y, cls.Precision.DAY
        # 1920-04
        m = re.fullmatch(r"(\d{4})[-/.](\d{1,2})", text)
        if m:
            y, mo = int(m.group(1)), int(m.group(2))
            return None, mo, y, cls.Precision.MONTH
        # 1920
        m = re.fullmatch(r"(\d{4})", text)
        if m:
            return None, None, int(m.group(1)), cls.Precision.YEAR
        # 1920-аад он
        m = re.fullmatch(r"(\d{4})\s*-?\s*аад\s*он", text)
        if m:
            y = int(m.group(1))
            return None, None, y, cls.Precision.DECADE
        return None, None, None, cls.Precision.UNKNOWN

    @classmethod
    def create_from_text(cls, text):
        """Текстээс шууд үүсгэж хадгална — форм болон quick-add-д хэрэглэнэ."""
        obj = cls.parse(text)
        obj.save()
        return obj

    @staticmethod
    def _safe_date(year, month, day):
        try:
            return date(year, month or 1, day or 1)
        except ValueError:
            return None

    @staticmethod
    def _last_day(year, month):
        import calendar
        return date(year, month, calendar.monthrange(year, month)[1])


# ---------------------------------------------------------------------------
# Газар
# ---------------------------------------------------------------------------


class Place(models.Model):
    """Газар — аймаг/сум/хот зэрэг шаталсан бүтэцтэй."""

    class PlaceType(models.TextChoices):
        COUNTRY = "country", "Улс"
        AIMAG = "aimag", "Аймаг"
        SUM = "sum", "Сум"
        BAG = "bag", "Баг"
        CITY = "city", "Хот"
        TOWN = "town", "Суурин"
        OTHER = "other", "Бусад"

    name = models.CharField("Нэр", max_length=200)
    place_type = models.CharField("Төрөл", max_length=20, choices=PlaceType.choices, default=PlaceType.OTHER)
    parent = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="children", verbose_name="Дээд нэгж",
    )
    latitude = models.DecimalField("Өргөрөг", max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField("Уртраг", max_digits=9, decimal_places=6, null=True, blank=True)
    gedcom_raw = models.CharField("GEDCOM газар", max_length=300, blank=True)

    class Meta:
        db_table = "genealogy_place"
        verbose_name = "Газар"
        verbose_name_plural = "Газрууд"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["name", "parent"], name="place_unique_name_parent"),
        ]

    def __str__(self):
        return self.full_name

    @property
    def full_name(self):
        parts, node, seen = [], self, set()
        while node is not None and node.pk not in seen:
            seen.add(node.pk)
            parts.append(node.name)
            node = node.parent
        return ", ".join(reversed(parts))


# ---------------------------------------------------------------------------
# Хүн
# ---------------------------------------------------------------------------


def _lineage_ids(user):
    """Хэрэглэгчтэй холбогдсон хүний өвөг дээдэс + үр хойчсын id (frozenset).

    Засварлагч өөрийн удмыг засах эрхтэй. CTE нь хүсэлт бүрд нэг л удаа
    ажиллахын тулд үр дүнг `user` объект дээр кэшилнэ (`request.user` нь
    хүсэлт бүрд шинэ объект тул хуучирсан кэш хүсэлт дамжихгүй).
    """
    cached = getattr(user, "_lineage_ids_cache", None)
    if cached is not None:
        return cached
    from .services import ancestors, descendants  # services нь models-ийг импортолдог

    ids = frozenset()
    person = Person.objects.alive().filter(linked_user_id=user.pk).first()
    if person is not None:
        ids = frozenset(ancestors(person)) | frozenset(descendants(person))
    user._lineage_ids_cache = ids
    return ids


class PersonQuerySet(models.QuerySet):
    def alive(self):
        """Нэгтгэгдэж устсан (merged_into) бичлэгүүдийг хасна."""
        return self.filter(merged_into__isnull=True)

    @staticmethod
    def deceased_q():
        """«Нас барсан» гэж SQL түвшинд тодорхойлох (Person.life_status-тай нийцнэ).

        Огноо огт байхгүй бол «нас барсан» гэж ҮЗЭХГҮЙ — тодорхойгүй хүнийг
        амьд мэт хамгаална (нууцлалын аюулгүй тал).
        """
        cutoff = date(timezone.now().year - settings.LIVING_YEARS, 1, 1)
        return (
            Q(living_override=False)
            | Q(living_override__isnull=True, death_date__isnull=False)
            | Q(
                living_override__isnull=True,
                death_date__isnull=True,
                birth_date__isnull=False,
                birth_date__sort_key__lt=cutoff,
            )
        )

    def visible_to(self, user):
        """Хэрэглэгчид НЭРИЙН түвшинд харагдах хүмүүс.

        Нуухыг query түвшинд шийднэ — template-д нуух нь хангалтгүй (JSON
        endpoint алдагдана). «Хувийн» хүний НЭР харагдана (нас/хүйс нь
        `can_see_details`-аар хаалттай), «Хаалттай» хүнийг зөвхөн админ харна.
        """
        qs = self.alive()
        if user is not None and user.is_authenticated:
            if getattr(user, "is_admin", False):
                return qs
            # Засварлагч ч, энгийн гишүүн ч «Хаалттай»-г харахгүй. Холбогдсон
            # хүн өөрийн бичлэгээ CLOSED байсан ч харна — эс бөгөөс засах
            # эрхтэй мөртлөө профайл нь 404 болно (унших/засах зөрүү).
            return qs.filter(Q(linked_user_id=user.pk) | ~Q(visibility=Person.Visibility.CLOSED))
        # Аноним: зөвхөн нээлттэй, эсвэл «автомат» ба нас барсан.
        return qs.filter(
            Q(visibility=Person.Visibility.PUBLIC)
            | (Q(visibility=Person.Visibility.AUTO) & self.deceased_q())
        )

    def editable_by(self, user):
        """Хэрэглэгчийн ЗАСАХ эрхтэй хүмүүс.

        Засварлагч зөвхөн өөрийн нэмсэн, «Нээлттэй» гэж тэмдэглэсэн, оруулсан
        нь тодорхойгүй (хуучин) хүн, эсвэл өөрийн өвөг дээдэс / үр хойчсыг
        засна. Админ хязгааргүй.

        `Person.can_edit_by`-тай ЯГ ижил дүрэм байх ёстой — хоёулаа хамт
        өөрчлөгдөнө (README-ийн `_link` ↔ `_blocked_for_relation` хоёрын адил).
        """
        qs = self.alive()
        if (user is None or not getattr(user, "is_authenticated", False)
                or not getattr(user, "is_active", False)):
            return qs.none()
        # Холбогдсон хэрэглэгч ЗӨВХӨН өөрийн бичлэгийг засна — үзэгч эрхтэй ч.
        own = Q(linked_user_id=user.pk)
        if not getattr(user, "can_edit", False):
            return qs.filter(own)
        if getattr(user, "is_admin", False):
            return qs
        # «Хаалттай» хүнийг засварлагч ч засахгүй (зөвхөн админ). Холбогдсон
        # хүний `own` нь үл хамаарна — `can_edit_by`-тай ижил.
        return qs.filter(
            own | Q(created_by=user) | Q(created_by__isnull=True) | Q(is_open_for_edit=True)
            | Q(pk__in=_lineage_ids(user))
        ).exclude(Q(visibility=Person.Visibility.CLOSED) & ~own)

    def search(self, text):
        """Нэр/овог/газраар хайх. pg_trgm индекс дээр trigram + энгийн хайлт."""
        text = (text or "").strip()
        if not text:
            return self.none()
        # Сан нь `C` locale-тай тул PostgreSQL-ийн ILIKE кирилл үсгийн том/жижгийг
        # ялгана («Ү» ≠ «ү»). `search_text` нь Python-оор жижиг үсэг болгосон тул
        # асуулгыг ч Python-оор жижигрүүлж харьцуулна.
        return self.alive().filter(
            Q(search_text__contains=text.lower())
            | Q(search_text__icontains=text)
            | Q(given_name__icontains=text)
            | Q(patronymic__icontains=text)
            | Q(clan_name__icontains=text)
            | Q(surname__icontains=text)
        ).distinct()


class Person(models.Model):
    """Нэг хүн (GEDCOM INDI)."""

    class Sex(models.TextChoices):
        MALE = "M", "Эрэгтэй"
        FEMALE = "F", "Эмэгтэй"
        OTHER = "X", "Бусад"
        UNKNOWN = "U", "Тодорхойгүй"

    class Visibility(models.TextChoices):
        AUTO = "auto", "Автоматаар"           # нас барсан → нээлттэй, амьд → гэр бүл
        PUBLIC = "public", "Нээлттэй"        # хүн бүр
        FAMILY = "family", "Гэр бүл"          # нэвтэрсэн гишүүд
        PRIVATE = "private", "Хувийн"         # зөвхөн админ/засварлагч
        # «Хаалттай»: засварлагч ч бүтэн мэдээлэл харахгүй — зөвхөн админ.
        # Бусдад зөвхөн `•••` placeholder харагдана (мод, профайл, хайлт).
        CLOSED = "closed", "Хаалттай"

    class LifeStatus(models.TextChoices):
        LIVING = "living", "Амьд"
        DECEASED = "deceased", "Нас барсан"
        UNKNOWN = "unknown", "Тодорхойгүй"

    # -- нэр (монголын онцлог: овог/яс, эцгийн нэр, өөрийн нэр) --------------
    clan_name = models.CharField("Ургийн овог", max_length=100, blank=True)
    patronymic = models.CharField('Эцгийн нэр ("…ийн")', max_length=100, blank=True)
    surname = models.CharField("Овог (албан)", max_length=100, blank=True)
    given_name = models.CharField("Өөрийн нэр", max_length=100)
    # Хүмүүс хоорондоо дуудах нэр (жишээ нь албан «Батбаясгалан» → «Батаа»).
    # CALLED төрлийн `PersonName`-тэй хоёр тийшээ синхрончлогдоно.
    called_name = models.CharField("Авгайлдаг нэр", max_length=100, blank=True)

    sex = models.CharField("Хүйс", max_length=1, choices=Sex.choices, default=Sex.UNKNOWN)
    birth_date = models.ForeignKey(
        FlexDate, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
        verbose_name="Төрсөн огноо",
    )
    death_date = models.ForeignKey(
        FlexDate, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
        verbose_name="Нас барсан огноо",
    )
    birth_place = models.ForeignKey(
        Place, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
        verbose_name="Төрсөн газар",
    )
    death_place = models.ForeignKey(
        Place, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
        verbose_name="Нас барсан газар",
    )

    # None = таамаглана (нас барсан огноо байхгүй ба сүүлийн 110 жилд төрсөн бол амьд).
    living_override = models.BooleanField(
        "Амьд эсэх (гараар)", null=True, blank=True,
        help_text="Хоосон бол огнооноос таамаглана.",
    )
    visibility = models.CharField(
        "Харагдах байдал", max_length=10, choices=Visibility.choices, default=Visibility.AUTO,
        help_text=("«Автоматаар»: нас барсан хүн нээлттэй, амьд хүн зөвхөн гэр бүлд. "
                   "«Хувийн»: нэр нь харагдана, харин нас/хүйс/газар хаалттай. "
                   "«Хаалттай»: зөвхөн админ харна — бусдад `•••` placeholder."),
    )

    bio = models.TextField("Намтар", blank=True)
    notes = models.TextField("Тэмдэглэл (дотоод)", blank=True)

    # Эсрэг талаас нь шууд авах боломж (person.citations.all(), person.media_links.all()).
    citations = GenericRelation("genealogy.Citation")
    media_links = GenericRelation("genealogy.MediaLink")

    merged_into = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="merged_from", verbose_name="Нэгтгэсэн хүн",
    )
    gedcom_xref = models.CharField("GEDCOM ID", max_length=20, blank=True, null=True, unique=True)

    search_text = models.CharField("Хайлтын текст", max_length=500, blank=True, editable=False)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="persons_created", verbose_name="Оруулсан",
    )
    # «visibility = PUBLIC» нь мөн «Нээлттэй» гэсэн шошготой тул нэрийг
    # зориуд ялгаатай өгсөн — админд хоёр ойлголт холилдохгүйн тулд.
    is_open_for_edit = models.BooleanField(
        "Бүх засварлагч засах боломжтой", default=False,
        help_text="Асаавал зөвхөн эзэмшигч биш, БҮХ засварлагч энэ хүнийг засна.",
    )
    # Амьд хүнийг нэг бодит хэрэглэгчтэй холбоно (1:1). Холбогдсон хэрэглэгч
    # ЗӨВХӨН энэ бичлэгийг засах эрхтэй — үзэгч эрхтэй ч гэсэн.
    linked_user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="person", verbose_name="Холбогдсон хэрэглэгч",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = PersonQuerySet.as_manager()

    class Meta:
        db_table = "genealogy_person"
        verbose_name = "Хүн"
        verbose_name_plural = "Хүмүүс"
        ordering = ["clan_name", "patronymic", "given_name"]
        indexes = [
            # pg_trgm GIN — кирилл нэрээр ойролцоо/хэсэгчилсэн хайлтад (миграц
            # нь CREATE EXTENSION pg_trgm-ийг урьдчилан ажиллуулна).
            GinIndex(fields=["search_text"], name="person_search_trgm", opclasses=["gin_trgm_ops"]),
            models.Index(fields=["merged_into"], name="person_merged_idx"),
        ]

    def __str__(self):
        return self.display_name

    def save(self, *args, **kwargs):
        self.search_text = self.build_search_text()
        super().save(*args, **kwargs)

    # -- нэр ---------------------------------------------------------------

    def build_search_text(self):
        """Альтернатив нэрс (PersonName) хүртэл багтаана — "бүртгэсэн нэр" өөр байж болно."""
        parts = [self.clan_name, self.patronymic, self.surname, self.given_name,
                 self.called_name]
        if self.pk:
            parts.extend(str(n) for n in self.names.all())
        return " ".join(p for p in parts if p).strip().lower()[:500]

    @property
    def named_display(self):
        """Албан нэр ба авгайлдаг нэрийг хамт: «Батбаясгалан (Батаа)».

        Авгайлдаг нэр байхгүй бол `display_name`-тай ижил.
        """
        if self.called_name:
            return f"{self.display_name} ({self.called_name})"
        return self.display_name

    @property
    def display_name(self):
        """Монгол хэв: "Бат-Эрдэнийн Ганбат" (эцгийн нэр + өөрийн нэр)."""
        parts = [p for p in (self.patronymic, self.given_name) if p]
        if not parts:
            parts = [p for p in (self.surname, self.clan_name) if p]
        return " ".join(parts) or "Нэргүй"

    @property
    def short_name(self):
        return self.given_name or self.display_name

    @property
    def lifespan(self):
        """«1950–2007» — ойролцоо он бол «?» тэмдэг хамт («1950?–»).

        Модны зангилаа ба карт дээр энэ мөр харагддаг тул ойролцоо гэдгийг
        энд ч илэрхийлэх ёстой (`FlexDate.display`-тэй ижил дүрэм).
        """
        def mark(fd):
            if not fd:
                return None
            year = fd.year
            if year is None:
                return None
            return f"{year}?" if fd.qualifier == FlexDate.Qualifier.ABOUT else str(year)

        b = mark(self.birth_date)
        d = mark(self.death_date)
        if b and d:
            return f"{b}–{d}"
        if b:
            return f"{b}–"
        if d:
            return f"–{d}"
        return ""

    # -- амьд эсэх ---------------------------------------------------------

    @property
    def life_status(self):
        """Гараар заасан бол түүнийг, эс бөгөөс огнооноос таамаглана.

        Огноо огт байхгүй бол UNKNOWN — нууцлалын хувьд амьд мэт хамгаална.
        """
        if self.living_override is not None:
            return self.LifeStatus.LIVING if self.living_override else self.LifeStatus.DECEASED
        if self.death_date_id is not None:
            return self.LifeStatus.DECEASED
        birth = self.birth_date.year if self.birth_date else None
        if birth is None:
            return self.LifeStatus.UNKNOWN
        if (timezone.now().year - birth) < settings.LIVING_YEARS:
            return self.LifeStatus.LIVING
        return self.LifeStatus.DECEASED

    @property
    def is_living(self):
        return self.life_status == self.LifeStatus.LIVING

    @property
    def is_deceased(self):
        return self.life_status == self.LifeStatus.DECEASED

    @property
    def effective_visibility(self):
        """«Автомат»-ыг бодит түвшин болгож шийднэ."""
        if self.visibility != self.Visibility.AUTO:
            return self.visibility
        return self.Visibility.PUBLIC if self.is_deceased else self.Visibility.FAMILY

    # -- эрх ---------------------------------------------------------------

    def can_view(self, user):
        """НЭРИЙГ харах эрх. «Хаалттай» хүнийг зөвхөн админ харна.

        `visible_to`-той ЯГ ижил дүрэм — хоёулаа хамт өөрчлөгдөнө.
        """
        if user is not None and user.is_authenticated:
            # Холбогдсон хэрэглэгч өөрийн бичлэгээ үргэлж харна.
            if (self.linked_user_id is not None
                    and self.linked_user_id == getattr(user, "pk", None)
                    and getattr(user, "is_active", False)):
                return True
            if getattr(user, "is_admin", False):
                return True
            return self.visibility != self.Visibility.CLOSED
        return self.effective_visibility == self.Visibility.PUBLIC

    def can_see_details(self, user):
        """Нас, хүйс, газар, намтар зэрэг нарийн мэдээлэл харах эрх.

        «Хувийн» хүний нэр харагдана (`can_view`), харин нарийн мэдээлэл нь
        зөвхөн админ, ТУХАЙН хүнийг засах эрхтэй засварлагч (`can_edit_by` —
        өөрийн нэмсэн/нээлттэй/өөрийн удам) болон хүн өөрт нь харагдана.
        Засварлагч нэмэгдэх бүрт хувийн мэдээлэл бүгдэд нь нээгдэхгүй.
        """
        if not self.can_view(user):
            return False
        if user is not None and user.is_authenticated:
            if (self.linked_user_id is not None
                    and self.linked_user_id == getattr(user, "pk", None)):
                return bool(getattr(user, "is_active", False))
            if self.can_edit_by(user):
                return True
        return self.effective_visibility in (
            self.Visibility.PUBLIC, self.Visibility.FAMILY)

    @property
    def is_closed(self):
        return self.visibility == self.Visibility.CLOSED

    def is_masked_for(self, user):
        """Нэр нь харагдах ч нарийн мэдээлэл (нас, хүйс, …) хаалттай эсэх."""
        return self.can_view(user) and not self.can_see_details(user)

    def can_edit_by(self, user):
        """Энэ хүний мэдээллийг `user` засах эрхтэй эсэх.

        `PersonQuerySet.editable_by`-тай ЯГ ижил дүрэм — хоёулаа хамт өөрчлөгдөнө.
        """
        if user is None or not getattr(user, "is_authenticated", False):
            return False
        # Холбогдсон хэрэглэгч ЗӨВХӨН өөрийн бичлэгийг засна — үзэгч эрхтэй ч
        # (`can_edit` хаалтаас ӨМНӨ, эс бөгөөс үзэгч хэзээ ч нэвтрэхгүй).
        if self.linked_user_id is not None and self.linked_user_id == getattr(user, "pk", None):
            return bool(getattr(user, "is_active", False))
        if not getattr(user, "can_edit", False):
            return False
        if getattr(user, "is_admin", False):
            return True
        # «Хаалттай» хүнийг засварлагч ч засахгүй — `editable_by`-тай ижил.
        if self.visibility == self.Visibility.CLOSED:
            return False
        if self.created_by_id is None or self.is_open_for_edit:
            return True
        if self.created_by_id == getattr(user, "pk", None):
            return True
        # Өөрийн өвөг дээдэс / үр хойчис (холбогдсон хүнээс CTE-ээр).
        return self.pk in _lineage_ids(user)

    # Template-ууд `p.is_hidden`-ээр «харагдахгүй хүний» placeholder-ыг ялгана
    # (`services.HiddenPerson` нь `True`). Жинхэнэ хүн хэзээ ч placeholder биш.
    is_hidden = False

    def masked_name(self):
        """Нууцлах үед зөвхөн нэрийн эхний үсэг."""
        return " ".join(f"{p[0]}." for p in (self.patronymic, self.given_name) if p)

    # -- холбоо ------------------------------------------------------------

    @property
    def father(self):
        """Төрсөн гэр бүлийн эцэг — ХҮЙСЭЭР тодорхойлно.

        `partner1`/`partner2`-оор биш: `_link` чөлөөт талд тавьдаг тул эцэг
        хоёрдугаарт ч орж болно. Хүйс нь тодорхойгүй бол таахгүй (`None`) —
        буруу хүний овгийг өвлүүлэхээс сэргийлнэ.
        """
        family = self.birth_family
        if family is None:
            return None
        for partner in family.partners:
            if partner.pk != self.pk and partner.sex == self.Sex.MALE:
                return partner
        return None

    def inherit_clan_name(self):
        """Ургийн овог хоосон бол эцгээс нь нөхнө. Нөхсөн бол `True`.

        Овог нь хүүхдэд эцгээсээ дамждаг тул гараар бичих шаардлагагүй.
        Хэрэглэгч өөрөө бичсэн бол (хоосон биш) хэзээ ч дарж бичихгүй.
        """
        if self.clan_name:
            return False
        father = self.father
        if father is None or not father.clan_name:
            return False
        self.clan_name = father.clan_name
        self.save(update_fields=["clan_name", "search_text"])
        return True

    @property
    def birth_family(self):
        """Төрсөн гэр бүл (нэг л байна — pedigree=BIRTH)."""
        link = self.parent_links.filter(pedigree=ChildLink.Pedigree.BIRTH).select_related(
            "family__partner1", "family__partner2"
        ).first()
        return link.family if link else None

    @property
    def parents(self):
        fam = self.birth_family
        return [p for p in (fam.partner1, fam.partner2) if p] if fam else []

    @property
    def families(self):
        """Эхнэр/нөхөр болж байгаа гэр бүлүүд."""
        return Family.objects.filter(Q(partner1=self) | Q(partner2=self)).select_related(
            "partner1", "partner2"
        )


class PersonName(models.Model):
    """Нэмэлт нэр — гэрлэсэн нэр, хоч, өөр бичлэг ("бүртгэсэн нэр ≠ албан нэр")."""

    class NameType(models.TextChoices):
        BIRTH = "birth", "Төрсөн нэр"
        MARRIED = "married", "Гэрлэсэн нэр"
        RELIGIOUS = "religious", "Шашны нэр"
        NICKNAME = "nickname", "Хоч"
        CALLED = "called", "Авгайлдаг нэр"
        AKA = "aka", "Өөр бичлэг"
        OTHER = "other", "Бусад"

    person = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="names", verbose_name="Хүн")
    name_type = models.CharField("Нэрийн төрөл", max_length=20, choices=NameType.choices, default=NameType.AKA)
    clan_name = models.CharField("Ургийн овог", max_length=100, blank=True)
    patronymic = models.CharField("Эцгийн нэр", max_length=100, blank=True)
    surname = models.CharField("Овог", max_length=100, blank=True)
    given_name = models.CharField("Өөрийн нэр", max_length=100, blank=True)
    note = models.CharField("Тайлбар", max_length=200, blank=True)

    class Meta:
        db_table = "genealogy_personname"
        verbose_name = "Хүний нэр"
        verbose_name_plural = "Хүний нэрс"

    def __str__(self):
        return " ".join(p for p in (self.patronymic, self.given_name, self.surname) if p)

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        self.sync_person()

    def delete(self, *args, **kwargs):
        person_id = self.person_id
        super().delete(*args, **kwargs)
        # Устгасны дараа л синхрончлох ёстой — үлдсэн CALLED нэрээр шинэчилнэ.
        person = Person.objects.filter(pk=person_id).first()
        if person is not None:
            self.person = person
            self.sync_person()

    def sync_person(self):
        """Хайлтын текст ба «авгайлдаг нэр»-ийг эх хүн дээрээ шинэчилнэ.

        `Person.called_name` ба `CALLED` төрлийн `PersonName` хоёр нэг утгыг
        хадгалдаг тул аль замаар ч (апп эсвэл admin) засахад тааруулна.
        `QuerySet.update()` нь `Person.save()`-ыг дуудахгүй — давталт үүсэхгүй.
        """
        person = self.person
        if person is None or person.pk is None:
            return
        first_called = (
            PersonName.objects.filter(person_id=person.pk,
                                      name_type=self.NameType.CALLED)
            .exclude(given_name="").order_by("pk").first()
        )
        Person.objects.filter(pk=person.pk).update(
            search_text=person.build_search_text(),
            called_name=first_called.given_name if first_called else "",
        )


# ---------------------------------------------------------------------------
# Гэр бүл (хос)
# ---------------------------------------------------------------------------


class Family(models.Model):
    """Хос/гэр бүл (GEDCOM FAM). Ижил хүйсийн болон бүртгэлгүй хосын хувьд
    тусгай талбар шаардлагагүй — зүгээр л хоёр partner. Олон дараалсан гэрлэлт
    нь нэг хүн хэд хэдэн Family-д орохыг хэлнэ."""

    class FamilyType(models.TextChoices):
        MARRIAGE = "marriage", "Гэрлэлт"
        PARTNERSHIP = "partnership", "Хамтын амьдрал"
        UNKNOWN = "unknown", "Тодорхойгүй"

    partner1 = models.ForeignKey(
        Person, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="families_as_partner1", verbose_name="Нэгдүгээр хүн",
    )
    partner2 = models.ForeignKey(
        Person, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="families_as_partner2", verbose_name="Хоёрдугаар хүн",
    )
    family_type = models.CharField("Төрөл", max_length=20, choices=FamilyType.choices, default=FamilyType.MARRIAGE)
    marriage_date = models.ForeignKey(
        FlexDate, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
        verbose_name="Гэрлэсэн огноо",
    )
    marriage_place = models.ForeignKey(
        Place, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
        verbose_name="Гэрлэсэн газар",
    )
    divorce_date = models.ForeignKey(
        FlexDate, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
        verbose_name="Салсан огноо",
    )
    notes = models.TextField("Тэмдэглэл", blank=True)
    gedcom_xref = models.CharField("GEDCOM ID", max_length=20, blank=True, null=True, unique=True)
    citations = GenericRelation("genealogy.Citation")
    media_links = GenericRelation("genealogy.MediaLink")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "genealogy_family"
        verbose_name = "Гэр бүл"
        verbose_name_plural = "Гэр бүлүүд"
        indexes = [
            models.Index(fields=["partner1"], name="family_partner1_idx"),
            models.Index(fields=["partner2"], name="family_partner2_idx"),
        ]

    def __str__(self):
        a, b = self.partner1, self.partner2
        if a and b:
            return f"{a.display_name} & {b.display_name}"
        return (a or b).display_name if (a or b) else "Хос (тодорхойгүй)"

    @property
    def partners(self):
        return [p for p in (self.partner1, self.partner2) if p]

    def can_edit_by(self, user):
        """Гэр бүлийг засах эрхтэй эсэх.

        Гэр бүл нь хэд хэдэн хүнийг холбодог тул оролцогч БҮР (partner ба
        хүүхэд) засах эрхтэй байх ёстой — эс бөгөөс бусдын хүнийг салгаж,
        сольж болно. Хоосон гэр бүлд `all([])` нь True буцаана — хор хөнөөлгүй
        (өөрчлөх зүйл нь зөвхөн засах эрхтэй хүн рүү заах partner үүр).
        """
        members = list(self.partners) + [link.child for link in self.children.all()]
        return all(p.can_edit_by(user) for p in members)

    def clean(self):
        if self.partner1_id and self.partner1_id == self.partner2_id:
            raise ValidationError("Нэг хүн хоёр талд байж болохгүй.")


class ChildLink(models.Model):
    """Хүүхэд ↔ гэр бүл (GEDCOM FAMC/CHIL/PEDI).

    Нэг хүүхэд хэд хэдэн гэр бүлд байж болно (төрсөн + өргөж авсан), гэхдээ
    зөвхөн НЭГ төрсөн (BIRTH) гэр бүлтэй байна — үүнийг constraint-аар батална.
    Эцэг эх нь танигдаагүй бол Family-ийн partner тал нь NULL байна.
    """

    class Pedigree(models.TextChoices):
        BIRTH = "birth", "Төрсөн"
        ADOPTED = "adopted", "Өргөж авсан"
        FOSTER = "foster", "Асран хүмүүжүүлсэн"
        STEP = "step", "Хойд эцэг/эх"
        UNKNOWN = "unknown", "Тодорхойгүй"

    # Дэлгэц дээрх шошго — төрөл бүрийг ялгаж харуулна.
    PARENT_LABELS = {
        "birth": "Эцэг эх",
        "adopted": "Өргөмөл эцэг эх",
        "foster": "Асран хүмүүжүүлсэн эцэг эх",
        "step": "Хойд эцэг эх",
        "unknown": "Эцэг эх (тодорхойгүй)",
    }
    CHILD_LABELS = {
        "birth": "Хүүхдүүд",
        "adopted": "Өргөмөл хүүхдүүд",
        "foster": "Асран хүмүүжүүлсэн хүүхдүүд",
        "step": "Хойд хүүхдүүд",
        "unknown": "Хүүхдүүд (тодорхойгүй)",
    }

    family = models.ForeignKey(Family, on_delete=models.CASCADE, related_name="children", verbose_name="Гэр бүл")
    child = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="parent_links", verbose_name="Хүүхэд")
    pedigree = models.CharField("Холбооны төрөл", max_length=20, choices=Pedigree.choices, default=Pedigree.BIRTH)
    child_order = models.PositiveSmallIntegerField("Дараалал", default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "genealogy_childlink"
        verbose_name = "Хүүхдийн холбоо"
        verbose_name_plural = "Хүүхдийн холбоонууд"
        ordering = ["child_order", "id"]
        constraints = [
            models.UniqueConstraint(fields=["family", "child"], name="childlink_unique_family_child"),
            # Нэг хүүхэд зөвхөн НЭГ төрсөн гэр бүлтэй байна.
            models.UniqueConstraint(
                fields=["child"], condition=Q(pedigree="birth"), name="childlink_one_birth_family",
            ),
        ]

    def __str__(self):
        return f"{self.child} ← {self.family}"

    @property
    def parent_label(self):
        return self.PARENT_LABELS.get(self.pedigree, "Эцэг эх")

    @property
    def child_label(self):
        return self.CHILD_LABELS.get(self.pedigree, "Хүүхдүүд")

    @property
    def is_birth(self):
        return self.pedigree == self.Pedigree.BIRTH

    def clean(self):
        """Эцэг эх ↔ хүүхэд хоорондын циклийг хориглоно (A → B → A)."""
        if self.pk is None and self.child_id and self.family_id:
            from .services import would_create_cycle

            if would_create_cycle(child=self.child, family=self.family):
                raise ValidationError(
                    "Энэ холбоо цикл үүсгэнэ: хүн өөрийн үр хойшсын эцэг эх болохгүй."
                )
        super().clean()


# ---------------------------------------------------------------------------
# Үйл явдал
# ---------------------------------------------------------------------------


class Event(models.Model):
    """Үйл явдал (GEDCOM INDI/FAM event)."""

    class EventType(models.TextChoices):
        BIRTH = "BIRT", "Төрсөн"
        DEATH = "DEAT", "Нас барсан"
        BURIAL = "BURI", "Оршуулсан"
        MARRIAGE = "MARR", "Гэрлэсэн"
        DIVORCE = "DIV", "Салсан"
        BAPTISM = "BAPM", "Баптисм"
        CENSUS = "CENS", "Тооллого"
        OCCUPATION = "OCCU", "Ажил/мэргэжил"
        RESIDENCE = "RESI", "Оршин суусан"
        EDUCATION = "EDUC", "Боловсрол"
        EMIGRATION = "EMIG", "Гадаа явсан"
        IMMIGRATION = "IMMI", "Ирсэн"
        MILITARY = "MILI", "Цэргийн алба"
        WILL = "WILL", "Гэрээслэл"
        OTHER = "EVEN", "Бусад"

    event_type = models.CharField("Төрөл", max_length=10, choices=EventType.choices)
    date = models.ForeignKey(
        FlexDate, on_delete=models.SET_NULL, null=True, blank=True, related_name="+", verbose_name="Огноо"
    )
    place = models.ForeignKey(
        Place, on_delete=models.SET_NULL, null=True, blank=True, related_name="events", verbose_name="Газар"
    )
    family = models.ForeignKey(
        Family, on_delete=models.CASCADE, null=True, blank=True, related_name="events",
        verbose_name="Холбоотой гэр бүл",
    )
    description = models.TextField("Тайлбар", blank=True)
    gedcom_xref = models.CharField("GEDCOM ID", max_length=20, blank=True, null=True, unique=True)
    citations = GenericRelation("genealogy.Citation")
    media_links = GenericRelation("genealogy.MediaLink")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "genealogy_event"
        verbose_name = "Үйл явдал"
        verbose_name_plural = "Үйл явдлууд"
        ordering = ["-date__sort_key", "pk"]
        indexes = [models.Index(fields=["event_type"], name="event_type_idx")]

    def __str__(self):
        when = self.date.display if self.date else "?"
        return f"{self.get_event_type_display()} · {when}"

    @property
    def is_death(self):
        return self.event_type == self.EventType.DEATH


class EventParticipant(models.Model):
    """Үйл явдлын оролцогч (GEDCOM-д үйл явдал нь хүмүүстэй олон-олон)."""

    class Role(models.TextChoices):
        PRINCIPAL = "principal", "Гол оролцогч"
        SPOUSE = "spouse", "Эхнэр/нөхөр"
        PARENT = "parent", "Эцэг/эх"
        CHILD = "child", "Хүүхэд"
        WITNESS = "witness", "Гэрч"
        OFFICIANT = "officiant", "Хурим хийсэн"

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="participants", verbose_name="Үйл явдал")
    person = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="event_participations", verbose_name="Хүн")
    role = models.CharField("Үүрэг", max_length=20, choices=Role.choices, default=Role.PRINCIPAL)
    note = models.CharField("Тайлбар", max_length=200, blank=True)

    class Meta:
        db_table = "genealogy_eventparticipant"
        verbose_name = "Үйл явдлын оролцогч"
        verbose_name_plural = "Үйл явдлын оролцогчид"
        constraints = [
            models.UniqueConstraint(fields=["event", "person", "role"], name="eventparticipant_unique"),
        ]

    def __str__(self):
        return f"{self.person} ({self.get_role_display()})"


# ---------------------------------------------------------------------------
# Эх сурвалж ба баримт
# ---------------------------------------------------------------------------


class Source(models.Model):
    """Эх сурвалж: архив бичиг, ном, аман түүх, гэрчилгээ, вэб."""

    class SourceType(models.TextChoices):
        ARCHIVE = "archive", "Архивын бичиг"
        CERTIFICATE = "certificate", "Гэрчилгээ"
        REGISTER = "register", "Бүртгэлийн дэвтэр"
        INTERVIEW = "interview", "Аман түүх / ярилцлага"
        PHOTO = "photo", "Гэрэл зураг"
        BOOK = "book", "Ном, хэвлэл"
        WEBSITE = "website", "Вэб сайт"
        OTHER = "other", "Бусад"

    source_type = models.CharField("Төрөл", max_length=20, choices=SourceType.choices, default=SourceType.OTHER)
    title = models.CharField("Гарчиг", max_length=300)
    author = models.CharField("Зохиогч/бүртгэсэн", max_length=200, blank=True)
    repository = models.CharField("Хадгалагдаж буй газар", max_length=200, blank=True)
    publication = models.CharField("Хэвлэл/дугаар", max_length=200, blank=True)
    url = models.URLField("Холбоос", blank=True)
    notes = models.TextField("Тэмдэглэл", blank=True)
    gedcom_raw = models.CharField("GEDCOM эх", max_length=300, blank=True)
    citations = GenericRelation("genealogy.Citation")
    media_links = GenericRelation("genealogy.MediaLink")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "genealogy_source"
        verbose_name = "Эх сурвалж"
        verbose_name_plural = "Эх сурвалжууд"
        ordering = ["title"]

    def __str__(self):
        return self.title


class Citation(models.Model):
    """Эх сурвалжийн ишлэл — "энэ мэдээлэл хаанаас?" гэдгийг баримтална."""

    class Quality(models.TextChoices):
        PRIMARY = "3", "Үндсэн (шугмын бичиг)"
        SECONDARY = "2", "Хоёрдогч"
        UNCERTAIN = "1", "Тодорхойгүй"
        UNRELIABLE = "0", "Найдваргүй"

    source = models.ForeignKey(Source, on_delete=models.CASCADE, related_name="citations", verbose_name="Эх сурвалж")
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveBigIntegerField()
    content_object = GenericForeignKey("content_type", "object_id")
    page = models.CharField("Хуудас/байршил", max_length=100, blank=True)
    quality = models.CharField("Найдвартай байдал", max_length=1, choices=Quality.choices, default=Quality.SECONDARY)
    note = models.TextField("Тайлбар", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "genealogy_citation"
        verbose_name = "Ишлэл"
        verbose_name_plural = "Ишлэлүүд"
        indexes = [models.Index(fields=["content_type", "object_id"], name="citation_target_idx")]

    def __str__(self):
        return f"{self.source} → {self.content_object}"


class Media(models.Model):
    """Зураг, баримт бичгийн файл."""

    class Kind(models.TextChoices):
        PHOTO = "photo", "Гэрэл зураг"
        DOCUMENT = "document", "Баримт бичиг"
        AUDIO = "audio", "Дуу бичлэг"
        VIDEO = "video", "Видео"

    kind = models.CharField("Төрөл", max_length=20, choices=Kind.choices, default=Kind.PHOTO)
    file = models.FileField("Файл", upload_to="uploads/%Y/%m/")
    thumbnail = models.ImageField("Жижиг зураг", upload_to="thumbs/%Y/%m/", blank=True, null=True)
    title = models.CharField("Гарчиг", max_length=200, blank=True)
    caption = models.TextField("Тайлбар", blank=True)
    date = models.ForeignKey(
        FlexDate, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
        verbose_name="Огноо",
    )
    source = models.ForeignKey(
        Source, on_delete=models.SET_NULL, null=True, blank=True, related_name="media",
        verbose_name="Эх сурвалж",
    )
    mime_type = models.CharField("MIME", max_length=100, blank=True)
    file_size = models.PositiveBigIntegerField("Хэмжээ (байт)", default=0)
    width = models.PositiveIntegerField("Өргөн", null=True, blank=True)
    height = models.PositiveIntegerField("Өндөр", null=True, blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="media_uploaded", verbose_name="Оруулсан",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "genealogy_media"
        verbose_name = "Медиа"
        verbose_name_plural = "Медиа"
        ordering = ["-created_at"]

    def __str__(self):
        return self.title or (self.file.name if self.file else f"Медиа #{self.pk}")

    @property
    def is_image(self):
        return bool(self.mime_type and self.mime_type.startswith("image/"))

    def save(self, *args, **kwargs):
        if self.file and not self.file_size:
            try:
                self.file_size = self.file.size
            except (OSError, ValueError):
                pass
        super().save(*args, **kwargs)


class MediaLink(models.Model):
    """Медиа ↔ (Хүн | Үйл явдал | Эх сурвалж)."""

    media = models.ForeignKey(Media, on_delete=models.CASCADE, related_name="links", verbose_name="Медиа")
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveBigIntegerField()
    content_object = GenericForeignKey("content_type", "object_id")
    is_primary = models.BooleanField("Үндсэн зураг", default=False)
    note = models.CharField("Тайлбар", max_length=200, blank=True)

    class Meta:
        db_table = "genealogy_medialink"
        verbose_name = "Медиа холбоос"
        verbose_name_plural = "Медиа холбоосууд"
        constraints = [
            models.UniqueConstraint(fields=["media", "content_type", "object_id"], name="medialink_unique"),
        ]
        indexes = [models.Index(fields=["content_type", "object_id"], name="medialink_target_idx")]

    def __str__(self):
        return f"{self.media} → {self.content_object}"


# ---------------------------------------------------------------------------
# Аудит
# ---------------------------------------------------------------------------


class AuditLog(models.Model):
    """Хэн, хэзээ, юуг өөрчилсөн — энэ өгөгдөл нөхөгдөхгүй тул буцаах боломж чухал."""

    class Action(models.TextChoices):
        CREATE = "create", "Үүсгэсэн"
        UPDATE = "update", "Засварласан"
        DELETE = "delete", "Устгасан"
        MERGE = "merge", "Нэгтгэсэн"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="audit_logs", verbose_name="Хэрэглэгч",
    )
    action = models.CharField("Үйлдэл", max_length=10, choices=Action.choices)
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE, null=True, blank=True)
    object_id = models.PositiveBigIntegerField(null=True, blank=True)
    object_repr = models.CharField("Объект", max_length=300, blank=True)
    changes = models.JSONField("Өөрчлөлт", default=dict, blank=True)
    ip_address = models.GenericIPAddressField("IP", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "genealogy_auditlog"
        verbose_name = "Аудит бүртгэл"
        verbose_name_plural = "Аудит бүртгэлүүд"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["content_type", "object_id"], name="audit_target_idx")]

    def __str__(self):
        return f"{self.get_action_display()} · {self.object_repr} · {self.created_at:%Y-%m-%d %H:%M}"
