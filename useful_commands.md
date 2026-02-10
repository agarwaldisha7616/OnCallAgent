# Useful Commands for Project Setup and Testing

## Process Manager

```
# Start process manager
python processmanager.py

# Check instances of Utility
curl -s localhost:7070/instances

# Scale process
curl -X POST localhost:7070/scale -H 'content-type: application/json' -d '{"replicas":2}'
```

## Inventory Service

```
# Update item ID 1 with product details (on two different ports)
curl -X PUT "http://localhost:8001/items/1" -H "Content-Type: application/json" -d '{"name": "Laptop", "price": 999.99, "manufacturer": "TechCorp"}'
curl -X PUT "http://localhost:8002/items/1" -H "Content-Type: application/json" -d '{"name": "Laptop", "price": 999.99, "manufacturer": "TechCorp"}'
```

## Load Balancer

```
# Start load balancer
uvicorn loadbalancer:app --port 9000

# Test load balancer
curl -X PUT "http://localhost:9000/items/1" -H "Content-Type: application/json" -d '{"name": "Laptop", "price": 999.99, "manufacturer": "TechCorp"}'

# Note: 404 errors are expected for random IDs; focus on 5xx errors.
```

## Traffic Generation

```
# Constant traffic
python traffic_gen.py --url http://localhost:9000 --profile constant --rps 10 --duration 60

# Agentic (malicious) traffic
python traffic_gen.py --url http://localhost:9000 --profile agentic --normal-duration 30 --error-duration 180 --normal-rps 5 --error-rps 20 --duration 210
```

## Prometheus & AlertManager

```
# Start Prometheus
# 1. Download Prometheus: https://prometheus.io/download/
# 2. Add to System Path
prometheus --config.file=./prometheus.yaml --storage.tsdb.path=./prom-data

# Start AlertManager
# 1. Download AlertManager: https://prometheus.io/download/#alertmanager
# 2. Add to System Path
./alertmanager --config.file=../alertmanager.yaml

# Prometheus UI: http://localhost:9090
# Alerts: http://localhost:9090/alerts

# Example Prometheus queries:
sum by (instance, method) (
    increase(http_request_duration_seconds_count{route="/items/{item_id}"}[1m])
)
sum by (instance) (
    increase(http_request_duration_seconds_count{route="/metrics"}[1m])
)

# Check error rate:
(sum(rate(http_errors_total{job="inventory"} [1m])) / sum(rate(http_requests_total{job="inventory"}[1m])))
```

## On Call Agent

```
# Start On Call Agent
uvicorn on_call_agent:app  --port 8088 --log-level warning
```

---

## Notes
- Prometheus is a time-series database with its own query language (PromQL).
- The `/metrics` endpoint is scraped by Prometheus.
- AlertManager is a service of Prometheus for alerting.
- 404 errors from the load balancer are expected for random IDs; focus on 5xx errors for incident detection.
