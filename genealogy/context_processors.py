"""Template-д хэрэгтэй жижиг туслахууд."""

# Эдгээр нэртэй талбарууд нь Place руу заасан FK — форм дээр «+ Газар»
# товчтойгоор зурагдана (partials/_form_fields.html-ийг харна уу).
PLACE_FIELDS = ("birth_place", "death_place", "marriage_place", "place")


def genealogy(request):
    context = {"place_field_names": PLACE_FIELDS}

    # Батлах хүсэлтийн тоолуур (navbar-ийн badge). Зөвхөн засах эрхтэй
    # хүнд — эс бөгөөс хуудас бүрд дэмий query ажиллана.
    user = getattr(request, "user", None)
    if user is not None and getattr(user, "can_edit", False):
        from .models import Person, PersonEditRequest
        context["pending_person_edits_count"] = (
            PersonEditRequest.objects
            .filter(status=PersonEditRequest.Status.PENDING,
                    person__in=Person.objects.editable_by(user))
            .exclude(submitted_by=user)
            .count()
        )
    return context
