import re
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.contrib.staticfiles import finders
from django.db import connection
from django.template.loader import get_template
from django.test import SimpleTestCase, TestCase
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from reportlab.lib.units import mm
from reportlab.platypus import Spacer

from company.models import CompanySetting
from partners.models import Partner
from products.models import Product
from financeapp.filename_utils import document_pdf_filename, safe_filename

from .forms import CommercialInvoiceForm, ProformaInvoiceForm
from .models import CommercialInvoice, CommercialInvoiceItem, ProformaInvoice, ProformaInvoiceItem
from .pdf_builder import (
    PDF_BOTTOM_MARGIN,
    PDF_FONT_BOLD,
    PDF_FONT_BOLD_PATH,
    PDF_FONT_REGULAR,
    PDF_FONT_REGULAR_PATH,
    PDF_INVOICE_BOX_HEIGHT_MM,
    PDF_INVOICE_BOX_TITLE_GAP_MM,
    PDF_INVOICE_BOX_WIDTH_MM,
    PDF_INVOICE_COLUMN_WIDTH_MM,
    PDF_INVOICE_CONTENT_PADDING_MM,
    PDF_INVOICE_REFERENCE_GAP_MM,
    PDF_INVOICE_SIDE_MARGIN_MM,
    PDF_TOP_MARGIN,
    TOTALS_AMOUNT_COLUMN_WIDTH_MM,
    TOTALS_CELL_HORIZONTAL_PADDING,
    SHIPPING_ITEM_COLUMN_WIDTHS_MM,
    PACKING_COLUMN_WIDTHS_MM,
    PRICE_FOR_AMOUNT_RIGHT_INSET_MM,
    _info_box,
    _build_invoice_details,
    _build_invoice_item_table_styles,
    _build_items_table,
    _build_purchase_order_items_table,
    _build_totals_table,
    _build_shipping_items_table,
    _build_packing_section,
    _build_info_box_paragraphs,
    _build_purchase_order_styles,
    _build_styles,
    _compute_page_totals,
    _compute_purchase_order_page_totals,
    _format_invoice_note_text,
    _format_decimal_comma,
    _format_quantity,
    _format_measurement,
    _format_pdf_title,
    _format_preserving_layout,
    _partner_card,
    build_invoice_pdf,
    build_purchase_order_pdf,
    format_footer_invoice_lines,
)


class InvoiceProductAutofillJavaScriptTests(SimpleTestCase):

    def test_product_data_hs_code_maps_to_invoice_inline_field(self):
        with open(finders.find("admin/js/invoice_product_info.js"), encoding="utf-8") as script_file:
            script = script_file.read()

        self.assertIn("forceDefaultHsCode: productChanged", script)
        self.assertIn('hsCodeInput.value = data.hs_code || "";', script)
        self.assertIn('new Event("input", { bubbles: true })', script)
        self.assertIn('new Event("change", { bubbles: true })', script)

    def test_custom_delete_initializer_is_disabled_for_native_django_formsets(self):
        with open(finders.find("admin/js/inline_row_tools.js"), encoding="utf-8") as script_file:
            script = script_file.read()

        self.assertNotIn("initializeDeleteButtons(root || document)", script)
        self.assertNotIn("totalInput.value = String(total - 1)", script)


class PdfFilenameTests(SimpleTestCase):

    def test_safe_filename_replaces_forbidden_characters(self):
        self.assertEqual(safe_filename(' CI/2026\\0015:*?"<>| '), "CI-2026-0015-------")

    def test_document_pdf_filename_uses_readable_prefix(self):
        self.assertEqual(
            document_pdf_filename("Commercial-Invoice", "CI/2026/0015"),
            "Commercial-Invoice-CI-2026-0015.pdf",
        )

    def test_invoice_admin_pdf_links_open_new_tab_without_download_attribute(self):
        template_source = get_template("admin/invoices/change_form.html").template.source

        self.assertIn('target="_blank"', template_source)
        self.assertNotIn(" download", template_source)


class InvoiceCreatorAuditTests(TestCase):

    def setUp(self):
        self.creator = get_user_model().objects.create_superuser(
            username="invoice-creator",
            password="password123",
            email="creator@example.com",
        )
        self.other_user = get_user_model().objects.create_superuser(
            username="invoice-editor",
            password="password123",
            email="editor@example.com",
        )

    def test_invoice_drafts_store_creator_and_admin_displays_it(self):
        self.client.force_login(self.creator)

        for model, url_name in (
            (CommercialInvoice, "admin:invoices_commercialinvoice_add"),
        ):
            response = self.client.get(reverse(url_name))
            self.assertEqual(response.status_code, 302)
            document = model.objects.latest("pk")
            self.assertEqual(document.created_by, self.creator)

            response = self.client.get(
                reverse(
                    f"admin:invoices_{model._meta.model_name}_change",
                    args=[document.pk],
                )
            )
            self.assertContains(response, self.creator.get_username())

    def test_creator_is_not_replaced_when_document_is_edited(self):
        document = CommercialInvoice.objects.create(created_by=self.creator)
        model_admin = admin.site._registry[CommercialInvoice]
        request = type("Request", (), {"user": self.other_user})()

        model_admin.save_model(request, document, form=None, change=True)
        document.refresh_from_db()

        self.assertEqual(document.created_by, self.creator)


class ProformaStaffAccessTests(TestCase):

    def test_staff_account_can_open_proforma_changelist(self):
        staff_group = Group.objects.create(name="Staff")
        user = get_user_model().objects.create_user(
            username="proforma-staff",
            password="password123",
            is_active=True,
            is_staff=True,
        )
        user.groups.add(staff_group)
        user.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="invoices",
                codename="view_proformainvoice",
            )
        )
        ProformaInvoice.objects.create()
        self.client.force_login(user)

        response = self.client.get(
            reverse("admin:invoices_proformainvoice_changelist")
        )

        self.assertEqual(response.status_code, 200)

    def test_proforma_creator_is_recorded_in_native_admin_log(self):
        user = get_user_model().objects.create_superuser(
            username="proforma-creator",
            password="password123",
            email="proforma-creator@example.com",
        )
        self.client.force_login(user)

        response = self.client.get(reverse("admin:invoices_proformainvoice_add"))

        self.assertEqual(response.status_code, 302)
        proforma = ProformaInvoice.objects.latest("pk")
        model_admin = admin.site._registry[ProformaInvoice]
        self.assertEqual(model_admin.created_by_display(proforma), user)


class StaffInvoiceScopeTests(TestCase):

    def setUp(self):
        group = Group.objects.create(name="Staff")
        group.permissions.add(
            *Permission.objects.filter(
                content_type__app_label="invoices",
                codename__in=(
                    "view_proformainvoice",
                    "add_proformainvoice",
                    "change_proformainvoice",
                    "view_commercialinvoice",
                    "add_commercialinvoice",
                    "change_commercialinvoice",
                ),
            )
        )
        self.user = get_user_model().objects.create_user(
            username="scoped-invoice-staff",
            password="password123",
            is_active=True,
            is_staff=True,
        )
        self.user.groups.add(group)
        self.client.force_login(self.user)

    def test_staff_sees_only_own_commercial_invoices(self):
        own = CommercialInvoice.objects.create(created_by=self.user)
        other = CommercialInvoice.objects.create()

        response = self.client.get(
            reverse("admin:invoices_commercialinvoice_changelist")
        )

        self.assertContains(response, own.invoice_number)
        self.assertNotContains(response, other.invoice_number)
        self.assertEqual(
            self.client.get(
                reverse("admin:invoices_commercialinvoice_change", args=[other.pk])
            ).status_code,
            403,
        )

    def test_staff_sees_only_proformas_created_through_own_session(self):
        response = self.client.get(reverse("admin:invoices_proformainvoice_add"))
        self.assertEqual(response.status_code, 302)
        own = ProformaInvoice.objects.latest("pk")
        other = ProformaInvoice.objects.create()

        response = self.client.get(
            reverse("admin:invoices_proformainvoice_changelist")
        )

        self.assertContains(response, own.invoice_number)
        self.assertNotContains(response, other.invoice_number)
        self.assertEqual(
            self.client.get(
                reverse("admin:invoices_proformainvoice_change", args=[other.pk])
            ).status_code,
            403,
        )

    def test_staff_cannot_open_commercial_reports(self):
        response = self.client.get(reverse("admin:commercial_invoice_report"))

        self.assertEqual(response.status_code, 403)

    def test_report_permission_allows_report_but_keeps_document_scope(self):
        own = CommercialInvoice.objects.create(created_by=self.user)
        other = CommercialInvoice.objects.create()
        self.user.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="invoices",
                codename="view_commercial_invoice_reports",
            )
        )
        self.user = get_user_model().objects.get(pk=self.user.pk)
        self.client.force_login(self.user)

        response = self.client.get(reverse("admin:commercial_invoice_report"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, own.invoice_number)
        self.assertNotContains(response, other.invoice_number)

    def test_global_document_permission_reveals_all_invoices(self):
        other_commercial = CommercialInvoice.objects.create()
        other_proforma = ProformaInvoice.objects.create()
        self.user.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="invoices",
                codename="view_all_documents",
            )
        )
        self.user = get_user_model().objects.get(pk=self.user.pk)
        self.client.force_login(self.user)

        commercial_response = self.client.get(
            reverse("admin:invoices_commercialinvoice_changelist")
        )
        proforma_response = self.client.get(
            reverse("admin:invoices_proformainvoice_changelist")
        )

        self.assertContains(commercial_response, other_commercial.invoice_number)
        self.assertContains(proforma_response, other_proforma.invoice_number)


