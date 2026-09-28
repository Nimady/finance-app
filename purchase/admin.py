from decimal import Decimal
from io import BytesIO
from pathlib import Path
from urllib.parse import unquote, urlparse

from django.conf import settings
from django.contrib import admin
from django import forms
from django.core.exceptions import PermissionDenied, ValidationError
from django.forms.formsets import all_valid
from django.db.models import DecimalField, ExpressionWrapper, F, Sum, Value
from django.db.models.functions import Coalesce, TruncMonth
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import path
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html
from financeapp.admin_mixins import PageSizeAdminMixin, SaveRedirectToWelcomeMixin
from financeapp.access_control import (
    can_view_all_documents,
    can_view_purchase_reports,
    is_owned_by_user,
    is_staff_role,
)
from financeapp.filename_utils import document_pdf_filename
from financeapp.pdf_rendering import get_pdf_fallback_reason, should_try_weasyprint
from invoices.pdf_builder import build_purchase_order_pdf, build_purchase_report_pdf, format_currency_symbol

from company.models import CompanySetting
from partners.models import Partner
from products.models import Product

from .models import PurchaseOrder, PurchaseOrderItem


# ----------------------
# PURCHASE ORDER ITEMS
# ----------------------

class PurchaseOrderItemInline(admin.TabularInline):
    model = PurchaseOrderItem
    ordering = ("pk",)
    verbose_name = "ligne"
    verbose_name_plural = "lignes"
    extra = 1

    fields = (
        "product",
        "hs_code",
        "part_number",
        "quantity",
        "unit_price",
        "total_line",
    )

    readonly_fields = (
        "total_line",
    )

    autocomplete_fields = ("product",)

    def total_line(self, obj):
        return obj.total_line()

    total_line.short_description = "Total Amount"


class PurchaseOrderAdminForm(forms.ModelForm):
    class Meta:
        model = PurchaseOrder
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        company = CompanySetting.objects.first()

        for field_name in ("freight", "vat_percent"):
            self.fields[field_name].required = False

        if company and not self.instance.pk:
            self.fields["vat_percent"].initial = company.vat_amount
            self.fields["purchase_date"].initial = self._get_default_company_date(company.year)

    def _get_default_company_date(self, year):
        today = timezone.now().date()
        try:
            return today.replace(year=year)
        except ValueError:
            return today.replace(year=year, day=28)

    def clean_vat_percent(self):
        vat_percent = self.cleaned_data.get("vat_percent")

        if vat_percent in (None, ""):
            company = CompanySetting.objects.first()
            if company:
                return company.vat_amount

        return vat_percent

    def clean_freight(self):
        return self.cleaned_data.get("freight") or Decimal("0.00")


# ----------------------
# PURCHASE ORDER ADMIN
# ----------------------

