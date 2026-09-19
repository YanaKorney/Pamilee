"""Проверки основы приложения.

Запуск:  python3 -m unittest discover -s tests -t .
Используется только стандартная библиотека плюс то, что и так нужно программе.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

# Каждый прогон тестов работает в своей временной папке,
# чтобы не трогать настоящую базу пользователя.
_TMP = Path(tempfile.mkdtemp(prefix="kvartira-test-"))

from app import config  # noqa: E402

config.DATA_DIR = _TMP / "data"
config.PROJECTS_DIR = config.DATA_DIR / "projects"
config.LOGS_DIR = _TMP / "logs"
config.DB_PATH = config.DATA_DIR / "app.db"

from app import db, storage  # noqa: E402

db.DB_PATH = config.DB_PATH
storage.PROJECTS_DIR = config.PROJECTS_DIR

from fastapi.testclient import TestClient  # noqa: E402

from app.server import app  # noqa: E402


def tearDownModule() -> None:
    shutil.rmtree(_TMP, ignore_errors=True)


class TestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        db.init_db()
        cls.client = TestClient(app)


class TestHealth(TestBase):
    def test_health_answers(self) -> None:
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])

    def test_status_lists_services(self) -> None:
        data = self.client.get("/api/status").json()
        self.assertIn("plan", data["services"])
        self.assertIn("image", data["services"])
        self.assertIsInstance(data["services"]["plan"]["ready"], bool)

    def test_pages_open(self) -> None:
        for url in ("/", "/settings", "/project/1"):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)


class TestProjects(TestBase):
    def test_create_read_update_delete(self) -> None:
        created = self.client.post(
            "/api/projects",
            json={"name": "Моя квартира", "ceiling_height_mm": 2900, "declared_area_m2": 74.37},
        )
        self.assertEqual(created.status_code, 201)
        project = created.json()
        self.assertEqual(project["name"], "Моя квартира")
        self.assertEqual(project["ceiling_height_mm"], 2900)
        self.assertAlmostEqual(project["declared_area_m2"], 74.37)

        project_id = project["id"]
        self.assertTrue(storage.project_dir(project_id).exists())

        fetched = self.client.get(f"/api/projects/{project_id}").json()
        self.assertEqual(fetched["id"], project_id)
        self.assertIn("disk_mb", fetched)

        patched = self.client.patch(
            f"/api/projects/{project_id}", json={"ceiling_height_mm": 2700}
        ).json()
        self.assertEqual(patched["ceiling_height_mm"], 2700)

        listed = self.client.get("/api/projects").json()
        self.assertTrue(any(p["id"] == project_id for p in listed))

        self.assertEqual(self.client.delete(f"/api/projects/{project_id}").status_code, 200)
        self.assertEqual(self.client.get(f"/api/projects/{project_id}").status_code, 404)

    def test_default_ceiling_height_is_2900(self) -> None:
        project = self.client.post("/api/projects", json={"name": "Без высоты"}).json()
        self.assertEqual(project["ceiling_height_mm"], 2900)
        self.client.delete(f"/api/projects/{project['id']}")

    def test_missing_project_gives_human_message(self) -> None:
        response = self.client.get("/api/projects/999999")
        self.assertEqual(response.status_code, 404)
        body = response.json()
        self.assertIn("не найден", body["error"].lower())
        self.assertTrue(body["hint"])


class TestStorage(unittest.TestCase):
    def test_russian_file_names_become_latin(self) -> None:
        self.assertEqual(storage.safe_name("План квартиры.PDF"), "Plan-kvartiry.pdf")
        self.assertEqual(storage.safe_name("гостиная — диван.jpg"), "gostinaya-divan.jpg")

    def test_name_never_empty(self) -> None:
        self.assertTrue(storage.safe_name("???.png").endswith(".png"))

    def test_unique_path_avoids_overwrite(self) -> None:
        folder = _TMP / "unique"
        first = storage.unique_path(folder, "plan.pdf")
        first.write_text("1", encoding="utf-8")
        second = storage.unique_path(folder, "plan.pdf")
        self.assertNotEqual(first, second)
        self.assertEqual(second.name, "plan-2.pdf")


class TestFixtures(unittest.TestCase):
    """Файлы реальной квартиры лежат в проекте и доступны для проверок."""

    def test_plans_are_present(self) -> None:
        fixtures = BASE_DIR / "tests" / "fixtures"
        self.assertTrue((fixtures / "plan-zastroyshchik.pdf").exists())
        self.assertTrue((fixtures / "plan-dizayner.jpg").exists())


if __name__ == "__main__":
    unittest.main()
