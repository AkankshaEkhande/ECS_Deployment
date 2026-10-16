# SpendSight - 3-tier app (Nginx + Flask + PostgreSQL)

```
Browser ──► frontend (Nginx: static dashboard + reverse proxy /api)
                │
                ▼
            backend (Flask + gunicorn, REST API)
                │
                ▼
            database (PostgreSQL)
```

| Tier | Tech | Notes |
|---|---|---|
| Frontend | Nginx + HTML/JS + Chart.js | Doughnut + bar charts, stat cards, add/delete form. Calls `/api/...` (same origin) |
| Backend | Flask, gunicorn, psycopg2 | Creates the table and seeds demo data on startup |
| Database | PostgreSQL 16 | Compose: container with volume. AWS: Amazon RDS |

The browser only ever talks to Nginx. Nginx forwards `/api/*` to the backend, so
there is no CORS to configure and the backend and DB are never exposed publicly.

---------------------------------------------------------------------------

## 1. Run locally with Docker Compose

```bash
cp .env.example .env          # edit DB_PASSWORD
docker compose up -d --build
docker compose ps             # all 3 should be running / healthy
```
Open http://localhost:8080

Useful commands:
```bash
docker compose logs -f backend      # watch backend logs
docker compose exec db psql -U appuser -d expenses -c "select count(*) from expenses;"
docker compose down                 # stop (data kept in volume)
docker compose down -v              # stop AND delete DB data
```
Things to observe: the dashboard says "Database connected"; adding an expense updates
the charts; `docker compose restart` keeps your data (named volume `pgdata`).

---------------------------------------------------------------------------

## 2. AWS Option A - EC2 + Docker Compose (quick, same compose file)

Good for learning and demos. Not highly available (one server, DB on its disk).

1. EC2 > Launch instance: Amazon Linux 2023, t3.small, key pair.
   Security group: SSH 22 from **My IP**, HTTP 80 from anywhere.
2. Connect and install Docker + Compose:
```bash
sudo dnf install -y docker
sudo systemctl enable --now docker
sudo usermod -aG docker ec2-user && newgrp docker
sudo mkdir -p /usr/local/lib/docker/cli-plugins
sudo curl -SL https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64 \
  -o /usr/local/lib/docker/cli-plugins/docker-compose
sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
docker compose version
```
3. Copy the project (from your laptop) and start it:
```bash
scp -i key.pem -r expense-tracker ec2-user@<EC2_PUBLIC_IP>:~
ssh -i key.pem ec2-user@<EC2_PUBLIC_IP>
cd expense-tracker && cp .env.example .env     # set a strong DB_PASSWORD
echo "APP_PORT=80" >> .env
docker compose up -d --build
```
4. Open http://<EC2_PUBLIC_IP>

---------------------------------------------------------------------------

## 3. AWS Option B - ECR + ECS Fargate + ALB + RDS (production style)

Docker's old "compose to ECS" integration has been retired, so we translate the
compose file by hand: each compose service becomes a container in an ECS task,
and the `db` service is replaced by managed RDS.

```
Internet ─► ALB (public subnets)
              │ :80
              ▼
   ECS service (private subnets, no public IP)
   ┌──────────── one Fargate task ────────────┐
   │ frontend (nginx :80) ─127.0.0.1:5000─►   │
   │                       backend (:5000)    │
   └──────────────────────┬───────────────────┘
                          │ :5432
                          ▼
               RDS PostgreSQL (private subnets)
```
Why frontend + backend in ONE task? Containers in a task share localhost, so Nginx
reaches the backend at 127.0.0.1:5000 (that is why `BACKEND_HOST` is an env var).
Later you can split them into two services using Cloud Map service discovery or an
internal ALB so each scales independently.

You can reuse the VPC from the private-subnet exercise (2 public + 2 private subnets,
NAT Gateway or VPC endpoints). Variables used below:
```bash
export REGION=us-east-1
export ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
```

### Step 1 - Security groups (same VPC)
| SG | Inbound |
|---|---|
| `alb-sg` | TCP 80 from 0.0.0.0/0 |
| `app-sg` | TCP 80 from `alb-sg` |
| `rds-sg` | TCP 5432 from `app-sg` |

### Step 2 - RDS PostgreSQL
RDS > Create database:
- Engine PostgreSQL 16, template **Free tier / Dev-Test**, instance `db.t4g.micro`
- Master username `appuser`
- Credentials: **Managed in AWS Secrets Manager** (RDS creates the secret for you)
- Initial database name (Additional configuration): `expenses`
- Connectivity: your VPC, DB subnet group = **private subnets**, Public access **No**, security group `rds-sg`
- Create, wait until Available, then copy the **Endpoint** and the **secret ARN**
  (Secrets Manager > the secret `rds!db-...`).

