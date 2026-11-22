import json
import os
import tempfile
import unittest
from unittest import mock

os.environ.update({
    "AMO_SUBDOMAIN": "demo", "AMO_TOKEN": "t", "MAX_TOKEN": "maxtok",
    "WEBHOOK_SECRET": "s3cret", "AMO_MONTAGE_STATUS_ID": "777",
})
import app as bot  # noqa: E402

LEAD = {
    "name": "Септик Иванов", "responsible_user_id": 42,
    "custom_fields_values": [
        {"field_name": "Адрес", "values": [{"value": "д. Ивановка, ул. Лесная, 5"}]},
        {"field_name": "Дата монтажа", "values": [{"value": 1791885600}]},  # 2026-10-13 10:00 UTC = 13:00 МСК
    ],
    "_embedded": {"contacts": [{"id": 9, "is_main": True}]},
}
CONTACT = {"name": "Иван Петров", "custom_fields_values": [
    {"field_code": "PHONE", "values": [{"value": "+79000000000"}]}]}


def fake_get(url, headers=None, params=None, timeout=None):
    r = mock.Mock(status_code=200, content=b"x")
    r.json.return_value = LEAD if "/leads/" in url else CONTACT
    return r


class Tests(unittest.TestCase):
    def setUp(self):
        self.client = bot.app.test_client()
        bot._recent.clear()
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump([{"name": "Сергей", "amo_user_id": 42, "max_user_id": 555}], f)
        f.close()
        os.environ["EMPLOYEES_FILE"] = f.name
        os.environ.pop("MANAGER_MAX_USER_ID", None)

    def test_message_goes_to_responsible_installer(self):
        with mock.patch("app.requests.get", fake_get), mock.patch("app.requests.post") as post:
            post.return_value = mock.Mock(status_code=200)
            bot.notify_installer("1")
        _, kw = post.call_args
        self.assertEqual(kw["params"], {"user_id": 555})
        self.assertEqual(kw["headers"], {"Authorization": "maxtok"})
        text = kw["json"]["text"]
        for part in ("Септик Иванов", "Лесная", "+79000000000", "Иван Петров", "13.10.2026 13:00"):
            self.assertIn(part, text)

    def test_unknown_installer_goes_to_manager(self):
        os.environ["MANAGER_MAX_USER_ID"] = "999"
        LEAD2 = dict(LEAD, responsible_user_id=1)
        with mock.patch("app.requests.get", lambda url, **k: mock.Mock(
                content=b"x", json=lambda: LEAD2 if "/leads/" in url else CONTACT)), \
                mock.patch("app.requests.post") as post:
            post.return_value = mock.Mock(status_code=200)
            bot.notify_installer("1")
        self.assertEqual(post.call_args[1]["params"], {"user_id": "999"})

    def test_webhook_rejects_wrong_secret(self):
        r = self.client.post("/amo-webhook?secret=bad", data={"leads[status][0][id]": "1"})
        self.assertEqual(r.status_code, 403)

    def test_webhook_triggers_once_and_filters_status(self):
        data = {"leads[status][0][id]": "1", "leads[status][0][status_id]": "777"}
        with mock.patch("app.threading.Thread") as th:
            self.assertEqual(self.client.post("/amo-webhook?secret=s3cret", data=data).status_code, 200)
            self.client.post("/amo-webhook?secret=s3cret", data=data)  # дубль
            self.assertEqual(th.call_count, 1)
            self.client.post("/amo-webhook?secret=s3cret", data={
                "leads[status][0][id]": "2", "leads[status][0][status_id]": "111"})  # чужой этап
            self.assertEqual(th.call_count, 1)

    def test_max_start_replies_with_user_id(self):
        with mock.patch("app.requests.post") as post:
            post.return_value = mock.Mock(status_code=200)
            r = self.client.post("/max-webhook?secret=s3cret",
                                 json={"update_type": "bot_started", "user": {"user_id": 321}})
        self.assertEqual(r.status_code, 200)
        self.assertIn("321", post.call_args[1]["json"]["text"])


if __name__ == "__main__":
    unittest.main()
