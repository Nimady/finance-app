from django.conf import settings
from django.db import models, transaction
from django.db.models import F
from partners.models import Partner
from products.models import Product
from django.utils import timezone
from decimal import Decimal
from company.models import CompanySetting
from datetime import timedelta
import re


# ----------------------
# BASE INVOICE
# ----------------------

class BaseInvoice(models.Model):

    invoice_number = models.CharField(
        max_length=50,
        unique=True,
        blank=True,
        editable=True
    )

    invoice_date = models.DateField(default=timezone.now)

    importer = models.ForeignKey(
        Partner,
        related_name="%(class)s_importer",
        limit_choices_to={'partner_type': 'importer'},
        on_delete=models.PROTECT,
        null=True,
        blank=True
    )

    end_user = models.ForeignKey(
        Partner,
        related_name="%(class)s_enduser",
        limit_choices_to={'partner_type': 'enduser'},
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )

    freight = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    discount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    vat_percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))
    delivery_time = models.CharField(max_length=255, blank=True)
    terms_conditions = models.CharField(max_length=255, blank=True)

    class Meta:
        abstract = True

    # ----------------------
    # SUBTOTAL
    # ----------------------

    def subtotal(self):

        total = Decimal("0.00")

        for item in self.items.all():
            total += item.total_line()

        return total

    # ----------------------
    # VAT
    # ----------------------

    def vat_amount(self):
        return (self.subtotal() * self.vat_percent) / Decimal("100")

    # ----------------------
    # TOTAL
    # ----------------------

    def total_amount(self):

        return self.subtotal() + self.vat_amount() + self.freight - self.discount

    # ----------------------
    # AUTO NUMBER
    # ----------------------

    def save(self, *args, **kwargs):

        if not self.invoice_number:

            company = CompanySetting.objects.first()
            year = company.year if company and company.year else timezone.now().year

            previous_invoices = self.__class__.objects.filter(
                models.Q(invoice_number__startswith=f"FR-{year}-") |
                models.Q(invoice_number__startswith=f"FR/{year}/")
            ).values_list("invoice_number", flat=True)

            last_number = 0
            for invoice_number in previous_invoices:
                suffix = re.split(r"[-/]", str(invoice_number))[-1]
                match = re.search(r"(\d+)$", suffix)
                if match:
                    last_number = max(last_number, int(match.group(1)))

            new_number = last_number + 1

            self.invoice_number = f"FR-{year}-{new_number:04d}"
        else:
            self.invoice_number = self.invoice_number.replace("/", "-")

        super().save(*args, **kwargs)


# ----------------------
# PROFORMA INVOICE
# ----------------------

class ProformaInvoice(BaseInvoice):
    our_reference = models.CharField(max_length=100, blank=True)
    price_for = models.CharField(max_length=255, blank=True)

    def ready_for_report(self):

        company = CompanySetting.objects.first()

        if not company:
            return False

        validity_days = company.proforma_validity

        expire_date = self.invoice_date + timedelta(days=validity_days)

        return timezone.now().date() >= expire_date

    # ----------------------
    # CONVERT TO COMMERCIAL
    # ----------------------

    def convert_to_commercial(self, *, user_initiated=False):
        if not user_initiated:
            return None

        with transaction.atomic():
            commercial = CommercialInvoice.objects.filter(
                our_reference=self.invoice_number
            ).first()

            if commercial:
                commercial.invoice_date = self.invoice_date
                commercial.importer = self.importer
                commercial.end_user = self.end_user
                commercial.freight = self.freight
                commercial.discount = self.discount
                commercial.vat_percent = self.vat_percent
                commercial.our_reference = self.invoice_number
                commercial.price_for = self.price_for
                commercial.delivery_time = self.delivery_time
                commercial.terms_conditions = self.terms_conditions
                commercial.save()

                for commercial_item in commercial.items.select_related("product"):
                    commercial_item.delete()
            else:
                commercial = CommercialInvoice.objects.create(
                    invoice_date=self.invoice_date,
                    importer=self.importer,
                    end_user=self.end_user,
                    freight=self.freight,
                    discount=self.discount,
                    vat_percent=self.vat_percent,
                    our_reference=self.invoice_number,
                    price_for=self.price_for,
                    delivery_time=self.delivery_time,
                    terms_conditions=self.terms_conditions,
                )

            for item in self.items.select_related("product").order_by("pk"):
                CommercialInvoiceItem.objects.create(
                    invoice=commercial,
                    product=item.product,
                    hs_code=item.hs_code,
                    part_number=item.part_number,
                    quantity=item.quantity,
                    unit_price=item.unit_price
                )

            return commercial

    def __str__(self):
        return f"Proforma {self.invoice_number}"


# ----------------------
# PROFORMA ITEM
# ----------------------

