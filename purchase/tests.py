from decimal import Decimal

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

from company.models import CompanySetting
from products.models import Product
from purchase.admin import PurchaseOrderAdminForm
from purchase.models import PurchaseOrder, PurchaseOrderItem


class PurchaseOrderCreatorAuditTests(TestCase):

    def setUp(self):
        self.creator = get_user_model().objects.create_superuser(
            username="purchase-creator",
            password="password123",
            email="purchase-creator@example.com",
        )
        self.other_user = get_user_model().objects.create_superuser(
            username="purchase-editor",
            password="password123",
            email="purchase-editor@example.com",
        )

    def test_purchase_draft_stores_creator_and_admin_displays_it(self):
        self.client.force_login(self.creator)

        response = self.client.get(reverse("admin:purchase_purchaseorder_add"))

        self.assertEqual(response.status_code, 302)
        purchase_order = PurchaseOrder.objects.latest("pk")
        self.assertEqual(purchase_order.created_by, self.creator)

        response = self.client.get(
            reverse("admin:purchase_purchaseorder_change", args=[purchase_order.pk])
        )
        self.assertContains(response, self.creator.get_username())

    def test_creator_is_not_replaced_when_purchase_order_is_edited(self):
        purchase_order = PurchaseOrder.objects.create(created_by=self.creator)
        model_admin = admin.site._registry[PurchaseOrder]
        request = type("Request", (), {"user": self.other_user})()

        model_admin.save_model(request, purchase_order, form=None, change=True)
        purchase_order.refresh_from_db()

        self.assertEqual(purchase_order.created_by, self.creator)


class StaffPurchaseOrderScopeTests(TestCase):

    def setUp(self):
        group = Group.objects.create(name="Staff")
        group.permissions.add(
            *Permission.objects.filter(
                content_type__app_label="purchase",
                codename__in=(
                    "view_purchaseorder",
                    "add_purchaseorder",
                    "change_purchaseorder",
                ),
            )
        )
        self.user = get_user_model().objects.create_user(
            username="scoped-purchase-staff",
            password="password123",
            is_active=True,
            is_staff=True,
        )
        self.user.groups.add(group)
        self.client.force_login(self.user)

    def test_staff_sees_only_own_purchase_orders(self):
        own = PurchaseOrder.objects.create(created_by=self.user)
        other = PurchaseOrder.objects.create()

        response = self.client.get(
            reverse("admin:purchase_purchaseorder_changelist")
        )

        self.assertContains(response, own.purchase_number)
        self.assertNotContains(response, other.purchase_number)
        self.assertEqual(
            self.client.get(
                reverse("admin:purchase_purchaseorder_change", args=[other.pk])
            ).status_code,
            403,
        )

    def test_staff_cannot_open_purchase_reports(self):
        self.assertEqual(
            self.client.get(reverse("purchase_home")).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(reverse("purchase_report_filter")).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(reverse("purchase_report_result")).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(reverse("admin:purchase_report")).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(reverse("admin:purchase_report_pdf")).status_code,
            403,
        )

    def test_global_document_permission_reveals_all_purchase_orders(self):
        other = PurchaseOrder.objects.create()
        self.user.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="invoices",
                codename="view_all_documents",
            )
        )
        self.user = get_user_model().objects.get(pk=self.user.pk)
        self.client.force_login(self.user)

        response = self.client.get(
            reverse("admin:purchase_purchaseorder_changelist")
        )

        self.assertContains(response, other.purchase_number)

    def test_report_permission_allows_reports_but_keeps_document_scope(self):
        own = PurchaseOrder.objects.create(created_by=self.user)
        other = PurchaseOrder.objects.create()
        self.user.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="purchase",
                codename="view_purchase_order_reports",
            )
        )
        self.user = get_user_model().objects.get(pk=self.user.pk)
        self.client.force_login(self.user)

        admin_response = self.client.get(reverse("admin:purchase_report"))
        public_response = self.client.get(reverse("purchase_report_result"))

        self.assertEqual(admin_response.status_code, 200)
        self.assertContains(admin_response, own.purchase_number)
        self.assertNotContains(admin_response, other.purchase_number)
        self.assertEqual(public_response.status_code, 200)
        self.assertContains(public_response, own.purchase_number)
        self.assertNotContains(public_response, other.purchase_number)