class PdfPaginationTests(SimpleTestCase):

    def test_product_description_layout_is_preserved_for_pdf_tables(self):
        formatted = _format_preserving_layout(
            "First line\n\n Second line   with spaces "
        )

        self.assertEqual(
            formatted,
            "First line<br/><br/>&nbsp;Second line&nbsp;&nbsp; with spaces&nbsp;",
        )

    def test_product_description_supports_safe_bold_formatting(self):
        formatted = _format_preserving_layout(
            "Normal **bold words**\n**complete bold line**"
        )

        self.assertEqual(
            formatted,
            "Normal <b>bold words</b><br/><b>complete bold line</b>",
        )

    def test_product_description_supports_underlined_text(self):
        formatted = _format_preserving_layout(
            "Normal __underlined words__\n__complete underlined line__"
        )

        self.assertEqual(
            formatted,
            "Normal <u>underlined words</u><br/><u>complete underlined line</u>",
        )

    def test_product_description_escapes_html_before_applying_bold(self):
        formatted = _format_preserving_layout("**<script>alert(1)</script>**")

        self.assertEqual(
            formatted,
            "<b>&lt;script&gt;alert(1)&lt;/script&gt;</b>",
        )


    def test_pdf_numbers_use_thousands_separator_and_decimal_point(self):
        self.assertEqual(_format_decimal_comma(100), "100.00")
        self.assertEqual(_format_decimal_comma(3390), "3,390.00")
        self.assertEqual(_format_decimal_comma(339000), "339,000.00")
        self.assertEqual(_format_decimal_comma(12345678.9), "12,345,678.90")

    def test_purchase_order_quantity_omits_unnecessary_decimals(self):
        self.assertEqual(_format_quantity(400), "400")
        self.assertEqual(_format_quantity("1200.00"), "1,200")
        self.assertEqual(_format_quantity("2.5"), "2.5")

    def test_packing_measurements_omit_unnecessary_decimals(self):
        self.assertEqual(_format_measurement("648.000"), "648")
        self.assertEqual(_format_measurement("60.00"), "60")
        self.assertEqual(_format_measurement("60.50"), "60.5")
        self.assertEqual(_format_measurement("60.125"), "60.125")

    def test_pdf_uses_bundled_dejavu_fonts(self):
        styles = _build_styles()
        pdf_bytes = self._build_test_invoice_pdf(1)

        self.assertTrue(PDF_FONT_REGULAR_PATH.is_file())
        self.assertTrue(PDF_FONT_BOLD_PATH.is_file())
        self.assertEqual(styles["body"].fontName, PDF_FONT_REGULAR)
        self.assertEqual(styles["title"].fontName, PDF_FONT_BOLD)
        self.assertIn(b"DejaVuSans", pdf_bytes)

    def test_item_table_allows_reportlab_to_split_rows_across_pages(self):
        table = _build_items_table(self._pdf_items(51), "EUR", _build_styles())

        self.assertEqual(table.repeatRows, 1)
        self.assertEqual(table.splitByRow, 1)
        self.assertEqual(len(table._cellvalues), 52)

    def test_invoice_pagination_uses_available_page_height(self):
        expected_page_counts = {
            1: 1,
            9: 1,
            10: 1,
            20: 2,
            21: 2,
            49: 2,
            51: 2,
        }

        actual_page_counts = {
            item_count: self._pdf_page_count(self._build_test_invoice_pdf(item_count))
            for item_count in expected_page_counts
        }

        self.assertEqual(actual_page_counts, expected_page_counts)

    def test_commercial_invoice_uses_the_same_natural_pagination(self):
        expected_page_counts = {
            1: 1,
            9: 1,
            10: 1,
            20: 2,
            21: 2,
            49: 2,
            51: 2,
        }

        actual_page_counts = {
            item_count: self._pdf_page_count(
                self._build_test_invoice_pdf(item_count, invoice_type_name="CommercialInvoice")
            )
            for item_count in expected_page_counts
        }

        self.assertEqual(actual_page_counts, expected_page_counts)

    def test_purchase_order_pagination_uses_available_page_height(self):
        expected_page_counts = {
            1: 1,
            9: 1,
            10: 1,
            20: 1,
            21: 1,
            49: 2,
            51: 2,
        }

        actual_page_counts = {
            item_count: self._pdf_page_count(self._build_test_purchase_order_pdf(item_count))
            for item_count in expected_page_counts
        }

        self.assertEqual(actual_page_counts, expected_page_counts)

    def test_product_rows_use_compact_padding_and_keep_header_padding(self):
        item = {**self._pdf_items(1)[0], "vat_percent": Decimal("0.00")}
        tables = [
            _build_items_table([item], "EUR", _build_styles()),
            _build_shipping_items_table([item], _build_styles()),
            _build_purchase_order_items_table([item], "EUR", _build_purchase_order_styles()),
        ]

        for table in tables:
            with self.subTest(column_count=len(table._cellStyles[0])):
                self.assertEqual(table.repeatRows, 1)
                self.assertTrue(all(cell.topPadding == 5 for cell in table._cellStyles[0]))
                self.assertTrue(all(cell.bottomPadding == 5 for cell in table._cellStyles[0]))
                self.assertTrue(all(cell.topPadding == 4 for cell in table._cellStyles[1]))
                self.assertTrue(all(cell.bottomPadding == 4 for cell in table._cellStyles[1]))

    def test_51_product_totals_remain_complete(self):
        items = self._pdf_items(51)
        invoice = SimpleNamespace(
            vat_percent=Decimal("20.00"),
            freight=Decimal("15.00"),
            discount=Decimal("5.00"),
        )
        purchase_order = SimpleNamespace(
            vat_percent=Decimal("20.00"),
            freight=Decimal("15.00"),
        )

        invoice_totals = _compute_page_totals(invoice=invoice, item_pages=[items])[-1]
        purchase_totals = _compute_purchase_order_page_totals(
            purchase_order=purchase_order,
            item_pages=[items],
        )[-1]

        self.assertEqual(invoice_totals["all_pages_gross_value"], Decimal("510.00"))
        self.assertEqual(invoice_totals["all_pages_total"], Decimal("622.00"))
        self.assertEqual(purchase_totals["all_pages_gross"], Decimal("510.00"))
        self.assertEqual(purchase_totals["all_pages_total"], Decimal("627.00"))

    @staticmethod
    def _pdf_items(item_count):
        return [
            {
                "index": index,
                "description": f"Test product {index}",
                "part_number": f"PN-{index}",
                "hs_code": "1234",
                "quantity": 1,
                "unit_price": Decimal("10.00"),
                "total_amount": Decimal("10.00"),
            }
            for index in range(1, item_count + 1)
        ]

    @classmethod
    def _build_test_invoice_pdf(cls, item_count, invoice_type_name="ProformaInvoice"):
        invoice_class = type(
            invoice_type_name,
            (),
            {"total_amount": lambda self: Decimal(item_count * 10)},
        )
        invoice = invoice_class()
        invoice.invoice_number = "TEST-PDF"
        invoice.invoice_date = date(2026, 9, 29)
        invoice.freight = Decimal("0.00")
        invoice.discount = Decimal("0.00")
        invoice.vat_percent = Decimal("0.00")
        invoice.terms_conditions = ""
        invoice.delivery_time = ""
        invoice.price_for = ""
        invoice.our_reference = ""
        company = SimpleNamespace(
            company_name="Test Company",
            currency="EUR",
            company_logo=None,
            invoice_note="",
            terms_conditions="",
            delivery_time="",
            proforma_validity=None,
            bank="",
            iban="",
            bic="",
            footer_invoice="",
            company_phone="",
            company_fax="",
            company_email="",
        )
        partner = {
            "name": "Test Partner",
            "addresses": ["Test address"],
            "phones": [],
            "email": "",
            "website": "",
            "fax": "",
        }
        return build_invoice_pdf(
            invoice=invoice,
            company=company,
            items=cls._pdf_items(item_count),
            importer=partner,
            end_user=partner,
            invoice_title="Proforma Invoice",
            currency="EUR",
        )

    @classmethod
    def _build_test_purchase_order_pdf(cls, item_count):
        purchase_order = SimpleNamespace(
            purchase_number="TEST-PO",
            purchase_date=date(2026, 9, 29),
            vat_percent=Decimal("0.00"),
            freight=Decimal("0.00"),
            shipment="",
            sent_by="",
            sales_condition="",
            payment_condition="",
            delivery_terms="",
        )
        company = SimpleNamespace(
            company_name="Test Company",
            currency="EUR",
            company_logo=None,
            note="",
            address="",
            company_address="",
            siren="",
            company_email="",
            company_phone="",
            company_fax="",
            footer_order="",
            president="",
            bank="",
            iban="",
            bic="",
            footer_invoice="",
        )
        partner = {
            "name": "Test Partner",
            "addresses": ["Test address"],
            "phones": [],
            "email": "",
            "website": "",
            "fax": "",
        }
        items = [
            {**item, "vat_percent": Decimal("0.00")}
            for item in cls._pdf_items(item_count)
        ]
        return build_purchase_order_pdf(
            purchase_order=purchase_order,
            company=company,
            items=items,
            seller=partner,
            requester=partner,
            currency="EUR",
        )

    @staticmethod
    def _pdf_page_count(pdf_bytes):
        return len(re.findall(rb"/Type\s*/Page\b", pdf_bytes))

    def test_pdf_body_uses_extra_space_above_footer(self):
        self.assertEqual(PDF_TOP_MARGIN, 38)
        self.assertEqual(PDF_BOTTOM_MARGIN, 23)
        self.assertEqual(PDF_INVOICE_COLUMN_WIDTH_MM, 96)
        self.assertEqual(PDF_INVOICE_SIDE_MARGIN_MM, 9)

    def test_item_table_font_size_is_not_reduced_for_pdf_pagination(self):
        styles = _build_styles()
        item_styles = _build_invoice_item_table_styles(styles)

        self.assertEqual(item_styles["table_cell"].fontSize, styles["table_cell"].fontSize)
        self.assertEqual(item_styles["table_cell_part_number"].fontSize, styles["table_cell_part_number"].fontSize)
        self.assertEqual(
            item_styles["table_cell"].fontSize,
            item_styles["table_cell_part_number"].fontSize,
        )
        self.assertEqual(
            item_styles["table_cell_amount"].fontSize,
            item_styles["table_cell_part_number"].fontSize,
        )

    def test_purchase_order_item_table_font_size_keeps_normal_pdf_size(self):
        styles = _build_styles()
        purchase_styles = _build_purchase_order_styles()

        self.assertEqual(purchase_styles["table_cell"].fontSize, styles["table_cell"].fontSize)
        self.assertEqual(purchase_styles["table_cell_part_number"].fontSize, styles["table_cell_part_number"].fontSize)
        self.assertEqual(
            purchase_styles["table_cell"].fontSize,
            purchase_styles["table_cell_part_number"].fontSize,
        )
        self.assertEqual(
            purchase_styles["table_cell_amount"].fontSize,
            purchase_styles["table_cell_part_number"].fontSize,
        )

    def test_pdf_table_headers_do_not_split_words(self):
        styles = _build_styles()

        self.assertFalse(styles["table_head"].splitLongWords)
        self.assertFalse(styles["table_head_center"].splitLongWords)
        self.assertFalse(styles["table_head_amount"].splitLongWords)

    def test_shipping_pdf_table_uses_item_hs_code(self):
        table = _build_shipping_items_table(
            [
                {
                    "index": 1,
                    "description": "Test item",
                    "part_number": "PART-001",
                    "hs_code": "8481.80",
                    "quantity": 2,
                }
            ],
            _build_styles(),
        )

        self.assertEqual(table._cellvalues[0][3].getPlainText(), "HS Code")
        self.assertEqual(table._cellvalues[1][3].getPlainText(), "8481.80")

    def test_shipping_item_number_header_stays_on_one_line(self):
        table = _build_shipping_items_table([], _build_styles())
        header = table._cellvalues[0][0]
        available_width = table._colWidths[0] - 8
        _width, height = header.wrap(available_width, 100 * mm)

        self.assertEqual(header.getPlainText(), "Item No")
        self.assertNotIn("Item\nNo.", header.text)
        self.assertEqual(table._colWidths[0], SHIPPING_ITEM_COLUMN_WIDTHS_MM[0] * mm)
        self.assertLessEqual(height, header.style.leading)
        self.assertEqual(header.style.alignment, 1)
        self.assertEqual(table._cellvalues[1][0].style.alignment, 1)

    def test_packing_item_number_header_stays_on_one_line(self):
        invoice = CommercialInvoice(packing_specification="Boxes")
        packing_table = _build_packing_section(
            invoice=invoice,
            packing_entries=[],
            styles=_build_styles(),
        )[-1]
        header = packing_table._cellvalues[0][0]
        available_width = packing_table._colWidths[0] - 8
        _width, height = header.wrap(available_width, 100 * mm)

        self.assertEqual(header.getPlainText(), "Item No")
        self.assertNotIn("Item\nNo.", header.text)
        self.assertEqual(packing_table._colWidths[0], PACKING_COLUMN_WIDTHS_MM[0] * mm)
        self.assertLessEqual(height, header.style.leading)
        shipping_header = _build_shipping_items_table([], _build_styles())._cellvalues[0][0]
        self.assertEqual(header.style.name, shipping_header.style.name)
        self.assertEqual(header.style.fontName, shipping_header.style.fontName)
        self.assertEqual(header.style.fontSize, shipping_header.style.fontSize)
        self.assertEqual(header.style.alignment, shipping_header.style.alignment)

    def test_pdf_titles_capitalize_each_word(self):
        self.assertEqual(_format_pdf_title("COMMERCIAL INVOICE"), "Commercial Invoice")
        self.assertEqual(_format_pdf_title("Proforma Invoice"), "Proforma Invoice")
        self.assertEqual(_format_pdf_title("PACKING LIST"), "Packing List")
        self.assertEqual(_format_pdf_title("COMMAND / ORDER"), "Command / Order")

    def test_dispatching_note_line_uses_bold_font(self):
        styles = _build_styles()

        self.assertEqual(styles["body_left_bold"].fontName, styles["invoice_box_title"].fontName)
        self.assertNotEqual(styles["body_left_bold"].fontName, styles["body_left"].fontName)

    def test_totals_labels_use_the_main_pdf_color(self):
        styles = _build_styles()

        self.assertEqual(styles["totals_label"].textColor, styles["table_head"].textColor)

    def test_invoice_information_boxes_use_the_pdf_accent_border(self):
        styles = _build_styles()
        partner = {"name": "Test", "addresses": [], "phones": []}

        importer_box = _partner_card("Importer", partner, styles, accent_border=True)
        note_box = _info_box("Invoice Note", "-", styles)

        self.assertIn("BOX", [command[0] for command in importer_box._linecmds])
        self.assertNotIn("BOX", [command[0] for command in note_box._linecmds])
        self.assertEqual(
            [command[3] for command in importer_box._linecmds if command[0] == "BOX"],
            [1],
        )
        self.assertEqual(importer_box._argH, [PDF_INVOICE_BOX_HEIGHT_MM * mm])
        self.assertEqual(importer_box._argW, [PDF_INVOICE_BOX_WIDTH_MM * mm])
        self.assertEqual(note_box._argH, importer_box._argH)
        self.assertEqual(note_box._argW, importer_box._argW)
        self.assertEqual(importer_box._cellStyles[0][0].valign, "TOP")
        self.assertEqual(note_box._cellStyles[0][0].valign, "TOP")
        self.assertEqual(importer_box._cellvalues[0][0][1].height, PDF_INVOICE_BOX_TITLE_GAP_MM * mm)
        self.assertEqual(note_box._cellvalues[0][0][1].height, PDF_INVOICE_BOX_TITLE_GAP_MM * mm)

    def test_company_setting_blank_lines_are_preserved_in_pdf_boxes(self):
        styles = _build_styles()
        formatted_note = _format_invoice_note_text("First line\n\nSecond line")
        paragraphs = _build_info_box_paragraphs(
            formatted_note,
            styles,
            body_style_key="invoice_note_body",
        )

        self.assertIn("<br/><br/>", formatted_note)
        self.assertEqual(sum(isinstance(item, Spacer) for item in paragraphs), 1)

    def test_invoice_note_does_not_invent_line_breaks(self):
        formatted_note = _format_invoice_note_text(
            "First sentence. Second sentence\n\nAuthorised signature"
        )

        self.assertEqual(
            formatted_note,
            "First sentence. Second sentence<br/><br/>Authorised signature",
        )

    def test_invoice_note_preserves_repeated_spaces_from_company_settings(self):
        formatted_note = _format_invoice_note_text(
            "Payment   in    four batches"
        )

        self.assertEqual(
            formatted_note,
            "Payment&nbsp;&nbsp; in&nbsp;&nbsp;&nbsp; four batches",
        )

    def test_invoice_reference_stays_about_five_lines_below_invoice_note(self):
        invoice = ProformaInvoice()
        invoice.subtotal = lambda: Decimal("0.00")
        blocks = _build_invoice_details(
            invoice,
            CompanySetting(invoice_note="Invoice note"),
            _build_styles(),
        )

        self.assertIsInstance(blocks[1], Spacer)
        self.assertEqual(blocks[1].height, PDF_INVOICE_REFERENCE_GAP_MM * mm)
        self.assertEqual(PDF_INVOICE_REFERENCE_GAP_MM, 17)
        reference_table = blocks[2]
        self.assertEqual(
            [cell.leftPadding for cell in reference_table._cellStyles[0]],
            [4, 4, TOTALS_CELL_HORIZONTAL_PADDING, 4],
        )

    def test_price_for_row_shows_formatted_grand_total_in_amount_column(self):
        invoice = ProformaInvoice(
            price_for="CPT Isfahan / Iran",
            freight=Decimal("100.00"),
            discount=Decimal("7.50"),
            vat_percent=Decimal("20.00"),
        )
        invoice.subtotal = lambda: Decimal("516500.00")

        blocks = _build_invoice_details(
            invoice,
            CompanySetting(invoice_note="Invoice note"),
            _build_styles(),
            currency="EUR",
        )

        reference_table = blocks[2]
        totals_table = _build_totals_table(invoice, "EUR", _build_styles())
        self.assertEqual(reference_table._colWidths[2], totals_table._colWidths[-1])
        self.assertEqual(reference_table._colWidths[2], TOTALS_AMOUNT_COLUMN_WIDTH_MM * mm)
        self.assertEqual(reference_table._colWidths[3], PRICE_FOR_AMOUNT_RIGHT_INSET_MM * mm)
        self.assertEqual(sum(reference_table._colWidths), PDF_INVOICE_COLUMN_WIDTH_MM * 2 * mm)
        self.assertEqual(reference_table._cellStyles[0][2].rightPadding, TOTALS_CELL_HORIZONTAL_PADDING)
        self.assertEqual(
            reference_table._cellStyles[0][2].rightPadding,
            totals_table._cellStyles[0][1].rightPadding,
        )
        self.assertEqual(reference_table.hAlign, "LEFT")
        self.assertEqual(totals_table.hAlign, "RIGHT")
        self.assertIn("Price for:", reference_table._cellvalues[0][1].getPlainText())
        self.assertIn("CPT Isfahan / Iran", reference_table._cellvalues[0][1].getPlainText())
        self.assertEqual(
            reference_table._cellvalues[0][2].getPlainText(),
            "619,892.50 €",
        )
        self.assertEqual(
            reference_table._cellvalues[0][2].getPlainText(),
            f"{_format_decimal_comma(invoice.total_amount())} €",
        )
        self.assertEqual(reference_table._cellStyles[0][2].alignment, "RIGHT")

    def test_footer_invoice_city_country_moves_to_next_line(self):
        lines = format_footer_invoice_lines(
            "VERTEA SAS 23 route de Gisy - 91570 Bievres / France"
        )

        self.assertEqual(
            lines,
            [
                "VERTEA SAS",
                "23 route de Gisy - 91570",
                "Bievres / France",
            ],
        )


