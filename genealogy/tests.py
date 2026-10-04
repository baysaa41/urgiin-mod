"""Ургийн модны тестүүд.

Ажиллуулах: .venv/bin/python manage.py test
"""

import io
from io import StringIO
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from accounts.models import Invite
from django.urls import reverse

from .models import (
    ChildLink,
    Media,
    MediaLink,
    Event,
    EventParticipant,
    Family,
    AuditLog,
    Citation,
    FlexDate,
    Person,
    PersonName,
    Place,
    Source,
)
from .forms import (
    EventForm,
    FamilyForm,
    FlexDateField,
    MediaForm,
    PersonForm,
    QuickAddPersonForm,
)
from .services import (
    ancestor_paths,
    ancestors,
    build_graph,
    create_relative,
    descendant_paths,
    descendants,
    effective_sex_map,
    direct_relation_label,
    link_existing,
    merge_persons,
    suggested_clan_name,
    missing_parent_slots,
    relationship_between,
    sibling_parent_candidates,
    suggest_parents,
    would_create_cycle,
)

User = get_user_model()


class FlexDateTests(TestCase):
    """Огноо таних — монголын архивт бүрэн бус огноо түгээмэл."""

    def test_year_only(self):
        d = FlexDate.parse("1920")
        self.assertEqual(d.qualifier, FlexDate.Qualifier.EXACT)
        self.assertEqual(d.precision, FlexDate.Precision.YEAR)
        self.assertEqual(d.date_from, date(1920, 1, 1))
        self.assertEqual(d.date_to, date(1920, 12, 31))

    def test_full_date(self):
        d = FlexDate.parse("1920-05-03")
        self.assertEqual(d.precision, FlexDate.Precision.DAY)
        self.assertEqual(d.date_from, date(1920, 5, 3))

    def test_dotted_ru_format(self):
        self.assertEqual(FlexDate.parse("15.04.1920").date_from, date(1920, 4, 15))

    def test_about_qualifier(self):
        d = FlexDate.parse("ойролцоогоор 1920")
        self.assertEqual(d.qualifier, FlexDate.Qualifier.ABOUT)
        self.assertEqual(d.year, 1920)

    def test_before_and_after(self):
        self.assertEqual(FlexDate.parse("өмнө 1920").qualifier, FlexDate.Qualifier.BEFORE)
        self.assertEqual(FlexDate.parse("after 1920").qualifier, FlexDate.Qualifier.AFTER)

    def test_range(self):
        d = FlexDate.parse("1920/1925")
        self.assertEqual(d.qualifier, FlexDate.Qualifier.RANGE)
        self.assertEqual(d.date_from, date(1920, 1, 1))
        self.assertEqual(d.date_to, date(1925, 12, 31))

    def test_unknown_keeps_raw_text(self):
        d = FlexDate.parse("?")
        self.assertEqual(d.qualifier, FlexDate.Qualifier.UNKNOWN)
        self.assertEqual(d.raw, "?")

    def test_garbage_is_preserved_not_crashing(self):
        d = FlexDate.parse("баримтгүй")
        self.assertEqual(d.qualifier, FlexDate.Qualifier.UNKNOWN)
        self.assertEqual(d.raw, "баримтгүй")

    def test_invalid_day_does_not_crash(self):
        d = FlexDate.parse("1920-02-30")
        self.assertEqual(d.precision, FlexDate.Precision.DAY)  # текст хадгалагдана
        self.assertIsNone(d.date_from)

    def test_sort_key_ordering(self):
        a, b = FlexDate.parse("1919"), FlexDate.parse("1920")
        self.assertLess(a.sort_key, b.sort_key)

    # -- дүрслэл: qualifier нь тэмдгийг өгнө, raw-д давхар бичихгүй -----------
    def test_display_adds_qualifier_marker(self):
        """«Ойролцоогоор» нь АРДЫН «?», бусад нь угтвар үг."""
        self.assertEqual(FlexDate.parse("ойролцоогоор 1920").display, "1920?")
        self.assertEqual(FlexDate.parse("өмнө 1920").display, "өмнө 1920")
        self.assertEqual(FlexDate.parse("дараа 1920").display, "дараа 1920")
        self.assertEqual(FlexDate.parse("тооцоолсон 1920").display, "тооцоолсон 1920")

    def test_display_marker_comes_from_qualifier_not_raw(self):
        """raw нь зөвхөн он — «?»-г qualifier өгнө (давхардалгүй)."""
        d = FlexDate(raw="1950", qualifier=FlexDate.Qualifier.ABOUT,
                     precision=FlexDate.Precision.YEAR)
        self.assertEqual(d.display, "1950?")

    def test_display_does_not_double_mark_legacy_raw(self):
        """Хуучин бичлэгт угтвар нь raw-д байгаа — «?» дахин нэмэгдэхгүй."""
        d = FlexDate(raw="ойролцоогоор 1950", qualifier=FlexDate.Qualifier.ABOUT)
        self.assertEqual(d.display, "1950?")
        self.assertNotIn("ойролцоогоор", d.display)
        self.assertNotIn("??", d.display)

    def test_display_exact_and_range_unchanged(self):
        self.assertEqual(FlexDate.parse("15.04.1950").display, "15.04.1950")
        self.assertEqual(FlexDate.parse("1920/1925").display, "1920/1925")
        self.assertEqual(FlexDate.parse("?").display, "?")

    # -- оныг шууд тоогоор авах ---------------------------------------------
    def test_year_taken_directly_from_any_text(self):
        """Хатуу хэлбэр таарахгүй ч текст доторх 4 оронтой оныг авна."""
        for text, year in (("1950?", 1950), ("1933?", 1933), ("1945~", 1945),
                           ("1950 он", 1950), ("1950 гэж үзсэн", 1950)):
            with self.subTest(text=text):
                d = FlexDate.parse(text)
                self.assertEqual(d.year, year)
                self.assertEqual(d.precision, FlexDate.Precision.YEAR)

    def test_uncertain_marker_becomes_about(self):
        for text in ("1950?", "1945~", "~1920"):
            with self.subTest(text=text):
                self.assertEqual(FlexDate.parse(text).qualifier, FlexDate.Qualifier.ABOUT)
        # Тэмдэггүй бол exact хэвээр
        self.assertEqual(FlexDate.parse("1950 он").qualifier, FlexDate.Qualifier.EXACT)

    def test_uncertain_marker_not_duplicated_in_display(self):
        """«?»/«~» хэрхэн бичсэн ч гаралт нэг л «<он>?» байна."""
        for text, expected in (("1950?", "1950?"), ("1945~", "1945?"),
                               ("~1920", "1920?"), ("ойролцоогоор 1920", "1920?")):
            with self.subTest(text=text):
                out = FlexDate.parse(text).display
                self.assertEqual(out, expected)
                self.assertEqual(out.count("?"), 1)

    def test_text_without_year_stays_unknown(self):
        d = FlexDate.parse("баримтгүй")
        self.assertIsNone(d.year)
        self.assertEqual(d.qualifier, FlexDate.Qualifier.UNKNOWN)


class TreeBuilderMixin:
    """Туршилтын мод: өвөг эцэг → эцэг → хүүхэд (+ эх)."""

    def build_family(self):
        self.grandpa = Person.objects.create(given_name="Өвөг", sex=Person.Sex.MALE)
        self.grandma = Person.objects.create(given_name="Эмээ", sex=Person.Sex.FEMALE)
        self.grand_family = Family.objects.create(partner1=self.grandpa, partner2=self.grandma)

        self.father = Person.objects.create(given_name="Эцэг", sex=Person.Sex.MALE)
        ChildLink.objects.create(family=self.grand_family, child=self.father)

        self.mother = Person.objects.create(given_name="Эх", sex=Person.Sex.FEMALE)
        self.parent_family = Family.objects.create(partner1=self.father, partner2=self.mother)

        self.child = Person.objects.create(given_name="Хүүхэд", sex=Person.Sex.MALE)
        self.grandchild = Person.objects.create(given_name="Ач", sex=Person.Sex.MALE)
        ChildLink.objects.create(family=self.parent_family, child=self.child)
        self.child_family = Family.objects.create(partner1=self.child)
        ChildLink.objects.create(family=self.child_family, child=self.grandchild)
        return self


class TraversalTests(TreeBuilderMixin, TestCase):
    def setUp(self):
        self.build_family()

    def test_ancestors(self):
        result = ancestors(self.child)
        self.assertEqual(result.get(self.father.pk), 1)
        self.assertEqual(result.get(self.mother.pk), 1)
        self.assertEqual(result.get(self.grandpa.pk), 2)
        self.assertEqual(result.get(self.grandma.pk), 2)

    def test_descendants(self):
        result = descendants(self.grandpa)
        self.assertEqual(result.get(self.father.pk), 1)
        self.assertEqual(result.get(self.child.pk), 2)
        self.assertEqual(result.get(self.grandchild.pk), 3)

    def test_dag_diamond_deduplicated(self):
        """Нэг өвөг хоёр замаар ирэхэд НЭГ л удаа, хамгийн ойр замаар буцаана.

        Гурвалжин: эцэг, эх нь ах дүү (нэг өвөгтэй) → тэр өвөг нь хүүхдэд
        2 замaар хүрдэг.
        """
        g1 = Person.objects.create(given_name="Өвөг1")
        g2 = Person.objects.create(given_name="Өвөг2")
        grand_family = Family.objects.create(partner1=g1, partner2=g2)

        father = Person.objects.create(given_name="Эцэг2", sex=Person.Sex.MALE)
        mother = Person.objects.create(given_name="Эх2", sex=Person.Sex.FEMALE)
        ChildLink.objects.create(family=grand_family, child=father)
        ChildLink.objects.create(family=grand_family, child=mother)

        parents = Family.objects.create(partner1=father, partner2=mother)
        child = Person.objects.create(given_name="Хүүхэд2")
        ChildLink.objects.create(family=parents, child=child)

        result = ancestors(child)
        # g1 нь эцгээр (2 үе) ба эхээр (2 үе) — хоёулаа ижил зайтай тул нэг л удаа.
        self.assertEqual(result.get(g1.pk), 2)
        self.assertEqual(result.get(g2.pk), 2)
        self.assertEqual(list(result).count(g1.pk), 1)
        self.assertEqual(len(result), 4)  # father, mother, g1, g2 — давхардалгүй

    def test_cycle_is_prevented_by_constraint(self):
        """А → Б → А холбоо үүсгэх оролдлого ValidationError өгнө."""
        with self.assertRaises(ValidationError):
            link = ChildLink(family=self.child_family, child=self.grandpa)
            link.full_clean()

    def test_would_create_cycle_helper(self):
        self.assertTrue(would_create_cycle(self.grandpa, self.child_family))
        self.assertFalse(would_create_cycle(Person.objects.create(given_name="Гадны"),
                                            self.child_family))

    def test_self_parent_rejected(self):
        fam = Family.objects.create(partner1=self.child)
        self.assertTrue(would_create_cycle(self.child, fam))

    def test_one_biological_family_only(self):
        """Хоёр дахь төрсөн гэр бүл үүсгэх боломжгүй."""
        other = Family.objects.create(family_type=Family.FamilyType.UNKNOWN)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ChildLink.objects.create(family=other, child=self.child,
                                         pedigree=ChildLink.Pedigree.BIRTH)

    def test_adoptive_family_allowed_alongside_birth(self):
        other = Family.objects.create(family_type=Family.FamilyType.UNKNOWN)
        ChildLink.objects.create(family=other, child=self.child,
                                 pedigree=ChildLink.Pedigree.ADOPTED)
        self.assertEqual(self.child.parent_links.count(), 2)

    def test_relationship_labels(self):
        self.assertEqual(relationship_between(self.child, self.father)["label"], "эцэг/эх")
        self.assertEqual(relationship_between(self.child, self.grandchild)["label"], "хүү/охин")
        self.assertEqual(relationship_between(self.child, self.grandpa)["label"], "өвөг эцэг/эх")


class DirectRelationLabelTests(TestCase):
    """Модны зангилааны хамаатны нэр — хүйсээр ялгасан хэлбэр."""

    def test_root_has_no_label(self):
        self.assertIsNone(direct_relation_label(0, "M", "ancestors"))
        self.assertIsNone(direct_relation_label(0, "F", "descendants"))

    def test_ancestor_labels_gendered(self):
        """Гарны үеийн дараалал: аав → өвөг аав → өндөр өвөг → элэнц → хуланц."""
        self.assertEqual(direct_relation_label(1, "M", "ancestors"), "эцэг")
        self.assertEqual(direct_relation_label(1, "F", "ancestors"), "эх")
        self.assertEqual(direct_relation_label(2, "M", "ancestors"), "өвөг эцэг")
        self.assertEqual(direct_relation_label(3, "F", "ancestors"), "өндөр эмээ")
        self.assertEqual(direct_relation_label(4, "M", "ancestors"), "элэнц эцэг")
        self.assertEqual(direct_relation_label(5, "F", "ancestors"), "хуланц эх")
        self.assertEqual(direct_relation_label(6, "M", "ancestors"), "элэн")
        self.assertEqual(direct_relation_label(7, "F", "ancestors"), "хулан")

    def test_unknown_sex_falls_back_to_pair(self):
        self.assertEqual(direct_relation_label(1, "U", "ancestors"), "эцэг/эх")
        self.assertEqual(direct_relation_label(2, "X", "ancestors"), "өвөг эцэг/өвөг эх")

    def test_descendant_labels_follow_the_ladder(self):
        """Үр хойш: хүү/охин → ач/зээ → жич → гуч → дөч → тач → жилийх → талийх."""
        self.assertEqual(direct_relation_label(1, "F", "descendants"), "охин")
        self.assertEqual(direct_relation_label(2, "M", "descendants"), "ач/зээ")
        self.assertEqual(direct_relation_label(3, "M", "descendants"), "жич")
        self.assertEqual(direct_relation_label(4, "M", "descendants"), "гуч")
        self.assertEqual(direct_relation_label(5, "M", "descendants"), "дөч")
        self.assertEqual(direct_relation_label(8, "M", "descendants"), "талийх")

    def test_beyond_known_ladder_falls_back(self):
        self.assertEqual(direct_relation_label(8, "M", "ancestors"), "8 үеийн өвөг")
        self.assertEqual(direct_relation_label(9, "F", "descendants"), "9 үеийн үр хойчис")

    def test_descendant_labels(self):
        self.assertEqual(direct_relation_label(1, "M", "descendants"), "хүү")
        self.assertEqual(direct_relation_label(1, "F", "descendants"), "охин")
        # «ач/зээ» нь хүүхдийн хүйсээр бус, завсрын үеийн хүйсээр тодорхойлогдоно.
        self.assertEqual(direct_relation_label(2, "M", "descendants"), "ач/зээ")


class SameSexAndRemarriageTests(TestCase):
    def test_same_sex_couple(self):
        a = Person.objects.create(given_name="А", sex=Person.Sex.MALE)
        b = Person.objects.create(given_name="Б", sex=Person.Sex.MALE)
        fam = Family.objects.create(partner1=a, partner2=b)
        self.assertEqual(len(fam.partners), 2)

    def test_multiple_marriages(self):
        a = Person.objects.create(given_name="А")
        b = Person.objects.create(given_name="Б")
        c = Person.objects.create(given_name="В")
        Family.objects.create(partner1=a, partner2=b)
        Family.objects.create(partner1=a, partner2=c)
        self.assertEqual(a.families.count(), 2)

    def test_single_known_parent(self):
        """Эцэг нь танигдаагүй — гэр бүлд зөвхөн эх байна."""
        mother = Person.objects.create(given_name="Эх", sex=Person.Sex.FEMALE)
        child = Person.objects.create(given_name="Хүүхэд")
        fam = Family.objects.create(partner1=mother)
        ChildLink.objects.create(family=fam, child=child)
        self.assertEqual(child.parents, [mother])


class LivingAndVisibilityTests(TestCase):
    def setUp(self):
        self.editor = User.objects.create_user(email="ed@example.com", password="x",
                                               role=User.Role.EDITOR)

    def test_living_inferred_from_recent_birth(self):
        p = Person.objects.create(given_name="Залуу",
                                  birth_date=FlexDate.create_from_text("2000"))
        self.assertTrue(p.is_living)
        self.assertEqual(p.life_status, Person.LifeStatus.LIVING)

    def test_deceased_when_death_date_present(self):
        p = Person.objects.create(given_name="Хөгшин",
                                  birth_date=FlexDate.create_from_text("1900"),
                                  death_date=FlexDate.create_from_text("1970"))
        self.assertFalse(p.is_living)
        self.assertTrue(p.is_deceased)

    def test_unknown_when_no_dates(self):
        p = Person.objects.create(given_name="Огноогүй")
        self.assertEqual(p.life_status, Person.LifeStatus.UNKNOWN)

    def test_auto_visibility_public_for_deceased(self):
        p = Person.objects.create(given_name="Өвөг", birth_date=FlexDate.create_from_text("1850"))
        self.assertEqual(p.effective_visibility, Person.Visibility.PUBLIC)
        self.assertTrue(p.can_view(None))  # аноним харна

    def test_auto_visibility_family_for_living(self):
        p = Person.objects.create(given_name="Залуу", birth_date=FlexDate.create_from_text("1990"))
        self.assertEqual(p.effective_visibility, Person.Visibility.FAMILY)
        self.assertFalse(p.can_view(None))          # аноним харахгүй
        self.assertTrue(p.can_view(self.editor))

    def test_auto_visibility_family_for_unknown_dates(self):
        """Огноо огт байхгүй хүнийг амьд мэт хамгаална."""
        p = Person.objects.create(given_name="Огноогүй")
        self.assertEqual(p.effective_visibility, Person.Visibility.FAMILY)
        self.assertFalse(p.can_view(None))

    def test_manual_override_wins(self):
        p = Person.objects.create(given_name="Тодорхойгүй", living_override=True)
        self.assertTrue(p.is_living)

    def test_anonymous_sees_public_only(self):
        public = Person.objects.create(given_name="Нээлттэй", visibility=Person.Visibility.PUBLIC)
        Person.objects.create(given_name="Гэр бүл", visibility=Person.Visibility.FAMILY)
        Person.objects.create(given_name="Хувийн", visibility=Person.Visibility.PRIVATE)
        from django.contrib.auth.models import AnonymousUser

        visible = list(Person.objects.visible_to(AnonymousUser()))
        self.assertEqual(visible, [public])

    def test_family_member_sees_everything_but_closed(self):
        """Гишүүн: бүгдийн НЭРИЙГ харна, «Хаалттай»-гаас бусад нь.

        «Хувийн» хүний нарийн мэдээлэл (нас, хүйс) хаалттай хэвээр.
        """
        user = User.objects.create_user(email="member@example.com", password="x")
        public = Person.objects.create(given_name="Нээлттэй", visibility=Person.Visibility.PUBLIC)
        family = Person.objects.create(given_name="Гэр бүл", visibility=Person.Visibility.FAMILY)
        private = Person.objects.create(given_name="Хувийн", visibility=Person.Visibility.PRIVATE)
        Person.objects.create(given_name="Хаалттай", visibility=Person.Visibility.CLOSED)

        self.assertEqual(set(Person.objects.visible_to(user)), {public, family, private})
        self.assertTrue(private.can_view(user))          # нэр харагдана
        self.assertFalse(private.can_see_details(user))  # нас/хүйс хаалттай
        self.assertTrue(family.can_see_details(user))

    def test_editor_sees_private_details_but_not_closed(self):
        user = User.objects.create_user(email="editor@example.com", password="x",
                                        role=User.Role.EDITOR)
        private = Person.objects.create(given_name="Хувийн", visibility=Person.Visibility.PRIVATE)
        closed = Person.objects.create(given_name="Хаалттай", visibility=Person.Visibility.CLOSED)

        self.assertEqual(set(Person.objects.visible_to(user)), {private})
        self.assertTrue(private.can_see_details(user))
        self.assertFalse(closed.can_view(user))

    def test_admin_sees_closed_person(self):
        admin = User.objects.create_user(email="admin-vis@example.com", password="x",
                                         role=User.Role.ADMIN)
        closed = Person.objects.create(given_name="Хаалттай", visibility=Person.Visibility.CLOSED)
        self.assertIn(closed, Person.objects.visible_to(admin))
        self.assertTrue(closed.can_see_details(admin))

    def test_can_view_method(self):
        from django.contrib.auth.models import AnonymousUser

        anon = AnonymousUser()
        private = Person.objects.create(given_name="Хувийн", visibility=Person.Visibility.PRIVATE)
        self.assertFalse(private.can_view(anon))

    def test_merged_persons_hidden(self):
        editor = User.objects.create_user(email="ed2@example.com", password="x",
                                          role=User.Role.EDITOR)
        keep = Person.objects.create(given_name="Үлдэх")
        dup = Person.objects.create(given_name="Давхардал")
        Person.objects.create(given_name="Харагдах")
        merge_persons(keep, dup)
        visible = Person.objects.visible_to(editor)
        self.assertNotIn(dup, list(visible))
        self.assertEqual(visible.count(), 2)


