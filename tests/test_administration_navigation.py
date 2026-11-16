import unittest

from app import main
import test_booking_api
from test_compact_tables import prepare_all_tables_fixture


ADMIN_PAGES = {
    "/settings": "/settings", "/backups": "/backups", "/catalog": "/catalog", "/characteristics": "/characteristics",
    "/users": "/users", "/api-keys": "/api-keys", "/notifications": "/notifications",
    "/catalog/types/license/1/edit": "/catalog", "/characteristics/1/edit": "/characteristics",
    "/users/2/edit": "/users", "/api-keys/new": "/api-keys", "/api-keys/1/edit": "/api-keys",
}


class AdministrationNavigationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_booking_api.BookingApiTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        prepare_all_tables_fixture(self.fixture)
        self.fixture.login(1)

    def test_all_administration_pages_share_sidebar_and_active_section(self):
        for path, active_href in ADMIN_PAGES.items():
            with self.subTest(path=path):
                response = self.fixture.client.get(path)
                self.assertEqual(response.status_code, 200, response.text[:500])
                self.assertTrue(response.context["administration_active"])
                items = response.context["administration_items"]
                self.assertEqual(len(items), 7)
                self.assertEqual(items[0]["href"], "/settings")
                self.assertEqual([item["href"] for item in items if item["active"]], [active_href])
                self.assertEqual(response.text.count('class="administration-sidebar"'), 1)
                self.assertIn(f'href="{active_href}" class="active" aria-current="page"', response.text)
                self.assertNotIn('class="nav-menu"', response.text)
                self.assertNotIn('class="nav-dropdown"', response.text)
                self.assertEqual(response.context["breadcrumbs"][0]["href"], "/settings")

    def test_header_links_directly_to_settings_outside_administration(self):
        for path in ("/", "/companies", "/contracts/1", "/time-entries"):
            response = self.fixture.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.context["administration_active"])
            self.assertIn('href="/settings" class="" >Verwaltung</a>', response.text)
            self.assertNotIn('class="administration-sidebar"', response.text)
            self.assertNotIn('/static/administration.css', response.text)

    def test_manager_and_consultant_do_not_gain_administration_access(self):
        for user_id in (2, 3):
            self.fixture.login(user_id)
            response = self.fixture.client.get("/")
            self.assertEqual(response.context["administration_items"], [])
            self.assertNotIn('>Verwaltung</a>', response.text)
            for path in ADMIN_PAGES:
                self.assertEqual(self.fixture.client.get(path).status_code, 403, path)

    def test_navigation_obeys_individual_permissions_and_exact_prefixes(self):
        limited = {"role_name": "custom", "permissions": ["catalog.manage"]}
        self.assertEqual(main.administration_navigation(limited, "/catalog/types/license/1/edit"), [
            {"label": "Katalog", "href": "/catalog", "active": True},
        ])
        self.assertFalse(main.administration_navigation(limited, "/catalogue")[0]["active"])
        self.assertEqual(main.administration_navigation(None, "/settings"), [])


if __name__ == "__main__":
    unittest.main()
