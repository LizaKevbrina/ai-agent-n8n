# ===== Makefile =====
.PHONY: help build start stop restart logs clean test load-test deploy

# Colors
RED    := \033[0;31m
GREEN  := \033[0;32m
YELLOW := \033[0;33m
NC     := \033[0m # No Color

help: ## Show this help message
	@echo "$(GREEN)AI Agent Microservices - Management Commands$(NC)"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  $(YELLOW)%-20s$(NC) %s\n", $$1, $$2}'

# ============================================
# BUILD & DEPLOYMENT
# ============================================

build: ## Build all Docker images
	@echo "$(GREEN)Building all services...$(NC)"
	docker-compose build

build-service: ## Build specific service (use: make build-service SERVICE=llm)
	@echo "$(GREEN)Building $(SERVICE) service...$(NC)"
	docker-compose build $(SERVICE)-service

start: ## Start all services
	@echo "$(GREEN)Starting all services...$(NC)"
	docker-compose up -d
	@echo "$(GREEN)Services started! Check health:$(NC)"
	@make health

start-infra: ## Start only infrastructure (Redis, Postgres, monitoring)
	@echo "$(GREEN)Starting infrastructure...$(NC)"
	docker-compose up -d redis postgres prometheus grafana alertmanager

start-services: ## Start application services only
	@echo "$(GREEN)Starting application services...$(NC)"
	docker-compose up -d validation-service intent-service rag-service llm-service memory-service logging-service

stop: ## Stop all services
	@echo "$(YELLOW)Stopping all services...$(NC)"
	docker-compose down

stop-services: ## Stop only application services (keep infrastructure)
	@echo "$(YELLOW)Stopping application services...$(NC)"
	docker-compose stop validation-service intent-service rag-service llm-service memory-service logging-service

restart: ## Restart all services
	@echo "$(YELLOW)Restarting all services...$(NC)"
	docker-compose restart

restart-service: ## Restart specific service (use: make restart-service SERVICE=llm)
	@echo "$(YELLOW)Restarting $(SERVICE) service...$(NC)"
	docker-compose restart $(SERVICE)-service

# ============================================
# SCALING
# ============================================

scale-llm: ## Scale LLM service (use: make scale-llm REPLICAS=10)
	@echo "$(GREEN)Scaling LLM service to $(REPLICAS) replicas...$(NC)"
	docker-compose up -d --scale llm-service=$(REPLICAS)

scale-rag: ## Scale RAG service (use: make scale-rag REPLICAS=5)
	@echo "$(GREEN)Scaling RAG service to $(REPLICAS) replicas...$(NC)"
	docker-compose up -d --scale rag-service=$(REPLICAS)

# ============================================
# MONITORING & LOGS
# ============================================

logs: ## Show logs from all services
	docker-compose logs -f

logs-service: ## Show logs from specific service (use: make logs-service SERVICE=llm)
	docker-compose logs -f $(SERVICE)-service

logs-errors: ## Show only error logs
	docker-compose logs -f | grep -i error

health: ## Check health of all services
	@echo "$(GREEN)Checking service health...$(NC)"
	@curl -s http://localhost:8001/health | jq '.' || echo "$(RED)Validation service down$(NC)"
	@curl -s http://localhost:8004/health | jq '.' || echo "$(RED)Intent service down$(NC)"
	@curl -s http://localhost:8003/health | jq '.' || echo "$(RED)RAG service down$(NC)"
	@curl -s http://localhost:8005/health | jq '.' || echo "$(RED)LLM service down$(NC)"
	@curl -s http://localhost:8006/health | jq '.' || echo "$(RED)Memory service down$(NC)"
	@curl -s http://localhost:8007/health | jq '.' || echo "$(RED)Logging service down$(NC)"

metrics: ## Show Prometheus metrics
	@echo "$(GREEN)Prometheus: http://localhost:9090$(NC)"
	@echo "$(GREEN)Grafana: http://localhost:3000$(NC)"
	@echo "$(GREEN)Alertmanager: http://localhost:9093$(NC)"

ps: ## Show running containers
	docker-compose ps

stats: ## Show container resource usage
	docker stats

# ============================================
# TESTING
# ============================================

test: ## Run all tests
	@echo "$(GREEN)Running tests...$(NC)"
	pytest tests/ -v --cov=services --cov-report=html --cov-report=term-missing

test-unit: ## Run unit tests only
	@echo "$(GREEN)Running unit tests...$(NC)"
	pytest tests/ -v -k "not integration" --cov=services

test-integration: ## Run integration tests
	@echo "$(GREEN)Running integration tests...$(NC)"
	@make start
	@sleep 10
	pytest tests/test_integration.py -v

test-coverage: ## Generate coverage report
	@echo "$(GREEN)Generating coverage report...$(NC)"
	pytest tests/ --cov=services --cov-report=html
	@echo "$(GREEN)Coverage report: htmlcov/index.html$(NC)"

load-test: ## Run load test (normal load)
	@echo "$(GREEN)Running load test...$(NC)"
	k6 run tests/load_test.js --out json=load_test_results.json

load-test-smoke: ## Run smoke test (1 user, 1 min)
	@echo "$(GREEN)Running smoke test...$(NC)"
	k6 run tests/load_test.js --vus 1 --duration 1m

load-test-stress: ## Run stress test (200 users, 10 min)
	@echo "$(RED)Running stress test...$(NC)"
	k6 run tests/load_test.js --vus 200 --duration 10m

load-test-spike: ## Run spike test
	@echo "$(RED)Running spike test...$(NC)"
	k6 run tests/load_test.js --stage "0s:0,10s:100,20s:0"

# ============================================
# DATABASE
# ============================================

db-migrate: ## Run database migrations
	@echo "$(GREEN)Running migrations...$(NC)"
	docker-compose exec postgres psql -U ai_user -d ai_db -f /docker-entrypoint-initdb.d/init.sql

db-backup: ## Backup database
	@echo "$(GREEN)Backing up database...$(NC)"
	docker-compose exec postgres pg_dump -U ai_user ai_db > backup_$(shell date +%Y%m%d_%H%M%S).sql
	@echo "$(GREEN)Backup created: backup_$(shell date +%Y%m%d_%H%M%S).sql$(NC)"

db-restore: ## Restore database (use: make db-restore FILE=backup.sql)
	@echo "$(YELLOW)Restoring database from $(FILE)...$(NC)"
	docker-compose exec -T postgres psql -U ai_user ai_db < $(FILE)

db-shell: ## Open Postgres shell
	docker-compose exec postgres psql -U ai_user -d ai_db

db-clean: ## Clean old chat history (>30 days)
	@echo "$(YELLOW)Cleaning old chat history...$(NC)"
	docker-compose exec postgres psql -U ai_user -d ai_db -c "SELECT clean_old_chat_history();"

# ============================================
# CACHE
# ============================================

cache-flush: ## Flush Redis cache
	@echo "$(YELLOW)Flushing Redis cache...$(NC)"
	docker-compose exec redis redis-cli FLUSHALL
	@echo "$(GREEN)Cache flushed!$(NC)"

cache-stats: ## Show Redis stats
	docker-compose exec redis redis-cli INFO stats

cache-shell: ## Open Redis shell
	docker-compose exec redis redis-cli

# ============================================
# CLEANUP
# ============================================

clean: ## Stop and remove all containers, networks, volumes
	@echo "$(RED)Cleaning up everything...$(NC)"
	docker-compose down -v
	@echo "$(GREEN)Cleanup complete!$(NC)"

clean-images: ## Remove all project images
	@echo "$(RED)Removing Docker images...$(NC)"
	docker-compose down --rmi all

clean-volumes: ## Remove all volumes (WARNING: data loss!)
	@echo "$(RED)⚠️  WARNING: This will delete all data!$(NC)"
	@read -p "Are you sure? (yes/no): " confirm && [ "$$confirm" = "yes" ]
	docker-compose down -v

clean-logs: ## Remove old log files
	@echo "$(YELLOW)Cleaning log files...$(NC)"
	find . -name "*.log" -type f -delete

# ============================================
# SECRETS MANAGEMENT
# ============================================

secrets-check: ## Check if all secrets are configured
	@echo "$(GREEN)Checking secrets...$(NC)"
	@test -f secrets/yandex_api_key.txt || echo "$(RED)✗ yandex_api_key.txt missing$(NC)"
	@test -f secrets/yandex_folder_id.txt || echo "$(RED)✗ yandex_folder_id.txt missing$(NC)"
	@test -f secrets/supabase_url.txt || echo "$(RED)✗ supabase_url.txt missing$(NC)"
	@test -f secrets/supabase_key.txt || echo "$(RED)✗ supabase_key.txt missing$(NC)"
	@test -f secrets/postgres_password.txt || echo "$(RED)✗ postgres_password.txt missing$(NC)"
	@test -f secrets/yandex_api_key.txt && \
	     test -f secrets/yandex_folder_id.txt && \
	     test -f secrets/supabase_url.txt && \
	     test -f secrets/supabase_key.txt && \
	     test -f secrets/postgres_password.txt && \
	     echo "$(GREEN)✓ All secrets configured$(NC)"

secrets-template: ## Create secrets template files
	@echo "$(GREEN)Creating secrets template...$(NC)"
	@mkdir -p secrets
	@echo "YOUR_YANDEX_API_KEY_HERE" > secrets/yandex_api_key.txt.example
	@echo "YOUR_FOLDER_ID_HERE" > secrets/yandex_folder_id.txt.example
	@echo "https://xxxxx.supabase.co" > secrets/supabase_url.txt.example
	@echo "YOUR_SUPABASE_KEY_HERE" > secrets/supabase_key.txt.example
	@echo "YOUR_POSTGRES_PASSWORD_HERE" > secrets/postgres_password.txt.example
	@echo "$(GREEN)Template created in secrets/ directory$(NC)"
	@echo "$(YELLOW)Copy .example files and fill with real values$(NC)"

# ============================================
# DEVELOPMENT
# ============================================

dev: ## Start in development mode (with hot reload)
	@echo "$(GREEN)Starting in development mode...$(NC)"
	docker-compose -f docker-compose.yml -f docker-compose.dev.yml up

shell-validation: ## Open shell in validation service
	docker-compose exec validation-service /bin/sh

shell-llm: ## Open shell in LLM service
	docker-compose exec llm-service /bin/sh

shell-memory: ## Open shell in memory service
	docker-compose exec memory-service /bin/sh

lint: ## Run linting on all services
	@echo "$(GREEN)Running linters...$(NC)"
	flake8 services/ --max-line-length=127
	black --check services/
	isort --check-only services/

format: ## Format code
	@echo "$(GREEN)Formatting code...$(NC)"
	black services/
	isort services/

# ============================================
# CI/CD
# ============================================

ci: ## Run full CI pipeline locally
	@echo "$(GREEN)Running CI pipeline...$(NC)"
	@make lint
	@make test
	@make build
	@make load-test-smoke

deploy-prod: ## Deploy to production (use with caution!)
	@echo "$(RED)⚠️  Deploying to PRODUCTION$(NC)"
	@read -p "Are you sure? (yes/no): " confirm && [ "$$confirm" = "yes" ]
	@make build
	@make test
	docker-compose -f docker-compose.prod.yml up -d
	@make health

# ============================================
# MONITORING & DEBUGGING
# ============================================

top: ## Show top processes in containers
	docker-compose top

inspect: ## Inspect specific service (use: make inspect SERVICE=llm)
	docker-compose exec $(SERVICE)-service /bin/sh -c "ps aux && df -h && free -m"

network: ## Show network configuration
	docker network ls
	docker network inspect ai-agent-microservices_ai-network

tail-prometheus: ## Tail Prometheus logs
	docker-compose logs -f prometheus

tail-grafana: ## Tail Grafana logs
	docker-compose logs -f grafana

alerts: ## Show active Prometheus alerts
	@curl -s http://localhost:9090/api/v1/alerts | jq '.data.alerts[] | select(.state=="firing")'

# ============================================
# UTILITIES
# ============================================

version: ## Show versions of all services
	@echo "$(GREEN)Service versions:$(NC)"
	@docker-compose exec validation-service python -c "import sys; print(f'Python: {sys.version}')" 2>/dev/null || true
	@docker-compose exec redis redis-cli INFO server | grep redis_version 2>/dev/null || true
	@docker-compose exec postgres psql --version 2>/dev/null || true

ports: ## Show all exposed ports
	@echo "$(GREEN)Exposed ports:$(NC)"
	@echo "  Validation: 8001"
	@echo "  RAG: 8003"
	@echo "  Intent: 8004"
	@echo "  LLM: 8005"
	@echo "  Memory: 8006"
	@echo "  Logging: 8007"
	@echo "  Redis: 6379"
	@echo "  Postgres: 5432"
	@echo "  Prometheus: 9090"
	@echo "  Grafana: 3000"
	@echo "  Alertmanager: 9093"

docs: ## Generate API documentation
	@echo "$(GREEN)Generating API docs...$(NC)"
	@echo "Visit http://localhost:8001/docs for Validation Service"
	@echo "Visit http://localhost:8003/docs for RAG Service"
	@echo "Visit http://localhost:8005/docs for LLM Service"

# Default target
.DEFAULT_GOAL := help

