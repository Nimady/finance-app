from decimal import Decimal

from django import forms
from django.contrib import admin
from django.contrib.admin.models import ADDITION, LogEntry
from django.core.exceptions import PermissionDenied, ValidationError
from django.forms.formsets import all_valid
from django.db.models import Count, DecimalField, ExpressionWrapper, F, Sum, Value
from django.db.models.functions import Coalesce, TruncMonth
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path
from django.urls import reverse
from django.urls import NoReverseMatch
from django.utils.html import format_html
from django.utils import timezone
from financeapp.admin_mixins import PageSizeAdminMixin, SaveRedirectToWelcomeMixin
from financeapp.access_control import (
    can_view_all_documents,
    can_view_commercial_reports,
    is_owned_by_user,
    is_staff_role,
)
from financeapp.filename_utils import document_pdf_filename
from company.models import CompanySetting
from partners.models import Partner
from products.models import Product
from .forms import CommercialInvoiceForm, ProformaInvoiceForm
from .pdf_builder import build_commercial_report_pdf, build_invoice_pdf, format_footer_invoice_lines
from .models import (
    ProformaInvoice,
    CommercialInvoice,
    ProformaInvoiceItem,
    CommercialInvoiceItem,
    CommercialInvoicePacking,
)


# ----------------------
# PROFORMA ITEMS
# ----------------------

class ProformaItemInline(admin.TabularInline):

    model = ProformaInvoiceItem
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

    def product_description(self, obj):
        value = obj.product.description if obj.product else "-"
        return format_html('<span data-product-field="description">{}</span>', value)

    product_description.short_description = "Description"

    def part_number(self, obj):
        value = obj.part_number or (obj.product.part_number if obj.product and obj.product.part_number else "-")
        return format_html('<span data-product-field="part_number">{}</span>', value)

    part_number.short_description = "Part Number"

    def stock_info(self, obj):
        value = obj.product.unit_qty if obj.product else "-"
        return format_html('<span data-product-field="stock">{}</span>', value)

    stock_info.short_description = "Stock"

    def sale_price_info(self, obj):
        value = obj.product.sale_price if obj.product else "-"
        return format_html('<span data-product-field="sale_price">{}</span>', value)

    sale_price_info.short_description = "Sale Price"

    def purchase_price_info(self, obj):
        value = obj.product.purchase_price if obj.product else "-"
        return format_html('<span data-product-field="purchase_price">{}</span>', value)

    purchase_price_info.short_description = "Purchase Price"

    def product_note(self, obj):
        value = obj.product.note if obj.product and obj.product.note else "-"
        return format_html('<span data-product-field="note">{}</span>', value)

    product_note.short_description = "Note"

    def total_line(self, obj):
        return obj.total_line()

    total_line.short_description = "Total Amount"


class InvoiceAdminForm(forms.ModelForm):
    class Meta:
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        company = CompanySetting.objects.first()

        if company and not self.instance.pk:
            if "vat_percent" in self.fields:
                self.fields["vat_percent"].initial = company.vat_amount
            if "invoice_date" in self.fields:
                self.fields["invoice_date"].initial = self._get_default_company_date(company.year)
            if "delivery_time" in self.fields:
                self.fields["delivery_time"].initial = company.delivery_time
            if "terms_conditions" in self.fields:
                self.fields["terms_conditions"].initial = company.terms_conditions

    def _get_default_company_date(self, year):
        today = timezone.now().date()
        try:
            return today.replace(year=year)
        except ValueError:
            return today.replace(year=year, day=28)


