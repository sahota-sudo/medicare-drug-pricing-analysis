.PHONY: help install test analysis db-up db-down load analysis-db lint clean

help:
	@echo "install     Install Python dependencies"
	@echo "test        Run the test suite (no database required)"
	@echo "analysis    Run the analysis; writes docs/ and docs/charts/"
	@echo "db-up       Start PostgreSQL in Docker"
	@echo "load        Build the schema and load the warehouse"
	@echo "analysis-db Run the analysis against the PostgreSQL views"

install:
	python -m pip install -r requirements.txt

test:
	PYTHONPATH=src python -m pytest

analysis:
	PYTHONPATH=src python -m cms.analysis

db-up:
	docker compose up -d
	@echo "Waiting for PostgreSQL ..."
	@until docker compose exec -T postgres pg_isready -U cms -d cms_drug_spending \
		>/dev/null 2>&1; do sleep 1; done
	@echo "PostgreSQL is ready on localhost:5432"

db-down:
	docker compose down

load:
	PYTHONPATH=src python -m cms.load --reset

analysis-db:
	PYTHONPATH=src python -m cms.analysis --from-db

lint:
	ruff check src tests

clean:
	rm -rf .pytest_cache src/**/__pycache__ data/processed
