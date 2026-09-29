from decimal import Decimal
from io import BytesIO
from pathlib import Path
import re

pdf_canvas = None
PDF_TOP_MARGIN = 38
# The footer finishes below 23 mm. Keep that safety boundary while allowing
# the body to use the small strip that was previously left empty above it.
PDF_BOTTOM_MARGIN = 23
PDF_INVOICE_BOX_HEIGHT_MM = 43
PDF_INVOICE_BOX_WIDTH_MM = 95
PDF_INVOICE_COLUMN_WIDTH_MM = 96
PDF_INVOICE_SIDE_MARGIN_MM = 9
PDF_INVOICE_BOX_TITLE_GAP_MM = 1.5
PDF_INVOICE_CONTENT_PADDING_MM = 1.5
PDF_INVOICE_REFERENCE_GAP_MM = 17
INVOICE_ITEM_COLUMN_WIDTHS_MM = (10, 50, 31, 22, 12, 27, 28)
INVOICE_AMOUNT_COLUMN_WIDTH_MM = INVOICE_ITEM_COLUMN_WIDTHS_MM[-1]
TOTALS_LABEL_COLUMN_WIDTH_MM = 50
TOTALS_AMOUNT_COLUMN_WIDTH_MM = 35
TOTALS_CELL_HORIZONTAL_PADDING = 6
SHIPPING_ITEM_COLUMN_WIDTHS_MM = (22, 70, 45, 27, 20)
PACKING_COLUMN_WIDTHS_MM = (22, 66, 24, 24, 16, 16, 16)
PRICE_FOR_AMOUNT_RIGHT_INSET_MM = 4
PDF_ACCENT_HEX = "#FF3300"

try:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT, TA_RIGHT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.graphics.charts.barcharts import VerticalBarChart
    from reportlab.graphics.shapes import Drawing, Rect, String
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas as pdf_canvas
    from reportlab.platypus import (
        HRFlowable,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )
    REPORTLAB_IMPORT_ERROR = None
except ImportError as exc:
    REPORTLAB_IMPORT_ERROR = exc


if REPORTLAB_IMPORT_ERROR is None:
    class InvoiceWidthHRFlowable(HRFlowable):
        """Horizontal rule matching the fixed-width invoice partner frames."""

        def wrap(self, availWidth, availHeight):
            width = self.width
            if isinstance(width, str):
                width = width.strip()
                if width.endswith("%"):
                    width = availWidth * float(width[:-1]) * 0.01
                else:
                    width = float(width)
            self._width = width
            return width, self.lineWidth


PDF_FONT_REGULAR = "FinanceDejaVuSans"
PDF_FONT_BOLD = "FinanceDejaVuSans-Bold"
PDF_FONTS_REGISTERED = False
PDF_FONT_DIR = Path(__file__).resolve().parent / "fonts"
PDF_FONT_REGULAR_PATH = PDF_FONT_DIR / "DejaVuSans.ttf"
PDF_FONT_BOLD_PATH = PDF_FONT_DIR / "DejaVuSans-Bold.ttf"


def _register_pdf_fonts():
    global PDF_FONT_REGULAR, PDF_FONT_BOLD, PDF_FONTS_REGISTERED

    if PDF_FONTS_REGISTERED or REPORTLAB_IMPORT_ERROR is not None:
        return

    missing_fonts = [
        str(path)
        for path in (PDF_FONT_REGULAR_PATH, PDF_FONT_BOLD_PATH)
        if not path.is_file()
    ]
    if missing_fonts:
        raise RuntimeError(f"Bundled PDF font files are missing: {', '.join(missing_fonts)}")

    pdfmetrics.registerFont(TTFont(PDF_FONT_REGULAR, str(PDF_FONT_REGULAR_PATH)))
    pdfmetrics.registerFont(TTFont(PDF_FONT_BOLD, str(PDF_FONT_BOLD_PATH)))
    pdfmetrics.registerFontFamily(
        PDF_FONT_REGULAR,
        normal=PDF_FONT_REGULAR,
        bold=PDF_FONT_BOLD,
        italic=PDF_FONT_REGULAR,
        boldItalic=PDF_FONT_BOLD,
    )

    PDF_FONTS_REGISTERED = True


def build_invoice_pdf(*, invoice, company, items, importer, end_user, invoice_title, currency, document_type="default", packing_entries=None, **_ignored):
    if REPORTLAB_IMPORT_ERROR is not None:
        raise RuntimeError("ReportLab is not installed in the active Python environment.") from REPORTLAB_IMPORT_ERROR

    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=PDF_INVOICE_SIDE_MARGIN_MM * mm,
        rightMargin=PDF_INVOICE_SIDE_MARGIN_MM * mm,
        topMargin=PDF_TOP_MARGIN * mm,
        bottomMargin=PDF_BOTTOM_MARGIN * mm,
        title=invoice.invoice_number or invoice_title,
    )

    styles = _build_styles()
    story = []

    story.append(Spacer(1, 0))
    story.extend(_build_document_info(invoice, styles))
    story.append(Spacer(1, 3 * mm))
    story.extend(_build_partner_blocks(importer, end_user, styles))
    story.append(Spacer(1, 2 * mm))
    story.append(
        InvoiceWidthHRFlowable(
            width=(PDF_INVOICE_COLUMN_WIDTH_MM + PDF_INVOICE_BOX_WIDTH_MM) * mm,
            thickness=0.8,
            color=colors.HexColor(PDF_ACCENT_HEX),
            spaceBefore=0,
            spaceAfter=0,
            hAlign="LEFT",
        )
    )
    story.append(Spacer(1, 2 * mm))
    story.extend(_build_invoice_details(invoice, company, styles, document_type=document_type, currency=currency))
    story.append(Spacer(1, 6 * mm))
    page_totals = []
    if _is_shipping_document(document_type):
        story.extend(_build_shipping_document_intro(invoice, company, styles))
        story.append(Spacer(1, 4 * mm))
        story.append(_build_shipping_items_table(items, styles))
        packing_section = _build_packing_section(
            invoice=invoice,
            packing_entries=packing_entries or [],
            styles=styles,
        )
        if packing_section:
            story.append(Spacer(1, 4 * mm))
            story.extend(packing_section)
    else:
        item_pages = [items]
        page_totals = _compute_page_totals(
            invoice=invoice,
            item_pages=item_pages,
        )
        story.append(_build_items_table(items, currency, styles))
        story.append(Spacer(1, 1 * mm))
        story.append(_build_page_totals_flowable(0, invoice, currency, styles, page_totals))

    def draw_page(canvas, doc):
        _draw_page_frame(
            canvas,
            doc,
            company=company,
            invoice=invoice,
            invoice_title=invoice_title,
            currency=currency,
            page_totals=page_totals,
            styles=styles,
        )

    document.build(story, onFirstPage=draw_page, onLaterPages=draw_page, canvasmaker=NumberedCanvas)
    return buffer.getvalue()


def build_commercial_report_pdf(
    *,
    company,
    currency,
    invoices,
    chart_labels,
    chart_totals,
    total_qty,
    total_subtotal,
    total_vat,
    total_freight,
    total_discount,
    total_amount,
    from_date,
    to_date,
):
    if REPORTLAB_IMPORT_ERROR is not None:
        raise RuntimeError("ReportLab is not installed in the active Python environment.") from REPORTLAB_IMPORT_ERROR

    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=12 * mm,
        rightMargin=14 * mm,
        topMargin=PDF_TOP_MARGIN * mm,
        bottomMargin=PDF_BOTTOM_MARGIN * mm,
        title="Commercial Invoices Report",
    )

    styles = _build_styles()
    story = [
        Spacer(1, 0),
        Paragraph("<b>Report Period:</b> {} to {}".format(_escape(from_date or "-"), _escape(to_date or "-")), styles["body"]),
        Spacer(1, 2 * mm),
        _build_report_summary_table(
            currency=currency,
            total_qty=total_qty,
            total_subtotal=total_subtotal,
            total_vat=total_vat,
            total_freight=total_freight,
            total_discount=total_discount,
            total_amount=total_amount,
            styles=styles,
        ),
        Spacer(1, 5 * mm),
    ]

    chart = _build_commercial_report_chart(chart_labels, chart_totals)
    if chart is not None:
        story.extend(
            [
                Paragraph("Monthly Commercial Chart", styles["section_title"]),
                Spacer(1, 2 * mm),
                chart,
                Spacer(1, 5 * mm),
            ]
        )

    story.extend(
        [
            Paragraph("Commercial Invoices", styles["section_title"]),
            Spacer(1, 2 * mm),
            _build_commercial_report_table(invoices=invoices, currency=currency, styles=styles),
        ]
    )

    def draw_page(canvas, doc):
        _draw_report_page_frame(
            canvas,
            doc,
            company=company,
            report_title="Commercial Invoices Report",
            styles=styles,
        )

    document.build(story, onFirstPage=draw_page, onLaterPages=draw_page, canvasmaker=NumberedCanvas)
    return buffer.getvalue()


def build_purchase_report_pdf(
    *,
    company,
    currency,
    purchase_orders,
    chart_labels,
    chart_totals,
    total_qty,
    total_gross,
    total_vat,
    total_freight,
    total_amount,
    from_date,
    to_date,
):
    if REPORTLAB_IMPORT_ERROR is not None:
        raise RuntimeError("ReportLab is not installed in the active Python environment.") from REPORTLAB_IMPORT_ERROR

    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=12 * mm,
        rightMargin=14 * mm,
        topMargin=PDF_TOP_MARGIN * mm,
        bottomMargin=PDF_BOTTOM_MARGIN * mm,
        title="Purchase Orders Report",
    )

    styles = _build_styles()
    story = [
        Spacer(1, 0),
        Paragraph("<b>Report Period:</b> {} to {}".format(_escape(from_date or "-"), _escape(to_date or "-")), styles["body"]),
        Spacer(1, 2 * mm),
        _build_purchase_report_summary_table(
            currency=currency,
            total_qty=total_qty,
            total_gross=total_gross,
            total_vat=total_vat,
            total_freight=total_freight,
            total_amount=total_amount,
            styles=styles,
        ),
        Spacer(1, 5 * mm),
    ]

    chart = _build_report_histogram(
        chart_labels=chart_labels,
        chart_totals=chart_totals,
        title="Monthly Purchase Chart",
    )
    if chart is not None:
        story.extend([chart, Spacer(1, 5 * mm)])

    story.extend(
        [
            Paragraph("Purchase Orders", styles["section_title"]),
            Spacer(1, 2 * mm),
            _build_purchase_report_table(purchase_orders=purchase_orders, currency=currency, styles=styles),
        ]
    )

    def draw_page(canvas, doc):
        _draw_report_page_frame(
            canvas,
            doc,
            company=company,
            report_title="Purchase Orders Report",
            styles=styles,
        )

    document.build(story, onFirstPage=draw_page, onLaterPages=draw_page, canvasmaker=NumberedCanvas)
    return buffer.getvalue()


