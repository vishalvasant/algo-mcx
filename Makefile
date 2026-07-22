.PHONY: build up up-prod down logs ps health dev

build:
	docker compose build

# Local dev without Docker — UI at http://localhost:8080 (requires postgres running)
dev:
	./scripts/dev-local.sh

up:
	docker compose up -d --build

up-prod:
	./scripts/deploy.sh prod

down:
	docker compose down

logs:
	docker compose logs -f

ps:
	docker compose ps

health:
	@curl -sf http://127.0.0.1:$${WEB_PORT:-8080}/api/health | python3 -m json.tool
