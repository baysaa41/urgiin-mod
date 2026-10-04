"""Удмын графикийн тооцоолол ба бизнес логик.

Django ORM-д рекурсив CTE байхгүй тул эдгээрийг `connection.cursor()`-оор
ажиллуулна. Ургийн мод бол DAG (мод биш) — нэг өвөг олон замаар ирж болно,
тиймээс үр дүнг MIN(depth)-ээр нэгтгэж, давталтаас `path` массивaар сэргийлнэ.
"""

from collections import defaultdict

from django.contrib.contenttypes.models import ContentType
from django.db import connection, transaction

from .models import (
    AuditLog,
    ChildLink,
    Family,
    Person,
    PersonName,
)

DEFAULT_MAX_GENERATIONS = 12
CYCLE_SEARCH_LIMIT = 60


# ---------------------------------------------------------------------------
# CTE — өвөг / үр хойчис
# ---------------------------------------------------------------------------


def _rows(sql, params):
    with connection.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


_ANCESTORS_SQL = """
WITH RECURSIVE anc(person_id, depth, path) AS (
    SELECT %(root)s::bigint, 0, ARRAY[%(root)s::bigint]
  UNION ALL
    SELECT par.partner_id, a.depth + 1, a.path || par.partner_id
    FROM anc a
    JOIN genealogy_childlink cl ON cl.child_id = a.person_id
    JOIN LATERAL (
        SELECT f.partner1_id AS partner_id FROM genealogy_family f
         WHERE f.id = cl.family_id AND f.partner1_id IS NOT NULL
        UNION ALL
        SELECT f.partner2_id FROM genealogy_family f
         WHERE f.id = cl.family_id AND f.partner2_id IS NOT NULL
    ) par ON TRUE
    WHERE a.depth < %(max_depth)s
      AND NOT (par.partner_id = ANY(a.path))
      AND (NOT %(bloodline)s OR cl.pedigree = 'birth')
)
-- DISTINCT ON нь `MIN(depth)`-тэй ижил мөрийг сонгоно — гэхдээ `path`-ыг
-- ч хамт буцаана (замын «А/Э» код бодоход хэрэгтэй).
SELECT DISTINCT ON (person_id) person_id, depth, path FROM anc
 WHERE person_id <> %(root)s
 ORDER BY person_id, depth
"""

_DESCENDANTS_SQL = """
WITH RECURSIVE des(person_id, depth, path) AS (
    SELECT %(root)s::bigint, 0, ARRAY[%(root)s::bigint]
  UNION ALL
    SELECT cl.child_id, d.depth + 1, d.path || cl.child_id
    FROM des d
    JOIN genealogy_family f
      ON f.partner1_id = d.person_id OR f.partner2_id = d.person_id
    JOIN genealogy_childlink cl ON cl.family_id = f.id
    WHERE d.depth < %(max_depth)s
      AND NOT (cl.child_id = ANY(d.path))
      AND (NOT %(bloodline)s OR cl.pedigree = 'birth')
)
-- DISTINCT ON нь `MIN(depth)`-тэй ижил мөрийг сонгоно — гэхдээ `path`-ыг
-- ч хамт буцаана (замын «Х/О» код бодоход хэрэгтэй).
SELECT DISTINCT ON (person_id) person_id, depth, path FROM des
 WHERE person_id <> %(root)s
 ORDER BY person_id, depth
"""


def ancestors(person, max_depth=DEFAULT_MAX_GENERATIONS, bloodline_only=False):
    """{person_id: depth} — өвөг дээшээ. Давхардсан өвөг нэг л удаа, хамгийн ойр замаар.

    `bloodline_only=True` бол зөвхөн ТӨРСӨН холбоогоор (цусан удмын мод),
    эс бөгөөс өргөмөл/асран хүмүүжүүлсэн/хойд эцэг эхийг ч хамруулна.
    """
    return {
        pid: depth
        for pid, depth, _path in _rows(_ANCESTORS_SQL, {
            "root": person.pk, "max_depth": max_depth, "bloodline": bloodline_only,
        })
    }


def ancestor_paths(person, max_depth=DEFAULT_MAX_GENERATIONS, bloodline_only=False):
    """{person_id: [root, …, тухайн хүн]} — root-оос эхэлсэн зам.

    `ancestors()`-тай ЯГ ижил CTE (нэмэлт query биш — зөвхөн `path` баганыг
    буцаана). `depth` нь `len(path) - 1`, учир нь замд root ч багтсан.

    Нэг өвөг хэдэн замаар ирж болно (pedigree collapse) — тэр үед хамгийн
    ойрын замыг л буцаана (`ancestors()`-ийн MIN(depth)-тай нийцнэ).
    """
    return {
        pid: list(path)
        for pid, _depth, path in _rows(_ANCESTORS_SQL, {
            "root": person.pk, "max_depth": max_depth, "bloodline": bloodline_only,
        })
    }


def descendants(person, max_depth=DEFAULT_MAX_GENERATIONS, bloodline_only=False):
    """{person_id: depth} — үр хойш. Мөн адил давхардлыг нэгтгэнэ."""
    return {
        pid: depth
        for pid, depth, _path in _rows(_DESCENDANTS_SQL, {
            "root": person.pk, "max_depth": max_depth, "bloodline": bloodline_only,
        })
    }


def descendant_paths(person, max_depth=DEFAULT_MAX_GENERATIONS, bloodline_only=False):
    """{person_id: [root, …, тухайн хүн]} — үр хойш руу чиглэсэн зам.

    `descendants()`-тай ЯГ ижил CTE (нэмэлт query биш). `ancestor_paths`-тай
    ижил бүтэцтэй тул `build_graph` хоёр горимд нэг кодоор ажиллана.
    """
    return {
        pid: list(path)
        for pid, _depth, path in _rows(_DESCENDANTS_SQL, {
            "root": person.pk, "max_depth": max_depth, "bloodline": bloodline_only,
        })
    }


def ancestor_graph(person, max_depth=DEFAULT_MAX_GENERATIONS):
    """График зурахад хэрэгтэй {person_id: depth} + гэр бүлийн холбоосууд."""
    return ancestors(person, max_depth)


def would_create_cycle(child, family):
    """`child`-ыг энэ гэр бүлийн хүүхэд болговол цикл үүсэх үү (хүн өөрийн өвөг болох)? """
    if child is None or family is None or child.pk is None or family.pk is None:
        return False
    partner_ids = {p.pk for p in family.partners}
    if child.pk in partner_ids:
        return True
    reachable = descendants(child, max_depth=CYCLE_SEARCH_LIMIT)
    return bool(partner_ids & set(reachable))