def build_purchase_order_pdf(*, purchase_order, company, items, seller, requester, currency, requester_is_explicit=False):
    if REPORTLAB_IMPORT_ERROR is not None:
        raise RuntimeError("ReportLab is not installed in the active Python environment.") from REPORTLAB_IMPORT_ERROR

    document_title = "COMMAND / ORDER"
    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=12 * mm,
        rightMargin=14 * mm,
        topMargin=PDF_TOP_MARGIN * mm,
        bottomMargin=PDF_BOTTOM_MARGIN * mm,
        title=purchase_order.purchase_number or document_title,
    )

    styles = _build_purchase_order_styles()
    story = [
        Spacer(1, 0),
        *_build_purchase_company_address(company, styles),
        Paragraph(f"<b>Purchase Number:</b> {_escape(purchase_order.purchase_number or '-')}", styles["body"]),
        Paragraph(
            f"<b>Purchase Date:</b> {purchase_order.purchase_date.strftime('%d/%m/%Y') if purchase_order.purchase_date else '-'}",
            styles["body_compact"],
        ),
        Spacer(1, 3 * mm),
    ]
    story.extend(_build_purchase_order_context_blocks(seller, requester, purchase_order, company, styles, requester_is_explicit))
    story.append(Spacer(1, 6 * mm))

    item_pages = [items]
    page_totals = _compute_purchase_order_page_totals(purchase_order=purchase_order, item_pages=item_pages)
    story.append(_build_purchase_order_items_table(items, currency, styles))
    story.append(Spacer(1, 1 * mm))
    story.append(_build_purchase_order_totals_flowable(0, purchase_order, currency, styles, page_totals))
    story.append(Spacer(1, 4 * mm))
    story.append(_build_purchase_order_commercial_terms_box(purchase_order, company, styles))

    def draw_page(canvas, doc):
        _draw_report_page_frame(
            canvas,
            doc,
            company=company,
            report_title=document_title,
            report_meta=[
                f"<b>Purchase Number:</b> {_escape(purchase_order.purchase_number or '-')}",
                f"<b>Purchase Date:</b> {purchase_order.purchase_date.strftime('%d/%m/%Y') if purchase_order.purchase_date else '-'}",
            ],
            styles=styles,
        )

    document.build(story, onFirstPage=draw_page, onLaterPages=draw_page, canvasmaker=NumberedCanvas)
    return buffer.getvalue()


def _build_styles():
    _register_pdf_fonts()
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "InvoiceTitle",
            parent=base["Heading1"],
            fontName=PDF_FONT_BOLD,
            fontSize=20.5,
            leading=22.5,
            textColor=colors.HexColor(PDF_ACCENT_HEX),
            spaceAfter=0,
        ),
        "section_title": ParagraphStyle(
            "SectionTitle",
            parent=base["Heading4"],
            fontName=PDF_FONT_BOLD,
            fontSize=8.5,
            leading=9.5,
            textColor=colors.HexColor(PDF_ACCENT_HEX),
            spaceAfter=0,
        ),
        "invoice_box_title": ParagraphStyle(
            "InvoiceBoxTitle",
            parent=base["Heading4"],
            fontName=PDF_FONT_BOLD,
            fontSize=8.5,
            leading=9.5,
            textColor=colors.HexColor(PDF_ACCENT_HEX),
            spaceAfter=0,
        ),
        "document_type_title": ParagraphStyle(
            "DocumentTypeTitle",
            parent=base["Heading4"],
            fontName=PDF_FONT_BOLD,
            fontSize=11,
            leading=12.5,
            textColor=colors.HexColor(PDF_ACCENT_HEX),
            spaceAfter=0,
        ),
        "label": ParagraphStyle(
            "Label",
            parent=base["BodyText"],
            fontName=PDF_FONT_BOLD,
            fontSize=8,
            leading=9,
            textColor=colors.HexColor(PDF_ACCENT_HEX),
        ),
        "label_right": ParagraphStyle(
            "LabelRight",
            parent=base["BodyText"],
            fontName=PDF_FONT_BOLD,
            fontSize=8,
            leading=9,
            alignment=TA_RIGHT,
            textColor=colors.HexColor(PDF_ACCENT_HEX),
        ),
        "totals_label": ParagraphStyle(
            "TotalsLabel",
            parent=base["BodyText"],
            fontName=PDF_FONT_BOLD,
            fontSize=8,
            leading=9,
            textColor=colors.HexColor(PDF_ACCENT_HEX),
        ),
        "body": ParagraphStyle(
            "Body",
            parent=base["BodyText"],
            fontName=PDF_FONT_REGULAR,
            fontSize=9,
            leading=10.2,
            alignment=TA_JUSTIFY,
            textColor=colors.black,
        ),
        "body_left": ParagraphStyle(
            "BodyLeft",
            parent=base["BodyText"],
            fontName=PDF_FONT_REGULAR,
            fontSize=9,
            leading=10.2,
            alignment=TA_LEFT,
            textColor=colors.HexColor("#1F2933"),
        ),
        "body_left_bold": ParagraphStyle(
            "BodyLeftBold",
            parent=base["BodyText"],
            fontName=PDF_FONT_BOLD,
            fontSize=9,
            leading=10.2,
            alignment=TA_LEFT,
            textColor=colors.HexColor("#1F2933"),
        ),
        "invoice_box_body": ParagraphStyle(
            "InvoiceBoxBody",
            parent=base["BodyText"],
            fontName=PDF_FONT_REGULAR,
            fontSize=9,
            leading=10.2,
            alignment=TA_LEFT,
            textColor=colors.HexColor("#1F2933"),
            splitLongWords=True,
        ),
        "partner_compact_body": ParagraphStyle(
            "PartnerCompactBody",
            parent=base["BodyText"],
            fontName=PDF_FONT_REGULAR,
            fontSize=9,
            leading=10.6,
            alignment=TA_LEFT,
            textColor=colors.HexColor("#1F2933"),
            splitLongWords=True,
            spaceBefore=0,
            spaceAfter=0,
        ),
        "partner_body": ParagraphStyle(
            "PartnerBody",
            parent=base["BodyText"],
            fontName=PDF_FONT_REGULAR,
            fontSize=9,
            leading=10.2,
            alignment=TA_LEFT,
            textColor=colors.HexColor("#1F2933"),
            splitLongWords=True,
        ),
        "info_box_body": ParagraphStyle(
            "InfoBoxBody",
            parent=base["BodyText"],
            fontName=PDF_FONT_REGULAR,
            fontSize=9,
            leading=10.2,
            alignment=TA_LEFT,
            textColor=colors.HexColor("#1F2933"),
            splitLongWords=True,
        ),
        "invoice_note_body": ParagraphStyle(
            "InvoiceNoteBody",
            parent=base["BodyText"],
            fontName=PDF_FONT_REGULAR,
            fontSize=9,
            leading=9.8,
            alignment=TA_LEFT,
            textColor=colors.HexColor("#1F2933"),
            splitLongWords=True,
            spaceBefore=0,
            spaceAfter=0,
        ),
        "terms_body": ParagraphStyle(
            "TermsBody",
            parent=base["BodyText"],
            fontName=PDF_FONT_REGULAR,
            fontSize=9,
            leading=9.8,
            alignment=TA_LEFT,
            textColor=colors.HexColor("#1F2933"),
            splitLongWords=True,
            spaceBefore=0,
            spaceAfter=0,
        ),
        "body_compact": ParagraphStyle(
            "BodyCompact",
            parent=base["BodyText"],
            fontName=PDF_FONT_REGULAR,
            fontSize=9,
            leading=9.7,
            spaceBefore=0,
            spaceAfter=0,
            alignment=TA_JUSTIFY,
            textColor=colors.black,
        ),
        "body_small": ParagraphStyle(
            "BodySmall",
            parent=base["BodyText"],
            fontName=PDF_FONT_REGULAR,
            fontSize=8,
            leading=9,
            alignment=TA_JUSTIFY,
            textColor=colors.black,
        ),
        "body_right": ParagraphStyle(
            "BodyRight",
            parent=base["BodyText"],
            fontName=PDF_FONT_REGULAR,
            fontSize=9,
            leading=10.2,
            alignment=TA_RIGHT,
            textColor=colors.black,
        ),
        "table_head": ParagraphStyle(
            "TableHead",
            parent=base["BodyText"],
            fontName=PDF_FONT_BOLD,
            fontSize=8,
            leading=9,
            alignment=TA_LEFT,
            textColor=colors.HexColor(PDF_ACCENT_HEX),
            splitLongWords=False,
        ),
        "table_head_center": ParagraphStyle(
            "TableHeadCenter",
            parent=base["BodyText"],
            fontName=PDF_FONT_BOLD,
            fontSize=8,
            leading=9,
            alignment=TA_CENTER,
            textColor=colors.HexColor(PDF_ACCENT_HEX),
            splitLongWords=False,
        ),
        "table_head_amount": ParagraphStyle(
            "TableHeadAmount",
            parent=base["BodyText"],
            fontName=PDF_FONT_BOLD,
            fontSize=8,
            leading=9,
            alignment=TA_RIGHT,
            textColor=colors.HexColor(PDF_ACCENT_HEX),
            splitLongWords=False,
        ),
        "table_cell": ParagraphStyle(
            "TableCell",
            parent=base["BodyText"],
            fontName=PDF_FONT_REGULAR,
            fontSize=7.2,
            leading=8,
            alignment=TA_JUSTIFY,
            textColor=colors.HexColor("#3F2A14"),
        ),
        "table_cell_part_number": ParagraphStyle(
            "TableCellPartNumber",
            parent=base["BodyText"],
            fontName=PDF_FONT_REGULAR,
            fontSize=7.2,
            leading=8,
            alignment=TA_LEFT,
            textColor=colors.HexColor("#3F2A14"),
            wordWrap="CJK",
        ),
        "table_cell_right": ParagraphStyle(
            "TableCellRight",
            parent=base["BodyText"],
            fontName=PDF_FONT_REGULAR,
            fontSize=7.2,
            leading=8,
            alignment=TA_RIGHT,
            textColor=colors.HexColor("#3F2A14"),
        ),
        "table_cell_center": ParagraphStyle(
            "TableCellCenter",
            parent=base["BodyText"],
            fontName=PDF_FONT_REGULAR,
            fontSize=7.2,
            leading=8,
            alignment=TA_CENTER,
            textColor=colors.HexColor("#3F2A14"),
        ),
        "purchase_order_date_value": ParagraphStyle(
            "PurchaseOrderDateValue",
            parent=base["BodyText"],
            fontName=PDF_FONT_REGULAR,
            fontSize=8.3,
            leading=9.4,
            alignment=TA_LEFT,
            textColor=colors.HexColor("#3F2A14"),
        ),
        "table_cell_amount": ParagraphStyle(
            "TableCellAmount",
            parent=base["BodyText"],
            fontName=PDF_FONT_REGULAR,
            fontSize=7.2,
            leading=8,
            alignment=TA_RIGHT,
            textColor=colors.HexColor("#3F2A14"),
        ),
        "footer": ParagraphStyle(
            "Footer",
            parent=base["BodyText"],
            fontName=PDF_FONT_REGULAR,
            fontSize=8,
            leading=9,
            alignment=TA_RIGHT,
            textColor=colors.black,
        ),
        "footer_left_small": ParagraphStyle(
            "FooterLeftSmall",
            parent=base["BodyText"],
            fontName=PDF_FONT_REGULAR,
            fontSize=7,
            leading=8,
            alignment=TA_LEFT,
            textColor=colors.black,
        ),
        "footer_center_small": ParagraphStyle(
            "FooterCenterSmall",
            parent=base["BodyText"],
            fontName=PDF_FONT_REGULAR,
            fontSize=7,
            leading=8,
            alignment=TA_CENTER,
            textColor=colors.black,
        ),
        "footer_right_small": ParagraphStyle(
            "FooterRightSmall",
            parent=base["BodyText"],
            fontName=PDF_FONT_REGULAR,
            fontSize=7,
            leading=8,
            alignment=TA_RIGHT,
            textColor=colors.black,
        ),
    }


