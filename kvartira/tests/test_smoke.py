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


class TestPlanUpload(TestBase):
    """Загрузка настоящих планов квартиры."""

    FIXTURES = BASE_DIR / "tests" / "fixtures"

    def setUp(self) -> None:
        self.project_id = self.client.post(
            "/api/projects", json={"name": "Для загрузки"}
        ).json()["id"]

    def tearDown(self) -> None:
        self.client.delete(f"/api/projects/{self.project_id}")

    def _upload(self, filename: str, mime: str):
        data = (self.FIXTURES / filename).read_bytes()
        return self.client.post(
            f"/api/projects/{self.project_id}/files",
            files={"files": (filename, data, mime)},
        )

    def test_vector_pdf_is_recognised_as_vector(self) -> None:
        response = self._upload("plan-zastroyshchik.pdf", "application/pdf")
        self.assertEqual(response.status_code, 201)
        added = response.json()["added"]
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]["pages"], 1)
        self.assertTrue(added[0]["is_vector"], "Чертёж застройщика — векторный PDF")

    def test_photo_plan_is_not_vector(self) -> None:
        response = self._upload("plan-dizayner.jpg", "image/jpeg")
        self.assertEqual(response.status_code, 201)
        added = response.json()["added"]
        self.assertFalse(added[0]["is_vector"], "Картинка не может быть векторной")

    def test_documents_and_previews(self) -> None:
        self._upload("plan-zastroyshchik.pdf", "application/pdf")
        self._upload("plan-dizayner.jpg", "image/jpeg")

        documents = self.client.get(f"/api/projects/{self.project_id}/documents").json()
        self.assertEqual(len(documents), 2)
        self.assertEqual(sum(d["page_count"] for d in documents), 2)

        page_id = documents[0]["pages"][0]["id"]
        preview = self.client.get(f"/api/files/{page_id}/preview")
        self.assertEqual(preview.status_code, 200)
        self.assertTrue(preview.headers["content-type"].startswith("image/"))

        raw = self.client.get(f"/api/files/{page_id}/raw")
        self.assertEqual(raw.status_code, 200)

    def test_label_is_saved(self) -> None:
        self._upload("plan-dizayner.jpg", "image/jpeg")
        documents = self.client.get(f"/api/projects/{self.project_id}/documents").json()
        page_id = documents[0]["pages"][0]["id"]
        self.client.patch(f"/api/files/{page_id}/label", json={"label": "план мебели"})
        documents = self.client.get(f"/api/projects/{self.project_id}/documents").json()
        self.assertEqual(documents[0]["pages"][0]["label"], "план мебели")

    def test_delete_removes_document_and_files(self) -> None:
        self._upload("plan-zastroyshchik.pdf", "application/pdf")
        documents = self.client.get(f"/api/projects/{self.project_id}/documents").json()
        stored = documents[0]["stored_name"]

        uploads = storage.project_dir(self.project_id) / "uploads" / stored
        self.assertTrue(uploads.exists())

        response = self.client.delete(
            f"/api/projects/{self.project_id}/documents/{stored}"
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(uploads.exists(), "Оригинал должен удаляться с диска")
        self.assertEqual(
            self.client.get(f"/api/projects/{self.project_id}/documents").json(), []
        )

    def test_unsupported_file_explains_itself(self) -> None:
        response = self.client.post(
            f"/api/projects/{self.project_id}/files",
            files={"files": ("чертёж.dwg", b"x" * 100, "application/octet-stream")},
        )
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertIn("не поддерживается", body["error"])
        self.assertIn("PDF", body["hint"])

    def test_broken_pdf_does_not_crash(self) -> None:
        response = self.client.post(
            f"/api/projects/{self.project_id}/files",
            files={"files": ("сломанный.pdf", b"not a pdf at all", "application/pdf")},
        )
        self.assertEqual(response.status_code, 400)
        self.assertTrue(response.json()["hint"])

    def test_project_stats_count_pages(self) -> None:
        self._upload("plan-zastroyshchik.pdf", "application/pdf")
        project = self.client.get(f"/api/projects/{self.project_id}").json()
        self.assertEqual(project["plan_files"], 1)
        self.assertEqual(project["plan_pages"], 1)

    def test_plan_page_opens(self) -> None:
        response = self.client.get(f"/project/{self.project_id}/plan")
        self.assertEqual(response.status_code, 200)


class TestAiSettings(TestBase):
    """Выбор моделей и проверка доступа."""

    def test_settings_have_defaults(self) -> None:
        data = self.client.get("/api/ai/settings").json()
        self.assertIn("plan", data)
        self.assertIn("image", data)
        self.assertTrue(data["plan"]["base_url"].startswith("http"))

    def test_chosen_model_is_saved(self) -> None:
        self.client.patch(
            "/api/ai/settings",
            json={"plan_model": "claude-sonnet-5", "image_model": "flux.2-max"},
        )
        data = self.client.get("/api/ai/settings").json()
        self.assertEqual(data["plan"]["model"], "claude-sonnet-5")
        self.assertEqual(data["image"]["model"], "flux.2-max")
        # выбор из интерфейса важнее значения из .env
        self.assertEqual(self.client.get("/api/status").json()
                         ["services"]["plan"]["model"], "claude-sonnet-5")

    def test_check_without_key_explains_itself(self) -> None:
        report = self.client.post("/api/ai/check").json()
        for part in ("plan", "image"):
            with self.subTest(part=part):
                self.assertFalse(report[part]["ok"])
                self.assertIn("Ключ", report[part]["message"])
                self.assertIn(".env", report[part]["hint"])


class TestModelKind(unittest.TestCase):
    """Программа должна сама понимать, какая модель рисует, а какая пишет."""

    def test_image_models(self) -> None:
        from app.providers.base import guess_kind
        for model_id in ("flux.2-pro", "FLUX.2-max", "gemini-3-pro-image",
                         "dall-e-3", "recraft-v3", "nano-banana"):
            with self.subTest(model=model_id):
                self.assertEqual(guess_kind(model_id), "image")

    def test_text_models(self) -> None:
        from app.providers.base import guess_kind
        for model_id in ("claude-opus-5", "claude-sonnet-5", "gpt-5", "deepseek-v3"):
            with self.subTest(model=model_id):
                self.assertEqual(guess_kind(model_id), "text")

    def test_service_models_are_skipped(self) -> None:
        from app.providers.base import guess_kind
        self.assertEqual(guess_kind("text-embedding-3-large"), "other")
        self.assertEqual(guess_kind("whisper-1"), "other")

    def test_output_modality_wins_over_name(self) -> None:
        from app.providers.base import guess_kind
        self.assertEqual(
            guess_kind("mystery-model-7", {"output_modalities": ["image"]}), "image"
        )


class TestProviderErrors(unittest.TestCase):
    """Ошибки сервиса должны превращаться в человеческие сообщения."""

    def setUp(self) -> None:
        from app.providers import OpenAiCompatProvider
        self.provider = OpenAiCompatProvider("https://example.invalid/v1", "key", "Тест")

    def _explain(self, status: int, body: dict | None = None):
        import httpx
        response = httpx.Response(status, json=body or {}, request=httpx.Request("GET", "https://x"))
        return self.provider._explain(response)

    def test_bad_key(self) -> None:
        error = self._explain(401)
        self.assertIn("ключ", error.message.lower())
        self.assertIn(".env", error.hint)

    def test_no_money(self) -> None:
        error = self._explain(402)
        self.assertIn("деньги", error.message.lower())
        self.assertIn("Пополните", error.hint)

    def test_unknown_model(self) -> None:
        error = self._explain(404)
        self.assertIn("модели", error.message.lower())
        self.assertIn("Настройки", error.hint)

    def test_too_many_requests(self) -> None:
        self.assertIn("Подождите", self._explain(429).hint)

    def test_service_down(self) -> None:
        self.assertIn("недоступен", self._explain(503).message)

    def test_никакого_технического_текста(self) -> None:
        """В сообщении для человека не должно быть кода ошибки и латиницы."""
        for status in (401, 402, 404, 429, 503):
            with self.subTest(status=status):
                message = self._explain(status).message
                self.assertNotIn(str(status), message)


class TestWithoutPillow(TestBase):
    """Программа не должна зависеть от необязательной библиотеки.

    pillow нужна только для WEBP. Раньше она была обязательной и на свежих
    версиях Python ломала весь запуск. Проверяем, что без неё всё живо.
    """

    FIXTURES = BASE_DIR / "tests" / "fixtures"

    def setUp(self) -> None:
        self.project_id = self.client.post(
            "/api/projects", json={"name": "Без pillow"}
        ).json()["id"]
        # Делаем import PIL неудачным на время проверки.
        self._saved = sys.modules.get("PIL", "отсутствовала")
        sys.modules["PIL"] = None  # type: ignore[assignment]

    def tearDown(self) -> None:
        if self._saved == "отсутствовала":
            sys.modules.pop("PIL", None)
        else:
            sys.modules["PIL"] = self._saved
        self.client.delete(f"/api/projects/{self.project_id}")

    def _upload(self, filename: str, data: bytes, mime: str):
        return self.client.post(
            f"/api/projects/{self.project_id}/files",
            files={"files": (filename, data, mime)},
        )

    def test_jpg_works_without_pillow(self) -> None:
        data = (self.FIXTURES / "plan-dizayner.jpg").read_bytes()
        response = self._upload("plan-dizayner.jpg", data, "image/jpeg")
        self.assertEqual(response.status_code, 201, "JPG обязан читаться без pillow")
        added = response.json()["added"]
        self.assertEqual(len(added), 1)

        documents = self.client.get(f"/api/projects/{self.project_id}/documents").json()
        page = documents[0]["pages"][0]
        self.assertEqual((page["width_px"], page["height_px"]), (1701, 1184))
        preview = self.client.get(f"/api/files/{page['id']}/preview")
        self.assertEqual(preview.status_code, 200)

    def test_pdf_works_without_pillow(self) -> None:
        data = (self.FIXTURES / "plan-zastroyshchik.pdf").read_bytes()
        response = self._upload("plan-zastroyshchik.pdf", data, "application/pdf")
        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.json()["added"][0]["is_vector"])

    def test_webp_explains_what_to_do(self) -> None:
        response = self._upload("фото.webp", b"RIFF____WEBPVP8 ", "image/webp")
        self.assertEqual(response.status_code, 400)
        hint = response.json()["hint"]
        self.assertIn("JPG", hint, "Человеку надо подсказать, что делать")