class ProductInfoViewTests(TestCase):

    def test_product_info_returns_invoice_and_purchase_fields(self):
        user = get_user_model().objects.create_user(
            username="staff",
            password="password123",
            is_staff=True,
        )
        product = Product.objects.create(
            description="Produit API",
            part_number="API-001",
            hs_code="8471.30",
            note="Note produit",
            unit_qty=12,
            purchase_price=Decimal("5.50"),
            sale_price=Decimal("9.90"),
        )

        anonymous_response = self.client.get(reverse("product_info", args=[product.id]))
        self.assertEqual(anonymous_response.status_code, 302)

        self.client.force_login(user)
        response = self.client.get(reverse("product_info", args=[product.id]))

        self.assertEqual(response.status_code, 200)
        self.assertIn("no-cache", response["Cache-Control"])
        self.assertEqual(
            response.json(),
            {
                "description": "Produit API",
                "part_number": "API-001",
                "hs_code": "8471.30",
                "note": "Note produit",
                "unit_qty": "12",
                "sale_price": "9.90",
                "purchase_price": "5.50",
            },
        )

    def test_product_info_returns_updated_hs_code(self):
        user = get_user_model().objects.create_user(
            username="fresh-hs-code-staff", password="password123", is_staff=True
        )
        product = Product.objects.create(description="Produit actualise", hs_code="85444200")
        product.hs_code = "85444290"
        product.save(update_fields=["hs_code"])

        self.client.force_login(user)
        response = self.client.get(reverse("product_info", args=[product.pk]))

        self.assertEqual(response.json()["hs_code"], "85444290")


