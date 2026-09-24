.PHONY: up down logs ps test lint proto build-dataset train seq-validate-b seq-experiments db-roles clean-junk ui-dev ui-build
PYTHONPATH_LOCAL = ml/src:services/insight/src:services/orchestrator/src:services/analyzer/src:services/collector/src:libs/ws_common/src
up:            ; docker compose up -d --build
down:          ; docker compose down
logs:          ; docker compose logs -f
ps:            ; docker compose ps
# Модульные тесты без Docker и сети (565+ тестов, стандартный unittest).
test:
	PYTHONPATH=$(PYTHONPATH_LOCAL) python -m unittest discover -s services/collector/tests/unit -t .
	PYTHONPATH=$(PYTHONPATH_LOCAL) python -m unittest discover -s services/analyzer/tests/unit -t .
	PYTHONPATH=$(PYTHONPATH_LOCAL) python -m unittest discover -s services/orchestrator/tests/unit -t .
	PYTHONPATH=$(PYTHONPATH_LOCAL) python -m unittest discover -s services/orchestrator/tests/contract -t .
	PYTHONPATH=$(PYTHONPATH_LOCAL) python -m unittest discover -s services/insight/tests/unit -t .
	PYTHONPATH=$(PYTHONPATH_LOCAL) python -m unittest discover -s ml/tests -t ml
	python tools/check_proto_conformance.py
	python tools/check_sql_conformance.py
	python tools/check_ui_conformance.py
lint:          ; uv run ruff check . && uv run mypy libs services ml
proto:         ; bash tools/gen_proto.sh
build-dataset: ; docker compose --profile ml run --rm trainer build-dataset
train:         ; docker compose --profile ml run --rm trainer train
seq-validate-b: ; docker compose --profile ml run --rm trainer seq-validate-b
seq-experiments: ; docker compose --profile ml run --rm trainer seq-experiments --embedder e5
# Повторно применить роли и пароли из .env к уже созданному тому PostgreSQL (данные не теряются).
db-roles:      ; docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U postgres -d postgres -f /docker-entrypoint-initdb.d/00_roles.sql
# Удалить каталоги-артефакты несработавшего brace expansion и отладочные файлы.
clean-junk:    ; find . -depth -type d -name '*{*' -not -path './.git/*' -exec rm -rf {} + ; rm -f all_logs.txt a.xml
ui-dev:        ; cd services/ui && npm install && npm run dev
ui-build:      ; cd services/ui && npm install && npm run build