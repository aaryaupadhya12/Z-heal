# ZoneHeal / ZoneRL — AWS Infrastructure

This document outlines the AWS infrastructure, architecture, and Console setup instructions for **ZoneRL / ZoneHeal**, a self-healing reinforcement-learning router distributed across AWS Availability Zones.

> **Note**: This README focuses on the **AWS Infrastructure** and Console-based setup. For application code, local execution, and Docker/ECR deployment instructions, please refer to the complementary `README.md` (or `README_ZoneHeal_Code.md`) document in the repository.

---

## 1. Architecture Overview

* Multiple backend services (`backend-a`, `backend-b`, `backend-c`) are distributed across AWS Availability Zones (AZs).
* A `router` receives requests and selects a backend according to an RL policy.
* The router observes latency, cross-AZ traffic, and errors, emitting CloudWatch Embedded Metric Format (EMF) logs.
* CloudWatch provides observability and monitors a p99 latency guardrail.
* If the p99 latency guardrail enters `ALARM`, the router's **kill switch** triggers, switching from policy mode to deterministic rule mode.
* EventBridge and Lambda handle the AWS-side alarm event acknowledgement.
* Locust generates workload against the router.
* All deployments are hosted on ECS Fargate.

## 2. Configuration Policy

### AWS Console Strict Preference
This project enforces a **strict preference for AWS Console-only configuration and verification**.
The AWS Console **must** be used for setting up and managing:
* VPC, Subnets, Route Tables, Internet Gateway
* Security Groups, IAM, S3
* ECS, ECS Task Definitions, ECS Services, Service Connect
* CloudWatch (Dashboards, Alarms), EventBridge, and Lambda

### CLI Exceptions
The AWS CLI is allowed **only** for:
1. `curl` testing of deployed HTTP endpoints.
2. ECR Docker image authentication and push.
3. Specific alarm state testing (e.g., forcing an alarm state).

## 3. Account & Region
* **Region**: `ap-southeast-2` (Sydney)
* **Account ID**: `819168518877`

## 4. Networking (VPC)

* **VPC**: `zonerl-vpc` (`10.0.0.0/16`)
* **Internet Gateway**: `zonerl-igw`
* **Route Table**: `zonerl-public-rt` (routes `0.0.0.0/0` to `zonerl-igw`)

### Public Subnets
All subnets have **public IPv4 auto-assignment enabled** and route through the IGW:
* `zonerl-public-a`: `10.0.1.0/24` (AZ: `ap-southeast-2a`)
* `zonerl-public-b`: `10.0.2.0/24` (AZ: `ap-southeast-2b`)
* `zonerl-public-c`: `10.0.3.0/24` (AZ: `ap-southeast-2c`)

## 5. Security Groups

### Router (`zonerl-router-sg`)
* **Inbound**: TCP `8000` from VPC CIDR `10.0.0.0/16` (to allow Locust traffic) and tester IPs.
* **Outbound**: All

### Backend (`zonerl-backend-sg`)
* **Inbound**: TCP `8080` from `zonerl-router-sg`.
* **Outbound**: All

### Locust (`zonerl-locust-sg`)
* **Inbound**: None required.
* **Outbound**: All

## 6. S3 Storage
* **Bucket**: `zonerl-819168518877`
* **Configuration**: Versioning ON, Block Public Access ON.
* **Objects**: `policy/policy.json`, `results/results.json`

## 7. IAM Roles
* **Execution Role**: `zonerl-ecs-execution-role` with `AmazonECSTaskExecutionRolePolicy`.
* **Router Task Role**: `zonerl-router-task-role`. Deliberately narrow permissions:
  * S3 `GetObject` only for the required policy objects.
  * `cloudwatch:DescribeAlarms` with Resource `*`.
* **Locust Task Role**: None required.

## 8. Elastic Container Service (ECS)

* **Cluster**: `zonerl`
* **Launch Type**: Fargate
* **Service Connect Namespace**: `zonerl.local`

### Backends
* **Task Definition**: `zonerl-backend` (0.25 vCPU, 0.5 GB RAM, x86_64)
* **Port**: `8080` (Service Connect name: `backend-8080-tcp`)
* **Services**: `backend-a`, `backend-b`, `backend-c` (Pinned one per AZ)
* **Service Connect Endpoints**: `backend-a:8080`, `backend-b:8080`, `backend-c:8080`

### Router
* **Task Definition**: `zonerl-router` (Port `8000`)
* **Service Connect**: Enabled, advertises endpoint `router:8000`
* **Functionality**: Distributes across all AZs, discovers its own AZ automatically, maps traffic, and polls the CloudWatch alarm every 10 seconds.

### Locust
* **Task Definition**: Approx. 0.25 vCPU / 0.5 GB RAM.
* **Environment Variable**: `TARGET_HOST=http://router:8000`
* **Service Connect**: Client-only enabled.
* **Log Group**: `/ecs/zonerl-locust`

## 9. CloudWatch & Dashboards

### EMF Metrics (Namespace: `ZoneRL`)
* **Metrics**: `LatencyMs`, `CrossAZBytes`, `Requests`, `Errors`
* **Dimensions**: `[Mode, SourceAZ]` and `[Mode]`

### Dashboard (`zonerl`)
Contains widgets for:
* **p99 Latency**: `LatencyMs` p99 grouped by `Mode`.
* **Requests**: Sum of `Requests`.
* **Cross-AZ bytes**: Sum of `CrossAZBytes`.
* **Cross-AZ cost**: Metric math (`m1 * 60 / 1000000000 * 0.01 * 95.9355`) yielding ₹/hour.

## 10. p99 Guardrail & Kill Switch

### CloudWatch Alarm (`zonerl-p99-guardrail`)
* **Metric**: `ZoneRL / LatencyMs`, `Mode=policy`, Statistic: `p99`
* **Evaluation**: 1 minute period, threshold `> 200 ms`, 3 out of 3 datapoints.
* **Missing data**: Treat as not breaching.

### EventBridge + Lambda
* **Rule**: `zonerl-p99-kill-switch` matches alarm state change to `ALARM`.
* **Lambda**: `zonerl-p99-kill-switch` (Python 3.x, no VPC) logs the event.
* **Note**: EventBridge provides the AWS event path, but the **router mode change is performed by the router actively polling CloudWatch**.

### Testing the Kill Switch (CLI allowed)
Force the alarm state:
```bash
aws cloudwatch set-alarm-state \
 --alarm-name "zonerl-p99-guardrail" \
 --state-value ALARM \
 --state-reason "ZoneRL kill-switch integration test" \
 --region ap-southeast-2
```
Verify the router switches to `rule` mode via `GET /mode`. To restore:
```bash
aws cloudwatch set-alarm-state \
 --alarm-name "zonerl-p99-guardrail" \
 --state-value OK \
 --state-reason "ZoneRL kill-switch integration test complete" \
 --region ap-southeast-2
```

## 11. Critical Troubleshooting

* **ECS Task Revisions**: Locust may report 100% failures if the ECS Router service is using an old task definition revision. **Creating a new task definition revision does not automatically update the service.** Always update the ECS service to use the correct revision and wait for it to become healthy.
* **Locust Logs**: Ensure the ECS Log Group for Locust is set to `/ecs/zonerl-locust` to avoid `ResourceInitializationError`.