class InvoiceAdminMixin:
    change_form_template = "admin/invoices/change_form.html"
    form = InvoiceAdminForm

    readonly_fields = (
        "subtotal_display",
        "vat_amount_display",
        "total_amount_display",
    )

    class Media:
        css = {
            "all": ("admin/css/large_partner_autocomplete.css",),
        }
        js = (
            "admin/js/raw_id_label_display.js",
        )

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        formfield = super().formfield_for_foreignkey(db_field, request, **kwargs)
        if db_field.name in {"importer", "end_user"}:
            existing_classes = formfield.widget.attrs.get("class", "")
            formfield.widget.attrs["class"] = (
                f"{existing_classes} partner-large-select"
            ).strip()
        return formfield

    def get_company_year(self):
        company = CompanySetting.objects.first()
        return company.year if company and company.year else None

    def get_default_invoice_date(self):
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

    def create_draft_invoice(self, request=None):
        company = CompanySetting.objects.first()
        values = dict(
            invoice_date=self.get_default_invoice_date(),
            vat_percent=self.get_default_vat_percent(),
            delivery_time=company.delivery_time if company else "",
            terms_conditions=company.terms_conditions if company else "",
        )
        if request and any(field.name == "created_by" for field in self.model._meta.fields):
            values["created_by"] = request.user
        return self.model.objects.create(**values)

    def save_model(self, request, obj, form, change):
        if not change and hasattr(obj, "created_by_id") and not obj.created_by_id:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)

    def has_explicit_year_filter(self, request, field_name):
        return any(key.startswith(field_name) for key in request.GET.keys())

    def should_apply_default_year_filter(self, request):
        match = getattr(request, "resolver_match", None)
        return bool(match and match.url_name and match.url_name.endswith("_changelist"))

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        company_year = self.get_company_year()
        if (
            company_year and
            not is_staff_role(request.user) and
            self.should_apply_default_year_filter(request) and
            not self.has_explicit_year_filter(request, "invoice_date")
        ):
            queryset = queryset.filter(invoice_date__year=company_year)
        return queryset

    def _summary_value(self, key, value):
        return format_html(
            '<span data-invoice-summary="{key}">{value}</span>',
            key=key,
            value=f"{value:.2f}",
        )

    def subtotal_display(self, obj):
        value = obj.subtotal() if obj.pk else 0
        return self._summary_value("subtotal", value)

    subtotal_display.short_description = "Subtotal"

    def vat_amount_display(self, obj):
        value = obj.vat_amount() if obj.pk else 0
        return self._summary_value("vat", value)

    vat_amount_display.short_description = "VAT Amount"

    def total_amount_display(self, obj):
        value = obj.total_amount() if obj.pk else 0
        return self._summary_value("total", value)

    total_amount_display.short_description = "Total Amount"

    def pdf_link(self, obj):
        if not obj.pk:
            return "-"

        return format_html('<a class="invoice-pdf-button" href="{}" target="_blank">PDF</a>', self.get_invoice_pdf_url(obj))

    pdf_link.short_description = "PDF"

    def render_change_form(self, request, context, *args, **kwargs):
        original = context.get("original")
        context["invoice_pdf_url"] = self.get_invoice_pdf_url(original) if original and original.pk else ""
        context["invoice_pdf_links"] = self.get_invoice_pdf_links(original) if original and original.pk else []
        context["invoice_autosave_url"] = self.get_invoice_autosave_url(original) if original and original.pk else ""
        return super().render_change_form(request, context, *args, **kwargs)

    def changelist_view(self, request, extra_context=None):
        if (request.GET.get("q") or "").strip():
            query = request.GET.copy()
            removed_year_filter = False
            for key in list(query.keys()):
                if key.startswith("invoice_date__year"):
                    query.pop(key, None)
                    removed_year_filter = True
            if removed_year_filter:
                return redirect(f"{request.path}?{query.urlencode()}")

        extra_context = extra_context or {}
        extra_context["can_view_reports"] = can_view_commercial_reports(request.user)
        try:
            extra_context["draft_add_url"] = self.get_invoice_draft_add_url()
        except NoReverseMatch:
            extra_context["draft_add_url"] = ""
        return super().changelist_view(request, extra_context=extra_context)

    def response_change(self, request, obj):
        if "_save_and_pdf" in request.POST and "_save" not in request.POST:
            return redirect(request.POST.get("_save_and_pdf_url") or self.get_invoice_pdf_url(obj))
        return super().response_change(request, obj)

    def add_view(self, request, form_url="", extra_context=None):
        if request.method == "GET" and not request.GET.get("_popup"):
            if not self.has_add_permission(request):
                raise PermissionDenied
            draft = self.create_draft_invoice(request)
            return redirect(reverse(
                f"admin:{self.model._meta.app_label}_{self.model._meta.model_name}_change",
                args=[draft.pk],
            ))
        return super().add_view(request, form_url, extra_context)

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

    def get_invoice_items_for_pdf(self, obj):
        return [
            {
                "index": index,
                "description": item.product.description if item.product else "-",
                "item_date": (
                    getattr(item, "item_date", None) or getattr(obj, "invoice_date", None)
                ).strftime("%d/%m/%Y") if (getattr(item, "item_date", None) or getattr(obj, "invoice_date", None)) else "-",
                "part_number": item.part_number or (item.product.part_number if item.product and item.product.part_number else "-"),
                "hs_code": item.hs_code or "-",
                "quantity": item.quantity,
                "unit_price": item.unit_price,
                "total_amount": item.total_line(),
            }
            for index, item in enumerate(
                obj.items.select_related("product").order_by("pk"),
                start=1,
            )
        ]

    def get_invoice_hs_code(self, obj):
        codes = sorted({item.hs_code for item in obj.items.all() if item.hs_code and item.hs_code != "-"})
        return ", ".join(codes) if codes else "-"

    def get_invoice_title(self):
        return self.model._meta.verbose_name.replace("_", " ").title()

    def get_invoice_pdf_filename(self, obj):
        document_type = (
            "Proforma-Invoice"
            if self.model is ProformaInvoice
            else "Commercial-Invoice"
        )
        return document_pdf_filename(document_type, obj.invoice_number)

    def get_search_results(self, request, queryset, search_term):
        normalized_term = (search_term or "").strip()
        if normalized_term:
            number_variants = {
                normalized_term,
                normalized_term.replace("/", "-"),
                normalized_term.replace("-", "/"),
            }
            search_queryset = (
                queryset
                if request and not can_view_all_documents(request.user)
                else self.model._default_manager.all()
            )
            exact_numbers = search_queryset.filter(
                invoice_number__in=number_variants
            ).order_by("-invoice_date", "-id")
            if exact_numbers.exists():
                return exact_numbers, False

        return super().get_search_results(request, queryset, search_term)

    def get_invoice_pdf_url(self, obj):
        return reverse(f"admin:{self.model._meta.app_label}_{self.model._meta.model_name}_pdf", args=[obj.pk])

    def get_invoice_pdf_links(self, obj):
        return [
            {
                "label": "Create PDF",
                "url": self.get_invoice_pdf_url(obj),
            }
        ]

    def get_invoice_autosave_url(self, obj):
        return reverse(f"admin:{self.model._meta.app_label}_{self.model._meta.model_name}_autosave", args=[obj.pk])

    def get_invoice_draft_add_url(self):
        return reverse(
            f"admin:{self.model._meta.app_label}_{self.model._meta.model_name}_draft_add"
        )

    def create_draft_view(self, request):
        if request.method != "GET":
            return JsonResponse({"ok": False, "error": "GET required."}, status=405)

        if not self.has_add_permission(request):
            raise PermissionDenied
        draft = self.create_draft_invoice(request)
        return redirect(
            reverse(
                f"admin:{self.model._meta.app_label}_{self.model._meta.model_name}_change",
                args=[draft.pk],
            )
        )

    def get_invoice_pdf_context(self, request, obj):
        company = CompanySetting.objects.first()
        return {
            "invoice": obj,
            "invoice_title": self.get_invoice_title(),
            "is_proforma": isinstance(obj, ProformaInvoice),
            "company": company,
            "company_logo_url": request.build_absolute_uri(company.company_logo.url) if company and company.company_logo else "",
            "importer": self.build_partner_context(obj.importer),
            "end_user": self.build_partner_context(obj.end_user),
            "items": self.get_invoice_items_for_pdf(obj),
            "hs_code": self.get_invoice_hs_code(obj),
            "currency": company.currency if company else "EUR",
            "subtotal": obj.subtotal(),
            "vat_amount": obj.vat_amount(),
            "total_amount": obj.total_amount(),
            "packing_entries": self.get_invoice_packing_entries_for_pdf(obj),
        }

    def get_invoice_packing_entries_for_pdf(self, obj):
        if not hasattr(obj, "packing_entries"):
            return []

        return [
            {
                "index": index,
                "no_packing": entry.no_packing or "-",
                "gross_weight": entry.gross_weight,
                "net_weight": entry.net_weight,
                "dimension_length": entry.dimension_length,
                "dimension_width": entry.dimension_width,
                "dimension_height": entry.dimension_height,
            }
            for index, entry in enumerate(obj.packing_entries.all(), start=1)
        ]

    def get_pdf_document_title(self, obj, document_type="default"):
        return self.get_invoice_title()

    def get_pdf_filename(self, obj, document_type="default"):
        return self.get_invoice_pdf_filename(obj)

    def export_pdf(self, request, object_id, document_type="default"):
        queryset = self.model.objects.select_related("importer", "end_user").prefetch_related(
            "items__product",
            "importer__addresses",
            "importer__phones",
            "end_user__addresses",
            "end_user__phones",
        )
        if hasattr(self.model, "packing_entries"):
            queryset = queryset.prefetch_related("packing_entries")

        obj = get_object_or_404(queryset, pk=object_id)
        if not self.has_view_permission(request, obj):
            raise PermissionDenied

        context = self.get_invoice_pdf_context(request, obj)
        context["document_type"] = document_type
        context["invoice_title"] = self.get_pdf_document_title(obj, document_type=document_type)
        try:
            pdf_bytes = build_invoice_pdf(**context)
        except Exception as exc:
            return HttpResponse(
                f"PDF generation failed: {exc}",
                content_type="text/plain; charset=utf-8",
                status=500,
            )

        response = HttpResponse(pdf_bytes, content_type="application/pdf")
        response["Content-Disposition"] = f'inline; filename="{self.get_pdf_filename(obj, document_type=document_type)}"'
        response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response["Pragma"] = "no-cache"
        response["Expires"] = "0"
        return response

    def autosave(self, request, object_id):
        if request.method != "POST":
            return JsonResponse({"ok": False, "error": "POST required."}, status=405)

        obj = get_object_or_404(self.model, pk=object_id)
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
                    "invoice_number": new_object.invoice_number or "",
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
        prefixes = [
            key[:-len("-TOTAL_FORMS")]
            for key in post_data.keys()
            if key.endswith("-TOTAL_FORMS")
        ]

        for prefix in prefixes:
            total_key = f"{prefix}-TOTAL_FORMS"
            initial_key = f"{prefix}-INITIAL_FORMS"
            if initial_key not in post_data:
                continue

            try:
                total_forms = int(post_data.get(total_key) or 0)
                initial_forms = int(post_data.get(initial_key) or 0)
            except (TypeError, ValueError):
                continue

            if total_forms <= initial_forms:
                continue

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

        if prefix == "items":
            return self._autosave_product_inline_form_has_data(post_data, prefix, index)

        meaningful_fields = (
            "no_packing",
            "gross_weight",
            "net_weight",
            "dimension_length",
            "dimension_width",
            "dimension_height",
        )
        for field_name in meaningful_fields:
            value = self._normalized_autosave_value(post_data.get(f"{prefix}-{index}-{field_name}"))
            if value and value not in {"0", "0.0", "0.00"}:
                return True
        return False

    def _normalized_autosave_value(self, value):
        return (value or "").strip().replace(",", "")

    def _autosave_product_inline_form_has_data(self, post_data, prefix, index):
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