class ViewerVisibilityGuaranteeTests(TreeBuilderMixin, TestCase):
    """БАТАЛГАА: модонд нэмэгдсэн хүн бүр үзэгчид харагдана.

    `visibility` нь анхдагчаараа `auto` (нас барсан → PUBLIC, бусад → FAMILY)
    тул шинээр нэмэгдсэн хүн бүр нэвтэрсэн гишүүдэд автоматаар харагдана.
    Үзэгчээс нуух цорын ганц арга бол `visibility = private`.

    Энэ зан төлөвийг санамсаргүй өөрчлөлтөөс хамгаалах тестүүд — зорилго нь
    «үзэгчид харагдахгүй болчихсон» гэдгийг чимээгүй өнгөрөөхгүй байх.
    """

    def setUp(self):
        self.build_family()
        self.viewer = User.objects.create_user(email="viewer-guarantee@example.com",
                                                password="x", role=User.Role.VIEWER)
        self.client.force_login(self.viewer)

    def _visible_ids(self):
        return set(Person.objects.visible_to(self.viewer).values_list("pk", flat=True))

    def test_default_visibility_is_auto(self):
        self.assertEqual(self.child.visibility, Person.Visibility.AUTO)

    def test_viewer_sees_every_default_person(self):
        self.assertEqual(self._visible_ids(),
                         set(Person.objects.alive().values_list("pk", flat=True)))

    def test_newly_created_person_is_visible_to_viewer(self):
        """Editor шинэ хүн нэмэхэд үзэгчид автоматаар харагдана."""
        creator = User.objects.create_user(email="viewer-creator@example.com", password="x",
                                           role=User.Role.EDITOR)
        created = create_relative(self.child, "sibling", given_name="Шинэ", user=creator)
        self.assertEqual(created.visibility, Person.Visibility.AUTO)
        self.assertIn(created.pk, self._visible_ids())

    def test_viewer_can_open_profile(self):
        r = self.client.get(reverse("genealogy:person_detail", args=[self.child.pk]))
        self.assertEqual(r.status_code, 200)

    def test_viewer_sees_full_tree_with_names(self):
        data = build_graph(self.child, mode="ancestors", max_depth=3, user=self.viewer)
        self.assertFalse(any(n["hidden"] for n in data["nodes"]))
        self.assertIn(self.grandpa.display_name,
                      [n["name"] for n in data["nodes"]])

    def test_closed_is_the_only_exception(self):
        """«Хаалттай» хүн л үзэгчээс бүрэн нуугдана.

        («Хувийн» хүний нэр харагдана, харин нарийн мэдээлэл нь хаалттай.)
        """
        Person.objects.filter(pk=self.grandpa.pk).update(
            visibility=Person.Visibility.CLOSED)
        self.assertNotIn(self.grandpa.pk, self._visible_ids())

        r = self.client.get(reverse("genealogy:person_detail", args=[self.grandpa.pk]))
        self.assertEqual(r.status_code, 404)

        data = build_graph(self.child, mode="ancestors", max_depth=3, user=self.viewer)
        node = next(n for n in data["nodes"] if n["id"] == self.grandpa.pk)
        self.assertTrue(node["hidden"])
        self.assertEqual(node["name"], "•••")

    def test_viewer_has_no_edit_rights(self):
        self.assertFalse(self.child.can_edit_by(self.viewer))
        r = self.client.get(reverse("genealogy:person_edit", args=[self.child.pk]))
        self.assertEqual(r.status_code, 302)     # CanEditMixin → нүүр рүү


class ClosedVisibilityTests(TreeBuilderMixin, TestCase):
    """«Хувийн» / «Хаалттай» — нарийн мэдээлэл хэн хэнд харагдах вэ.

    Шаардлага:
      * «Хувийн»   — НЭР бусдад харагдана, нас/хүйс хаалттай
      * «Хаалттай» — нэр ч харагдахгүй, зөвхөн `•••` placeholder
                     (засварлагчид ч; зөвхөн админ бүтнээр харна)
    """

    def setUp(self):
        self.build_family()
        self.editor = User.objects.create_user(email="closed-ed@example.com", password="x",
                                               role=User.Role.EDITOR)
        self.viewer = User.objects.create_user(email="closed-viewer@example.com", password="x",
                                               role=User.Role.VIEWER)
        self.admin = User.objects.create_user(email="closed-admin@example.com", password="x",
                                              role=User.Role.ADMIN)
        # Ялгах боломжтой нэр — «Эцэг» гэсэн шошготой холилдохгүйн тулд.
        Person.objects.filter(pk=self.father.pk).update(
            given_name="Дорж", birth_date=FlexDate.create_from_text("1950"))
        self.father.refresh_from_db()
        self.father_url = reverse("genealogy:person_detail", args=[self.father.pk])

    def _profile(self, user):
        self.client.force_login(user)
        return self.client.get(reverse("genealogy:person_detail", args=[self.child.pk]))

    def _set(self, visibility):
        Person.objects.filter(pk=self.father.pk).update(visibility=visibility)
        self.father.refresh_from_db()

    def test_visibility_has_closed_choice(self):
        self.assertIn(Person.Visibility.CLOSED, Person.Visibility.values)

    # -- «Хувийн» ----------------------------------------------------------

    def test_private_parent_name_visible_but_details_hidden(self):
        self._set(Person.Visibility.PRIVATE)
        r = self._profile(self.viewer)
        self.assertContains(r, self.father_url)       # нэр (холбоос) харагдана
        self.assertContains(r, "Дорж")
        self.assertNotContains(r, "1950")             # нас хаалттай

    def test_private_profile_hides_birth_and_sex(self):
        """«Хувийн» хүний профайл: нэр л харагдана, нас/хүйс хаалттай."""
        self._set(Person.Visibility.PRIVATE)
        self.client.force_login(self.viewer)
        r = self.client.get(self.father_url)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Дорж")
        self.assertContains(r, "хаалттай")
        self.assertNotContains(r, "1950")
        self.assertNotContains(r, "Эрэгтэй")

    def test_private_deceased_parent_has_no_grey_styling(self):
        """«Нас барсан» гэдэг нь өөрөө мэдээлэл — саарал өнгөөр задруулахгүй."""
        self._set(Person.Visibility.PRIVATE)
        Person.objects.filter(pk=self.father.pk).update(living_override=False)
        r = self._profile(self.viewer)
        self.assertContains(r, "Дорж")
        self.assertNotContains(r, "text-secondary")

    # -- «Хаалттай» --------------------------------------------------------

    def test_closed_parent_is_placeholder_for_viewer(self):
        self._set(Person.Visibility.CLOSED)
        r = self._profile(self.viewer)
        self.assertContains(r, "•••")
        self.assertContains(r, "(хаалттай)")        # шалтгаан нь харагдана
        self.assertNotContains(r, "Дорж")
        self.assertNotContains(r, self.father_url)
        self.assertNotContains(r, "1950")

    def test_closed_parent_is_placeholder_for_editor_too(self):
        """«Хаалттай» нь засварлагчид ч хаалттай — зөвхөн админ харна."""
        self._set(Person.Visibility.CLOSED)
        r = self._profile(self.editor)
        self.assertContains(r, "•••")
        self.assertNotContains(r, "Дорж")

    def test_admin_sees_closed_parent(self):
        self._set(Person.Visibility.CLOSED)
        r = self._profile(self.admin)
        self.assertContains(r, "Дорж")
        self.assertContains(r, self.father_url)

    def test_closed_node_is_labelled_in_the_tree(self):
        """Модон дээр «Хаалттай» зангилаа «(хаалттай)» гэж харагдана."""
        self._set(Person.Visibility.CLOSED)
        self.client.force_login(self.viewer)
        data = self.client.get(
            reverse("genealogy:tree_data", args=[self.child.pk])
            + "?mode=ancestors&depth=3").json()
        node = next(n for n in data["nodes"] if n["id"] == self.father.pk)
        self.assertTrue(node["hidden"])
        self.assertTrue(node["closed"])

    def test_admin_sees_no_closed_placeholder(self):
        """Админд «Хаалттай» гэж байхгүй — зангилаа бүтэн харагдана."""
        self._set(Person.Visibility.CLOSED)
        self.client.force_login(self.admin)
        data = self.client.get(
            reverse("genealogy:tree_data", args=[self.child.pk])
            + "?mode=ancestors&depth=3").json()
        node = next(n for n in data["nodes"] if n["id"] == self.father.pk)
        self.assertFalse(node["hidden"])

    def test_closed_person_absent_from_search(self):
        self._set(Person.Visibility.CLOSED)
        self.client.force_login(self.viewer)
        r = self.client.get(reverse("genealogy:search"), {"q": "Дорж"})
        self.assertNotContains(r, self.father_url)

    def test_private_person_found_in_search_without_age(self):
        self._set(Person.Visibility.PRIVATE)
        self.client.force_login(self.viewer)
        r = self.client.get(reverse("genealogy:search"), {"q": "Дорж"})
        self.assertContains(r, self.father_url)
        self.assertNotContains(r, "1950")

    def test_closed_person_not_editable_by_editor(self):
        self._set(Person.Visibility.CLOSED)
        self.assertFalse(self.father.can_edit_by(self.editor))
        self.assertNotIn(self.father.pk, set(
            Person.objects.editable_by(self.editor).values_list("pk", flat=True)))
        self.assertTrue(self.father.can_edit_by(self.admin))

    def test_linked_viewer_keeps_seeing_own_closed_record(self):
        """Холбогдсон хүн өөрийн бичлэгээ «Хаалттай» байсан ч харна."""
        Person.objects.filter(pk=self.child.pk).update(
            visibility=Person.Visibility.CLOSED, linked_user=self.viewer)
        self.child.refresh_from_db()
        self.assertTrue(self.child.can_view(self.viewer))
        self.assertTrue(self.child.can_see_details(self.viewer))

    def test_visible_to_matches_can_view_for_closed(self):
        """`visible_to` ↔ `can_view` салж болохгүй (query ба объект хоёр зам)."""
        self._set(Person.Visibility.CLOSED)
        for user in (self.viewer, self.editor, self.admin):
            with self.subTest(user=user.role):
                in_qs = Person.objects.visible_to(user).filter(pk=self.father.pk).exists()
                self.assertEqual(in_qs, self.father.can_view(user))


class ParentRoleTagTests(TreeBuilderMixin, TestCase):
    """Профайл дээр эцэг эх бүрд тодруулга: (эцэг), (эх), (өргөж авсан эцэг)…"""

    def setUp(self):
        self.build_family()
        self.editor = User.objects.create_user(email="role-ed@example.com", password="x",
                                               role=User.Role.EDITOR)
        self.viewer = User.objects.create_user(email="role-viewer@example.com", password="x",
                                               role=User.Role.VIEWER)
        self.url = reverse("genealogy:person_detail", args=[self.child.pk])

    def test_birth_parents_are_labelled(self):
        """«Эцэг эх: Дорж, Санга» гэвэл хэн нь эцэг, хэн нь эх нь мэдэгдэхгүй."""
        self.client.force_login(self.editor)
        r = self.client.get(self.url)
        self.assertContains(r, "(эцэг)")
        self.assertContains(r, "(эх)")

    def test_sex_is_inferred_from_the_partner(self):
        """Хүйс тодорхойгүй эцэг хамтрагчаасаа таагдана."""
        Person.objects.filter(pk=self.father.pk).update(sex=Person.Sex.UNKNOWN)
        self.client.force_login(self.editor)
        self.assertContains(self.client.get(self.url), "(эцэг)")

    def test_adoptive_parent_is_labelled(self):
        adopter = Person.objects.create(given_name="ӨргөмөлЭцэг", sex=Person.Sex.MALE)
        family = Family.objects.create(partner1=adopter)
        ChildLink.objects.create(family=family, child=self.child,
                                 pedigree=ChildLink.Pedigree.ADOPTED)
        self.client.force_login(self.editor)
        self.assertContains(self.client.get(self.url), "(өргөж авсан эцэг)")

    def test_unknown_sex_falls_back_to_neutral_tag(self):
        """Хүйс таагдахгүй бол «эцэг эх» — буруу талд хамааруулахгүй."""
        Person.objects.filter(pk__in=[self.father.pk, self.mother.pk]).update(
            sex=Person.Sex.UNKNOWN)
        self.client.force_login(self.editor)
        r = self.client.get(self.url)
        self.assertContains(r, "(эцэг эх)")
        self.assertNotContains(r, "(эцэг), ")     # «(эцэг)» гэж тодорхойлоогүй

    def test_masked_parent_has_no_role_tag(self):
        """«Хувийн» хүний хүйс хаалттай тул тодруулга өгөхгүй."""
        Person.objects.filter(pk=self.father.pk).update(
            visibility=Person.Visibility.PRIVATE)
        self.client.force_login(self.viewer)
        r = self.client.get(self.url)
        self.assertContains(r, "(хувийн)")
        self.assertNotContains(r, "(эцэг)")

    def test_closed_parent_has_no_role_tag(self):
        Person.objects.filter(pk=self.father.pk).update(
            visibility=Person.Visibility.CLOSED)
        self.client.force_login(self.editor)
        r = self.client.get(self.url)
        self.assertContains(r, "••• (хаалттай)")
        self.assertNotContains(r, "(эцэг)")


class ClanNameSuggestionTests(TreeBuilderMixin, TestCase):
    """Ургийн овог нь эцгээсээ САНАЛ БОЛОГДОНО — автоматаар бичигдэхгүй."""

    def setUp(self):
        self.build_family()
        self.editor = User.objects.create_user(email="clan@example.com", password="x",
                                               role=User.Role.EDITOR)
        Person.objects.filter(pk=self.father.pk).update(clan_name="Боржигон")
        self.father.refresh_from_db()

    # -- эцгийг олох -------------------------------------------------------

    def test_father_is_resolved_by_sex_not_slot(self):
        """Эцэг нь ХҮЙСЭЭР олдоно — partner1/partner2 байрлалаар биш."""
        Family.objects.filter(pk=self.parent_family.pk).update(
            partner1=self.mother, partner2=self.father)
        self.child.refresh_from_db()
        self.assertEqual(self.child.father, self.father)

    def test_no_father_when_sex_unknown(self):
        """Хүйс нь тодорхойгүй бол таахгүй — буруу овог санал болгохоос сэргийлнэ."""
        Person.objects.filter(pk=self.father.pk).update(sex=Person.Sex.UNKNOWN)
        self.child.refresh_from_db()
        self.assertIsNone(self.child.father)

    # -- санал болгох ------------------------------------------------------

    def test_suggestion_for_child_uses_father(self):
        self.assertEqual(suggested_clan_name(self.father, "child"), "Боржигон")

    def test_suggestion_for_child_of_mother_uses_her_partner(self):
        """Эхэд хүүхэд нэмэхэд эцэг нь түүний хос — тэрний овог санал болгоно."""
        self.assertEqual(suggested_clan_name(self.mother, "child"), "Боржигон")

    def test_suggestion_for_sibling_uses_anchor_father(self):
        self.assertEqual(suggested_clan_name(self.child, "sibling"), "Боржигон")

    def test_no_suggestion_without_a_father(self):
        orphan = Person.objects.create(given_name="Өнчин", sex=Person.Sex.MALE)
        self.assertEqual(suggested_clan_name(orphan, "sibling"), "")

    def test_no_suggestion_for_partner(self):
        """Эхнэр/нөхөр нь өөр овогтой — санал болгохгүй."""
        self.assertEqual(suggested_clan_name(self.father, "partner"), "")

    # -- автоматаар бичихгүй ----------------------------------------------

    def test_creating_a_child_does_not_autofill(self):
        """Эцэг нь овогтой ч шинэ хүний овог хоосон үлдэнэ (санал л болно)."""
        child = create_relative(self.father, "child", given_name="Хүү", user=self.editor)
        self.assertEqual(child.clan_name, "")

    def test_explicit_clan_name_is_kept(self):
        child = create_relative(self.father, "child", given_name="Хүү",
                                clan_name="Хатагин", user=self.editor)
        self.assertEqual(child.clan_name, "Хатагин")

    # -- форм дээр ---------------------------------------------------------

    def test_edit_form_prefills_father_clan_name(self):
        """Овог хоосон бол форм дээр эцгийн овог урьдчилан бөглөгдөнө."""
        form = PersonForm(instance=self.child)
        self.assertEqual(form["clan_name"].value(), "Боржигон")
        self.assertIn("Боржигон", form.fields["clan_name"].help_text)
        self.assertIn("сольж болно", form.fields["clan_name"].help_text)

    def test_edit_form_keeps_own_clan_name(self):
        """Өөрийн овогтой бол санал хамаарахгүй."""
        Person.objects.filter(pk=self.child.pk).update(clan_name="Хатагин")
        self.child.refresh_from_db()
        form = PersonForm(instance=self.child)
        self.assertEqual(form["clan_name"].value(), "Хатагин")
        self.assertNotIn("Боржигон", form.fields["clan_name"].help_text or "")

    def test_saving_an_empty_clan_name_stays_empty(self):
        """Санал бол зөвхөн санал — хадгалахад автоматаар бичигдэхгүй."""
        form = PersonForm(data={
            "clan_name": "", "given_name": self.child.given_name, "sex": self.child.sex,
            "visibility": Person.Visibility.AUTO, "living_override": "",
            "birth_date": "", "death_date": "",
        }, instance=self.child)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().clan_name, "")

    def test_quick_add_modal_prefills_suggestion(self):
        """Модны «+» цонхон дээр эцгийн овог санал болгогдоно."""
        self.client.force_login(self.editor)
        r = self.client.get(reverse("genealogy:quick_add",
                                    args=[self.father.pk, "child"]),
                            HTTP_HX_REQUEST="true")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Боржигон")
        self.assertContains(r, "сольж болно")

    # -- нэг удаагийн нөхөлт ----------------------------------------------

    def test_inherit_clan_name_is_the_one_off_backfill(self):
        self.assertTrue(self.child.inherit_clan_name())
        self.child.refresh_from_db()
        self.assertEqual(self.child.clan_name, "Боржигон")
        self.assertIn("боржигон", self.child.search_text)
        # Хоёр дахь удаа нөхөхгүй (аль хэдийн бичээстэй).
        self.assertFalse(self.child.inherit_clan_name())

    def test_command_reports_and_applies(self):
        out = StringIO()
        call_command("inherit_clan_names", stdout=out)
        self.assertIn("Боржигон", out.getvalue())
        self.assertIn("--apply", out.getvalue())          # анхдагчаар бичихгүй
        self.child.refresh_from_db()
        self.assertEqual(self.child.clan_name, "")        # хараахан бичээгүй

        call_command("inherit_clan_names", "--apply", stdout=out)
        self.child.refresh_from_db()
        self.assertEqual(self.child.clan_name, "Боржигон")