# ---------------------------------------------------------------------------
# Хамаатан садан
# ---------------------------------------------------------------------------

_DIRECT_UP = {1: "эцэг/эх", 2: "өвөг эцэг/эх", 3: "өндөр өвөг/эмээ",
              4: "элэнц эцэг/эх", 5: "хуланц эцэг/эх", 6: "элэн", 7: "хулан"}
_DIRECT_DOWN = {1: "хүү/охин", 2: "ач/зээ", 3: "жич", 4: "гуч",
                5: "дөч", 6: "тач", 7: "жилийх", 8: "талийх"}

# Модны зангилаанд root-той харьцах хамаатны нэр (хүйсээр ялгасан хэлбэр).
# `_DIRECT_UP`-ээс тусдаа: тэр нь `relationship_between`-ий дээр хүйсгүй
# хэлбэрээр хэрэглэгддэг хэвээр үлдэнэ.
#
# Дараалал нь гарны үеийн мнемоник ёсоор: мөр суга → аав, тохой → өвөг аав,
# бугуй → өндөр өвөг, алга → элэнц эцэг, хурууны дунд үе → хуланц эцэг,
# хурууны өндөг → элэн, хурууны үзүүр → хулан (долоон үе).
# `элэн`/`хулан` нь эх бичвэрт хүйсээр ялгагдаагүй тул хоёр хүйстэд ижил.
_REL_UP = {
    1: ("эцэг", "эх"),
    2: ("өвөг эцэг", "өвөг эх"),
    3: ("өндөр өвөг", "өндөр эмээ"),
    4: ("элэнц эцэг", "элэнц эх"),
    5: ("хуланц эцэг", "хуланц эх"),
    6: ("элэн", "элэн"),
    7: ("хулан", "хулан"),
}


# Чиглэлийн нэрс: `mode` (ancestors/descendants) ба зангилааны `branch`
# (up/down) хоёуланг хүлээнэ — «Нэгдсэн» горимд нэг зурган дээр хоёулаа
# байдаг тул ЗАНГИЛААНЫ салаагаар шийдэх ёстой.
_UP_MODES = {"ancestors", "up"}


def direct_relation_label(depth, sex, mode):
    """Модны зангилааны нэрний дараа хаалтанд бичих хамаатан; root бол None.

    Ургийн модны зангилаа бүр root-ын ШУУД өвөг эсвэл ШУУД үр хойчис байдаг
    (`build_graph` нь зөвхөн `ancestors`/`descendants` CTE-ээс зангилаа авдаг)
    тул хамаатныг `depth`-ээс шууд тодорхойлно — нэмэлт query хэрэггүй.

    `mode` нь чиглэл: `"ancestors"`/`"up"` (өвөг дээшээ) эсвэл
    `"descendants"`/`"down"` (үр хойш).
    """
    if depth == 0:
        return None
    if mode in _UP_MODES:
        pair = _REL_UP.get(depth)
        if pair is None:
            return f"{depth} үеийн өвөг"
        return {"M": pair[0], "F": pair[1]}.get(sex, f"{pair[0]}/{pair[1]}")
    # Үр хойш: зөвхөн 1-р үе хүйсээрээ ялгагдана. «ач/зээ», «жич/гуч» нь
    # хүүхдийн хүйсээр бус, ЗАВСРЫН үеийн хүйсээр тодорхойлогддог.
    if depth == 1:
        return {"M": "хүү", "F": "охин"}.get(sex, "хүү/охин")
    return _DIRECT_DOWN.get(depth, f"{depth} үеийн үр хойчис")


# ---------------------------------------------------------------------------
# Дутуу эцэг/эх — илрүүлэлт ба кандидат санал
# ---------------------------------------------------------------------------

_PARENT_ROLES = {"father": "Эцэг", "mother": "Эх"}

# Кандидатын оноо. Хүйс таарахгүй бол хүчтэй хасагдана — апп ижил хүйстэй хосыг
# дэмждэг ч санал болгох жагсаалт цэвэр байх ёстой. Гараар хайх зам
# (`link_relative`-ийн picker) үргэлж нээлттэй хэвээр.
_SCORE_PATRONYMIC = 100      # Монголд эцгийн нэр (patronymic) = ЭЦГИЙН өөрийн нэр
_SCORE_CLAN = 20
_SCORE_SURNAME = 10
_SCORE_SEX_MATCH = 20
_SCORE_SEX_MISMATCH = -40
_SCORE_DATE_OK = 30          # эцэг эх нь хүүхдээс 14–50 жил өмнө төрсөн байх ёстой
_SCORE_DATE_BAD = -30
_PARENT_MIN_GAP, _PARENT_MAX_GAP = 14, 50
# Оноог Python-д боддог тул кандидатын сан хязгаартай байх ёстой.
_PARENT_POOL_LIMIT = 5000


def missing_parent_slots(person):
    """Төрсөн гэр бүлд дутуу байгаа эцэг/эх үүрүүд.

    Буцаах: [] | ["father"] | ["mother"] | ["father", "mother"].

    `Family.partner1`/`partner2` нь танигдаагүй эцэг эхийн хувьд NULL байж
    болно. Үүрийн дугаараар биш ХҮЙСЭЭР тоолно: `_link` нь эцгийг partner1-д,
    эхийг partner2-д тавьдаг ч эхний үүр дүүрэн бол нөгөө рүү шилждэг тул
    үүр нь хүйсээр хатуу тогтоогдоогүй.
    """
    family = person.birth_family
    partners = family.partners if family else []      # `Family.partners` нь list
    if len(partners) >= 2:
        return []
    if not partners:
        return ["father", "mother"]
    sexes = {p.sex for p in partners}
    if "M" in sexes:
        return ["mother"]
    if "F" in sexes:
        return ["father"]
    # Хүйс тодорхойгүй нэг эцэг эх — аль нь дутууг мэдэхгүй тул хоёуланг санал болгоно.
    return ["father", "mother"]