class ProformaInvoiceItem(models.Model):

    invoice = models.ForeignKey(
        ProformaInvoice,
        related_name="items",
        on_delete=models.CASCADE
    )

    product = models.ForeignKey(
        Product,
        related_name="proforma_items",
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )

    hs_code = models.CharField(max_length=20, blank=True)

    part_number = models.CharField(max_length=255, blank=True)

    item_date = models.DateField(null=True, blank=True)

    quantity = models.IntegerField(default=1)

    unit_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00")
    )

    def save(self, *args, **kwargs):
        if self.product_id:
            if not self.hs_code:
                self.hs_code = self.product.hs_code or "-"
            if not self.part_number:
                self.part_number = self.product.part_number or ""

        if not self.hs_code:
            self.hs_code = "-"

        if not self.item_date and self.invoice_id and self.invoice and self.invoice.invoice_date:
            self.item_date = self.invoice.invoice_date

        super().save(*args, **kwargs)

    def total_line(self):

        return Decimal(self.quantity) * self.unit_price

    def __str__(self):

        return f"{self.product or 'Deleted product'} - {self.invoice}"


# ----------------------
# COMMERCIAL INVOICE
# ----------------------

class CommercialInvoice(BaseInvoice):

    class Meta:
        permissions = (
            (
                "view_all_documents",
                "Can view all users' invoices and purchase orders",
            ),
            (
                "view_commercial_invoice_reports",
                "Can access Commercial Invoice reports",
            ),
        )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="commercial_invoices_created",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        editable=False,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    our_order_no = models.CharField(max_length=100, blank=True)

    our_reference = models.CharField(max_length=100, blank=True)
    price_for = models.CharField(max_length=255, blank=True)
    packing_specification = models.CharField(max_length=255, blank=True)
    dispatching_note = models.CharField(max_length=255, blank=True)

    def __str__(self):

        return f"Commercial {self.invoice_number}"


# ----------------------
# COMMERCIAL ITEM
# ----------------------

class CommercialInvoiceItem(models.Model):

    invoice = models.ForeignKey(
        CommercialInvoice,
        related_name="items",
        on_delete=models.CASCADE
    )

    product = models.ForeignKey(
        Product,
        related_name="commercial_items",
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )

    hs_code = models.CharField(max_length=20, blank=True)

    part_number = models.CharField(max_length=255, blank=True)

    quantity = models.IntegerField(default=1)

    unit_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00")
    )

    def save(self, *args, **kwargs):
        if self.product_id:
            if not self.hs_code:
                self.hs_code = self.product.hs_code or "-"
            if not self.part_number:
                self.part_number = self.product.part_number or ""

        if not self.hs_code:
            self.hs_code = "-"

        previous_item = None

        if self.pk:
            previous_item = CommercialInvoiceItem.objects.get(pk=self.pk)

        with transaction.atomic():
            super().save(*args, **kwargs)

            if previous_item:
                if previous_item.product_id == self.product_id:
                    quantity_diff = self.quantity - previous_item.quantity

                    if quantity_diff and self.product_id:
                        Product.objects.filter(pk=self.product_id).update(
                            unit_qty=F("unit_qty") - quantity_diff
                        )
                else:
                    if previous_item.product_id:
                        Product.objects.filter(pk=previous_item.product_id).update(
                            unit_qty=F("unit_qty") + previous_item.quantity
                        )
                    if self.product_id:
                        Product.objects.filter(pk=self.product_id).update(
                            unit_qty=F("unit_qty") - self.quantity
                        )
            elif self.product_id:
                Product.objects.filter(pk=self.product_id).update(
                    unit_qty=F("unit_qty") - self.quantity
                )

    def delete(self, *args, **kwargs):
        with transaction.atomic():
            if self.product_id:
                Product.objects.filter(pk=self.product_id).update(
                    unit_qty=F("unit_qty") + self.quantity
                )
            super().delete(*args, **kwargs)

    def total_line(self):

        return Decimal(self.quantity) * self.unit_price

    def __str__(self):

        return f"{self.product or 'Deleted product'} - {self.invoice}"


class CommercialInvoicePacking(models.Model):

    invoice = models.ForeignKey(
        CommercialInvoice,
        related_name="packing_entries",
        on_delete=models.CASCADE
    )

    no_packing = models.CharField(max_length=255, blank=True)
    gross_weight = models.DecimalField(max_digits=11, decimal_places=3, default=Decimal("0.000"))
    net_weight = models.DecimalField(max_digits=11, decimal_places=3, default=Decimal("0.000"))
    dimension_length = models.DecimalField(max_digits=11, decimal_places=3, default=Decimal("0.000"))
    dimension_width = models.DecimalField(max_digits=11, decimal_places=3, default=Decimal("0.000"))
    dimension_height = models.DecimalField(max_digits=11, decimal_places=3, default=Decimal("0.000"))

    def __str__(self):
        label = self.no_packing or "Packing"
        return f"{label} - {self.invoice}"