class MergeTests(TreeBuilderMixin, TestCase):
    def setUp(self):
        self.build_family()

    def test_merge_moves_relations_and_keeps_record(self):
        keep = Person.objects.create(given_name="Нэгтгэх")
        dup = Person.objects.create(given_name="Давхардал")
        family = Family.objects.create(partner1=dup)
        child = Person.objects.create(given_name="Хүүхэд2")
        ChildLink.objects.create(family=family, child=child)
        EventParticipant.objects.create(
            event=Event.objects.create(event_type=Event.EventType.OCCUPATION),
            person=dup, role=EventParticipant.Role.PRINCIPAL,
        )

        merge_persons(keep, dup)
        dup.refresh_from_db()
        family.refresh_from_db()

        self.assertEqual(dup.merged_into_id, keep.pk)   # устгагдаагүй
        self.assertEqual(family.partner1_id, keep.pk)   # холбоо шилжсэн
        self.assertEqual(dup.event_participations.count(), 0)
        self.assertEqual(keep.event_participations.count(), 1)

    def test_merge_is_idempotent(self):
        keep = Person.objects.create(given_name="А")
        dup = Person.objects.create(given_name="Б")
        merge_persons(keep, dup)
        merge_persons(keep, dup)  # алдаа өгөхгүй
        dup.refresh_from_db()
        self.assertEqual(dup.merged_into_id, keep.pk)

    def test_merge_fills_empty_fields(self):
        keep = Person.objects.create(given_name="А")
        dup = Person.objects.create(given_name="Б", clan_name="Боржигон", bio="Намтар")
        merge_persons(keep, dup)
        keep.refresh_from_db()
        self.assertEqual(keep.clan_name, "Боржигон")

    def test_merge_rejects_self(self):
        keep = Person.objects.create(given_name="А")
        with self.assertRaises(ValueError):
            merge_persons(keep, keep)


class SearchTests(TestCase):
    def test_alternative_names_are_searchable(self):
        person = Person.objects.create(given_name="Ганбат", patronymic="Бат-Эрдэнийн")
        PersonName.objects.create(person=person, name_type=PersonName.NameType.MARRIED,
                                  given_name="Дулмаа")
        person.refresh_from_db()
        self.assertIn("дулмаа", person.search_text)

    def test_search_matches_partial_cyrillic(self):
        Person.objects.create(given_name="Ганбат", patronymic="Бат-Эрдэнийн")
        self.assertEqual(Person.objects.search("Ганб").count(), 1)
        self.assertEqual(Person.objects.search("Бат-Эрд").count(), 1)

    def test_search_respects_merge(self):
        keep = Person.objects.create(given_name="Ганбат")
        dup = Person.objects.create(given_name="Ганбат")
        merge_persons(keep, dup)
        self.assertEqual(Person.objects.search("Ганбат").count(), 1)


class QuickAddTests(TreeBuilderMixin, TestCase):
    def setUp(self):
        self.build_family()

    def test_add_father_when_unknown(self):
        orphan = Person.objects.create(given_name="Өнчин")
        father = create_relative(orphan, "father", given_name="Эцэг нь")
        self.assertEqual(orphan.parents, [father])

    def test_add_child(self):
        new_child = create_relative(self.child, "child", given_name="Шинэ хүүхэд")
        self.assertIn(self.child.pk, [p.pk for p in new_child.parents])

    def test_add_partner(self):
        partner = create_relative(self.child, "partner", given_name="Эхнэр")
        self.assertEqual(self.child.families.first().partner2, partner)

    def test_add_sibling_shares_family(self):
        sibling = create_relative(self.child, "sibling", given_name="Ах")
        self.assertEqual(sibling.birth_family, self.child.birth_family)

    def test_adding_third_parent_raises(self):
        with self.assertRaises(ValueError):
            create_relative(self.child, "father", given_name="Гурав дахь эцэг")


class GraphTests(TreeBuilderMixin, TestCase):
    def setUp(self):
        self.build_family()
        self.editor = User.objects.create_user(email="graph@example.com", password="x",
                                               role=User.Role.EDITOR)

    def test_graph_shape(self):
        data = build_graph(self.child, mode="ancestors", max_depth=3, user=self.editor)
        ids = {n["id"] for n in data["nodes"]}
        self.assertIn(self.child.pk, ids)
        self.assertIn(self.grandpa.pk, ids)
        self.assertTrue(any(n["root"] for n in data["nodes"]))

    def test_graph_nodes_carry_relation_label(self):
        """Зангилаа бүр төвийн хүнтэй харьцах хамаатнаа хаалтанд харуулна."""
        data = build_graph(self.child, mode="ancestors", max_depth=3, user=self.editor)
        by_id = {n["id"]: n for n in data["nodes"]}
        self.assertEqual(by_id[self.father.pk]["relation"], "эцэг")
        self.assertEqual(by_id[self.mother.pk]["relation"], "эх")
        self.assertEqual(by_id[self.grandpa.pk]["relation"], "өвөг эцэг")
        self.assertEqual(by_id[self.grandma.pk]["relation"], "өвөг эх")
        self.assertIsNone(by_id[self.child.pk]["relation"])  # root өөрөө
        self.assertTrue(all("relation" in n for n in data["nodes"]))

    def test_graph_descendant_relation_labels(self):
        data = build_graph(self.grandpa, mode="descendants", max_depth=3, user=self.editor)
        by_id = {n["id"]: n for n in data["nodes"]}
        self.assertEqual(by_id[self.father.pk]["relation"], "хүү")
        self.assertEqual(by_id[self.child.pk]["relation"], "ач/зээ")
        self.assertEqual(by_id[self.grandchild.pk]["relation"], "жич")

    def test_graph_hidden_node_has_no_relation(self):
        """«Хаалттай» хүний нэр ч, хамаатан ч харагдахгүй (зөвхөн админ)."""
        Person.objects.filter(pk=self.grandpa.pk).update(visibility=Person.Visibility.CLOSED)
        viewer = User.objects.create_user(email="hidden-rel@example.com", password="x")
        data = build_graph(self.child, mode="ancestors", max_depth=3, user=viewer)
        hidden = next(n for n in data["nodes"] if n["id"] == self.grandpa.pk)
        self.assertTrue(hidden["hidden"])
        self.assertIsNone(hidden["relation"])

    def test_graph_masks_private_person_details(self):
        """«Хувийн» хүн: нэр харагдана, нас/хүйс/хамаатан хаалттай."""
        Person.objects.filter(pk=self.grandpa.pk).update(
            visibility=Person.Visibility.PRIVATE,
            birth_date=FlexDate.create_from_text("1880"))
        viewer = User.objects.create_user(email="private-node@example.com", password="x")
        data = build_graph(self.child, mode="ancestors", max_depth=3, user=viewer)
        node = next(n for n in data["nodes"] if n["id"] == self.grandpa.pk)

        self.assertFalse(node["hidden"])
        self.assertTrue(node["masked"])
        self.assertIn("Өвөг", node["name"])       # нэр нь ХАРАГДАНА
        self.assertIsNone(node["relation"])       # «өвөг эцэг» нь хүйсийг задруулна
        self.assertEqual(node["lifespan"], "")
        self.assertIsNone(node["birth"])
        self.assertIsNone(node["death"])
        self.assertEqual(node["sex"], "U")
        # Замын код: аав (харагдах) → «А», өвөг (хаалттай) → «·».
        self.assertEqual(node["path_code"], "А·")

    def test_graph_masked_person_does_not_leak_sex_via_partner(self):
        """Хамтрагчийн хүйсээр дамжин хаалттай хүний хүйс алдрахгүй."""
        Person.objects.filter(pk=self.grandpa.pk).update(
            visibility=Person.Visibility.PRIVATE)
        Person.objects.filter(pk=self.grandma.pk).update(sex=Person.Sex.UNKNOWN)
        viewer = User.objects.create_user(email="sex-leak@example.com", password="x")
        data = build_graph(self.child, mode="ancestors", max_depth=3, user=viewer)
        by_id = {n["id"]: n for n in data["nodes"]}
        self.assertEqual(by_id[self.grandpa.pk]["sex"], "U")
        # Эмээгийн хүйс өвөөгөөс (хаалттай) таагдахгүй — тодорхойгүй хэвээр.
        self.assertEqual(by_id[self.grandma.pk]["sex"], "U")

    def test_graph_root_is_present_for_editor(self):
        data = build_graph(self.grandpa, mode="descendants", max_depth=3, user=self.editor)
        root_nodes = [n for n in data["nodes"] if n["root"]]
        self.assertEqual(len(root_nodes), 1)
        self.assertFalse(root_nodes[0]["hidden"])

    def test_graph_closed_person_hidden_even_from_editor(self):
        """«Хаалттай» хүнийг засварлагч ч харахгүй — зөвхөн админ."""
        Person.objects.filter(pk=self.grandpa.pk).update(visibility=Person.Visibility.CLOSED)
        data = build_graph(self.child, mode="ancestors", max_depth=3, user=self.editor)
        node = next(n for n in data["nodes"] if n["id"] == self.grandpa.pk)
        self.assertTrue(node["hidden"])
        self.assertEqual(node["name"], "•••")
        self.assertEqual(node["lifespan"], "")

    def test_graph_root_visible_to_anonymous_when_deceased(self):
        """Нас барсан үндэс нь анонимд харагдана (AUTO visibility)."""
        Person.objects.filter(pk=self.grandpa.pk).update(
            birth_date=FlexDate.create_from_text("1850")
        )
        data = build_graph(self.grandpa, mode="descendants", max_depth=1)
        root = next(n for n in data["nodes"] if n["root"])
        self.assertFalse(root["hidden"])

    def test_graph_excludes_merged(self):
        dup = Person.objects.create(given_name="Давхардал")
        ChildLink.objects.create(family=self.parent_family, child=dup)
        merge_persons(self.child, dup)
        data = build_graph(self.father, mode="descendants", max_depth=2, user=self.editor)
        self.assertNotIn(dup.pk, {n["id"] for n in data["nodes"]})


class MissingParentSuggestionTests(TreeBuilderMixin, TestCase):
    """«Дутуу холбоос» — дутуу эцэг/эх илрүүлэх ба кандидат санал болгох."""

    def setUp(self):
        self.build_family()
        self.editor = User.objects.create_user(email="suggest@example.com", password="x",
                                               role=User.Role.EDITOR)

    def _child_with(self, **partners):
        family = Family.objects.create(**partners)
        child = Person.objects.create(given_name="Хүүхэд", sex=Person.Sex.MALE)
        ChildLink.objects.create(family=family, child=child)
        return child

    @staticmethod
    def _pks(slot):
        return [c["person"].pk for c in slot["candidates"]]

    # --- аль үүр дутуу вэ -------------------------------------------------
    def test_slots_when_no_birth_family(self):
        orphan = Person.objects.create(given_name="Өнчин", sex=Person.Sex.MALE)
        self.assertEqual(missing_parent_slots(orphan), ["father", "mother"])

    def test_slots_when_only_father_known(self):
        child = self._child_with(partner1=self.father)
        self.assertEqual(missing_parent_slots(child), ["mother"])

    def test_slots_when_only_mother_known(self):
        child = self._child_with(partner2=self.mother)
        self.assertEqual(missing_parent_slots(child), ["father"])

    def test_slots_empty_when_both_parents_known(self):
        self.assertEqual(missing_parent_slots(self.child), [])

    def test_slots_unknown_sex_partner_offers_both(self):
        """Ганц эцэг/эх байгаа ч хүйс нь тодорхойгүй — аль нь дутуу нь мэдэгдэхгүй."""
        child = self._child_with(partner1=Person.objects.create(given_name="Хүйсгүй"))
        self.assertEqual(missing_parent_slots(child), ["father", "mother"])

    def test_ambiguous_flag_set_for_unknown_sex(self):
        child = self._child_with(partner1=Person.objects.create(given_name="Хүйсгүй"))
        self.assertTrue(all(s["ambiguous"] for s in suggest_parents(child, self.editor)))

    # --- кандидатын эрэмбэлэлт --------------------------------------------
    def test_patronymic_match_ranks_first(self):
        """Монголд эцгийн нэр = ЭЦГИЙН өөрийн нэр — хамгийн хүчтэй дохио."""
        orphan = Person.objects.create(given_name="Бат", patronymic="Балхүү",
                                       sex=Person.Sex.MALE)
        Person.objects.create(given_name="Болд", sex=Person.Sex.MALE)   # зөвхөн хүйс таарна
        named_after = Person.objects.create(given_name="Балхүү", sex=Person.Sex.UNKNOWN)
        slot = next(s for s in suggest_parents(orphan, self.editor) if s["role"] == "father")
        self.assertEqual(slot["candidates"][0]["person"].pk, named_after.pk)

    def test_candidates_empty_when_no_slots(self):
        self.assertEqual(suggest_parents(self.child, self.editor), [])

    def test_self_never_suggested(self):
        for slot in suggest_parents(self.grandpa, self.editor):
            self.assertNotIn(self.grandpa.pk, self._pks(slot))

    def test_candidates_exclude_descendants(self):
        """Өөрийн үр хойчсыг эцэг болговол цикл үүснэ — санал болгож болохгүй."""
        blocked = {self.father.pk, self.child.pk, self.grandchild.pk}
        slot = next(s for s in suggest_parents(self.grandpa, self.editor) if s["role"] == "father")
        self.assertFalse(blocked & set(self._pks(slot)))

    def test_candidates_exclude_existing_partner(self):
        child = self._child_with(partner1=self.father)
        slot = next(s for s in suggest_parents(child, self.editor) if s["role"] == "mother")
        self.assertNotIn(self.father.pk, self._pks(slot))

    def test_unrelated_stranger_not_suggested(self):
        """Ямар ч дохиогүй хүн (нэр, овог, огноо, хүйс таарахгүй) санал болохгүй."""
        stranger = Person.objects.create(given_name="Танихгүй", sex=Person.Sex.FEMALE)
        orphan = Person.objects.create(given_name="Өнчин", sex=Person.Sex.MALE)
        slot = next(s for s in suggest_parents(orphan, self.editor) if s["role"] == "father")
        self.assertNotIn(stranger.pk, self._pks(slot))

    def test_sex_only_match_is_not_suggested(self):
        """Зөвхөн хүйс таарна гэдэг нь дохио биш — нэр эсвэл он жил хэрэгтэй."""
        orphan = Person.objects.create(given_name="Өнчин", sex=Person.Sex.MALE)
        Person.objects.create(given_name="Эрэгтэй", sex=Person.Sex.MALE)
        slot = next(s for s in suggest_parents(orphan, self.editor) if s["role"] == "father")
        self.assertEqual(slot["candidates"], [])

    def test_plausible_birth_year_alone_is_enough(self):
        """Он жилийн зөрүү боломжит хэмжээнд байвал хүйс таарсан хүн санал болно."""
        orphan = Person.objects.create(given_name="Өнчин", sex=Person.Sex.MALE,
                                       birth_date=FlexDate.create_from_text("1950"))
        candidate = Person.objects.create(given_name="Эрэгтэй", sex=Person.Sex.MALE,
                                          birth_date=FlexDate.create_from_text("1920"))
        slot = next(s for s in suggest_parents(orphan, self.editor) if s["role"] == "father")
        self.assertIn(candidate.pk, self._pks(slot))

    def test_candidate_born_after_child_is_excluded(self):
        """Хүүхдээс хойш төрсөн хүн эцэг эх болж чадахгүй."""
        child = Person.objects.create(given_name="Хүүхэд", sex=Person.Sex.MALE,
                                      clan_name="Санга",
                                      birth_date=FlexDate.create_from_text("1950"))
        too_young = Person.objects.create(given_name="Залуу", sex=Person.Sex.MALE,
                                          clan_name="Санга",
                                          birth_date=FlexDate.create_from_text("1990"))
        slot = next(s for s in suggest_parents(child, self.editor) if s["role"] == "father")
        self.assertNotIn(too_young.pk, self._pks(slot))

    def test_mother_ignores_clan_match(self):
        """Хүүхдийн овог ЭЦГИЙНх тул эхэд овгийн таарал дохио болохгүй."""
        child = Person.objects.create(given_name="Хүүхэд", sex=Person.Sex.MALE,
                                      clan_name="Санга")
        Person.objects.create(given_name="Эмэгтэй", sex=Person.Sex.FEMALE, clan_name="Санга")
        slot = next(s for s in suggest_parents(child, self.editor) if s["role"] == "mother")
        self.assertEqual(slot["candidates"], [])

    # --- ах дүүгийн бүртгэсэн эцэг/эх (хамгийн хүчтэй сэжүүр) --------------
    def test_sibling_parent_is_suggested_first(self):
        """Эцэг нэгтэй ах дүүгийн бүртгэсэн эх нь хамгийн түрүүнд санал болно."""
        father = Person.objects.create(given_name="Эцэг", sex=Person.Sex.MALE)
        mother = Person.objects.create(given_name="Эх", sex=Person.Sex.FEMALE)
        with_mother = Family.objects.create(partner1=father, partner2=mother)
        sibling = Person.objects.create(given_name="Ах")
        ChildLink.objects.create(family=with_mother, child=sibling)

        without = Family.objects.create(partner1=father)
        child = Person.objects.create(given_name="Дүү", sex=Person.Sex.MALE)
        ChildLink.objects.create(family=without, child=child)

        slot = next(s for s in suggest_parents(child, self.editor) if s["role"] == "mother")
        self.assertEqual(slot["candidates"][0]["person"].pk, mother.pk)
        self.assertIn("ах дүүгийн эх", slot["candidates"][0]["reason"])

    def test_sibling_candidates_exclude_own_parent_and_self(self):
        father = Person.objects.create(given_name="Эцэг", sex=Person.Sex.MALE)
        mother = Person.objects.create(given_name="Эх", sex=Person.Sex.FEMALE)
        Family.objects.create(partner1=father, partner2=mother)
        without = Family.objects.create(partner1=father)
        child = Person.objects.create(given_name="Хүүхэд")
        ChildLink.objects.create(family=without, child=child)

        got = [p.pk for p in sibling_parent_candidates(child)]
        self.assertEqual(got, [mother.pk])
        self.assertNotIn(father.pk, got)
        self.assertNotIn(child.pk, got)

    def test_sibling_candidates_empty_when_both_parents_known(self):
        self.assertEqual(sibling_parent_candidates(self.child), [])

    def test_sibling_candidates_empty_without_birth_family(self):
        orphan = Person.objects.create(given_name="Өнчин")
        self.assertEqual(sibling_parent_candidates(orphan), [])

    def test_candidates_capped_at_limit(self):
        orphan = Person.objects.create(given_name="Өнчин", sex=Person.Sex.MALE,
                                       birth_date=FlexDate.create_from_text("1950"))
        for i in range(8):
            Person.objects.create(given_name=f"Эрэгтэй{i}", sex=Person.Sex.MALE,
                                  birth_date=FlexDate.create_from_text("1920"))
        slot = next(s for s in suggest_parents(orphan, self.editor, limit=3)
                    if s["role"] == "father")
        self.assertEqual(len(slot["candidates"]), 3)

    # --- профайл ба эрх ----------------------------------------------------
    def test_profile_shows_missing_links_for_editor(self):
        orphan = Person.objects.create(given_name="Өнчин", sex=Person.Sex.MALE)
        self.client.force_login(self.editor)
        r = self.client.get(reverse("genealogy:person_detail", args=[orphan.pk]))
        self.assertContains(r, "Дутуу холбоос")
        self.assertContains(r, "бүртгэгдээгүй")

    def test_profile_renders_candidate_chip(self):
        """Кандидатын чип template дээр зөв зурагдаж, урьдчилан сонгох линк агуулна."""
        father = Person.objects.create(given_name="Эцэг", sex=Person.Sex.MALE)
        mother = Person.objects.create(given_name="Эх", sex=Person.Sex.FEMALE)
        with_mother = Family.objects.create(partner1=father, partner2=mother)
        ChildLink.objects.create(family=with_mother,
                                 child=Person.objects.create(given_name="Ах"))
        without = Family.objects.create(partner1=father)
        child = Person.objects.create(given_name="Дүү", sex=Person.Sex.MALE)
        ChildLink.objects.create(family=without, child=child)

        self.client.force_login(self.editor)
        r = self.client.get(reverse("genealogy:person_detail", args=[child.pk]))
        self.assertContains(r, "ах дүүгийн эх")
        self.assertContains(r, f"?person={mother.pk}")

    def test_profile_hides_missing_links_from_viewer(self):
        orphan = Person.objects.create(given_name="Өнчин", sex=Person.Sex.MALE)
        viewer = User.objects.create_user(email="plainviewer@example.com", password="x")
        self.client.force_login(viewer)
        r = self.client.get(reverse("genealogy:person_detail", args=[orphan.pk]))
        self.assertNotContains(r, "Дутуу холбоос")

    def test_profile_hides_block_when_both_parents_known(self):
        self.client.force_login(self.editor)
        r = self.client.get(reverse("genealogy:person_detail", args=[self.child.pk]))
        self.assertNotContains(r, "Дутуу холбоос")

    def test_link_modal_passes_selected_to_picker(self):
        """Кандидат дээр дарахад баталгаажуулах цонх тэр хүнийг сонгосон байдлаар нээгдэнэ."""
        self.client.force_login(self.editor)
        r = self.client.get(
            reverse("genealogy:link_relative", args=[self.grandpa.pk, "father"]),
            {"person": self.father.pk})
        self.assertContains(r, f"selected={self.father.pk}")

    def test_preselected_person_is_checked_in_picker(self):
        orphan = Person.objects.create(given_name="Өнчин", sex=Person.Sex.MALE)
        self.client.force_login(self.editor)
        r = self.client.get(reverse("genealogy:person_picker"),
                            {"anchor": orphan.pk, "relation": "father",
                             "selected": self.grandpa.pk})
        self.assertRegex(r.content.decode(),
                         rf'value="{self.grandpa.pk}"[^>]*checked')