def sibling_parent_candidates(person):
    """Хүнтэй НЭГ эцэг/эх хуваалцдаг ах дүүсийн бүртгэсэн НӨГӨӨ эцэг/эх.

    Ах дүүс нь нөгөө эцэг/эхээ бүртгэсэн байж болно — энэ нь дутуу үүрийн
    хамгийн хүчтэй сэжүүр, учир нь ихэвчлэн ЯГ тэр хүн байдаг. Баталгаа бол
    биш (дахин гэрлэсэн бол өөр хүн байж болно) тул зөвхөн санал болгоно.

    Төрсөн гэр бүлдээ яг НЭГ эцэг/эх бүртгэгдсэн үед л ажиллана — хоёулаа
    байвал дутуу үүр байхгүй, нэг ч байхгүй бол хуваалцах эцэг/эх байхгүй.
    """
    family = person.birth_family
    if family is None:
        return []
    known = family.partners
    if len(known) != 1:
        return []
    parent = known[0]
    seen = {parent.pk, person.pk}
    result = []
    others = (
        Family.objects.filter(models_q_family([parent.pk]))
        .exclude(pk=family.pk)
        .prefetch_related("partner1", "partner2")
    )
    for other in others:
        for partner in other.partners:
            if partner.pk not in seen:
                seen.add(partner.pk)
                result.append(partner)
    return result


def _parent_candidate_score(child, candidate, role):
    """Кандидат тухайн үүрд хэр тохирохыг оноогоор илэрхийлнэ.

    Зөвхөн хүйс таарснаар санал болгохгүй: нэр (эцгийн нэр / овог), эсвэл
    боломжит он жилийн дохио ЗААВАЛ байх ёстой. Эс бөгөөс огноогүй, эцгийн
    нэргүй хүмүүст бүх эрэгтэй/эмэгтэй санал болж, жагсаалт утгагүй болно.
    """
    child_year = child.birth_date.year if child.birth_date else None
    cand_year = candidate.birth_date.year if candidate.birth_date else None
    # Хүүхдээс ХОЙШ төрсөн хүн эцэг эх болж чадахгүй — бусад дохионоос үл хамааран хасна.
    if child_year is not None and cand_year is not None and cand_year > child_year:
        return 0

    score = 0
    has_signal = False
    if (role == "father" and child.patronymic and candidate.given_name
            and candidate.given_name.casefold() == child.patronymic.casefold()):
        score += _SCORE_PATRONYMIC
        has_signal = True
    # Хүүхдийн овог ЭЦГИЙНх тул эхэд овгийн таарлыг дохио гэж үзэхгүй.
    if role != "mother" and child.clan_name and candidate.clan_name == child.clan_name:
        score += _SCORE_CLAN
        has_signal = True
    if child.surname and candidate.surname == child.surname:
        score += _SCORE_SURNAME
        has_signal = True
    if candidate.sex in ("M", "F"):
        wanted = "M" if role == "father" else "F"
        score += _SCORE_SEX_MATCH if candidate.sex == wanted else _SCORE_SEX_MISMATCH
    if child_year is not None and cand_year is not None:
        gap = child_year - cand_year
        if _PARENT_MIN_GAP <= gap <= _PARENT_MAX_GAP:
            score += _SCORE_DATE_OK
            has_signal = True
        else:
            score += _SCORE_DATE_BAD
    return score if has_signal else 0


def suggest_parents(person, user, limit=5):
    """Дутуу эцэг/эх үүр бүрд тохирох хүмүүсийг эрэмбэлж санал болгоно.

    Буцаах: [{"role", "label", "candidates", "ambiguous"}]. `ambiguous=True` нь
    одоогийн ганц эцэг/эхийн хүйс тодорхойгүй тул аль үүр дутууг мэдэхгүй
    байгааг заана — тэр үед хоёуланг харуулж, хэрэглэгч зөвийг нь сонгоно.
    """
    roles = missing_parent_slots(person)
    if not roles:
        return []

    family = person.birth_family
    partners = family.partners if family else []
    ambiguous = len(partners) == 1 and partners[0].sex not in ("M", "F")

    blocked = {person.pk} | {p.pk for p in partners}
    # Эцэг/эх болгоход хориглох нь ЯГ үр хойчис — `views._blocked_for_relation`-тэй
    # ижил дүрэм. (`services` нь `views`-ийг импортлож болохгүй — circular.)
    blocked |= set(descendants(person, max_depth=CYCLE_SEARCH_LIMIT))

    # `visible_to` биш `editable_by`: санал болгосон хүн нь засах эрхтэй байх
    # ёстой — эс бөгөөс picker-т орохгүй, чип нь мухардалд хүрнэ.
    # Анхаар: editor-т энэ нь БҮХ амьд хүнийг буцаана. Оноог Python-д боддог
    # тул санг хязгаарлана; энэ аппын хэмжээнд хүрэлцэнэ.
    pool = (Person.objects.editable_by(user)
            .exclude(pk__in=blocked)
            .select_related("birth_date", "death_date")[:_PARENT_POOL_LIMIT])

    # Ах дүүгийн бүртгэсэн нөгөө эцэг/эх — онооноос үл хамааран тэргүүнд.
    from_siblings = sibling_parent_candidates(person)
    if from_siblings:
        allowed = set(
            Person.objects.editable_by(user)
            .filter(pk__in=[q.pk for q in from_siblings])
            .values_list("pk", flat=True))
        from_siblings = [q for q in from_siblings if q.pk in allowed]
    sibling_ids = {q.pk for q in from_siblings}

    result = []
    for role in roles:
        ranked = sorted(
            ((_parent_candidate_score(person, c, role), c) for c in pool),
            key=lambda pair: (-pair[0], pair[1].display_name),
        )
        candidates = [
            {"person": q, "reason": f"ах дүүгийн {_PARENT_ROLES[role].lower()}"}
            for q in from_siblings
        ]
        candidates += [
            {"person": c, "reason": None}
            for score, c in ranked[:limit]
            if score > 0 and c.pk not in sibling_ids
        ]
        result.append({
            "role": role,
            "label": _PARENT_ROLES[role],
            "ambiguous": ambiguous,
            "candidates": candidates[:limit],
        })
    return result


