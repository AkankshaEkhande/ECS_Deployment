# Flask on AWS ECS Fargate (ECR + ALB) - Step by Step

## Architecture

```
Internet
   |
[ Application Load Balancer ]  (public subnets, SG: allow 80 from anywhere)
   |  forwards to Target Group (type: IP, port 5000, health check /health)
   |
[ ECS Service (Fargate) ]      (desired count = 2, spread across 2 AZs)
   |-- Task 1: container "flask-app" (AZ-a)
   |-- Task 2: container "flask-app" (AZ-b)
   |       SG: allow 5000 ONLY from the ALB security group
   |
   |-- pulls image from --> [ ECR repository: flask-ecs-demo ]
   |-- ships logs to   --> [ CloudWatch Logs: /ecs/flask-ecs-demo ]
```

Learning setup: tasks run in public subnets with public IP (needed to pull from ECR
without NAT Gateway). Production setup: tasks in private subnets + NAT Gateway
or VPC endpoints (ecr.api, ecr.dkr, s3 gateway, logs).

## 0. Prerequisites
- AWS CLI v2 configured (`aws configure`) with an admin-ish user/role
- Docker Desktop / Docker Engine running

```bash
export REGION=ap-south-1          # change if needed
export ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export REPO=flask-ecs-demo
```

## 1. Run locally first
```bash
docker build -t $REPO:v1 .
docker run --rm -p 5000:5000 $REPO:v1
# new terminal
curl localhost:5000/
curl localhost:5000/health
curl -X POST localhost:5000/notes -H "Content-Type: application/json" -d '{"text":"hello"}'
curl localhost:5000/notes
```

## 2. ECR - create repo, build, push
```bash
# create repository
aws ecr create-repository --repository-name $REPO --region $REGION \
  --image-scanning-configuration scanOnPush=true

# authenticate Docker to ECR (token valid 12 hours)
aws ecr get-login-password --region $REGION | \
  docker login --username AWS --password-stdin $ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com

# build (use --platform if you are on Apple Silicon / ARM; Fargate task below is X86_64)
docker build --platform linux/amd64 -t $REPO:v1 .

# tag + push
docker tag $REPO:v1 $ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/$REPO:v1
docker push $ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/$REPO:v1
```
Console check: ECR > Repositories > flask-ecs-demo > image tag v1 present.

## 3. Networking - security groups (use default VPC for learning)
Console: EC2 > Security Groups > Create

1. `alb-sg`  : inbound HTTP 80 from 0.0.0.0/0
2. `task-sg` : inbound Custom TCP 5000, source = `alb-sg` (security group, not CIDR)

## 4. Load balancer + target group
Console: EC2 > Target Groups > Create
- Target type: **IP addresses** (mandatory for Fargate/awsvpc)
- Name: `flask-tg`, Protocol HTTP, Port **5000**, VPC: default
- Health check path: `/health`
- Do NOT register targets manually (ECS does it). Create.

Console: EC2 > Load Balancers > Create > Application Load Balancer
- Name `flask-alb`, Internet-facing, IPv4
- Mappings: select at least **2 AZs** (public subnets)
- Security group: `alb-sg`
- Listener HTTP:80 -> forward to `flask-tg`

## 5. IAM - task execution role
Role `ecsTaskExecutionRole` is created automatically when you use the ECS console.
If missing: IAM > Roles > Create > trusted entity "Elastic Container Service Task"
> attach policy `AmazonECSTaskExecutionRolePolicy`.
(This role lets ECS pull from ECR and write to CloudWatch Logs. It is NOT the
role your app code uses - that is the "task role", optional, add it later when
the app needs S3/DynamoDB access.)

## 6. ECS cluster
Console: ECS > Clusters > Create cluster
- Name: `flask-cluster`
- Infrastructure: **AWS Fargate (serverless)**

CLI alternative: `aws ecs create-cluster --cluster-name flask-cluster --region $REGION`

## 7. Task definition
Console: ECS > Task definitions > Create new
- Family: `flask-ecs-demo-task`, Launch type Fargate, OS Linux/X86_64
- CPU 0.25 vCPU, Memory 0.5 GB
- Task execution role: `ecsTaskExecutionRole`
- Container: name `flask-app`, image URI
  `<ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com/flask-ecs-demo:v1`
- Port mapping: container port 5000, TCP
- Env var: APP_VERSION = v1
- Logging: awslogs (auto-create)

CLI alternative (edit placeholders in `task-definition.json` first):
```bash
sed -i "s/<ACCOUNT_ID>/$ACCOUNT_ID/g; s/<REGION>/$REGION/g" task-definition.json
aws ecs register-task-definition --cli-input-json file://task-definition.json --region $REGION
```

## 8. ECS service (this connects everything)
Console: ECS > Clusters > flask-cluster > Services > Create
- Compute: Launch type Fargate
- Task definition: `flask-ecs-demo-task` (latest)
- Service name: `flask-service`, Desired tasks: **2**
- Networking: default VPC, select 2 subnets, security group `task-sg`,
  **Public IP: ENABLED** (needed to pull from ECR in public subnets)
- Load balancing: Application Load Balancer > use existing `flask-alb`,
  listener 80, target group `flask-tg`, container `flask-app:5000`
- Create

## 9. Test
- EC2 > Load Balancers > `flask-alb` > copy DNS name
```bash
curl http://<ALB-DNS-NAME>/
curl http://<ALB-DNS-NAME>/health
```
Call `/` several times: `container_id` changes between the 2 tasks = load balancing works.

Logs: CloudWatch > Log groups > `/ecs/flask-ecs-demo`

## 10. Deploy a new version (rolling update)
1. Change `app.py` (e.g. message text), then:
```bash
docker build --platform linux/amd64 -t $REPO:v2 .
docker tag $REPO:v2 $ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/$REPO:v2
docker push $ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/$REPO:v2
```
2. ECS > Task definitions > flask-ecs-demo-task > Create new revision > change image tag to `v2`
3. ECS > Service > Update > choose the new revision > Update.
   ECS starts new tasks, waits for healthy, drains old ones. Zero downtime.

## 11. Troubleshooting cheat-sheet
| Symptom | Likely cause |
|---|---|
| Task stops: `CannotPullContainerError` | No public IP / no NAT / endpoints; or wrong image URI/tag; or exec role missing |
| Task stops: `exec format error` | Image built on ARM, task is X86_64 -> use `--platform linux/amd64` |
| Targets `unhealthy` in target group | task-sg not allowing 5000 from alb-sg; wrong health check path/port |
| 503 from ALB | No healthy targets yet / service still starting |
| No logs | Exec role lacks CloudWatch Logs permission |

Check failures: ECS > cluster > service > Tasks tab > stopped task > "Stopped reason".

## 12. Cleanup (avoid charges!)
ALB and Fargate tasks cost money while running.
1. ECS service: set desired tasks to 0, then delete service
2. Delete cluster
3. Delete ALB, then target group
4. Delete ECR images/repo: `aws ecr delete-repository --repository-name $REPO --force --region $REGION`
5. Delete log group `/ecs/flask-ecs-demo`, security groups

## 13. Next steps to practice
- Put tasks in private subnets + NAT / VPC endpoints
- Add HTTPS with ACM certificate on ALB listener 443
- Service auto scaling (target tracking on CPU 50%)
- Task role + DynamoDB/S3 access from the app
- CI/CD: GitHub Actions or CodePipeline to build > push ECR > update service
- Define everything in Terraform/CloudFormation