class SiblingAcrossFamiliesTests(TreeBuilderMixin, TestCase):
    """Нэг хос хэд хэдэн `Family` мөрөнд бүртгэгдсэн бол ах дүүс бүгд харагдана."""

    def setUp(self):
        self.build_family()
        self.editor = User.objects.create_user(email="sib@example.com", password="x",
                                               role=User.Role.EDITOR)

    def test_siblings_found_across_duplicate_family_rows(self):
        other_family = Family.objects.create(partner1=self.father, partner2=self.mother)
        brother = Person.objects.create(given_name="Ах")
        sister = Person.objects.create(given_name="ОхинТест", sex=Person.Sex.FEMALE)
        ChildLink.objects.create(family=other_family, child=brother)
        ChildLink.objects.create(family=other_family, child=sister)

        self.client.force_login(self.editor)
        r = self.client.get(reverse("genealogy:person_detail", args=[brother.pk]))
        # Ижил эцэг эхтэй ч өөр Family мөрөнд байгаа эгч нь харагдах ёстой
        self.assertContains(r, "ОхинТест")

    def test_half_sibling_through_parent_other_marriage(self):
        """Эцгийн өөр гэрлэлтээс төрсөн хүүхэд ч эцэг нэгтэй ах дүү болно."""
        other_mother = Person.objects.create(given_name="ӨөрЭх", sex=Person.Sex.FEMALE)
        other_family = Family.objects.create(partner1=self.father, partner2=other_mother)
        half = Person.objects.create(given_name="ХагасДүү")
        ChildLink.objects.create(family=other_family, child=half)

        self.client.force_login(self.editor)
        r = self.client.get(reverse("genealogy:person_detail", args=[self.child.pk]))
        self.assertContains(r, "ХагасДүү")

    def test_self_not_listed_as_own_sibling(self):
        sibling = Person.objects.create(given_name="АхТест")
        ChildLink.objects.create(family=self.parent_family, child=sibling)

        self.client.force_login(self.editor)
        r = self.client.get(reverse("genealogy:person_detail", args=[self.child.pk]))
        self.assertContains(r, "Ах/эгч/дүү:")          # «Эцэг эхн ах/эгч/дүү» биш
        self.assertNotContains(r, "Эцэг эхн ах/эгч/дүү")
        sib_section = r.content.decode().split("Ах/эгч/дүү:")[1].split("</li>")[0]
        self.assertIn("АхТест", sib_section)
        self.assertNotIn("Хүүхэд", sib_section)   # өөрөө өөрийнхөө ах дүү биш

    def test_adopted_sibling_group_is_labelled(self):
        """Өргөмөл эцэг эхийн талын ах дүү нь тусдаа шошготой.

        Бүлэг нь ХҮНИЙ өөрийнх нь эцэг эхийн холбоогоор тодорхойлогддог тул
        (ах дүүгийнх нь биш) харагдаж буй хүнд өргөмөл эцэг эх байх ёстой.
        """
        adopter = Person.objects.create(given_name="ӨргөмөлЭцэг", sex=Person.Sex.MALE)
        ad_family = Family.objects.create(partner1=adopter)
        ChildLink.objects.create(family=ad_family, child=self.child,
                                 pedigree=ChildLink.Pedigree.ADOPTED)
        sibling = Person.objects.create(given_name="ӨргөмөлАх")
        ChildLink.objects.create(family=ad_family, child=sibling,
                                 pedigree=ChildLink.Pedigree.ADOPTED)

        self.client.force_login(self.editor)
        r = self.client.get(reverse("genealogy:person_detail", args=[self.child.pk]))
        self.assertContains(r, "Өргөмөл ах/эгч/дүү:")


class AncestorPathCodeTests(TreeBuilderMixin, TestCase):
    """Өвгийн замын «А/Э» код: аав → А, ээж → Э (root-оос дээшээ)."""

    def setUp(self):
        self.build_family()
        self.editor = User.objects.create_user(email="path@example.com", password="x",
                                               role=User.Role.EDITOR)
        self.viewer = User.objects.create_user(email="path-viewer@example.com", password="x",
                                               role=User.Role.VIEWER)

    def _codes(self, person=None, mode="ancestors", max_depth=3, user=None):
        data = build_graph(person or self.child, mode=mode, max_depth=max_depth,
                           user=user or self.editor)
        return {n["id"]: n["path_code"] for n in data["nodes"]}

    def test_ancestor_paths_trace_from_root(self):
        paths = ancestor_paths(self.child, max_depth=3)
        self.assertEqual(paths[self.father.pk], [self.child.pk, self.father.pk])
        self.assertEqual(paths[self.grandpa.pk],
                         [self.child.pk, self.father.pk, self.grandpa.pk])

    def test_paternal_codes(self):
        codes = self._codes()
        self.assertEqual(codes[self.child.pk], "")        # root — хоосон
        self.assertEqual(codes[self.father.pk], "А")      # аав
        self.assertEqual(codes[self.grandpa.pk], "АА")    # аавын аав
        self.assertEqual(codes[self.grandma.pk], "АЭ")    # аавын ээж

    def test_maternal_grandmother_is_ee(self):
        """Хэрэглэгчийн жишээ: ээжийн ээж → «ЭЭ»."""
        maternal = Person.objects.create(given_name="Ээжийн ээж", sex=Person.Sex.FEMALE)
        family = Family.objects.create(partner2=maternal)
        ChildLink.objects.create(family=family, child=self.mother)

        codes = self._codes()
        self.assertEqual(codes[self.mother.pk], "Э")      # ээж
        self.assertEqual(codes[maternal.pk], "ЭЭ")        # ээжийн ээж

    def test_hidden_intermediate_does_not_leak_sex(self):
        """Нуугдсан завсрын хүний хүйс «·» болно — задруулахгүй."""
        Person.objects.filter(pk=self.father.pk).update(
            visibility=Person.Visibility.PRIVATE)
        codes = self._codes(user=self.viewer)
        self.assertEqual(codes[self.grandpa.pk], "·А")    # аав нуугдсан, өвөг харагдана

    # -- үеийн хязгаар -------------------------------------------------------
    def test_tree_serves_nine_generations_both_ways(self):
        """Мод дээшээ ч, доошоо ч 9 үе хүртэл ажиллана."""
        self.client.force_login(self.editor)
        for mode in ("ancestors", "descendants"):
            with self.subTest(mode=mode):
                r = self.client.get(reverse("genealogy:tree_data", args=[self.child.pk]),
                                    {"mode": mode, "depth": 9})
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.json()["mode"], mode)

    def test_tree_nodes_carry_birth_sort_key(self):
        """Эрэмбэлэх түлхүүр: он байвал он, байхгүй бол 9999 (төгсгөлд)."""
        kid = Person.objects.create(given_name="Ах", sex=Person.Sex.MALE,
                                    birth_date=FlexDate.create_from_text("1985"))
        ChildLink.objects.create(family=self.child_family, child=kid)

        data = build_graph(self.child, mode="descendants", max_depth=1, user=self.editor)
        by_id = {n["id"]: n for n in data["nodes"]}
        self.assertEqual(by_id[kid.pk]["birth_sort"], 1985)
        self.assertEqual(by_id[self.grandchild.pk]["birth_sort"], 9999)   # огноогүй
        self.assertTrue(all("birth_sort" in n for n in data["nodes"]))

    def test_tree_without_pk_centers_on_linked_person(self):
        """«Мод харах» нь холбогдсон хүнийг төвд авчирна."""
        Person.objects.filter(pk=self.grandpa.pk).update(linked_user=self.editor)
        self.client.force_login(self.editor)
        r = self.client.get(reverse("genealogy:tree"))
        self.assertContains(r, f"let ROOT_ID = {self.grandpa.pk};")

    def test_tree_without_pk_falls_back_to_first_person(self):
        """Холбоос байхгүй бол хамгийн эрт бүртгэгдсэн хүн (хуучин зан төлөв)."""
        self.client.force_login(self.editor)
        first = Person.objects.visible_to(self.editor).order_by("pk").first()
        r = self.client.get(reverse("genealogy:tree"))
        self.assertContains(r, f"let ROOT_ID = {first.pk};")

    def test_tree_with_pk_ignores_the_link(self):
        """pk заасан бол холбоос үл хамаарна — тэр хүн л төв болно."""
        Person.objects.filter(pk=self.grandpa.pk).update(linked_user=self.editor)
        self.client.force_login(self.editor)
        r = self.client.get(reverse("genealogy:tree_person", args=[self.child.pk]))
        self.assertContains(r, f"let ROOT_ID = {self.child.pk};")

    def test_linked_closed_person_still_becomes_the_root(self):
        """«Хаалттай» байсан ч холбогдсон хүн өөрт нь төв болж харагдана."""
        Person.objects.filter(pk=self.child.pk).update(
            visibility=Person.Visibility.CLOSED, linked_user=self.editor)
        self.client.force_login(self.editor)
        r = self.client.get(reverse("genealogy:tree"))
        self.assertContains(r, f"let ROOT_ID = {self.child.pk};")

    def test_tree_can_zoom_to_the_root(self):
        """Том мод дээр гол хүнээ ТОМРУУЛААД харах боломжтой.

        «Бүтэн харах» нь 100+ зангилаатай модыг ~10px болгож жижигрүүлдэг тул
        масштаб 1-ээр гол хүн дээр төвлөрөх зам заавал байх ёстой.
        """
        self.client.force_login(self.editor)
        html = self.client.get(
            reverse("genealogy:tree_person", args=[self.child.pk])).content.decode()
        self.assertIn('id="root"', html)                 # «Гол хүн» товч
        self.assertIn("centerOnRoot", html)
        self.assertIn("scaleExtent([0.02, 40])", html)   # өргөн томруулалт
        # Багтаах нь viewBox-оор биш, d3.zoom-оор хийгдэнэ (масштаб 1 = бодит хэмжээ).
        self.assertNotIn('svg.attr("viewBox"', html)
        self.assertIn("fitAll", html)

    def test_tree_default_mode_is_both(self):
        """Анхдагч горим — «Нэгдсэн» (URL-д `mode` байхгүй үед ч)."""
        self.client.force_login(self.editor)
        r = self.client.get(reverse("genealogy:tree_person", args=[self.child.pk]))
        self.assertContains(r, 'let mode = "both";')
        data = self.client.get(reverse("genealogy:tree_data", args=[self.child.pk])).json()
        self.assertEqual(data["mode"], "both")

    def test_tree_mode_buttons_order(self):
        """Товчны дараалал: Нэгдсэн → Өвөг дээдэс → Үр хойчис."""
        self.client.force_login(self.editor)
        html = self.client.get(
            reverse("genealogy:tree_person", args=[self.child.pk])).content.decode()
        both = html.index('data-mode="both"')
        anc = html.index('data-mode="ancestors"')
        desc = html.index('data-mode="descendants"')
        self.assertLess(both, anc)
        self.assertLess(anc, desc)

    def test_tree_row_order_comes_from_backend(self):
        """Мөр нь backend-ийн `order`-оор эрэмбэлэгдэнэ (фронт өөрөө бодохгүй)."""
        self.client.force_login(self.editor)
        r = self.client.get(reverse("genealogy:tree_person", args=[self.child.pk]))
        self.assertContains(r, "(a.order ?? 0) - (b.order ?? 0)")

    def test_elder_siblings_children_come_first(self):
        """Ахмад ах/эгчийн хүүхдүүд мөрөнд УРД, дотроо төрсөн оноор жагсана."""
        # Тусдаа бяцхан мод: эгч (1970) → 2 хүүхэд, дүү (1980) → 1 хүүхэд.
        root = Person.objects.create(given_name="Эцэг", sex=Person.Sex.MALE)
        fam = Family.objects.create(partner1=root)
        sister = Person.objects.create(given_name="Эгч", sex=Person.Sex.FEMALE,
                                       birth_date=FlexDate.create_from_text("1970"))
        brother = Person.objects.create(given_name="Дүү", sex=Person.Sex.MALE,
                                        birth_date=FlexDate.create_from_text("1980"))
        for kid in (sister, brother):
            ChildLink.objects.create(family=fam, child=kid)

        niece_young = Person.objects.create(given_name="Зээ охин", sex=Person.Sex.FEMALE,
                                            birth_date=FlexDate.create_from_text("2000"))
        niece_old = Person.objects.create(given_name="Зээ хүү", sex=Person.Sex.MALE,
                                          birth_date=FlexDate.create_from_text("1995"))
        nephew = Person.objects.create(given_name="Ач", sex=Person.Sex.MALE,
                                       birth_date=FlexDate.create_from_text("1998"))
        for kid in (niece_young, niece_old):
            ChildLink.objects.create(
                family=Family.objects.create(partner2=sister), child=kid)
        ChildLink.objects.create(family=Family.objects.create(partner1=brother),
                                 child=nephew)

        data = build_graph(root, mode="descendants", max_depth=2, user=self.editor)
        order = {n["id"]: n["order"] for n in data["nodes"]}
        rows = {n["id"]: n["row"] for n in data["nodes"]}

        # 1-р үе: ахмад эгч дүүгээсээ урд.
        self.assertEqual(rows[sister.pk], rows[brother.pk])
        self.assertLess(order[sister.pk], order[brother.pk])

        # 2-р үе: эгчийн хүүхдүүд (дотроо оноор) дүүгийн хүүхдээс урд.
        self.assertEqual(rows[niece_old.pk], rows[nephew.pk])
        self.assertLess(order[niece_old.pk], order[niece_young.pk])
        self.assertLess(order[niece_young.pk], order[nephew.pk])

    def test_siblings_of_one_family_stay_adjacent(self):
        """Нэг айлын хүүхдүүд зэрэгцээ — хооронд нь өөр айлын хүүхэд орохгүй."""
        root = Person.objects.create(given_name="Эцэг", sex=Person.Sex.MALE)
        fam = Family.objects.create(partner1=root)
        kids = [Person.objects.create(given_name=f"Хүүхэд{i}", sex=Person.Sex.MALE,
                                      birth_date=FlexDate.create_from_text(str(1990 + i)))
                for i in range(3)]
        for kid in kids:
            ChildLink.objects.create(family=fam, child=kid)

        data = build_graph(root, mode="descendants", max_depth=1, user=self.editor)
        order = {n["id"]: n["order"] for n in data["nodes"]}
        self.assertEqual([order[k.pk] for k in kids], [0, 1, 2])   # төрсөн оноор

    def test_tree_root_has_edit_link(self):
        """Гол хүнийг засах линк модны толгойд байна."""
        self.client.force_login(self.editor)
        r = self.client.get(reverse("genealogy:tree_person", args=[self.child.pk]))
        self.assertContains(r, 'id="tree-root-edit"')
        self.assertContains(r, reverse("genealogy:person_edit", args=[self.child.pk]))

    def test_tree_edit_link_hidden_for_viewer(self):
        """Зөвхөн харах эрхтэй хүнд линк нуугдсан (`d-none`), гэхдээ DOM-д бий."""
        self.client.force_login(self.viewer)
        r = self.client.get(reverse("genealogy:tree_person", args=[self.child.pk]))
        self.assertContains(r, 'id="tree-root-edit"')
        self.assertContains(r, 'd-none')

    def test_tree_recenter_updates_edit_link(self):
        """Төв солиход линк шинэ хүн рүү заана (JS)."""
        self.client.force_login(self.editor)
        r = self.client.get(reverse("genealogy:tree_person", args=[self.child.pk]))
        self.assertContains(r, "/person/${node.id}/edit/")

    def test_tree_ancestors_render_upward(self):
        """Өвөг дээдсийн горимд үеүүд ДЭЭШЭЭ (root доод талд).

        Байрлал нь backend-ийн `row`-оос гарна: өвөг сөрөг, үр хойш эерэг,
        root 0. Ингэснээр «Нэгдсэн» горимд хоёулаа нэг зурган дээр багтана.
        """
        self.client.force_login(self.editor)
        r = self.client.get(reverse("genealogy:tree_person", args=[self.child.pk]))
        self.assertContains(r, "d3.group(nodes, d => d.row)")

        data = build_graph(self.child, mode="ancestors", max_depth=2, user=self.editor)
        rows = {n["id"]: n["row"] for n in data["nodes"]}
        self.assertEqual(rows[self.child.pk], 0)
        self.assertLess(rows[self.father.pk], 0)
        self.assertLess(rows[self.grandpa.pk], rows[self.father.pk])

    def test_both_mode_uses_branch_for_relation_label(self):
        """«Нэгдсэн» горимд өвөг нь өвгийн, үр хойчис нь үр хойшсын шошготой."""
        data = build_graph(self.child, mode="both", max_depth=2, user=self.editor)
        by_id = {n["id"]: n for n in data["nodes"]}
        self.assertEqual(by_id[self.father.pk]["relation"], "эцэг")   # дээшээ
        self.assertEqual(by_id[self.grandpa.pk]["relation"], "өвөг эцэг")
        self.assertEqual(by_id[self.grandchild.pk]["relation"], "хүү")  # доошоо
        self.assertEqual(by_id[self.father.pk]["row"], -1)
        self.assertEqual(by_id[self.grandchild.pk]["row"], 1)

    def test_tree_tooltip_includes_path_code(self):
        """Түүлтип нь замын кодыг харуулна (модны `title` элемент)."""
        self.client.force_login(self.editor)
        r = self.client.get(reverse("genealogy:tree_person", args=[self.child.pk]))
        self.assertContains(r, "Зам:")
        self.assertContains(r, "d.path_code")

    def test_depth_slider_offers_nine(self):
        self.client.force_login(self.editor)
        r = self.client.get(reverse("genealogy:tree_person", args=[self.child.pk]))
        self.assertContains(r, 'id="depth"')
        self.assertContains(r, 'max="9"')

    # -- үр хойш: хүү → «Х», охин → «О» -------------------------------------
    def test_descendant_paths_trace_from_root(self):
        paths = descendant_paths(self.child, max_depth=2)
        self.assertEqual(paths[self.grandchild.pk], [self.child.pk, self.grandchild.pk])

    def test_descendant_code_uses_child_sex(self):
        codes = self._codes(person=self.child, mode="descendants", max_depth=2)
        self.assertEqual(codes[self.child.pk], "")          # root — хоосон
        self.assertEqual(codes[self.grandchild.pk], "Х")    # хүү (эрэгтэй)

    def test_daughter_code_is_o(self):
        daughter = Person.objects.create(given_name="Охин", sex=Person.Sex.FEMALE)
        ChildLink.objects.create(family=self.child_family, child=daughter)
        codes = self._codes(person=self.child, mode="descendants", max_depth=2)
        self.assertEqual(codes[daughter.pk], "О")

    def test_granddaughter_code_is_kho(self):
        """Хүүгийн охин → «ХО» (өвгийн «ЭЭ»-тэй тэгш хэмтэй)."""
        grandchild_family = Family.objects.create(partner1=self.grandchild)
        granddaughter = Person.objects.create(given_name="Ач охин", sex=Person.Sex.FEMALE)
        ChildLink.objects.create(family=grandchild_family, child=granddaughter)

        codes = self._codes(person=self.child, mode="descendants", max_depth=3)
        self.assertEqual(codes[self.grandchild.pk], "Х")     # хүү
        self.assertEqual(codes[granddaughter.pk], "ХО")      # хүүгийн охин