def relationship_between(a, b, max_depth=DEFAULT_MAX_GENERATIONS):
    """Хоёр хүний хамаатны ойролцоо тодорхойлолт.

    Нийтлэг өвгийг олж, хоёр талын зайнаас хамаатан нэрлэнэ.
    Буцаах: {"label", "common_ancestor_id", "distance_a", "distance_b", "closest"}
    """
    if a.pk == b.pk:
        return {"label": "өөрөө", "common_ancestor_id": a.pk, "distance_a": 0,
                "distance_b": 0, "closest": True}

    anc_a = {a.pk: 0, **ancestors(a, max_depth)}
    anc_b = {b.pk: 0, **ancestors(b, max_depth)}
    desc_a = {a.pk: 0, **descendants(a, max_depth)}
    desc_b = {b.pk: 0, **descendants(b, max_depth)}

    # Шууд дээшээ / доошоо
    if b.pk in anc_a:
        d = anc_a[b.pk]
        return {"label": _DIRECT_UP.get(d, f"{d} үеийн өвөг"), "common_ancestor_id": b.pk,
                "distance_a": d, "distance_b": 0, "closest": True}
    if b.pk in desc_a:
        d = desc_a[b.pk]
        return {"label": _DIRECT_DOWN.get(d, f"{d} үеийн үр хойчис"), "common_ancestor_id": a.pk,
                "distance_a": 0, "distance_b": d, "closest": True}

    common = set(anc_a) & set(anc_b)
    if not common:
        return {"label": "хамаатан тогтоогдоогүй", "common_ancestor_id": None,
                "distance_a": None, "distance_b": None, "closest": False}

    best = min(common, key=lambda pid: (anc_a[pid] + anc_b[pid], max(anc_a[pid], anc_b[pid])))
    da, db = anc_a[best], anc_b[best]

    if da == 1 and db == 1:
        label = "ах/эгч/дүү"
    elif da == 1 and db == 2:
        label = "авга/нагац (эцэг эхийн ах дүү)"
    elif da == 2 and db == 1:
        label = "авга/нагац (эцэг эхийн ах дүү)"
    elif da == 2 and db == 2:
        label = "үеэл"
    elif da == 3 and db == 3:
        label = "хоёр дахь үеэл"
    else:
        generations = min(da, db)
        label = f"{generations} үеийн угийн хамаатан" if generations > 1 else "хамаатан"

    return {"label": label, "common_ancestor_id": best, "distance_a": da,
            "distance_b": db, "closest": True}


# ---------------------------------------------------------------------------
# Нэгтгэл (давхардсан хүн)
# ---------------------------------------------------------------------------


@transaction.atomic
def merge_persons(keep, duplicate, user=None):
    """`duplicate`-ыг `keep` рүү нэгтгэнэ. Бичлэгийг УСТГАХГҮЙ — merged_into тавьж,
    холбоог шилжүүлнэ (буцаах боломжтой байх ёстой)."""
    if keep.pk == duplicate.pk:
        raise ValueError("Нэг хүнийг өөртэй нь нэгтгэх боломжгүй.")
    if duplicate.merged_into_id == keep.pk:
        return keep  # аль хэдийн нэгтгэсэн — дахин хийхэд аюулгүй

    summary = defaultdict(int)

    # 1. Нэрс
    for name in duplicate.names.all():
        exists = PersonName.objects.filter(
            person=keep, name_type=name.name_type, given_name=name.given_name,
            patronymic=name.patronymic, surname=name.surname, clan_name=name.clan_name,
        ).exists()
        if exists:
            name.delete()
        else:
            name.person = keep
            name.save(update_fields=["person"])
        summary["names"] += 1

    # 2. Гэр бүлд эхнэр/нөхөр болж байгаа
    for family in list(Family.objects.filter(partner1=duplicate)):
        _retarget_family_partner(family, "partner1", keep)
        summary["families"] += 1
    for family in list(Family.objects.filter(partner2=duplicate)):
        _retarget_family_partner(family, "partner2", keep)
        summary["families"] += 1

    # 3. Хүүхдийн холбоо
    for link in list(ChildLink.objects.filter(child=duplicate)):
        clash = ChildLink.objects.filter(child=keep, family=link.family).exists()
        birth_clash = (
            link.pedigree == ChildLink.Pedigree.BIRTH
            and ChildLink.objects.filter(child=keep, pedigree=ChildLink.Pedigree.BIRTH).exists()
        )
        if clash or birth_clash:
            link.delete()
        else:
            link.child = keep
            link.save(update_fields=["child"])
        summary["child_links"] += 1

    # 4. Үйл явдлын оролцогч
    for part in list(duplicate.event_participations.all()):
        clash = part.__class__.objects.filter(
            event=part.event, person=keep, role=part.role
        ).exists()
        if clash:
            part.delete()
        else:
            part.person = keep
            part.save(update_fields=["person"])
        summary["event_parts"] += 1

    # 5. Ишлэл ба медиа холбоос (generic)
    summary["citations"] += _retarget_generic(duplicate, keep, "citations", "genealogy.Citation")
    summary["media_links"] += _retarget_generic(duplicate, keep, "media_links", "genealogy.MediaLink")

    # 6. Талбарыг нөхөх — хоосон талыг нөгөөгөөс авна
    filled = []
    for field in ("clan_name", "patronymic", "surname", "bio", "birth_date", "death_date",
                  "birth_place", "death_place", "living_override"):
        if not getattr(keep, f"{field}_id", None) and not getattr(keep, field, None):
            value = getattr(duplicate, field, None)
            if value is not None:
                setattr(keep, field, value)
                filled.append(field)
    if fill_sex := (keep.sex == Person.Sex.UNKNOWN and duplicate.sex != Person.Sex.UNKNOWN):
        keep.sex = duplicate.sex
        filled.append("sex")
    keep.save()

    duplicate.merged_into = keep
    duplicate.save(update_fields=["merged_into"])

    log_action(
        user, AuditLog.Action.MERGE, keep,
        {"merged_person": duplicate.pk, "merged_name": duplicate.display_name,
         "moved": dict(summary), "fields_filled": filled, "fill_sex": bool(fill_sex)},
    )
    return keep


def _retarget_family_partner(family, slot, keep):
    """Гэр бүлийн нэг талыг солих. Нөгөө тал нь мөн `keep` болвол давхардал
    үүсэх тул гэр бүлийг устгана (өөрөө өөртэйгөө гэрлэсэн байх ёсгүй)."""
    other_slot = "partner2" if slot == "partner1" else "partner1"
    if getattr(family, f"{other_slot}_id") == keep.pk:
        # Нөгөө тал нь ч бас keep болж байна — гэр бүл утгагүй болно.
        # Хүүхдүүдийг шилжүүлэх газар байхгүй тул гэр бүлийг хэвээр үлдээж,
        # зөвхөн хоосон талыг үлдээнэ.
        setattr(family, slot, None)
        family.save(update_fields=[slot])
        return
    setattr(family, slot, keep)
    family.save(update_fields=[slot])


