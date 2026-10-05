"""Изоляция тестов от настоящих данных пользователя.

Папка данных теперь живёт в домашней папке, а не рядом с программой.
Без этой заглушки тесты писали бы базу прямо туда — то есть в рабочие
данные человека, который запустил их у себя.

Импортируется ПЕРВЫМ, до wbads и run: wbads.config вычисляет пути при
импорте модуля, и позже переменную окружения менять поздно.
"""

import atexit
import os
import shutil
import tempfile

_SANDBOX = tempfile.mkdtemp(prefix="wbads-tests-")
os.environ["WBADS_HOME"] = _SANDBOX
atexit.register(lambda: shutil.rmtree(_SANDBOX, ignore_errors=True))
