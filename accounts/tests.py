"""Хүн ↔ хэрэглэгч холбоос, урилга, editor хүсэлтийн тестүүд.

`genealogy`-ийн олон нийтийн тестээс тусад нь — энэ файл нь бүртгэлийн
(accounts) урсгалыг шалгана.
"""

from datetime import timedelta

from unittest import mock
from uuid import UUID

from django.core import mail
from django.db import IntegrityError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from genealogy.models import ChildLink, Family, FlexDate, Person

from .emails import send_edit_request_email, send_edit_resolved_email
from .models import EditRequest, Invite, User


class AccountTestMixin:
    """Нийтлэг fixture: 3 эрх + нэг эзэнгүй, нэг нас барсан хүн."""

    def setUp(self):
        self.admin = User.objects.create_user(email="acc-admin@example.com", password="x",
                                              role=User.Role.ADMIN)
        self.editor = User.objects.create_user(email="acc-editor@example.com", password="x",
                                               role=User.Role.EDITOR)
        self.viewer = User.objects.create_user(email="acc-viewer@example.com", password="x",
                                               role=User.Role.VIEWER)
        # `created_by=None` → бүх editor засах эрхтэй
        self.person = Person.objects.create(given_name="Хүн", sex=Person.Sex.MALE)
        self.deceased = Person.objects.create(given_name="Өвөг",
                                              living_override=False)

    def link(self, person, user):
        person.linked_user = user
        person.save(update_fields=["linked_user"])


class PersonAccountLinkTests(AccountTestMixin, TestCase):
    def test_link_grants_edit_only_for_that_person(self):
        other = Person.objects.create(given_name="Бусад")
        self.link(self.person, self.viewer)
        self.assertTrue(self.person.can_edit_by(self.viewer))
        self.assertFalse(other.can_edit_by(self.viewer))

    def test_editable_by_includes_only_the_linked_record(self):
        self.link(self.person, self.viewer)
        ids = set(Person.objects.editable_by(self.viewer).values_list("pk", flat=True))
        self.assertEqual(ids, {self.person.pk})

    def test_editable_by_matches_can_edit_by_with_link(self):
        """Хоёр зам (queryset ба объект) холбоостой үед ч ижил байх ёстой."""
        self.link(self.person, self.viewer)
        for user in (self.admin, self.editor, self.viewer):
            with self.subTest(user=user.email):
                ids = set(Person.objects.editable_by(user).values_list("pk", flat=True))
                expected = {p.pk for p in Person.objects.alive() if p.can_edit_by(user)}
                self.assertEqual(ids, expected)

    def test_linked_viewer_can_open_own_edit_form(self):
        self.link(self.person, self.viewer)
        self.client.force_login(self.viewer)
        r = self.client.get(reverse("genealogy:person_edit", args=[self.person.pk]))
        self.assertEqual(r.status_code, 200)

    def test_linked_viewer_cannot_open_other_edit_form(self):
        other = Person.objects.create(given_name="Бусад")
        self.link(self.person, self.viewer)
        self.client.force_login(self.viewer)
        r = self.client.get(reverse("genealogy:person_edit", args=[other.pk]))
        self.assertEqual(r.status_code, 404)

    def test_linked_viewer_cannot_delete_own_person(self):
        """Холбоос нь ЗАСАХ эрх л өгнө — устгахгүй."""
        self.link(self.person, self.viewer)
        self.client.force_login(self.viewer)
        r = self.client.get(reverse("genealogy:person_delete", args=[self.person.pk]))
        self.assertEqual(r.status_code, 302)      # CanEditMixin → нүүр рүү

    def test_linked_viewer_cannot_manage_own_link(self):
        """Өөрийн холбоосыг өөр хүн рүү шилжүүлж эрхээ бусдад өгөхгүй."""
        self.link(self.person, self.viewer)
        self.client.force_login(self.viewer)
        r = self.client.post(
            reverse("genealogy:person_account_unlink", args=[self.person.pk]))
        self.assertEqual(r.status_code, 404)
        self.person.refresh_from_db()
        self.assertEqual(self.person.linked_user_id, self.viewer.pk)

    def test_linked_viewer_sees_own_private_person(self):
        """PRIVATE ч гэсэн өөрийн бичлэгээ харна — унших/засах зөрүү гарахгүй."""
        Person.objects.filter(pk=self.person.pk).update(
            visibility=Person.Visibility.PRIVATE)
        self.person.refresh_from_db()
        self.link(self.person, self.viewer)
        self.assertTrue(self.person.can_view(self.viewer))
        self.assertIn(self.person.pk, set(
            Person.objects.visible_to(self.viewer).values_list("pk", flat=True)))

    def test_editor_cannot_link_existing_user(self):
        self.client.force_login(self.editor)
        self.client.post(reverse("genealogy:person_account_link", args=[self.person.pk]),
                         {"user": self.viewer.pk})
        self.person.refresh_from_db()
        self.assertIsNone(self.person.linked_user_id)

    def test_admin_can_link_existing_user(self):
        self.client.force_login(self.admin)
        self.client.post(reverse("genealogy:person_account_link", args=[self.person.pk]),
                         {"user": self.viewer.pk})
        self.person.refresh_from_db()
        self.assertEqual(self.person.linked_user_id, self.viewer.pk)

    def test_user_cannot_be_linked_to_two_persons(self):
        self.link(self.person, self.viewer)
        other = Person.objects.create(given_name="Бусад")
        self.client.force_login(self.admin)
        self.client.post(reverse("genealogy:person_account_link", args=[other.pk]),
                         {"user": self.viewer.pk})
        other.refresh_from_db()
        self.assertIsNone(other.linked_user_id)

    def test_cannot_link_deceased_person(self):
        self.client.force_login(self.admin)
        self.client.post(reverse("genealogy:person_account_link", args=[self.deceased.pk]),
                         {"user": self.viewer.pk})
        self.deceased.refresh_from_db()
        self.assertIsNone(self.deceased.linked_user_id)

    def test_unlink_allowed_even_when_deceased(self):
        """Салгах нь эрх БАГАСГАЖ байна — нас барсан ч зөвшөөрнө."""
        self.link(self.person, self.viewer)
        Person.objects.filter(pk=self.person.pk).update(living_override=False)
        self.client.force_login(self.admin)
        self.client.post(reverse("genealogy:person_account_unlink",
                                 args=[self.person.pk]))
        self.person.refresh_from_db()
        self.assertIsNone(self.person.linked_user_id)

    def test_account_endpoints_require_login(self):
        for name in ("person_account_link", "person_account_unlink",
                     "person_account_invite"):
            with self.subTest(name=name):
                r = self.client.get(reverse(f"genealogy:{name}", args=[self.person.pk]))
                self.assertEqual(r.status_code, 302)