def _retarget_generic(source_obj, target_obj, accessor, model_path):
    """GenericRelation-оор холбогдсон бичлэгүүдийг шилжүүлэх."""
    from django.apps import apps

    model = apps.get_model(model_path)
    ctype = ContentType.objects.get_for_model(target_obj)
    moved = 0
    for obj in list(getattr(source_obj, accessor).all()):
        obj.object_id = target_obj.pk
        obj.content_type = ctype
        try:
            obj.save(update_fields=["object_id", "content_type"])
            moved += 1
        except Exception:
            # Давхардал үүссэн бол хаяна (unique constraint).
            obj.delete()
    return moved


# ---------------------------------------------------------------------------
# Аудит
# ---------------------------------------------------------------------------


def log_action(user, action, obj, changes=None, ip_address=None):
    if obj is None:
        return None
    return AuditLog.objects.create(
        user=user if (user is not None and getattr(user, "is_authenticated", False)) else None,
        action=action,
        content_type=ContentType.objects.get_for_model(obj),
        object_id=obj.pk,
        object_repr=str(obj)[:300],
        changes=changes or {},
        ip_address=ip_address,
    )


def client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


# ---------------------------------------------------------------------------
# Хүн үүсгэх туслах (quick-add)
# ---------------------------------------------------------------------------


@transaction.atomic
def suggested_clan_name(anchor, relation):
    """Хамаатан нэмэхэд САНАЛ БОЛГОХ ургийн овог (хоосон бол "").

    Овог нь эцгээсээ дамждаг тул шинэ хүний эцэг хэн болохыг тааж, түүний
    овгийг санал болгоно. ХЭЗЭЭ Ч автоматаар бичихгүй — форм дээр урьдчилан
    бөглөгдөж, хэрэглэгч сольж болно (`Person.inherit_clan_name` бол
    нэг удаагийн нөхөлтийн команд дотор л ажиллана).
    """
    father = _suggested_father(anchor, relation)
    return father.clan_name if father is not None else ""


def _suggested_father(anchor, relation):
    """Шинэ хүний эцэг хэн болохыг таана (`None` — тодорхойгүй)."""
    if relation in {"child", "son", "daughter"}:
        # Хүүхэд anchor-ын гэр бүлд орно — тухайн гэр бүлийн эрэгтэй тал.
        for family in anchor.families:
            for partner in family.partners:
                if partner.sex == Person.Sex.MALE:
                    return partner
        return anchor if anchor.sex == Person.Sex.MALE else None
    if relation in {"sibling", "brother", "sister"}:
        return anchor.father          # ах/эгч/дүү нэг эцэгтэй
    return None


def create_relative(anchor, relation, *, given_name, patronymic="", clan_name="",
                    surname="", sex=Person.Sex.UNKNOWN, birth_date=None, user=None,
                    pedigree=ChildLink.Pedigree.BIRTH, **extra):
    """`anchor` хүнд хамаатан нэмэх: father / mother / partner / child / sibling.

    Гэр бүлийн бүтцийг автоматаар үүсгэнэ (эцэг эх байхгүй бол шинэ гэр бүл,
    эхнэр/нөхөр байхгүй бол шинэ Family гэх мэт).
    """
    person = Person.objects.create(
        given_name=given_name, patronymic=patronymic, clan_name=clan_name,
        surname=surname, sex=sex, birth_date=birth_date,
        created_by=user if getattr(user, "is_authenticated", False) else None,
        **extra,
    )
    _link(anchor, relation, person, pedigree)
    log_action(user, AuditLog.Action.CREATE, person,
               {"relation": relation, "anchor": anchor.pk, "pedigree": pedigree})
    return person


@transaction.atomic
def link_existing(anchor, relation, person, user=None, pedigree=ChildLink.Pedigree.BIRTH):
    """БАЙГАА хүнийг `anchor`-тай холбоно (шинэ хүн үүсгэхгүй).

    Атомар: шалгалт унавал юу ч өөрчлөгдөхгүй.
    """
    if person.pk == anchor.pk:
        raise ValueError("Хүнийг өөртэй нь холбох боломжгүй.")
    _link(anchor, relation, person, pedigree)
    log_action(user, AuditLog.Action.UPDATE, anchor,
               {"linked": person.pk, "linked_name": person.display_name,
                "relation": relation, "pedigree": pedigree})
    return person


def _resolve_birth_family(anchor):
    """Төрсөн гэр бүл байхгүй бол үүсгэж, anchor-ыг хүүхэд болгож холбоно."""
    family = anchor.birth_family
    if family is None:
        family = Family.objects.create(family_type=Family.FamilyType.UNKNOWN)
        ChildLink.objects.create(family=family, child=anchor)
    return family