class PurchaseOrderAdminFormTests(TestCase):

    def test_partner_autocompletes_have_targeted_large_class(self):
        model_admin = admin.site._registry[PurchaseOrder]

        for field_name in ("seller", "requester"):
            formfield = model_admin.formfield_for_foreignkey(
                PurchaseOrder._meta.get_field(field_name),
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

    def test_blank_purchase_financial_fields_are_saved_as_zero(self):
        CompanySetting.objects.create(
            company_name="Societe Zero",
            vat_amount=Decimal("20.00"),
        )
        form = PurchaseOrderAdminForm(
            data={
                "purchase_number": "",
                "purchase_date": "2026-07-20",
                "seller": "",
                "requester": "",
                "freight": "",
                "vat_percent": "",
            }
        )

        self.assertTrue(form.is_valid(), form.errors)
        purchase_order = form.save()
        self.assertEqual(purchase_order.freight, Decimal("0.00"))
        self.assertEqual(purchase_order.vat_percent, Decimal("20.00"))

    def test_vat_percent_uses_company_setting_as_initial_value(self):
        CompanySetting.objects.create(
            company_name="Societe Test",
            vat_amount=Decimal("20.00"),
        )

        form = PurchaseOrderAdminForm()

        self.assertEqual(form.fields["vat_percent"].initial, Decimal("20.00"))

    def test_purchase_order_admin_loads_autosave_script(self):
        template_source = get_template("admin/purchase/purchaseorder/change_form.html").template.source

        self.assertIn("admin/js/invoice_autosave.js", template_source)
        self.assertIn("20260730-unblock-save", template_source)
        self.assertIn("20260803-hs-code-refresh", template_source)
        self.assertIn("20260803-native-formset", template_source)
        self.assertIn("window.invoiceAutosaveNow", template_source)
        self.assertNotIn("fetch(form.action", template_source)
        self.assertIn('target="_blank"', template_source)
        self.assertNotIn(" download", template_source)

    def test_product_autofill_maps_current_hs_code_when_product_changes(self):
        with open(finders.find("admin/js/product_autofill.js"), encoding="utf-8") as script_file:
            script = script_file.read()

        self.assertIn("productChanged || !hsCodeInput.value.trim()", script)
        self.assertIn('hsCodeInput.value = data.hs_code || "";', script)
        self.assertIn('new Event("input", { bubbles: true })', script)
        self.assertIn('new Event("change", { bubbles: true })', script)


class PurchaseOrderItemTests(TestCase):

    def test_purchase_number_uses_company_setting_year(self):
        CompanySetting.objects.create(
            company_name="Year Test",
            year=2031,
        )

        purchase_order = PurchaseOrder.objects.create()

        self.assertEqual(purchase_order.purchase_number, "PO/2031-0001")

    def test_exact_purchase_number_search_excludes_other_field_matches(self):
        exact_order = PurchaseOrder.objects.create(
            purchase_number="PO/2024-0002",
            purchase_date="2024-01-04",
        )
        PurchaseOrder.objects.create(
            purchase_number="PO/2024-0005",
            purchase_date="2024-01-05",
            shipment="PO/2024-0002",
        )
        model_admin = admin.site._registry[PurchaseOrder]

        results, may_have_duplicates = model_admin.get_search_results(
            None,
            PurchaseOrder.objects.filter(purchase_date__year=2026),
            "PO/2024-0002",
        )

        self.assertEqual(list(results), [exact_order])
        self.assertFalse(may_have_duplicates)

    def test_item_uses_product_hs_code_and_part_number_by_default(self):
        product = Product.objects.create(
            description="Produit PO",
            part_number="PO-001",
            hs_code="8504.40",
            purchase_price=Decimal("12.50"),
        )
        purchase_order = PurchaseOrder.objects.create()

        item = PurchaseOrderItem.objects.create(
            purchase_order=purchase_order,
            product=product,
            quantity=2,
        )

        self.assertEqual(item.description, "Produit PO")
        self.assertEqual(item.part_number, "PO-001")
        self.assertEqual(item.hs_code, "8504.40")
        self.assertEqual(item.unit_price, Decimal("12.50"))

    def test_item_price_change_does_not_update_product_purchase_price(self):
        product = Product.objects.create(
            description="Produit Prix PO",
            part_number="PO-PRICE-001",
            hs_code="8504.40",
            purchase_price=Decimal("12.50"),
        )
        purchase_order = PurchaseOrder.objects.create()
        item = PurchaseOrderItem.objects.create(
            purchase_order=purchase_order,
            product=product,
            quantity=2,
            unit_price=Decimal("12.50"),
        )

        item.unit_price = Decimal("18.75")
        item.save()
        product.refresh_from_db()

        self.assertEqual(item.unit_price, Decimal("18.75"))
        self.assertEqual(product.purchase_price, Decimal("12.50"))


class PurchaseOrderItemPdfOrderingTests(TestCase):

    def test_purchase_admin_and_pdf_items_use_line_pk_order(self):
        CompanySetting.objects.create(
            company_name="Societe Ordering",
            vat_amount=Decimal("20.00"),
        )
        products = {
            name: Product.objects.create(
                description=f"Product {name}",
                part_number=f"ORDER-{name}",
                purchase_price=Decimal("10.00"),
            )
            for name in ("A", "B", "X", "Z")
        }
        purchase_order = PurchaseOrder.objects.create()
        for name in ("X", "A", "Z", "B"):
            PurchaseOrderItem.objects.create(
                purchase_order=purchase_order,
                product=products[name],
                quantity=1,
                unit_price=Decimal("10.00"),
            )

        model_admin = admin.site._registry[PurchaseOrder]
        self.assertEqual(model_admin.inlines[0].ordering, ("pk",))

        with CaptureQueriesContext(connection) as queries:
            pdf_items = model_admin.get_purchase_items_for_pdf(purchase_order)

        self.assertEqual(
            [item["description"] for item in pdf_items],
            ["Product X", "Product A", "Product Z", "Product B"],
        )
        item_queries = [
            query["sql"]
            for query in queries.captured_queries
            if PurchaseOrderItem._meta.db_table in query["sql"]
        ]
        self.assertTrue(item_queries)
        self.assertTrue(
            any(
                "ORDER BY" in query.upper() and '"ID" ASC' in query.upper()
                for query in item_queries
            )
        )


@override_settings(ALLOWED_HOSTS=["testserver", "localhost", "127.0.0.1"])
class PurchaseOrderAdminAutosaveTests(TestCase):

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

    def test_purchase_pdf_keeps_admin_session_authenticated(self):
        purchase_order = PurchaseOrder.objects.create(
            purchase_number="PO/2026/0012",
            vat_percent=Decimal("20.00"),
        )

        response = self.client.get(
            reverse("admin:purchase_purchaseorder_pdf", args=[purchase_order.pk])
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertEqual(
            response["Content-Disposition"],
            'inline; filename="Purchase-Order-PO-2026-0012.pdf"',
        )
        self.assertTrue(response.content.startswith(b"%PDF-"))
        self.assertGreater(len(response.content), 100)
        self.assertIn("_auth_user_id", self.client.session)

    def test_search_finds_purchase_order_outside_company_setting_year(self):
        company = CompanySetting.objects.get()
        company.year = 2026
        company.save()
        old_order = PurchaseOrder.objects.create(
            purchase_number="PO/2024-0002",
            purchase_date="2024-01-04",
        )

        response = self.client.get(
            reverse("admin:purchase_purchaseorder_changelist"),
            {
                "q": "PO/2024-0002",
                "purchase_date__year": "2026",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, old_order.purchase_number)
        self.assertNotIn("purchase_date__year", response.request["QUERY_STRING"])

    def test_invalid_autosave_does_not_partially_change_purchase_order(self):
        purchase_order = PurchaseOrder.objects.create(
            shipment="Original shipment",
            vat_percent=Decimal("20.00"),
        )

        response = self.client.post(
            reverse("admin:purchase_purchaseorder_autosave", args=[purchase_order.pk]),
            {
                "purchase_date": "",
                "seller": "",
                "requester": "",
                "sent_by": "",
                "shipment": "Unsaved invalid change",
                "freight": "0.00",
                "vat_percent": "20.00",
                "sales_condition": "",
                "payment_condition": "",
                "delivery_terms": "",
                "items-TOTAL_FORMS": "0",
                "items-INITIAL_FORMS": "0",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 400)
        purchase_order.refresh_from_db()
        self.assertEqual(purchase_order.shipment, "Original shipment")

    def test_autosave_updates_existing_item_price_without_updating_product_default_price(self):
        product = Product.objects.create(
            description="Produit Prix PO Autosave",
            part_number="PO-AUTO-PRICE",
            hs_code="8504.40",
            purchase_price=Decimal("12.50"),
            sale_price=Decimal("20.00"),
        )
        purchase_order = PurchaseOrder.objects.create(vat_percent=Decimal("20.00"))
        item = PurchaseOrderItem.objects.create(
            purchase_order=purchase_order,
            product=product,
            description=product.description,
            part_number=product.part_number,
            hs_code=product.hs_code,
            quantity=2,
            unit_price=Decimal("12.50"),
        )

        response = self.client.post(
            reverse("admin:purchase_purchaseorder_autosave", args=[purchase_order.pk]),
            {
                "purchase_number": purchase_order.purchase_number,
                "purchase_date": purchase_order.purchase_date.strftime("%Y-%m-%d"),
                "seller": "",
                "requester": "",
                "sent_by": "",
                "shipment": "",
                "freight": "0.00",
                "vat_percent": "20.00",
                "sales_condition": "",
                "payment_condition": "",
                "delivery_terms": "",
                "items-TOTAL_FORMS": "1",
                "items-INITIAL_FORMS": "1",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
                "items-0-id": str(item.pk),
                "items-0-purchase_order": str(purchase_order.pk),
                "items-0-product": str(product.pk),
                "items-0-hs_code": product.hs_code,
                "items-0-part_number": product.part_number,
                "items-0-quantity": "2",
                "items-0-unit_price": "18.75",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        item.refresh_from_db()
        product.refresh_from_db()

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(item.unit_price, Decimal("18.75"))
        self.assertEqual(product.purchase_price, Decimal("12.50"))

    def test_autosave_creates_new_inline_item_with_product(self):
        product = Product.objects.create(
            description="Produit Nouveau PO Autosave",
            part_number="PO-AUTO-NEW",
            hs_code="8504.40",
            purchase_price=Decimal("12.50"),
            sale_price=Decimal("20.00"),
        )
        purchase_order = PurchaseOrder.objects.create(vat_percent=Decimal("20.00"))

        response = self.client.post(
            reverse("admin:purchase_purchaseorder_autosave", args=[purchase_order.pk]),
            {
                "purchase_number": purchase_order.purchase_number,
                "purchase_date": purchase_order.purchase_date.strftime("%Y-%m-%d"),
                "seller": "",
                "requester": "",
                "sent_by": "",
                "shipment": "",
                "freight": "0.00",
                "vat_percent": "20.00",
                "sales_condition": "",
                "payment_condition": "",
                "delivery_terms": "",
                "items-TOTAL_FORMS": "1",
                "items-INITIAL_FORMS": "0",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
                "items-0-id": "",
                "items-0-purchase_order": str(purchase_order.pk),
                "items-0-product": str(product.pk),
                "items-0-hs_code": product.hs_code,
                "items-0-part_number": product.part_number,
                "items-0-quantity": "2",
                "items-0-unit_price": "12.50",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200, response.content)
        item = purchase_order.items.get()
        self.assertEqual(item.product, product)
        self.assertEqual(item.quantity, 2)
        self.assertEqual(item.unit_price, Decimal("12.50"))
        self.assertEqual(response.json()["inline_objects"][0]["id"], str(item.pk))

    def test_autosave_keeps_new_inline_item_when_only_quantity_changed(self):
        purchase_order = PurchaseOrder.objects.create(vat_percent=Decimal("20.00"))

        response = self.client.post(
            reverse("admin:purchase_purchaseorder_autosave", args=[purchase_order.pk]),
            {
                "purchase_number": purchase_order.purchase_number,
                "purchase_date": purchase_order.purchase_date.strftime("%Y-%m-%d"),
                "seller": "",
                "requester": "",
                "sent_by": "",
                "shipment": "",
                "freight": "0.00",
                "vat_percent": "20.00",
                "sales_condition": "",
                "payment_condition": "",
                "delivery_terms": "",
                "items-TOTAL_FORMS": "1",
                "items-INITIAL_FORMS": "0",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
                "items-0-id": "",
                "items-0-purchase_order": str(purchase_order.pk),
                "items-0-product": "",
                "items-0-hs_code": "",
                "items-0-part_number": "",
                "items-0-quantity": "4",
                "items-0-unit_price": "0.00",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200, response.content)
        item = purchase_order.items.get()
        self.assertEqual(item.quantity, 4)
        self.assertEqual(item.product, None)
        self.assertEqual(response.json()["inline_objects"][0]["id"], str(item.pk))

    def test_autosave_deletes_only_checked_item(self):
        product_one = Product.objects.create(
            description="Produit Delete PO",
            part_number="DEL-PO-001",
            hs_code="8504.40",
            purchase_price=Decimal("12.50"),
            sale_price=Decimal("20.00"),
        )
        product_two = Product.objects.create(
            description="Produit Keep PO",
            part_number="KEEP-PO-001",
            hs_code="8504.50",
            purchase_price=Decimal("15.50"),
            sale_price=Decimal("25.00"),
        )
        purchase_order = PurchaseOrder.objects.create(vat_percent=Decimal("20.00"))
        item_delete = PurchaseOrderItem.objects.create(
            purchase_order=purchase_order,
            product=product_one,
            description=product_one.description,
            part_number=product_one.part_number,
            hs_code=product_one.hs_code,
            quantity=1,
            unit_price=Decimal("12.50"),
        )
        item_keep = PurchaseOrderItem.objects.create(
            purchase_order=purchase_order,
            product=product_two,
            description=product_two.description,
            part_number=product_two.part_number,
            hs_code=product_two.hs_code,
            quantity=2,
            unit_price=Decimal("15.50"),
        )

        response = self.client.post(
            reverse("admin:purchase_purchaseorder_autosave", args=[purchase_order.pk]),
            {
                "purchase_number": purchase_order.purchase_number,
                "purchase_date": purchase_order.purchase_date.strftime("%Y-%m-%d"),
                "seller": "",
                "requester": "",
                "sent_by": "",
                "shipment": "",
                "freight": "0.00",
                "vat_percent": "20.00",
                "sales_condition": "",
                "payment_condition": "",
                "delivery_terms": "",
                "items-TOTAL_FORMS": "2",
                "items-INITIAL_FORMS": "2",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
                "items-0-id": str(item_delete.pk),
                "items-0-purchase_order": str(purchase_order.pk),
                "items-0-product": str(product_one.pk),
                "items-0-hs_code": product_one.hs_code,
                "items-0-part_number": product_one.part_number,
                "items-0-quantity": "1",
                "items-0-unit_price": "12.50",
                "items-0-DELETE": "on",
                "items-1-id": str(item_keep.pk),
                "items-1-purchase_order": str(purchase_order.pk),
                "items-1-product": str(product_two.pk),
                "items-1-hs_code": product_two.hs_code,
                "items-1-part_number": product_two.part_number,
                "items-1-quantity": "2",
                "items-1-unit_price": "15.50",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(PurchaseOrderItem.objects.filter(pk=item_delete.pk).exists())
        self.assertTrue(PurchaseOrderItem.objects.filter(pk=item_keep.pk).exists())

    def test_save_and_add_another_redirects_to_new_purchase_order_form(self):
        purchase_order = PurchaseOrder.objects.create(vat_percent=Decimal("20.00"))

        response = self.client.post(
            reverse("admin:purchase_purchaseorder_change", args=[purchase_order.pk]),
            {
                "purchase_date": purchase_order.purchase_date.strftime("%Y-%m-%d"),
                "seller": "",
                "sent_by": "",
                "shipment": "",
                "freight": "0.00",
                "vat_percent": "20.00",
                "sales_condition": "",
                "payment_condition": "",
                "delivery_terms": "",
                "items-TOTAL_FORMS": "0",
                "items-INITIAL_FORMS": "0",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
                "_addanother": "Save and add another",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("admin:purchase_purchaseorder_add"))

    def test_save_and_pdf_saves_new_inline_item_then_redirects_to_pdf(self):
        product = Product.objects.create(
            description="Produit PDF PO",
            part_number="PO-PDF-001",
            hs_code="8504.40",
            purchase_price=Decimal("12.50"),
            sale_price=Decimal("20.00"),
        )
        purchase_order = PurchaseOrder.objects.create(vat_percent=Decimal("20.00"))
        pdf_url = reverse("admin:purchase_purchaseorder_pdf", args=[purchase_order.pk])

        response = self.client.post(
            reverse("admin:purchase_purchaseorder_change", args=[purchase_order.pk]),
            {
                "purchase_date": purchase_order.purchase_date.strftime("%Y-%m-%d"),
                "seller": "",
                "requester": "",
                "sent_by": "",
                "shipment": "",
                "freight": "0.00",
                "vat_percent": "20.00",
                "sales_condition": "",
                "payment_condition": "",
                "delivery_terms": "",
                "items-TOTAL_FORMS": "1",
                "items-INITIAL_FORMS": "0",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
                "items-0-id": "",
                "items-0-purchase_order": str(purchase_order.pk),
                "items-0-product": str(product.pk),
                "items-0-hs_code": product.hs_code,
                "items-0-part_number": product.part_number,
                "items-0-quantity": "2",
                "items-0-unit_price": "12.50",
                "_save_and_pdf": "1",
                "_save_and_pdf_url": pdf_url,
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], pdf_url)
        self.assertEqual(purchase_order.items.count(), 1)

    def test_save_button_ignores_stale_save_and_pdf_fields(self):
        purchase_order = PurchaseOrder.objects.create(vat_percent=Decimal("20.00"))
        pdf_url = reverse("admin:purchase_purchaseorder_pdf", args=[purchase_order.pk])

        response = self.client.post(
            reverse("admin:purchase_purchaseorder_change", args=[purchase_order.pk]),
            {
                "purchase_date": purchase_order.purchase_date.strftime("%Y-%m-%d"),
                "seller": "",
                "requester": "",
                "sent_by": "",
                "shipment": "",
                "freight": "0.00",
                "vat_percent": "20.00",
                "sales_condition": "",
                "payment_condition": "",
                "delivery_terms": "",
                "items-TOTAL_FORMS": "0",
                "items-INITIAL_FORMS": "0",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
                "_save": "Save",
                "_save_and_pdf": "1",
                "_save_and_pdf_url": pdf_url,
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertNotEqual(response["Location"], pdf_url)
        self.assertEqual(response["Location"], reverse("admin:purchase_purchaseorder_changelist"))