class PersonInviteTests(AccountTestMixin, TestCase):
    def make_invite(self, person=None, email="new@example.com"):
        return Invite.objects.create(
            email=email, role=User.Role.VIEWER,
            person=self.person if person is None else person,
            invited_by=self.editor,
            expires_at=timezone.now() + timedelta(days=7),
        )

    def test_editor_creates_person_invite(self):
        self.client.force_login(self.editor)
        r = self.client.post(reverse("genealogy:person_account_invite",
                                     args=[self.person.pk]),
                             {"email": "new@example.com"})
        self.assertEqual(r.status_code, 302)
        invite = Invite.objects.get(email="new@example.com")
        self.assertEqual(invite.person_id, self.person.pk)
        self.assertEqual(invite.role, User.Role.VIEWER)   # анхдагч нь «энгийн хүн»

    def test_invite_can_grant_editor_role(self):
        """«Editor» сонголтоор урихад тэр эрхээр бүртгэгдэнэ."""
        self.client.force_login(self.editor)
        self.client.post(reverse("genealogy:person_account_invite",
                                 args=[self.person.pk]),
                         {"email": "ed-new@example.com", "role": "editor"})
        invite = Invite.objects.get(email="ed-new@example.com")
        self.assertEqual(invite.role, User.Role.EDITOR)

        self.client.post(reverse("accounts:invite_accept", args=[invite.token]),
                         {"new_password1": "s3cret-pass", "new_password2": "s3cret-pass"})
        self.assertEqual(User.objects.get(email="ed-new@example.com").role,
                         User.Role.EDITOR)

    def test_invite_cannot_grant_admin(self):
        """Урилгаар админ эрх олгогдохгүй — эрх өргөжүүлэх зам хаалттай."""
        self.client.force_login(self.editor)
        self.client.post(reverse("genealogy:person_account_invite",
                                 args=[self.person.pk]),
                         {"email": "adm@example.com", "role": "admin"})
        self.assertFalse(Invite.objects.filter(email="adm@example.com").exists())

    def test_every_invite_is_tied_to_a_person(self):
        """Хүнгүй (чөлөөт) урилга үүсгэх боломжгүй."""
        with self.assertRaises(IntegrityError):
            Invite.objects.create(email="free@example.com",
                                  expires_at=timezone.now() + timedelta(days=7))

    def test_invite_list_shows_person_and_is_read_only(self):
        self.make_invite()
        self.client.force_login(self.admin)
        r = self.client.get(reverse("accounts:invite_list"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, self.person.display_name)
        self.assertNotContains(r, "Урилга үүсгэх")     # чөлөөт урилгын форм байхгүй

    def test_invite_blocked_for_deceased_person(self):
        self.client.force_login(self.editor)
        self.client.post(reverse("genealogy:person_account_invite",
                                 args=[self.deceased.pk]), {"email": "x@example.com"})
        self.assertFalse(Invite.objects.filter(email="x@example.com").exists())

    def test_second_invite_for_same_person_blocked(self):
        self.make_invite()
        self.client.force_login(self.editor)
        self.client.post(reverse("genealogy:person_account_invite",
                                 args=[self.person.pk]), {"email": "other@example.com"})
        self.assertFalse(Invite.objects.filter(email="other@example.com").exists())

    def test_accept_links_new_user_to_person(self):
        invite = self.make_invite()
        r = self.client.post(reverse("accounts:invite_accept", args=[invite.token]),
                             {"new_password1": "s3cret-pass", "new_password2": "s3cret-pass",
                              "full_name": "Шинэ Гишүүн"})
        self.assertEqual(r.status_code, 302)
        user = User.objects.get(email="new@example.com")
        self.person.refresh_from_db()
        self.assertEqual(self.person.linked_user_id, user.pk)
        self.assertEqual(user.role, User.Role.VIEWER)

    def test_accept_410_when_person_deceased(self):
        invite = self.make_invite()
        Person.objects.filter(pk=self.person.pk).update(living_override=False)
        r = self.client.get(reverse("accounts:invite_accept", args=[invite.token]))
        self.assertEqual(r.status_code, 410)

    def test_accept_410_when_person_already_linked(self):
        invite = self.make_invite()
        self.link(self.person, self.viewer)
        r = self.client.get(reverse("accounts:invite_accept", args=[invite.token]))
        self.assertEqual(r.status_code, 410)

    def test_accept_410_when_email_already_registered(self):
        invite = self.make_invite(email=self.viewer.email)
        r = self.client.post(reverse("accounts:invite_accept", args=[invite.token]),
                             {"new_password1": "s3cret-pass",
                              "new_password2": "s3cret-pass"})
        self.assertEqual(r.status_code, 410)              # 500 (IntegrityError) биш

    def test_plain_invite_without_person_still_works(self):
        invite = self.make_invite(person=None)
        r = self.client.get(reverse("accounts:invite_accept", args=[invite.token]))
        self.assertEqual(r.status_code, 200)

    def test_deleting_person_cascades_invite(self):
        invite = self.make_invite()
        token = invite.token
        self.person.delete()
        self.assertFalse(Invite.objects.filter(token=token).exists())


class EditorRequestTests(AccountTestMixin, TestCase):
    def test_editor_request_page_requires_login(self):
        r = self.client.get(reverse("accounts:editor_request"))
        self.assertEqual(r.status_code, 302)

    def test_viewer_can_submit_request(self):
        self.client.force_login(self.viewer)
        self.client.post(reverse("accounts:editor_request"), {"note": "хүсэлт"})
        self.assertTrue(EditRequest.objects.filter(
            user=self.viewer, status=EditRequest.Status.PENDING).exists())

    def test_second_submit_does_not_duplicate(self):
        self.client.force_login(self.viewer)
        for _ in range(2):
            self.client.post(reverse("accounts:editor_request"), {})
        self.assertEqual(EditRequest.objects.filter(user=self.viewer).count(), 1)

    def test_editor_is_redirected_from_request_page(self):
        self.client.force_login(self.editor)
        r = self.client.get(reverse("accounts:editor_request"))
        self.assertEqual(r.status_code, 302)

    def test_admin_approves_and_promotes_to_editor(self):
        req = EditRequest.objects.create(user=self.viewer)
        self.client.force_login(self.admin)
        self.client.post(reverse("accounts:editor_request_decide",
                                 args=[req.pk, "approve"]))
        req.refresh_from_db()
        self.viewer.refresh_from_db()
        self.assertEqual(req.status, EditRequest.Status.APPROVED)
        self.assertEqual(req.resolved_by_id, self.admin.pk)
        self.assertEqual(self.viewer.role, User.Role.EDITOR)
        # Эрх ӨРГӨЖӨӨГҮЙ — зөвхөн editor
        self.assertFalse(self.viewer.is_staff)
        self.assertFalse(self.viewer.is_superuser)

    def test_admin_rejects_and_keeps_viewer(self):
        req = EditRequest.objects.create(user=self.viewer)
        self.client.force_login(self.admin)
        self.client.post(reverse("accounts:editor_request_decide",
                                 args=[req.pk, "reject"]))
        req.refresh_from_db()
        self.viewer.refresh_from_db()
        self.assertEqual(req.status, EditRequest.Status.REJECTED)
        self.assertEqual(self.viewer.role, User.Role.VIEWER)

    def test_non_admin_cannot_decide(self):
        req = EditRequest.objects.create(user=self.viewer)
        self.client.force_login(self.editor)
        r = self.client.post(reverse("accounts:editor_request_decide",
                                     args=[req.pk, "approve"]))
        self.assertEqual(r.status_code, 302)
        self.viewer.refresh_from_db()
        self.assertEqual(self.viewer.role, User.Role.VIEWER)

    def test_request_list_requires_admin(self):
        self.client.force_login(self.editor)
        r = self.client.get(reverse("accounts:editor_request_list"))
        self.assertEqual(r.status_code, 302)


class AdminViewModeTests(TestCase):
    """Админы «энгийн / админ харагдац» — анхдагч нь админ."""

    def setUp(self):
        self.grandpa = Person.objects.create(given_name="Өвөг", sex=Person.Sex.MALE)
        self.father = Person.objects.create(given_name="Эцэг", sex=Person.Sex.MALE)
        self.child = Person.objects.create(given_name="Хүүхэд", sex=Person.Sex.MALE)
        self.grand_family = Family.objects.create(partner1=self.grandpa)
        ChildLink.objects.create(family=self.grand_family, child=self.father)
        self.family = Family.objects.create(partner1=self.father)
        ChildLink.objects.create(family=self.family, child=self.child)

        self.admin = User.objects.create_user(email="vm-admin@example.com", password="x",
                                              role=User.Role.ADMIN)
        self.editor = User.objects.create_user(email="vm-editor@example.com", password="x",
                                               role=User.Role.EDITOR)
        Person.objects.filter(pk=self.grandpa.pk).update(
            visibility=Person.Visibility.CLOSED)
        Person.objects.filter(pk=self.father.pk).update(
            visibility=Person.Visibility.PRIVATE,
            birth_date=FlexDate.create_from_text("1950"))
        self.mode_url = reverse("accounts:set_view_mode")
        self.tree_url = reverse("genealogy:tree_data", args=[self.child.pk])

    def _set(self, mode):
        return self.client.post(self.mode_url, {"mode": mode, "next": "/"})

    def _node(self, pk):
        data = self.client.get(self.tree_url + "?mode=ancestors&depth=3").json()
        return next(n for n in data["nodes"] if UUID(n["id"]) == pk)

    def test_default_is_admin_view(self):
        """Тохируулаагүй бол админ бүгдийг харна."""
        self.client.force_login(self.admin)
        self.assertFalse(self._node(self.grandpa.pk)["hidden"])

    def test_normal_view_hides_closed_and_masks_private(self):
        self.client.force_login(self.admin)
        self._set("normal")
        self.assertTrue(self._node(self.grandpa.pk)["hidden"])      # «Хаалттай» → •••
        father = self._node(self.father.pk)
        self.assertTrue(father["masked"])                           # «Хувийн» → нэр л
        self.assertEqual(father["lifespan"], "")
        self.assertNotIn("1950", str(father))

    def test_normal_view_disables_editing(self):
        self.client.force_login(self.admin)
        self._set("normal")
        r = self.client.get(reverse("genealogy:person_edit", args=[self.child.pk]))
        self.assertEqual(r.status_code, 302)      # CanEditMixin → нүүр рүү

    def test_normal_view_keeps_the_session(self):
        """Харагдац нь нэвтрэлтийг өөрчлөхгүй — зөвхөн уншихад нөлөөлнө."""
        self.client.force_login(self.admin)
        self._set("normal")
        r = self.client.get(reverse("genealogy:person_detail", args=[self.child.pk]))
        self.assertEqual(r.status_code, 200)

    def test_admin_can_switch_back_from_normal_view(self):
        """«Энгийн харагдац»-аас буцаж асаах боломжтой (бүрхүүл эрхээ хаадаг ч)."""
        self.client.force_login(self.admin)
        self._set("normal")
        self.assertEqual(self._set("admin").status_code, 302)
        self.assertFalse(self._node(self.grandpa.pk)["hidden"])

    def test_editor_can_use_the_toggle(self):
        """Засварлагч ч «Энгийн view»-оор засах эрхгүй гишүүн мэт харна."""
        self.client.force_login(self.editor)
        self.assertEqual(self._set("normal").status_code, 302)
        r = self.client.get(reverse("genealogy:person_edit", args=[self.child.pk]))
        self.assertEqual(r.status_code, 302)      # засах эрх хаагдсан
        self.assertEqual(self._set("admin").status_code, 302)   # буцааж асаана
        r = self.client.get(reverse("genealogy:person_edit", args=[self.child.pk]))
        self.assertEqual(r.status_code, 200)

    def test_tree_page_has_the_toggle_for_editor(self):
        self.client.force_login(self.editor)
        self.assertContains(self.client.get(reverse("genealogy:tree_person",
                                                    args=[self.child.pk])),
                            'id="normal-view"')

    def test_viewer_cannot_use_the_toggle(self):
        viewer = User.objects.create_user(email="vm-viewer@example.com", password="x")
        self.client.force_login(viewer)
        self.assertEqual(self._set("normal").status_code, 404)
        self.assertNotContains(self.client.get(reverse("genealogy:tree_person",
                                                       args=[self.child.pk])),
                               'id="normal-view"')

    def test_unknown_mode_is_rejected(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.post(self.mode_url, {"mode": "hack"}).status_code, 404)

    def test_toggle_requires_post(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(self.mode_url).status_code, 405)

    def test_next_is_limited_to_internal_paths(self):
        """Гадаад хаяг руу шилжүүлэхгүй (open redirect)."""
        self.client.force_login(self.admin)
        r = self.client.post(self.mode_url, {"mode": "normal", "next": "https://evil.example"})
        self.assertEqual(r["Location"], reverse("genealogy:home"))

    def test_tree_page_has_the_toggle_for_admin(self):
        self.client.force_login(self.admin)
        html = self.client.get(
            reverse("genealogy:tree_person", args=[self.child.pk])).content.decode()
        self.assertIn('id="normal-view"', html)
        self.assertNotContains(self.client.get(reverse("genealogy:home")),
                               reverse("accounts:set_view_mode"))   # navbar-т биш

    def test_toggle_is_a_checkbox_not_a_navbar_menu(self):
        """Модны хуудсан дээрх энгийн чекбокс — navbar-ын цэс биш."""
        self.client.force_login(self.admin)
        self.assertNotContains(self.client.get(reverse("genealogy:home")),
                               "Админ харагдац")
        self.assertContains(self.client.get(reverse("genealogy:tree_person",
                                                    args=[self.child.pk])),
                            "Энгийн view")

    def test_checkbox_off_switches_back_to_admin_view(self):
        """Чекбоксыг авахад админ харагдац буцаж ирнэ."""
        self.client.force_login(self.admin)
        self.client.post(self.mode_url, {"normal": "1", "next": "/"})
        self.assertTrue(self._node(self.father.pk)["masked"])
        self.client.post(self.mode_url, {"next": "/"})       # чекбокс тэмдэглээгүй
        self.assertFalse(self._node(self.father.pk)["masked"])



@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class PersonEditEmailTests(TestCase):
    """Батламжийн имэйл туслахууд — хэлбэр ба алдааны тэсвэр."""

    def setUp(self):
        mail.outbox.clear()

    def test_request_email_carries_the_diff_and_link(self):
        send_edit_request_email("apo@example.com", "Бат Болд", "hereglegch@example.com",
                                "  Өөрийн нэр: Хуучин → Шинэ", "https://жишээ/холбоос")

        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["apo@example.com"])
        self.assertIn("батлах хүсэлт", message.subject)
        self.assertIn("Бат Болд", message.body)
        self.assertIn("hereglegch@example.com", message.body)
        self.assertIn("Хуучин → Шинэ", message.body)
        self.assertIn("https://жишээ/холбоос", message.body)

    def test_approved_email(self):
        send_edit_resolved_email("hereglegch@example.com", "Бат Болд", True, "https://жишээ")

        self.assertIn("батлагдлаа", mail.outbox[0].subject)
        self.assertIn("батлагдлаа", mail.outbox[0].body)

    def test_rejected_email_mentions_restore(self):
        send_edit_resolved_email("hereglegch@example.com", "Бат Болд", False, "https://жишээ")

        self.assertIn("татгалзлаа", mail.outbox[0].subject)
        self.assertIn("сэргээгдлээ", mail.outbox[0].body)

    def test_send_failure_is_swallowed(self):
        """SMTP унасан ч урсгал зогсохгүй (`send_invite_email`-ийн адил)."""
        with mock.patch("accounts.emails.send_mail", side_effect=Exception("SMTP унасан")):
            send_edit_request_email("apo@example.com", "Бат", "х@example.com", "—", "—")
            send_edit_resolved_email("apo@example.com", "Бат", True, "—")
        self.assertEqual(mail.outbox, [])
