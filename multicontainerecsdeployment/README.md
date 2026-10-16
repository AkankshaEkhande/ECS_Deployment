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

## Authentication (added in v3)

The app now opens on a **sign in / create account** page (`/login.html`). The dashboard
(`/`) redirects there when you are not signed in, and every user sees only their own expenses.

```
Browser ──POST /api/auth/login──► Nginx ──► Flask: checks password hash, sets HttpOnly cookie (JWT)
Browser ──GET /api/summary (cookie sent automatically)──► Flask: verifies JWT, returns THIS user's data
```

| Endpoint | Auth? | Purpose |
|---|---|---|
| `POST /api/auth/register` | no | name, email, password (8-128 chars) |
| `POST /api/auth/login` | no | sets the session cookie |
| `POST /api/auth/logout` | no | clears the cookie |
| `GET /api/auth/me` | yes | who am I (used by the dashboard) |
| `POST /api/demo-data` | yes | load sample expenses (only if you have none) |
| `/api/expenses`, `/api/summary` | yes | now filtered by the logged-in user |
| `/api/health`, `/api/info` | no | health checks / metadata |

Design choices and why:
- **Passwords** are stored only as salted hashes (Werkzeug scrypt), never in plain text.
- **JWT in an HttpOnly, SameSite=Lax cookie**: JavaScript cannot read it (limits XSS damage) and
  other sites cannot make your browser send it on cross-site POSTs (limits CSRF).
- **Stateless**: no server-side session store, so it works with 2+ ECS tasks behind the ALB.
- **JWT_SECRET** must be the same on every backend task and must be long and random
  (`openssl rand -hex 32`). Changing it signs everyone out.
- **COOKIE_SECURE**: keep `false` while the site is plain http (ALB listener on 80). Set `true`
  as soon as you add HTTPS, otherwise browsers will not send the cookie over http.
- Login errors are generic ("Invalid email or password") so the page does not reveal which
  emails have accounts.

Upgrading an existing database: on startup the backend creates the `users` table and adds a
`user_id` column to `expenses`. Old rows (from before authentication) have no owner and are
not shown to anyone. To start clean locally: `docker compose down -v`.

Not included (next steps for production): login rate limiting / lockout (AWS WAF rate-based
rule on the ALB), email verification, password reset, HTTPS (ACM + ALB 443), or replacing the
whole thing with Amazon Cognito.

### ECS changes for the auth version
1. Rebuild both images with NEW tags (frontend now also ships `login.html`):
   `expense-backend:v4` and `expense-frontend:v4`, and push them to ECR.
2. Register a new task definition revision with `ecs-task-definition-optionB.json`:
   fill `<REGION>` and set `<JWT_SECRET>` (the file also pins the image tags to v4).
3. Update the service with the new revision and **Force new deployment** (desired tasks: 1
   while the database is the Postgres container in the task).
Because that database is lost when the task is replaced, accounts are lost too. That is fine
for practice; use RDS or an EC2/EBS database to keep users.

---------------------------------------------------------------------------

## Email verification (added in v4)

New accounts must confirm their email with a 6-digit code before they can use the app.

```
Create account ─► backend saves user (email_verified = false) + a hashed code ─► email sent
Enter the code ─► POST /api/auth/verify ─► email_verified = true ─► signed in (cookie)
Sign in before verifying ─► 403 "email_not_verified" ─► a new code is sent, verify screen opens
```

| Endpoint | Purpose |
|---|---|
| `POST /api/auth/register` | creates the account (unverified), sends the code. No login cookie yet. |
| `POST /api/auth/verify` | `{email, code}` marks the email verified and signs the user in |
| `POST /api/auth/resend` | sends a new code (60-second cool-down). Same reply for unknown emails. |

Security details: the code is random, only its HMAC hash is stored, it expires after 10 minutes,
and 5 wrong guesses invalidate it (a 6-digit code could otherwise be brute-forced). If someone
registers your email address but never verifies it, your own later sign-up simply replaces that
unverified entry. A verified email can never be re-registered. Accounts created before this
version are treated as verified.

### Email backends (`EMAIL_BACKEND`)
| Value | Where it sends | Use it for |
|---|---|---|
| `smtp` | SMTP server (`SMTP_HOST`, `SMTP_PORT`...) | **Local**: Compose runs Mailpit, a fake inbox at http://localhost:8025 |
| `console` | Writes the email, including the code, into the container log | Quick AWS test with no email service (read it in CloudWatch). Learning only. |
| `ses` | Amazon SES | Real emails on AWS |

### Local test
```bash
docker compose up -d --build
```
Create an account at http://localhost:8080, then open http://localhost:8025 (Mailpit), open the
email and type the code. Try a wrong code 5 times, an expired code, and the Resend button.

### AWS: use `console` first (no setup)
Deploy with `EMAIL_BACKEND=console`. After registering, open CloudWatch log group
`/ecs/expense-task` (stream `backend/...`) and find the line `[EMAIL console] to=...` with the
code. This proves the whole flow works before you set up real email.

### AWS: real email with Amazon SES
1. SES console (same region as the service) > Verified identities > Create identity > **Email address**
   (the sender, e.g. no-reply@yourdomain or your own mailbox) > click the link AWS emails you.
2. A new SES account is in the **sandbox**: it can only send to addresses that are also verified
   identities, with low daily limits. For testing, verify your own recipient addresses too.
   To email anyone, request production access (SES > Account dashboard).
3. Create an IAM role for the **task** (not the execution role):
   trusted entity = Elastic Container Service Task, with this inline policy:
```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["ses:SendEmail"],
    "Resource": "*"
  }]
}
```
   (Tighten `Resource` to your identity ARN later.)
4. In the task definition add `"taskRoleArn": "arn:aws:iam::<ACCOUNT_ID>:role/<that-role>"` at the top
   level, and change the backend environment to:
   `EMAIL_BACKEND=ses`, `EMAIL_FROM=<your verified sender address>`, `SES_REGION=<region>`.
5. The task needs a network path to SES: a public IP, a NAT Gateway, or an SES VPC endpoint.
6. Register the revision, update the service (force new deployment).

Check the SES region and free-plan terms in your own account; I cannot see what your plan includes.

### Mobile (SMS) verification: not implemented
SMS would use Amazon SNS `Publish` to a phone number, with a code flow identical to the email one
(only the delivery function changes). Reasons it is not included: for numbers in India you must
register your entity, sender ID and message template (DLT/TRAI) before SMS is delivered, SNS in a
new account starts in an SMS sandbox (verified destination numbers only) with spend limits, and
every SMS costs money. A managed alternative that supports email and SMS verification is Amazon Cognito.

---------------------------------------------------------------------------

## 1. Run locally with Docker Compose

```bash
cp .env.example .env          # edit DB_PASSWORD and JWT_SECRET
docker compose up -d --build
docker compose ps             # all 3 should be running / healthy
```
Open http://localhost:8080 - you will land on the sign-in page. Choose **Create account**, register,
open the verification email in Mailpit (http://localhost:8025), enter the code, then click
**Load sample data** on the empty dashboard.

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