# ----------------------
# PROFORMA ADMIN
# ----------------------

@admin.register(ProformaInvoice)
class ProformaInvoiceAdmin(InvoiceAdminMixin, SaveRedirectToWelcomeMixin, PageSizeAdminMixin, admin.ModelAdmin):
    changelist_template = "admin/invoices/change_list.html"
    form = ProformaInvoiceForm
    readonly_fields = InvoiceAdminMixin.readonly_fields + ("created_by_display",)

    fieldsets = (
        ("Invoice Overview", {
            "fields": (
                ("invoice_date", "invoice_number"),
                "importer",
                "end_user",
                "our_reference",
                "price_for",
                "created_by_display",
                ("delivery_time", "terms_conditions"),
            )
        }),
        ("Financial Summary", {
            "fields": (
                ("freight", "discount", "vat_percent"),
                ("subtotal_display", "vat_amount_display"),
                "total_amount_display",
            )
        }),
    )

    inlines = [ProformaItemInline]

    actions = ["convert_to_commercial"]

    list_display = (
        "id",
        "proforma_number",
        "invoice_date_display",
        "importer",
        "end_user",
        "created_by_display",
        "amount_display",
        "pdf_link",
    )

    autocomplete_fields = (
        "importer",
        "end_user",
    )

    search_fields = (
        "invoice_number",
        "our_reference",
        "importer__description",
        "importer__email",
        "end_user__description",
        "end_user__email",
        "items__product__description",
        "items__product__part_number",
        "items__part_number",
        "items__hs_code",
    )

    list_filter = (
        "invoice_date",
        "importer",
    )

    def create_draft_invoice(self, request=None):
        draft = super().create_draft_invoice(request)
        if request:
            LogEntry.objects.log_actions(
                user_id=request.user.pk,
                queryset=[draft],
                action_flag=ADDITION,
                change_message="Proforma invoice draft created.",
                single_object=True,
            )
        return draft

    def created_by_display(self, obj):
        if not obj or not obj.pk:
            return "-"
        creation = (
            LogEntry.objects.filter(
                content_type__app_label=obj._meta.app_label,
                content_type__model=obj._meta.model_name,
                object_id=str(obj.pk),
                action_flag=ADDITION,
            )
            .select_related("user")
            .order_by("action_time", "pk")
            .first()
        )
        return creation.user if creation else "-"

    created_by_display.short_description = "Created by"

    def _is_created_by(self, obj, user):
        return bool(obj and obj.pk) and LogEntry.objects.filter(
            content_type__app_label=obj._meta.app_label,
            content_type__model=obj._meta.model_name,
            object_id=str(obj.pk),
            action_flag=ADDITION,
            user=user,
        ).exists()

    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        if object_id and not can_view_all_documents(request.user):
            obj = self.model._default_manager.filter(pk=object_id).first()
            if obj and not self._is_created_by(obj, request.user):
                raise PermissionDenied
        return super().changeform_view(request, object_id, form_url, extra_context)

    def has_view_permission(self, request, obj=None):
        allowed = super().has_view_permission(request, obj)
        if not allowed or can_view_all_documents(request.user) or obj is None:
            return allowed
        return self._is_created_by(obj, request.user)

    def has_change_permission(self, request, obj=None):
        allowed = super().has_change_permission(request, obj)
        if not allowed or can_view_all_documents(request.user) or obj is None:
            return allowed
        return self._is_created_by(obj, request.user)

    def has_delete_permission(self, request, obj=None):
        if is_staff_role(request.user):
            return False
        return super().has_delete_permission(request, obj)

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        if not can_view_all_documents(request.user):
            object_ids = LogEntry.objects.filter(
                content_type__app_label=self.model._meta.app_label,
                content_type__model=self.model._meta.model_name,
                action_flag=ADDITION,
                user=request.user,
            ).values_list("object_id", flat=True)
            queryset = queryset.filter(pk__in=list(object_ids))
        queryset = queryset.annotate(items_count=Count("items", distinct=True))
        if not request.GET.get("o"):
            queryset = queryset.order_by("-invoice_date", "-id")
        return queryset

    # ----------------------
    # DATE DISPLAY
    # ----------------------

    def invoice_date_display(self, obj):

        if obj.invoice_date:
            return obj.invoice_date.strftime("%d/%m/%Y")

        return "-"

    invoice_date_display.short_description = "Date"
    invoice_date_display.admin_order_field = "invoice_date"

    # ----------------------
    # HS CODE DISPLAY
    # ----------------------

    def hs_code_display(self, obj):
        codes = sorted({item.hs_code for item in obj.items.all() if item.hs_code and item.hs_code != "-"})
        return ", ".join(codes) if codes else "Not specified"

    hs_code_display.short_description = "HS Code"

    # ----------------------
    # NUMBER
    # ----------------------

    def proforma_number(self, obj):

        return obj.invoice_number

    proforma_number.short_description = "Proforma Number"
    proforma_number.admin_order_field = "invoice_number"

    # ----------------------
    # AMOUNT
    # ----------------------

    def amount_display(self, obj):

        return obj.total_amount()

    amount_display.short_description = "Amount"

    # ----------------------
    # ACTION
    # ----------------------

    def convert_to_commercial(self, request, queryset):

        for proforma in queryset:

            if hasattr(proforma, "convert_to_commercial"):
                commercial = proforma.convert_to_commercial(user_initiated=True)
                if commercial and not commercial.created_by_id:
                    commercial.created_by = request.user
                    commercial.save(update_fields=["created_by"])

    convert_to_commercial.short_description = "Convert to Commercial Invoice"

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "create-draft/",
                self.admin_site.admin_view(self.create_draft_view),
                name="invoices_proformainvoice_draft_add",
            ),
            path(
                "<int:object_id>/autosave/",
                self.admin_site.admin_view(self.autosave),
                name="invoices_proformainvoice_autosave",
            ),
            path(
                "<int:object_id>/pdf/",
                self.admin_site.admin_view(self.export_pdf),
                name="invoices_proformainvoice_pdf",
            ),
        ]
        return custom_urls + urls