def _link(anchor, relation, person, pedigree=ChildLink.Pedigree.BIRTH):
    """Холбоог бодитоор үүсгэх. Алдаа гарвал ValueError (дуудагч тал хариуцна).

    `pedigree` нь холбооны төрөл: төрсөн / өргөмөл / асран хүмүүжүүлсэн /
    хойд. Зөвхөн «төрсөн» нь нэг л байж болно; бусад нь нэмэлтээр оршино.
    """
    if pedigree not in dict(ChildLink.Pedigree.choices):
        raise ValueError(f"Тодорхойгүй холбооны төрөл: {pedigree}")
    is_birth = pedigree == ChildLink.Pedigree.BIRTH

    if relation in {"father", "mother", "parent"}:
        family = (
            _resolve_birth_family(anchor) if is_birth
            else _resolve_pedigree_family(anchor, pedigree)
        )
        # Хүйсээр талыг тодорхойлно; "parent" бол хоосон талд нь тавина.
        if relation == "father":
            slot, fallback = "partner1", "partner2"
        elif relation == "mother":
            slot, fallback = "partner2", "partner1"
        else:
            slot, fallback = (
                ("partner1", "partner2") if family.partner1_id is None else ("partner2", "partner1")
            )
        existing = {p.pk for p in family.partners}
        if person.pk in existing:
            raise ValueError(f"{person.display_name} аль хэдийн энэ гэр бүлд байна.")
        # Цикл зөвхөн «person нь anchor-ын үр хойчис» үед үүснэ
        # (хүн өөрийн үр хойшсын эцэг эх болох).
        if person.pk in set(descendants(anchor, max_depth=CYCLE_SEARCH_LIMIT)):
            raise ValueError(
                f"{person.display_name} нь {anchor.display_name}-ийн үр хойчис — "
                "эцэг эх болгож холбовол удмын мод циклд орно."
            )
        if getattr(family, f"{slot}_id") is None:
            setattr(family, slot, person)
            family.save(update_fields=[slot])
        elif getattr(family, f"{fallback}_id") is None:
            setattr(family, fallback, person)
            family.save(update_fields=[fallback])
        else:
            # Хоёр тал дүүрэн — чимээгүй эвдэхээс илүү алдаа өгнө.
            raise ValueError(
                f"{anchor.display_name} хүний «{family}» гэр бүлд хоёр эцэг эх аль хэдийн "
                "бүртгэгдсэн байна. Эхлээд гэр бүлийг засна уу."
            )

    elif relation == "partner":
        if any(person in f.partners for f in anchor.families):
            raise ValueError(f"{person.display_name} аль хэдийн хос болж бүртгэгдсэн байна.")
        # Дараалсан гэрлэлт хэвийн, гэхдээ шууд өвөг/үр хойчтойгоо хос болохыг хориглоно.
        if person.pk in set(ancestors(anchor)) or person.pk in set(descendants(anchor)):
            raise ValueError(
                f"{person.display_name} нь {anchor.display_name}-ийн шууд өвөг/үр хойчис — "
                "хос болгож холбох боломжгүй."
            )
        # Хоосон талтай гэр бүл байвал (жишээ нь хүүхэд бий, нөгөө эцэг эх
        # тодорхойгүй) түүнийг ашиглана; эс бөгөөс шинэ гэр бүл үүсгэнэ.
        family = next(
            (f for f in anchor.families if f.partner1_id is None or f.partner2_id is None),
            None,
        )
        if family is None:
            Family.objects.create(partner1=anchor, partner2=person)
        else:
            slot = "partner1" if family.partner1_id is None else "partner2"
            setattr(family, slot, person)
            family.save(update_fields=[slot])

    elif relation in {"child", "son", "daughter"}:
        _assert_can_be_child(person, pedigree)
        # Цикл: anchor нь person-ы үр хойчис байвал (өөрийн үр хойшсоо хүүхэд болгох).
        if anchor.pk in set(descendants(person, max_depth=CYCLE_SEARCH_LIMIT)):
            raise ValueError(
                f"{anchor.display_name} нь {person.display_name}-ийн үр хойчис — "
                "хүүхэд болгож холбовол удмын мод циклд орно."
            )
        anchor_families = list(anchor.families)
        family = anchor_families[0] if anchor_families else Family.objects.create(partner1=anchor)
        ChildLink.objects.create(family=family, child=person, pedigree=pedigree)

    elif relation in {"sibling", "brother", "sister"}:
        _assert_can_be_child(person, pedigree)
        family = (
            _resolve_birth_family(anchor) if is_birth
            else _resolve_pedigree_family(anchor, pedigree,
                                          copy_partners_from=anchor.birth_family)
        )
        if person.pk in {p.pk for p in family.partners}:
            raise ValueError(
                f"{person.display_name} нь {anchor.display_name}-ийн эцэг эх — "
                "ах/эгч/дүү болгож болохгүй."
            )
        if person.pk in set(ancestors(anchor)):
            raise ValueError(
                f"{person.display_name} нь {anchor.display_name}-ийн өвөг — "
                "ах/эгч/дүү болговол удмын мод циклд орно."
            )
        ChildLink.objects.create(family=family, child=person, pedigree=pedigree)

    else:
        raise ValueError(f"Тодорхойгүй хамаатан: {relation}")


def _resolve_pedigree_family(anchor, pedigree, copy_partners_from=None):
    """Тухайн төрлийн эцэг эхийн гэр бүлийг олно; байхгүй бол үүсгэнэ.

    Жишээ: өргөмөл эцэг нэмэхэд төрсөн гэр бүлд биш, тусдаа өргөмөл гэр бүлд
    нэмэгдэнэ — ингэснээр хоёр холбоо зэрэг оршиж, ялгаатай харагдана.
    """
    link = anchor.parent_links.filter(pedigree=pedigree).first()
    if link is not None:
        return link.family

    partners = {}
    if copy_partners_from is not None:
        # Ах/эгч/дүү нэмэхэд төрсөн гэр бүлийн эцэг эхийг хуулна.
        partners = {
            "partner1": copy_partners_from.partner1,
            "partner2": copy_partners_from.partner2,
        }
    family = Family.objects.create(family_type=Family.FamilyType.UNKNOWN, **partners)
    ChildLink.objects.create(family=family, child=anchor, pedigree=pedigree)
    return family


def _assert_can_be_child(person, pedigree):
    """Нэг хүүхэд зөвхөн НЭГ ТӨРСӨН гэр бүлтэй байна (DB constraint-ийн ойлгомжтой алдаа).

    Өргөмөл/асран хүмүүжүүлсэн гэр бүл нэмэлтээр байж болно — тиймээс зөвхөн
    `birth` төрөлд шалгана.
    """
    if pedigree != ChildLink.Pedigree.BIRTH:
        return
    if person.parent_links.filter(pedigree=ChildLink.Pedigree.BIRTH).exists():
        family = person.birth_family
        raise ValueError(
            f"{person.display_name} аль хэдийн «{family}» гэр бүлд төрсөн хүүхэд болж "
            "бүртгэгдсэн байна. Эхлээд тэр холбоог засна уу."
        )


# ---------------------------------------------------------------------------
# Модны графикийн өгөгдөл (JSON)
# ---------------------------------------------------------------------------


# Замын код — чиглэлээс хамаарч үсэг өөр:
#   өвөг дээшээ: аав → «А», ээж → «Э»  (аль эцэг эхийг дагаснаа заана)
#   үр хойш:    хүү → «Х», охин → «О»  (аль хүүхдийг дагаснаа заана)
_PATH_LETTERS = {
    "ancestors": {"M": "А", "F": "Э"},
    "descendants": {"M": "Х", "F": "О"},
}


