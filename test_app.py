"""Server API integration tests."""

import io
import json
import shutil
import tempfile
import unittest
from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop
from aiohttp import FormData
from PIL import Image, PngImagePlugin
import pypdf

from app import create_app


class TestAppServer(AioHTTPTestCase):

    async def get_application(self):
        return create_app()

    def setUp(self):
        super().setUp()
        self.temp_dir = tempfile.mkdtemp(prefix="api_test_")

    def tearDown(self):
        super().tearDown()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    @unittest_run_loop
    async def test_status(self):
        resp = await self.client.request("GET", "/api/status")
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data["status"], "online")

    @unittest_run_loop
    async def test_get_index(self):
        resp = await self.client.request("GET", "/")
        self.assertEqual(resp.status, 200)
        text = await resp.text()
        self.assertIn("hermes", text)

    @unittest_run_loop
    async def test_inspect_and_scrub_upload(self):
        # Create a test PNG with metadata
        img = Image.new("RGB", (60, 60), color="purple")
        pinfo = PngImagePlugin.PngInfo()
        pinfo.add_text("Author", "Test Agent")
        pinfo.add_text("Comment", "Confidential Details")
        buf = io.BytesIO()
        img.save(buf, format="PNG", pnginfo=pinfo)
        png_bytes = buf.getvalue()

        # Test /api/inspect
        form = FormData()
        form.add_field("file", png_bytes, filename="secret.png", content_type="image/png")
        resp = await self.client.request("POST", "/api/inspect", data=form)
        self.assertEqual(resp.status, 200)
        insp_data = await resp.json()
        self.assertTrue(insp_data["success"])
        self.assertGreaterEqual(insp_data["inspection"]["metadata_count"], 2)
        self.assertIsNotNone(insp_data["thumbnail"])

        # Test /api/scrub
        form2 = FormData()
        form2.add_field("file", png_bytes, filename="secret.png", content_type="image/png")
        scrub_resp = await self.client.request("POST", "/api/scrub", data=form2)
        self.assertEqual(scrub_resp.status, 200)
        cleaned_bytes = await scrub_resp.read()
        self.assertLess(len(cleaned_bytes), len(png_bytes))
        self.assertIn("X-Stripped-Count", scrub_resp.headers)
        self.assertGreater(int(scrub_resp.headers["X-Stripped-Count"]), 0)

    @unittest_run_loop
    async def test_scrub_local_path(self):
        # Create local test file in temp dir
        test_file = f"{self.temp_dir}/doc.pdf"
        writer = pypdf.PdfWriter()
        writer.add_blank_page(width=100, height=100)
        writer.add_metadata({"/Author": "Local Author", "/Title": "Local Doc"})
        with open(test_file, "wb") as f:
            writer.write(f)

        payload = {"path": self.temp_dir, "recursive": True, "backup": False}
        resp = await self.client.request("POST", "/api/scrub-local-path", json=payload)
        self.assertEqual(resp.status, 200)
        res_data = await resp.json()
        self.assertTrue(res_data["success"])
        self.assertEqual(res_data["result"]["total_scrubbed"], 1)

        # Verify the file was overwritten in-place and metadata is purged
        reader = pypdf.PdfReader(test_file)
        self.assertIsNone(reader.metadata)


if __name__ == "__main__":
    unittest.main()