class ProformaConversionTests(TestCase):

    def setUp(self):
        CompanySetting.objects.create(
            company_name="Societe Test",
            vat_amount=Decimal("20.00"),
        )
        self.importer = Partner.objects.create(
            description="Client Test",
            partner_type="importer",
        )
        self.product = Product.objects.create(
            description="Produit Test",
            part_number="TEST-001",
            hs_code="8544.42",
            unit_qty=10,
            sale_price=Decimal("25.00"),
        )

    def test_new_invoice_number_uses_hyphens(self):
        invoice = ProformaInvoice.objects.create(importer=self.importer)

        self.assertRegex(invoice.invoice_number, r"^FR-2026-\d{4}$")

    def test_invoice_number_uses_company_setting_year(self):
        company = CompanySetting.objects.get()
        company.year = 2031
        company.save()

        invoice = ProformaInvoice.objects.create(importer=self.importer)

        self.assertEqual(invoice.invoice_number, "FR-2031-0001")

    def test_number_sequence_reads_legacy_slash_numbers(self):
        year = CompanySetting.objects.get().year
        legacy_invoice = ProformaInvoice.objects.create(
            importer=self.importer,
            invoice_number=f"LEGACY-{year}-0008",
        )
        ProformaInvoice.objects.filter(pk=legacy_invoice.pk).update(
            invoice_number=f"FR/{year}/0008",
        )

        invoice = ProformaInvoice.objects.create(importer=self.importer)

        self.assertEqual(invoice.invoice_number, f"FR-{year}-0009")

    def test_exact_invoice_number_search_excludes_other_field_matches(self):
        exact_invoice = ProformaInvoice.objects.create(
            importer=self.importer,
            invoice_number="FR-2024-0002",
            invoice_date="2024-01-04",
        )
        ProformaInvoice.objects.create(
            importer=self.importer,
            invoice_number="FR-2024-0005",
            our_reference="FR-2024-0002",
            invoice_date="2024-01-05",
        )
        model_admin = admin.site._registry[ProformaInvoice]

        results, may_have_duplicates = model_admin.get_search_results(
            None,
            ProformaInvoice.objects.filter(invoice_date__year=2026),
            "FR-2024-0002",
        )

        self.assertEqual(list(results), [exact_invoice])
        self.assertFalse(may_have_duplicates)

    def test_convert_to_commercial_decreases_product_quantity(self):
        proforma = ProformaInvoice.objects.create(importer=self.importer)
        ProformaInvoiceItem.objects.create(
            invoice=proforma,
            product=self.product,
            quantity=3,
            unit_price=Decimal("25.00"),
        )

        commercial = proforma.convert_to_commercial(user_initiated=True)

        self.product.refresh_from_db()

        self.assertIsInstance(commercial, CommercialInvoice)
        self.assertEqual(commercial.items.count(), 1)
        self.assertEqual(self.product.unit_qty, 7)

    def test_convert_to_commercial_preserves_proforma_item_pk_order(self):
        products = {
            name: Product.objects.create(
                description=f"Product {name}",
                part_number=f"ORDER-{name}",
                unit_qty=10,
                sale_price=Decimal("25.00"),
            )
            for name in ("A", "B", "X", "Z")
        }
        proforma = ProformaInvoice.objects.create(importer=self.importer)
        for name in ("X", "A", "Z", "B"):
            ProformaInvoiceItem.objects.create(
                invoice=proforma,
                product=products[name],
                quantity=1,
                unit_price=Decimal("25.00"),
            )

        with CaptureQueriesContext(connection) as queries:
            commercial = proforma.convert_to_commercial(user_initiated=True)

        self.assertEqual(
            list(commercial.items.order_by("pk").values_list("product__description", flat=True)),
            ["Product X", "Product A", "Product Z", "Product B"],
        )
        source_table = ProformaInvoiceItem._meta.db_table
        source_queries = [
            query["sql"]
            for query in queries.captured_queries
            if source_table in query["sql"] and query["sql"].lstrip().upper().startswith("SELECT")
        ]
        self.assertTrue(source_queries)
        self.assertTrue(any("ORDER BY" in query.upper() and '"ID" ASC' in query.upper() for query in source_queries))

    def test_convert_to_commercial_only_decreases_stock_once(self):
        proforma = ProformaInvoice.objects.create(importer=self.importer)
        ProformaInvoiceItem.objects.create(
            invoice=proforma,
            product=self.product,
            quantity=4,
            unit_price=Decimal("25.00"),
        )

        first_commercial = proforma.convert_to_commercial(user_initiated=True)
        second_commercial = proforma.convert_to_commercial(user_initiated=True)

        self.product.refresh_from_db()

        self.assertEqual(first_commercial.pk, second_commercial.pk)
        self.assertEqual(CommercialInvoice.objects.count(), 1)
        self.assertEqual(self.product.unit_qty, 6)

    def test_reconvert_to_commercial_syncs_existing_invoice_changes(self):
        proforma = ProformaInvoice.objects.create(
            importer=self.importer,
            freight=Decimal("10.00"),
            discount=Decimal("1.00"),
            vat_percent=Decimal("20.00"),
        )
        item = ProformaInvoiceItem.objects.create(
            invoice=proforma,
            product=self.product,
            part_number="INITIAL-REF",
            quantity=4,
            unit_price=Decimal("25.00"),
        )
        commercial = proforma.convert_to_commercial(user_initiated=True)
        original_commercial_number = commercial.invoice_number

        proforma.freight = Decimal("30.00")
        proforma.discount = Decimal("5.00")
        proforma.vat_percent = Decimal("7.50")
        proforma.save()
        item.part_number = "UPDATED-REF"
        item.quantity = 2
        item.unit_price = Decimal("40.00")
        item.save()

        synced_commercial = proforma.convert_to_commercial(user_initiated=True)
        synced_item = synced_commercial.items.get()
        self.product.refresh_from_db()

        self.assertEqual(synced_commercial.pk, commercial.pk)
        self.assertEqual(CommercialInvoice.objects.count(), 1)
        self.assertEqual(synced_commercial.invoice_number, original_commercial_number)
        self.assertEqual(synced_commercial.freight, Decimal("30.00"))
        self.assertEqual(synced_commercial.discount, Decimal("5.00"))
        self.assertEqual(synced_commercial.vat_percent, Decimal("7.50"))
        self.assertEqual(synced_item.part_number, "UPDATED-REF")
        self.assertEqual(synced_item.quantity, 2)
        self.assertEqual(synced_item.unit_price, Decimal("40.00"))
        self.assertEqual(self.product.unit_qty, 8)

    def test_convert_to_commercial_requires_user_initiated_action(self):
        proforma = ProformaInvoice.objects.create(importer=self.importer)
        ProformaInvoiceItem.objects.create(
            invoice=proforma,
            product=self.product,
            quantity=2,
            unit_price=Decimal("25.00"),
        )

        commercial = proforma.convert_to_commercial()

        self.product.refresh_from_db()

        self.assertIsNone(commercial)
        self.assertEqual(CommercialInvoice.objects.count(), 0)
        self.assertEqual(self.product.unit_qty, 10)

    def test_convert_to_commercial_keeps_item_hs_code(self):
        proforma = ProformaInvoice.objects.create(importer=self.importer)
        ProformaInvoiceItem.objects.create(
            invoice=proforma,
            product=self.product,
            hs_code="8544.42",
            quantity=1,
            unit_price=Decimal("25.00"),
        )

        commercial = proforma.convert_to_commercial(user_initiated=True)

        self.assertEqual(commercial.items.count(), 1)
        self.assertEqual(commercial.items.first().hs_code, "8544.42")

    def test_convert_to_commercial_keeps_item_part_number(self):
        proforma = ProformaInvoice.objects.create(importer=self.importer)
        ProformaInvoiceItem.objects.create(
            invoice=proforma,
            product=self.product,
            part_number="CUSTOM-REF",
            quantity=1,
            unit_price=Decimal("25.00"),
        )

        commercial = proforma.convert_to_commercial(user_initiated=True)

        self.assertEqual(commercial.items.count(), 1)
        self.assertEqual(commercial.items.first().part_number, "CUSTOM-REF")

    def test_proforma_item_uses_product_hs_code_by_default(self):
        proforma = ProformaInvoice.objects.create(importer=self.importer)

        item = ProformaInvoiceItem.objects.create(
            invoice=proforma,
            product=self.product,
            quantity=1,
            unit_price=Decimal("25.00"),
        )

        self.assertEqual(item.hs_code, "8544.42")

    def test_proforma_item_uses_product_part_number_by_default(self):
        proforma = ProformaInvoice.objects.create(importer=self.importer)

        item = ProformaInvoiceItem.objects.create(
            invoice=proforma,
            product=self.product,
            quantity=1,
            unit_price=Decimal("25.00"),
        )

        self.assertEqual(item.part_number, "TEST-001")

    def test_total_amount_uses_company_setting_vat(self):
        proforma = ProformaInvoice.objects.create(
            importer=self.importer,
            freight=Decimal("10.00"),
            discount=Decimal("5.00"),
            vat_percent=Decimal("20.00"),
        )
        ProformaInvoiceItem.objects.create(
            invoice=proforma,
            product=self.product,
            quantity=2,
            unit_price=Decimal("25.00"),
        )

        self.assertEqual(proforma.subtotal(), Decimal("50.00"))
        self.assertEqual(proforma.vat_amount(), Decimal("10.00"))
        self.assertEqual(proforma.total_amount(), Decimal("65.00"))

    def test_convert_to_commercial_keeps_invoice_vat_percent(self):
        proforma = ProformaInvoice.objects.create(
            importer=self.importer,
            vat_percent=Decimal("7.50"),
        )
        ProformaInvoiceItem.objects.create(
            invoice=proforma,
            product=self.product,
            quantity=2,
            unit_price=Decimal("25.00"),
        )

        commercial = proforma.convert_to_commercial(user_initiated=True)

        self.assertEqual(commercial.vat_percent, Decimal("7.50"))

    def test_convert_to_commercial_keeps_edited_delivery_and_payment_terms(self):
        proforma = ProformaInvoice.objects.create(
            importer=self.importer,
            delivery_time="Custom delivery",
            terms_conditions="Custom payment",
        )

        commercial = proforma.convert_to_commercial(user_initiated=True)

        self.assertEqual(commercial.delivery_time, "Custom delivery")
        self.assertEqual(commercial.terms_conditions, "Custom payment")

    def test_proforma_item_price_change_does_not_update_product_sale_price(self):
        proforma = ProformaInvoice.objects.create(importer=self.importer)
        item = ProformaInvoiceItem.objects.create(
            invoice=proforma,
            product=self.product,
            quantity=1,
            unit_price=Decimal("25.00"),
        )

        item.unit_price = Decimal("42.00")
        item.save()
        self.product.refresh_from_db()

        self.assertEqual(item.unit_price, Decimal("42.00"))
        self.assertEqual(self.product.sale_price, Decimal("25.00"))


