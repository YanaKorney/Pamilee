"""Проверки основы приложения.

Запуск:  python3 -m unittest discover -s tests -t .
Используется только стандартная библиотека плюс то, что и так нужно программе.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import warnings

# Тестовый клиент ругается на библиотеку, которую мы не выбирали.
warnings.filterwarnings("ignore", module="starlette.*")
warnings.filterwarnings("ignore", message=".*httpx.*")
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

from app.errors import UserError  # noqa: E402

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
                # Подсказка должна вести к полю на этой же странице,
                # а не отправлять человека искать файлы на диске.
                self.assertIn("Новый ключ", report[part]["hint"])
                self.assertNotIn(".env", report[part]["hint"])


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
        self.assertIn("Настройки", error.hint)
        self.assertNotIn(".env", error.hint, "Человека больше не гоняют к файлу")

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


class TestRequirements(unittest.TestCase):
    """Список библиотек должен пережить смену версии Python.

    На Python 3.14 жёстко закреплённая версия pillow не нашла готовой
    сборки и попыталась собраться из исходников — запуск оборвался.
    Чтобы это не повторилось, версии указываем как «не ниже».
    """

    def _lines(self, name: str) -> list[str]:
        path = BASE_DIR / name
        return [
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]

    def test_versions_are_not_frozen(self) -> None:
        for line in self._lines("requirements.txt"):
            with self.subTest(line=line):
                self.assertNotIn("==", line, "Жёсткая версия ломает запуск на новом Python")
                self.assertIn(">=", line, "Нужна нижняя граница версии")

    def test_only_one_compiled_dependency(self) -> None:
        """Чем меньше библиотек с компиляцией, тем надёжнее установка."""
        compiled = {"pymupdf", "pillow", "numpy", "lxml", "opencv-python"}
        required = {line.split(">=")[0].lower() for line in self._lines("requirements.txt")}
        self.assertEqual(
            required & compiled, {"pymupdf"},
            "В обязательных должна остаться только pymupdf",
        )

    def test_pillow_is_optional(self) -> None:
        optional = {line.split(">=")[0].lower() for line in self._lines("requirements-optional.txt")}
        self.assertIn("pillow", optional)


class TestConnectionDiagnosis(unittest.TestCase):
    """«Не удалось связаться» — бесполезно. Программа обязана назвать причину."""

    def test_unknown_host(self) -> None:
        from app.netcheck import diagnose
        found = diagnose("https://takogo-servisa-net-12345.invalid/v1")
        self.assertIn("найти", found.message.lower())
        self.assertTrue(found.hint)
        self.assertTrue(found.technical, "Разработчику нужна техническая строка")

    def test_broken_address(self) -> None:
        from app.netcheck import diagnose
        found = diagnose("")
        self.assertIn("неверный адрес", found.message.lower())
        self.assertIn("aitunnel", found.hint)

    def test_message_is_human(self) -> None:
        """В сообщении для человека не должно быть кода и латиницы."""
        from app.netcheck import diagnose
        found = diagnose("https://takogo-servisa-net-12345.invalid/v1")
        self.assertNotIn("Error", found.message)
        self.assertNotIn("socket", found.message.lower())

    def test_error_carries_technical_note(self) -> None:
        from app.errors import UserError
        error = UserError("Сообщение", "Подсказка", technical="TimeoutError: ...")
        self.assertEqual(error.to_dict()["technical"], "TimeoutError: ...")


class TestVectorPlan(unittest.TestCase):
    """Чтение геометрии из настоящего чертежа квартиры.

    Проверяется на реальном плане застройщика: масштаб, площади,
    разделение комнат и лоджии. Всё это считается без AI.
    """

    @classmethod
    def setUpClass(cls) -> None:
        from app.plan_vector import read_plan
        cls.plan = read_plan(BASE_DIR / "tests" / "fixtures" / "plan-zastroyshchik.pdf")

    def test_recognised_as_vector(self) -> None:
        self.assertTrue(self.plan.is_vector)

    def test_scale_is_precise(self) -> None:
        self.assertTrue(self.plan.scale_is_reliable)
        self.assertGreaterEqual(self.plan.scale_samples, 20)
        self.assertLessEqual(
            self.plan.scale_worst_error_mm, 5,
            "Масштаб проверяется по трём десяткам размеров — расхождение "
            "больше нескольких миллиметров означает ошибку разбора",
        )

    def test_every_dimension_agrees_with_the_drawing(self) -> None:
        """Каждая размерная линия должна сойтись со своей подписью."""
        scale = self.plan.scale_mm_per_unit
        self.assertIsNotNone(scale)
        matched = [d for d in self.plan.dimensions if d.span_units]
        self.assertGreaterEqual(len(matched), 20)

    def test_total_area_matches_the_apartment(self) -> None:
        self.assertEqual(self.plan.total_area_m2, 74.37)

    def test_seven_rooms_add_up(self) -> None:
        self.assertEqual(len(self.plan.room_areas), 7)
        self.assertEqual(self.plan.rooms_sum_m2, 74.37)
        self.assertEqual(
            sorted(self.plan.room_areas, reverse=True),
            [19.09, 13.95, 12.23, 10.59, 10.37, 4.48, 3.66],
        )

    def test_loggia_is_kept_apart(self) -> None:
        """Лоджия не входит в общую площадь — её нельзя считать комнатой."""
        self.assertIn(4.49, self.plan.extra_areas)

    def test_bathroom_is_not_confused_with_loggia(self) -> None:
        """4,48 и 4,49 почти одинаковы — их легко перепутать."""
        self.assertIn(4.48, self.plan.room_areas)
        self.assertNotIn(4.49, self.plan.room_areas)

    def test_explication_is_read(self) -> None:
        self.assertEqual(self.plan.summary, [43.63, 74.37, 76.62, 78.86])

    def test_living_area_matches_three_rooms(self) -> None:
        """Жилая площадь из штампа складывается ровно из трёх комнат.

        Взять три самые большие нельзя: кухня 12,23 больше третьей
        комнаты 10,59, но жилой не считается. Проверяем, что такая
        тройка вообще находится — это сходится с экспликацией.
        """
        from itertools import combinations
        triples = [
            trio for trio in combinations(self.plan.room_areas, 3)
            if abs(sum(trio) - 43.63) < 0.02
        ]
        self.assertTrue(triples, "Жилая площадь не сложилась ни из какой тройки комнат")
        self.assertEqual(sorted(triples[0], reverse=True), [19.09, 13.95, 10.59])


class TestRasterPlanIsHonest(unittest.TestCase):
    """С картинки геометрию не прочитать — программа не должна притворяться."""

    def test_raster_pdf_gives_no_scale(self) -> None:
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            import pymupdf

        source = BASE_DIR / "tests" / "fixtures" / "plan-dizayner.jpg"
        raster = _TMP / "raster-plan.pdf"
        with pymupdf.open(source) as image:
            raster.write_bytes(image.convert_to_pdf())

        from app.plan_vector import read_plan
        plan = read_plan(raster)
        self.assertFalse(plan.is_vector)
        self.assertIsNone(plan.scale_mm_per_unit)
        self.assertFalse(plan.scale_is_reliable)


class FakeProvider:
    """Подставной сервис: отвечает заготовкой, в интернет не ходит."""

    def __init__(self, answer_text: str) -> None:
        self.answer_text = answer_text
        self.asked_prompt = ""
        self.asked_images = 0

    def ask(self, model, prompt, images=None, pdf=None, max_tokens=8000,
            stream=True):
        from app.providers.base import TextAnswer
        self.asked_prompt = prompt
        self.asked_images = len(images or [])
        return TextAnswer(self.answer_text, input_tokens=21000, output_tokens=4000,
                          model=model)


class TestPlanAnalysis(TestBase):
    """Разбор листа: перевод ответа модели в миллиметры и проверка."""

    PDF = BASE_DIR / "tests" / "fixtures" / "plan-zastroyshchik.pdf"

    def _answer(self, rooms, items=(), page_kind="dimensioned_plan", refs=()):
        import json as js
        return js.dumps({
            "page_kind": page_kind,
            "scale_references": list(refs),
            "rooms": rooms,
            "items": list(items),
            "notes": "",
        })

    def test_prompt_carries_known_numbers(self) -> None:
        """Модели не дают угадывать то, что уже известно точно."""
        from app.plan_analysis import analyse_page
        fake = FakeProvider(self._answer([]))
        analyse_page(1, self.PDF, 1, True, 74.37, provider=fake)
        self.assertIn("74.37", fake.asked_prompt)
        # Масштаб передан числом, а не поручен модели
        self.assertIn("Один пиксель этой картинки", fake.asked_prompt)
        self.assertNotIn("Масштаб этого листа неизвестен", fake.asked_prompt)
        self.assertEqual(fake.asked_images, 1)

    def test_pixels_become_millimetres(self) -> None:
        from app.plan_analysis import analyse_page
        # Прямоугольник 100×100 px при известном масштабе даёт понятную площадь.
        rooms = [{"name": "Комната", "kind": "bedroom",
                  "polygon": [[100, 100], [200, 100], [200, 200], [100, 200]]}]
        fake = FakeProvider(self._answer(rooms))
        result = analyse_page(1, self.PDF, 1, True, 74.37, provider=fake)
        self.assertEqual(len(result.rooms), 1)
        self.assertEqual(result.scale_source, "из чертежа")
        room = result.rooms[0]
        side_mm = room.polygon_mm[1][0] - room.polygon_mm[0][0]
        self.assertAlmostEqual(side_mm, 100 * result.mm_per_px, delta=1)
        self.assertAlmostEqual(room.area_m2, (side_mm / 1000) ** 2, delta=0.02)

    def test_markdown_wrapper_is_survived(self) -> None:
        from app.plan_analysis import parse_answer
        data = parse_answer('Вот ответ:\n```json\n{"rooms": []}\n```\nГотово.')
        self.assertEqual(data, {"rooms": []})

    def test_nonsense_answer_gives_human_message(self) -> None:
        from app.plan_analysis import analyse_page
        fake = FakeProvider("Извините, я не могу прочитать этот чертёж.")
        with self.assertRaises(UserError) as caught:
            analyse_page(1, self.PDF, 1, True, 74.37, provider=fake)
        self.assertIn("разобрать ответ", caught.exception.message)
        self.assertTrue(caught.exception.hint)

    def test_scale_from_dimension_lines_when_unknown(self) -> None:
        """Для картинки масштаб берётся из размерных линий, найденных моделью."""
        from app.plan_analysis import scale_from_references
        mm_per_px = scale_from_references([
            {"mm": 4200, "x1": 0, "y1": 0, "x2": 420, "y2": 0},
            {"mm": 1900, "x1": 0, "y1": 0, "x2": 190, "y2": 0},
        ])
        self.assertAlmostEqual(mm_per_px, 10.0, places=3)

    def test_area_mismatch_is_reported(self) -> None:
        from app.plan_analysis import analyse_page
        rooms = [{"name": "Крошечная", "kind": "bedroom", "area_m2": 19.09,
                  "polygon": [[0, 0], [30, 0], [30, 30], [0, 30]]}]
        fake = FakeProvider(self._answer(rooms))
        result = analyse_page(1, self.PDF, 1, True, 74.37, provider=fake)
        self.assertTrue(result.warnings)
        self.assertTrue(any("Крошечная" in w for w in result.warnings))

    def test_wild_item_size_falls_back_to_sensible(self) -> None:
        from app.plan_analysis import analyse_page
        items = [{"category": "furniture", "subtype": "bed", "label": "Кровать",
                  "x": 100, "y": 100, "width_px": 99999, "depth_px": 1,
                  "confidence": 0.9}]
        fake = FakeProvider(self._answer([], items))
        result = analyse_page(1, self.PDF, 1, True, 74.37, provider=fake)
        self.assertEqual(len(result.items), 1)
        self.assertEqual((result.items[0].width_mm, result.items[0].depth_mm), (1600, 2000))

    def test_result_is_saved_and_readable(self) -> None:
        from app.plan_analysis import analyse_page, store
        project_id = self.client.post(
            "/api/projects", json={"name": "С разбором", "declared_area_m2": 74.37}
        ).json()["id"]
        rooms = [{"name": "Гостиная", "kind": "living",
                  "polygon": [[0, 0], [300, 0], [300, 200], [0, 200]]}]
        items = [{"category": "furniture", "subtype": "sofa", "label": "Диван",
                  "x": 150, "y": 100, "width_px": 60, "depth_px": 25, "confidence": 0.8}]
        fake = FakeProvider(self._answer(rooms, items))
        result = analyse_page(project_id, self.PDF, 1, True, 74.37, provider=fake)
        store(project_id, result)

        saved = self.client.get(f"/api/projects/{project_id}/rooms").json()
        self.assertEqual(len(saved["rooms"]), 1)
        self.assertEqual(saved["rooms"][0]["name"], "Гостиная")
        self.assertEqual(len(saved["items"]), 1)
        self.assertEqual(saved["items"][0]["label"], "Диван")
        self.client.delete(f"/api/projects/{project_id}")


class TestSpending(TestBase):
    """Счётчик расходов и дневной лимит."""

    def test_cost_is_counted_in_roubles(self) -> None:
        from app import ai
        # 21000 входных и 4000 выходных при цене 100 / 5000 ₽ за миллион
        self.assertAlmostEqual(ai.text_cost_rub(21000, 4000), 2.1 + 20.0, places=2)

    def test_limit_stops_before_spending(self) -> None:
        from app import ai
        from app.config import settings
        ai.record_spend("plan", settings.daily_limit_rub + 1, "проверка лимита")
        with self.assertRaises(UserError) as caught:
            ai.check_daily_limit()
        self.assertIn("лимит", caught.exception.message.lower())
        self.assertIn("DAILY_LIMIT_RUB", caught.exception.hint)


class TestLoggiaIsNotPartOfTheFlat(TestBase):
    """Лоджия не входит в общую площадь — и не должна ломать сверку.

    На настоящем разборе квартиры это дало ложную тревогу: сумма восьми
    помещений вместе с лоджией — 79,59 м² против 74,37 по документам,
    а без лоджии — 74,54, то есть расхождение 0,2 %.
    """

    PDF = BASE_DIR / "tests" / "fixtures" / "plan-zastroyshchik.pdf"

    def _run(self, rooms):
        import json as js
        from app.plan_analysis import analyse_page
        fake = FakeProvider(js.dumps({
            "page_kind": "dimensioned_plan", "rooms": rooms, "items": [], "notes": "",
        }))
        return analyse_page(1, self.PDF, 1, True, 74.37, provider=fake)

    def _square(self, side_px, offset=0):
        return [[offset, 0], [offset + side_px, 0],
                [offset + side_px, side_px], [offset, side_px]]

    def test_balcony_is_out_of_the_total(self) -> None:
        result = self._run([
            {"name": "Гостиная", "kind": "living", "polygon": self._square(200)},
            {"name": "Лоджия", "kind": "balcony", "polygon": self._square(60, 400)},
        ])
        living = next(r for r in result.rooms if r.kind == "living")
        self.assertEqual(result.total_area_m2, living.area_m2)
        self.assertGreater(result.outside_area_m2, 0)

    def test_balcony_is_not_counted_as_a_room(self) -> None:
        """Семь помещений плюс лоджия — это всё ещё семь помещений."""
        rooms = [
            {"name": f"Комната {n}", "kind": "bedroom", "polygon": self._square(60, n * 70)}
            for n in range(7)
        ]
        rooms.append({"name": "Лоджия", "kind": "balcony", "polygon": self._square(40, 700)})
        result = self._run(rooms)
        about_count = [w for w in result.warnings if "подписано" in w]
        self.assertFalse(
            about_count,
            "Семь помещений и лоджия — счёт сходится, тревожить человека незачем",
        )


class TestPromptHelpsTheModel(TestBase):
    """Модели передаётся всё, что программа знает точно."""

    PDF = BASE_DIR / "tests" / "fixtures" / "plan-zastroyshchik.pdf"

    def setUp(self) -> None:
        import json as js
        from app.plan_analysis import analyse_page
        self.fake = FakeProvider(js.dumps({"rooms": [], "items": []}))
        analyse_page(1, self.PDF, 1, True, 74.37, provider=self.fake)

    def test_scale_is_given_so_the_model_can_check_itself(self) -> None:
        self.assertIn("ПРОВЕРЬ СЕБЯ", self.fake.asked_prompt)
        self.assertIn("мм", self.fake.asked_prompt)

    def test_dimension_marks_are_given(self) -> None:
        self.assertIn("ОПОРНЫЕ РАЗМЕРЫ", self.fake.asked_prompt)
        self.assertIn("7830 мм", self.fake.asked_prompt)
        self.assertIn("5805 мм", self.fake.asked_prompt)

    def test_inner_faces_are_required(self) -> None:
        """Разница между осями и внутренними гранями — это те самые проценты."""
        self.assertIn("ВНУТРЕННИМ граням", self.fake.asked_prompt)

    def test_balcony_rule_is_explained(self) -> None:
        self.assertIn("НЕ входят", self.fake.asked_prompt)
        self.assertIn("kind = balcony", self.fake.asked_prompt)


class TestDataLivesOutsideTheProgram(unittest.TestCase):
    """Проекты и ключ лежат отдельно от программы.

    Иначе обновление стирает всё, что человек успел сделать: скачал
    новую версию, распаковал — и нет ни ключа, ни загруженных планов.
    """

    def test_app_home_can_be_pointed_anywhere(self) -> None:
        import os
        from app.config import resolve_app_home
        os.environ["APP_HOME"] = str(_TMP / "своя-папка")
        try:
            self.assertEqual(resolve_app_home(), _TMP / "своя-папка")
        finally:
            os.environ.pop("APP_HOME", None)

    def test_default_home_is_in_documents(self) -> None:
        from app.config import HOME_FOLDER_NAME, resolve_app_home
        home = resolve_app_home()
        self.assertEqual(home.name, HOME_FOLDER_NAME)
        self.assertIn(str(Path.home()), str(home))

    def test_migration_moves_key_and_projects(self) -> None:
        from app import config

        program = _TMP / "программа"
        (program / "data" / "projects" / "1").mkdir(parents=True)
        (program / "data" / "app.db").write_bytes("база".encode())
        (program / ".env").write_text("PLAN_API_KEY=секрет", encoding="utf-8")
        new_home = _TMP / "новый-дом"

        saved = (config.BASE_DIR, config.APP_HOME, config.ENV_PATH,
                 config.DATA_DIR, config.LOGS_DIR)
        config.BASE_DIR = program
        config.APP_HOME = new_home
        config.ENV_PATH = new_home / ".env"
        config.DATA_DIR = new_home / "data"
        config.LOGS_DIR = new_home / "logs"
        try:
            moved = config.migrate_from_program_folder()

            self.assertEqual(len(moved), 2, "Перенести надо и ключ, и проекты")
            self.assertEqual(
                (new_home / ".env").read_text(encoding="utf-8"), "PLAN_API_KEY=секрет"
            )
            self.assertEqual((new_home / "data" / "app.db").read_bytes(), "база".encode())
            self.assertTrue((new_home / "data" / "projects" / "1").is_dir())

            self.assertFalse((program / ".env").exists(), "Старое место должно опустеть")
            self.assertFalse((program / "data").exists())

            # Повторный запуск ничего не ломает
            self.assertEqual(config.migrate_from_program_folder(), [])
        finally:
            (config.BASE_DIR, config.APP_HOME, config.ENV_PATH,
             config.DATA_DIR, config.LOGS_DIR) = saved

    def test_migration_never_overwrites_newer_data(self) -> None:
        """Если в новом месте уже что-то есть — старое туда не лезет."""
        from app import config

        program = _TMP / "программа-2"
        (program / "data").mkdir(parents=True)
        (program / "data" / "app.db").write_bytes("старое".encode())
        (program / ".env").write_text("старый ключ", encoding="utf-8")
        new_home = _TMP / "дом-2"
        (new_home / "data").mkdir(parents=True)
        (new_home / "data" / "app.db").write_bytes("новое".encode())
        (new_home / ".env").write_text("новый ключ", encoding="utf-8")

        saved = (config.BASE_DIR, config.APP_HOME, config.ENV_PATH, config.DATA_DIR)
        config.BASE_DIR = program
        config.APP_HOME = new_home
        config.ENV_PATH = new_home / ".env"
        config.DATA_DIR = new_home / "data"
        try:
            self.assertEqual(config.migrate_from_program_folder(), [])
            self.assertEqual((new_home / ".env").read_text(encoding="utf-8"), "новый ключ")
            self.assertEqual((new_home / "data" / "app.db").read_bytes(), "новое".encode())
        finally:
            (config.BASE_DIR, config.APP_HOME, config.ENV_PATH, config.DATA_DIR) = saved


class TestRussianPathsAreSafe(unittest.TestCase):
    """Путь к данным содержит русские буквы — файлы должны открываться."""

    def test_pdf_opens_from_a_russian_path(self) -> None:
        from app.mupdf import open_file
        folder = _TMP / "Документы" / "Моя квартира" / "планы"
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / "план застройщика.pdf"
        target.write_bytes(
            (BASE_DIR / "tests" / "fixtures" / "plan-zastroyshchik.pdf").read_bytes()
        )
        with open_file(target) as document:
            self.assertEqual(document.page_count, 1)

    def test_vector_reading_works_from_a_russian_path(self) -> None:
        from app.plan_vector import read_plan
        folder = _TMP / "Мои документы" / "квартира"
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / "чертёж.pdf"
        target.write_bytes(
            (BASE_DIR / "tests" / "fixtures" / "plan-zastroyshchik.pdf").read_bytes()
        )
        plan = read_plan(target)
        self.assertTrue(plan.scale_is_reliable)
        self.assertEqual(plan.total_area_m2, 74.37)

    def test_image_preview_works_from_a_russian_path(self) -> None:
        from app.plan_files import _make_image_preview
        folder = _TMP / "Рабочий стол" / "планы квартиры"
        folder.mkdir(parents=True, exist_ok=True)
        source = folder / "план дизайнера.jpg"
        source.write_bytes(
            (BASE_DIR / "tests" / "fixtures" / "plan-dizayner.jpg").read_bytes()
        )
        size = _make_image_preview(source, folder / "превью.jpg", "план дизайнера.jpg")
        self.assertEqual(size, (1701, 1184))


class TestBrokenDatabase(unittest.TestCase):
    """Повреждённый файл базы объясняется словами, а не трассировкой."""

    def test_human_message_instead_of_crash(self) -> None:
        from app import config, db

        broken_home = _TMP / "сломанный-дом"
        (broken_home / "data").mkdir(parents=True, exist_ok=True)
        broken = broken_home / "data" / "app.db"
        broken.write_text("это не база данных", encoding="utf-8")

        saved_config = config.DATA_DIR, config.PROJECTS_DIR, config.LOGS_DIR
        saved_db = db.DB_PATH
        config.DATA_DIR = broken_home / "data"
        config.PROJECTS_DIR = broken_home / "data" / "projects"
        config.LOGS_DIR = broken_home / "logs"
        db.DB_PATH = broken
        try:
            with self.assertRaises(UserError) as caught:
                db.init_db()
            message = caught.exception.message
            self.assertIn("повреждён", message)
            self.assertNotIn("sqlite", message.lower())
            self.assertIn("app.db", caught.exception.hint)
        finally:
            config.DATA_DIR, config.PROJECTS_DIR, config.LOGS_DIR = saved_config
            db.DB_PATH = saved_db


class TestKeyFromTheInterface(TestBase):
    """Ключ вставляется прямо в программе, без поиска скрытых файлов."""

    def setUp(self) -> None:
        from app import config
        self.saved_env_path = config.ENV_PATH
        self.saved_home = config.APP_HOME
        config.APP_HOME = _TMP / "ключи"
        config.ENV_PATH = config.APP_HOME / ".env"
        config.APP_HOME.mkdir(parents=True, exist_ok=True)
        self.saved_keys = (config.settings.plan_api_key, config.settings.image_api_key)

    def tearDown(self) -> None:
        from app import config
        config.ENV_PATH = self.saved_env_path
        config.APP_HOME = self.saved_home
        config.settings.plan_api_key, config.settings.image_api_key = self.saved_keys

    def test_one_key_serves_both_services(self) -> None:
        response = self.client.put(
            "/api/ai/keys", json={"plan_key": "sk-proverka-1234", "same_for_both": True}
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["plan"]["ready"])
        self.assertTrue(data["image"]["ready"])

    def test_key_starts_working_without_restart(self) -> None:
        from app.config import settings
        self.client.put(
            "/api/ai/keys", json={"plan_key": "sk-srazu-9876", "same_for_both": True}
        )
        self.assertEqual(settings.plan_api_key, "sk-srazu-9876")
        self.assertTrue(settings.plan_ready)

    def test_key_is_written_to_the_settings_file(self) -> None:
        from app import config
        self.client.put(
            "/api/ai/keys", json={"plan_key": "sk-v-fayl-5555", "same_for_both": True}
        )
        written = config.ENV_PATH.read_text(encoding="utf-8")
        self.assertIn("PLAN_API_KEY=sk-v-fayl-5555", written)
        self.assertIn("IMAGE_API_KEY=sk-v-fayl-5555", written)

    def test_comments_in_the_settings_file_survive(self) -> None:
        """Файл остаётся понятным человеку: подсказки не затираются."""
        from app import config
        config.ENV_PATH.write_text(
            "# как пользоваться этим файлом\nPLAN_API_KEY=\nPLAN_MODEL=claude-opus-5\n",
            encoding="utf-8",
        )
        self.client.put("/api/ai/keys", json={"plan_key": "noviy-klyuch", "same_for_both": False})
        written = config.ENV_PATH.read_text(encoding="utf-8")
        self.assertIn("# как пользоваться этим файлом", written)
        self.assertIn("PLAN_MODEL=claude-opus-5", written)
        self.assertIn("PLAN_API_KEY=noviy-klyuch", written)

    def test_empty_key_is_refused_politely(self) -> None:
        response = self.client.put("/api/ai/keys", json={"plan_key": "   "})
        self.assertEqual(response.status_code, 400)
        self.assertIn("не вписали", response.json()["error"])

    def test_key_is_never_shown_in_full(self) -> None:
        """На экране виден только хвост ключа — чтобы узнать, но не подсмотреть."""
        self.client.put(
            "/api/ai/keys", json={"plan_key": "sk-very-secret-abcd", "same_for_both": True}
        )
        shown = self.client.get("/api/ai/settings").json()["plan"]["key_hint"]
        self.assertNotIn("very-secret", shown)
        self.assertIn("abcd", shown)


class TestKeyIsCleanedUp(TestBase):
    """При копировании из браузера к ключу липнут невидимые символы.

    Неразрывный пробел не видно глазом, но запрос из-за него падает
    с ошибкой кодировки, по которой ничего не понять.
    """

    def setUp(self) -> None:
        from app import config
        self.saved = (config.ENV_PATH, config.APP_HOME,
                      config.settings.plan_api_key, config.settings.image_api_key)
        config.APP_HOME = _TMP / "чистка-ключей"
        config.ENV_PATH = config.APP_HOME / ".env"
        config.APP_HOME.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        from app import config
        (config.ENV_PATH, config.APP_HOME,
         config.settings.plan_api_key, config.settings.image_api_key) = self.saved

    def test_invisible_characters_are_stripped(self) -> None:
        from app.config import clean_secret
        self.assertEqual(clean_secret("  sk-abc123  "), "sk-abc123")
        self.assertEqual(clean_secret("﻿sk-abc123​"), "sk-abc123")
        self.assertEqual(clean_secret('"sk-abc123"'), "sk-abc123")

    def test_key_with_spaces_still_works(self) -> None:
        response = self.client.put(
            "/api/ai/keys",
            json={"plan_key": " sk-with-spaces-0001  ", "same_for_both": True},
        )
        self.assertEqual(response.status_code, 200)
        from app.config import settings
        self.assertEqual(settings.plan_api_key, "sk-with-spaces-0001")

    def test_russian_letters_are_refused_politely(self) -> None:
        response = self.client.put(
            "/api/ai/keys", json={"plan_key": "ключ-по-русски", "same_for_both": True}
        )
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertIn("посторонние символы", body["error"])
        self.assertIn("Скопируйте", body["hint"])

    def test_service_refuses_a_broken_key_without_crashing(self) -> None:
        """Последняя линия обороны: даже если ключ попал в настройки."""
        from app.providers import OpenAiCompatProvider
        provider = OpenAiCompatProvider("https://example.invalid/v1", "ключ-кириллицей")
        with self.assertRaises(UserError) as caught:
            provider.list_models()
        self.assertIn("посторонние символы", caught.exception.message)


class TestWallsFromRooms(unittest.TestCase):
    """Стены выводятся из границ комнат, а не приходят от AI.

    Поэтому разойтись с комнатами они не могут. Главная тонкость —
    соседи часто граничат не всей стеной, а её частью.
    """

    @staticmethod
    def _room(room_id, polygon, name="Комната"):
        import json as js
        return {"id": room_id, "name": name, "polygon": js.dumps(polygon)}

    def test_two_rooms_share_one_wall(self) -> None:
        from app.geometry import walls_from_rooms
        walls = walls_from_rooms([
            self._room(1, [[0, 0], [3000, 0], [3000, 4000], [0, 4000]]),
            self._room(2, [[3000, 0], [6000, 0], [6000, 4000], [3000, 4000]]),
        ])
        inner = [w for w in walls if w.kind == "inner"]
        self.assertEqual(len(inner), 1, "Общая граница — одна стена, а не две")
        self.assertEqual(sorted(inner[0].rooms), [1, 2])
        self.assertEqual(inner[0].thickness_mm, 120)

    def test_partial_overlap_is_split(self) -> None:
        """Длинная стена одной комнаты и две коротких с той стороны.

        Раньше такие грани не склеивались, и на границе вырастали две
        стены вместо одной: дверь упиралась в лишнюю.
        """
        from app.geometry import walls_from_rooms
        walls = walls_from_rooms([
            self._room(1, [[0, 0], [6000, 0], [6000, 3000], [0, 3000]]),
            self._room(2, [[0, 3000], [3000, 3000], [3000, 6000], [0, 6000]]),
            self._room(3, [[3000, 3000], [6000, 3000], [6000, 6000], [3000, 6000]]),
        ])
        border = [w for w in walls if w.y1 == 3000 and w.y2 == 3000]
        self.assertEqual(len(border), 2, "Длинная грань режется по границе соседей")
        self.assertTrue(
            all(w.kind == "inner" for w in border),
            "Оба куска граничат с комнатой сверху — значит это перегородки",
        )
        self.assertEqual(sum(w.length_mm for w in border), 6000)
        self.assertEqual(sorted(border[0].rooms), sorted(border[0].rooms))
        self.assertEqual({tuple(sorted(w.rooms)) for w in border}, {(1, 2), (1, 3)})

    def test_outer_walls_are_thicker(self) -> None:
        from app.geometry import walls_from_rooms
        walls = walls_from_rooms([
            self._room(1, [[0, 0], [4000, 0], [4000, 3000], [0, 3000]]),
        ])
        self.assertEqual(len(walls), 4)
        self.assertTrue(all(w.kind == "outer" for w in walls))
        self.assertTrue(all(w.thickness_mm == 250 for w in walls))

    def test_perimeter_is_preserved(self) -> None:
        """Сумма наружных стен обязана дать периметр квартиры."""
        from app.geometry import walls_from_rooms
        walls = walls_from_rooms([
            self._room(1, [[0, 0], [5000, 0], [5000, 4000], [0, 4000]]),
            self._room(2, [[5000, 0], [9000, 0], [9000, 4000], [5000, 4000]]),
            self._room(3, [[0, 4000], [9000, 4000], [9000, 7500], [0, 7500]]),
        ])
        outer = sum(w.length_mm for w in walls if w.kind == "outer")
        self.assertAlmostEqual(outer, 2 * (9000 + 7500), delta=1)


class TestOpenings(unittest.TestCase):
    """Двери и окна привязываются к ближайшей стене."""

    @staticmethod
    def _walls():
        from app.geometry import Wall
        return [
            Wall(0, 0, 4000, 0, 250, "outer"),
            Wall(4000, 0, 4000, 3000, 120, "inner"),
        ]

    def test_door_lands_on_the_right_wall(self) -> None:
        from app.geometry import attach_openings
        walls = self._walls()
        attached = attach_openings(walls, [{"kind": "door", "x": 4000, "y": 1500, "width_mm": 900}])
        self.assertEqual(attached, 1)
        self.assertEqual(len(walls[0].openings), 0)
        self.assertEqual(len(walls[1].openings), 1)
        opening = walls[1].openings[0]
        self.assertEqual(opening["width_mm"], 900)
        self.assertEqual(opening["offset_mm"], 1050)

    def test_window_keeps_its_sill(self) -> None:
        from app.geometry import attach_openings
        walls = self._walls()
        attach_openings(walls, [{"kind": "window", "x": 2000, "y": 0, "width_mm": 1500}])
        opening = walls[0].openings[0]
        self.assertEqual(opening["kind"], "window")
        self.assertEqual(opening["sill_mm"], 800)

    def test_opening_in_the_middle_of_nowhere_is_dropped(self) -> None:
        """Дверь посреди комнаты — ошибка распознавания, а не дверь."""
        from app.geometry import attach_openings
        walls = self._walls()
        self.assertEqual(
            attach_openings(walls, [{"kind": "door", "x": 2000, "y": 9000, "width_mm": 900}]), 0
        )

    def test_opening_never_hangs_off_the_wall_end(self) -> None:
        from app.geometry import attach_openings
        walls = self._walls()
        attach_openings(walls, [{"kind": "door", "x": 0, "y": 0, "width_mm": 900}])
        opening = walls[0].openings[0]
        self.assertGreaterEqual(opening["offset_mm"], 0)
        self.assertLessEqual(opening["offset_mm"] + opening["width_mm"], 4000)


class TestSceneEndpoint(TestBase):
    """Данные для трёхмерной модели отдаются в миллиметрах."""

    def test_scene_of_an_empty_project(self) -> None:
        project_id = self.client.post(
            "/api/projects", json={"name": "Пустой", "ceiling_height_mm": 2900}
        ).json()["id"]
        scene = self.client.get(f"/api/projects/{project_id}/scene").json()
        self.assertEqual(scene["rooms"], [])
        self.assertEqual(scene["ceiling_height_mm"], 2900)
        self.client.delete(f"/api/projects/{project_id}")

    def test_scene_carries_rooms_walls_and_bounds(self) -> None:
        import json as js
        from app import db, geometry
        project_id = self.client.post(
            "/api/projects", json={"name": "Со сценой", "ceiling_height_mm": 2700}
        ).json()["id"]
        db.add_room(project_id, "Гостиная", "living",
                    js.dumps([[0, 0], [4000, 0], [4000, 3000], [0, 3000]]), 12.0)
        for wall in geometry.walls_from_rooms(db.list_rooms(project_id)):
            db.add_wall(project_id, wall.x1, wall.y1, wall.x2, wall.y2,
                        wall.thickness_mm, wall.kind)

        scene = self.client.get(f"/api/projects/{project_id}/scene").json()
        self.assertEqual(len(scene["rooms"]), 1)
        self.assertEqual(scene["rooms"][0]["area_m2"], 12.0)
        self.assertEqual(scene["rooms"][0]["height_mm"], 2700)
        self.assertEqual(len(scene["walls"]), 4)
        self.assertEqual(scene["bounds"]["width"], 4000)
        self.assertEqual(scene["bounds"]["depth"], 3000)
        self.client.delete(f"/api/projects/{project_id}")

    def test_viewer_page_opens(self) -> None:
        self.assertEqual(self.client.get("/project/1/viewer").status_code, 200)


class TestMessagesDoNotSendPeopleHunting(TestBase):
    """Сообщения не должны отправлять человека искать файлы на диске.

    Ключ вставляется в «Настройках», и все подсказки обязаны вести туда.
    Иначе человек ищет скрытый файл в папке, где его уже нет.
    """

    def test_missing_key_points_to_the_settings_page(self) -> None:
        from app.errors import ai_not_configured
        error = ai_not_configured("Чтение чертежа")
        self.assertIn("Настройки", error.hint)
        self.assertIn("Новый ключ", error.hint)
        self.assertNotIn("README", error.hint)
        self.assertNotIn("рядом с программой", error.hint)

    def test_analyse_without_key_explains_where_to_go(self) -> None:
        project_id = self.client.post(
            "/api/projects", json={"name": "Без ключа"}
        ).json()["id"]
        data = (BASE_DIR / "tests" / "fixtures" / "plan-zastroyshchik.pdf").read_bytes()
        upload = self.client.post(
            f"/api/projects/{project_id}/files",
            files={"files": ("plan.pdf", data, "application/pdf")},
        ).json()
        documents = self.client.get(f"/api/projects/{project_id}/documents").json()
        page_id = documents[0]["pages"][0]["id"]

        response = self.client.post(
            f"/api/projects/{project_id}/files/{page_id}/analyse"
        )
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertIn("ключа доступа", body["error"])
        self.assertIn("Настройки", body["hint"])
        self.client.delete(f"/api/projects/{project_id}")


class TestTruncatedAnswerIsRescued(TestBase):
    """Оборванный ответ модели не должен пропадать целиком.

    Модель пишет длинный список комнат и иногда не успевает его
    дописать. Обрывок всё равно ценен: комнаты в нём уже есть.
    """

    PDF = BASE_DIR / "tests" / "fixtures" / "plan-zastroyshchik.pdf"

    def test_cut_off_json_is_repaired(self) -> None:
        from app.plan_analysis import parse_answer
        broken = (
            '{"page_kind":"dimensioned_plan","rooms":['
            '{"name":"Гостиная","kind":"living","polygon":[[0,0],[100,0],[100,80],[0,80]]},'
            '{"name":"Спальня","kind":"bedroom","polygon":[[0,80],[100,80],[100,'
        )
        data = parse_answer(broken, finish_reason="length")
        self.assertEqual(len(data["rooms"]), 1, "Целая комната должна уцелеть")
        self.assertEqual(data["rooms"][0]["name"], "Гостиная")

    def test_whole_answer_still_parses(self) -> None:
        from app.plan_analysis import parse_answer
        data = parse_answer('{"rooms":[{"name":"Кухня"}],"items":[]}')
        self.assertEqual(data["rooms"][0]["name"], "Кухня")

    def test_hopeless_answer_explains_what_to_do(self) -> None:
        from app.plan_analysis import parse_answer
        with self.assertRaises(UserError) as caught:
            parse_answer("Извините, я не могу прочитать этот чертёж.", "stop")
        error = caught.exception
        self.assertIn("Настройк", error.hint, "Подсказка должна вести в настройки")
        self.assertTrue(error.technical, "Разработчику нужен кусок ответа")
        self.assertIn("Извините", error.technical)

    def test_length_limit_is_named_plainly(self) -> None:
        from app.plan_analysis import parse_answer
        with self.assertRaises(UserError) as caught:
            parse_answer('{"rooms":[{"name":"Обор', "length")
        self.assertIn("оборвался", caught.exception.message)
        self.assertNotIn("JSON", caught.exception.message)

    def test_rescued_answer_reaches_the_rooms(self) -> None:
        """Проверка целиком: оборванный ответ всё равно даёт комнаты."""
        from app.plan_analysis import analyse_page
        broken = (
            '{"page_kind":"dimensioned_plan","rooms":['
            '{"name":"Гостиная","kind":"living","polygon":[[100,100],[400,100],[400,300],[100,300]]},'
            '{"name":"Кухня","kind":"kitchen","polygon":[[400,100],[600'
        )
        fake = FakeProvider(broken)
        result = analyse_page(1, self.PDF, 1, True, 74.37, provider=fake)
        self.assertEqual(len(result.rooms), 1)
        self.assertEqual(result.rooms[0].name, "Гостиная")


# ── Ответ по частям ──────────────────────────────────────────────────


def _sse(*chunks: str) -> bytes:
    """Собирает ответ в том виде, в каком его присылает сервис."""
    return "".join(f"data: {chunk}\n\n" for chunk in chunks).encode("utf-8")


class StreamingTestBase(unittest.TestCase):
    """Заготовка: подменяет сервис заранее заготовленными ответами."""

    def setUp(self) -> None:
        from app.providers import OpenAiCompatProvider
        self.provider = OpenAiCompatProvider("https://example.invalid/v1", "key", "Тест")
        self.sent: list[dict] = []

    def _serve(self, handler) -> None:
        """Подменяет связь с сервисом на заранее заготовленные ответы."""
        import httpx, json as _json

        def record(request: httpx.Request) -> httpx.Response:
            self.sent.append(_json.loads(request.content))
            return handler(len(self.sent) - 1)

        self.provider._client = lambda: httpx.Client(
            transport=httpx.MockTransport(record)
        )


class TestStreamingAnswer(StreamingTestBase):
    """Разбор чертежа идёт минутами.

    Если всё это время по соединению ничего не передаётся, защита сервиса
    считает его брошенным и обрывает — человек видит «запрос не дошёл».
    Поэтому ответ забирается по частям: данные идут постоянно.
    """

    def test_запрос_идёт_потоком(self) -> None:
        """Главное: программа просит присылать ответ по частям."""
        import httpx
        self._serve(lambda n: httpx.Response(
            200,
            content=_sse('{"choices":[{"delta":{"content":"да"}}]}', "[DONE]"),
            headers={"content-type": "text/event-stream"},
        ))
        self.provider.ask("model", "вопрос")
        self.assertTrue(self.sent[0].get("stream"), "Ответ должен идти потоком")

    def test_куски_склеиваются_по_порядку(self) -> None:
        import httpx
        self._serve(lambda n: httpx.Response(
            200,
            content=_sse(
                '{"model":"claude","choices":[{"delta":{"content":"Гости"}}]}',
                '{"choices":[{"delta":{"content":"ная"}}]}',
                '{"choices":[{"delta":{},"finish_reason":"stop"}]}',
                '{"usage":{"prompt_tokens":11,"completion_tokens":22}}',
                "[DONE]",
            ),
            headers={"content-type": "text/event-stream"},
        ))
        answer = self.provider.ask("model", "вопрос")
        self.assertEqual(answer.text, "Гостиная")
        self.assertEqual(answer.finish_reason, "stop")
        self.assertEqual(answer.input_tokens, 11)
        self.assertEqual(answer.output_tokens, 22)
        self.assertEqual(answer.model, "claude")

    def test_расход_считается_и_из_потока(self) -> None:
        """Без счётчика расхода не посчитать стоимость и дневной предел."""
        import httpx
        self._serve(lambda n: httpx.Response(
            200,
            content=_sse(
                '{"choices":[{"delta":{"content":"текст"}}]}',
                '{"usage":{"prompt_tokens":5,"completion_tokens":7}}',
                "[DONE]",
            ),
            headers={"content-type": "text/event-stream"},
        ))
        self.provider.ask("model", "вопрос")
        self.assertEqual(
            self.sent[0].get("stream_options"), {"include_usage": True},
            "Счётчик расхода надо запросить явно",
        )

    def test_обрыв_ответа_виден(self) -> None:
        """Если модель не договорила, об этом должно быть известно."""
        import httpx
        self._serve(lambda n: httpx.Response(
            200,
            content=_sse(
                json.dumps({"choices": [{"delta": {"content": '{"rooms":['}}]}),
                '{"choices":[{"delta":{},"finish_reason":"length"}]}',
                "[DONE]",
            ),
            headers={"content-type": "text/event-stream"},
        ))
        self.assertEqual(self.provider.ask("model", "вопрос").finish_reason, "length")

    def test_мусор_в_потоке_не_ломает_ответ(self) -> None:
        import httpx
        self._serve(lambda n: httpx.Response(
            200,
            content=(
                b": keep-alive\n\n"
                + _sse("не json", '{"choices":[{"delta":{"content":"цел"}}]}')
                + b"\n"
                + _sse('{"choices":[{"delta":{"content":"ый"}}]}', "[DONE]")
            ),
            headers={"content-type": "text/event-stream"},
        ))
        self.assertEqual(self.provider.ask("model", "вопрос").text, "целый")

    def test_ответ_списком_блоков(self) -> None:
        import httpx
        self._serve(lambda n: httpx.Response(
            200,
            content=_sse(
                '{"choices":[{"delta":{"content":[{"text":"два "},{"text":"блока"}]}}]}',
                "[DONE]",
            ),
            headers={"content-type": "text/event-stream"},
        ))
        self.assertEqual(self.provider.ask("model", "вопрос").text, "два блока")


class TestStreamingFallback(StreamingTestBase):
    """Не каждый сервис умеет поток. Тогда пробуем запрос попроще."""

    def test_сервис_не_понял_счётчик(self) -> None:
        """Отказ на stream_options — повод повторить без него, но потоком."""
        import httpx

        def handler(number: int) -> httpx.Response:
            if number == 0:
                return httpx.Response(400, json={"error": {"message": "stream_options"}})
            return httpx.Response(
                200,
                content=_sse('{"choices":[{"delta":{"content":"готово"}}]}', "[DONE]"),
                headers={"content-type": "text/event-stream"},
            )

        self._serve(handler)
        self.assertEqual(self.provider.ask("model", "вопрос").text, "готово")
        self.assertNotIn("stream_options", self.sent[1])
        self.assertTrue(self.sent[1].get("stream"), "Поток всё ещё нужен")

    def test_сервис_совсем_не_умеет_поток(self) -> None:
        import httpx

        def handler(number: int) -> httpx.Response:
            if number < 2:
                return httpx.Response(400, json={"error": {"message": "stream"}})
            return httpx.Response(200, json={
                "model": "claude",
                "choices": [{"message": {"content": "обычный ответ"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 4},
            })

        self._serve(handler)
        answer = self.provider.ask("model", "вопрос")
        self.assertEqual(answer.text, "обычный ответ")
        self.assertEqual(len(self.sent), 3)
        self.assertNotIn("stream", self.sent[2])

    def test_пустой_поток_не_остаётся_без_ответа(self) -> None:
        import httpx

        def handler(number: int) -> httpx.Response:
            if number < 2:
                return httpx.Response(
                    200, content=b"data: [DONE]\n\n",
                    headers={"content-type": "text/event-stream"},
                )
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "всё-таки ответ"}}],
            })

        self._serve(handler)
        self.assertEqual(self.provider.ask("model", "вопрос").text, "всё-таки ответ")

    def test_настоящая_ошибка_не_прячется_за_повтором(self) -> None:
        """Отказ по ключу — не повод слать запрос снова и снова."""
        import httpx
        self._serve(lambda n: httpx.Response(401, json={"error": "bad key"}))
        with self.assertRaises(UserError) as caught:
            self.provider.ask("model", "вопрос")
        self.assertIn("ключ", caught.exception.message.lower())
        self.assertEqual(len(self.sent), 1, "Незачем повторять запрос с плохим ключом")

    def test_если_не_вышло_ничем_то_человеку_понятная_ошибка(self) -> None:
        import httpx
        self._serve(lambda n: httpx.Response(400, json={"error": "нет"}))
        with self.assertRaises(UserError) as caught:
            self.provider.ask("model", "вопрос")
        self.assertNotIn("stream", caught.exception.message.lower())
        self.assertTrue(caught.exception.technical, "Подробности нужны для разбора")


class TestLongRequestIsExplained(unittest.TestCase):
    """Если долгий запрос всё же оборвался, причину надо назвать верно."""

    def test_не_успел_ответить(self) -> None:
        import httpx
        from app.netcheck import diagnose
        found = diagnose("https://example.com/v1", httpx.ReadTimeout("время вышло"))
        self.assertIn("не успел", found.message.lower())
        self.assertIn("ещё раз", found.hint)

    def test_соединение_оборвалось(self) -> None:
        import httpx
        from app.netcheck import diagnose
        found = diagnose(
            "https://example.com/v1", httpx.RemoteProtocolError("закрыли")
        )
        self.assertIn("оборвалась", found.message.lower())

    def test_человека_не_отправляют_искать_опечатку_в_env(self) -> None:
        """Адрес сервиса она не вводила — винить его было неправдой."""
        import httpx
        from app.netcheck import diagnose
        for error in (httpx.ReadTimeout("x"), httpx.RemoteProtocolError("x"), None):
            with self.subTest(error=type(error).__name__):
                found = diagnose("https://example.com/v1", error)
                self.assertNotIn(".env", found.hint)
                self.assertNotIn("опечатка", found.hint)


# ── Подгонка контуров под чертёж ─────────────────────────────────────

def _grid_flat(noise: int = 0, seed: int = 1):
    """Выдуманная квартира: 7 комнат, перегородки, известные площади."""
    import random
    from app.align import area_m2
    cx, cy = [0, 3400, 6200, 8000, 11000], [0, 3300, 4400, 7000]
    outer, inner = 125, 60

    def room(i, j, k, l):
        x1 = cx[i] + (outer if i == 0 else inner)
        x2 = cx[j] - (outer if j == len(cx) - 1 else inner)
        y1 = cy[k] + (outer if k == 0 else inner)
        y2 = cy[l] - (outer if l == len(cy) - 1 else inner)
        return [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]

    true = [room(0, 1, 0, 1), room(1, 3, 0, 1), room(3, 4, 0, 1),
            room(0, 1, 1, 2), room(1, 2, 1, 3), room(2, 4, 1, 2),
            room(2, 4, 2, 3)]
    targets = [round(area_m2([(float(a), float(b)) for a, b in p]), 2) for p in true]
    if not noise:
        return true, true, targets
    rnd = random.Random(seed)
    guess = [[(x + rnd.randint(-noise, noise), y + rnd.randint(-noise, noise))
              for x, y in p] for p in true]
    return true, guess, targets


def _area_error(polygons, targets) -> float:
    from app.align import area_m2
    return round(sum(
        abs(area_m2([(float(x), float(y)) for x, y in poly]) - target)
        for poly, target in zip(polygons, targets) if target
    ), 3)


class TestAlignment(unittest.TestCase):
    """Площади из чертежа известны точно — контуры обязаны к ним сойтись.

    «Квартира должна оставаться моей квартирой»: AI обводит комнаты
    по картинке и промахивается на сантиметры, а экспликация на чертеже
    даёт точную площадь каждой комнаты. Подгонка сводит их вместе.
    """

    def test_площади_сходятся_с_чертежом(self) -> None:
        from app.align import align_rooms
        true, guess, targets = _grid_flat(noise=100, seed=3)
        fixed = align_rooms(guess, targets)
        self.assertLess(_area_error(fixed, targets), 0.3)
        self.assertGreater(_area_error(guess, targets), 1.0)

    def test_никогда_не_делает_хуже(self) -> None:
        """Главная гарантия: подгонка не имеет права испортить планировку."""
        from app.align import align_rooms
        for seed in range(15):
            for noise in (40, 80, 120):
                with self.subTest(seed=seed, noise=noise):
                    _, guess, targets = _grid_flat(noise=noise, seed=seed)
                    fixed = align_rooms(guess, targets)
                    self.assertLessEqual(
                        _area_error(fixed, targets),
                        _area_error(guess, targets) + 0.01,
                    )

    def test_стены_остаются_на_месте(self) -> None:
        """Комната не должна уехать: сдвиги — сантиметры, а не метры."""
        from app.align import align_rooms, report
        _, guess, targets = _grid_flat(noise=100, seed=5)
        fixed = align_rooms(guess, targets)
        self.assertLess(report(guess, fixed, targets)["biggest_shift_mm"], 350)

    def test_форма_комнат_не_меняется(self) -> None:
        from app.align import align_rooms
        _, guess, targets = _grid_flat(noise=80, seed=2)
        fixed = align_rooms(guess, targets)
        self.assertEqual(len(fixed), len(guess))
        for old, new in zip(guess, fixed):
            self.assertEqual(len(old), len(new), "Углы не добавляются и не пропадают")

    def test_повторная_подгонка_ничего_не_ломает(self) -> None:
        from app.align import align_rooms
        _, guess, targets = _grid_flat(noise=100, seed=8)
        once = align_rooms(guess, targets)
        twice = align_rooms(once, targets)
        self.assertLessEqual(_area_error(twice, targets),
                             _area_error(once, targets) + 0.01)

    def test_без_площадей_контуры_не_трогаем(self) -> None:
        from app.align import align_rooms
        _, guess, _ = _grid_flat(noise=80, seed=4)
        self.assertEqual(align_rooms(guess, [None] * len(guess)), guess)

    def test_комната_без_подписанной_площади_переживает_подгонку(self) -> None:
        from app.align import align_rooms
        _, guess, targets = _grid_flat(noise=80, seed=6)
        targets = list(targets)
        targets[2] = None                      # площадь этой комнаты неизвестна
        fixed = align_rooms(guess, targets)
        self.assertEqual(len(fixed[2]), len(guess[2]))
        self.assertLess(_area_error(fixed, targets), _area_error(guess, targets))

    def test_обрывки_не_роняют_подгонку(self) -> None:
        from app.align import align_rooms
        self.assertEqual(align_rooms([], []), [])
        self.assertEqual(align_rooms([[(0, 0), (10, 0)]], [1.0]), [[(0, 0), (10, 0)]])


class TestOldProjectGetsFixed(TestBase):
    """Разбор уже оплачен. Улучшать его надо без нового запроса к сервису."""

    def _project_with_rooms(self) -> int:
        project_id = db.create_project("Проверка", ceiling_height_mm=2900)
        _, guess, targets = _grid_flat(noise=100, seed=11)
        for order, (poly, target) in enumerate(zip(guess, targets)):
            db.add_room(project_id=project_id, name=f"Комната {order + 1}",
                        kind="room", polygon=json.dumps(poly),
                        declared_area_m2=target, sort_order=order)
        walls = geometry_module().walls_from_rooms(db.list_rooms(project_id))
        for wall in walls:
            db.add_wall(project_id, wall.x1, wall.y1, wall.x2, wall.y2,
                        wall.thickness_mm, wall.kind)
        self.targets = targets
        return project_id

    def test_сохранённые_комнаты_подтягиваются_сами(self) -> None:
        from app import plan_analysis
        project_id = self._project_with_rooms()
        before = [geometry_module().parse_polygon(r["polygon"])
                  for r in db.list_rooms(project_id)]
        changes = plan_analysis.realign_saved_rooms(project_id)

        self.assertTrue(changes and changes["applied"])
        after = [geometry_module().parse_polygon(r["polygon"])
                 for r in db.list_rooms(project_id)]
        self.assertLess(_area_error(after, self.targets),
                        _area_error(before, self.targets))
        self.assertLess(_area_error(after, self.targets), 0.3)

    def test_стены_перекладываются_под_новые_комнаты(self) -> None:
        from app import plan_analysis
        project_id = self._project_with_rooms()
        plan_analysis.realign_saved_rooms(project_id)
        walls = db.list_walls(project_id)
        self.assertTrue(walls, "Стены должны остаться")

        # Каждая стена обязана лежать на грани какой-нибудь комнаты.
        corners = {
            (x, y)
            for room in db.list_rooms(project_id)
            for x, y in geometry_module().parse_polygon(room["polygon"])
        }
        for wall in walls:
            near = min(
                abs(wall["x1"] - x) + abs(wall["y1"] - y) for x, y in corners
            )
            self.assertLess(near, 200, "Стена оторвалась от комнат")

    def test_починка_идёт_один_раз_и_молча(self) -> None:
        from app import server
        project_id = self._project_with_rooms()
        server.repair_old_projects()
        first = [r["polygon"] for r in db.list_rooms(project_id)]
        server.repair_old_projects()
        self.assertEqual([r["polygon"] for r in db.list_rooms(project_id)], first)

    def test_пустой_проект_не_ломает_запуск(self) -> None:
        from app import plan_analysis, server
        db.create_project("Совсем пустой", ceiling_height_mm=2900)
        self.assertIsNone(plan_analysis.realign_saved_rooms(999999))
        server.repair_old_projects()


def geometry_module():
    from app import geometry
    return geometry


# ── Одна лоджия, а не две ────────────────────────────────────────────

class TestLoggiaCountedOnce(unittest.TestCase):
    """В экспликации лоджию подписывают дважды.

    Сначала полную площадь, потом её же с понижающим коэффициентом
    (0,5 для лоджии, 0,3 для балкона). Программа принимала это за две
    разные лоджии. На её чертеже: 4,49 м² и 2,25 м² — одна лоджия.
    """

    PDF = Path(__file__).parent / "fixtures" / "plan-zastroyshchik.pdf"

    def test_на_её_чертеже_лоджия_одна(self) -> None:
        from app.plan_vector import read_plan
        plan = read_plan(self.PDF, 1, 74.37)
        self.assertEqual(plan.extra_areas, [4.49])

    def test_комнаты_и_итог_не_пострадали(self) -> None:
        from app.plan_vector import read_plan
        plan = read_plan(self.PDF, 1, 74.37)
        self.assertEqual(len(plan.room_areas), 7)
        self.assertAlmostEqual(sum(plan.room_areas), 74.37, places=2)

    def test_арифметика_чертежа_подтверждает_повтор(self) -> None:
        """74,37 + 2,25 = 76,62 — строка с листа, а не догадка."""
        from app.plan_vector import _drop_coefficient_twins
        kept = _drop_coefficient_twins([4.49, 2.25], 74.37, [74.37, 76.62, 78.86])
        self.assertEqual(kept, [4.49])

    def test_два_настоящих_балкона_не_слипаются(self) -> None:
        """Один балкон ровно вдвое меньше другого — но это два балкона.

        Потерять часть квартиры хуже, чем показать лишнюю подпись,
        поэтому без подтверждения арифметикой повтор не убирается.
        """
        from app.plan_vector import _drop_coefficient_twins
        kept = _drop_coefficient_twins([6.0, 3.0], 74.37, [74.37, 83.37])
        self.assertEqual(kept, [6.0, 3.0])

    def test_без_итогов_ничего_не_выбрасываем(self) -> None:
        from app.plan_vector import _drop_coefficient_twins
        self.assertEqual(_drop_coefficient_twins([4.49, 2.25], 74.37, []), [4.49, 2.25])
        self.assertEqual(_drop_coefficient_twins([4.49, 2.25], None, [76.62]), [4.49, 2.25])

    def test_балкон_с_коэффициентом_0_3_тоже_повтор(self) -> None:
        from app.plan_vector import _drop_coefficient_twins
        kept = _drop_coefficient_twins([5.0, 1.5], 60.0, [60.0, 61.5, 65.0])
        self.assertEqual(kept, [5.0])

    def test_модели_говорят_сколько_лоджий_искать(self) -> None:
        from app.plan_vector import read_plan
        from app.plan_analysis import build_prompt
        plan = read_plan(self.PDF, 1, 74.37)
        prompt = build_prompt(plan, 1540, 1100, 15.24, 1.0)
        self.assertIn("ровно одно", prompt)
        self.assertNotIn("2.25", prompt)

    def test_лишняя_лоджия_попадает_в_предупреждения(self) -> None:
        from app.plan_analysis import Analysis, RoomGuess, _add_warnings
        from app.plan_vector import VectorPlan
        plan = VectorPlan()
        plan.extra_areas = [4.49]
        result = Analysis()
        square = [(0, 0), (2000, 0), (2000, 2000), (0, 2000)]
        result.rooms = [
            RoomGuess("Лоджия", "balcony", square, 4.0),
            RoomGuess("Балкон", "balcony", square, 2.0),
        ]
        _add_warnings(result, plan)
        self.assertTrue(any("лоджия или балкон" in w for w in result.warnings))


# ── Разбор виден и после обновления страницы ─────────────────────────

class TestSavedAnalysisIsVisible(TestBase):
    """Разбор плана стоит денег и делается один раз.

    Раньше его результат показывался только в ту минуту, когда он
    пришёл: стоило обновить страницу — и на экране оставалась одна
    строчка. Человек решает, что разбор не сохранился.
    """

    def _with_rooms(self) -> int:
        project_id = db.create_project("С разбором", ceiling_height_mm=2900)
        db.update_project(project_id, declared_area_m2=30.0)
        db.add_room(project_id=project_id, name="Гостиная", kind="living",
                    polygon=json.dumps([[0, 0], [5000, 0], [5000, 4000], [0, 4000]]),
                    declared_area_m2=20.0, sort_order=0)
        db.add_room(project_id=project_id, name="Кухня", kind="kitchen",
                    polygon=json.dumps([[5000, 0], [8000, 0], [8000, 4000], [5000, 4000]]),
                    declared_area_m2=12.0, sort_order=1)
        db.add_room(project_id=project_id, name="Лоджия", kind="balcony",
                    polygon=json.dumps([[0, 4000], [3000, 4000], [3000, 5500], [0, 5500]]),
                    declared_area_m2=4.49, sort_order=2)
        return project_id

    def test_комнаты_возвращаются_с_площадями(self) -> None:
        project_id = self._with_rooms()
        data = self.client.get(f"/api/projects/{project_id}/rooms").json()
        self.assertEqual(len(data["rooms"]), 3)
        self.assertAlmostEqual(data["rooms"][0]["area_m2"], 20.0, places=2)
        self.assertAlmostEqual(data["rooms"][1]["area_m2"], 12.0, places=2)

    def test_лоджия_не_входит_в_площадь_квартиры(self) -> None:
        project_id = self._with_rooms()
        data = self.client.get(f"/api/projects/{project_id}/rooms").json()
        self.assertAlmostEqual(data["total_area_m2"], 32.0, places=2)
        self.assertAlmostEqual(data["outside_area_m2"], 4.5, places=1)

    def test_известна_площадь_по_документам(self) -> None:
        project_id = self._with_rooms()
        data = self.client.get(f"/api/projects/{project_id}/rooms").json()
        self.assertEqual(data["declared_total_m2"], 30.0)

    def test_отклонение_от_чертежа_посчитано(self) -> None:
        project_id = self._with_rooms()
        rooms = self.client.get(f"/api/projects/{project_id}/rooms").json()["rooms"]
        self.assertEqual(rooms[0]["deviation_percent"], 0.0)
        self.assertIsNotNone(rooms[1]["deviation_percent"])

    def test_проект_без_разбора_отдаёт_пустой_список(self) -> None:
        project_id = db.create_project("Пустой", ceiling_height_mm=2900)
        data = self.client.get(f"/api/projects/{project_id}/rooms").json()
        self.assertEqual(data["rooms"], [])
        self.assertEqual(data["total_area_m2"], 0)

    def test_неуверенные_предметы_посчитаны(self) -> None:
        project_id = self._with_rooms()
        db.add_item(project_id=project_id, category="furniture", subtype="bed",
                    label="Кровать", x=100, y=100, width_mm=1600, depth_mm=2000,
                    height_mm=500, rotation_deg=0, confidence=0.3)
        data = self.client.get(f"/api/projects/{project_id}/rooms").json()
        self.assertEqual(data["unsure_items"], 1)


class TestSecondCopyIsNoticed(unittest.TestCase):
    """Две запущенные копии — верный способ запутаться в версиях."""

    def test_свободный_порт_ничего_не_говорит(self) -> None:
        import run
        self.assertIsNone(run.running_version(1))

    def test_своя_программа_узнаётся_по_версии(self) -> None:
        import run
        from app import __version__
        with self.client_on_port() as port:
            self.assertEqual(run.running_version(port), __version__)

    @contextlib.contextmanager
    def client_on_port(self):
        """Поднимает настоящий сервер на свободном порту."""
        import threading
        import uvicorn
        from app.server import app

        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="critical")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            for _ in range(200):
                if server.started and server.servers:
                    break
                time.sleep(0.05)
            yield server.servers[0].sockets[0].getsockname()[1]
        finally:
            server.should_exit = True
            thread.join(timeout=5)
