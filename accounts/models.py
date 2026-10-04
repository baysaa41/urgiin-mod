"""Хэрэглэгч, эрх, урилга.

Бүртгэл нээлттэй БИШ — зөвхөн админы илгээсэн урилгаар гишүүн нэгдэнэ.
Нэвтрэх нэр нь имэйл хаяг.
"""

import uuid

from django.conf import settings
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.utils import timezone


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, email, password, **extra):
        if not email:
            raise ValueError("Имэйл хаяг заавал шаардлагатай.")
        email = self.normalize_email(email).lower()
        user = self.model(email=email, **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra):
        extra.setdefault("role", User.Role.VIEWER)
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra)

    def create_superuser(self, email, password=None, **extra):
        extra.setdefault("role", User.Role.ADMIN)
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        extra.setdefault("is_active", True)
        if extra.get("is_staff") is not True:
            raise ValueError("Супер хэрэглэгч is_staff=True байх ёстой.")
        return self._create_user(email, password, **extra)


class User(AbstractBaseUser, PermissionsMixin):
    """Ургийн модны хэрэглэгч. Нэвтрэх нэр = имэйл."""

    class Role(models.TextChoices):
        ADMIN = "admin", "Админ"          # бүх эрх, хэрэглэгч удирдана
        EDITOR = "editor", "Засварлагч"   # мэдээлэл оруулах/засах
        VIEWER = "viewer", "Үзэгч"        # зөвхөн харах

    email = models.EmailField("Имэйл", unique=True)
    full_name = models.CharField("Бүтэн нэр", max_length=150, blank=True)
    role = models.CharField("Эрх", max_length=20, choices=Role.choices, default=Role.VIEWER)
    is_active = models.BooleanField("Идэвхтэй", default=True)
    is_staff = models.BooleanField("Admin сайт ашиглах эрх", default=False)
    date_joined = models.DateTimeField("Бүртгүүлсэн", default=timezone.now)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    class Meta:
        db_table = "accounts_user"
        verbose_name = "Хэрэглэгч"
        verbose_name_plural = "Хэрэглэгчид"
        ordering = ["email"]

    def __str__(self):
        return self.full_name or self.email

    @property
    def can_edit(self):
        """Мэдээлэл оруулах/засах эрхтэй эсэх."""
        return self.is_active and (self.is_superuser or self.role in {self.Role.ADMIN, self.Role.EDITOR})

    @property
    def is_admin(self):
        return self.is_active and (self.is_superuser or self.role == self.Role.ADMIN)


class Invite(models.Model):
    """Нэг удаагийн урилга. Токен нь DB-д хадгалагдана — хүчингүй болгох боломжтой.

    Урилга нь ЗААВАЛ модонд бичигдсэн хүнтэй холбогдоно: урьсан хүн нь
    тухайн хүний имэйлээр орж, нууц үгээ тавихад `Person.linked_user`
    тавигдана. Хаягаар нь чөлөөтэй урих зам байхгүй — хэрэглэгч бүр модны
    хүнтэй холбоотой байх ёстой.
    """

    email = models.EmailField("Имэйл")
    token = models.UUIDField("Токен", default=uuid.uuid4, unique=True, editable=False)
    role = models.CharField("Олгох эрх", max_length=20, choices=User.Role.choices, default=User.Role.VIEWER)
    person = models.ForeignKey(
        "genealogy.Person", on_delete=models.CASCADE,
        related_name="invites", verbose_name="Хүн",
        help_text="Хүлээн авахад энэ хүнтэй холбогдоно.",
    )
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="invites_sent",
        verbose_name="Урьсан",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField("Хугацаа дуусах")
    accepted_at = models.DateTimeField("Хүлээн авсан", null=True, blank=True)
    accepted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="invite_accepted", verbose_name="Хүлээн авсан хэрэглэгч",
    )

    class Meta:
        db_table = "accounts_invite"
        verbose_name = "Урилга"
        verbose_name_plural = "Урилгууд"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.email} ({self.get_role_display()})"

    @property
    def is_valid(self):
        return self.accepted_at is None and timezone.now() < self.expires_at


class EditRequest(models.Model):
    """«Editor болох» хүсэлт. Админ апп дотроос шийдвэрлэнэ.

    `Invite`-ийн `accepted_at`/`is_valid` хэв маягийг дагана — pending төлөв
    нь олон удаагийн хүсэлтээс сэргийлж DB-д хязгаарлагдана.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Хүлээгдэж байна"
        APPROVED = "approved", "Зөвшөөрсөн"
        REJECTED = "rejected", "Татгалзсан"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="edit_requests", verbose_name="Хэрэглэгч",
    )
    status = models.CharField("Төлөв", max_length=20, choices=Status.choices,
                              default=Status.PENDING)
    note = models.TextField("Тайлбар", blank=True)
    created_at = models.DateTimeField("Үүссэн", auto_now_add=True)
    resolved_at = models.DateTimeField("Шийдсэн", null=True, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="edit_requests_resolved", verbose_name="Шийдсэн",
    )

    class Meta:
        db_table = "accounts_editrequest"
        verbose_name = "Editor хүсэлт"
        verbose_name_plural = "Editor хүсэлтүүд"
        ordering = ["-created_at"]
        constraints = [
            # Нэг хэрэглэгчид нэг л хүлээгдэж буй хүсэлт байна.
            models.UniqueConstraint(
                fields=["user"], condition=models.Q(status="pending"),
                name="unique_pending_edit_request_per_user",
            )
        ]

    def __str__(self):
        return f"{self.user} — {self.get_status_display()}"

    @property
    def is_pending(self):
        return self.status == self.Status.PENDING
