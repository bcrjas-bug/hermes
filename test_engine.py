"""Comprehensive tests for the Metadata Scrubber Core Engine."""

import io
import os
import shutil
import tempfile
import unittest
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


class TestMetadataScrubber(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="scrubber_test_")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_png_scrubbing(self):
        # 1. Create a PNG with text metadata
        img = Image.new("RGBA", (80, 80), color=(255, 100, 50, 255))
        pnginfo = PngImagePlugin.PngInfo()
        pnginfo.add_text("Author", "Test User Secret")
        pnginfo.add_text("Software", "AI Generator 9000")
        pnginfo.add_text("Comment", "Confidential Photo")

        buf = io.BytesIO()
        img.save(buf, format="PNG", pnginfo=pnginfo)
        raw_png = buf.getvalue()

        # Verify inspection
        inspection = inspect_metadata(raw_png, "sample.png")
        self.assertEqual(inspection["file_type"], "PNG")
        self.assertGreaterEqual(inspection["metadata_count"], 3)
        self.assertTrue(inspection["has_high_risk"])

        # Scrub bytes
        scrubbed, summary = scrub_bytes(raw_png, "sample.png")
        self.assertLess(len(scrubbed), len(raw_png))
        self.assertGreater(summary["stripped_count"], 0)

        # Verify scrubbed PNG has zero metadata
        post_inspection = inspect_metadata(scrubbed, "sample.png")
        self.assertEqual(post_inspection["metadata_count"], 0)

        # Verify scrubbed image opens cleanly and has matching dimensions
        with Image.open(io.BytesIO(scrubbed)) as check_img:
            self.assertEqual(check_img.size, (80, 80))
            self.assertEqual(list(check_img.info.keys()), [])

    def test_jpeg_scrubbing_with_gps(self):
        # 1. Create a JPEG with EXIF and GPS
        img = Image.new("RGB", (64, 64), color=(0, 128, 255))
        exif = img.getexif()
        exif[0x010F] = "TestCameraVendor"
        exif[0x0110] = "SuperCam Pro"
        exif[0x0131] = "Secret Editing Software"

        from fractions import Fraction
        gps_ifd = {
            1: "N",
            2: (Fraction(37, 1), Fraction(46, 1), Fraction(298, 10)),
            3: "W",
            4: (Fraction(122, 1), Fraction(25, 1), Fraction(101, 10))
        }
        exif_gps = exif.get_ifd(0x8825)
        for k, v in gps_ifd.items():
            exif_gps[k] = v

        buf = io.BytesIO()
        img.save(buf, format="JPEG", exif=exif)
        raw_jpeg = buf.getvalue()

        # Verify inspection flags GPS and High Risk
        inspection = inspect_metadata(raw_jpeg, "photo.jpg")
        self.assertEqual(inspection["file_type"], "JPEG")
        self.assertTrue(inspection["has_high_risk"])
        gps_entries = [e for e in inspection["entries"] if "GPS" in e["category"]]
        self.assertGreater(len(gps_entries), 0)

        # Scrub bytes
        scrubbed, summary = scrub_bytes(raw_jpeg, "photo.jpg")
        self.assertLess(len(scrubbed), len(raw_jpeg))

        # Verify EXIF is completely gone
        with Image.open(io.BytesIO(scrubbed)) as check_img:
            self.assertEqual(check_img.size, (64, 64))
            self.assertEqual(len(check_img.getexif().keys()), 0)

    def test_pdf_scrubbing(self):
        # 1. Create a PDF with /Info and pages
        writer = pypdf.PdfWriter()
        writer.add_blank_page(width=200, height=200)
        writer.add_metadata({
            "/Title": "Classified Document",
            "/Author": "Jane Doe",
            "/Creator": "Confidential PDF Maker",
            "/Subject": "Secret Project Plan"
        })

        buf = io.BytesIO()
        writer.write(buf)
        raw_pdf = buf.getvalue()

        # Inspect
        inspection = inspect_metadata(raw_pdf, "doc.pdf")
        self.assertEqual(inspection["file_type"], "PDF")
        self.assertTrue(inspection["has_high_risk"])
        self.assertGreaterEqual(inspection["metadata_count"], 4)

        # Scrub
        scrubbed, summary = scrub_bytes(raw_pdf, "doc.pdf")
        self.assertGreater(summary["stripped_count"], 0)

        # Verify with pypdf
        r = pypdf.PdfReader(io.BytesIO(scrubbed))
        self.assertEqual(len(r.pages), 1)
        self.assertIsNone(r.metadata)
        self.assertIsNone(r.xmp_metadata)

    def test_in_place_file_overwrite(self):
        file_path = Path(self.test_dir) / "original_image.png"

        # Create original file
        img = Image.new("RGB", (50, 50), color="green")
        pnginfo = PngImagePlugin.PngInfo()
        pnginfo.add_text("Author", "Test In Place")
        img.save(file_path, format="PNG", pnginfo=pnginfo)

        orig_stat = file_path.stat()

        # Run in-place scrub
        result = scrub_file_inplace(file_path, backup=True)
        self.assertTrue(result["success"])
        self.assertTrue(file_path.exists())
        self.assertTrue((Path(self.test_dir) / "original_image.png.bak").exists())

        # Inspect the overwritten file directly
        with open(file_path, "rb") as f:
            new_data = f.read()
        inspection = inspect_metadata(new_data, file_path.name)
        self.assertEqual(inspection["metadata_count"], 0)

    def test_in_place_directory_scrub(self):
        # Create a folder with 1 PNG, 1 JPEG, and 1 PDF
        p_png = Path(self.test_dir) / "pic.png"
        p_jpg = Path(self.test_dir) / "pic.jpg"
        p_pdf = Path(self.test_dir) / "doc.pdf"

        # PNG
        img1 = Image.new("RGB", (30, 30), color="red")
        pinfo = PngImagePlugin.PngInfo()
        pinfo.add_text("Author", "Person1")
        img1.save(p_png, pnginfo=pinfo)

        # JPG
        img2 = Image.new("RGB", (30, 30), color="blue")
        ex = img2.getexif()
        ex[0x010F] = "Camera1"
        img2.save(p_jpg, exif=ex)

        # PDF
        w = pypdf.PdfWriter()
        w.add_blank_page(width=100, height=100)
        w.add_metadata({"/Author": "Writer1"})
        with open(p_pdf, "wb") as f:
            w.write(f)

        # Scrub directory
        res = scrub_directory_inplace(self.test_dir, recursive=True, backup=False)
        self.assertEqual(res["total_found"], 3)
        self.assertEqual(res["total_scrubbed"], 3)

        # Verify all files are clean
        for f in [p_png, p_jpg, p_pdf]:
            with open(f, "rb") as stream:
                insp = inspect_metadata(stream.read(), f.name)
                self.assertEqual(insp["metadata_count"], 0)


if __name__ == "__main__":
    unittest.main()