class EffectiveSexTests(TreeBuilderMixin, TestCase):
    """Хүйс тодорхойгүйг «эцэг = эрэгтэй, эх = эмэгтэй» зарчмаар нөхөх."""

    def setUp(self):
        self.build_family()
        self.editor = User.objects.create_user(email="sex@example.com", password="x",
                                               role=User.Role.EDITOR)

    def test_inferred_from_partner_sex(self):
        wife = Person.objects.create(given_name="Эх", sex=Person.Sex.FEMALE)
        spouse = Person.objects.create(given_name="Тодорхойгүй")       # sex=U
        Family.objects.create(partner1=spouse, partner2=wife)

        sexes = effective_sex_map({p.pk: p for p in (spouse, wife)})
        self.assertEqual(sexes[spouse.pk], "M")     # нөгөө нь эмэгтэй → энэ эрэгтэй
        self.assertEqual(sexes[wife.pk], "F")       # хадгалагдсан хэвээр

    def test_both_unknown_stays_unknown(self):
        a = Person.objects.create(given_name="А")
        b = Person.objects.create(given_name="Б")
        Family.objects.create(partner1=a, partner2=b)

        sexes = effective_sex_map({p.pk: p for p in (a, b)})
        self.assertEqual(sexes[a.pk], "U")
        self.assertEqual(sexes[b.pk], "U")

    def test_stored_sex_never_overridden(self):
        husband = Person.objects.create(given_name="Эцэг", sex=Person.Sex.MALE)
        wife = Person.objects.create(given_name="Эх", sex=Person.Sex.FEMALE)
        Family.objects.create(partner1=husband, partner2=wife)

        sexes = effective_sex_map({p.pk: p for p in (husband, wife)})
        self.assertEqual(sexes[husband.pk], "M")
        self.assertEqual(sexes[wife.pk], "F")

    def test_tree_uses_inferred_sex_for_label_and_code(self):
        """Эцэг нь хүйсгүй бүртгэгдсэн ч модон дээр «эцэг» ба «А» болно."""
        mother = Person.objects.create(given_name="Эх", sex=Person.Sex.FEMALE)
        father = Person.objects.create(given_name="Тодорхойгүй")       # sex=U
        family = Family.objects.create(partner1=father, partner2=mother)
        kid = Person.objects.create(given_name="Хүүхэд", sex=Person.Sex.MALE)
        ChildLink.objects.create(family=family, child=kid)

        data = build_graph(kid, mode="ancestors", max_depth=1, user=self.editor)
        by_id = {n["id"]: n for n in data["nodes"]}
        self.assertEqual(by_id[father.pk]["sex"], "M")
        self.assertEqual(by_id[father.pk]["relation"], "эцэг")
        self.assertEqual(by_id[father.pk]["path_code"], "А")   # А = аав


