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

    def ask(self, model, prompt, images=None, pdf=None, max_tokens=8000):
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
        self.assertIn("планировку", caught.exception.message)
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