def _build_purchase_order_styles():
    base_styles = _build_styles()
    styles = _build_styles()
    for style in styles.values():
        if hasattr(style, "fontSize"):
            style.fontSize = max(1, style.fontSize - 1)
        if hasattr(style, "leading"):
            style.leading = max(1, style.leading - 1)

    # Keep product rows readable; increasing the first-page item limit must not
    # shrink item text to force more lines onto the page.
    for key in (
        "table_head",
        "table_head_amount",
        "table_cell",
        "table_cell_part_number",
        "table_cell_amount",
        "table_cell_right",
        "table_cell_center",
    ):
        styles[key] = base_styles[key]

    return styles


def _build_invoice_item_table_styles(styles):
    item_styles = styles.copy()
    for key in (
        "table_cell",
        "table_cell_part_number",
        "table_cell_amount",
        "table_cell_right",
        "table_cell_center",
    ):
        item_styles[key] = styles[key].clone(
            f"{styles[key].name}ProductRow",
            leading=7.5,
        )
    return item_styles


def _build_document_info(invoice, styles):
    info_lines = [
        Paragraph(f"<b>Invoice Number:</b> {_escape(invoice.invoice_number or '-')}", styles["body"]),
        Paragraph(
            f"<b>Invoice Date:</b> {invoice.invoice_date.strftime('%d/%m/%Y') if invoice.invoice_date else '-'}",
            styles["body_compact"],
        ),
    ]
    return [Spacer(1, -2 * mm)] + info_lines


def _build_partner_blocks(importer, end_user, styles):
    importer_card = _partner_card("Importer", importer, styles, accent_border=True)
    end_user_card = _partner_card("End User", end_user, styles, accent_border=True)

    table = Table(
        [[importer_card, end_user_card]],
        colWidths=[PDF_INVOICE_COLUMN_WIDTH_MM * mm, PDF_INVOICE_COLUMN_WIDTH_MM * mm],
        hAlign="LEFT",
    )
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    return [table]


def _build_invoice_details(invoice, company, styles, document_type="default", currency="EUR"):
    blocks = []

    invoice_note = getattr(company, "invoice_note", "") or "-"
    terms_lines = []
    terms_conditions = getattr(invoice, "terms_conditions", "") or getattr(company, "terms_conditions", "")
    delivery_time = getattr(invoice, "delivery_time", "") or getattr(company, "delivery_time", "")
    if terms_conditions:
        terms_lines.append(f"<b>Terms and Conditions:</b> {_format_preserving_layout(terms_conditions)}")
    if delivery_time:
        terms_lines.append(f"<b>Delivery Time:</b> {_format_preserving_layout(delivery_time)}")
    if _is_proforma_invoice(invoice) and getattr(company, "proforma_validity", None):
        terms_lines.append(f"<b>Proforma Validity:</b> {_escape(company.proforma_validity)} days")
    note_box = _info_box("Invoice Note", _format_invoice_note_text(invoice_note), styles, body_style_key="invoice_note_body")
    left_column = [note_box]

    if _is_shipping_document(document_type):
        right_column = []
    else:
        terms_text = "<br/>".join(terms_lines) if terms_lines else "-"
        terms_box = _info_box("Terms", terms_text, styles, body_style_key="terms_body", paragraph_gap=1.2 * mm)
        right_column = [terms_box]

    if not right_column:
        right_column = [Spacer(1, 0)]

    details_table = Table(
        [[left_column, right_column]],
        colWidths=[PDF_INVOICE_COLUMN_WIDTH_MM * mm, PDF_INVOICE_COLUMN_WIDTH_MM * mm],
        hAlign="LEFT",
    )
    details_table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    blocks.append(details_table)

    if not _is_shipping_document(document_type):
        price_for_text = ""
        if _is_proforma_invoice(invoice) or hasattr(invoice, "price_for"):
            price_for = getattr(invoice, "price_for", "") or "-"
            price_for_text = f"<b>Price for:</b> {_format_preserving_layout(price_for)}"

        reference_table_width_mm = PDF_INVOICE_COLUMN_WIDTH_MM * 2
        reference_width_mm = PDF_INVOICE_COLUMN_WIDTH_MM
        price_for_width_mm = (
            reference_table_width_mm
            - reference_width_mm
            - TOTALS_AMOUNT_COLUMN_WIDTH_MM
            - PRICE_FOR_AMOUNT_RIGHT_INSET_MM
        )
        reference_table = Table(
            [[
                Paragraph(
                    f"<b>Our Reference:</b> {_format_preserving_layout(getattr(invoice, 'our_reference', '') or '-')}",
                    styles["body"],
                ),
                Paragraph(price_for_text, styles["body"]) if price_for_text else Spacer(1, 0),
                Paragraph(_escape(_format_money(invoice.total_amount(), currency)), styles["body_right"])
                if price_for_text else Spacer(1, 0),
                Spacer(1, 0),
            ]],
            colWidths=[
                reference_width_mm * mm,
                price_for_width_mm * mm,
                TOTALS_AMOUNT_COLUMN_WIDTH_MM * mm,
                PRICE_FOR_AMOUNT_RIGHT_INSET_MM * mm,
            ],
            hAlign="LEFT",
        )
        reference_table.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    # Align both labels with the content inside the
                    # Invoice Note and Terms frames above.
                    ("LEFTPADDING", (0, 0), (-1, -1), 4),
                    ("RIGHTPADDING", (0, 0), (1, 0), 4),
                    ("LEFTPADDING", (2, 0), (2, 0), TOTALS_CELL_HORIZONTAL_PADDING),
                    ("RIGHTPADDING", (2, 0), (2, 0), TOTALS_CELL_HORIZONTAL_PADDING),
                    ("ALIGN", (2, 0), (2, 0), "RIGHT"),
                    ("TOPPADDING", (0, 0), (-1, -1), 0),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ]
            )
        )
        blocks.extend(
            [
                # Keep about three text lines between the invoice note/terms
                # section and the Our Reference / Price for row.
                Spacer(1, PDF_INVOICE_REFERENCE_GAP_MM * mm),
                reference_table,
                Spacer(1, 2 * mm),
                InvoiceWidthHRFlowable(
                    width=(PDF_INVOICE_COLUMN_WIDTH_MM + PDF_INVOICE_BOX_WIDTH_MM) * mm,
                    thickness=0.8,
                    color=colors.HexColor(PDF_ACCENT_HEX),
                    spaceBefore=0,
                    spaceAfter=0,
                    hAlign="LEFT",
                ),
            ]
        )

    return blocks


def _build_shipping_document_intro(invoice, company, styles):
    left_column = [
        Paragraph(f"<b>Our Order No:</b> {_format_preserving_layout(getattr(invoice, 'our_order_no', '') or '-')}", styles["body"]),
        Spacer(1, 1.5 * mm),
        Paragraph(f"<b>Our Reference:</b> {_format_preserving_layout(getattr(invoice, 'our_reference', '') or '-')}", styles["body"]),
    ]

    right_column = [Spacer(1, 0)]

    summary_table = Table([[left_column, right_column]], colWidths=[92 * mm, 92 * mm], hAlign="LEFT")
    summary_table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    return [
        summary_table,
        Spacer(1, 2 * mm),
        InvoiceWidthHRFlowable(
            width=(PDF_INVOICE_COLUMN_WIDTH_MM + PDF_INVOICE_BOX_WIDTH_MM) * mm,
            thickness=0.8,
            color=colors.HexColor(PDF_ACCENT_HEX),
            spaceBefore=0,
            spaceAfter=0,
            hAlign="LEFT",
        ),
    ]


def _build_purchase_order_details(purchase_order, company, styles):
    order_details_box, commercial_terms_box = _build_purchase_order_detail_boxes(purchase_order, company, styles)
    table = Table([[order_details_box, commercial_terms_box]], colWidths=[92 * mm, 92 * mm], hAlign="LEFT")
    table.setStyle(_two_column_table_style())
    return [table]


def _build_purchase_order_context_blocks(seller, requester, purchase_order, company, styles, requester_is_explicit=False):
    order_details_box, _ = _build_purchase_order_detail_boxes(purchase_order, company, styles)
    seller_card = _partner_card("Seller", seller, styles, left_padding=0, top_padding=0, bottom_padding=1 * mm)
    order_info_table = _build_purchase_order_info_table(purchase_order, requester, styles)
    note_currency_box = _build_purchase_note_currency_box(company, styles)
    rows = [
        [[order_details_box, Spacer(1, 0.5 * mm), note_currency_box], Spacer(1, 0)],
        [seller_card, Spacer(1, 0)],
        [order_info_table, ""],
    ]

    table = Table(
        rows,
        colWidths=[92 * mm, 92 * mm],
        hAlign="LEFT",
    )
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0.5 * mm),
                ("SPAN", (0, 2), (1, 2)),
            ]
        )
    )
    return [table]