class InvoiceFormVatDefaultTests(TestCase):

    def test_blank_invoice_financial_fields_are_saved_as_zero(self):
        CompanySetting.objects.create(
            company_name="Societe Zero",
            vat_amount=Decimal("20.00"),
        )
        form = CommercialInvoiceForm(
            data={
                "invoice_number": "",
                "invoice_date": "2026-07-20",
                "importer": "",
                "end_user": "",
                "freight": "",
                "discount": "",
                "vat_percent": "",
            }
        )

        self.assertTrue(form.is_valid(), form.errors)
        invoice = form.save()
        self.assertEqual(invoice.freight, Decimal("0.00"))
        self.assertEqual(invoice.discount, Decimal("0.00"))
        self.assertEqual(invoice.vat_percent, Decimal("20.00"))

    def test_proforma_form_uses_company_vat_as_initial_value(self):
        CompanySetting.objects.create(
            company_name="Societe TVA",
            vat_amount=Decimal("19.60"),
            delivery_time="2 weeks",
            terms_conditions="30 days",
        )

        form = ProformaInvoiceForm()

        self.assertEqual(form.fields["vat_percent"].initial, Decimal("19.60"))
        self.assertEqual(form.fields["delivery_time"].initial, "2 weeks")
        self.assertEqual(form.fields["terms_conditions"].initial, "30 days")

    def test_commercial_form_uses_company_vat_as_initial_value(self):
        CompanySetting.objects.create(
            company_name="Societe TVA",
            vat_amount=Decimal("8.50"),
            delivery_time="1 week",
            terms_conditions="At sight",
        )

        form = CommercialInvoiceForm()

        self.assertEqual(form.fields["vat_percent"].initial, Decimal("8.50"))
        self.assertEqual(form.fields["delivery_time"].initial, "1 week")
        self.assertEqual(form.fields["terms_conditions"].initial, "At sight")

    def test_proforma_form_saves_user_edited_delivery_and_payment_terms(self):
        CompanySetting.objects.create(
            company_name="Societe TVA",
            vat_amount=Decimal("19.60"),
            delivery_time="Company delivery",
            terms_conditions="Company payment",
        )

        form = ProformaInvoiceForm(
            data={
                "invoice_date": "2026-07-20",
                "importer": "",
                "end_user": "",
                "vat_percent": "19.60",
                "our_reference": "",
                "price_for": "",
                "delivery_time": "User delivery",
                "terms_conditions": "User payment",
                "freight": "0.00",
                "discount": "0.00",
            }
        )

        self.assertTrue(form.is_valid(), form.errors)
        invoice = form.save()
        self.assertEqual(invoice.delivery_time, "User delivery")
        self.assertEqual(invoice.terms_conditions, "User payment")


