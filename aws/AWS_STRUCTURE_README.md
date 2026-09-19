# ZoneHeal / ZoneRL — Application Code

This repository contains the application code for **ZoneRL / ZoneHeal**, a self-healing reinforcement-learning router distributed across AWS Availability Zones.

> **Note**: This README focuses exclusively on the **application code**, local execution, Docker configuration, and the ECR deployment workflow. For AWS infrastructure configuration (VPC, ECS, CloudWatch, EventBridge, IAM), please refer to the complementary `README_ZoneHeal_AWS.md` document.

## Repository Structure

```text
.
├── Dockerfile                  # Router Dockerfile
├── backend/
│   ├── app.py                  # Backend application code (FastAPI)
│   ├── Dockerfile              # Backend Dockerfile
│   └── requirements.txt        # Backend dependencies
├── locust/
│   ├── locustfile.py           # Locust load testing script
│   └── Dockerfile              # Locust Dockerfile
├── router/
│   ├── app.py                  # Router application code (FastAPI)
│   └── requirements.txt        # Router dependencies
└── policy/                     # (Optional) Local policy for testing
```

## Router

The Router (`router/app.py`) handles incoming requests, makes routing decisions based on an RL policy, and emits CloudWatch Embedded Metric Format (EMF) logs.

### API Endpoints
* `GET /health` - Health check. Returns `{"ok": true, "az": "..."}`.
* `GET /work?size=1000` - Simulated work endpoint. Routes requests to a backend and records latency, cross-AZ traffic, and errors via EMF.
* `GET /mode` - Get current routing mode (`policy`, `rule`, `shadow`).
* `POST /mode?m=<mode>` - Set routing mode manually.
* `GET /policy` - Returns current policy version and state count.
* `POST /reload` - Reloads policy from S3 (or local file if running locally).
* `GET /results` - Returns policy execution results.

### Core Logic & ECS Integration
* **AZ Discovery**: In ECS, the router automatically determines its Availability Zone by querying the `ECS_CONTAINER_METADATA_URI_V4/task` endpoint. For local testing, set the `FAKE_AZ` environment variable.
* **Policy Loading**: In ECS, the policy is loaded from S3 (configured via `BUCKET` and `POLICY_KEY`). For local testing, it is loaded from the filesystem (`POLICY_PATH`).
* **Kill Switch**: The router runs a background thread polling the CloudWatch alarm (`ALARM_NAME`). If the alarm enters the `ALARM` state, the mode automatically switches to `rule` to restore deterministic routing.

## Backend

The Backend (`backend/app.py`) is deployed across multiple AZs and processes the simulated workloads.

### API Endpoints
* `GET /health` - Health check.
* `GET /work?size=1000` - Processes work. Simulates delay based on `BASE_MS` and `EXTRA_MS`. Returns an octet-stream payload.
* `POST /slow?on=<true/false>` - Fault injection endpoint. Toggles `EXTRA_MS` delay to simulate application degradation.
* `GET /stats` - Returns in-flight requests and total capacity.

## Locust

The Locust container (`locust/locustfile.py`) generates load against the Router.
It relies on the `TARGET_HOST` environment variable to locate the router (e.g., `http://router:8000` via ECS Service Connect).

## Communication Flow

1. **Locust -> Router**: Locust sends `GET /work` to `TARGET_HOST` (Router).
2. **Router -> Backend**: Router evaluates the policy (or rule), selects a target AZ, maps it to a Service Connect endpoint (e.g., `backend-a:8080`), and forwards the request.
3. **Router -> CloudWatch**: Router logs request metrics (LatencyMs, CrossAZBytes, Requests, Errors) using CloudWatch EMF.

## Local Execution (Docker)

You can run these components locally to test logic without ECS.

### Environment Variables Reference

**Router:**
* `AWS_REGION` (default: `ap-southeast-2`)
* `FAKE_AZ` (set to `ap-southeast-2a` etc. for local testing to bypass ECS metadata lookup)
* `AZ_TO_BACKEND` (JSON mapping AZs to endpoints)
* `START_MODE` (default: `shadow`)
* `POLICY_PATH` (local path to policy file, default: `policy/policy.json`)
* `ALARM_NAME` (default: `zonerl-p99-guardrail`)

**Backend:**
* `FAKE_AZ` (set for local testing to bypass ECS metadata lookup)
* `BASE_MS` (default: 20)
* `EXTRA_MS` (default: 0)
* `CAPACITY` (default: 50)

### Local Build & Run

1. **Router**:
   ```bash
   docker build -t zonerl-router -f Dockerfile .
   docker run -p 8000:8000 -e FAKE_AZ=ap-southeast-2a -e POLICY_PATH=/app/policy/policy.json zonerl-router
   ```

2. **Backend**:
   ```bash
   docker build -t zonerl-backend -f backend/Dockerfile .
   docker run -p 8080:8080 -e FAKE_AZ=ap-southeast-2a zonerl-backend
   ```

3. **Locust**:
   ```bash
   docker build -t zonerl-locust -f locust/Dockerfile .
   docker run -e TARGET_HOST=http://host.docker.internal:8000 zonerl-locust
   ```

## ECR Image / Tag / Push Workflow

To deploy these applications to AWS ECS, you must push the images to Elastic Container Registry (ECR). *This is the only workflow where the AWS CLI is explicitly permitted.*

```bash
# 1. Authenticate with ECR
aws ecr get-login-password --region ap-southeast-2 | docker login --username AWS --password-stdin 819168518877.dkr.ecr.ap-southeast-2.amazonaws.com

# 2. Build Images
docker build -t zonerl/router -f Dockerfile .
docker build -t zonerl/backend -f backend/Dockerfile .
docker build -t zonerl/locust -f locust/Dockerfile .

# 3. Tag Images
docker tag zonerl/router:latest 819168518877.dkr.ecr.ap-southeast-2.amazonaws.com/zonerl/router:latest
docker tag zonerl/backend:latest 819168518877.dkr.ecr.ap-southeast-2.amazonaws.com/zonerl/backend:latest
docker tag zonerl/locust:latest 819168518877.dkr.ecr.ap-southeast-2.amazonaws.com/zonerl/locust:latest

# 4. Push Images
docker push 819168518877.dkr.ecr.ap-southeast-2.amazonaws.com/zonerl/router:latest
docker push 819168518877.dkr.ecr.ap-southeast-2.amazonaws.com/zonerl/backend:latest
docker push 819168518877.dkr.ecr.ap-southeast-2.amazonaws.com/zonerl/locust:latest
```

## ECS Deployment & Troubleshooting

### Dockerfile Constraints
* **Locust**: The official Locust base image already defines `ENTRYPOINT ["locust"]`. The provided `locust/Dockerfile` uses `CMD` to pass arguments (`-f`, `--headless`, etc.). **Do not** add `locust` to the ECS Command override, as it will execute `locust locust ...` and crash.
* **Router & Backend**: Both use Uvicorn via `CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "..."]`. Ensure these ports match your ECS container port definitions (8000 for Router, 8080 for Backend).

### Deployment Workflow
When pushing updated code:
1. Push the new image to ECR using the AWS CLI.
2. In the **AWS Console**, create a new revision of the corresponding ECS Task Definition.
3. Update the ECS Service to use the *new* Task Definition revision.
4. Wait for the new task to reach the `RUNNING` state and pass health checks.

> **CRITICAL TROUBLESHOOTING LESSON**: If Locust reports 100% failures or high latency but the Router appears healthy, you are likely routing to an old task. **Never assume creating a new task definition automatically updates the service.** Always verify the ECS Service is actually configured to use the newly created revision before running load tests.
