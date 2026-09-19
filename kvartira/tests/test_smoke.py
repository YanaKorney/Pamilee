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
        self.assertIn("Масштаб чертежа уже известен", fake.asked_prompt)
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