The backend creates the table and seeds data itself, so you do not run any SQL.

### Step 3 - ECR repositories, build and push both images
```bash
aws ecr create-repository --repository-name expense-backend  --region $REGION
aws ecr create-repository --repository-name expense-frontend --region $REGION

aws ecr get-login-password --region $REGION | \
  docker login --username AWS --password-stdin $ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com

docker build --platform linux/amd64 -t expense-backend:v1  ./backend
docker build --platform linux/amd64 -t expense-frontend:v1 ./frontend

for s in backend frontend; do
  docker tag expense-$s:v1 $ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/expense-$s:v1
  docker push $ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/expense-$s:v1
done
```

### Step 4 - Let the task read the DB secret (IAM)
IAM > Roles > `ecsTaskExecutionRole` > Add permissions > Create inline policy:
```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": "secretsmanager:GetSecretValue",
    "Resource": "<SECRET_ARN>"
  }]
}
```

### Step 5 - Task definition
Edit `ecs-task-definition.json` and replace `<ACCOUNT_ID>`, `<REGION>`,
`<RDS_ENDPOINT>`, `<SECRET_ARN>`, then:
```bash
aws ecs register-task-definition --cli-input-json file://ecs-task-definition.json --region $REGION
```
Points to notice in the file:
- two containers, one task (0.5 vCPU / 1 GB)
- backend gets `DB_HOST`, `DB_NAME`, `DB_USER` as plain env vars
- `DB_PASSWORD` comes from Secrets Manager via `"valueFrom": "<SECRET_ARN>:password::"`
  (this syntax picks the `password` key out of the JSON secret)
- frontend has `BACKEND_HOST=127.0.0.1` and starts after the backend

### Step 6 - Target group and ALB
- Target group: type **IP**, HTTP **80**, health check path **/healthz**, no manual targets
- ALB `expense-alb`: internet-facing, 2 **public** subnets, SG `alb-sg`,
  listener HTTP:80 -> that target group

### Step 7 - ECS cluster and service
- ECS > Create cluster (Fargate) `expense-cluster` (or reuse the old one)
- Create service: task definition `expense-tracker-task`, desired tasks **2**
- Networking: **private subnets**, SG `app-sg`, Public IP **OFF**
- Load balancing: ALB `expense-alb`, container **frontend : 80**, the target group above

### Step 8 - Test
Open the ALB DNS name. You should see the dashboard with seeded data and the status pill
"Database connected". The footer shows the backend container ID; with 2 tasks it changes
as the ALB balances your refreshes.

Check logs in CloudWatch: `/ecs/expense-tracker` (streams `backend/...` and `frontend/...`).

### Updating the app
Build and push new tags (v2...), register a new task definition revision with the new
image tags, then update the service. Always use a new tag, never reuse one.

### Troubleshooting
| Symptom | Likely cause |
|---|---|
| Task stops, `ResourceInitializationError ... secretsmanager` | Execution role lacks `GetSecretValue`, or no NAT / `secretsmanager` VPC endpoint |
| `CannotPullContainerError` | No NAT / ECR + S3 endpoints in private subnets |
| Dashboard loads but "Backend unreachable" | Backend crashed: check `/ecs/expense-tracker` backend logs (usually DB host/password/SG) |
| Backend log "DB not ready ... timeout" | `rds-sg` does not allow 5432 from `app-sg`, or wrong `DB_HOST` |
| ALB targets unhealthy | Health check path must be `/healthz`, port 80; `app-sg` must allow 80 from `alb-sg` |
| `exec format error` | Image built on ARM; use `--platform linux/amd64` |

### Cleanup (avoid charges)
ECS service -> 0 tasks -> delete; delete cluster, ALB, target group; delete RDS
(skip final snapshot for practice); delete ECR repos; delete NAT Gateway + release
Elastic IP (or the VPC endpoints) if you created them for this exercise.

---------------------------------------------------------------------------

## 4. Ideas to extend
- HTTPS: ACM certificate + ALB listener 443 (redirect 80 -> 443)
- ECS service auto scaling (target tracking on CPU / ALBRequestCountPerTarget)
- Separate frontend and backend services (Cloud Map or internal ALB)
- RDS Multi-AZ, automated backups
- CI/CD with GitHub Actions: build > push to ECR > update ECS service
- Define everything in Terraform or CloudFormation