# ----------------------
# COMMERCIAL ITEMS
# ----------------------

class CommercialItemInline(admin.TabularInline):

    model = CommercialInvoiceItem
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

    def product_description(self, obj):
        value = obj.product.description if obj.product else "-"
        return format_html('<span data-product-field="description">{}</span>', value)

    product_description.short_description = "Description"

    def part_number(self, obj):
        value = obj.part_number or (obj.product.part_number if obj.product and obj.product.part_number else "-")
        return format_html('<span data-product-field="part_number">{}</span>', value)

    part_number.short_description = "Part Number"

    def stock_info(self, obj):
        value = obj.product.unit_qty if obj.product else "-"
        return format_html('<span data-product-field="stock">{}</span>', value)

    stock_info.short_description = "Stock"

    def sale_price_info(self, obj):
        value = obj.product.sale_price if obj.product else "-"
        return format_html('<span data-product-field="sale_price">{}</span>', value)

    sale_price_info.short_description = "Sale Price"

    def purchase_price_info(self, obj):
        value = obj.product.purchase_price if obj.product else "-"
        return format_html('<span data-product-field="purchase_price">{}</span>', value)

    purchase_price_info.short_description = "Purchase Price"

    def product_note(self, obj):
        value = obj.product.note if obj.product and obj.product.note else "-"
        return format_html('<span data-product-field="note">{}</span>', value)

    product_note.short_description = "Note"

    def total_line(self, obj):
        return obj.total_line()

    total_line.short_description = "Total Amount"


