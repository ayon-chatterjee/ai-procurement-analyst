"""Generate a 30-line stress dataset: same messy formats, ten times the line items.

The point is not more of the same. At 30 lines the failure modes change: partial
coverage becomes the norm, line matching has many more chances to go wrong, and the
comparison has to stay readable. Coverage, units, currencies and gaps all vary by
supplier on purpose.

Run:  python3 scripts/make_stress_fixtures.py
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from scripts.make_supplier_fixtures import write_docx, write_pdf  # noqa: E402

OUT = os.path.join(ROOT, "fixtures", "suppliers", "stress")

#: 30 carton sizes, deliberately including near-duplicates so matching has to work
#: on the actual numbers rather than on rough similarity.
SIZES = [
    "6 x 6 x 4", "8 x 6 x 4", "8 x 8 x 6", "10 x 8 x 6", "10 x 10 x 5", "10 x 10 x 8",
    "12 x 9 x 6", "12 x 10 x 6", "12 x 10 x 8", "12 x 12 x 8", "14 x 10 x 6", "14 x 12 x 10",
    "15 x 10 x 8", "16 x 12 x 8", "16 x 16 x 12", "18 x 12 x 10", "18 x 14 x 12", "18 x 18 x 14",
    "20 x 15 x 10", "20 x 16 x 14", "20 x 20 x 16", "22 x 16 x 12", "24 x 18 x 12", "24 x 20 x 16",
    "26 x 20 x 14", "28 x 22 x 18", "30 x 20 x 15", "30 x 24 x 20", "32 x 24 x 18", "36 x 28 x 22",
]
QTY = 1500


def _price(i: int, base: float) -> float:
    return round(base + i * 0.043, 2)


def supplier_a() -> str:
    """Complete coverage, clean spreadsheet, per piece."""
    from openpyxl import Workbook
    from openpyxl.styles import Font
    wb = Workbook(); ws = wb.active; ws.title = "Quotation"
    ws["A1"] = "ANHUI PACKAGING CO., LTD"; ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = "Quotation Ref: AP-2026-0501 (30 line enquiry)"
    for col, h in enumerate(["Line", "Description", "Qty", "Unit", "Unit Price (USD)"], start=1):
        ws.cell(row=4, column=col, value=h).font = Font(bold=True)
    for i, size in enumerate(SIZES):
        r = 5 + i
        ws.cell(row=r, column=1, value=i + 1)
        ws.cell(row=r, column=2, value="%s inch corrugated carton" % size)
        ws.cell(row=r, column=3, value=QTY)
        ws.cell(row=r, column=4, value="pcs")
        ws.cell(row=r, column=5, value=_price(i, 0.31))
    base = 5 + len(SIZES) + 1
    ws.cell(row=base, column=1, value="Commercial Terms").font = Font(bold=True)
    ws.cell(row=base + 1, column=1, value="Minimum order quantity"); ws.cell(row=base + 1, column=2, value="1,500 pcs per size")
    ws.cell(row=base + 2, column=1, value="Lead time"); ws.cell(row=base + 2, column=2, value="21 days from artwork approval")
    ws.cell(row=base + 3, column=1, value="Payment"); ws.cell(row=base + 3, column=2, value="30% advance, 70% against B/L")
    ws.cell(row=base + 4, column=1, value="Price basis"); ws.cell(row=base + 4, column=2, value="FOB Shanghai")
    ws.cell(row=base + 5, column=1, value="Validity"); ws.cell(row=base + 5, column=2, value="30 days")
    ws.cell(row=base + 7, column=1, value="ISO 9001:2015"); ws.cell(row=base + 7, column=2, value="Cert CN-ISO-884512, expires 2027-03-14")
    for col, w in (("A", 26), ("B", 46), ("C", 10), ("D", 8), ("E", 18)):
        ws.column_dimensions[col].width = w
    path = os.path.join(OUT, "stress_a_quote.xlsx"); wb.save(path); return path


def supplier_b() -> str:
    """Per-1000 pricing this time, discount footnote, skips five sizes, lead-time conflict."""
    skip = {4, 11, 18, 25, 29}
    page1 = ["SHENZHEN PRINT & PACK LIMITED",
             "Quotation SZPP/Q/2026/2210      Date: 20 September 2026", "",
             "All prices are quoted PER 1000 PIECES, EXW Shenzhen.", "",
             "Item   Size (inch)        Qty        Price per 1000 pcs (USD)"]
    for i, size in enumerate(SIZES):
        if i in skip:
            continue
        page1.append("%-6d %-18s %-10s %.2f" % (i + 1, size, "1500 pcs", _price(i, 0.29) * 1000))
    page1 += ["", "Sizes 5, 12, 19, 26 and 30 are not offered: tooling unavailable.",
              "Minimum order quantity: 4,000 pieces per size.",
              "Production lead time: 18 days after approved artwork."]
    page2 = ["SHENZHEN PRINT & PACK LIMITED - page 2", "",
             "Board: 3-ply B-flute, 125gsm kraft liner. Printing: up to 2 colours included.",
             "Quotation valid 21 days from issue.", "", "Notes:",
             "1. Prices exclude pallets.",
             "2. 7% discount applicable for total order quantities above 20,000 pcs.",
             "3. Artwork changes after approval are chargeable."]
    page3 = ["SHENZHEN PRINT & PACK LIMITED - page 3", "", "Quality and compliance", "",
             "We are ISO 9001 certified. FSC certification is in progress.", "",
             "Please note production lead time of 30 days should be allowed for orders",
             "placed during the October to December peak season.", "",
             "For and on behalf of Shenzhen Print & Pack Limited"]
    return write_pdf(os.path.join(OUT, "stress_b_quote.pdf"), [page1, page2, page3])


def supplier_c() -> str:
    """Prose, sparse coverage, one per-kg line, conditional validity, a question back."""
    picks = [0, 4, 7, 12, 15, 22, 26]
    paras = ["VIET CARTON JOINT STOCK COMPANY",
             "Response to your 30-size enquiry - 21 September 2026", "",
             "Dear Procurement team,",
             "Thank you for the extensive enquiry. Our die inventory covers only part of your list, "
             "so we are able to offer the following sizes."]
    for i in picks[:-1]:
        paras.append("For the %s carton our price is USD %.2f per piece at 1,500 units." % (SIZES[i], _price(i, 0.33)))
    last = picks[-1]
    paras += [
        "For the %s size our mill prices by weight: USD 2.60 per kg of finished carton. "
        "A per piece figure can follow once the board grade is fixed." % SIZES[last],
        "",
        "We cannot quote the remaining sizes in this round.",
        "Minimum order quantity is 1,500 pieces per size. Payment 40% deposit, balance against documents. "
        "Delivery FOB Haiphong. Lead time approximately 20 days.",
        "",
        "This quotation is valid subject to kraft paper prices, which remain volatile.",
        "",
        "One question: should the larger sizes above 24 inches use double-wall board? "
        "This materially changes our pricing.",
        "",
        "We hold ISO 9001 certification; a copy can be provided on request.",
        "", "Best regards,", "Nguyen Thi Mai, Export Sales"]
    return write_docx(os.path.join(OUT, "stress_c_response.docx"), paras)


def supplier_d() -> str:
    """Email referring to sizes by position, prices in rupees, many gaps."""
    picks = [1, 3, 6, 9, 14, 20, 23, 27]
    lines = ["From: sales@gujaratboxes.in", "To: procurement@buyer.example",
             "Subject: Re: RFQ 30 carton sizes", "Date: Mon, 21 Sep 2026 11:40:12 +0530", "",
             "Hi,", "", "We can offer the following from your list:"]
    for i in picks:
        lines.append("  item %d  -  INR %.2f per piece" % (i + 1, _price(i, 0.30) * 84))
    lines += ["", "MOQ 1000 each. Lead time 24 days. Ex works Ahmedabad.",
              "ISO available. We cannot offer the remaining sizes.", "",
              "Regards,", "Rakesh Patel", "Gujarat Boxes Pvt Ltd"]
    path = os.path.join(OUT, "stress_d_response.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


def supplier_e() -> str:
    """Photographed quotation in EUR, a subset of sizes, deliberately imperfect."""
    from PIL import Image, ImageDraw, ImageFilter
    picks = list(range(0, 30, 2))[:12]
    W, H = 1240, 1500
    canvas = Image.new("RGB", (W, H), (231, 229, 225))
    page = Image.new("RGB", (1020, 1340), (253, 252, 249))
    d = ImageDraw.Draw(page)

    def line(x, y, t, bold=False):
        d.text((x, y), t, fill=(20, 20, 24))
        if bold:
            d.text((x + 1, y), t, fill=(20, 20, 24))

    line(40, 30, "ISTANBUL AMBALAJ SANAYI A.S.", True)
    line(40, 54, "Teklif / Quotation No: IA-2026-1120      Tarih: 22.09.2026")
    d.line([(40, 78), (980, 78)], fill=(90, 90, 95), width=2)
    line(40, 96, "QUOTATION - CORRUGATED CARTONS       All prices in EUR per piece", True)
    line(40, 126, "No.    Size (inch)            Qty         Unit Price (EUR)")
    d.line([(40, 144), (760, 144)], fill=(150, 150, 155), width=1)
    y = 162
    for i in picks:
        line(46, y, str(i + 1))
        line(120, y, SIZES[i])
        line(330, y, str(QTY))
        line(470, y, "%.2f" % (_price(i, 0.27) * 0.92))
        y += 28
    y += 16
    line(40, y, "Sizes not listed above are not quoted in this round.")
    line(40, y + 28, "MOQ: 1500 pcs per size")
    line(40, y + 52, "Lead time: 26 days after order confirmation")
    line(40, y + 76, "Payment: 50% advance, 50% before dispatch")
    line(40, y + 100, "Delivery: FOB Istanbul        Validity: 20 days")
    line(40, y + 136, "ISO 9001 certificate no. TR-9001-4471 attached.")
    line(40, y + 172, "Emre Yilmaz - Sales Manager")

    page = page.rotate(-1.7, expand=True, fillcolor=(231, 229, 225))
    canvas.paste(page, (110, 80))
    canvas = canvas.filter(ImageFilter.GaussianBlur(0.55))
    shade = Image.linear_gradient("L").resize((W, H)).point(lambda v: 220 + v // 9)
    canvas = Image.composite(canvas, Image.new("RGB", (W, H), (255, 255, 255)), shade)
    path = os.path.join(OUT, "stress_e_quote.png"); canvas.save(path, "PNG"); return path


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    for p in (supplier_a(), supplier_b(), supplier_c(), supplier_d(), supplier_e()):
        print("  %-30s %8d bytes" % (os.path.basename(p), os.path.getsize(p)))
    print("\n30-line stress fixtures written to %s" % OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