class PersonEditPermissionTests(TreeBuilderMixin, TestCase):
    """Мөр тус бүрийн эрх: засварлагч зөвхөн өөрийн/нээлттэй/эзэнгүй хүнийг засна."""

    def setUp(self):
        self.build_family()
        self.admin = User.objects.create_user(email="perm-admin@example.com", password="x",
                                              role=User.Role.ADMIN)
        self.editor = User.objects.create_user(email="perm-editor@example.com", password="x",
                                               role=User.Role.EDITOR)
        self.other = User.objects.create_user(email="perm-other@example.com", password="x",
                                              role=User.Role.EDITOR)
        self.viewer = User.objects.create_user(email="perm-viewer@example.com", password="x",
                                               role=User.Role.VIEWER)
        self.null_owner = self.child                       # fixture: created_by = NULL
        self.foreign = Person.objects.create(given_name="Бусдын", created_by=self.other)
        self.opened = Person.objects.create(given_name="Нээлттэй", created_by=self.other,
                                            is_open_for_edit=True)
        self.own = Person.objects.create(given_name="Өөрийн", created_by=self.editor)

    # --- дүрэм ------------------------------------------------------------
    def test_admin_can_edit_anything(self):
        for p in (self.null_owner, self.foreign, self.opened, self.own):
            self.assertTrue(p.can_edit_by(self.admin), p.display_name)

    def test_editor_can_edit_own_null_and_open(self):
        for p in (self.own, self.null_owner, self.opened):
            self.assertTrue(p.can_edit_by(self.editor), p.display_name)

    def test_editor_cannot_edit_foreign_closed(self):
        self.assertFalse(self.foreign.can_edit_by(self.editor))

    def test_viewer_and_anonymous_cannot_edit(self):
        for user in (self.viewer, None):
            for p in (self.null_owner, self.foreign, self.opened, self.own):
                self.assertFalse(p.can_edit_by(user), f"{user} / {p.display_name}")

    def _make_fixture_foreign(self):
        """Fixture-ийн бүх хүнийг бусдын болгоно (created_by = other)."""
        Person.objects.filter(pk__in=[self.grandpa.pk, self.grandma.pk, self.father.pk,
                                      self.mother.pk, self.child.pk, self.grandchild.pk]
                              ).update(created_by=self.other)

    def test_editor_can_edit_own_ancestors_and_descendants(self):
        self._make_fixture_foreign()
        Person.objects.filter(pk=self.father.pk).update(linked_user=self.editor)
        lineage = (self.grandpa, self.grandma, self.child, self.grandchild)
        for p in lineage:
            p.refresh_from_db()
            self.assertTrue(p.can_edit_by(self.editor), p.display_name)
        ids = set(Person.objects.editable_by(self.editor).values_list("pk", flat=True))
        self.assertTrue({p.pk for p in lineage} <= ids)

    def test_lineage_excludes_spouse_and_closed(self):
        """Эхнэр/нөхөр удамд орохгүй; «Хаалттай» өвгийг засварлагч засахгүй."""
        self._make_fixture_foreign()
        Person.objects.filter(pk=self.father.pk).update(linked_user=self.editor)
        Person.objects.filter(pk=self.grandpa.pk).update(visibility=Person.Visibility.CLOSED)
        self.mother.refresh_from_db()
        self.grandpa.refresh_from_db()
        self.assertFalse(self.mother.can_edit_by(self.editor))
        self.assertFalse(self.grandpa.can_edit_by(self.editor))
        ids = set(Person.objects.editable_by(self.editor).values_list("pk", flat=True))
        self.assertNotIn(self.mother.pk, ids)
        self.assertNotIn(self.grandpa.pk, ids)

    def test_viewer_does_not_get_lineage_rights(self):
        self._make_fixture_foreign()
        Person.objects.filter(pk=self.father.pk).update(linked_user=self.viewer)
        self.grandpa.refresh_from_db()
        self.assertFalse(self.grandpa.can_edit_by(self.viewer))

    def test_editor_200_on_ancestor_edit(self):
        self._make_fixture_foreign()
        Person.objects.filter(pk=self.child.pk).update(linked_user=self.editor)
        self.client.force_login(self.editor)
        r = self.client.get(reverse("genealogy:person_edit", args=[self.grandpa.pk]))
        self.assertEqual(r.status_code, 200)

    def test_private_details_only_for_editors_who_can_edit(self):
        """«Хувийн» хүний нарийн мэдээллийг зөвхөн засах эрхтэй засварлагч харна."""
        self._make_fixture_foreign()
        Person.objects.filter(pk=self.grandpa.pk).update(visibility=Person.Visibility.PRIVATE)
        self.grandpa.refresh_from_db()
        # Удамд нь биш засварлагч — нэр харна, нарийн мэдээлэл хаалттай.
        self.assertTrue(self.grandpa.can_view(self.editor))
        self.assertFalse(self.grandpa.can_see_details(self.editor))
        # Удамд нь орсон (ач нь) — засах эрхтэй тул харна.
        Person.objects.filter(pk=self.grandchild.pk).update(linked_user=self.editor)
        self.editor._lineage_ids_cache = None
        self.assertTrue(self.grandpa.can_see_details(self.editor))
        # Админ ямагт харна.
        self.assertTrue(self.grandpa.can_see_details(self.admin))

    def test_editable_by_matches_can_edit_by(self):
        """Хоёр зам (queryset ба объект) ижил байх ёстой — зөрвөл энэ тест унана."""
        Person.objects.filter(pk=self.father.pk).update(linked_user=self.editor)
        for user in (self.admin, self.editor, self.other, self.viewer):
            ids = set(Person.objects.editable_by(user).values_list("pk", flat=True))
            expected = {p.pk for p in Person.objects.alive() if p.can_edit_by(user)}
            self.assertEqual(ids, expected, msg=f"зөрүү: {user.email}")

    # --- Person засах / устгах ---------------------------------------------
    def test_editor_404_on_foreign_edit(self):
        self.client.force_login(self.editor)
        r = self.client.get(reverse("genealogy:person_edit", args=[self.foreign.pk]))
        self.assertEqual(r.status_code, 404)

    def test_editor_200_on_open_and_null_owner_edit(self):
        self.client.force_login(self.editor)
        for p in (self.opened, self.null_owner):
            with self.subTest(pk=p.pk):
                r = self.client.get(reverse("genealogy:person_edit", args=[p.pk]))
                self.assertEqual(r.status_code, 200)

    def test_admin_can_open_foreign_edit(self):
        self.client.force_login(self.admin)
        r = self.client.get(reverse("genealogy:person_edit", args=[self.foreign.pk]))
        self.assertEqual(r.status_code, 200)

    def test_editor_404_on_foreign_delete(self):
        self.client.force_login(self.editor)
        r = self.client.get(reverse("genealogy:person_delete", args=[self.foreign.pk]))
        self.assertEqual(r.status_code, 404)

    # --- хамаатан холбох ---------------------------------------------------
    def test_quick_add_404_on_foreign_anchor(self):
        self.client.force_login(self.editor)
        r = self.client.get(reverse("genealogy:quick_add", args=[self.foreign.pk, "child"]))
        self.assertEqual(r.status_code, 404)

    def test_link_relative_404_on_foreign_anchor(self):
        self.client.force_login(self.editor)
        r = self.client.get(reverse("genealogy:link_relative", args=[self.foreign.pk, "child"]))
        self.assertEqual(r.status_code, 404)

    def test_link_relative_rejects_foreign_target(self):
        """Anchor нь эзэнгүй (засна), харин холбох хүн нь бусдынх — форм алдаа өгнө."""
        self.client.force_login(self.editor)
        r = self.client.post(
            reverse("genealogy:link_relative", args=[self.null_owner.pk, "sibling"]),
            {"relation": "sibling", "person": self.foreign.pk, "pedigree": "birth"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Сонгосон хүн олдсонгүй")
        self.assertEqual(self.null_owner.parent_links.count(), 1)   # өөрчлөгдөөгүй

    def test_picker_excludes_foreign_person(self):
        self.client.force_login(self.editor)
        r = self.client.get(reverse("genealogy:person_picker"),
                            {"anchor": self.null_owner.pk, "relation": "sibling"})
        self.assertNotContains(r, f'value="{self.foreign.pk}"')
        self.assertContains(r, f'value="{self.opened.pk}"')

    def test_merge_404_when_one_side_is_foreign(self):
        self.client.force_login(self.editor)
        r = self.client.post(reverse("genealogy:merge"),
                             {"keep": self.null_owner.pk, "duplicate": self.foreign.pk})
        self.assertEqual(r.status_code, 404)

    # --- гэр бүл -----------------------------------------------------------
    def test_family_edit_404_when_partner_is_foreign(self):
        fam = Family.objects.create(partner1=self.null_owner, partner2=self.foreign)
        self.client.force_login(self.editor)
        r = self.client.get(reverse("genealogy:family_edit", args=[fam.pk]))
        self.assertEqual(r.status_code, 404)

    def test_family_edit_200_when_all_members_editable(self):
        self.client.force_login(self.editor)
        r = self.client.get(reverse("genealogy:family_edit", args=[self.parent_family.pk]))
        self.assertEqual(r.status_code, 200)

    def test_family_form_rejects_foreign_partner(self):
        self.client.force_login(self.editor)
        r = self.client.post(
            reverse("genealogy:family_edit", args=[self.parent_family.pk]),
            {"partner1": self.father.pk, "partner2": self.foreign.pk,
             "family_type": "marriage", "marriage_place": "", "notes": "",
             "marriage_date": "", "divorce_date": ""})
        self.assertEqual(r.status_code, 200)          # форм дахин зурагдана
        self.parent_family.refresh_from_db()
        self.assertEqual(self.parent_family.partner2, self.mother)   # өөрчлөгдөөгүй

    # --- UI ----------------------------------------------------------------
    def test_detail_hides_edit_controls_for_foreign(self):
        self.client.force_login(self.editor)
        r = self.client.get(reverse("genealogy:person_detail", args=[self.foreign.pk]))
        self.assertNotContains(r, "+ Хамаатан")
        self.assertContains(r, "Мод дээр")            # харах эрх хэвээр

    def test_detail_shows_edit_controls_for_open_person(self):
        self.client.force_login(self.editor)
        r = self.client.get(reverse("genealogy:person_detail", args=[self.opened.pk]))
        self.assertContains(r, "+ Хамаатан")

    def test_tree_node_carries_can_edit_flag(self):
        data = build_graph(self.null_owner, mode="ancestors", max_depth=2, user=self.editor)
        by_id = {n["id"]: n for n in data["nodes"]}
        self.assertTrue(by_id[self.null_owner.pk]["can_edit"])
        self.assertIn("can_edit", by_id[self.father.pk])

        data = build_graph(self.foreign, mode="descendants", max_depth=1, user=self.editor)
        by_id = {n["id"]: n for n in data["nodes"]}
        self.assertFalse(by_id[self.foreign.pk]["can_edit"])


class FamilyConstraintTests(TestCase):
    def test_partner_cannot_be_self(self):
        p = Person.objects.create(given_name="А")
        fam = Family(partner1=p, partner2=p)
        with self.assertRaises(ValidationError):
            fam.full_clean()


class ViewTests(TreeBuilderMixin, TestCase):
    """View түвшний шалгалт — эрх, нууцлал, HTMX урсгал."""

    def setUp(self):
        self.build_family()
        self.editor = User.objects.create_user(email="v-editor@example.com", password="x",
                                               role=User.Role.EDITOR)
        self.viewer = User.objects.create_user(email="v-viewer@example.com", password="x",
                                               role=User.Role.VIEWER)
        self.url_person = reverse("genealogy:person_detail", args=[self.child.pk])
        self.url_tree_data = reverse("genealogy:tree_data", args=[self.child.pk])
        self.url_quick = reverse("genealogy:quick_add", args=[self.child.pk, "child"])

    # -- нэвтрэлт ----------------------------------------------------------

    def test_home_renders_for_logged_in_user(self):
        self.client.force_login(self.viewer)
        response = self.client.get(reverse("genealogy:home"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ургийн мод")

    def test_login_page_uses_email_field(self):
        response = self.client.get(reverse("accounts:login"))
        self.assertContains(response, 'type="email"')

    def test_edit_view_requires_login(self):
        response = self.client.get(reverse("genealogy:person_edit", args=[self.child.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])

    def test_viewer_cannot_edit(self):
        self.client.force_login(self.viewer)
        response = self.client.get(reverse("genealogy:person_edit", args=[self.child.pk]))
        self.assertEqual(response.status_code, 302)  # эрхгүй → нүүр рүү

    def test_editor_can_open_edit_form(self):
        self.client.force_login(self.editor)
        response = self.client.get(reverse("genealogy:person_edit", args=[self.child.pk]))
        self.assertEqual(response.status_code, 200)

    # -- нууцлал -----------------------------------------------------------

    def test_person_detail_404_for_closed_person(self):
        """«Хаалттай» хүн жирийн нэвтэрсэн гишүүнд 404 (байгаа эсэхийг задруулахгүй)."""
        Person.objects.filter(pk=self.child.pk).update(visibility=Person.Visibility.CLOSED)
        self.client.force_login(self.viewer)
        response = self.client.get(self.url_person)
        self.assertEqual(response.status_code, 404)

    def test_person_detail_opens_for_private_but_hides_details(self):
        """«Хувийн» хүний профайл нээгдэнэ — гэхдээ нас/хүйс харагдахгүй."""
        Person.objects.filter(pk=self.child.pk).update(
            visibility=Person.Visibility.PRIVATE,
            birth_date=FlexDate.create_from_text("1977"),
            sex=Person.Sex.MALE)
        self.client.force_login(self.viewer)
        response = self.client.get(self.url_person)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "хаалттай")
        self.assertNotContains(response, "1977")          # нас хаалттай
        self.assertNotContains(response, "Эрэгтэй")       # хүйс хаалттай

    def test_person_detail_ok_for_editor(self):
        Person.objects.filter(pk=self.child.pk).update(visibility=Person.Visibility.PRIVATE)
        self.client.force_login(self.editor)
        self.assertEqual(self.client.get(self.url_person).status_code, 200)

    def test_tree_json_does_not_leak_closed_person(self):
        Person.objects.filter(pk=self.grandpa.pk).update(visibility=Person.Visibility.CLOSED)
        self.client.force_login(self.viewer)
        data = self.client.get(self.url_tree_data + "?mode=ancestors&depth=4").json()
        grandpa_node = next(n for n in data["nodes"] if n["id"] == self.grandpa.pk)
        self.assertTrue(grandpa_node["hidden"])
        self.assertEqual(grandpa_node["name"], "•••")

    def test_tree_json_masks_private_person(self):
        """«Хувийн» хүн модны JSON-д нэртэй гарна, харин нас/хүйс огт явахгүй."""
        Person.objects.filter(pk=self.grandpa.pk).update(
            visibility=Person.Visibility.PRIVATE,
            birth_date=FlexDate.create_from_text("1880"))
        self.client.force_login(self.viewer)
        data = self.client.get(self.url_tree_data + "?mode=ancestors&depth=4").json()
        node = next(n for n in data["nodes"] if n["id"] == self.grandpa.pk)
        self.assertFalse(node["hidden"])
        self.assertTrue(node["masked"])
        self.assertEqual(node["lifespan"], "")
        self.assertIsNone(node["birth"])
        self.assertNotIn("1880", str(node))

    def test_tree_json_shows_deceased_ancestor_to_logged_in_viewer(self):
        """Нас барсан үндэс нь гишүүнд бүрэн харагдана, «Хаалттай» хүн нуугдана."""
        Person.objects.filter(pk=self.grandpa.pk).update(
            birth_date=FlexDate.create_from_text("1850"))
        Person.objects.filter(pk=self.father.pk).update(visibility=Person.Visibility.CLOSED)
        self.client.force_login(self.viewer)
        url = reverse("genealogy:tree_data", args=[self.grandpa.pk])
        data = self.client.get(url + "?mode=descendants&depth=4").json()
        grandpa_node = next(n for n in data["nodes"] if n["id"] == self.grandpa.pk)
        self.assertFalse(grandpa_node["hidden"])
        father_node = next(n for n in data["nodes"] if n["id"] == self.father.pk)
        self.assertTrue(father_node["hidden"])

    # -- quick-add (HTMX) --------------------------------------------------

    def test_quick_add_get_returns_modal_fragment(self):
        self.client.force_login(self.editor)
        response = self.client.get(self.url_quick, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "genealogy/partials/_quick_add_modal.html")
        self.assertNotContains(response, "<html")  # бүтэн хуудас биш, хэсэг

    def test_quick_add_creates_child_and_triggers_refresh(self):
        self.client.force_login(self.editor)
        response = self.client.post(self.url_quick, {
            "relation": "child", "given_name": "Шинэ", "patronymic": "Эцгийн",
            "clan_name": "", "sex": "M", "birth_date": "ойролцоогоор 2010",
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn("tree:refresh", response["HX-Trigger"])
        child = Person.objects.get(given_name="Шинэ")
        self.assertEqual(child.birth_date.year, 2010)
        self.assertIn(self.child.pk, [p.pk for p in child.parents])

    def test_quick_add_rejects_viewer(self):
        self.client.force_login(self.viewer)
        response = self.client.post(self.url_quick, {"relation": "child", "given_name": "Хулгайч"})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Person.objects.filter(given_name="Хулгайч").exists())

    def test_quick_add_shows_error_when_both_parents_known(self):
        self.client.force_login(self.editor)
        response = self.client.post(
            reverse("genealogy:quick_add", args=[self.child.pk, "father"]),
            {"relation": "father", "given_name": "Гурав дахь"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "аль хэдийн бүртгэгдсэн")

    # -- хайлт -------------------------------------------------------------

    def test_search_returns_partial_for_htmx(self):
        self.client.force_login(self.editor)
        response = self.client.get(reverse("genealogy:search"), {"q": "Эцэг"},
                                   HTTP_HX_REQUEST="true")
        self.assertTemplateUsed(response, "genealogy/partials/_search_results.html")
        self.assertContains(response, "Эцэг")

    def test_search_full_page_without_htmx(self):
        self.client.force_login(self.editor)
        response = self.client.get(reverse("genealogy:search"), {"q": "Эцэг"})
        self.assertTemplateUsed(response, "genealogy/search.html")

    def test_person_lookup_json(self):
        self.client.force_login(self.editor)
        data = self.client.get(reverse("genealogy:person_lookup"), {"q": "Эцэг"}).json()
        self.assertTrue(any("Эцэг" in r["text"] for r in data["results"]))


class MediaSecurityTests(TreeBuilderMixin, TestCase):
    def setUp(self):
        self.build_family()
        self.editor = User.objects.create_user(email="m-editor@example.com", password="x",
                                               role=User.Role.EDITOR)
        self.viewer = User.objects.create_user(email="m-viewer@example.com", password="x")
        from django.core.files.base import ContentFile

        self.media = Media.objects.create(kind=Media.Kind.DOCUMENT, title="Архивын бичиг")
        self.media.file.save("test.txt", ContentFile(b"secret archive"), save=True)
        self.url = reverse("genealogy:media_serve", args=[self.media.pk])

    def test_media_requires_authentication(self):
        """Нэвтрээгүй хүн медиа файлд хүрэхгүй (нэвтрэх хуудас руу шилжинэ)."""
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])
        self.assertNotIn(b"secret archive", response.content)

    def test_media_denied_when_linked_only_to_hidden_person(self):
        from django.contrib.contenttypes.models import ContentType

        Person.objects.filter(pk=self.child.pk).update(visibility=Person.Visibility.PRIVATE)
        MediaLink.objects.create(media=self.media, content_object=self.child, is_primary=True)
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_media_allowed_for_visible_person(self):
        MediaLink.objects.create(media=self.media, content_object=self.child, is_primary=True)
        self.client.force_login(self.viewer)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content) if response.streaming else response.content,
                         b"secret archive")

    def test_media_allowed_for_editor(self):
        self.client.force_login(self.editor)
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_s3_redirects_to_presigned_url_after_permission_check(self):
        """S3 үед файлыг дамжуулахгүй, эрх шалгасны дараа presigned URL руу шилжинэ."""
        from unittest import mock

        from django.test import override_settings

        MediaLink.objects.create(media=self.media, content_object=self.child, is_primary=True)
        self.client.force_login(self.viewer)
        storage = self.media.file.storage
        with override_settings(USE_S3_MEDIA=True), mock.patch.object(
            storage, "url", return_value="https://bucket.s3.amazonaws.com/x?X-Amz-Signature=s"
        ) as url:
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "https://bucket.s3.amazonaws.com/x?X-Amz-Signature=s")
        self.assertEqual(response["Cache-Control"], "private, no-store")
        params = url.call_args.kwargs["parameters"]
        self.assertIn("ResponseContentDisposition", params)

    def test_s3_still_denies_hidden_person(self):
        from unittest import mock

        from django.test import override_settings

        Person.objects.filter(pk=self.child.pk).update(visibility=Person.Visibility.PRIVATE)
        MediaLink.objects.create(media=self.media, content_object=self.child, is_primary=True)
        self.client.force_login(self.viewer)
        with override_settings(USE_S3_MEDIA=True), mock.patch.object(
            self.media.file.storage, "url"
        ) as url:
            self.assertEqual(self.client.get(self.url).status_code, 404)
        url.assert_not_called()


class AdminSmokeTests(TreeBuilderMixin, TestCase):
    """Admin-ийн БҮХ хуудсыг бодит өгөгдөлтэйгээр шалгана.

    Анхаар: хоосон сан дээр changelist нь нэг ч мөр зурахгүй тул
    `list_display`-ийн метод (жишээ нь `living_badge`) огт дуудагдахгүй —
    ийм алдаа (format_html-ыг аргументгүй дуудах) яг тийнхээс болж
    илрээгүй байсан. Тиймээс энд өгөгдөл ЗААВАЛ үүсгэнэ.
    """

    def setUp(self):
        self.build_family()
        # changelist-д мөр гарахын тулд огноо, газар, холбоо зэргийг бөглөнө.
        place = Place.objects.create(name="Архангай", place_type=Place.PlaceType.AIMAG)
        summ = Place.objects.create(name="Цэнхэр сум", parent=place)
        self.child.birth_date = FlexDate.create_from_text("1977-04-01")
        self.child.birth_place = summ
        self.child.death_date = FlexDate.create_from_text("2020")
        self.child.save()
        self.father.birth_date = FlexDate.create_from_text("ойролцоогоор 1950")
        self.father.save()
        PersonName.objects.create(person=self.child, name_type=PersonName.NameType.AKA,
                                  given_name="Хоч нэр")
        source = Source.objects.create(title="Архивын бичиг №12",
                                       source_type=Source.SourceType.ARCHIVE)
        Citation.objects.create(source=source, content_object=self.child, page="3")
        event = Event.objects.create(event_type=Event.EventType.OCCUPATION,
                                     date=FlexDate.create_from_text("1940"))
        EventParticipant.objects.create(event=event, person=self.child)
        Media.objects.create(kind=Media.Kind.PHOTO, title="Гэрэл зураг")
        self.parent_family.marriage_date = FlexDate.create_from_text("1955")
        self.parent_family.save()

        self.admin_user = User.objects.create_superuser(email="admin@example.com",
                                                        password="pw12345!")
        self.client.force_login(self.admin_user)

    def _urls(self):
        """Бүртгэгдсэн admin хуудсууд. Хандах эрхгүй (жишээ нь зөвхөн
        уншигддаг AuditLog) хуудсыг Django-ийн эрхийн API-аар шүүнэ."""
        from django.contrib import admin as django_admin
        from django.test import RequestFactory

        request = RequestFactory().get("/")
        request.user = self.admin_user

        urls = ["/admin/"]
        for model, model_admin in django_admin.site._registry.items():
            app_label = model._meta.app_label
            if app_label not in {"genealogy", "accounts"}:
                continue
            name = model._meta.model_name
            urls.append(f"/admin/{app_label}/{name}/")
            if model_admin.has_add_permission(request):
                urls.append(f"/admin/{app_label}/{name}/add/")
            for obj in model.objects.all()[:2]:
                if model_admin.has_change_permission(request, obj):
                    urls.append(f"/admin/{app_label}/{name}/{obj.pk}/change/")
        return urls

    def test_every_admin_page_loads(self):
        urls = self._urls()
        self.assertGreater(len(urls), 20, "Хэт цөөн хуудас шалгагдлаа")
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_changelist_actually_renders_rows(self):
        """Жагсаалт үнэхээр мөр зурах ёстой — эс бөгөөс тест утгагүй."""
        response = self.client.get("/admin/genealogy/person/")
        self.assertContains(response, self.child.display_name)
        self.assertContains(response, "нас барсан", msg_prefix="life_badge зурагдаагүй")

    def test_auditlog_is_read_only(self):
        """Аудит бүртгэлийг нэмэх/засах боломжгүй, зөвхөн харна.

        Django-д «харах» эрх тусад нь байдаг тул засах хуудас 200 буцаж,
        зөвхөн уншигдах хэлбэрээр зурагдана — хадгалах товч БАЙХГҮЙ.
        """
        self.assertEqual(self.client.get("/admin/genealogy/auditlog/add/").status_code, 403)

        log = AuditLog.objects.create(action=AuditLog.Action.CREATE, object_repr="тест")
        response = self.client.get(f"/admin/genealogy/auditlog/{log.pk}/change/")
        self.assertNotContains(response, 'name="_save"', msg_prefix="Хадгалах товч гарчээ")
        self.assertNotContains(response, 'name="_continue"')

    def test_merge_action_is_available(self):
        response = self.client.get("/admin/genealogy/person/")
        self.assertContains(response, "нэгтгэх")


class PlaceTests(TestCase):
    """Газар нэмэх урсгал (форм дотороос, admin шаардахгүй)."""

    def setUp(self):
        self.editor = User.objects.create_user(email="p-editor@example.com", password="x",
                                               role=User.Role.EDITOR)
        self.viewer = User.objects.create_user(email="p-viewer@example.com", password="x")
        self.url = reverse("genealogy:place_create")

    def test_requires_edit_permission(self):
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertFalse(Place.objects.exists())

    def test_get_returns_modal_partial(self):
        self.client.force_login(self.editor)
        response = self.client.get(self.url + "?target=id_birth_place", HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "genealogy/partials/_place_modal.html")
        self.assertContains(response, 'name="target" value="id_birth_place"')

    def test_post_creates_place_and_returns_options(self):
        self.client.force_login(self.editor)
        response = self.client.post(self.url, {
            "name": "Архангай", "place_type": "aimag", "target": "id_birth_place",
        })
        self.assertEqual(response.status_code, 200)
        place = Place.objects.get(name="Архангай")
        self.assertTemplateUsed(response, "genealogy/partials/_place_options.html")
        self.assertContains(response, f'value="{place.pk}" selected')
        self.assertIn("place:created", response["HX-Trigger"])

    def test_hierarchy_full_name(self):
        aimag = Place.objects.create(name="Архангай", place_type=Place.PlaceType.AIMAG)
        summ = Place.objects.create(name="Цэнхэр сум", place_type=Place.PlaceType.SUM, parent=aimag)
        self.assertEqual(summ.full_name, "Архангай, Цэнхэр сум")

    def test_options_show_full_hierarchical_label(self):
        """Шинэ газар нэмэгдэхэд буцах сонголтууд бүтэн замаар харагдана."""
        aimag = Place.objects.create(name="Архангай", place_type=Place.PlaceType.AIMAG)
        summ = Place.objects.create(name="Цэнхэр сум", parent=aimag)
        self.client.force_login(self.editor)
        response = self.client.post(self.url, {
            "name": "Ихтамир сум", "place_type": "sum", "parent": aimag.pk,
            "target": "id_birth_place",
        })
        self.assertContains(response, "Архангай, Цэнхэр сум")
        self.assertContains(response, "Архангай, Ихтамир сум")
        self.assertContains(response, f'value="{summ.pk}"')

    def test_person_form_shows_add_place_button(self):
        admin = User.objects.create_user(email="place-admin@example.com", password="x",
                                         role=User.Role.ADMIN)
        self.client.force_login(admin)
        response = self.client.get(reverse("genealogy:person_create"))
        self.assertContains(response, "+ Газар")
        self.assertContains(response, f'hx-target="#htmx-modal-content"')

    def test_place_saved_on_person(self):
        place = Place.objects.create(name="Улаанбаатар", place_type=Place.PlaceType.CITY)
        person = Person.objects.create(given_name="Ганбат", birth_place=place)
        self.assertEqual(person.birth_place.full_name, "Улаанбаатар")


class LinkExistingTests(TreeBuilderMixin, TestCase):
    """БАЙГАА хүмүүсийг холбох — цикл хамгаалалт, DB constraint-ийн ойлгомжтой алдаа."""

    def setUp(self):
        self.build_family()
        self.editor = User.objects.create_user(email="l-editor@example.com", password="x",
                                               role=User.Role.EDITOR)

    # -- амжилттай холбоо --------------------------------------------------

    def test_link_existing_as_father(self):
        orphan = Person.objects.create(given_name="Өнчин")
        father = Person.objects.create(given_name="Эцэг нь", sex=Person.Sex.MALE)
        link_existing(orphan, "father", father)
        self.assertEqual(orphan.parents, [father])

    def test_link_existing_mother_into_remaining_slot(self):
        orphan = Person.objects.create(given_name="Өнчин")
        father = Person.objects.create(given_name="Эцэг", sex=Person.Sex.MALE)
        mother = Person.objects.create(given_name="Эх", sex=Person.Sex.FEMALE)
        link_existing(orphan, "father", father)
        link_existing(orphan, "mother", mother)
        family = orphan.birth_family
        self.assertEqual({family.partner1, family.partner2}, {father, mother})

    def test_link_existing_partner(self):
        a = Person.objects.create(given_name="А")
        b = Person.objects.create(given_name="Б")
        link_existing(a, "partner", b)
        self.assertIn(b, a.families.first().partners)

    def test_link_existing_sibling(self):
        sibling = Person.objects.create(given_name="Ах")
        link_existing(self.child, "sibling", sibling)
        self.assertEqual(sibling.birth_family, self.child.birth_family)

    def test_link_existing_child(self):
        person = Person.objects.create(given_name="Хүүхэд нь")
        link_existing(self.child, "child", person)
        self.assertIn(self.child.pk, [p.pk for p in person.parents])

    def test_link_does_not_create_new_person(self):
        other = Person.objects.create(given_name="Оршин буй")
        before = Person.objects.count()
        link_existing(self.child, "sibling", other)
        self.assertEqual(Person.objects.count(), before)

    # -- цикл ба бусад хориглолт -------------------------------------------

    def test_cannot_link_descendant_as_parent(self):
        """Ач хүүгээ өөрийн эцэг болгох — цикл."""
        another_grandchild = Person.objects.create(given_name="Ач2")
        ChildLink.objects.create(family=self.child_family, child=another_grandchild)
        with self.assertRaises(ValueError) as ctx:
            link_existing(self.child, "father", self.grandchild)
        self.assertIn("үр хойчис", str(ctx.exception))

    def test_cannot_link_ancestor_as_child(self):
        with self.assertRaises(ValueError):
            link_existing(self.grandchild, "child", self.child)

    def test_cannot_link_self(self):
        with self.assertRaises(ValueError):
            link_existing(self.child, "partner", self.child)

    def test_cannot_link_someone_who_already_has_birth_family(self):
        other = Person.objects.create(given_name="Өөр")
        with self.assertRaises(ValueError) as ctx:
            link_existing(self.child, "child", self.father)
        self.assertIn("аль хэдийн", str(ctx.exception))

    def test_cannot_link_parent_twice(self):
        with self.assertRaises(ValueError):
            link_existing(self.child, "father", self.father)

    def test_cannot_link_when_both_parent_slots_full(self):
        other = Person.objects.create(given_name="Гурав дахь")
        with self.assertRaises(ValueError) as ctx:
            link_existing(self.child, "parent", other)
        self.assertIn("хоёр эцэг эх", str(ctx.exception))

    def test_cannot_link_ancestor_as_partner(self):
        with self.assertRaises(ValueError):
            link_existing(self.child, "partner", self.father)

    def test_failed_link_leaves_no_trace(self):
        """Атомар байдал: алдаа гарвал хагас өөрчлөлт үлдэхгүй."""
        orphan = Person.objects.create(given_name="Өнчин2")
        with self.assertRaises(ValueError):
            link_existing(orphan, "child", self.father)
        self.assertIsNone(orphan.birth_family)


class LinkExistingViewTests(TreeBuilderMixin, TestCase):
    def setUp(self):
        self.build_family()
        self.editor = User.objects.create_user(email="lv-editor@example.com", password="x",
                                               role=User.Role.EDITOR)
        self.viewer = User.objects.create_user(email="lv-viewer@example.com", password="x")
        self.orphan = Person.objects.create(given_name="Өнчин")
        self.candidate = Person.objects.create(given_name="Кандидат", sex=Person.Sex.MALE)
        self.url = reverse("genealogy:link_relative", args=[self.orphan.pk, "father"])

    def test_requires_edit_permission(self):
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_get_returns_modal_partial(self):
        self.client.force_login(self.editor)
        response = self.client.get(self.url, HTTP_HX_REQUEST="true")
        self.assertTemplateUsed(response, "genealogy/partials/_link_modal.html")
        self.assertContains(response, "picker-results")

    def test_post_links_existing_person(self):
        self.client.force_login(self.editor)
        response = self.client.post(self.url, {"relation": "father", "person": self.candidate.pk})
        self.assertEqual(response.status_code, 200)
        self.assertIn("tree:refresh", response["HX-Trigger"])
        self.assertEqual(self.orphan.parents, [self.candidate])

    def test_post_without_selection_shows_error_not_500(self):
        self.client.force_login(self.editor)
        response = self.client.post(self.url, {"relation": "father"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "сонгоно уу")

    def test_post_cycle_shows_error_in_modal(self):
        self.client.force_login(self.editor)
        url = reverse("genealogy:link_relative", args=[self.child.pk, "father"])
        response = self.client.post(url, {"relation": "father", "person": self.grandchild.pk})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "үр хойчис")
        self.child.refresh_from_db()
        self.assertEqual(self.child.birth_family.partner2, self.mother)  # өөрчлөгдөөгүй


class PersonPickerTests(TreeBuilderMixin, TestCase):
    def setUp(self):
        self.build_family()
        self.editor = User.objects.create_user(email="pp@example.com", password="x",
                                               role=User.Role.EDITOR)
        # Ямар ч холбоогүй хүн — picker-т үргэлж сонгогдох ёстой.
        self.free = Person.objects.create(given_name="Холбоогүй")
        self.url = reverse("genealogy:person_picker")

    def test_requires_login(self):
        response = self.client.get(self.url, {"anchor": self.child.pk})
        self.assertEqual(response.status_code, 302)

    def test_excludes_anchor_itself(self):
        self.client.force_login(self.editor)
        response = self.client.get(self.url, {"anchor": self.child.pk, "relation": "father"})
        self.assertNotContains(response, f'value="{self.child.pk}"')

    def test_excludes_descendants_for_parent_relation(self):
        self.client.force_login(self.editor)
        response = self.client.get(self.url, {"anchor": self.child.pk, "relation": "father"})
        self.assertNotContains(response, f'value="{self.grandchild.pk}"')

    def test_searches_by_name(self):
        self.client.force_login(self.editor)
        response = self.client.get(self.url, {"anchor": self.child.pk, "relation": "father",
                                              "q": "Эмээ"})
        self.assertContains(response, "Эмээ")
        self.assertNotContains(response, "Өвөг")

    def test_renders_radio_inputs_named_person(self):
        self.client.force_login(self.editor)
        response = self.client.get(self.url, {"anchor": self.child.pk, "relation": "father"})
        self.assertContains(response, 'name="person"')
        self.assertContains(response, f'value="{self.free.pk}"')

    def test_excludes_people_who_already_have_birth_family(self):
        """Хүүхэд/ах дүү болгох бол өөр төрсөн гэр бүлтэй хүн сонгогдохгүй."""
        self.client.force_login(self.editor)
        response = self.client.get(self.url, {"anchor": self.child.pk, "relation": "child"})
        self.assertNotContains(response, f'value="{self.father.pk}"')
        self.assertContains(response, f'value="{self.free.pk}"')

    def test_excludes_ancestors_for_child_relation(self):
        self.client.force_login(self.editor)
        response = self.client.get(self.url, {"anchor": self.child.pk, "relation": "child"})
        self.assertNotContains(response, f'value="{self.grandpa.pk}"')

    def test_person_detail_offers_both_flows(self):
        self.client.force_login(self.editor)
        response = self.client.get(reverse("genealogy:person_detail", args=[self.child.pk]))
        self.assertContains(response, "Шинэ хүн бүртгэх")
        self.assertContains(response, "Байгаа хүнийг холбох")


class FlexDatePersistenceTests(TreeBuilderMixin, TestCase):
    """Огноо ЧИМЭЭГҮЙ хаягдахаас сэргийлэх хамгаалалт.

    Django-ийн `ModelForm.construct_instance` нь зөвхөн `Meta.fields`-д
    жагсаасан талбарыг объект руу хуулдаг. Огнооны талбарыг `__init__` дотор
    нэмээд `Meta.fields`-д оруулахгүй бол форм дээр бөглөсөн ч хадгалагдахгүй,
    ямар ч алдаа гарахгүй — яг ийм алдаа нэг удаа гарч байсан.
    """

    FORMS = {
        PersonForm: ["birth_date", "death_date"],
        FamilyForm: ["marriage_date", "divorce_date"],
        EventForm: ["date"],
        MediaForm: ["date"],
        QuickAddPersonForm: ["birth_date"],
    }

    def setUp(self):
        self.build_family()

    def test_no_form_has_an_undeclared_flexdate_field(self):
        for form_class, expected in self.FORMS.items():
            with self.subTest(form=form_class.__name__):
                form = form_class()
                declared = sorted(n for n, f in form.fields.items()
                                  if isinstance(f, FlexDateField))
                self.assertEqual(declared, sorted(expected))
                for name in declared:
                    self.assertIn(
                        name, form._meta.fields,
                        f"{form_class.__name__}.{name} нь Meta.fields-д байхгүй — "
                        "форм дээр бөглөсөн ч хадгалагдахгүй.",
                    )

    def test_person_form_persists_dates(self):
        form = PersonForm(data={
            "given_name": "Ганбат", "patronymic": "Бат-Эрдэнийн", "clan_name": "Боржигон",
            "surname": "", "sex": "M", "visibility": "auto", "living_override": "",
            "bio": "", "birth_place": "", "death_place": "",
            "birth_date": "ойролцоогоор 1920", "death_date": "1975",
        })
        self.assertTrue(form.is_valid(), form.errors)
        person = form.save()
        self.assertIsNotNone(person.birth_date)
        self.assertEqual(person.birth_date.year, 1920)
        self.assertEqual(person.birth_date.qualifier, FlexDate.Qualifier.ABOUT)
        self.assertEqual(person.death_date.year, 1975)

    def test_person_form_updates_existing_dates(self):
        person = Person.objects.create(given_name="Хуучин",
                                       birth_date=FlexDate.create_from_text("1900"))
        form = PersonForm(data={
            "given_name": "Хуучин", "patronymic": "", "clan_name": "", "surname": "",
            "sex": "U", "visibility": "auto", "living_override": "", "bio": "",
            "birth_place": "", "death_place": "",
            "birth_date": "1901", "death_date": "1960",
        }, instance=person)
        self.assertTrue(form.is_valid(), form.errors)
        updated = form.save()
        self.assertEqual(updated.birth_date.year, 1901)
        self.assertEqual(updated.death_date.year, 1960)

    def test_person_form_can_clear_a_date(self):
        person = Person.objects.create(given_name="Цэвэрлэх",
                                       birth_date=FlexDate.create_from_text("1900"))
        form = PersonForm(data={
            "given_name": "Цэвэрлэх", "patronymic": "", "clan_name": "", "surname": "",
            "sex": "U", "visibility": "auto", "living_override": "", "bio": "",
            "birth_place": "", "death_place": "",
            "birth_date": "", "death_date": "",
        }, instance=person)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIsNone(form.save().birth_date)

    def test_family_form_persists_marriage_date(self):
        # `user` нь хүйсийн сонголтыг засах эрхтэй хүмүүсээр хязгаарладаг —
        # форм нь fail-closed тул заавал дамжуулна.
        editor = User.objects.create_user(email="flexdate@example.com", password="x",
                                          role=User.Role.EDITOR)
        form = FamilyForm(data={
            "partner1": self.father.pk, "partner2": self.mother.pk,
            "family_type": "marriage", "marriage_place": "", "notes": "",
            "marriage_date": "1955", "divorce_date": "",
        }, instance=self.parent_family, user=editor)
        self.assertTrue(form.is_valid(), form.errors)
        family = form.save()
        self.assertEqual(family.marriage_date.year, 1955)

    # -- «Ойролцоогоор» чек --------------------------------------------------
    _PERSON_DATA = {
        "given_name": "Тест", "sex": "U", "clan_name": "", "patronymic": "",
        "surname": "", "bio": "", "visibility": "auto",
        "birth_place": "", "death_place": "", "death_date": "",
    }

    def test_approx_flags_sit_right_after_their_date(self):
        form = PersonForm()
        names = list(form.fields)
        self.assertEqual(names[names.index("birth_date") + 1], "birth_date_approx")
        self.assertEqual(names[names.index("death_date") + 1], "death_date_approx")

    def test_approx_flag_initial_follows_qualifier(self):
        person = Person.objects.create(
            given_name="Тест", birth_date=FlexDate.create_from_text("ойролцоогоор 1950"))
        form = PersonForm(instance=person)
        self.assertTrue(form.fields["birth_date_approx"].initial)
        self.assertFalse(form.fields["death_date_approx"].initial)

    def test_approx_flag_sets_about(self):
        person = Person.objects.create(given_name="Тест")
        form = PersonForm(data={**self._PERSON_DATA, "birth_date": "1950",
                                "birth_date_approx": "on"}, instance=person)
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertEqual(saved.birth_date.qualifier, FlexDate.Qualifier.ABOUT)
        self.assertEqual(saved.birth_date.year, 1950)
        self.assertEqual(saved.birth_date.display, "1950?")

    def test_unchecking_approx_flag_removes_it(self):
        """Чекээр тавьсан «ойролцоогоор»-ыг авахад EXACT болно."""
        person = Person.objects.create(given_name="Тест")
        PersonForm(data={**self._PERSON_DATA, "birth_date": "1950",
                         "birth_date_approx": "on"}, instance=person).save()
        person.refresh_from_db()
        self.assertEqual(person.birth_date.qualifier, FlexDate.Qualifier.ABOUT)

        form = PersonForm(data={**self._PERSON_DATA, "birth_date": "1950"}, instance=person)
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertEqual(saved.birth_date.qualifier, FlexDate.Qualifier.EXACT)
        self.assertEqual(saved.birth_date.display, "1950")

    def test_text_approximation_survives_without_checkbox(self):
        """«ойролцоогоор 1950» гэж БИЧСЭН бол чек ирээгүй ч ABOUT хэвээр.

        Бичсэн үг нь баримтжуулсан хэлбэр (help_text) тул чек түүнийг
        устгах ёсгүй — «алга болсон чек» ба «тавиагүй чек» ялгагдахгүй.
        """
        person = Person.objects.create(
            given_name="Тест", birth_date=FlexDate.create_from_text("ойролцоогоор 1950"))
        form = PersonForm(data={**self._PERSON_DATA, "birth_date": "ойролцоогоор 1950"},
                          instance=person)
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertEqual(saved.birth_date.qualifier, FlexDate.Qualifier.ABOUT)
        self.assertEqual(saved.birth_date.raw, "ойролцоогоор 1950")

    def test_approx_flag_does_not_mutate_shared_flexdate(self):
        """Хуваалцсан FlexDate мөрийг газар дээр нь өөрчлөхгүй — шинэ мөр үүснэ."""
        shared = FlexDate.objects.create(raw="1900", qualifier=FlexDate.Qualifier.EXACT,
                                         precision=FlexDate.Precision.YEAR,
                                         date_from=date(1900, 1, 1), date_to=date(1900, 12, 31))
        person = Person.objects.create(given_name="Тест", birth_date=shared)
        form = PersonForm(data={**self._PERSON_DATA, "birth_date": "1900",
                                "birth_date_approx": "on"}, instance=person)
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        shared.refresh_from_db()
        self.assertEqual(shared.qualifier, FlexDate.Qualifier.EXACT)   # хөндөгдөөгүй
        self.assertEqual(saved.birth_date.qualifier, FlexDate.Qualifier.ABOUT)
        self.assertNotEqual(saved.birth_date.pk, shared.pk)

    def test_quick_add_form_drops_orphan_approx_flag(self):
        """death_date байхгүй тул түүний чек ч гарахгүй."""
        form = QuickAddPersonForm()
        self.assertIn("birth_date_approx", form.fields)
        self.assertNotIn("death_date_approx", form.fields)

    def test_event_form_persists_date(self):
        form = EventForm(data={
            "event_type": "OCCU", "place": "", "family": "", "description": "Төмөр замын ажилчин",
            "date": "1940-04", "participants": [],
        })
        self.assertTrue(form.is_valid(), form.errors)
        event = form.save()
        self.assertIsNotNone(event.date)
        self.assertEqual(event.date.year, 1940)
        self.assertEqual(event.date.precision, FlexDate.Precision.MONTH)

    def test_media_form_persists_date(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        form = MediaForm(
            data={"kind": "photo", "title": "Хуримын зураг", "caption": "",
                  "date": "1962", "source": ""},
            files={"file": SimpleUploadedFile("z.jpg", b"\xff\xd8\xff\xe0test", content_type="image/jpeg")},
        )
        self.assertTrue(form.is_valid(), form.errors)
        media = form.save()
        self.assertEqual(media.date.year, 1962)

    def test_dates_survive_the_edit_view(self):
        """Бүтэн HTTP замаар: форм POST → огноо санд үлдсэн эсэх."""
        editor = User.objects.create_user(email="fd@example.com", password="x",
                                          role=User.Role.EDITOR)
        self.client.force_login(editor)
        person = Person.objects.create(given_name="Тест")
        response = self.client.post(reverse("genealogy:person_edit", args=[person.pk]), {
            "given_name": "Тест", "patronymic": "Эцгийн", "clan_name": "", "surname": "",
            "sex": "U", "visibility": "auto", "living_override": "", "bio": "",
            "birth_place": "", "death_place": "",
            "birth_date": "1913-06-01", "death_date": "",
        })
        self.assertEqual(response.status_code, 302)
        person.refresh_from_db()
        self.assertIsNotNone(person.birth_date, "HTTP замаар огноо алдагдсан")
        self.assertEqual(person.birth_date.precision, FlexDate.Precision.DAY)


class PersonDetailDisplayTests(TreeBuilderMixin, TestCase):
    """Профайл хуудас огноо, газрыг БҮТНЭЭР харуулах ёстой."""

    def setUp(self):
        self.build_family()
        self.editor = User.objects.create_user(email="pd@example.com", password="x",
                                               role=User.Role.EDITOR)
        self.client.force_login(self.editor)

    def test_shows_full_date_not_just_year(self):
        place = Place.objects.create(name="Архангай", place_type=Place.PlaceType.AIMAG)
        summ = Place.objects.create(name="Цэнхэр сум", parent=place)
        self.child.birth_date = FlexDate.create_from_text("1977-04-01")
        self.child.birth_place = summ
        self.child.death_date = FlexDate.create_from_text("ойролцоогоор 2020")
        self.child.save()

        response = self.client.get(reverse("genealogy:person_detail", args=[self.child.pk]))
        self.assertContains(response, "1977-04-01")
        self.assertContains(response, "Архангай, Цэнхэр сум")
        self.assertContains(response, "2020?")

    def test_hides_block_when_no_dates_or_places(self):
        response = self.client.get(reverse("genealogy:person_detail", args=[self.child.pk]))
        self.assertNotContains(response, "Нас барсан:")


class FlexDateReuseTests(TreeBuilderMixin, TestCase):
    """Форм бөглөх бүрт шинэ FlexDate мөр үүсэхээс сэргийлнэ."""

    def setUp(self):
        self.build_family()

    def _form(self, birth):
        return PersonForm(data={
            "given_name": "Тест", "patronymic": "", "clan_name": "", "surname": "",
            "sex": "U", "visibility": "auto", "living_override": "", "bio": "",
            "birth_place": "", "death_place": "",
            "birth_date": birth, "death_date": "",
        })

    def test_identical_dates_are_shared_not_duplicated(self):
        before = FlexDate.objects.count()
        self._form("1977-04-01").save()
        self._form("1977-04-01").save()
        self.assertEqual(FlexDate.objects.count(), before + 1,
                         "Ижил огноо хоёр удаа үүсгэгдлээ")

    def test_different_dates_still_create_new_rows(self):
        before = FlexDate.objects.count()
        self._form("1977-04-01").save()
        self._form("1978-04-01").save()
        self.assertEqual(FlexDate.objects.count(), before + 2)

    def test_invalid_form_does_not_leak_a_row(self):
        """Форм унавал дараагийн ижил оролдлого нь мөр нэмэхгүй (дахин ашиглана)."""
        form = self._form("1977-04-01")
        form.is_valid()  # огноо үүснэ, гэхдээ форм хүчинтэй
        before = FlexDate.objects.count()
        bad = PersonForm(data={"given_name": "", "birth_date": "1977-04-01"})  # нэр хоосон → унана
        self.assertFalse(bad.is_valid())
        self.assertEqual(FlexDate.objects.count(), before, "Унасан форм мөр нэмлээ")


class CleanupFlexDatesCommandTests(TreeBuilderMixin, TestCase):
    def setUp(self):
        self.build_family()

    def test_dry_run_does_not_delete(self):
        orphan = FlexDate.create_from_text("6")
        out = io.StringIO()
        call_command("cleanup_flexdates", "--dry-run", stdout=out)
        self.assertIn("Устгах 1 мөр", out.getvalue())
        self.assertTrue(FlexDate.objects.filter(pk=orphan.pk).exists())

    def test_deletes_only_unreferenced(self):
        used = FlexDate.create_from_text("1900")
        self.child.birth_date = used
        self.child.save()
        orphan = FlexDate.create_from_text("6")

        call_command("cleanup_flexdates", stdout=io.StringIO())
        self.assertTrue(FlexDate.objects.filter(pk=used.pk).exists(), "Ашиглагдаж буй мөр устав")
        self.assertFalse(FlexDate.objects.filter(pk=orphan.pk).exists(), "Хэрэггүй мөр устаагүй")

    def test_reports_when_clean(self):
        used = FlexDate.create_from_text("1900")
        self.child.birth_date = used
        self.child.save()
        out = io.StringIO()
        call_command("cleanup_flexdates", stdout=out)
        self.assertIn("цэвэр", out.getvalue())


class FlexDateFormDisplayTests(TreeBuilderMixin, TestCase):
    """Засах форм дээр огнооны талбарт ID дугаар БИШ, бичсэн текст харагдана.

    ModelForm нь FK-ийн анхдагчийг pk-ээр өгдөг тул `prepare_value` үүнийг
    шийдэхгүй бол талбарт «20» гэх мэт дотоод ID харагдаж, хадгалахад нь
    тэр нь огноо болж санд ордог байв.
    """

    def setUp(self):
        self.build_family()
        self.birth = FlexDate.create_from_text("1977-04-01")
        self.child.birth_date = self.birth
        self.child.death_date = FlexDate.create_from_text("ойролцоогоор 2020")
        self.child.save()

    def test_edit_form_shows_text_not_pk(self):
        form = PersonForm(instance=self.child)
        self.assertEqual(form["birth_date"].value(), "1977-04-01")
        self.assertEqual(form["death_date"].value(), "ойролцоогоор 2020")

    def test_edit_form_html_contains_no_bare_pk(self):
        form = PersonForm(instance=self.child)
        html = str(form["birth_date"])
        self.assertIn('value="1977-04-01"', html)
        self.assertNotIn(f'value="{self.birth.pk}"', html)

    def test_saving_untouched_edit_form_keeps_same_date(self):
        """Хуудсыг нээгээд өөрчлөхгүй хадгалахад шинэ огноо ҮҮСЭХГҮЙ."""
        before = FlexDate.objects.count()
        form = PersonForm(data={
            "given_name": self.child.given_name, "patronymic": self.child.patronymic,
            "clan_name": self.child.clan_name, "surname": self.child.surname,
            "sex": self.child.sex, "visibility": self.child.visibility,
            "living_override": "", "bio": "", "birth_place": "", "death_place": "",
            "birth_date": "1977-04-01", "death_date": "ойролцоогоор 2020",
        }, instance=self.child)
        self.assertTrue(form.is_valid(), form.errors)
        person = form.save()
        self.assertEqual(person.birth_date_id, self.birth.pk, "Огноо солигдлоо")
        self.assertEqual(FlexDate.objects.count(), before, "Шинэ огноо мөр үүслээ")

    def test_edit_view_renders_the_date_text(self):
        editor = User.objects.create_user(email="dd@example.com", password="x",
                                          role=User.Role.EDITOR)
        self.client.force_login(editor)
        response = self.client.get(reverse("genealogy:person_edit", args=[self.child.pk]))
        self.assertContains(response, 'value="1977-04-01"')
        self.assertNotContains(response, f'value="{self.birth.pk}"')

    def test_admin_form_shows_text_too(self):
        from .forms import AdminPersonForm

        form = AdminPersonForm(instance=self.child)
        self.assertEqual(form["birth_date"].value(), "1977-04-01")


class LoginRequiredPolicyTests(TreeBuilderMixin, TestCase):
    """Default-deny: нэвтрээгүй хэрэглэгч ЯМАР Ч өгөгдөл харахгүй.

    Энэ бол нууцлалын үндсэн баталгаа — шинэ view нэмэгдэхэд автоматаар
    хамгаалагдана. Зөвшөөрөгдөх цорын ганц үл хамаарах зүйл нь нэвтрэх
    хуудас ба урилга хүлээн авах хуудас.
    """

    def setUp(self):
        self.build_family()

    def _data_urls(self):
        return [
            reverse("genealogy:home"),
            reverse("genealogy:person_detail", args=[self.child.pk]),
            reverse("genealogy:person_create"),
            reverse("genealogy:person_edit", args=[self.child.pk]),
            reverse("genealogy:person_delete", args=[self.child.pk]),
            reverse("genealogy:tree"),
            reverse("genealogy:tree_person", args=[self.child.pk]),
            reverse("genealogy:tree_data", args=[self.child.pk]),
            reverse("genealogy:search"),
            reverse("genealogy:person_lookup"),
            reverse("genealogy:person_picker"),
            reverse("genealogy:place_create"),
            reverse("genealogy:quick_add", args=[self.child.pk, "child"]),
            reverse("genealogy:link_relative", args=[self.child.pk, "father"]),
            reverse("genealogy:source_list"),
            reverse("genealogy:source_create"),
            reverse("genealogy:media_upload"),
            reverse("genealogy:duplicates"),
            reverse("genealogy:audit"),
            # Хүн ↔ бүртгэл
            reverse("genealogy:person_account_link", args=[self.child.pk]),
            reverse("genealogy:person_account_unlink", args=[self.child.pk]),
            reverse("genealogy:person_account_invite", args=[self.child.pk]),
            # Editor хүсэлт
            reverse("accounts:editor_request"),
            reverse("accounts:editor_request_list"),
        ]

    def test_every_data_page_redirects_anonymous_to_login(self):
        for url in self._data_urls():
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302, f"{url} нээлттэй байна!")
                self.assertIn("/accounts/login/", response["Location"])

    def test_admin_redirects_anonymous_to_its_own_login(self):
        """Admin нь өөрийн нэвтрэх хуудастай — middleware үүнийг чөлөөлдөг
        (Django-ийн тусгай тохиолдол). Хамгаалалт нь хэвээр байна."""
        response = self.client.get("/admin/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])

    def test_no_person_data_leaks_to_anonymous(self):
        """Хамгийн чухал шалгалт: нэрийн нэг ч хэсэг хариултад гарахгүй."""
        for url in (reverse("genealogy:home"), reverse("genealogy:search") + "?q=Эцэг"):
            with self.subTest(url=url):
                response = self.client.get(url, follow=True)
                self.assertNotContains(response, self.child.display_name)
                self.assertNotContains(response, self.father.display_name)
                self.assertNotContains(response, self.mother.display_name)

    def test_login_page_is_public(self):
        self.assertEqual(self.client.get(reverse("accounts:login")).status_code, 200)

    def test_invite_accept_is_public(self):
        invite = Invite.objects.create(
            email="new@example.com",
            person=self.child,   # урилга ЗААВАЛ модны хүнтэй холбогдоно
            expires_at=timezone.now() + timedelta(days=7),
        )
        response = self.client.get(reverse("accounts:invite_accept", args=[invite.token]))
        self.assertEqual(response.status_code, 200)

    def test_logged_in_member_is_not_redirected(self):
        user = User.objects.create_user(email="ok@example.com", password="x")
        self.client.force_login(user)
        for url in (reverse("genealogy:home"),
                    reverse("genealogy:person_detail", args=[self.child.pk])):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)


class HtmxLoginRedirectTests(TreeBuilderMixin, TestCase):
    """HTMX хүсэлт нэвтрэх хуудас руу шилжихдээ хуудсыг эвдэхгүй байх.

    HTMX нь 302-ыг XHR-ээр дагадаг тул нэвтрэх хуудсын HTML нь зорилтот
    элементийн дотор шургаж, хуудас эвдэрдэг байв.
    """

    def setUp(self):
        self.build_family()

    def test_htmx_search_while_anonymous_returns_hx_redirect(self):
        response = self.client.get(reverse("genealogy:search"), {"q": "Эцэг"},
                                   HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 204)
        self.assertIn("/accounts/login/", response["HX-Redirect"])
        # Хамгийн чухал: нэвтрэх хуудсын HTML хариултад ОРООГҮЙ байх ёстой
        # (эс бөгөөс HTMX түүнийг зорилтот элемент дотор шургуулж, хуудас эвдэрнэ).
        self.assertEqual(response.content, b"")

    def test_htmx_requests_to_every_data_endpoint_are_handled(self):
        for url in (reverse("genealogy:search"), reverse("genealogy:person_lookup"),
                    reverse("genealogy:person_picker"),
                    reverse("genealogy:tree_data", args=[self.child.pk]),
                    reverse("genealogy:quick_add", args=[self.child.pk, "child"])):
            with self.subTest(url=url):
                response = self.client.get(url, HTTP_HX_REQUEST="true")
                self.assertEqual(response.status_code, 204, f"{url} HTMX-д эвдэрнэ")
                self.assertIn("/accounts/login/", response["HX-Redirect"])

    def test_normal_request_still_redirects_normally(self):
        for path in (reverse("genealogy:home"), reverse("genealogy:search")):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 302)
                self.assertIn("/accounts/login/", response["Location"])

    def test_expired_session_mid_use_is_handled(self):
        """Хэрэглэгч нэвтэрсэн байгаад сесс нь дуусах үед HTMX товч эвдрэхгүй."""
        user = User.objects.create_user(email="exp@example.com", password="x",
                                        role=User.Role.EDITOR)
        self.client.force_login(user)
        self.assertEqual(self.client.get(reverse("genealogy:home")).status_code, 200)
        self.client.logout()
        response = self.client.get(reverse("genealogy:quick_add", args=[self.child.pk, "child"]),
                                   HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 204)
        self.assertIn("/accounts/login/", response["HX-Redirect"])

    def test_login_page_hides_search_box_and_data_links(self):
        response = self.client.get(reverse("accounts:login"))
        self.assertNotContains(response, 'name="q"')          # хайлтын талбар
        self.assertNotContains(response, 'hx-target="#search-results"')
        self.assertNotContains(response, ">Мод<")
        self.assertNotContains(response, ">Нүүр<")
        self.assertContains(response, "Нэвтрэх")

    def test_logged_in_user_still_sees_search(self):
        user = User.objects.create_user(email="s@example.com", password="x")
        self.client.force_login(user)
        response = self.client.get(reverse("genealogy:home"))
        self.assertContains(response, 'hx-target="#search-results"')
        self.assertContains(response, 'id="search-results"')