def _build_purchase_note_currency_box(company, styles):
    note = getattr(company, "note", "") or "-"
    currency = getattr(company, "currency", "") or "-"
    currency_text = "Euro" if str(currency).strip().upper() in {"EUR", "€", "EURO"} else str(currency).strip()
    return [
        Paragraph(_format_preserving_layout(note), styles["body"]),
        Paragraph(f"<b>Currency :</b> {_escape(currency_text)}", styles["body"]),
    ]


def _build_purchase_order_commercial_terms_box(purchase_order, company, styles):
    president = getattr(company, "president", "") or "-"
    purchase_date = purchase_order.purchase_date.strftime("%d/%m/%Y") if purchase_order.purchase_date else "-"
    terms_lines = []
    if getattr(company, "footer_order", ""):
        terms_lines.append(_format_preserving_layout(company.footer_order))
    if not terms_lines:
        terms_lines = ["-"]

    signature_block = [
        Paragraph(_format_preserving_layout(president), styles["body_right"]),
        Paragraph("President", styles["body_right"]),
        Spacer(1, 5 * mm),
        Paragraph(f"<b>Date:</b> {purchase_date}", styles["body_right"]),
    ]
    table = Table(
        [[
            Paragraph("<br/>".join(terms_lines), styles.get("body_left", styles["body"])),
            [Spacer(1, 2 * mm), *signature_block],
        ]],
        colWidths=[118 * mm, 66 * mm],
        hAlign="LEFT",
    )
    table.setStyle(_two_column_table_style())
    return table


def _build_purchase_order_shipment_box(purchase_order, styles):
    sales_condition = getattr(purchase_order, "sales_condition", "") or ""
    payment_condition = getattr(purchase_order, "payment_condition", "") or ""
    delivery_terms = getattr(purchase_order, "delivery_terms", "") or ""
    lines = [
        f"<b>SHIPMENT / EXPEDITION:</b> {_format_preserving_layout(getattr(purchase_order, 'shipment', '') or '-')}",
        f"<b>SEND BY / ENVOYER PAR:</b> {_format_preserving_layout(getattr(purchase_order, 'sent_by', '') or '-')}",
    ]
    if sales_condition:
        lines.append(f"<b>CONDITIONS DE PRIC/<br/>SALES CONDITIONS:</b> {_format_preserving_layout(sales_condition)}")
    if payment_condition:
        lines.append(f"<b>Payment Condition:</b> {_format_preserving_layout(payment_condition)}")
    if delivery_terms:
        lines.append(f"<b>Delivery Terms:</b> {_format_preserving_layout(delivery_terms)}")
    return [
        Paragraph("<br/>".join(lines), styles.get("body_left", styles["body"])),
    ]


def _build_purchase_order_info_table(purchase_order, requester, styles):
    requester = requester or {}
    requester_lines = [requester.get("name") or "-"]
    requester_lines.extend(address for address in requester.get("addresses", []) if address)
    requester_text = "<br/>".join(_format_preserving_layout(line) for line in requester_lines)
    table_width = 184 * mm
    right_padding = 4
    last_label_width = pdfmetrics.stringWidth(
        "Condition de prix /",
        styles["label"].fontName,
        styles["label"].fontSize,
    )
    last_column_width = last_label_width + right_padding
    regular_column_width = (table_width - last_column_width) / 4

    rows = [
        [
            Paragraph("Date de<br/>commande /<br/>Order Date", styles["label"]),
            Paragraph("Demander /<br/>Requester", styles["label"]),
            Paragraph("Envoyer par /<br/>Send By", styles["label"]),
            Paragraph("Expédition /<br/>Shipment", styles["label"]),
            Paragraph("Condition de prix /<br/>Sales Conditions", styles["label"]),
        ],
        [
            Paragraph(
                purchase_order.purchase_date.strftime("%d-%b-%Y") if purchase_order.purchase_date else "-",
                styles["purchase_order_date_value"],
            ),
            Paragraph(requester_text, styles["body_left"]),
            Paragraph(_format_preserving_layout(getattr(purchase_order, "sent_by", "") or "-"), styles["table_cell"]),
            Paragraph(_format_preserving_layout(getattr(purchase_order, "shipment", "") or "-"), styles["body_left"]),
            Paragraph(_format_preserving_layout(getattr(purchase_order, "sales_condition", "") or "-"), styles["table_cell"]),
        ],
    ]
    table = Table(
        rows,
        colWidths=[
            regular_column_width,
            regular_column_width,
            regular_column_width,
            regular_column_width,
            last_column_width,
        ],
        hAlign="LEFT",
    )
    table.setStyle(
        TableStyle(
            [
                ("LINEABOVE", (0, 0), (-1, 0), 0.5, colors.HexColor("#D9D9D9")),
                ("LINEBELOW", (0, -1), (-1, -1), 0.5, colors.HexColor("#D9D9D9")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), right_padding),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (1, 1), (1, 1), 4),
                ("BOTTOMPADDING", (1, 1), (1, 1), 4),
            ]
        )
    )
    return table


def _build_purchase_order_detail_boxes(purchase_order, company, styles):
    left_lines = []
    if getattr(company, "company_name", ""):
        left_lines.append(
            f'<font color="{PDF_ACCENT_HEX}"><b>{_format_preserving_layout(company.company_name)}</b></font>'
        )
    if getattr(company, "siren", ""):
        left_lines.append(f"<b>SIREN:</b> {_format_preserving_layout(company.siren)}")
    if getattr(company, "company_email", ""):
        left_lines.append(f"<b>Email:</b> {_format_preserving_layout(company.company_email)}")
    if getattr(company, "company_phone", ""):
        left_lines.append(f"<b>Telephone:</b> {_format_preserving_layout(company.company_phone)}")
    if getattr(company, "company_fax", ""):
        left_lines.append(f"<b>Fax:</b> {_format_preserving_layout(company.company_fax)}")
    if not left_lines:
        left_lines = ["-"]

    left_box = _purchase_order_company_contact_box("<br/>".join(left_lines), styles)
    right_box = _build_purchase_order_commercial_terms_box(purchase_order, company, styles)
    return left_box, right_box


def _purchase_order_company_contact_box(text, styles):
    box = Table([[Paragraph(text, styles.get("body_left", styles["body"]))]], colWidths=[89 * mm])
    box.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.white),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5 * mm),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2 * mm),
            ]
        )
    )
    return box


def _two_column_table_style():
    return TableStyle(
        [
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]
    )


def _build_purchase_company_address(company, styles):
    company_address = getattr(company, "address", "") or getattr(company, "company_address", "")
    if not company_address:
        return []

    return [
        Paragraph(_format_preserving_layout(company_address), styles["body"]),
        Spacer(1, 2 * mm),
    ]


def _partner_card(
    title,
    partner,
    styles,
    left_padding=None,
    top_padding=None,
    bottom_padding=None,
    accent_border=False,
):
    left_padding = PDF_INVOICE_CONTENT_PADDING_MM * mm if left_padding is None else left_padding
    top_padding = 1 * mm if top_padding is None else top_padding
    bottom_padding = 1 * mm if bottom_padding is None else bottom_padding

    lines = []
    if title:
        title_gap = PDF_INVOICE_BOX_TITLE_GAP_MM * mm if accent_border else 0.2 * mm
        lines.extend([Paragraph(title, styles["invoice_box_title"]), Spacer(1, title_gap)])

    name = _normalize_pdf_text(partner.get("name") or "-")
    info_lines = [f"<b>{_escape(name)}</b>"]
    info_lines.extend(_escape(_normalize_pdf_text(address)) for address in partner.get("addresses", []) if address)

    phones = [phone for phone in partner.get("phones", []) if phone]
    if phones:
        info_lines.append(f"Tel: {_escape(_normalize_pdf_text(', '.join(phones)))}")
    if partner.get("email"):
        info_lines.append(f"Email: {_escape(_normalize_pdf_text(partner['email']))}")
    if partner.get("website"):
        info_lines.append(_escape(_normalize_pdf_text(partner["website"])))
    if partner.get("fax"):
        info_lines.append(f"Fax: {_escape(_normalize_pdf_text(partner['fax']))}")

    partner_style = styles.get("partner_compact_body", styles.get("invoice_box_body", styles["body_left"]))
    lines.append(Paragraph("<br/>".join(info_lines), partner_style))
    row_heights = [PDF_INVOICE_BOX_HEIGHT_MM * mm] if accent_border else None
    card = Table(
        [[lines]],
        colWidths=[PDF_INVOICE_BOX_WIDTH_MM * mm],
        rowHeights=row_heights,
    )
    commands = [
        ("BACKGROUND", (0, 0), (-1, -1), colors.white),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), left_padding),
        ("RIGHTPADDING", (0, 0), (-1, -1), 1.5 * mm),
        ("TOPPADDING", (0, 0), (-1, -1), top_padding),
        ("BOTTOMPADDING", (0, 0), (-1, -1), bottom_padding),
    ]
    if accent_border:
        commands.append(("BOX", (0, 0), (-1, -1), 1, colors.HexColor(PDF_ACCENT_HEX)))
    card.setStyle(TableStyle(commands))
    return card


def _info_box(title, text, styles, body_style_key="invoice_box_body", title_gap=None, paragraph_gap=0):
    title_gap = PDF_INVOICE_BOX_TITLE_GAP_MM * mm if title_gap is None else title_gap
    content = [
        Paragraph(title, styles["invoice_box_title"]),
    ]
    if title_gap:
        content.append(Spacer(1, title_gap))
    content.extend(_build_info_box_paragraphs(text, styles, body_style_key=body_style_key, paragraph_gap=paragraph_gap))
    box = Table(
        [[content]],
        colWidths=[PDF_INVOICE_BOX_WIDTH_MM * mm],
        rowHeights=[PDF_INVOICE_BOX_HEIGHT_MM * mm],
    )
    box.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.white),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), PDF_INVOICE_CONTENT_PADDING_MM * mm),
                ("RIGHTPADDING", (0, 0), (-1, -1), PDF_INVOICE_CONTENT_PADDING_MM * mm),
                ("TOPPADDING", (0, 0), (-1, -1), 1 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1 * mm),
            ]
        )
    )
    return box


def _build_info_box_paragraphs(text, styles, body_style_key="invoice_box_body", paragraph_gap=0):
    style = styles.get(body_style_key, styles.get("invoice_box_body", styles.get("info_box_body", styles["body_left"])))
    if text == "-":
        return [Paragraph("-", style)]

    paragraphs = []
    for line in str(text).split("<br/>"):
        clean = _normalize_pdf_text(line)
        if clean:
            if paragraphs and paragraph_gap:
                paragraphs.append(Spacer(1, paragraph_gap))
            paragraphs.append(Paragraph(clean, style))
        else:
            paragraphs.append(Spacer(1, style.leading))
    return paragraphs or [Paragraph("-", style)]


