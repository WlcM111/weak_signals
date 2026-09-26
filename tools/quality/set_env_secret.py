"""Запись секрета в .env скрытым вводом: python3 tools/quality/set_env_secret.py WS_ИМЯ_ПЕРЕМЕННОЙ

Значение вводится скрыто (getpass) и нигде не печатается. Пустой ввод — ничего не менять.
Строка ИМЯ=... заменяется (если она одна) или добавляется в конец файла; если строк с этим именем
несколько — стоп без изменений. До записи копия .env кладётся в ~/ws-backups/env.bak-secret-<время>.
"""

from __future__ import annotations

import getpass
import re
import shutil
import sys
import time
from pathlib import Path

ENV = Path(".env")
_NAME_RE = re.compile(r"^WS_[A-Z0-9_]+$")


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not _NAME_RE.match(argv[1]):
        print("использование: python3 tools/quality/set_env_secret.py WS_ИМЯ_ПЕРЕМЕННОЙ")
        return 2
    name = argv[1]
    if not ENV.is_file():
        print("СТОП: нет файла .env — запустите из корня проекта")
        return 1
    value = getpass.getpass(f"Вставьте значение {name} (ввод скрыт; пусто — пропустить) и нажмите Enter: ").strip()
    if not value:
        print(f"пропущено: {name} не изменён")
        return 0
    if any(char.isspace() for char in value):
        print("СТОП: в значении есть пробелы или переводы строки — скопируйте ключ целиком без них")
        return 1
    lines = ENV.read_text(encoding="utf-8").splitlines(keepends=True)
    index = [i for i, line in enumerate(lines) if line.startswith(f"{name}=")]
    if len(index) > 1:
        print(f"СТОП: строк {name} в .env: {len(index)} (номера {[i + 1 for i in index]}); .env не изменён")
        return 1
    backup_dir = Path.home() / "ws-backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = backup_dir / f"env.bak-secret-{stamp}"
    number = 2
    while backup.exists():  # два ввода в одну секунду не должны затирать прежнюю копию
        backup = backup_dir / f"env.bak-secret-{stamp}-{number}"
        number += 1
    shutil.copy2(ENV, backup)
    if index:
        ending = "\n" if lines[index[0]].endswith("\n") else ""
        lines[index[0]] = f"{name}={value}{ending}"
    else:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(f"{name}={value}\n")
    ENV.write_text("".join(lines), encoding="utf-8")
    print(f"ЗАПИСАНО: {name} (длина {len(value)} символов); копия .env: {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