def effective_sex_map(people, masked_pks=()):
    """{person_id: хүйс} — хүйс тодорхойгүйг гэр бүлийн үүргээс таана.

    «Эцэг бол эрэгтэй, эх эмэгтэй» зарчмаар: **нөгөө хосийн** хүйс тодорхой
    бол эсрэг хүйсээр нөхнө. Зөвхөн үүрээр (partner1/partner2) таахгүй —
    `_link` нь эцгийг partner1-д тавьдаг ч чөлөөт үүр байхгүй бол нөгөө рүү
    шилждэг тул эх partner1-д ч орж болно. Хоёулаа тодорхойгүй бол хэвээр.

    `masked_pks` — нарийн мэдээлэл нь хаалттай («Хувийн») хүмүүс. Тэдний
    хүйсийг бодохгүй, мөн **тэднээс бусдын хүйсийг таахгүй**: эс бөгөөс
    хамтрагчийн хүйсээр дамжин хаалттай хүний хүйс алдарна.

    Нэг query — `build_graph`-ийн зангилаа бүрд query хийхээс сэргийлнэ.
    """
    masked_pks = set(masked_pks)
    result = {pid: (Person.Sex.UNKNOWN if pid in masked_pks else p.sex)
              for pid, p in people.items()}
    unknown = [pk for pk, sex in result.items() if sex == Person.Sex.UNKNOWN]
    if not unknown:
        return result

    for family in (Family.objects.filter(models_q_family(unknown))
                   .select_related("partner1", "partner2")):
        for me, other in ((family.partner1, family.partner2),
                          (family.partner2, family.partner1)):
            if me is None or other is None:
                continue
            if me.pk in masked_pks or other.pk in masked_pks:
                continue                     # хаалттай хүний хүйсийг задруулахгүй
            if result.get(me.pk) != Person.Sex.UNKNOWN:
                continue
            if other.sex == Person.Sex.MALE:
                result[me.pk] = Person.Sex.FEMALE    # нөгөө нь эцэг → энэ эх
            elif other.sex == Person.Sex.FEMALE:
                result[me.pk] = Person.Sex.MALE      # нөгөө нь эх → энэ эцэг
    return result


def _path_codes(paths, people, branch, sexes=None, masked_pks=()):
    """Зам бүрд үсгийн код бодно — салаа тус бүр өөрийн үсэгтэй.

    `branch`: {person_id: "up" | "down" | "root"}. `both` горимд нэг зурган
    дээр дээшээ «А/Э» (аав/ээж), доошоо «Х/О» (хүү/охин) холилдоно.

    Жишээ: ээжийн ээж → «ЭЭ», аавын ээж → «АЭ», хүүгийн охин → «ХО».
    Root нь хоосон кодтой.

    Хүйсийг зөвхөн ХАРАГДАХ хүмүүсийн сангаас (`people`) авна — нуугдсан
    (`people`-д байхгүй) болон нарийн мэдээлэл нь хаалттай (`masked_pks`)
    завсрын хүний хүйсийг задруулахгүйн тулд «·» тавина.
    """
    masked_pks = set(masked_pks)
    codes = {}
    for pid, path in paths.items():
        letters_for = (_PATH_LETTERS["ancestors"] if branch.get(pid) == "up"
                       else _PATH_LETTERS["descendants"])
        letters = []
        for step in path[1:]:
            person = people.get(step)
            if person is None or step in masked_pks:
                letters.append("·")
            else:
                sex = (sexes or {}).get(step, person.sex)
                letters.append(letters_for.get(sex, "?"))
        codes[pid] = "".join(letters)
    return codes


class HiddenPerson:
    """Харах эрхгүй хүний оронд харуулах placeholder.

    Нэг ч мэдээлэл задруулахгүй — нэр, нас, хүйс, холбоос бүгд хаалттай.
    Модны `•••` зангилаатай ижил утгатай. Template-д `Person`-той ижил
    интерфейсийг (`named_display`, `lifespan`, `is_hidden`) хангана.

    `closed` — шалтгаан нь «Хаалттай» харагдах байдал мөн эсэх. Тийм бол
    placeholder-ын хажууд «(хаалттай)» гэж бичнэ; эс бөгөөс (жишээ нь
    нэгтгэгдсэн бүртгэл) зөвхөн `•••` — шалтгааныг таамаглуулахгүй.
    """

    pk = None
    is_hidden = True
    display_name = "•••"
    named_display = "•••"
    lifespan = ""
    is_deceased = False
    role_tag = ""          # эцэг эхийн тодруулга — placeholder-д хоосон

    def __init__(self, closed=True):
        self.closed = closed

    def __repr__(self):
        return f"<HiddenPerson closed={self.closed}>"


