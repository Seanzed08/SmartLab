from django.db import models
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager

# -------- Custom user (needed for Django Admin access) --------
class CustomUserManager(BaseUserManager):
    def create_user(self, email, password=None, role=None):
        if not email:
            raise ValueError("Users must have an email address")
        user = self.model(email=self.normalize_email(email), role=role or "admin")
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None):
        user = self.create_user(email, password, role='admin')
        user.is_admin = True
        user.is_staff = True         # admin site access
        user.is_superuser = True     # full perms
        user.save(using=self._db)
        return user

class CustomUser(AbstractBaseUser):
    email = models.EmailField(unique=True)
    role = models.CharField(max_length=20, default="admin")
    is_active = models.BooleanField(default=True)
    is_admin = models.BooleanField(default=False)

    # Required by Django admin/permissions
    is_staff = models.BooleanField(default=False)
    is_superuser = models.BooleanField(default=False)

    objects = CustomUserManager()
    USERNAME_FIELD = 'email'

    def __str__(self):
        return self.email

    # minimal perms hooks
    def has_perm(self, perm, obj=None):
        return self.is_superuser or self.is_admin

    def has_module_perms(self, app_label):
        return True

# -------- Branding singleton (the thing you edit) --------
def branding_upload_path(instance, filename):
    return f"branding/{filename}"

class SiteSetting(models.Model):
    singleton_key = models.CharField(max_length=16, unique=True, default="SITE")
    brand_name = models.CharField(max_length=100, default="SMARTLAB")
    brand_logo = models.ImageField(upload_to=branding_upload_path, blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return "Site Settings"

    def save(self, *args, **kwargs):
        self.singleton_key = "SITE"  # enforce singleton
        super().save(*args, **kwargs)

    @classmethod
    def get_solo(cls):
        obj, _ = cls.objects.get_or_create(singleton_key="SITE")
        return obj