class DeceasedDisplayTests(TreeBuilderMixin, TestCase):
    """«Амьд» badge хэрэггүй; нас барсан хүнийг secondary (саарал) өнгөөр.

    Нэмэлт биш хасах зарчим: амьд хүнд ямар ч тэмдэг тавихгүй, зөвхөн
    нас барсан хүнийг мэдэгдэхүйц болгоно.
    """

    def setUp(self):
        self.build_family()
        self.editor = User.objects.create_user(email="dz@example.com", password="x",
                                               role=User.Role.EDITOR)
        # father-ыг нас барсан болгоно (child нь огноогүй, father нь хамаатан).
        self.father.death_date = FlexDate.create_from_text("1990")
        self.father.save()
        self.client.force_login(self.editor)

    def test_lifespan_marks_approximate_years(self):
        """Ойролцоо он нь `lifespan`-д ч «?»-тэй гарна (мод, карт)."""
        p = Person.objects.create(
            given_name="ТестОн",
            birth_date=FlexDate.create_from_text("ойролцоогоор 1950"),
            death_date=FlexDate.create_from_text("2007"))
        self.assertEqual(p.lifespan, "1950?–2007")

    def test_lifespan_plain_for_exact_years(self):
        p = Person.objects.create(
            given_name="ТестОн2",
            birth_date=FlexDate.create_from_text("1950"),
            death_date=FlexDate.create_from_text("2007"))
        self.assertEqual(p.lifespan, "1950–2007")

    def test_no_living_badge_on_any_page(self):
        for url in (reverse("genealogy:home"),
                    reverse("genealogy:search") + "?q=Эцэг",
                    reverse("genealogy:person_detail", args=[self.father.pk]),
                    reverse("genealogy:person_detail", args=[self.child.pk])):
            with self.subTest(url=url):
                self.assertNotContains(self.client.get(url), "амьд")

    def test_deceased_name_is_secondary_on_detail_page(self):
        response = self.client.get(reverse("genealogy:person_detail", args=[self.father.pk]))
        self.assertContains(response, '<h3 class="mb-1 text-secondary">')

    def test_living_name_is_not_secondary_on_detail_page(self):
        response = self.client.get(reverse("genealogy:person_detail", args=[self.child.pk]))
        self.assertNotContains(response, '<h3 class="mb-1 text-secondary">')

    def test_deceased_name_is_secondary_in_person_card(self):
        response = self.client.get(reverse("genealogy:home"))
        self.assertContains(response, 'class="text-decoration-none text-secondary"')

    def test_family_links_mark_deceased_relative(self):
        """Профайл дээрх эцэг эх/ах дүү/хүүхдийн холбоос ч мөн саарал болно."""
        response = self.client.get(reverse("genealogy:person_detail", args=[self.child.pk]))
        self.assertContains(response, f'href="/person/{self.father.pk}/"')
        self.assertContains(response, 'class="text-decoration-none text-secondary">Эцэг</a>')

    def test_living_relative_link_has_no_secondary_class(self):
        response = self.client.get(reverse("genealogy:person_detail", args=[self.child.pk]))
        self.assertContains(response, f'href="/person/{self.mother.pk}/"')
        self.assertContains(response, 'class="text-decoration-none ">Эх</a>')

    def test_tree_json_reports_deceased_flag(self):
        data = self.client.get(
            reverse("genealogy:tree_data", args=[self.child.pk]) + "?mode=ancestors&depth=2"
        ).json()
        father_node = next(n for n in data["nodes"] if n["id"] == self.father.pk)
        child_node = next(n for n in data["nodes"] if n["id"] == self.child.pk)
        self.assertTrue(father_node["deceased"])
        self.assertFalse(child_node["deceased"])

    def test_admin_shows_deceased_marker_and_no_living_marker(self):
        # Admin-д staff эрх хэрэгтэй (editor биш).
        admin_user = User.objects.create_superuser(email="dz-admin@example.com",
                                                   password="pw12345!")
        self.client.force_login(admin_user)
        response = self.client.get("/admin/genealogy/person/")
        self.assertContains(response, "нас барсан")
        self.assertNotContains(response, "амьд")


