"""Static-site contracts; run with unittest without installing the Furl engine."""

import hashlib
import json
import re
import struct
import unittest
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

ROOT = Path(__file__).resolve().parents[2] / "site"
ORIGIN = "https://furl-ctx.vercel.app"
PAGES = ("index", "privacy", "terms", "cookies", "support", "connect", "404")


class Document(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.elements = []
        self.titles = []
        self.title = False
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))
        if tag == "title":
            self.title = True

    def handle_endtag(self, tag):
        if tag == "title":
            self.title = False

    def handle_data(self, data):
        if self.title:
            self.titles.append(data)

    def tags(self, tag):
        return [a for t, a in self.elements if t == tag]


class SiteTests(unittest.TestCase):
    def test_required_pages_exist(self):
        for name in PAGES:
            with self.subTest(page=name):
                self.assertTrue((ROOT / (name + ".html")).is_file())

    def test_metadata_and_headings(self):
        titles = set()
        descriptions = set()
        for name in PAGES:
            with self.subTest(page=name):
                p = ROOT / (name + ".html")
                self.assertTrue(p.exists(), str(p))
                d = Document(p.read_text())
                title = "".join(d.titles)
                self.assertIn("Furl", title)
                self.assertNotIn(title, titles)
                titles.add(title)
                metas = {a.get("name", a.get("property")): a.get("content") for a in d.tags("meta")}
                description = metas["description"]
                self.assertTrue(30 < len(description) < 220)
                self.assertNotIn(description, descriptions)
                descriptions.add(description)
                canonical = [a["href"] for a in d.tags("link") if a.get("rel") == "canonical"]
                expected = ORIGIN + ("/" if name == "index" else "/" + name)
                self.assertEqual(canonical, [expected])
                self.assertEqual(len(d.tags("h1")), 1)
                if name == "404":
                    self.assertIn("noindex", metas["robots"])
                elif name != "index":
                    self.assertTrue(any(a.get("aria-label") == "Breadcrumb" for a in d.tags("nav")))

    def test_local_links_and_fragments_resolve(self):
        for page in ROOT.glob("*.html"):
            if page.name.startswith("google"):
                continue  # Verification responses are not content pages.
            doc = Document(page.read_text())
            for _tag, attrs in doc.elements:
                value = attrs.get("href") or attrs.get("src")
                if not value or value.startswith(("data:", "mailto:")):
                    continue
                url = urlsplit(urljoin(ORIGIN + "/" + page.name, value))
                if url.netloc != "furl-ctx.vercel.app":
                    continue
                target = ROOT / url.path.lstrip("/") if url.path != "/" else ROOT / "index.html"
                if not target.suffix:
                    target = target.with_suffix(".html")
                with self.subTest(page=page.name, link=value):
                    self.assertTrue(target.is_file(), str(target))
                    if url.fragment and target.suffix == ".html" and target.exists():
                        self.assertIn(
                            url.fragment,
                            {a.get("id") for _, a in Document(target.read_text()).elements},
                        )

    def test_sitemap_matches_public_pages(self):
        sitemap = ET.parse(ROOT / "sitemap.xml")
        locations = [n.text for n in sitemap.findall(".//{*}loc")]
        self.assertEqual(
            set(locations),
            {ORIGIN + ("/" if n == "index" else "/" + n) for n in PAGES if n != "404"},
        )
        self.assertEqual(len(locations), len(set(locations)))
        self.assertIn("Sitemap: " + ORIGIN + "/sitemap.xml", (ROOT / "robots.txt").read_text())

    def test_schema_is_truthful_and_capture_provenance_unchanged(self):
        text = (ROOT / "index.html").read_text()
        objs = [
            json.loads(s)
            for s in re.findall(r'<script type="application/ld\+json">(.*?)</script>', text, re.S)
        ]
        app = next(o for o in objs if o.get("@type") == "SoftwareApplication")
        self.assertEqual(app["softwareVersion"], "1.4.0")
        self.assertNotIn("LocalBusiness", text)
        self.assertNotIn("nothing is ever thrown away", text.lower())
        self.assertNotIn("nothing was cherry-picked", text.lower())
        manifest = json.loads((ROOT / "data/manifest.json").read_text())
        self.assertEqual(manifest["furl_version"], "1.2.0")
        self.assertEqual(manifest["generated_at"], "2026-07-13")
        self.assertIn("retention", text.lower())
        self.assertIn("prerecorded", text.lower())
        self.assertIn("synthetic", text.lower())
        self.assertNotIn("six real captures", text.lower())

    def test_png_icons_and_social_card(self):
        for name, size in [("directory-512.png", (512, 512)), ("composer-128.png", (128, 128))]:
            path = ROOT / "assets/icons" / name
            with self.subTest(icon=name):
                self.assertTrue(path.exists())
                data = path.read_bytes()
                self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
                self.assertEqual(struct.unpack(">II", data[16:24]), size)
                self.assertLess(len(data), 10240)
        self.assertEqual(
            struct.unpack(">II", (ROOT / "assets/og.png").read_bytes()[16:24]), (1200, 630)
        )

    def test_production_headers(self):
        cfg = json.loads((ROOT / "vercel.json").read_text())
        self.assertTrue(cfg["cleanUrls"])
        headers = {
            h["key"].lower(): h["value"]
            for r in cfg["headers"]
            if r["source"] == "/(.*)"
            for h in r["headers"]
        }
        self.assertEqual(headers["x-content-type-options"], "nosniff")
        self.assertEqual(headers["referrer-policy"], "strict-origin-when-cross-origin")
        csp = headers["content-security-policy"]
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("object-src 'none'", csp)
        self.assertNotIn("script-src 'self' 'unsafe-inline'", csp)
        for page in ROOT.glob("*.html"):
            for script in re.findall(
                r'<script type="application/ld\+json">(.*?)</script>', page.read_text(), re.S
            ):
                import base64

                digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
                self.assertIn("sha256-" + digest, csp)

    def test_site_has_no_trackers_or_source_maps(self):
        self.assertEqual(list(ROOT.rglob("*.map")), [])
        for path in [*ROOT.glob("*.html"), *ROOT.glob("assets/*.js")]:
            text = path.read_text()
            for forbidden in (
                "sourceMappingURL=",
                "googletagmanager.com",
                "google-analytics.com",
                "document.cookie",
                "localStorage.setItem",
                "iframe src=",
                "Vite + React",
            ):
                self.assertNotIn(forbidden, text, str(path))
        self.assertIn("no analytics", (ROOT / "cookies.html").read_text().lower())

    def test_asset_budgets(self):
        self.assertLess((ROOT / "assets/fonts.css").stat().st_size, 2000)
        self.assertLess((ROOT / "assets/app.js").stat().st_size, 18000)
        self.assertLess((ROOT / "data/furl-data.js").stat().st_size, 200000)


if __name__ == "__main__":
    unittest.main()