def build_graph(root, mode="descendants", max_depth=4, user=None, bloodline_only=False):
    """D3-д зориулсан {nodes, links} буцаана.

    Давхардсан хүнийг НЭГ л удаа оруулна (DAG) — layout дээр давхар зангилаа
    үүсэхгүй. Ирмэг бүр `pedigree`-тэй тул фронт дээр өргөмөл холбоог
    тасархай шугамаар ялгаж зурна.
    """
    # Хоёр CTE ч замаа буцаадаг — гүн нь замын уртаас гарна (`len(path) - 1`),
    # тэгэхээр нэмэлт query гарахгүй. `mode="both"` үед хоёуланг нь нийлүүлнэ.
    paths = {root.pk: [root.pk]}
    # Салаа: "up" = өвөг дээшээ, "down" = үр хойш. Root нь "root".
    # (Нэг хүн хоёуланд ч багтахгүй — цикл хориотой тул.)
    branch = {root.pk: "root"}
    if mode in ("ancestors", "both"):
        for pid, path in ancestor_paths(root, max_depth, bloodline_only=bloodline_only).items():
            paths[pid] = path
            branch[pid] = "up"
    if mode in ("descendants", "both"):
        for pid, path in descendant_paths(root, max_depth, bloodline_only=bloodline_only).items():
            paths[pid] = path
            branch[pid] = "down"
    depths = {pid: len(path) - 1 for pid, path in paths.items()}
    # Мөр: өвөг дээшээ сөрөг, үр хойш эерэг — `both` горимд хоёулаа
    # нэг зурган дээр давхцахгүй (root төвд).
    rows = {pid: (-d if branch[pid] == "up" else d) for pid, d in depths.items()}

    visible = Person.objects.visible_to(user).filter(pk__in=depths)
    people = {p.pk: p for p in visible.select_related("birth_date", "death_date")}
    # «Хувийн» хүн: нэр нь харагдана, харин нас/хүйс/хамаатан нь хаалттай.
    # (Модны JSON-оос огт явуулахгүй — эс бөгөөс endpoint-оос алдарна.)
    masked = {pid for pid, p in people.items() if p.is_masked_for(user)}
    # Хүйс тодорхойгүйг гэр бүлийн үүргээс нөхнө — мод, хамаатны шошго,
    # замын код бүгд үүнийг ашиглана (нэг query).
    sexes = effective_sex_map(people, masked)
    path_codes = _path_codes(paths, people, branch, sexes, masked)

    # Мөрийн эрэмбэ (tidy layout) — фронт дээр биш, ЭНД бодогдоно.
    # Root-оос гадагшлах дарааллаар (|row| өсөхөд) мөр бүрийг эрэмбэлнэ: хүн
    # бүр «root тал руу нэг алхам ойрхон» хүнийхээ мөр дэх байрлалаар, дараа
    # нь төрсөн он, дараа нь замын код, нэрээр. Ингэснээр нэг айлын хүүхдүүд
    # ЗЭРЭГЦЭЭ, ахмад ах/эгчийн хүүхдүүд урд, дотроо төрсөн оноор жагсана
    # (зөвхөн үсгээр бүлэглэвэл хүйсээр салж, ахмад эгчийн хүүхэд хойш
    # шилжинэ). `paths[pid][-2]` нь ямагт root тал руу нэг алхам ойрхон хүн
    # (үр хойш бол эцэг эх, өвөг дээшээ бол хүүхэд) бөгөөд |row| нь бага тул
    # аль хэдийн бодогдсон байдаг.
    def _birth_year(pid):
        p = people.get(pid)
        if pid in masked or p is None or not p.birth_date:
            return 9999          # хаалттай хүний насыг эрэмбээр ч задруулахгүй
        return p.birth_date.year or 9999

    order = {}
    for row_no in sorted(set(rows.values()), key=lambda r: (abs(r), r)):
        members = [pid for pid in paths if rows[pid] == row_no]
        members.sort(key=lambda pid: (
            order.get(paths[pid][-2], -1) if len(paths[pid]) > 1 else -1,
            _birth_year(pid),
            path_codes.get(pid) or "",
            people[pid].display_name if pid in people else "",
        ))
        for i, pid in enumerate(members):
            order[pid] = i

    def _node(pid, p):
        """Модны нэг зангилаа.

        «Хувийн» хүний хувьд (`masked`) зөвхөн НЭРИЙГ явуулна — нас, хүйс,
        хамаатны шошго бүгд хаалттай. Эдгээрийг frontend дээр нуух нь
        хангалтгүй: JSON endpoint-оос шууд уншиж болно.
        """
        hide = pid in masked
        return {
            "id": pid,
            "depth": depths[pid],
            "branch": branch[pid],
            "row": rows[pid],
            # Тухайн мөр дэх байр — фронт зөвхөн үүгээр эрэмбэлнэ (дээрх блок).
            "order": order[pid],
            "name": p.display_name,
            "called_name": p.called_name,
            "short": p.short_name,
            "sex": sexes[pid],
            "birth": None if hide else (p.birth_date.year if p.birth_date else None),
            # Эрэмбэлэх түлхүүр: нэг айлын хүүхдүүдийг төрсөн оноор эрэмбэлнэ.
            # Огноогүй нь 9999 — жагсаалтын төгсгөлд.
            "birth_sort": _birth_year(pid),
            "death": None if hide else (p.death_date.year if p.death_date else None),
            "lifespan": "" if hide else p.lifespan,
            # Салаагаар нь (`branch`) — `both` горимд өвөг ба үр хойчис
            # хоёулаа нэг зурган дээр байдаг тул `mode` хангалтгүй.
            # «эцэг/эх» гэсэн шошго нь хүйсийг задруулдаг тул хаалттай үед алга.
            "relation": None if hide else direct_relation_label(
                depths[pid], sexes[pid], branch[pid]),
            # «А/Э» замын код — зөвхөн өвөг дээдсийн горимд (доорх helper).
            "path_code": path_codes.get(pid),
            # Зангилааны "+" товчийг зөвхөн эрхтэй үед харуулна. Нэмэлт query
            # гарахгүй — created_by_id/is_open_for_edit нь мөр дээр байгаа.
            "can_edit": p.can_edit_by(user),
            "living": p.is_living,
            "deceased": p.is_deceased,
            "root": pid == root.pk,
            "hidden": False,
            # Нарийн мэдээлэл нь хаалттай — фронт дээр ч тэмдэглэгдэнэ.
            "masked": hide,
        }

    nodes = [_node(pid, p) for pid, p in people.items()]

    # Нуугдсан хүмүүс — байрлалаа эзлэх "хоосон" зангилаа (фан хэлбэр эвдрэхгүй).
    hidden_pks = [pid for pid in depths if pid not in people]
    # Шалтгааныг харуулж болох уу: нэвтэрсэн гишүүдэд `visible_to` нь ЗӨВХӨН
    # «Хаалттай»-г хасдаг тул нуугдсан нь тэр гэсэн үг. Анонимд бусад шалтгаан
    # (Гэр бүл/Хувийн) ч байж болно — тэгвэл зөвхөн `•••`.
    closed_pks = set()
    if hidden_pks and user is not None and getattr(user, "is_authenticated", False):
        closed_pks = set(Person.objects.filter(
            pk__in=hidden_pks, visibility=Person.Visibility.CLOSED,
        ).values_list("pk", flat=True))

    for pid in hidden_pks:
        nodes.append({
            "id": pid, "depth": depths[pid], "branch": branch[pid], "row": rows[pid],
            "order": order[pid],
            "name": "•••", "called_name": "", "short": "•••", "sex": "U",
            "birth": None, "birth_sort": 9999, "death": None,
            "lifespan": "", "relation": None,
            "path_code": None, "can_edit": False, "living": False, "deceased": False,
            "root": False, "hidden": True, "masked": False,
            # «Хаалттай» бол шалтгааныг харуулна (frontend дээр «(хаалттай)»).
            "closed": pid in closed_pks,
        })

    links, seen = [], set()
    node_ids = set(depths)

    def add_link(src, dst, kind, pedigree=None):
        key = (src, dst, kind, pedigree)
        if src in node_ids and dst in node_ids and key not in seen:
            seen.add(key)
            links.append({"source": src, "target": dst, "type": kind, "pedigree": pedigree})

    families = Family.objects.filter(
        models_q_family(depths)
    ).prefetch_related("children")

    for family in families:
        partners = [p.pk for p in family.partners if p.pk in node_ids]
        for i in range(len(partners)):
            for j in range(i + 1, len(partners)):
                add_link(partners[i], partners[j], "spouse")
        for link in family.children.all():
            if link.child_id not in node_ids:
                continue
            for parent_id in partners:
                add_link(parent_id, link.child_id, "parent", link.pedigree)

    return {"nodes": nodes, "links": links, "root": root.pk, "mode": mode,
            "bloodline_only": bloodline_only}


def models_q_family(node_ids):
    from django.db.models import Q

    return Q(partner1_id__in=node_ids) | Q(partner2_id__in=node_ids)