class InvoiceItemPdfOrderingTests(TestCase):

    def setUp(self):
        CompanySetting.objects.create(
            company_name="Societe Ordering",
            vat_amount=Decimal("20.00"),
        )
        self.products = {
            name: Product.objects.create(
                description=f"Product {name}",
                part_number=f"ORDER-{name}",
                unit_qty=100,
                sale_price=Decimal("10.00"),
            )
            for name in ("A", "B", "X", "Z")
        }

    def test_invoice_admin_and_pdf_items_use_line_pk_order(self):
        cases = (
            (ProformaInvoice, ProformaInvoiceItem),
            (CommercialInvoice, CommercialInvoiceItem),
        )

        for invoice_model, item_model in cases:
            with self.subTest(invoice_model=invoice_model.__name__):
                invoice = invoice_model.objects.create()
                for name in ("X", "A", "Z", "B"):
                    item_model.objects.create(
                        invoice=invoice,
                        product=self.products[name],
                        quantity=1,
                        unit_price=Decimal("10.00"),
                    )

                model_admin = admin.site._registry[invoice_model]
                self.assertEqual(model_admin.inlines[0].ordering, ("pk",))

                with CaptureQueriesContext(connection) as queries:
                    pdf_items = model_admin.get_invoice_items_for_pdf(invoice)

                self.assertEqual(
                    [item["description"] for item in pdf_items],
                    ["Product X", "Product A", "Product Z", "Product B"],
                )
                item_queries = [
                    query["sql"]
                    for query in queries.captured_queries
                    if item_model._meta.db_table in query["sql"]
                ]
                self.assertTrue(item_queries)
                self.assertTrue(
                    any(
                        "ORDER BY" in query.upper() and '"ID" ASC' in query.upper()
                        for query in item_queries
                    )
                )


