.DEFAULT_GOAL := help

PACKAGE      := wf
PROJECT_DIR  := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
UV           := $(shell command -v uv 2>/dev/null || echo $$HOME/.local/bin/uv)

.PHONY: help install reinstall uninstall dev clean check-uv

help: ## Показать доступные цели
	@awk 'BEGIN {FS = ":.*##"; printf "Цели:\n"} /^[a-zA-Z_-]+:.*##/ {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

check-uv: ## Поставить uv, если его нет
	@if ! command -v uv >/dev/null 2>&1 && [ ! -x "$$HOME/.local/bin/uv" ]; then \
		echo ">> uv не найден, ставлю официальным скриптом..."; \
		curl -LsSf https://astral.sh/uv/install.sh | sh; \
	else \
		echo ">> uv: $$($(UV) --version)"; \
	fi

install: check-uv ## Установить wf глобально через uv tool install
	$(UV) tool install --from $(PROJECT_DIR) $(PACKAGE)
	@echo ""
	@echo ">> Готово. Проверь, что директория с бинарём в PATH:"
	@echo "   $$($(UV) tool dir --bin)"

reinstall: check-uv ## Переустановить wf (после правок кода)
	$(UV) tool install --reinstall --from $(PROJECT_DIR) $(PACKAGE)

uninstall: ## Удалить wf и ~/.wf/ (конфиг). FORCE=1 — без подтверждения. Каталог данных (data_dir, по умолчанию ~/wf) удалите вручную, если нужно
	@echo ">> Будут удалены: wf tool и $$HOME/.wf/ (конфиг)"; \
	echo ">> Каталог данных (data_dir из ~/.wf/config.yaml, по умолчанию ~/wf) НЕ трогаю — удалите вручную при необходимости"; \
	if [ "$(FORCE)" != "1" ]; then \
		printf ">> Продолжить? [y/N] "; read ans; \
		case "$$ans" in y|Y|yes|YES) ;; *) echo "Отменено."; exit 1;; esac; \
	fi; \
	$(UV) tool uninstall $(PACKAGE) || true; \
	rm -rf "$$HOME/.wf"; \
	echo ">> Готово."

dev: check-uv ## Создать локальный .venv с editable-установкой (для разработки)
	$(UV) venv $(PROJECT_DIR)/.venv
	$(UV) pip install --python $(PROJECT_DIR)/.venv/bin/python -e $(PROJECT_DIR)
	@echo ""
	@echo ">> Активировать: source .venv/bin/activate"

clean: ## Удалить локальные артефакты сборки (.venv, build, dist, __pycache__)
	rm -rf $(PROJECT_DIR)/.venv $(PROJECT_DIR)/build $(PROJECT_DIR)/dist
	find $(PROJECT_DIR)/src -type d -name __pycache__ -exec rm -rf {} +
	find $(PROJECT_DIR) -type d -name "*.egg-info" -exec rm -rf {} +
