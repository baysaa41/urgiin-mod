"""Template-д хэрэгтэй жижиг туслахууд."""

# Эдгээр нэртэй талбарууд нь Place руу заасан FK — форм дээр «+ Газар»
# товчтойгоор зурагдана (partials/_form_fields.html-ийг харна уу).
PLACE_FIELDS = ("birth_place", "death_place", "marriage_place", "place")


def genealogy(request):
    return {"place_field_names": PLACE_FIELDS}