def _format_invoice_note_text(value):
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").expandtabs(4).strip()
    if not text:
        return "-"
    return _format_preserving_layout(text)


def _build_meta_section(invoice, company, styles):
    left_rows = [
        ["Order No", getattr(invoice, "our_order_no", "") or "-"],
        ["Currency", _format_currency_symbol(getattr(company, "currency", "") or "EUR")],
    ]

    if hasattr(invoice, "ready_for_report"):
        left_rows.append(["Validity", f"{getattr(company, 'proforma_validity', 0)} days"])

    right_rows = [
        ["Subtotal", _format_money(invoice.subtotal(), getattr(company, "currency", "EUR"))],
        ["VAT", f"{invoice.vat_percent:.2f}%"],
        ["Total", _format_money(invoice.total_amount(), getattr(company, "currency", "EUR"))],
    ]

    data = [
        [
            _meta_table(left_rows, styles),
            _meta_table(right_rows, styles, align_right=True),
        ]
    ]
    table = Table(data, colWidths=[85 * mm, 85 * mm])
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    return [table]


def _meta_table(rows, styles, align_right=False):
    label_style = styles["label"]
    value_style = styles["body_right"] if align_right else styles["body"]
    table = Table(
        [[Paragraph(_escape(label), label_style), Paragraph(_escape(value), value_style)] for label, value in rows],
        colWidths=[24 * mm, 58 * mm],
    )
    table.setStyle(
        TableStyle(
            [
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    return table


def _build_items_table(items, currency, styles, amount_from_last_page=None):
    styles = _build_invoice_item_table_styles(styles)
    rows = [
        [
            Paragraph("Item", styles["table_head"]),
            Paragraph("Description", styles["table_head"]),
            Paragraph("Part No", styles["table_head"]),
            Paragraph("HS Code", styles["table_head"]),
            Paragraph("Qty", styles["table_head_amount"]),
            Paragraph("Unit Price", styles["table_head_amount"]),
            Paragraph("Amount", styles["table_head_amount"]),
        ]
    ]
    if amount_from_last_page is not None:
        rows.append(
            [
                Paragraph("", styles["table_cell"]),
                Paragraph("Amount from Last Page", styles["table_cell"]),
                Paragraph("", styles["table_cell"]),
                Paragraph("", styles["table_cell"]),
                Paragraph("", styles["table_cell_amount"]),
                Paragraph("", styles["table_cell_amount"]),
                _build_money_split_cell(amount_from_last_page, currency, styles, 17 * mm),
            ]
        )

    for item in items:
        rows.append(
            [
                Paragraph(str(item["index"]), styles["table_cell"]),
                Paragraph(_format_preserving_layout(item["description"]), styles["table_cell"]),
                Paragraph(_escape(item["part_number"]), styles["table_cell_part_number"]),
                Paragraph(_escape(item["hs_code"]), styles["table_cell"]),
                Paragraph(str(item["quantity"]), styles["table_cell_amount"]),
                _build_money_split_cell(item["unit_price"], currency, styles, 17 * mm),
                _build_money_split_cell(item["total_amount"], currency, styles, 17 * mm),
            ]
        )

    if len(rows) == 1:
        rows.append(
            [
                Paragraph("-", styles["table_cell"]),
                Paragraph("No items", styles["table_cell"]),
                Paragraph("-", styles["table_cell"]),
                Paragraph("-", styles["table_cell"]),
                Paragraph("-", styles["table_cell_amount"]),
                Paragraph("-", styles["table_cell_amount"]),
                Paragraph("-", styles["table_cell_amount"]),
            ]
        )

    table = Table(
        rows,
        colWidths=[width * mm for width in INVOICE_ITEM_COLUMN_WIDTHS_MM],
        repeatRows=1,
    )
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.white),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (4, 0), (-1, -1), "RIGHT"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.white]),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("LEFTPADDING", (3, 0), (3, -1), 2),
                ("RIGHTPADDING", (3, 0), (3, -1), 2),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 1), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 1), (-1, -1), 3),
            ]
        )
    )
    return table


def _build_shipping_items_table(items, styles):
    styles = _build_invoice_item_table_styles(styles)
    rows = [
        [
            Paragraph("Item No", styles["table_head_center"]),
            Paragraph("Description", styles["table_head"]),
            Paragraph("Part Number", styles["table_head"]),
            Paragraph("HS Code", styles["table_head"]),
            Paragraph("Qty", styles["table_head"]),
        ]
    ]

    for item in items:
        rows.append(
            [
                Paragraph(str(item["index"]), styles["table_cell_center"]),
                Paragraph(_format_preserving_layout(item["description"]), styles["table_cell"]),
                Paragraph(_escape(item["part_number"]), styles["table_cell_part_number"]),
                Paragraph(_escape(item["hs_code"]), styles["table_cell"]),
                Paragraph(str(item["quantity"]), styles["table_cell"]),
            ]
        )

    if len(rows) == 1:
        rows.append(
            [
                Paragraph("-", styles["table_cell_center"]),
                Paragraph("No items", styles["table_cell"]),
                Paragraph("-", styles["table_cell"]),
                Paragraph("-", styles["table_cell"]),
                Paragraph("-", styles["table_cell"]),
            ]
        )

    table = Table(
        rows,
        colWidths=[width * mm for width in SHIPPING_ITEM_COLUMN_WIDTHS_MM],
        repeatRows=1,
    )
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.white),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.white]),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 1), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 1), (-1, -1), 3),
            ]
        )
    )
    return table


def _build_packing_section(*, invoice, packing_entries, styles):
    specification = getattr(invoice, "packing_specification", "") or "-"
    if not packing_entries and specification == "-":
        return []

    blocks = [
        Paragraph(f"<b>Packing Specification:</b> {_format_preserving_layout(specification)}", styles["body"]),
        Spacer(1, 3 * mm),
    ]

    header_top = [
        Paragraph("Item No", styles["table_head_center"]),
        Paragraph("No Packing", styles["table_head"]),
        Paragraph("Gross/kg", styles["table_head_center"]),
        Paragraph("Net/kg", styles["table_head_center"]),
        Paragraph("L/cm", styles["table_head_center"]),
        Paragraph("W/cm", styles["table_head_center"]),
        Paragraph("H/cm", styles["table_head_center"]),
    ]
    rows = [header_top]

    for entry in packing_entries:
        rows.append(
            [
                Paragraph(str(entry["index"]), styles["table_cell_center"]),
                Paragraph(_escape(entry["no_packing"]), styles["table_cell"]),
                Paragraph(_format_measurement(entry["gross_weight"]), styles["table_cell_center"]),
                Paragraph(_format_measurement(entry["net_weight"]), styles["table_cell_center"]),
                Paragraph(_format_measurement(entry["dimension_length"]), styles["table_cell_center"]),
                Paragraph(_format_measurement(entry["dimension_width"]), styles["table_cell_center"]),
                Paragraph(_format_measurement(entry["dimension_height"]), styles["table_cell_center"]),
            ]
        )

    if len(rows) == 1:
        rows.append(
            [
                Paragraph("-", styles["table_cell_center"]),
                Paragraph("No packing entries", styles["table_cell"]),
                Paragraph("-", styles["table_cell_center"]),
                Paragraph("-", styles["table_cell_center"]),
                Paragraph("-", styles["table_cell_center"]),
                Paragraph("-", styles["table_cell_center"]),
                Paragraph("-", styles["table_cell_center"]),
            ]
        )

    packing_table = Table(
        rows,
        colWidths=[width * mm for width in PACKING_COLUMN_WIDTHS_MM],
        repeatRows=1,
    )
    packing_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.white),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.white]),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    blocks.append(packing_table)
    return blocks


def _build_totals_table(invoice, currency, styles):
    return _build_totals_table_from_values(
        gross_value=invoice.subtotal(),
        freight=invoice.freight,
        vat_amount=invoice.vat_amount(),
        discount=invoice.discount,
        total_amount=invoice.total_amount(),
        currency=currency,
        styles=styles,
    )


def _build_totals_table_from_values(*, gross_value, freight, vat_amount, discount, total_amount, currency, styles, leading_rows=None, extra_rows=None):
    rows = []
    if leading_rows:
        rows.extend([[label, _format_money(value, currency)] for label, value in leading_rows])

    rows.extend([
        ["Total Amount", _format_money(gross_value, currency)],
        ["Freight", _format_money(freight, currency)],
        ["Vat Amount", _format_money(vat_amount, currency)],
    ])
    if _has_amount(discount):
        rows.append(["Discount", _format_money(discount, currency)])
    rows.append(["Grand Total Amount", _format_money(total_amount, currency)])

    if extra_rows:
        rows.extend([[label, _format_money(value, currency)] for label, value in extra_rows])

    total_row_index = len(rows) - 1
    cells = [
        [Paragraph(_escape(label), styles["totals_label"]), Paragraph(_escape(value), styles["body_right"])]
        for label, value in rows
    ]
    table = Table(
        cells,
        colWidths=[TOTALS_LABEL_COLUMN_WIDTH_MM * mm, TOTALS_AMOUNT_COLUMN_WIDTH_MM * mm],
        hAlign="RIGHT",
    )
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, total_row_index), (-1, total_row_index), colors.white),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("LEFTPADDING", (0, 0), (-1, -1), TOTALS_CELL_HORIZONTAL_PADDING),
                ("RIGHTPADDING", (0, 0), (-1, -1), TOTALS_CELL_HORIZONTAL_PADDING),
            ]
        )
    )
    return table


def _build_notes_text(invoice, company):
    notes = []
    invoice_note = getattr(company, "invoice_note", "")
    if invoice_note:
        notes.append(invoice_note)

    terms = getattr(company, "terms_conditions", "")
    if terms:
        notes.append(f"Terms: {terms}")

    delivery = getattr(company, "delivery_time", "")
    if delivery:
        notes.append(f"Delivery: {delivery}")

    footer_invoice = getattr(company, "footer_invoice", "")
    if footer_invoice:
        notes.append(footer_invoice)

    return "<br/><br/>".join(_escape(note) for note in notes if note)


def _build_footer_left_text(invoice, company):
    footer_parts = _clean_lines(
        f"Bank: {company.bank}" if getattr(company, "bank", "") else "",
        f"IBAN: {company.iban}" if getattr(company, "iban", "") else "",
        f"BIC: {company.bic}" if getattr(company, "bic", "") else "",
    )
    return "<br/>".join(_escape(part) for part in footer_parts)


