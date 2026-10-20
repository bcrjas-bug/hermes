"""Comprehensive edge-case and robustness tests for the Metadata Scrubber."""

import io
import shutil
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

import pypdf
from PIL import Image, PngImagePlugin

from engine import (
    detect_file_type,
    inspect_metadata,
    scrub_bytes,
    scrub_directory_inplace,
    scrub_file_inplace,
)


class TestScrubberEdgeCases(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="scrubber_edge_")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_file_type_detection(self):
        self.assertEqual(detect_file_type(b"\x89PNG\r\n\x1a\n\x00\x00"), "PNG")
        self.assertEqual(detect_file_type(b"\xff\xd8\xff\xe0\x00\x10JFIF"), "JPEG")
        self.assertEqual(detect_file_type(b"%PDF-1.7\n%abc"), "PDF")
        # PDF with leading whitespace / comment
        self.assertEqual(detect_file_type(b"\r\n\xef\xbb\xbf%PDF-2.0\n"), "PDF")
        # Extension fallback
        self.assertEqual(detect_file_type(b"corrupt", "photo.jpg"), "JPEG")
        self.assertEqual(detect_file_type(b"corrupt", "photo.png"), "PNG")
        self.assertEqual(detect_file_type(b"corrupt", "doc.pdf"), "PDF")
        self.assertIsNone(detect_file_type(b"hello world", "doc.txt"))

    def test_multi_page_pdf_with_annotations(self):
        writer = pypdf.PdfWriter()
        p1 = writer.add_blank_page(width=300, height=300)
        p2 = writer.add_blank_page(width=300, height=300)

        # Add annotation with author /T
        annotation = pypdf.generic.DictionaryObject({
            pypdf.generic.NameObject("/Type"): pypdf.generic.NameObject("/Annot"),
            pypdf.generic.NameObject("/Subtype"): pypdf.generic.NameObject("/Text"),
            pypdf.generic.NameObject("/Rect"): pypdf.generic.ArrayObject([
                pypdf.generic.FloatObject(50), pypdf.generic.FloatObject(50),
                pypdf.generic.FloatObject(100), pypdf.generic.FloatObject(100)
            ]),
            pypdf.generic.NameObject("/Contents"): pypdf.generic.create_string_object("Check this note"),
            pypdf.generic.NameObject("/T"): pypdf.generic.create_string_object("InspectorGadget"),
            pypdf.generic.NameObject("/M"): pypdf.generic.create_string_object("D:20261001150000Z"),
        })
        writer.add_annotation(page_number=0, annotation=annotation)
        writer.add_metadata({
            "/Title": "Multi-Page Report",
            "/Author": "Secret Author",
            "/Producer": "Enterprise PDF Studio 2026"
        })

        buf = io.BytesIO()
        writer.write(buf)
        raw_pdf = buf.getvalue()

        # Scrub
        scrubbed, summary = scrub_bytes(raw_pdf, "report.pdf")
        self.assertGreater(summary["stripped_count"], 0)

        # Verify pages kept and metadata cleaned
        reader = pypdf.PdfReader(io.BytesIO(scrubbed))
        self.assertEqual(len(reader.pages), 2)
        self.assertIsNone(reader.metadata)

        # Check annotation author is removed
        annots = reader.pages[0].get("/Annots")
        if annots:
            for a in annots:
                obj = a.get_object()
                self.assertNotIn("/T", obj)

    def test_png_color_preservation_and_icc_purge(self):
        # Create an RGBA image with ICC profile
        img = Image.new("RGBA", (40, 40), color=(10, 20, 30, 200))
        pnginfo = PngImagePlugin.PngInfo()
        pnginfo.add_itxt("parameters", "A photorealistic cat, unreal engine 5, 8k prompt", lang="en", tkey="Prompt")
        pnginfo.add_text("Software", "Midjourney v6")

        buf = io.BytesIO()
        img.save(buf, format="PNG", pnginfo=pnginfo)
        raw_png = buf.getvalue()

        # Inspect
        insp = inspect_metadata(raw_png, "art.png")
        self.assertTrue(any("Midjourney" in e["value"] for e in insp["entries"]))
        self.assertTrue(any("parameters" in e["key"] for e in insp["entries"]))

        # Scrub
        scrubbed, summary = scrub_bytes(raw_png, "art.png")
        self.assertLess(len(scrubbed), len(raw_png))

        # Pixel data check
        clean_img = Image.open(io.BytesIO(scrubbed))
        self.assertEqual(clean_img.size, (40, 40))
        self.assertEqual(clean_img.mode, "RGBA")
        self.assertEqual(clean_img.getpixel((0, 0)), (10, 20, 30, 200))


if __name__ == "__main__":
    unittest.main()
