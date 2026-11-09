import io

from pypdf import PdfReader
from reportlab.pdfgen import canvas

from pipeline.cards import results_pages


def pdf(pages: list[str]) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    for text in pages:
        for n, line in enumerate(text.split("\n")):
            c.drawString(72, 750 - 14 * n, line)
        c.showPage()
    c.save()
    return buf.getvalue()


def test_finds_table_past_the_cover_pages():
    pages = ["Cover letter to the exchange"] * 9 + [
        "Statement of consolidated results\nQuarter ended 30.09.2026\nRevenue from operations 1,200\nProfit before tax 200",
        "Notes to the results",
        "Statement of standalone results\nQuarter ended\nRevenue from operations\nProfit before tax",
    ]
    excerpt, picked = results_pages(pdf(pages))
    assert picked[0] == 10  # 1-based: the consolidated table
    assert 11 in picked and len(PdfReader(io.BytesIO(excerpt)).pages) == len(picked)


def test_falls_back_to_first_pages_without_a_text_layer():
    _, picked = results_pages(pdf(["scan"] * 12))
    assert picked == list(range(1, 9))