def format_footer_invoice_lines(footer_invoice):
    text = str(footer_invoice or "").strip()
    if not text:
        return []

    parts = [part.strip() for part in text.split("_") if part.strip()]
    if len(parts) != 1 or "_" in text:
        return parts

    match = re.search(r"\d+", parts[0])
    if not match:
        return parts

    first_line = parts[0][: match.start()].strip()
    second_line = parts[0][match.start() :].strip()
    address_match = re.match(r"^(.*\b\d{4,6})\s+([^/]+/\s*[^/]+)$", second_line)
    if address_match:
        address_line = address_match.group(1).strip()
        city_country_line = address_match.group(2).strip()
        return [part for part in (first_line, address_line, city_country_line) if part]

    return [part for part in (first_line, second_line) if part]


def _build_footer_center_text(invoice, company, hide_contact_details=False):
    footer_invoice = getattr(company, "footer_invoice", "") or ""
    parts = format_footer_invoice_lines(footer_invoice)
    if not hide_contact_details:
        return "<br/>".join(_escape(part) for part in parts)

    hidden_parts = _clean_lines(
        getattr(company, "address", ""),
        getattr(company, "company_address", ""),
        getattr(company, "company_phone", ""),
        getattr(company, "company_fax", ""),
        getattr(company, "company_email", ""),
    )
    hidden_parts_lower = [part.lower() for part in hidden_parts]
    filtered_parts = [
        part
        for part in parts
        if not any(hidden_part and hidden_part in part.lower() for hidden_part in hidden_parts_lower)
    ]
    return "<br/>".join(_escape(part) for part in filtered_parts)


def _build_footer_right_text(invoice, company, hide_contact_details=False):
    if hide_contact_details:
        return ""

    footer_parts = _clean_lines(
        f"Telephone: {company.company_phone}" if getattr(company, "company_phone", "") else "",
        f"Fax: {company.company_fax}" if getattr(company, "company_fax", "") else "",
        f"Email: {company.company_email}" if getattr(company, "company_email", "") else "",
    )
    return "<br/>".join(_escape(part) for part in footer_parts)

def _is_commercial_invoice(invoice):
    return invoice.__class__.__name__ == "CommercialInvoice"


def _is_proforma_invoice(invoice):
    return invoice.__class__.__name__ == "ProformaInvoice"


def _is_shipping_document(document_type):
    return document_type in {"packing_list", "dispatching_note"}


def _draw_page_frame(canvas, document, *, company, invoice, invoice_title, currency, page_totals, styles):
    canvas.saveState()

    _draw_page_header(canvas, document, company, invoice, invoice_title, styles)
    _draw_page_footer(canvas, document, invoice, company, styles)
    canvas.restoreState()


def _draw_report_page_frame(canvas, document, *, company, report_title, styles, report_meta=None):
    canvas.saveState()
    left_x = document.leftMargin
    top_y = document.pagesize[1] - 12 * mm

    company_name = Paragraph(_escape(getattr(company, "company_name", "") or "Company"), styles["title"])
    company_name.wrapOn(canvas, 110 * mm, 10 * mm)
    company_name.drawOn(canvas, left_x, top_y - 10 * mm)

    title_style = styles["document_type_title"] if report_title == "COMMAND / ORDER" else styles["section_title"]
    report_type = Paragraph(_escape(_format_pdf_title(report_title)), title_style)
    report_type.wrapOn(canvas, 90 * mm, 8 * mm)
    report_type.drawOn(canvas, left_x, top_y - 18 * mm)

    if report_meta and canvas.getPageNumber() > 1:
        meta = Paragraph("<br/>".join(report_meta), styles["body_small"])
        meta.wrapOn(canvas, 90 * mm, 12 * mm)
        meta.drawOn(canvas, left_x, top_y - 31 * mm)

    logo_path = _get_file_path(getattr(company, "company_logo", None))
    if logo_path and Path(logo_path).exists():
        logo_width = 22 * mm
        logo_height = 14 * mm
        logo_x = document.pagesize[0] - document.rightMargin - logo_width
        logo_y = top_y - 14 * mm
        canvas.drawImage(logo_path, logo_x, logo_y, width=logo_width, height=logo_height, preserveAspectRatio=True, mask="auto")

    _draw_page_footer(
        canvas,
        document,
        None,
        company,
        styles,
        hide_contact_details=False,
    )
    canvas.restoreState()


def _draw_page_header(canvas, document, company, invoice, invoice_title, styles):
    left_x = document.leftMargin
    top_y = document.pagesize[1] - 12 * mm
    page_number = canvas.getPageNumber()

    company_name = Paragraph(_escape(getattr(company, "company_name", "") or "Company"), styles["title"])
    company_name.wrapOn(canvas, 110 * mm, 10 * mm)
    company_name.drawOn(canvas, left_x, top_y - 10 * mm)

    invoice_type = Paragraph(_escape(_format_pdf_title(invoice_title)), styles["section_title"])
    invoice_type.wrapOn(canvas, 80 * mm, 6 * mm)
    invoice_type.drawOn(canvas, left_x, top_y - 18 * mm)

    if page_number > 1:
        invoice_number = Paragraph(
            f"<b>Invoice Number:</b> {_escape(invoice.invoice_number or '-')}",
            styles["body_small"],
        )
        invoice_number.wrapOn(canvas, 80 * mm, 5 * mm)
        invoice_number.drawOn(canvas, left_x, top_y - 23 * mm)

        invoice_date = Paragraph(
            f"<b>Invoice Date:</b> {invoice.invoice_date.strftime('%d/%m/%Y') if invoice.invoice_date else '-'}",
            styles["body_small"],
        )
        invoice_date.wrapOn(canvas, 80 * mm, 5 * mm)
        invoice_date.drawOn(canvas, left_x, top_y - 27 * mm)

    logo_path = _get_file_path(getattr(company, "company_logo", None))
    if logo_path and Path(logo_path).exists():
        logo_width = 22 * mm
        logo_height = 14 * mm
        logo_x = document.pagesize[0] - document.rightMargin - logo_width
        logo_y = top_y - 14 * mm
        canvas.drawImage(logo_path, logo_x, logo_y, width=logo_width, height=logo_height, preserveAspectRatio=True, mask="auto")


