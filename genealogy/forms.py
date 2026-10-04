"""Формууд.

Гол онцлог: `FlexDateField` нь хэрэглэгчийн бичсэн чөлөөт огноог
("ойролцоогоор 1920", "1920-04", "?") шууд FlexDate болгож хөрвүүлнэ.
"""

from datetime import date

from django import forms

from .models import (ChildLink, Event, Family, FlexDate, Media, Person, PersonName,
                     Place, Source)


def pedigree_choices():
    """Холбооны төрөл — «тодорхойгүй» нь сонголтод орохгүй (админ-д бий)."""
    return [
        (ChildLink.Pedigree.BIRTH, "Төрсөн"),
        (ChildLink.Pedigree.ADOPTED, "Өргөмөл"),
        (ChildLink.Pedigree.FOSTER, "Асран хүмүүжүүлсэн"),
        (ChildLink.Pedigree.STEP, "Хойд эцэг/эх"),
    ]


PEDIGREE_HELP = ("«Төрсөн» холбоо нэг л байж болно; өргөмөл, асран хүмүүжүүлсэн, "
                 "хойд холбоо нэмэлтээр оршино.")


class FlexDateField(forms.CharField):
    """Чөлөөт текст огноо → FlexDate объект.

    Хэрэв текст өөрчлөгдөөгүй бол одоо байгаа бичлэгийг дахин ашиглана
    (хэрэглэгч зөвхөн бусад талбарыг засах үед шинэ мөр үүсэхгүй).
    """

    default_error_messages = {"invalid": "Огноог таньж чадсангүй: %(value)s"}

    def __init__(self, *args, instance=None, **kwargs):
        self.instance = instance
        kwargs.setdefault("required", False)
        kwargs.setdefault("max_length", 100)
        # Жишээ бичихгүй — огноо нь он төдий бичигддэг тул урт заавар
        # шаардлагагүй. «Ойролцоогоор» чекийн зааврыг форм тус бүр тавина
        # (`PersonForm`), эс бөгөөс чекгүй форм дээр байхгүй чекийг заана.
        kwargs.setdefault("help_text", "")
        super().__init__(*args, **kwargs)

    def prepare_value(self, value):
        """Форм дээр ХҮНИЙ БИЧСЭН ТЕКСТ харагдах ёстой, pk дугаар биш.

        ModelForm нь FK-ийн `initial`-ыг объект биш pk-ээр өгдөг тул
        pk-г мөн FlexDate болгож шийднэ — эс бөгөөс талбарт «20» гэх мэт
        дотоод ID харагдаж, хадгалахад нь тэр нь огноо болж хадгалагдана.
        """
        if isinstance(value, FlexDate):
            return value.raw
        if value not in (None, ""):
            obj = FlexDate.objects.filter(pk=value).first()
            if obj is not None:
                return obj.raw
        return super().prepare_value(value)

    def clean(self, value):
        value = (super().clean(value) or "").strip()
        if not value:
            return None
        if self.instance is not None and self.instance.raw == value:
            return self.instance
        parsed = FlexDate.parse(value)
        if parsed.qualifier == FlexDate.Qualifier.UNKNOWN and value not in {"?", "-", "—"}:
            # Танихгүй ч гэсэн хадгална — гэхдээ хэрэглэгчид анхааруулна.
            self.help_text = "Энэ огноог машин уншиж чадсангүй — текстээр хадгаллаа."

        # Ижил утгатай бичлэг байвал ДАХИН АШИГЛАНА. Үгүй бол форм бүр
        # (буруу бөглөсөн ч) шинэ мөр үүсгээд, санд хэрэггүй огноо хуримтлагдана.
        existing = FlexDate.objects.filter(
            raw=parsed.raw, qualifier=parsed.qualifier, precision=parsed.precision,
            date_from=parsed.date_from, date_to=parsed.date_to,
        ).first()
        if existing is not None:
            return existing
        parsed.save()
        return parsed