class CommercialPackingInline(admin.TabularInline):

    model = CommercialInvoicePacking
    extra = 1

    fields = (
        "no_packing",
        "gross_weight",
        "net_weight",
        "dimension_length",
        "dimension_width",
        "dimension_height",
    )


# ----------------------
# COMMERCIAL ADMIN
# ----------------------

@admin.register(CommercialInvoice)
class CommercialInvoiceAdmin(InvoiceAdminMixin, SaveRedirectToWelcomeMixin, PageSizeAdminMixin, admin.ModelAdmin):
    changelist_template = "admin/invoices/change_list.html"
    form = CommercialInvoiceForm
    readonly_fields = InvoiceAdminMixin.readonly_fields + ("created_by",)

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
    commercial_document_titles = {
        "default": "Commercial Invoice",
        "packing_list": "Packing List",
        "dispatching_note": "Dispatching Note",
    }

    fieldsets = (
        ("Invoice Overview", {
            "fields": (
                ("invoice_date", "invoice_number"),
                "importer",
                "end_user",
                ("our_order_no", "our_reference"),
                "price_for",
                "dispatching_note",
                "packing_specification",
                "created_by",
                ("delivery_time", "terms_conditions"),
            )
        }),
        ("Financial Summary", {
            "fields": (
                ("freight", "discount", "vat_percent"),
                ("subtotal_display", "vat_amount_display"),
                "total_amount_display",
            )
        }),
    )

    inlines = [CommercialItemInline, CommercialPackingInline]

    list_display = (
        "id",
        "commercial_number",
        "invoice_date_display",
        "importer",
        "end_user",
        "created_by",
        "amount_display",
        "pdf_link",
    )

    autocomplete_fields = (
        "importer",
        "end_user",
    )

    search_fields = (
        "invoice_number",
        "our_order_no",
        "our_reference",
        "price_for",
        "dispatching_note",
        "packing_specification",
        "importer__description",
        "importer__email",
        "end_user__description",
        "end_user__email",
        "items__product__description",
        "items__product__part_number",
        "items__part_number",
        "items__hs_code",
        "packing_entries__no_packing",
    )

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

    list_filter = (
        "invoice_date",
        "importer",
    )

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        if not can_view_all_documents(request.user):
            queryset = queryset.filter(created_by=request.user)
        queryset = queryset.annotate(items_count=Count("items", distinct=True))
        if not request.GET.get("o"):
            queryset = queryset.order_by("-invoice_date", "-id")
        return queryset

    # ----------------------
    # DATE DISPLAY
    # ----------------------

    def invoice_date_display(self, obj):

        if obj.invoice_date:
            return obj.invoice_date.strftime("%d/%m/%Y")

        return "-"

    invoice_date_display.short_description = "Date"
    invoice_date_display.admin_order_field = "invoice_date"

    # ----------------------
    # HS CODE DISPLAY
    # ----------------------

    def hs_code_display(self, obj):
        codes = sorted({item.hs_code for item in obj.items.all() if item.hs_code and item.hs_code != "-"})
        return ", ".join(codes) if codes else "Not specified"

    hs_code_display.short_description = "HS Code"

    def get_invoice_hs_code(self, obj):
        codes = sorted({item.hs_code for item in obj.items.all() if item.hs_code and item.hs_code != "-"})
        return ", ".join(codes) if codes else "-"

    # ----------------------
    # NUMBER
    # ----------------------

    def commercial_number(self, obj):

        return obj.invoice_number

    commercial_number.short_description = "Commercial Number"
    commercial_number.admin_order_field = "invoice_number"

    # ----------------------
    # AMOUNT
    # ----------------------

    def amount_display(self, obj):

        return obj.total_amount()

    amount_display.short_description = "Amount"

    def get_invoice_pdf_links(self, obj):
        return [
            {
                "label": "Commercial Invoice PDF",
                "url": self.get_invoice_pdf_url(obj),
            },
            {
                "label": "Packing List PDF",
                "url": reverse("admin:invoices_commercialinvoice_packing_list_pdf", args=[obj.pk]),
            },
            {
                "label": "Dispatching Note PDF",
                "url": reverse("admin:invoices_commercialinvoice_dispatching_note_pdf", args=[obj.pk]),
            },
        ]

    def get_pdf_document_title(self, obj, document_type="default"):
        return self.commercial_document_titles.get(document_type, self.commercial_document_titles["default"])

    def get_pdf_filename(self, obj, document_type="default"):
        document_name_map = {
            "default": "Commercial-Invoice",
            "packing_list": "Packing-List",
            "dispatching_note": "Dispatching-Note",
        }
        document_name = document_name_map.get(document_type, document_name_map["default"])
        return document_pdf_filename(document_name, obj.invoice_number)

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "create-draft/",
                self.admin_site.admin_view(self.create_draft_view),
                name="invoices_commercialinvoice_draft_add",
            ),
            path(
                "<int:object_id>/autosave/",
                self.admin_site.admin_view(self.autosave),
                name="invoices_commercialinvoice_autosave",
            ),
            path(
                "<int:object_id>/pdf/",
                self.admin_site.admin_view(self.export_pdf),
                name="invoices_commercialinvoice_pdf",
            ),
            path(
                "<int:object_id>/packing-list-pdf/",
                self.admin_site.admin_view(self.export_packing_list_pdf),
                name="invoices_commercialinvoice_packing_list_pdf",
            ),
            path(
                "<int:object_id>/dispatching-note-pdf/",
                self.admin_site.admin_view(self.export_dispatching_note_pdf),
                name="invoices_commercialinvoice_dispatching_note_pdf",
            ),
            path(
                "report/",
                self.admin_site.admin_view(self.commercial_report),
                name="commercial_invoice_report",
            ),
        ]
        return custom_urls + urls

    def export_packing_list_pdf(self, request, object_id):
        return self.export_pdf(request, object_id, document_type="packing_list")

    def export_dispatching_note_pdf(self, request, object_id):
        return self.export_pdf(request, object_id, document_type="dispatching_note")

    def commercial_report(self, request):
        if not can_view_commercial_reports(request.user):
            raise PermissionDenied
        if not self.has_view_permission(request):
            raise PermissionDenied
        importers = Partner.objects.filter(partner_type="importer").order_by("description")
        products = Product.objects.all().order_by("description")
        company = CompanySetting.objects.first()

        selected_importers = request.GET.getlist("importers")
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
        vat_total_expr = ExpressionWrapper(
            F("items__quantity") * F("items__unit_price") * F("vat_percent") / Value(100),
            output_field=DecimalField(max_digits=14, decimal_places=2),
        )

        queryset = (
            CommercialInvoice.objects.select_related("importer", "end_user")
            .prefetch_related("items", "items__product")
            .order_by("invoice_date")
            .annotate(
                qty_total=Coalesce(Sum("items__quantity"), Value(0)),
                subtotal_db=Coalesce(
                    Sum(line_total),
                    Value(Decimal("0.00")),
                    output_field=DecimalField(max_digits=14, decimal_places=2),
                ),
                vat_amount_db=Coalesce(
                    Sum(vat_total_expr),
                    Value(Decimal("0.00")),
                    output_field=DecimalField(max_digits=14, decimal_places=2),
                ),
            )
        )

        if not can_view_all_documents(request.user):
            queryset = queryset.filter(created_by=request.user)

        if year:
            queryset = queryset.filter(invoice_date__year=year)

        if date_from:
            queryset = queryset.filter(invoice_date__gte=date_from)

        if date_to:
            queryset = queryset.filter(invoice_date__lte=date_to)

        if selected_importers:
            queryset = queryset.filter(importer_id__in=selected_importers)

        if selected_products:
            queryset = queryset.filter(items__product_id__in=selected_products).distinct()

        invoices = list(queryset)

        total_qty = sum((invoice.qty_total or 0) for invoice in invoices)
        total_subtotal = sum((invoice.subtotal_db or Decimal("0.00")) for invoice in invoices)
        total_vat = sum((invoice.vat_amount() or Decimal("0.00")) for invoice in invoices)
        total_freight = sum((invoice.freight or Decimal("0.00")) for invoice in invoices)
        total_discount = sum((invoice.discount or Decimal("0.00")) for invoice in invoices)
        total_amount = sum((invoice.total_amount() or Decimal("0.00")) for invoice in invoices)

        chart_labels, chart_totals = self._build_monthly_totals(queryset)

        if request.GET.get("export") == "pdf":
            try:
                pdf_bytes = build_commercial_report_pdf(
                    company=company,
                    currency=company.currency if company else "EUR",
                    invoices=invoices,
                    chart_labels=chart_labels,
                    chart_totals=chart_totals,
                    total_qty=total_qty,
                    total_subtotal=total_subtotal,
                    total_vat=total_vat,
                    total_freight=total_freight,
                    total_discount=total_discount,
                    total_amount=total_amount,
                    from_date=date_from,
                    to_date=date_to,
                )
            except Exception as exc:
                return HttpResponse(
                    f"PDF generation failed: {exc}",
                    content_type="text/plain; charset=utf-8",
                    status=500,
                )

            response = HttpResponse(pdf_bytes, content_type="application/pdf")
            response["Content-Disposition"] = 'inline; filename="commercial-invoices-report.pdf"'
            response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response["Pragma"] = "no-cache"
            response["Expires"] = "0"
            return response

        export_pdf_query = request.GET.copy()
        export_pdf_query["export"] = "pdf"

        context = dict(
            self.admin_site.each_context(request),
            company=company,
            report_footer_left=self._build_report_footer_left(company),
            report_footer_center=self._build_report_footer_center(company),
            report_footer_right=self._build_report_footer_right(company),
            importers=importers,
            products=products,
            selected_importers=selected_importers,
            selected_products=selected_products,
            year=year,
            date_from=date_from,
            date_to=date_to,
            invoices=invoices,
            chart_labels=chart_labels,
            chart_totals=chart_totals,
            export_pdf_url=f"{request.path}?{export_pdf_query.urlencode()}",
            total_qty=total_qty,
            total_subtotal=total_subtotal,
            total_vat=total_vat,
            total_freight=total_freight,
            total_discount=total_discount,
            total_amount=total_amount,
            from_date=date_from,
            to_date=date_to,
        )

        return render(request, "admin/invoices/commercial_report.html", context)

    def _build_report_footer_left(self, company):
        if not company:
            return []
        return [
            f"Bank: {company.bank}" if getattr(company, "bank", "") else "",
            f"IBAN: {company.iban}" if getattr(company, "iban", "") else "",
            f"BIC: {company.bic}" if getattr(company, "bic", "") else "",
        ]

    def _build_report_footer_center(self, company):
        if not company or not getattr(company, "footer_invoice", ""):
            return []
        return format_footer_invoice_lines(company.footer_invoice)

    def _build_report_footer_right(self, company):
        if not company:
            return []
        return [
            f"Telephone: {company.company_phone}" if getattr(company, "company_phone", "") else "",
            f"Fax: {company.company_fax}" if getattr(company, "company_fax", "") else "",
            f"Email: {company.company_email}" if getattr(company, "company_email", "") else "",
        ]

    def _build_monthly_totals(self, queryset):
        monthly_data = (
            queryset.annotate(month=TruncMonth("invoice_date"))
            .values("month")
            .annotate(
                subtotal_total=Coalesce(
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
                discount_total=Coalesce(
                    Sum("discount"),
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

            total_amount = (
                (row["subtotal_total"] or Decimal("0.00"))
                + (row["vat_total"] or Decimal("0.00"))
                + (row["freight_total"] or Decimal("0.00"))
                - (row["discount_total"] or Decimal("0.00"))
            )
            labels.append(month.strftime("%Y-%m"))
            totals.append(float(total_amount))

        return labels, totals