def _draw_page_footer(canvas, document, invoice, company, styles, hide_contact_details=False):
    footer_left = _build_footer_left_text(invoice, company)
    footer_center = _build_footer_center_text(invoice, company, hide_contact_details=hide_contact_details)
    footer_right = _build_footer_right_text(invoice, company, hide_contact_details=hide_contact_details)
    if not any([footer_left, footer_center, footer_right]):
        return

    available_width = document.width
    footer_table = Table(
        [[
            Paragraph(footer_left or "", styles["footer_left_small"]),
            Paragraph(footer_center or "", styles["footer_center_small"]),
            Paragraph(footer_right or "", styles["footer_right_small"]),
        ]],
        colWidths=[available_width * 0.3, available_width * 0.4, available_width * 0.3],
    )
    footer_table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ALIGN", (0, 0), (0, 0), "LEFT"),
                ("ALIGN", (1, 0), (1, 0), "CENTER"),
                ("ALIGN", (2, 0), (2, 0), "RIGHT"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    footer_width, footer_height = footer_table.wrap(document.width, 12 * mm)
    footer_table.drawOn(canvas, document.leftMargin, 11 * mm)


def _build_page_totals_flowable(page_index, invoice, currency, styles, page_totals):
    if not page_totals:
        return Spacer(1, 0)

    summary = page_totals[page_index]
    is_last_page = page_index == len(page_totals) - 1
    detail_table = _build_page_gross_value_table(
        page_number=page_index + 1,
        total_pages=len(page_totals),
        page_amount=summary["page_item_total"],
        subtotal_amount=summary["cumulative_gross_value"],
        currency=currency,
        styles=styles,
    )

    if is_last_page:
        totals_table = _build_totals_table_from_values(
            gross_value=summary["all_pages_gross_value"],
            freight=Decimal(getattr(invoice, "freight", 0) or 0),
            vat_amount=(summary["all_pages_gross_value"] * Decimal(getattr(invoice, "vat_percent", 0) or 0)) / Decimal("100"),
            discount=Decimal(getattr(invoice, "discount", 0) or 0),
            total_amount=summary["all_pages_total"],
            currency=currency,
            styles=styles,
        )
        if len(page_totals) == 1:
            return totals_table
        return totals_table

    return detail_table


def _format_money(value, currency):
    return f"{_format_decimal_comma(value)} {format_currency_symbol(currency)}"


def format_currency_symbol(currency):
    symbols = {
        "EUR": "\u20ac",
        "USD": "$",
        "CNY": "\u00a5",
        "MAD": "DH",
        "LBP": "L\u00a3",
        "IRR": "Rls",
    }
    code = str(currency or "").upper()
    return symbols.get(code, str(currency or ""))


def _format_currency_symbol(currency):
    return format_currency_symbol(currency)


def _format_decimal_comma(value):
    amount = Decimal(value or 0).quantize(Decimal("0.01"))
    return f"{amount:,.2f}"


def _build_money_split_cell(value, currency, styles, width, bold=False):
    total_width = max(width, 25 * mm)
    amount_text = f"{_format_decimal_comma(value)} {_escape(_format_currency_symbol(currency))}"
    if bold:
        amount_text = f"<b>{amount_text}</b>"
    table = Table(
        [[
            Paragraph(amount_text, styles["table_cell_amount"]),
        ]],
        colWidths=[total_width],
        hAlign="LEFT",
    )
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    return table


def _build_purchase_order_total_money_cell(value, currency, styles):
    table = Table(
        [[
            Paragraph(f"<b>{_format_decimal_comma(value)} {_escape(_format_currency_symbol(currency))}</b>", styles["table_cell_amount"]),
        ]],
        colWidths=[43 * mm],
        hAlign="LEFT",
    )
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    return table


def _has_amount(value):
    return Decimal(value or 0).quantize(Decimal("0.01")) != Decimal("0.00")


def _format_plain_decimal(value):
    return _format_decimal_comma(value)


def _format_quantity(value):
    amount = Decimal(value or 0)
    if amount == amount.to_integral_value():
        return f"{amount:,.0f}"
    return f"{amount:,.3f}".rstrip("0").rstrip(".")


def _format_measurement(value):
    return _format_quantity(value)


def _format_weight_kg(value):
    return _format_measurement(value)


def _clean_lines(*values):
    lines = []
    for value in values:
        if not value:
            continue
        for line in str(value).splitlines():
            cleaned = line.strip()
            if cleaned:
                lines.append(cleaned)
    return lines


def _get_file_path(field_file):
    if not field_file:
        return ""
    try:
        return field_file.path
    except (AttributeError, OSError, ValueError):
        return ""


def _escape(value):
    return (
        str(value or "-")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _normalize_pdf_text(value):
    return re.sub(r"[ \t]+", " ", str(value or "")).strip()


def _format_pdf_title(value):
    return str(value or "").strip().title()


def _format_preserving_layout(value):
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").expandtabs(4)
    escaped = _escape(text)
    escaped = re.sub(
        r" {2,}",
        lambda match: ("&nbsp;" * (len(match.group(0)) - 1)) + " ",
        escaped,
    )
    # ReportLab collapses a lone space at a line boundary unless it is
    # non-breaking. Preserve that indentation/trailing spacing as entered.
    escaped = re.sub(r"(?m)^ ", "&nbsp;", escaped)
    escaped = re.sub(r" (?=\n|$)", "&nbsp;", escaped)
    # Product descriptions use a deliberately small, safe formatting syntax.
    # Apply it only after HTML escaping so user-entered tags stay harmless.
    escaped = re.sub(r"__([^\n]+?)__", r"<u>\1</u>", escaped)
    escaped = re.sub(r"\*\*([^\n]+?)\*\*", r"<b>\1</b>", escaped)
    return escaped.replace("\n", "<br/>")


def _compute_page_totals(*, invoice, item_pages):
    if not item_pages:
        return []

    results = []
    cumulative_gross_value = Decimal("0.00")
    vat_percent = getattr(invoice, "vat_percent", Decimal("0.00")) or Decimal("0.00")
    all_pages_gross_value = sum(
        (sum((Decimal(item["total_amount"]) for item in page_items), Decimal("0.00")) for page_items in item_pages),
        Decimal("0.00"),
    )
    all_pages_total = all_pages_gross_value + (all_pages_gross_value * Decimal(vat_percent)) / Decimal("100") + Decimal(getattr(invoice, "freight", 0) or 0) - Decimal(getattr(invoice, "discount", 0) or 0)
    page_gross_values = []
    page_cumulative_gross_values = []

    for page_items in item_pages:
        page_total = sum((Decimal(item["total_amount"]) for item in page_items), Decimal("0.00"))
        page_gross_values.append(page_total)
        cumulative_gross_value += page_total
        page_cumulative_gross_values.append(cumulative_gross_value)

        gross_value = cumulative_gross_value
        freight = Decimal(getattr(invoice, "freight", 0) or 0)
        discount = Decimal(getattr(invoice, "discount", 0) or 0)

        vat_amount = (gross_value * Decimal(vat_percent)) / Decimal("100")
        total_amount = gross_value + vat_amount + freight - discount

        results.append(
            {
                "gross_value": gross_value,
                "freight": freight,
                "vat_amount": vat_amount,
                "discount": discount,
                "total_amount": total_amount,
                "page_item_total": page_total,
                "cumulative_gross_value": cumulative_gross_value,
                "all_pages_gross_value": all_pages_gross_value,
                "all_pages_total": all_pages_total,
                "page_gross_values": list(page_gross_values),
                "page_cumulative_gross_values": list(page_cumulative_gross_values),
            }
        )

    return results


def _build_page_gross_value_table(*, page_number, total_pages, page_amount, subtotal_amount, currency, styles):
    label = _page_gross_value_label(page_number, total_pages)
    value = page_amount if page_number == 1 else subtotal_amount
    rows = [[
        Paragraph(_escape(label), styles["table_head_amount"]),
        "",
        "",
        "",
        "",
        "",
        Paragraph(_format_money(value, currency), styles["body_right"]),
    ]]
    table = Table(
        rows,
        colWidths=[10 * mm, 50 * mm, 31 * mm, 22 * mm, 12 * mm, 27 * mm, 28 * mm],
    )
    table.setStyle(
        TableStyle(
            [
                ("SPAN", (0, 0), (5, 0)),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return table


def _page_gross_value_label(page_number, total_pages):
    if page_number == 1:
        return f"Amount of Page 1 of {total_pages}"
    return f"Sub Total of Page {page_number} of {total_pages}"


def _build_report_summary_table(*, currency, total_qty, total_subtotal, total_vat, total_freight, total_discount, total_amount, styles):
    rows = [
        ["Total Quantity", str(total_qty)],
        ["Subtotal", _format_money(total_subtotal, currency)],
        ["VAT", _format_money(total_vat, currency)],
        ["Freight", _format_money(total_freight, currency)],
        ["Discount", _format_money(total_discount, currency)],
        ["Total Amount", _format_money(total_amount, currency)],
    ]
    cells = [
        [Paragraph(_escape(label), styles["totals_label"]), Paragraph(_escape(value), styles["body_right"])]
        for label, value in rows
    ]
    table = Table(cells, colWidths=[40 * mm, 40 * mm], hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 5), (-1, 5), colors.white),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    return table


def _build_purchase_report_summary_table(*, currency, total_qty, total_gross, total_vat, total_freight, total_amount, styles):
    rows = [
        ["Total Quantity", str(total_qty)],
        ["Gross Value", _format_money(total_gross, currency)],
        ["VAT", _format_money(total_vat, currency)],
        ["Freight", _format_money(total_freight, currency)],
        ["Total Amount", _format_money(total_amount, currency)],
    ]
    cells = [
        [Paragraph(_escape(label), styles["totals_label"]), Paragraph(_escape(value), styles["body_right"])]
        for label, value in rows
    ]
    table = Table(cells, colWidths=[40 * mm, 40 * mm], hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 4), (-1, 4), colors.white),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    return table


def _build_report_histogram(*, chart_labels, chart_totals, title):
    if not chart_labels or not chart_totals:
        return None

    drawing = Drawing(180 * mm, 78 * mm)
    chart = VerticalBarChart()
    chart.x = 18 * mm
    chart.y = 12 * mm
    chart.height = 48 * mm
    chart.width = 150 * mm
    chart.data = [chart_totals]
    chart.categoryAxis.categoryNames = [str(label) for label in chart_labels]
    chart.categoryAxis.labels.angle = 0
    chart.categoryAxis.labels.boxAnchor = "n"
    chart.categoryAxis.labels.fontName = PDF_FONT_REGULAR
    chart.categoryAxis.labels.fontSize = 9.5
    chart.categoryAxis.labels.dy = -6
    chart.valueAxis.labels.fontName = PDF_FONT_REGULAR
    chart.valueAxis.labels.fontSize = 9.5
    chart.valueAxis.valueMin = 0
    chart.valueAxis.visibleGrid = True
    chart.valueAxis.gridStrokeColor = colors.HexColor("#D9D9D9")
    chart.valueAxis.gridStrokeWidth = 0.5
    chart.categoryAxis.strokeColor = colors.HexColor("#CFCFCF")
    chart.valueAxis.strokeColor = colors.HexColor("#CFCFCF")
    chart.categoryAxis.labels.fillColor = colors.HexColor("#5F6368")
    chart.valueAxis.labels.fillColor = colors.HexColor("#5F6368")
    chart.groupSpacing = 0
    chart.barSpacing = 0

    palette = [
        (colors.HexColor("#5C9F7D"), colors.HexColor("#3E7A5D")),
        (colors.HexColor("#648FAE"), colors.HexColor("#416C8B")),
        (colors.HexColor("#EDCB73"), colors.HexColor("#C5A24A")),
        (colors.HexColor("#F59E0B"), colors.HexColor("#D97706")),
        (colors.HexColor("#B78EC8"), colors.HexColor("#8A63A0")),
        (colors.HexColor("#57A39C"), colors.HexColor("#347D76")),
    ]
    for index, _ in enumerate(chart_totals):
        fill, stroke = palette[index % len(palette)]
        chart.bars[(0, index)].fillColor = fill
        chart.bars[(0, index)].strokeColor = stroke
        chart.bars[(0, index)].strokeWidth = 0.8

    drawing.add(chart)
    drawing.add(String(0, 70 * mm, title, fontName=PDF_FONT_BOLD, fontSize=15, fillColor=colors.HexColor(PDF_ACCENT_HEX)))
    drawing.add(String(0, 64 * mm, "Metric: Total Amount", fontName=PDF_FONT_BOLD, fontSize=10, fillColor=colors.HexColor("#5F6368")))
    drawing.add(String(90 * mm, 4 * mm, "Month", fontName=PDF_FONT_REGULAR, fontSize=10, fillColor=colors.HexColor("#5F6368"), textAnchor="middle"))
    return drawing


def _build_commercial_report_chart(chart_labels, chart_totals):
    return _build_report_histogram(
        chart_labels=chart_labels,
        chart_totals=chart_totals,
        title="Monthly Commercial Chart",
    )


def _build_commercial_report_table(*, invoices, currency, styles):
    rows = [[
        Paragraph("Date", styles["table_head"]),
        Paragraph("Number", styles["table_head"]),
        Paragraph("Importer", styles["table_head"]),
        Paragraph("End User", styles["table_head"]),
        Paragraph("Qty", styles["table_head_amount"]),
        Paragraph("Subtotal", styles["table_head_amount"]),
        Paragraph("VAT", styles["table_head_amount"]),
        Paragraph("Freight", styles["table_head_amount"]),
        Paragraph("Discount", styles["table_head_amount"]),
        Paragraph("Total", styles["table_head_amount"]),
    ]]

    for invoice in invoices:
        rows.append(
            [
                Paragraph(invoice.invoice_date.strftime("%d/%m/%Y") if invoice.invoice_date else "-", styles["table_cell"]),
                Paragraph(_escape(invoice.invoice_number or "-"), styles["table_cell"]),
                Paragraph(_escape(getattr(invoice.importer, "description", "-") or "-"), styles["table_cell"]),
                Paragraph(_escape(getattr(invoice.end_user, "description", "-") or "-"), styles["table_cell"]),
                Paragraph(_escape(invoice.qty_total or 0), styles["table_cell_right"]),
                Paragraph(_format_money(getattr(invoice, "subtotal_db", 0), currency), styles["table_cell_right"]),
                Paragraph(_format_money(invoice.vat_amount(), currency), styles["table_cell_right"]),
                Paragraph(_format_money(invoice.freight, currency), styles["table_cell_right"]),
                Paragraph(_format_money(invoice.discount, currency), styles["table_cell_right"]),
                Paragraph(_format_money(invoice.total_amount(), currency), styles["table_cell_right"]),
            ]
        )

    if len(rows) == 1:
        rows.append([Paragraph("-", styles["table_cell"])] * 10)

    table = Table(
        rows,
        colWidths=[17 * mm, 21 * mm, 22 * mm, 22 * mm, 8 * mm, 18 * mm, 18 * mm, 18 * mm, 18 * mm, 18 * mm],
        hAlign="LEFT",
        repeatRows=1,
    )
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.white),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.white]),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    return table


