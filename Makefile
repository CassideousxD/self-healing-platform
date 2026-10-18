.PHONY: cluster-up cluster-down deploy-dev deploy-prod metrics-server test lint

cluster-up:
	docker compose down || true
	kind create cluster --config infra/kind-config.yaml --name self-healing
	kubectl apply -k infra/metrics-server

cluster-down:
	kind delete cluster --name self-healing

deploy-dev:
	kubectl apply -k deploy/overlays/dev

deploy-prod:
	kubectl apply -k deploy/overlays/prod

metrics-server:
	kubectl apply -k infra/metrics-server

lint:
	ruff check .

test:
	pytest