class CommercialInvoiceStockTests(TestCase):

    def setUp(self):
        CompanySetting.objects.create(
            company_name="Societe Test",
            vat_amount=Decimal("20.00"),
        )
        self.importer = Partner.objects.create(
            description="Client Stock",
            partner_type="importer",
        )
        self.product = Product.objects.create(
            description="Produit Stock",
            part_number="STOCK-001",
            hs_code="9027.80",
            unit_qty=20,
            sale_price=Decimal("15.00"),
        )
        self.other_product = Product.objects.create(
            description="Produit Secondaire",
            hs_code="8414.59",
            unit_qty=12,
            sale_price=Decimal("18.00"),
        )
        self.invoice = CommercialInvoice.objects.create(importer=self.importer)

    def test_create_item_decreases_product_stock(self):
        item = CommercialInvoiceItem.objects.create(
            invoice=self.invoice,
            product=self.product,
            quantity=4,
            unit_price=Decimal("15.00"),
        )

        self.product.refresh_from_db()

        self.assertEqual(self.product.unit_qty, 16)
        self.assertEqual(item.hs_code, "9027.80")
        self.assertEqual(item.part_number, "STOCK-001")

    def test_update_item_quantity_updates_product_stock(self):
        item = CommercialInvoiceItem.objects.create(
            invoice=self.invoice,
            product=self.product,
            quantity=4,
            unit_price=Decimal("15.00"),
        )

        item.quantity = 7
        item.save()

        self.product.refresh_from_db()

        self.assertEqual(self.product.unit_qty, 13)

    def test_change_item_product_restores_old_stock_and_decreases_new_one(self):
        item = CommercialInvoiceItem.objects.create(
            invoice=self.invoice,
            product=self.product,
            quantity=5,
            unit_price=Decimal("15.00"),
        )

        item.product = self.other_product
        item.quantity = 3
        item.unit_price = Decimal("18.00")
        item.save()

        self.product.refresh_from_db()
        self.other_product.refresh_from_db()

        self.assertEqual(self.product.unit_qty, 20)
        self.assertEqual(self.other_product.unit_qty, 9)

    def test_delete_item_restores_product_stock(self):
        item = CommercialInvoiceItem.objects.create(
            invoice=self.invoice,
            product=self.product,
            quantity=6,
            unit_price=Decimal("15.00"),
        )

        item.delete()

        self.product.refresh_from_db()

        self.assertEqual(self.product.unit_qty, 20)

    def test_item_price_change_does_not_update_product_sale_price(self):
        item = CommercialInvoiceItem.objects.create(
            invoice=self.invoice,
            product=self.product,
            quantity=4,
            unit_price=Decimal("15.00"),
        )

        item.unit_price = Decimal("21.00")
        item.save()
        self.product.refresh_from_db()

        self.assertEqual(item.unit_price, Decimal("21.00"))
        self.assertEqual(self.product.sale_price, Decimal("15.00"))
        self.assertEqual(self.product.unit_qty, 16)