def _build_purchase_report_table(*, purchase_orders, currency, styles):
    rows = [[
        Paragraph("Date", styles["table_head"]),
        Paragraph("Number", styles["table_head"]),
        Paragraph("Seller", styles["table_head"]),
        Paragraph("Requester", styles["table_head"]),
        Paragraph("Qty", styles["table_head_amount"]),
        Paragraph("Gross", styles["table_head_amount"]),
        Paragraph("VAT", styles["table_head_amount"]),
        Paragraph("Freight", styles["table_head_amount"]),
        Paragraph("Total", styles["table_head_amount"]),
    ]]

    for po in purchase_orders:
        rows.append(
            [
                Paragraph(po.purchase_date.strftime("%d/%m/%Y") if po.purchase_date else "-", styles["table_cell"]),
                Paragraph(_escape(po.purchase_number or "-"), styles["table_cell"]),
                Paragraph(_escape(getattr(po.seller, "description", "-") or "-"), styles["table_cell"]),
                Paragraph(_escape(getattr(po.requester, "description", "-") or "-"), styles["table_cell"]),
                Paragraph(_escape(po.qty_total or 0), styles["table_cell_center"]),
                Paragraph(_format_money(getattr(po, "gross_value_db", 0), currency), styles["table_cell_right"]),
                Paragraph(_format_money(po.vat_amount(), currency), styles["table_cell_right"]),
                Paragraph(_format_money(po.freight, currency), styles["table_cell_right"]),
                Paragraph(_format_money(po.total_amount(), currency), styles["table_cell_right"]),
            ]
        )

    if len(rows) == 1:
        rows.append([Paragraph("-", styles["table_cell"])] * 9)

    table = Table(
        rows,
        colWidths=[18 * mm, 27 * mm, 29 * mm, 29 * mm, 8 * mm, 18 * mm, 18 * mm, 18 * mm, 19 * mm],
        hAlign="LEFT",
        repeatRows=1,
    )
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.white),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.white]),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    return table


def _build_purchase_order_items_table(items, currency, styles, amount_from_last_page=None):
    styles = _build_invoice_item_table_styles(styles)
    rows = [[
        Paragraph("Item", styles["table_head"]),
        Paragraph("Description", styles["table_head"]),
        Paragraph("Part Number", styles["table_head"]),
        Paragraph("HS Code", styles["table_head"]),
        Paragraph("Qty", styles["table_head_amount"]),
        Paragraph("Unit Price", styles["table_head_amount"]),
        Paragraph("Total Amount", styles["table_head_amount"]),
        Paragraph("VAT", styles["table_head_amount"]),
    ]]

    if amount_from_last_page is not None:
        rows.append(
            [
                Paragraph("", styles["table_cell"]),
                Paragraph("Amount from Last Page", styles["table_cell"]),
                Paragraph("", styles["table_cell"]),
                Paragraph("", styles["table_cell"]),
                Paragraph("", styles["table_cell_amount"]),
                Paragraph("", styles["table_cell_amount"]),
                Paragraph(_format_money(amount_from_last_page, currency), styles["table_cell_amount"]),
                Paragraph("", styles["table_cell_amount"]),
            ]
        )

    for item in items:
        vat_amount = (Decimal(item["total_amount"]) * Decimal(item.get("vat_percent", 0) or 0)) / Decimal("100")
        rows.append(
            [
                Paragraph(str(item["index"]), styles["table_cell"]),
                Paragraph(_format_preserving_layout(item["description"]), styles["table_cell"]),
                Paragraph(_escape(item["part_number"]), styles["table_cell_part_number"]),
                Paragraph(_escape(item["hs_code"]), styles["table_cell"]),
                Paragraph(_format_quantity(item["quantity"]), styles["table_cell_amount"]),
                _build_money_split_cell(item["unit_price"], currency, styles, 18 * mm),
                _build_money_split_cell(item["total_amount"], currency, styles, 17 * mm),
                Paragraph(_format_money(vat_amount, currency), styles["table_cell_amount"]),
            ]
        )

    if len(rows) == 1:
        rows.append([Paragraph("-", styles["table_cell"])] * 8)

    table = Table(
        rows,
        colWidths=[12 * mm, 42 * mm, 29 * mm, 22 * mm, 9 * mm, 25 * mm, 27 * mm, 18 * mm],
        hAlign="LEFT",
        repeatRows=1,
    )
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.white),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (4, 0), (-1, -1), "RIGHT"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.white]),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("LEFTPADDING", (3, 0), (3, -1), 2),
                ("RIGHTPADDING", (3, 0), (3, -1), 2),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (1, 1), (1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 1), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 1), (-1, -1), 3),
                ("LEFTPADDING", (4, 0), (-1, -1), 2),
                ("RIGHTPADDING", (4, 0), (-1, -1), 4),
            ]
        )
    )
    return table


def _compute_purchase_order_page_totals(*, purchase_order, item_pages):
    if not item_pages:
        return []

    results = []
    cumulative_gross = Decimal("0.00")
    vat_percent = getattr(purchase_order, "vat_percent", Decimal("0.00")) or Decimal("0.00")
    all_pages_gross = sum(
        (sum((Decimal(item["total_amount"]) for item in page_items), Decimal("0.00")) for page_items in item_pages),
        Decimal("0.00"),
    )
    all_pages_total = all_pages_gross + (all_pages_gross * Decimal(vat_percent)) / Decimal("100") + Decimal(getattr(purchase_order, "freight", 0) or 0)
    page_gross_values = []

    for page_items in item_pages:
        page_total = sum((Decimal(item["total_amount"]) for item in page_items), Decimal("0.00"))
        page_gross_values.append(page_total)
        cumulative_gross += page_total
        vat_amount = (cumulative_gross * Decimal(vat_percent)) / Decimal("100")
        total_amount = cumulative_gross + vat_amount + Decimal(getattr(purchase_order, "freight", 0) or 0)
        results.append(
            {
                "page_gross_values": list(page_gross_values),
                "all_pages_gross": all_pages_gross,
                "all_pages_total": all_pages_total,
                "gross_value": cumulative_gross,
                "vat_amount": vat_amount,
                "total_amount": total_amount,
            }
        )
    return results


def _build_purchase_order_totals_flowable(page_index, purchase_order, currency, styles, page_totals):
    if not page_totals:
        return Spacer(1, 0)

    summary = page_totals[page_index]
    is_last_page = page_index == len(page_totals) - 1
    detail_table = _build_purchase_order_page_gross_values_table(
        page_index + 1,
        summary["gross_value"],
        len(page_totals),
        currency,
        styles,
    )
    if is_last_page:
        totals_table = _build_purchase_order_payment_table(
            gross_value=summary["all_pages_gross"],
            vat_amount=(summary["all_pages_gross"] * Decimal(getattr(purchase_order, "vat_percent", 0) or 0)) / Decimal("100"),
            total_amount=summary["all_pages_total"],
            currency=currency,
            styles=styles,
        )
        if len(page_totals) == 1:
            return totals_table
        return totals_table
    return detail_table


def _build_purchase_order_page_gross_values_table(page_number, page_gross_value, total_pages, currency, styles):
    label = (
        f"Amount of Page 1 of {total_pages}"
        if page_number == 1
        else f"Sub Total of Page {page_number} of {total_pages}"
    )
    rows = [[
        Paragraph(_escape(label), styles["table_head_amount"]),
        "",
        "",
        "",
        "",
        "",
        Paragraph(_format_money(page_gross_value, currency), styles["body_right"]),
        "",
    ]]
    table = Table(
        rows,
        colWidths=[10 * mm, 44 * mm, 29 * mm, 22 * mm, 9 * mm, 25 * mm, 27 * mm, 18 * mm],
        hAlign="LEFT",
    )
    table.setStyle(
        TableStyle(
            [
                ("SPAN", (0, 0), (5, 0)),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return table


def _build_purchase_order_payment_table(*, gross_value, vat_amount, total_amount, currency, styles):
    rows = [[
        "",
        "",
        "",
        "",
        Paragraph("<b>Total Payment</b>", styles["table_cell_center"]),
        "",
        Paragraph(f"<b>{_format_money(gross_value, currency)}</b>", styles["table_cell_amount"]),
        Paragraph(f"<b>{_format_money(vat_amount, currency)}</b>", styles["table_cell_amount"]),
    ]]
    table = Table(
        rows,
        colWidths=[10 * mm, 44 * mm, 29 * mm, 22 * mm, 9 * mm, 25 * mm, 27 * mm, 18 * mm],
        hAlign="LEFT",
    )
    table.setStyle(
        TableStyle(
            [
                ("SPAN", (4, 0), (5, 0)),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (4, 0), (5, 0), "CENTER"),
                ("ALIGN", (6, 0), (-1, -1), "RIGHT"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("LEFTPADDING", (4, 0), (-1, -1), 2),
                ("RIGHTPADDING", (4, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return table


def _build_purchase_order_signature_block(purchase_order, company, styles):
    footer_lines = []
    footer_order = getattr(company, "footer_order", "") or ""
    for line in str(footer_order).splitlines():
        clean = line.strip()
        if clean:
            footer_lines.append(Paragraph(_escape(clean), styles["body_small"]))
    if not footer_lines:
        footer_lines = [Spacer(1, 0)]

    president = getattr(company, "president", "") or "-"
    signature = [
        Paragraph(_escape(president), styles["body_right"]),
        Paragraph("President", styles["body_right"]),
        Spacer(1, 5 * mm),
        Paragraph(
            f"Date: {purchase_order.purchase_date.strftime('%d-%b-%Y') if purchase_order.purchase_date else '-'}",
            styles["body_right"],
        ),
    ]
    table = Table([[footer_lines, signature]], colWidths=[118 * mm, 66 * mm], hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    return [table]




if pdf_canvas is not None:
    class NumberedCanvas(pdf_canvas.Canvas):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._saved_page_states = []
            self.page_count = 0

        def showPage(self):
            self._saved_page_states.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            self._saved_page_states.append(dict(self.__dict__))
            page_states = list(self._saved_page_states)
            total_pages = max(1, len(page_states) - 1)
            self.page_count = total_pages

            for index, state in enumerate(page_states[:-1], start=1):
                self.__dict__.update(state)
                self.page_count = total_pages
                self.setFont(PDF_FONT_REGULAR, 9)
                self.setFillColor(colors.HexColor("#6C7682"))
                self.drawCentredString(
                    self._pagesize[0] / 2,
                    8 * mm,
                    f"page {index} of {total_pages}",
                )
                super().showPage()
            super().save()