class AdoptionTests(TreeBuilderMixin, TestCase):
    """Төрсөн ба өргөмөл холбооны ялгаа — загвар, үйлчилгээ, дэлгэц, мод."""

    def setUp(self):
        self.build_family()
        self.editor = User.objects.create_user(email="ad@example.com", password="x",
                                               role=User.Role.EDITOR)

    # -- үйлчилгээ ---------------------------------------------------------

    def test_adopted_parent_creates_separate_family(self):
        """Өргөмөл эцэг нь ТӨРСӨН гэр бүлд биш, тусдаа гэр бүлд нэмэгдэнэ."""
        adopt_father = Person.objects.create(given_name="Өргөмөл эцэг", sex="M")
        link_existing(self.child, "father", adopt_father, pedigree=ChildLink.Pedigree.ADOPTED)

        birth_family = self.child.birth_family
        self.assertNotIn(adopt_father, birth_family.partners)
        adopted = self.child.parent_links.get(pedigree=ChildLink.Pedigree.ADOPTED)
        self.assertEqual(adopted.family.partner1, adopt_father)
        self.assertEqual(self.child.parent_links.count(), 2)

    def test_both_birth_and_adoptive_parents_coexist(self):
        adopt_father = Person.objects.create(given_name="Өргөмөл эцэг", sex="M")
        adopt_mother = Person.objects.create(given_name="Өргөмөл эх", sex="F")
        link_existing(self.child, "father", adopt_father, pedigree=ChildLink.Pedigree.ADOPTED)
        link_existing(self.child, "mother", adopt_mother, pedigree=ChildLink.Pedigree.ADOPTED)

        pedigrees = set(self.child.parent_links.values_list("pedigree", flat=True))
        self.assertEqual(pedigrees, {"birth", "adopted"})
        self.assertEqual(self.child.parents, [self.father, self.mother])  # birth_family хэвээр

    def test_second_birth_family_still_rejected(self):
        other = Person.objects.create(given_name="Өөр")
        with self.assertRaises(ValueError):
            link_existing(self.child, "father", other)  # pedigree=birth (анхдагч)

    def test_adopted_child_does_not_touch_birth_family(self):
        adopted = Person.objects.create(given_name="Өргөмөл хүүхэд")
        link_existing(self.child, "child", adopted, pedigree=ChildLink.Pedigree.ADOPTED)
        link = adopted.parent_links.get()
        self.assertEqual(link.pedigree, "adopted")

    def test_quick_add_with_adopted_pedigree(self):
        person = create_relative(self.child, "child", given_name="Өргөмөл",
                                 pedigree=ChildLink.Pedigree.FOSTER)
        self.assertEqual(person.parent_links.get().pedigree, "foster")

    def test_invalid_pedigree_rejected(self):
        other = Person.objects.create(given_name="Х")
        with self.assertRaises(ValueError):
            link_existing(self.child, "child", other, pedigree="хийсвэр")

    # -- дэлгэц ------------------------------------------------------------

    def test_profile_shows_adoptive_parents_with_label(self):
        adopt = Person.objects.create(given_name="Өргөмөл эцэг", sex="M")
        link_existing(self.child, "father", adopt, pedigree=ChildLink.Pedigree.ADOPTED)
        self.client.force_login(self.editor)
        html = self.client.get(
            reverse("genealogy:person_detail", args=[self.child.pk])
        ).content.decode()
        self.assertIn("Өргөмөл эцэг эх:", html)
        self.assertIn("Өргөмөл эцэг", html)
        self.assertIn("Эцэг эх:", html)          # төрсөн нь ч харагдана
        self.assertIn(self.father.display_name, html)

    def test_profile_groups_adopted_children_separately(self):
        adopted = Person.objects.create(given_name="Өргөмөл хүүхэд")
        link_existing(self.child, "child", adopted, pedigree=ChildLink.Pedigree.ADOPTED)
        self.client.force_login(self.editor)
        html = self.client.get(
            reverse("genealogy:person_detail", args=[self.child.pk])
        ).content.decode()
        self.assertIn("Өргөмөл хүүхдүүд:", html)
        self.assertIn("Хүүхдүүд:", html)

    # -- мод ---------------------------------------------------------------

    def _adoptive_setup(self):
        adopt = Person.objects.create(given_name="Өргөмөл эцэг", sex="M")
        link_existing(self.child, "father", adopt, pedigree=ChildLink.Pedigree.ADOPTED)
        return adopt

    def test_tree_includes_adoptive_parent_by_default(self):
        adopt = self._adoptive_setup()
        data = build_graph(self.child, mode="ancestors", max_depth=3, user=self.editor)
        ids = {n["id"] for n in data["nodes"]}
        self.assertIn(adopt.pk, ids)
        self.assertIn(self.father.pk, ids)

    def test_tree_bloodline_only_excludes_adoptive_parent(self):
        adopt = self._adoptive_setup()
        data = build_graph(self.child, mode="ancestors", max_depth=3, user=self.editor,
                           bloodline_only=True)
        ids = {n["id"] for n in data["nodes"]}
        self.assertNotIn(adopt.pk, ids)
        self.assertIn(self.father.pk, ids)

    def test_tree_marks_adopted_edges_with_pedigree(self):
        adopt = self._adoptive_setup()
        data = build_graph(self.child, mode="ancestors", max_depth=3, user=self.editor)
        edge = next(l for l in data["links"]
                    if l["type"] == "parent" and l["source"] == adopt.pk)
        self.assertEqual(edge["pedigree"], "adopted")
        birth_edge = next(l for l in data["links"]
                          if l["type"] == "parent" and l["source"] == self.father.pk)
        self.assertEqual(birth_edge["pedigree"], "birth")

    def test_tree_view_renders_bloodline_toggle(self):
        self.client.force_login(self.editor)
        html = self.client.get(reverse("genealogy:tree_person", args=[self.child.pk])).content.decode()
        self.assertIn('id="bloodline"', html)
        self.assertIn("Зөвхөн цусан холбоо", html)
        self.assertIn("bloodline=${bloodline", html)  # хүсэлтэд параметр дамжина

    def test_quick_add_modal_has_pedigree_selector(self):
        self.client.force_login(self.editor)
        response = self.client.get(
            reverse("genealogy:quick_add", args=[self.child.pk, "child"]),
            HTTP_HX_REQUEST="true")
        self.assertContains(response, 'name="pedigree"')
        self.assertContains(response, "Өргөмөл")


class UserMenuTests(TreeBuilderMixin, TestCase):
    """Navbar-ын хэрэглэгчийн цэс: нэр, эрх, өөрийн холбоос, гарах."""

    def setUp(self):
        self.build_family()
        self.user = User.objects.create_user(email="menu@example.com", password="x",
                                             role=User.Role.EDITOR)

    def test_menu_shows_role_and_logout(self):
        self.client.force_login(self.user)
        html = self.client.get(reverse("genealogy:home")).content.decode()
        self.assertIn("menu@example.com", html)
        self.assertIn("Засварлагч", html)
        self.assertIn(reverse("accounts:logout"), html)
        self.assertNotIn("Миний профайл", html)

    def test_menu_links_to_own_person_and_tree(self):
        Person.objects.filter(pk=self.child.pk).update(linked_user=self.user)
        self.client.force_login(self.user)
        html = self.client.get(reverse("genealogy:home")).content.decode()
        self.assertIn(reverse("genealogy:person_detail", args=[self.child.pk]), html)
        self.assertIn(reverse("genealogy:tree_person", args=[self.child.pk]), html)


class AdminOnlyPersonCreateTests(TreeBuilderMixin, TestCase):
    """Холбоогүй шинэ хүн нэмэх нь зөвхөн админд; засварлагч quick-add-аар."""

    def setUp(self):
        self.build_family()
        self.admin = User.objects.create_user(email="pc-admin@example.com", password="x",
                                              role=User.Role.ADMIN)
        self.editor = User.objects.create_user(email="pc-editor@example.com", password="x",
                                               role=User.Role.EDITOR)
        self.url = reverse("genealogy:person_create")

    def test_editor_is_redirected(self):
        self.client.force_login(self.editor)
        r = self.client.post(self.url, {"given_name": "Сул", "sex": "M"})
        self.assertEqual(r.status_code, 302)
        self.assertFalse(Person.objects.filter(given_name="Сул").exists())

    def test_admin_can_open(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_button_only_for_admin(self):
        self.client.force_login(self.editor)
        self.assertNotContains(self.client.get(reverse("genealogy:home")), self.url)
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(reverse("genealogy:home")), self.url)

    def test_editor_can_still_quick_add_relative(self):
        self.client.force_login(self.editor)
        r = self.client.get(reverse("genealogy:quick_add", args=[self.child.pk, "child"]))
        self.assertEqual(r.status_code, 200)


class CalledNameSearchTests(TestCase):
    """Авгайлдаг нэрээр ч хайлтад олдоно."""

    def test_search_matches_called_name(self):
        p = Person.objects.create(given_name="Батбаясгалан", called_name="Батаа")
        self.assertIn(p, Person.objects.search("батаа"))
        self.assertIn(p, Person.objects.search("Батбаяс"))

    def test_search_ignores_cyrillic_case(self):
        """C locale-тай санд ILIKE кирилл том/жижгийг ялгадаг — үүнийг тойрно."""
        p = Person.objects.create(given_name="Ойдов", called_name="Үндээ")
        for q in ("Үндээ", "үндээ", "ҮНДЭЭ", "ойдов", "ОЙДОВ"):
            with self.subTest(q=q):
                self.assertIn(p, Person.objects.search(q))


class MediaFileCleanupTests(TestCase):
    """Бичлэг устгах/файл солиход хуучин файл storage-аас устана."""

    def _media(self):
        from django.core.files.base import ContentFile

        m = Media.objects.create(kind=Media.Kind.PHOTO, title="Цэвэрлэгээ")
        m.file.save("clean.txt", ContentFile(b"x"), save=False)
        m.thumbnail.save("clean.jpg", ContentFile(b"y"), save=False)
        m.save()
        return m

    def test_delete_removes_file_and_thumbnail(self):
        m = self._media()
        storage, names = m.file.storage, (m.file.name, m.thumbnail.name)
        with self.captureOnCommitCallbacks(execute=True):
            m.delete()
        for name in names:
            self.assertFalse(storage.exists(name), name)

    def test_replacing_file_removes_old_one(self):
        from django.core.files.base import ContentFile

        m = self._media()
        old = m.file.name
        with self.captureOnCommitCallbacks(execute=True):
            m.file.save("clean2.txt", ContentFile(b"z"), save=True)
        self.assertFalse(m.file.storage.exists(old))
        self.assertTrue(m.file.storage.exists(m.file.name))

    def test_rollback_keeps_file(self):
        """Transaction буцаагдвал файл устахгүй (on_commit дуудагдахгүй)."""
        m = self._media()
        name = m.file.name
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            m.delete()
        self.assertEqual(len(callbacks), 2)
        self.assertTrue(m.file.storage.exists(name))