@admin.register(PurchaseOrder)
class PurchaseOrderAdmin(SaveRedirectToWelcomeMixin, PageSizeAdminMixin, admin.ModelAdmin):
    changelist_template = "admin/purchase/purchaseorder/change_list.html"
    change_form_template = "admin/purchase/purchaseorder/change_form.html"
    list_max_show_all = 100
    form = PurchaseOrderAdminForm

    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        if (
            object_id
            and not can_view_all_documents(request.user)
            and self.model._default_manager.filter(pk=object_id)
            .exclude(created_by=request.user)
            .exists()
        ):
            raise PermissionDenied
        return super().changeform_view(request, object_id, form_url, extra_context)

    readonly_fields = (
        "gross_value_display",
        "vat_amount_display",
        "total_amount_display",
        "created_by",
    )

    fieldsets = (
        ("Order Overview", {
            "fields": (
                "purchase_number",
                "purchase_date",
                "seller",
                ("sent_by", "shipment"),
                "created_by",
            )
        }),
        ("Financial Settings", {
            "fields": (
                ("freight", "vat_percent"),
                ("gross_value_display", "vat_amount_display", "total_amount_display"),
            )
        }),
        ("Commercial Terms", {
            "fields": (
                "sales_condition",
                "payment_condition",
                "delivery_terms",
            )
        }),
    )

    list_display = (
        "purchase_number",
        "purchase_date_display",
        "seller",
        "created_by",
        "amount_display",
        "pdf_link",
    )

    list_filter = ("purchase_date", "seller")
    search_fields = (
        "purchase_number",
        "seller__description",
        "seller__email",
        "requester__description",
        "requester__email",
        "sent_by",
        "shipment",
        "items__product__description",
        "items__product__part_number",
        "items__description",
        "items__part_number",
        "items__hs_code",
    )

    autocomplete_fields = ("seller", "requester")

    inlines = [PurchaseOrderItemInline]

    class Media:
        css = {
            "all": ("admin/css/large_partner_autocomplete.css",),
        }
        js = (
            "admin/js/raw_id_label_display.js",
        )

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        formfield = super().formfield_for_foreignkey(db_field, request, **kwargs)
        if db_field.name in {"seller", "requester"}:
            existing_classes = formfield.widget.attrs.get("class", "")
            formfield.widget.attrs["class"] = (
                f"{existing_classes} partner-large-select"
            ).strip()
        return formfield

    def get_default_purchase_date(self):
        company = CompanySetting.objects.first()
        today = timezone.now().date()
        if not company or not company.year:
            return today
        try:
            return today.replace(year=company.year)
        except ValueError:
            return today.replace(year=company.year, day=28)

    def get_default_vat_percent(self):
        company = CompanySetting.objects.first()
        return company.vat_amount if company else Decimal("0.00")

    def create_draft_purchase_order(self, request=None):
        return PurchaseOrder.objects.create(
            purchase_date=self.get_default_purchase_date(),
            vat_percent=self.get_default_vat_percent(),
            created_by=request.user if request else None,
        )

    def save_model(self, request, obj, form, change):
        if not change and not obj.created_by_id:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)

    def has_view_permission(self, request, obj=None):
        allowed = super().has_view_permission(request, obj)
        if not allowed or can_view_all_documents(request.user) or obj is None:
            return allowed
        return is_owned_by_user(obj, request.user)

    def has_change_permission(self, request, obj=None):
        allowed = super().has_change_permission(request, obj)
        if not allowed or can_view_all_documents(request.user) or obj is None:
            return allowed
        return is_owned_by_user(obj, request.user)

    def has_delete_permission(self, request, obj=None):
        if is_staff_role(request.user):
            return False
        return super().has_delete_permission(request, obj)

    def render_change_form(self, request, context, add=False, change=False, form_url="", obj=None):
        context["invoice_autosave_url"] = self.get_purchase_autosave_url(obj) if obj and obj.pk else ""
        context["purchase_pdf_url"] = self.get_purchase_pdf_url(obj) if obj and obj.pk else ""
        return super().render_change_form(request, context, add, change, form_url, obj)

    def add_view(self, request, form_url="", extra_context=None):
        if request.method == "GET" and not request.GET.get("_popup"):
            if not self.has_add_permission(request):
                raise PermissionDenied
            draft = self.create_draft_purchase_order(request)
            return redirect(reverse("admin:purchase_purchaseorder_change", args=[draft.pk]))
        return super().add_view(request, form_url, extra_context)

    def response_change(self, request, obj):
        if "_save_and_pdf" in request.POST and "_save" not in request.POST:
            return redirect(request.POST.get("_save_and_pdf_url") or self.get_purchase_pdf_url(obj))
        return super().response_change(request, obj)

    def get_purchase_autosave_url(self, obj):
        return reverse("admin:purchase_purchaseorder_autosave", args=[obj.pk])

    def get_purchase_pdf_url(self, obj):
        return reverse("admin:purchase_purchaseorder_pdf", args=[obj.pk])

    def get_company_year(self):
        company = CompanySetting.objects.first()
        return company.year if company and company.year else None

    def changelist_view(self, request, extra_context=None):
        if (request.GET.get("q") or "").strip():
            query = request.GET.copy()
            removed_year_filter = False
            for key in list(query.keys()):
                if key.startswith("purchase_date__year"):
                    query.pop(key, None)
                    removed_year_filter = True
            if removed_year_filter:
                return redirect(f"{request.path}?{query.urlencode()}")

        extra_context = extra_context or {}
        extra_context["can_view_reports"] = can_view_purchase_reports(request.user)
        return super().changelist_view(request, extra_context=extra_context)

    def has_explicit_year_filter(self, request, field_name):
        return any(key.startswith(field_name) for key in request.GET.keys())

    def should_apply_default_year_filter(self, request):
        match = getattr(request, "resolver_match", None)
        return bool(match and match.url_name and match.url_name.endswith("_changelist"))

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        if not can_view_all_documents(request.user):
            queryset = queryset.filter(created_by=request.user)
        company_year = self.get_company_year()
        if (
            company_year and
            not is_staff_role(request.user) and
            self.should_apply_default_year_filter(request) and
            not self.has_explicit_year_filter(request, "purchase_date") and
            not (request.GET.get("q") or "").strip()
        ):
            queryset = queryset.filter(purchase_date__year=company_year)
        return queryset

    def get_search_results(self, request, queryset, search_term):
        normalized_term = (search_term or "").strip()
        if normalized_term:
            number_variants = {
                normalized_term,
                normalized_term.replace("/", "-"),
            }
            if normalized_term.startswith("PO-"):
                number_variants.add(
                    "PO/" + normalized_term.removeprefix("PO-")
                )

            search_queryset = (
                queryset
                if request and not can_view_all_documents(request.user)
                else self.model._default_manager.all()
            )
            exact_numbers = search_queryset.filter(
                purchase_number__in=number_variants
            ).order_by("-purchase_date", "-id")
            if exact_numbers.exists():
                return exact_numbers, False

        return super().get_search_results(request, queryset, search_term)

    def purchase_date_display(self, obj):
        if obj.purchase_date:
            return obj.purchase_date.strftime("%d/%m/%Y")
        return "-"

    purchase_date_display.short_description = "Date"
    purchase_date_display.admin_order_field = "purchase_date"

    def amount_display(self, obj):
        return obj.total_amount()

    amount_display.short_description = "Amount"

    def pdf_link(self, obj):
        if not obj.pk:
            return "-"

        return format_html(
            '<a class="purchase-list-pdf-button" href="{}" target="_blank">PDF</a>',
            self.get_purchase_pdf_url(obj),
        )

    pdf_link.short_description = "PDF"

    def gross_value_display(self, obj):
        return obj.gross_value() if obj.pk else Decimal("0.00")

    gross_value_display.short_description = "Gross Value"

    def vat_amount_display(self, obj):
        return obj.vat_amount() if obj.pk else Decimal("0.00")

    vat_amount_display.short_description = "VAT Amount"

    def total_amount_display(self, obj):
        return obj.total_amount() if obj.pk else Decimal("0.00")

    total_amount_display.short_description = "Total Amount"

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "<int:object_id>/autosave/",
                self.admin_site.admin_view(self.autosave),
                name="purchase_purchaseorder_autosave",
            ),
            path(
                "<int:object_id>/pdf/",
                self.admin_site.admin_view(self.export_purchase_pdf),
                name="purchase_purchaseorder_pdf",
            ),
            path(
                "report/",
                self.admin_site.admin_view(self.purchase_report),
                name="purchase_report",
            ),
            path(
                "report/pdf/",
                self.admin_site.admin_view(self.purchase_report_pdf),
                name="purchase_report_pdf",
            ),
        ]
        return custom_urls + urls

    def autosave(self, request, object_id):
        if request.method != "POST":
            return JsonResponse({"ok": False, "error": "POST required."}, status=405)

        obj = get_object_or_404(PurchaseOrder, pk=object_id)
        if not self.has_change_permission(request, obj):
            raise PermissionDenied
        form_class = self.get_form(request, obj, change=True)
        post_data = request.POST.copy()
        self._remove_new_inline_forms_from_autosave(post_data)
        form = form_class(post_data, request.FILES, instance=obj)

        original_post = request.POST
        request.POST = post_data
        try:
            formsets, inline_instances = self._create_formsets(request, form.instance, change=True)
        finally:
            request.POST = original_post

        if form.is_valid() and all_valid(formsets):
            new_object = self.save_form(request, form, change=True)
            self.save_model(request, new_object, form, change=True)
            form.save_m2m()
            self.save_related(request, form, formsets, change=True)
            return JsonResponse(
                {
                    "ok": True,
                    "saved_at": timezone.localtime().strftime("%d/%m/%Y %H:%M:%S"),
                    "purchase_number": new_object.purchase_number or "",
                    "inline_objects": self._collect_saved_inline_objects(formsets),
                }
            )

        errors = {"form": form.errors}
        inline_errors = []
        for inline, formset in zip(inline_instances, formsets):
            if formset.non_form_errors() or any(child.errors for child in formset.forms):
                inline_errors.append(
                    {
                        "inline": inline.__class__.__name__,
                        "non_form_errors": list(formset.non_form_errors()),
                        "errors": [child.errors for child in formset.forms if child.errors],
                    }
                )
        errors["inlines"] = inline_errors
        return JsonResponse({"ok": False, "errors": errors}, status=400)

    def _autosave_main_form_fields(self, form, obj):
        update_fields = []
        model_field_names = {field.name for field in obj._meta.fields}

        for field_name, field in form.fields.items():
            if field_name not in model_field_names:
                continue

            try:
                raw_value = field.widget.value_from_datadict(
                    form.data,
                    form.files,
                    form.add_prefix(field_name),
                )
                cleaned_value = field.clean(raw_value)
            except ValidationError:
                continue

            setattr(obj, field_name, cleaned_value)
            update_fields.append(field_name)

        if update_fields:
            update_fields = list(dict.fromkeys(update_fields))
            obj.save(update_fields=update_fields)

        return update_fields

    def _remove_new_inline_forms_from_autosave(self, post_data):
        prefix = "items"
        total_key = f"{prefix}-TOTAL_FORMS"
        initial_key = f"{prefix}-INITIAL_FORMS"
        if total_key not in post_data or initial_key not in post_data:
            return

        try:
            total_forms = int(post_data.get(total_key) or 0)
            initial_forms = int(post_data.get(initial_key) or 0)
        except (TypeError, ValueError):
            return

        if total_forms <= initial_forms:
            return

        kept_new_form_count = 0
        for index in range(initial_forms, total_forms):
            if self._autosave_inline_form_has_data(post_data, prefix, index):
                kept_new_form_count += 1
                continue

            form_prefix = f"{prefix}-{index}-"
            for key in list(post_data.keys()):
                if key.startswith(form_prefix):
                    post_data.pop(key, None)

        if kept_new_form_count == 0:
            post_data[total_key] = str(initial_forms)

    def _autosave_inline_form_has_data(self, post_data, prefix, index):
        if post_data.get(f"{prefix}-{index}-DELETE"):
            return False

        product = (post_data.get(f"{prefix}-{index}-product") or "").strip()
        hs_code = (post_data.get(f"{prefix}-{index}-hs_code") or "").strip()
        part_number = (post_data.get(f"{prefix}-{index}-part_number") or "").strip()
        quantity = self._normalized_autosave_value(post_data.get(f"{prefix}-{index}-quantity"))
        unit_price = self._normalized_autosave_value(post_data.get(f"{prefix}-{index}-unit_price"))

        return bool(
            product or
            (hs_code and hs_code != "-") or
            part_number or
            (quantity and quantity != "1") or
            (unit_price and unit_price not in {"0", "0.0", "0.00"})
        )

    def _normalized_autosave_value(self, value):
        return (value or "").strip().replace(",", "")

    def _collect_saved_inline_objects(self, formsets):
        inline_objects = []
        for formset in formsets:
            for child_form in formset.forms:
                instance = getattr(child_form, "instance", None)
                if not instance or not instance.pk:
                    continue
                cleaned_data = getattr(child_form, "cleaned_data", None) or {}
                if cleaned_data.get("DELETE"):
                    continue
                inline_objects.append(
                    {
                        "prefix": formset.prefix,
                        "form_prefix": child_form.prefix,
                        "id": str(instance.pk),
                    }
                )
        return inline_objects

    def build_partner_context(self, partner):
        if not partner:
            return {
                "name": "-",
                "addresses": [],
                "phones": [],
                "email": "",
                "website": "",
                "fax": "",
            }

        return {
            "name": partner.description,
            "addresses": [address.address for address in partner.addresses.all()],
            "phones": [phone.phone_number for phone in partner.phones.all() if phone.phone_number],
            "email": partner.email,
            "website": partner.website,
            "fax": partner.fax,
        }

    def build_company_partner_context(self, company):
        if not company:
            return self.build_partner_context(None)

        addresses = []
        if company.company_address:
            addresses.append(company.company_address)
        if company.address and company.address not in addresses:
            addresses.append(company.address)

        phones = [company.company_phone] if company.company_phone else []

        return {
            "name": company.company_name or "-",
            "addresses": addresses,
            "phones": phones,
            "email": company.company_email,
            "website": "",
            "fax": company.company_fax,
        }

    def get_purchase_items_for_pdf(self, obj):
        return [
            {
                "index": index,
                "description": item.description or (item.product.description if item.product else "-"),
                "part_number": item.part_number or (item.product.part_number if item.product and item.product.part_number else "-"),
                "hs_code": item.hs_code or (item.product.hs_code if item.product and item.product.hs_code else "-"),
                "quantity": item.quantity,
                "unit_price": item.unit_price,
                "total_amount": item.total_line(),
                "vat_percent": obj.vat_percent,
            }
            for index, item in enumerate(
                obj.items.select_related("product").order_by("pk"),
                start=1,
            )
        ]

    def export_purchase_pdf(self, request, object_id):
        obj = get_object_or_404(
            PurchaseOrder.objects.select_related("seller", "requester").prefetch_related(
                "items__product",
                "seller__addresses",
                "seller__phones",
                "requester__addresses",
                "requester__phones",
            ),
            pk=object_id,
        )
        if not self.has_view_permission(request, obj):
            raise PermissionDenied
        company = CompanySetting.objects.first()
        try:
            pdf_bytes = build_purchase_order_pdf(
                purchase_order=obj,
                company=company,
                items=self.get_purchase_items_for_pdf(obj),
                seller=self.build_partner_context(obj.seller),
                requester={
                    "name": company.company_name if company and company.company_name else "-",
                    "addresses": [],
                    "phones": [],
                    "email": "",
                    "website": "",
                    "fax": "",
                },
                requester_is_explicit=False,
                currency=company.currency if company else "EUR",
            )
        except Exception as exc:
            return HttpResponse(
                f"PDF generation failed: {exc}",
                content_type="text/plain; charset=utf-8",
                status=500,
            )

        response = HttpResponse(pdf_bytes, content_type="application/pdf")
        filename = document_pdf_filename("Purchase-Order", obj.purchase_number)
        response["Content-Disposition"] = f'inline; filename="{filename}"'
        return response

    def get_purchase_report_pdf_url(self, request):
        query_string = request.GET.urlencode()
        base_url = reverse("admin:purchase_report_pdf")
        return f"{base_url}?{query_string}" if query_string else base_url

    def _pdf_link_callback(self, uri, rel):
        parsed = urlparse(uri)
        path = unquote(parsed.path or uri)

        if path.startswith(settings.MEDIA_URL):
            return str(Path(settings.MEDIA_ROOT) / path.removeprefix(settings.MEDIA_URL))

        if path.startswith(settings.STATIC_URL):
            static_roots = []
            static_root = getattr(settings, "STATIC_ROOT", None)
            if static_root:
                static_roots.append(Path(static_root))
            static_roots.extend(Path(root) for root in getattr(settings, "STATICFILES_DIRS", []))

            relative_path = path.removeprefix(settings.STATIC_URL)
            for root in static_roots:
                candidate = root / relative_path
                if candidate.exists():
                    return str(candidate)

        if parsed.scheme == "file":
            return parsed.path

        return uri

    def _build_pdf_with_weasyprint(self, html_string, base_url):
        from weasyprint import HTML

        return HTML(string=html_string, base_url=base_url).write_pdf()

    def _build_pdf_with_xhtml2pdf(self, html_string):
        from xhtml2pdf import pisa

        result = BytesIO()
        pdf = pisa.CreatePDF(
            src=html_string,
            dest=result,
            link_callback=self._pdf_link_callback,
        )
        if pdf.err:
            raise RuntimeError("xhtml2pdf could not render the purchase report.")
        return result.getvalue()

    def _build_report_context(self, request):
        if not can_view_purchase_reports(request.user):
            raise PermissionDenied
        if not self.has_view_permission(request):
            raise PermissionDenied
        company = CompanySetting.objects.first()
        sellers = Partner.objects.filter(partner_type="seller").order_by("description")
        products = Product.objects.all().order_by("description")

        selected_sellers = request.GET.getlist("importers")
        selected_products = request.GET.getlist("products")
        year = request.GET.get("year")
        date_from = request.GET.get("date_from")
        date_to = request.GET.get("date_to")
        company_year = self.get_company_year()

        if (
            not is_staff_role(request.user)
            and not year and not date_from and not date_to and company_year
        ):
            year = str(company_year)

        line_total = ExpressionWrapper(
            F("items__quantity") * F("items__unit_price"),
            output_field=DecimalField(max_digits=14, decimal_places=2),
        )

        queryset = (
            PurchaseOrder.objects.select_related("seller", "requester")
            .prefetch_related("items", "items__product")
            .order_by("purchase_date")
            .annotate(
                qty_total=Coalesce(Sum("items__quantity"), Value(0)),
                gross_value_db=Coalesce(
                    Sum(line_total),
                    Value(Decimal("0.00")),
                    output_field=DecimalField(max_digits=14, decimal_places=2),
                ),
            )
        )

        if not can_view_all_documents(request.user):
            queryset = queryset.filter(created_by=request.user)

        if year:
            queryset = queryset.filter(purchase_date__year=year)

        if date_from:
            queryset = queryset.filter(purchase_date__gte=date_from)

        if date_to:
            queryset = queryset.filter(purchase_date__lte=date_to)

        if selected_sellers:
            queryset = queryset.filter(seller_id__in=selected_sellers)

        if selected_products:
            queryset = queryset.filter(items__product_id__in=selected_products).distinct()

        purchase_orders = list(queryset)

        total_qty = sum((po.qty_total or 0) for po in purchase_orders)
        total_gross = sum((po.gross_value_db or Decimal("0.00")) for po in purchase_orders)
        total_vat = sum((po.vat_amount() or Decimal("0.00")) for po in purchase_orders)
        total_freight = sum((po.freight or Decimal("0.00")) for po in purchase_orders)
        total_amount = sum((po.total_amount() or Decimal("0.00")) for po in purchase_orders)

        chart_labels, chart_totals = self._build_monthly_totals(queryset)

        return dict(
            self.admin_site.each_context(request),
            company=company,
            company_logo_url=request.build_absolute_uri(company.company_logo.url) if company and company.company_logo else "",
            importers=sellers,
            products=products,
            selected_importers=selected_sellers,
            selected_products=selected_products,
            year=year,
            date_from=date_from,
            date_to=date_to,
            purchase_orders=purchase_orders,
            chart_labels=chart_labels,
            chart_totals=chart_totals,
            total_qty=total_qty,
            total_gross=total_gross,
            total_vat=total_vat,
            total_freight=total_freight,
            total_amount=total_amount,
            from_date=date_from,
            to_date=date_to,
            chart_rows=self._build_pdf_chart_rows(chart_labels, chart_totals),
            currency_symbol=format_currency_symbol(company.currency if company else "EUR"),
            chart_svg=self._build_pdf_chart_svg(chart_labels, chart_totals, format_currency_symbol(company.currency if company else "EUR")),
            purchase_report_pdf_url=self.get_purchase_report_pdf_url(request),
        )

    def _build_pdf_chart_rows(self, labels, totals):
        if not totals:
            return []

        palette = [
            "#2f7bb0",
            "#49a078",
            "#d98f38",
            "#8b5fbf",
            "#d45d79",
            "#3d5a80",
            "#e9c46a",
            "#2a9d8f",
        ]
        max_total = max(totals) or 1
        rows = []

        for index, (label, total) in enumerate(zip(labels, totals), start=1):
            height_percent = round((total / max_total) * 100, 2) if total else 0
            rows.append({
                "label": label,
                "total": Decimal(str(total)),
                "height_percent": max(6, height_percent) if total else 0,
                "color": palette[(index - 1) % len(palette)],
            })

        return rows

    def _build_pdf_chart_svg(self, labels, totals, currency):
        if not labels or not totals:
            return ""

        width = 920
        height = 300
        left = 62
        right = 20
        top = 18
        bottom = 52
        chart_width = width - left - right
        chart_height = height - top - bottom
        max_total = max(totals) or 1
        count = len(totals)
        slot_width = chart_width / count if count else chart_width
        bar_width = max(28, slot_width * 0.72)

        y_ticks = 5
        parts = [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
            '<rect width="100%" height="100%" fill="#ffffff"/>',
            f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top + chart_height}" stroke="#bfd0df" stroke-width="1"/>',
            f'<line x1="{left}" y1="{top + chart_height}" x2="{width - right}" y2="{top + chart_height}" stroke="#bfd0df" stroke-width="1"/>',
        ]

        for tick in range(y_ticks + 1):
            ratio = tick / y_ticks
            y = top + chart_height - (chart_height * ratio)
            value = round(max_total * ratio)
            parts.append(
                f'<line x1="{left}" y1="{y:.2f}" x2="{width - right}" y2="{y:.2f}" stroke="#edf2f7" stroke-width="1"/>'
            )
            parts.append(
                f'<text x="{left - 8}" y="{y + 4:.2f}" text-anchor="end" font-family="Arial, sans-serif" font-size="11" fill="#697a8c">{value}</text>'
            )

        for index, (label, total) in enumerate(zip(labels, totals)):
            x = left + (slot_width * index) + ((slot_width - bar_width) / 2)
            bar_height = 0 if max_total == 0 else (total / max_total) * chart_height
            y = top + chart_height - bar_height
            parts.append(
                f'<rect x="{x:.2f}" y="{y:.2f}" width="{bar_width:.2f}" height="{bar_height:.2f}" fill="#e596a0" stroke="#6c7480" stroke-width="1"/>'
            )
            parts.append(
                f'<text x="{x + (bar_width / 2):.2f}" y="{y - 8:.2f}" text-anchor="middle" font-family="Arial, sans-serif" font-size="11" font-weight="700" fill="#274257">{round(total)}</text>'
            )
            parts.append(
                f'<text x="{x + (bar_width / 2):.2f}" y="{top + chart_height + 22:.2f}" text-anchor="middle" font-family="Arial, sans-serif" font-size="11" font-weight="700" fill="#5d7488">{label}</text>'
            )

        parts.extend([
            f'<text x="{width / 2:.2f}" y="{height - 12}" text-anchor="middle" font-family="Arial, sans-serif" font-size="12" font-weight="700" fill="#697a8c">Month</text>',
            f'<text transform="translate(18 {top + (chart_height / 2):.2f}) rotate(-90)" text-anchor="middle" font-family="Arial, sans-serif" font-size="12" font-weight="700" fill="#697a8c">Total Amount ({currency})</text>',
            f'<rect x="{width / 2 - 70:.2f}" y="2" width="18" height="8" fill="#e596a0" stroke="#6c7480" stroke-width="1"/>',
            f'<text x="{width / 2 - 46:.2f}" y="10" font-family="Arial, sans-serif" font-size="11" font-weight="700" fill="#697a8c">Total Amount</text>',
            '</svg>',
        ])
        return "".join(parts)

    def purchase_report(self, request):
        context = self._build_report_context(request)
        return render(request, "admin/purchase/report.html", context)

    def purchase_report_pdf(self, request):
        context = self._build_report_context(request)
        try:
            pdf_bytes = build_purchase_report_pdf(
                company=context.get("company"),
                currency=context.get("company").currency if context.get("company") else "EUR",
                purchase_orders=context.get("purchase_orders", []),
                chart_labels=context.get("chart_labels", []),
                chart_totals=context.get("chart_totals", []),
                total_qty=context.get("total_qty", 0),
                total_gross=context.get("total_gross", Decimal("0.00")),
                total_vat=context.get("total_vat", Decimal("0.00")),
                total_freight=context.get("total_freight", Decimal("0.00")),
                total_amount=context.get("total_amount", Decimal("0.00")),
                from_date=context.get("from_date"),
                to_date=context.get("to_date"),
            )
        except Exception as exc:
            return HttpResponse(
                f"PDF generation failed: {exc}",
                content_type="text/plain; charset=utf-8",
                status=500,
            )

        response = HttpResponse(pdf_bytes, content_type="application/pdf")
        response["Content-Disposition"] = 'inline; filename="purchase-orders-report.pdf"'
        return response

    def _build_monthly_totals(self, queryset):
        monthly_data = (
            queryset.annotate(month=TruncMonth("purchase_date"))
            .values("month")
            .annotate(
                gross_total=Coalesce(
                    Sum(
                        ExpressionWrapper(
                            F("items__quantity") * F("items__unit_price"),
                            output_field=DecimalField(max_digits=14, decimal_places=2),
                        )
                    ),
                    Value(Decimal("0.00")),
                    output_field=DecimalField(max_digits=14, decimal_places=2),
                ),
                vat_total=Coalesce(
                    Sum(
                        ExpressionWrapper(
                            F("items__quantity") * F("items__unit_price") * F("vat_percent") / Value(100),
                            output_field=DecimalField(max_digits=14, decimal_places=2),
                        )
                    ),
                    Value(Decimal("0.00")),
                    output_field=DecimalField(max_digits=14, decimal_places=2),
                ),
                freight_total=Coalesce(
                    Sum("freight"),
                    Value(Decimal("0.00")),
                    output_field=DecimalField(max_digits=14, decimal_places=2),
                ),
            )
            .order_by("month")
        )

        labels = []
        totals = []

        for row in monthly_data:
            month = row["month"]
            if not month:
                continue

            total_amount = (row["gross_total"] or Decimal("0.00")) + (row["vat_total"] or Decimal("0.00")) + (row["freight_total"] or Decimal("0.00"))
            labels.append(month.strftime("%Y-%m"))
            totals.append(float(total_amount))

        return labels, totals