class BootstrapFormMixin:
    """Бүх виджетэд Bootstrap класс автоматаар өгнө."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            widget = field.widget
            if isinstance(widget, forms.CheckboxInput):
                widget.attrs.setdefault("class", "form-check-input")
            elif isinstance(widget, (forms.Select, forms.SelectMultiple)):
                widget.attrs.setdefault("class", "form-select")
            else:
                widget.attrs.setdefault("class", "form-control")


# Огнооны талбарын заавар. Жишээ бичихгүй — огноо нь он төдий бичигддэг тул
# хэрэглэгчид урт жишээ шаардлагагүй; харин ойролцоо бол доорх чекийг
# чагтлах ёстойгоо хэлэх нь чухал.
_APPROX_HINT = "Ойролцоо бол доорх «Ойролцоогоор»-ыг чагтална уу."


def _dated_fields(form, instance, names):
    """Тухайн моделийн FlexDate талбаруудыг FlexDateField-ээр солино."""
    for name, label in names:
        current = getattr(instance, name, None) if instance is not None else None
        form.fields[name] = FlexDateField(instance=current, label=label, required=False)
        if current is not None and hasattr(form, "initial"):
            # ModelForm-ийн анхдагч нь pk байдаг — объектоор дарж бичнэ.
            form.initial[name] = current


def _strip_approx_word(raw):
    """«ойролцоогоор 1950» → «1950». Чек авахад угтвар үг илүүц болно."""
    text = (raw or "").strip()
    lowered = text.lower()
    for word in ("ойролцоогоор", "ойролцоо", "тооцоолсон", "approx", "abt", "est"):
        if lowered.startswith(word):
            return text[len(word):].strip(" :~?") or text
    return text


class PersonForm(BootstrapFormMixin, forms.ModelForm):
    # Огноо бүрийн АРАД харагдах «ойролцоогоор» чек. `clean()` нь чагтлагдсан
    # үед тухайн FlexDate-ийн `qualifier`-ыг ABOUT болгоно — `display` нь
    # «ойролцоогоор 1950» гэж харуулна (үгийг `raw`-д бичих шаардлагагүй).
    birth_date_approx = forms.BooleanField(label="Ойролцоогоор", required=False)
    death_date_approx = forms.BooleanField(label="Ойролцоогоор", required=False)

    class Meta:
        model = Person
        # АНХААР: FlexDate талбарууд энд ЗААВАЛ байх ёстой. `construct_instance`
        # нь зөвхөн энэ жагсаалтад байгаа талбарыг объект руу хуулдаг тул
        # оруулаагүй бол форм дээр бөглөсөн огноо чимээгүй хаягдана.
        fields = [
            "clan_name", "patronymic", "surname", "given_name", "called_name", "sex",
            "birth_date", "death_date",
            "birth_place", "death_place", "living_override", "visibility", "bio",
        ]
        widgets = {
            "bio": forms.Textarea(attrs={"rows": 4}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _dated_fields(self, self.instance, [
            ("birth_date", "Төрсөн он"),
            ("death_date", "Нас барсан он"),
        ])
        # Огноо нь он төдий бичигддэг тул ойролцоо эсэхийг доорх чекоор
        # тэмдэглэнэ — талбар бүрд заавар тавина.
        for name in ("birth_date", "death_date"):
            if name in self.fields:
                self.fields[name].help_text = _APPROX_HINT
        for name in ("birth_place", "death_place"):
            if name in self.fields:  # QuickAddPersonForm эдгээрийг хасдаг
                self.fields[name].widget.attrs.update({"class": "form-select"})
                self.fields[name].queryset = Place.objects.all()
        self._setup_approx_flags()
        self._suggest_clan_name()

    def _suggest_clan_name(self):
        """Ургийн овог хоосон бол эцгийнхээ овгийг САНАЛ БОЛГОНО.

        Зөвхөн урьдчилан бөглөнө — автоматаар хадгалахгүй. Хэрэглэгч талбарыг
        харж, хүсвэл сольж болно (help_text нь хаанаас ирснийг хэлнэ).
        """
        field = self.fields.get("clan_name")
        if field is None or self.instance.clan_name or not self.instance.pk:
            return
        father = self.instance.father
        if father is None or not father.clan_name:
            return
        # ModelForm дээр `field.initial`-ыг үл харна: `self.initial` нь
        # `model_to_dict(instance)`-ээр дүүрсэн байдаг тул тэнд бичнэ.
        self.initial["clan_name"] = father.clan_name
        field.help_text = (f"Эцэг {father.display_name}-ийн ургийн овог: "
                           f"{father.clan_name}. Хүсвэл сольж болно.")

    def _setup_approx_flags(self):
        """«…_approx» чекүүдийг огнооны талбарын ЯГ АРАД байрлуулна.

        Class дээр зарласан талбарууд нь `Meta.fields`-ийн дараа нэмэгддэг тул
        гараар эрэмбэлнэ. Суурь огнооны талбар нь байхгүй бол (жишээ нь
        `QuickAddPersonForm` `death_date`-ыг хасдаг) чек нь ч хасагдана —
        эс бөгөөс хамааралгүй чек ганцаараа харагдана.
        """
        ordered = {}
        for name in list(self.fields):
            if name.endswith("_approx"):
                continue
            ordered[name] = self.fields[name]
            flag = f"{name}_approx"
            if flag in self.fields:
                ordered[flag] = self.fields[flag]
        self.fields = ordered

        # Одоо байгаа огнооны qualifier-аас чекийн анхдагч утгыг авна.
        for name in ("birth_date", "death_date"):
            flag = f"{name}_approx"
            if flag not in self.fields:
                continue
            current = getattr(self.instance, name, None)
            self.fields[flag].initial = bool(
                current is not None and current.qualifier == FlexDate.Qualifier.ABOUT
            )

    def _apply_approx_flags(self, cleaned):
        """Чекийн төлөвөөр FlexDate-ийн qualifier-ыг тааруулна.

        Үргэлж ШИНЭ FlexDate үүсгэнэ — хуучин мөр нь өөр бичлэгтэй
        хуваалцагдсан байж болзошгүй тул түүнийг газар дээр нь өөрчлөх нь
        бусад хүний огноог ч өөрчилнө.
        """
        for name in ("birth_date", "death_date"):
            fd = cleaned.get(name)
            if fd is None:
                continue
            wants = bool(cleaned.get(f"{name}_approx"))
            is_about = fd.qualifier == FlexDate.Qualifier.ABOUT
            if wants == is_about:
                continue
            if not wants and _strip_approx_word(fd.raw) != fd.raw:
                # Текст өөрөө «ойролцоогоор …» гэж бичсэн (help_text-д байгаа
                # хэлбэр) — чек нь зөвхөн НЭМНЭ, бичсэн үгийг устгахгүй.
                continue
            new = FlexDate(
                raw=fd.raw if wants else _strip_approx_word(fd.raw),
                qualifier=FlexDate.Qualifier.ABOUT if wants else FlexDate.Qualifier.EXACT,
                precision=fd.precision, date_from=fd.date_from, date_to=fd.date_to,
            )
            new.save()          # sort_key-г тооцно
            cleaned[name] = new

    def save(self, commit=True):
        person = super().save(commit=commit)
        if commit:
            self._sync_called_personname(person)
        return person

    @staticmethod
    def _sync_called_personname(person):
        """`called_name` талбарыг `CALLED` төрлийн `PersonName`-тай тааруулна.

        Мэдээлэл хоёр газар (энгийн талбар ба бүтэцтэй нэр) хадгалагддаг тул
        формээр засахад бүтэцтэй бичлэг нь ч шинэчлэгдэх ёстой. Урвуу чиглэл
        (admin-аас нэр нэмэх) нь `PersonName.sync_person()`-оор ажиллана.
        """
        wanted = (person.called_name or "").strip()
        existing = list(PersonName.objects.filter(
            person=person, name_type=PersonName.NameType.CALLED).order_by("pk"))

        if not wanted:
            for name in existing:
                name.delete()
            return
        if existing:
            first, rest = existing[0], existing[1:]
            if first.given_name != wanted:
                first.given_name = wanted
                first.save()
            for name in rest:
                name.delete()
        else:
            PersonName.objects.create(person=person,
                                      name_type=PersonName.NameType.CALLED,
                                      given_name=wanted)

    def clean(self):
        cleaned = super().clean()
        birth, death = cleaned.get("birth_date"), cleaned.get("death_date")
        # sort_key нь тодорхойгүй огнооны хувьд date.min — тэдгээрийг харьцуулахгүй.
        if birth and death and birth.sort_key > date.min and death.sort_key > date.min:
            if death.sort_key < birth.sort_key:
                self.add_error("death_date", "Нас барсан огноо төрсөн огнооноос өмнө байж болохгүй.")
        self._apply_approx_flags(cleaned)
        return cleaned


class FamilyForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = Family
        fields = ["partner1", "partner2", "family_type",
                  "marriage_date", "divorce_date", "marriage_place", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        _dated_fields(self, self.instance, [
            ("marriage_date", "Гэрлэсэн огноо"),
            ("divorce_date", "Салсан огноо"),
        ])
        # Зөвхөн засах эрхтэй хүмүүсийг сонголтод оруулна — эс бөгөөс засварлагч
        # бусдын хүнийг гэр бүлд холбож/сольж чадна. View нь тухайн гэр бүлийн
        # одоогийн partner-уудыг ч заавал editable байхаар шүүдэг тул одоогийн
        # утга нь үргэлж энэ queryset-д багтана.
        # `user` дамжуулаагүй бол хоосон — эрхийн формд fail-closed байх нь зөв.
        for name in ("partner1", "partner2"):
            self.fields[name].queryset = Person.objects.editable_by(user)
            self.fields[name].required = False

    def clean(self):
        cleaned = super().clean()
        p1, p2 = cleaned.get("partner1"), cleaned.get("partner2")
        if p1 and p2 and p1.pk == p2.pk:
            raise forms.ValidationError("Нэг хүн хоёр талд байж болохгүй.")
        return cleaned


class EventForm(BootstrapFormMixin, forms.ModelForm):
    participants = forms.ModelMultipleChoiceField(
        queryset=Person.objects.alive(), required=False, label="Оролцогчид",
        widget=forms.SelectMultiple(attrs={"class": "form-select", "size": 8}),
    )

    class Meta:
        model = Event
        fields = ["event_type", "date", "place", "family", "description"]
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _dated_fields(self, self.instance, [("date", "Огноо")])
        if self.instance.pk:
            self.fields["participants"].initial = [
                p.person_id for p in self.instance.participants.all()
            ]

    def save(self, commit=True):
        event = super().save(commit=commit)
        if commit:
            self._sync_participants(event)
        return event

    def _sync_participants(self, event):
        from .models import EventParticipant

        chosen = {p.pk for p in self.cleaned_data.get("participants", [])}
        existing = {p.person_id: p for p in event.participants.all()}
        for person_id in chosen - set(existing):
            EventParticipant.objects.create(event=event, person_id=person_id)
        for person_id in set(existing) - chosen:
            existing[person_id].delete()


class SourceForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = Source
        fields = ["source_type", "title", "author", "repository", "publication", "url", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}


class MediaForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = Media
        fields = ["kind", "file", "title", "caption", "date", "source"]
        widgets = {"caption": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _dated_fields(self, self.instance, [("date", "Огноо")])


class QuickAddPersonForm(PersonForm):
    """Модноос хамаатан хурдан нэмэх форм (зөвхөн зайлшгүй талбарууд)."""

    relation = forms.ChoiceField(
        label="Хэн болох",
        choices=[
            ("father", "Эцэг"), ("mother", "Эх"), ("partner", "Эхнэр/нөхөр"),
            ("child", "Хүүхэд"), ("sibling", "Ах/эгч/дүү"),
        ],
    )

    class Meta(PersonForm.Meta):
        # birth_date нь `_dated_fields`-ээр нэмэгддэг ч Meta.fields-д БАЙХ ёстой —
        # эс бөгөөс form.save() хийхэд огноо чимээгүй хаягдана (PersonForm дээр
        # яг ийм алдаа гарч байсан). Одоогийн view нь cleaned_data-г шууд
        # уншдаг тул ажиллаж байсан ч дараа нь .save() дуудвал эвдэрнэ.
        fields = ["given_name", "patronymic", "clan_name", "sex", "birth_date"]

    def __init__(self, *args, anchor=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["given_name"].required = True
        # Хүйс нь тодорхойгүй байх нь хэвийн — заавал бөглүүлэхгүй.
        self.fields["sex"].required = False
        self.fields["pedigree"] = forms.ChoiceField(
            label="Холбооны төрөл", choices=pedigree_choices(), required=False,
            initial=ChildLink.Pedigree.BIRTH, help_text=PEDIGREE_HELP,
        )
        # Quick-add-д эдгээр хэрэггүй.
        for name in ("birth_place", "death_place", "living_override", "visibility", "bio",
                     "surname", "death_date"):
            self.fields.pop(name, None)
        # Хасагдсан огнооны «ойролцоогоор» чек ч хамт явах ёстой — эс бөгөөс
        # хамааралгүй чек ганцаараа харагдана.
        self._setup_approx_flags()
        self._suggest_clan_from_anchor(anchor)

    def _suggest_clan_from_anchor(self, anchor):
        """Шинэ хамаатны эцгийн овгийг санал болгоно (автоматаар бичихгүй)."""
        from .services import suggested_clan_name

        field = self.fields.get("clan_name")
        if field is None or anchor is None:
            return
        relation = (self.data.get("relation") if self.is_bound
                    else self.initial.get("relation"))
        suggestion = suggested_clan_name(anchor, relation)
        if suggestion:
            # ModelForm дээр `field.initial`-ыг үл харна — `self.initial`-д бичнэ.
            self.initial["clan_name"] = suggestion
            field.help_text = (f"Эцгийн ургийн овог: {suggestion}. "
                               "Хүсвэл сольж болно.")


class LinkRelativeForm(BootstrapFormMixin, forms.Form):
    """БАЙГАА хүнийг хамаатан болгож холбох.

    `person` талбарыг энэ форм өөрөө зурахгүй — хайлтын үр дүнгийн радио
    товчнууд (`name="person"`) утгыг өгнө (partials/_link_modal.html).
    """

    relation = forms.ChoiceField(
        label="Хэн болох",
        choices=[
            ("father", "Эцэг"), ("mother", "Эх"), ("partner", "Эхнэр/нөхөр"),
            ("child", "Хүүхэд"), ("sibling", "Ах/эгч/дүү"),
        ],
    )
    # Сонгохгүй бол «төрсөн» гэж үзнэ (view: `data.get("pedigree") or "birth"`).
    pedigree = forms.ChoiceField(
        label="Холбооны төрөл", choices=pedigree_choices(), required=False,
        initial=ChildLink.Pedigree.BIRTH, help_text=PEDIGREE_HELP,
    )
    person = forms.ModelChoiceField(
        label="Хүн", queryset=Person.objects.none(), widget=forms.HiddenInput,
        error_messages={"required": "Доорх жагсаалтаас хүнээ сонгоно уу.",
                        "invalid_choice": "Сонгосон хүн олдсонгүй."},
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        # Зөвхөн засах эрхтэй хүмүүсийг холбож болно — холбох нь ТУХАЙН
        # хүний удмын байрлалыг өөрчилдөг тул түүнд ч эрх хэрэгтэй.
        # Picker UI мөн адил шүүдэг (`person_picker`), энэ нь сервер талын
        # баталгаа: гараар POST хийсэн тохиолдолд `invalid_choice` алдаа өгнө.
        # `user` дамжуулаагүй бол хоосон — fail-closed.
        self.fields["person"].queryset = Person.objects.editable_by(user)


class PlaceForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = Place
        fields = ["name", "place_type", "parent", "latitude", "longitude"]


# ---------------------------------------------------------------------------
# Admin-д зориулсан форм
# ---------------------------------------------------------------------------


class FlexDateAdminMixin:
    """Admin-д огноог чөлөөт текстээр оруулах боломж (FlexDate autocomplete
    утгагүй олон мөр үүсгэдэг тул)."""

    flex_date_fields = ()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        instance = self.instance if getattr(self.instance, "pk", None) else None
        for name in self.flex_date_fields:
            if name not in self.fields:
                continue
            current = getattr(instance, name, None) if instance else None
            label = self.fields[name].label
            self.fields[name] = FlexDateField(instance=current, label=label, required=False)
            if current is not None:
                self.initial[name] = current  # pk биш, объект


class AdminPersonForm(FlexDateAdminMixin, forms.ModelForm):
    flex_date_fields = ("birth_date", "death_date")

    class Meta:
        model = Person
        fields = "__all__"


class AdminFamilyForm(FlexDateAdminMixin, forms.ModelForm):
    flex_date_fields = ("marriage_date", "divorce_date")

    class Meta:
        model = Family
        fields = "__all__"


class AdminEventForm(FlexDateAdminMixin, forms.ModelForm):
    flex_date_fields = ("date",)

    class Meta:
        model = Event
        fields = "__all__"