@override_settings(ALLOWED_HOSTS=["testserver", "localhost", "127.0.0.1"])
class CommercialInvoiceAdminDraftTests(TestCase):

    def setUp(self):
        CompanySetting.objects.create(
            company_name="Societe Test",
            vat_amount=Decimal("20.00"),
        )
        User = get_user_model()
        self.user = User.objects.create_superuser(
            username="admin",
            email="admin@example.com",
            password="password123",
        )
        self.client.force_login(self.user)

    def test_add_view_creates_empty_commercial_invoice_draft(self):
        response = self.client.get(reverse("admin:invoices_commercialinvoice_add"))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(CommercialInvoice.objects.count(), 1)

        invoice = CommercialInvoice.objects.get()

        self.assertRedirects(
            response,
            reverse("admin:invoices_commercialinvoice_change", args=[invoice.pk]),
        )
        self.assertIsNotNone(invoice.invoice_date)
        self.assertEqual(invoice.vat_percent, Decimal("20.00"))

    def test_draft_add_url_creates_empty_commercial_invoice_draft(self):
        response = self.client.get(reverse("admin:invoices_commercialinvoice_draft_add"))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(CommercialInvoice.objects.count(), 1)

        invoice = CommercialInvoice.objects.get()

        self.assertRedirects(
            response,
            reverse("admin:invoices_commercialinvoice_change", args=[invoice.pk]),
        )

    def test_changelist_uses_draft_add_url_for_commercial_invoice_button(self):
        response = self.client.get(reverse("admin:invoices_commercialinvoice_changelist"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            reverse("admin:invoices_commercialinvoice_draft_add"),
        )

    def test_search_finds_old_proforma_despite_existing_year_filter(self):
        old_invoice = ProformaInvoice.objects.create(
            invoice_number="FR-2024-0002",
            invoice_date="2024-01-04",
        )

        response = self.client.get(
            reverse("admin:invoices_proformainvoice_changelist"),
            {
                "q": "FR-2024-0002",
                "invoice_date__year": "2026",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, old_invoice.invoice_number)
        self.assertNotIn("invoice_date__year", response.request["QUERY_STRING"])

    def test_autosave_creates_new_inline_item_with_product(self):
        importer = Partner.objects.create(
            description="Client Autosave",
            partner_type="importer",
        )
        product = Product.objects.create(
            description="Produit Autosave",
            part_number="AUTO-001",
            hs_code="8403.21",
            unit_qty=20,
            sale_price=Decimal("58.00"),
            purchase_price=Decimal("30.00"),
        )
        invoice = CommercialInvoice.objects.create(importer=importer, vat_percent=Decimal("20.00"))

        response = self.client.post(
            reverse("admin:invoices_commercialinvoice_autosave", args=[invoice.pk]),
            {
                "invoice_date": invoice.invoice_date.strftime("%Y-%m-%d"),
                "importer": str(importer.pk),
                "end_user": "",
                "our_order_no": "",
                "our_reference": "",
                "dispatching_note": "",
                "packing_specification": "",
                "freight": "0.00",
                "discount": "0.00",
                "vat_percent": "20.00",
                "items-TOTAL_FORMS": "1",
                "items-INITIAL_FORMS": "0",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
                "items-0-id": "",
                "items-0-invoice": str(invoice.pk),
                "items-0-product": str(product.pk),
                "items-0-hs_code": product.hs_code,
                "items-0-part_number": product.part_number,
                "items-0-quantity": "5",
                "items-0-unit_price": "58.00",
                "packing_entries-TOTAL_FORMS": "0",
                "packing_entries-INITIAL_FORMS": "0",
                "packing_entries-MIN_NUM_FORMS": "0",
                "packing_entries-MAX_NUM_FORMS": "1000",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200, response.content)
        item = invoice.items.get()
        self.assertEqual(item.product, product)
        self.assertEqual(item.quantity, 5)
        self.assertEqual(item.unit_price, Decimal("58.00"))
        self.assertEqual(response.json()["inline_objects"][0]["id"], str(item.pk))

    def test_invalid_autosave_does_not_partially_change_invoice(self):
        invoice = CommercialInvoice.objects.create(
            price_for="Original price condition",
            vat_percent=Decimal("20.00"),
        )

        response = self.client.post(
            reverse("admin:invoices_commercialinvoice_autosave", args=[invoice.pk]),
            {
                "invoice_date": "",
                "importer": "",
                "end_user": "",
                "our_order_no": "",
                "our_reference": "",
                "price_for": "Unsaved invalid change",
                "dispatching_note": "",
                "packing_specification": "",
                "delivery_time": "",
                "terms_conditions": "",
                "freight": "0.00",
                "discount": "0.00",
                "vat_percent": "20.00",
                "items-TOTAL_FORMS": "0",
                "items-INITIAL_FORMS": "0",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
                "packing_entries-TOTAL_FORMS": "0",
                "packing_entries-INITIAL_FORMS": "0",
                "packing_entries-MIN_NUM_FORMS": "0",
                "packing_entries-MAX_NUM_FORMS": "1000",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 400)
        invoice.refresh_from_db()
        self.assertEqual(invoice.price_for, "Original price condition")

    def test_autosave_keeps_manual_hs_code_on_invoice_without_updating_product(self):
        product = Product.objects.create(
            description="Produit sans HS Code",
            part_number="NO-HS-001",
            hs_code="",
            unit_qty=20,
            sale_price=Decimal("58.00"),
        )
        invoice = CommercialInvoice.objects.create(vat_percent=Decimal("20.00"))

        response = self.client.post(
            reverse("admin:invoices_commercialinvoice_autosave", args=[invoice.pk]),
            {
                "invoice_date": invoice.invoice_date.strftime("%Y-%m-%d"),
                "importer": "",
                "end_user": "",
                "our_order_no": "",
                "our_reference": "",
                "price_for": "",
                "dispatching_note": "",
                "packing_specification": "",
                "delivery_time": "",
                "terms_conditions": "",
                "freight": "0.00",
                "discount": "0.00",
                "vat_percent": "20.00",
                "items-TOTAL_FORMS": "1",
                "items-INITIAL_FORMS": "0",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
                "items-0-id": "",
                "items-0-invoice": str(invoice.pk),
                "items-0-product": str(product.pk),
                "items-0-hs_code": "CUSTOM-8481.80",
                "items-0-part_number": product.part_number,
                "items-0-quantity": "1",
                "items-0-unit_price": "58.00",
                "packing_entries-TOTAL_FORMS": "0",
                "packing_entries-INITIAL_FORMS": "0",
                "packing_entries-MIN_NUM_FORMS": "0",
                "packing_entries-MAX_NUM_FORMS": "1000",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(invoice.items.get().hs_code, "CUSTOM-8481.80")
        product.refresh_from_db()
        self.assertEqual(product.hs_code, "")

    def test_autosave_keeps_new_inline_item_when_only_quantity_changed(self):
        invoice = CommercialInvoice.objects.create(vat_percent=Decimal("20.00"))

        response = self.client.post(
            reverse("admin:invoices_commercialinvoice_autosave", args=[invoice.pk]),
            {
                "invoice_date": invoice.invoice_date.strftime("%Y-%m-%d"),
                "importer": "",
                "end_user": "",
                "our_order_no": "",
                "our_reference": "",
                "price_for": "",
                "dispatching_note": "",
                "packing_specification": "",
                "delivery_time": "",
                "terms_conditions": "",
                "freight": "0.00",
                "discount": "0.00",
                "vat_percent": "20.00",
                "items-TOTAL_FORMS": "1",
                "items-INITIAL_FORMS": "0",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
                "items-0-id": "",
                "items-0-invoice": str(invoice.pk),
                "items-0-product": "",
                "items-0-hs_code": "",
                "items-0-part_number": "",
                "items-0-quantity": "3",
                "items-0-unit_price": "0.00",
                "packing_entries-TOTAL_FORMS": "0",
                "packing_entries-INITIAL_FORMS": "0",
                "packing_entries-MIN_NUM_FORMS": "0",
                "packing_entries-MAX_NUM_FORMS": "1000",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200, response.content)
        item = invoice.items.get()
        self.assertEqual(item.quantity, 3)
        self.assertEqual(item.product, None)
        self.assertEqual(response.json()["inline_objects"][0]["id"], str(item.pk))

    def test_autosave_updates_existing_item_price_without_updating_product_default_price(self):
        importer = Partner.objects.create(
            description="Client Autosave Price",
            partner_type="importer",
        )
        product = Product.objects.create(
            description="Produit Prix Autosave",
            part_number="PRICE-001",
            hs_code="8403.21",
            unit_qty=20,
            sale_price=Decimal("58.00"),
            purchase_price=Decimal("30.00"),
        )
        invoice = CommercialInvoice.objects.create(importer=importer, vat_percent=Decimal("20.00"))
        item = CommercialInvoiceItem.objects.create(
            invoice=invoice,
            product=product,
            hs_code=product.hs_code,
            part_number=product.part_number,
            quantity=5,
            unit_price=Decimal("58.00"),
        )

        response = self.client.post(
            reverse("admin:invoices_commercialinvoice_autosave", args=[invoice.pk]),
            {
                "invoice_date": invoice.invoice_date.strftime("%Y-%m-%d"),
                "importer": str(importer.pk),
                "end_user": "",
                "our_order_no": "",
                "our_reference": "",
                "dispatching_note": "",
                "packing_specification": "",
                "freight": "0.00",
                "discount": "0.00",
                "vat_percent": "20.00",
                "items-TOTAL_FORMS": "1",
                "items-INITIAL_FORMS": "1",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
                "items-0-id": str(item.pk),
                "items-0-invoice": str(invoice.pk),
                "items-0-product": str(product.pk),
                "items-0-hs_code": product.hs_code,
                "items-0-part_number": product.part_number,
                "items-0-quantity": "5",
                "items-0-unit_price": "72.50",
                "packing_entries-TOTAL_FORMS": "0",
                "packing_entries-INITIAL_FORMS": "0",
                "packing_entries-MIN_NUM_FORMS": "0",
                "packing_entries-MAX_NUM_FORMS": "1000",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        item.refresh_from_db()
        product.refresh_from_db()

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(item.unit_price, Decimal("72.50"))
        self.assertEqual(product.sale_price, Decimal("58.00"))

    def test_commercial_autosave_deletes_only_checked_item(self):
        product_one = Product.objects.create(
            description="Produit Delete One",
            part_number="DEL-COM-001",
            hs_code="8504.40",
            unit_qty=20,
            sale_price=Decimal("10.00"),
        )
        product_two = Product.objects.create(
            description="Produit Keep One",
            part_number="KEEP-COM-001",
            hs_code="8504.50",
            unit_qty=20,
            sale_price=Decimal("12.00"),
        )
        invoice = CommercialInvoice.objects.create(vat_percent=Decimal("20.00"))
        item_delete = CommercialInvoiceItem.objects.create(
            invoice=invoice,
            product=product_one,
            hs_code=product_one.hs_code,
            part_number=product_one.part_number,
            quantity=1,
            unit_price=Decimal("10.00"),
        )
        item_keep = CommercialInvoiceItem.objects.create(
            invoice=invoice,
            product=product_two,
            hs_code=product_two.hs_code,
            part_number=product_two.part_number,
            quantity=2,
            unit_price=Decimal("12.00"),
        )

        response = self.client.post(
            reverse("admin:invoices_commercialinvoice_autosave", args=[invoice.pk]),
            {
                "invoice_date": invoice.invoice_date.strftime("%Y-%m-%d"),
                "importer": "",
                "end_user": "",
                "our_order_no": "",
                "our_reference": "",
                "dispatching_note": "",
                "packing_specification": "",
                "freight": "0.00",
                "discount": "0.00",
                "vat_percent": "20.00",
                "items-TOTAL_FORMS": "2",
                "items-INITIAL_FORMS": "2",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
                "items-0-id": str(item_delete.pk),
                "items-0-invoice": str(invoice.pk),
                "items-0-product": str(product_one.pk),
                "items-0-hs_code": product_one.hs_code,
                "items-0-part_number": product_one.part_number,
                "items-0-quantity": "1",
                "items-0-unit_price": "10.00",
                "items-0-DELETE": "on",
                "items-1-id": str(item_keep.pk),
                "items-1-invoice": str(invoice.pk),
                "items-1-product": str(product_two.pk),
                "items-1-hs_code": product_two.hs_code,
                "items-1-part_number": product_two.part_number,
                "items-1-quantity": "2",
                "items-1-unit_price": "12.00",
                "packing_entries-TOTAL_FORMS": "0",
                "packing_entries-INITIAL_FORMS": "0",
                "packing_entries-MIN_NUM_FORMS": "0",
                "packing_entries-MAX_NUM_FORMS": "1000",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(CommercialInvoiceItem.objects.filter(pk=item_delete.pk).exists())
        self.assertTrue(CommercialInvoiceItem.objects.filter(pk=item_keep.pk).exists())

    def test_proforma_autosave_deletes_only_checked_item(self):
        product_one = Product.objects.create(
            description="Produit Delete Proforma",
            part_number="DEL-PRO-001",
            hs_code="8504.40",
            sale_price=Decimal("10.00"),
        )
        product_two = Product.objects.create(
            description="Produit Keep Proforma",
            part_number="KEEP-PRO-001",
            hs_code="8504.50",
            sale_price=Decimal("12.00"),
        )
        proforma = ProformaInvoice.objects.create(vat_percent=Decimal("20.00"))
        item_delete = ProformaInvoiceItem.objects.create(
            invoice=proforma,
            product=product_one,
            hs_code=product_one.hs_code,
            part_number=product_one.part_number,
            quantity=1,
            unit_price=Decimal("10.00"),
        )
        item_keep = ProformaInvoiceItem.objects.create(
            invoice=proforma,
            product=product_two,
            hs_code=product_two.hs_code,
            part_number=product_two.part_number,
            quantity=2,
            unit_price=Decimal("12.00"),
        )

        response = self.client.post(
            reverse("admin:invoices_proformainvoice_autosave", args=[proforma.pk]),
            {
                "invoice_date": proforma.invoice_date.strftime("%Y-%m-%d"),
                "importer": "",
                "end_user": "",
                "our_reference": "",
                "price_for": "",
                "freight": "0.00",
                "discount": "0.00",
                "vat_percent": "20.00",
                "items-TOTAL_FORMS": "2",
                "items-INITIAL_FORMS": "2",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
                "items-0-id": str(item_delete.pk),
                "items-0-invoice": str(proforma.pk),
                "items-0-product": str(product_one.pk),
                "items-0-hs_code": product_one.hs_code,
                "items-0-part_number": product_one.part_number,
                "items-0-quantity": "1",
                "items-0-unit_price": "10.00",
                "items-0-DELETE": "on",
                "items-1-id": str(item_keep.pk),
                "items-1-invoice": str(proforma.pk),
                "items-1-product": str(product_two.pk),
                "items-1-hs_code": product_two.hs_code,
                "items-1-part_number": product_two.part_number,
                "items-1-quantity": "2",
                "items-1-unit_price": "12.00",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(ProformaInvoiceItem.objects.filter(pk=item_delete.pk).exists())
        self.assertTrue(ProformaInvoiceItem.objects.filter(pk=item_keep.pk).exists())

    def test_proforma_save_and_add_another_redirects_to_new_proforma_form(self):
        proforma = ProformaInvoice.objects.create(vat_percent=Decimal("20.00"))

        response = self.client.post(
            reverse("admin:invoices_proformainvoice_change", args=[proforma.pk]),
            {
                "invoice_date": proforma.invoice_date.strftime("%Y-%m-%d"),
                "importer": "",
                "end_user": "",
                "our_reference": "",
                "price_for": "",
                "freight": "0.00",
                "discount": "0.00",
                "vat_percent": "20.00",
                "items-TOTAL_FORMS": "0",
                "items-INITIAL_FORMS": "0",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
                "_addanother": "Save and add another",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("admin:invoices_proformainvoice_add"))

    def test_commercial_save_and_add_another_redirects_to_new_commercial_form(self):
        invoice = CommercialInvoice.objects.create(vat_percent=Decimal("20.00"))

        response = self.client.post(
            reverse("admin:invoices_commercialinvoice_change", args=[invoice.pk]),
            {
                "invoice_date": invoice.invoice_date.strftime("%Y-%m-%d"),
                "importer": "",
                "end_user": "",
                "our_order_no": "",
                "our_reference": "",
                "dispatching_note": "",
                "packing_specification": "",
                "freight": "0.00",
                "discount": "0.00",
                "vat_percent": "20.00",
                "items-TOTAL_FORMS": "0",
                "items-INITIAL_FORMS": "0",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
                "packing_entries-TOTAL_FORMS": "0",
                "packing_entries-INITIAL_FORMS": "0",
                "packing_entries-MIN_NUM_FORMS": "0",
                "packing_entries-MAX_NUM_FORMS": "1000",
                "_addanother": "Save and add another",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("admin:invoices_commercialinvoice_add"))

    def test_save_and_pdf_saves_new_inline_item_then_redirects_to_pdf(self):
        product = Product.objects.create(
            description="Produit PDF Save",
            part_number="PDF-SAVE-001",
            hs_code="8403.21",
            unit_qty=20,
            sale_price=Decimal("58.00"),
            purchase_price=Decimal("30.00"),
        )
        invoice = CommercialInvoice.objects.create(vat_percent=Decimal("20.00"))
        pdf_url = reverse("admin:invoices_commercialinvoice_pdf", args=[invoice.pk])

        response = self.client.post(
            reverse("admin:invoices_commercialinvoice_change", args=[invoice.pk]),
            {
                "invoice_date": invoice.invoice_date.strftime("%Y-%m-%d"),
                "importer": "",
                "end_user": "",
                "our_order_no": "",
                "our_reference": "",
                "dispatching_note": "",
                "packing_specification": "",
                "freight": "0.00",
                "discount": "0.00",
                "vat_percent": "20.00",
                "items-TOTAL_FORMS": "1",
                "items-INITIAL_FORMS": "0",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
                "items-0-id": "",
                "items-0-invoice": str(invoice.pk),
                "items-0-product": str(product.pk),
                "items-0-hs_code": product.hs_code,
                "items-0-part_number": product.part_number,
                "items-0-quantity": "2",
                "items-0-unit_price": "58.00",
                "packing_entries-TOTAL_FORMS": "0",
                "packing_entries-INITIAL_FORMS": "0",
                "packing_entries-MIN_NUM_FORMS": "0",
                "packing_entries-MAX_NUM_FORMS": "1000",
                "_save_and_pdf": "1",
                "_save_and_pdf_url": pdf_url,
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], pdf_url)
        self.assertEqual(invoice.items.count(), 1)

    def test_commercial_pdf_buttons_keep_admin_session_authenticated(self):
        invoice = CommercialInvoice.objects.create(vat_percent=Decimal("20.00"))
        pdf_urls = [
            reverse("admin:invoices_commercialinvoice_pdf", args=[invoice.pk]),
            reverse("admin:invoices_commercialinvoice_packing_list_pdf", args=[invoice.pk]),
            reverse("admin:invoices_commercialinvoice_dispatching_note_pdf", args=[invoice.pk]),
        ]

        for pdf_url in pdf_urls:
            response = self.client.get(pdf_url)

            self.assertEqual(
                response.status_code,
                200,
                response.content.decode("utf-8", errors="replace"),
            )
            self.assertEqual(response["Content-Type"], "application/pdf")
            self.assertNotIn("/admin/login/", response.get("Location", ""))
            self.assertEqual(
                str(self.client.session.get("_auth_user_id")),
                str(self.user.pk),
            )

    def test_commercial_pdf_has_safe_download_filename(self):
        invoice = CommercialInvoice.objects.create(
            invoice_number="CI-2026-0015",
            vat_percent=Decimal("20.00"),
        )
        CommercialInvoice.objects.filter(pk=invoice.pk).update(
            invoice_number="CI/2026/0015"
        )

        response = self.client.get(
            reverse("admin:invoices_commercialinvoice_pdf", args=[invoice.pk])
        )

        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertEqual(
            response["Content-Disposition"],
            'inline; filename="Commercial-Invoice-CI-2026-0015.pdf"',
        )
        self.assertTrue(response.content.startswith(b"%PDF-"))
        self.assertGreater(len(response.content), 100)

    def test_proforma_pdf_has_safe_download_filename(self):
        invoice = ProformaInvoice.objects.create(
            invoice_number="PR-2026-0005",
            vat_percent=Decimal("20.00"),
        )

        response = self.client.get(
            reverse("admin:invoices_proformainvoice_pdf", args=[invoice.pk])
        )

        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertEqual(
            response["Content-Disposition"],
            'inline; filename="Proforma-Invoice-PR-2026-0005.pdf"',
        )
        self.assertTrue(response.content.startswith(b"%PDF-"))
        self.assertGreater(len(response.content), 100)

    def test_save_button_ignores_stale_save_and_pdf_fields(self):
        invoice = CommercialInvoice.objects.create(vat_percent=Decimal("20.00"))
        pdf_url = reverse("admin:invoices_commercialinvoice_pdf", args=[invoice.pk])

        response = self.client.post(
            reverse("admin:invoices_commercialinvoice_change", args=[invoice.pk]),
            {
                "invoice_date": invoice.invoice_date.strftime("%Y-%m-%d"),
                "importer": "",
                "end_user": "",
                "our_order_no": "",
                "our_reference": "",
                "dispatching_note": "",
                "packing_specification": "",
                "freight": "0.00",
                "discount": "0.00",
                "vat_percent": "20.00",
                "items-TOTAL_FORMS": "0",
                "items-INITIAL_FORMS": "0",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
                "packing_entries-TOTAL_FORMS": "0",
                "packing_entries-INITIAL_FORMS": "0",
                "packing_entries-MIN_NUM_FORMS": "0",
                "packing_entries-MAX_NUM_FORMS": "1000",
                "_save": "Save",
                "_save_and_pdf": "1",
                "_save_and_pdf_url": pdf_url,
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertNotEqual(response["Location"], pdf_url)
        self.assertEqual(response["Location"], reverse("admin:invoices_commercialinvoice_changelist"))

    def test_proforma_form_shows_add_another_without_save_and_continue(self):
        proforma = ProformaInvoice.objects.create(vat_percent=Decimal("20.00"))

        response = self.client.get(
            reverse("admin:invoices_proformainvoice_change", args=[proforma.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="_addanother"')
        self.assertNotContains(response, 'name="_continue"')
        self.assertNotContains(response, "Save and continue editing")

    def test_commercial_form_shows_add_another_without_save_and_continue(self):
        invoice = CommercialInvoice.objects.create(vat_percent=Decimal("20.00"))

        response = self.client.get(
            reverse("admin:invoices_commercialinvoice_change", args=[invoice.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="_addanother"')
        self.assertNotContains(response, 'name="_continue"')
        self.assertNotContains(response, "Save and continue editing")


class PartnerAutocompleteWidgetTests(TestCase):

    def test_invoice_partner_autocompletes_have_targeted_large_class(self):
        model_admin = admin.site._registry[ProformaInvoice]

        for field_name in ("importer", "end_user"):
            formfield = model_admin.formfield_for_foreignkey(
                ProformaInvoice._meta.get_field(field_name),
                request=None,
            )
            self.assertIn(
                "partner-large-select",
                formfield.widget.attrs.get("class", "").split(),
            )

        self.assertIn(
            "admin/css/large_partner_autocomplete.css",
            model_admin.media._css["all"],
        )
