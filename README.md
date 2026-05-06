# AWS Cost Dashboard

Lightweight self-hosted dashboard showing per-project AWS cost breakdowns, powered by the Cost Explorer API.

## Architecture

```
Browser → Nginx (port 80) → static HTML/JS frontend
                          → /api/* proxy → Flask + boto3 → AWS Cost Explorer
```

Single EC2 t3.micro instance (~$8/month). IAM role — no credentials stored on disk.

## Prerequisites

1. **Tag your resources** with `Project = <project-name>` on EC2, S3, RDS, etc.
2. **Enable Cost Explorer** in your AWS account (Billing console → Cost Explorer → Enable).  
   Note: first-time activation can take up to 24 hours to populate data.
3. **EC2 instance** running Ubuntu 22.04 or Amazon Linux 2023.

## IAM Setup

Attach an instance profile with `deploy/iam-policy.json`. This grants read-only Cost Explorer access — no other permissions needed.

```bash
# Create policy
aws iam create-policy \
  --policy-name AWSCostDashboardPolicy \
  --policy-document file://deploy/iam-policy.json

# Attach to your instance role
aws iam attach-role-policy \
  --role-name YourEC2Role \
  --policy-arn arn:aws:iam::<account-id>:policy/AWSCostDashboardPolicy
```

## Deploy

```bash
git clone <this-repo>
cd aws-cost-dashboard
sudo bash deploy/setup.sh
```

Open `http://<your-ec2-ip>` in a browser. Restrict Security Group port 80 to your IP.

## Configuration

Edit `/etc/systemd/system/aws-cost-dashboard.service` to set env vars:

| Variable | Default | Description |
|---|---|---|
| `PROJECT_TAG_KEY` | `Project` | The AWS tag key used to group resources |
| `AWS_DEFAULT_REGION` | `us-east-1` | Region (Cost Explorer always uses us-east-1 internally) |

After editing: `sudo systemctl daemon-reload && sudo systemctl restart aws-cost-dashboard`

## API Endpoints

| Endpoint | Description |
|---|---|
| `GET /api/summary` | Cost per project + service breakdown, last 3 months |
| `GET /api/trend` | Daily cost by project, last 30 days |
| `GET /api/forecast` | MTD actual + this-month forecast |
| `GET /api/services` | Top services by cost, last 30 days |

## Cost of the dashboard itself

- EC2 t3.micro: ~$8/month
- Cost Explorer API: $0.01 per request (dashboard makes 4 calls per page load — negligible)


## Set password
```
sudo sed -i 's/Environment=PROJECT_TAG_KEY=Project/Environment=PROJECT_TAG_KEY=Project\nEnvironment=DASHBOARD_USER=admin\nEnvironment=DASHBOARD_PASS=lWRr31ovl0gyK/' /etc/systemd/system/aws-cost-dashboard.service
```

## Updating

Run this in the terminal after transferring files

```
# Move app.py
sudo mv ~/app.py /opt/aws-cost-dashboard/backend/
# Restart app
sudo systemctl daemon-reload
sudo systemctl restart aws-cost-dashboard

# Move index.html
sudo mv ~/index.html /var/www/aws-cost-dashboard/
```

Check the web interface, log in, click "Rescan".


# See status
```
sudo systemctl status aws-cost-dashboard
```

# Scan

```
# Get it to start scanning
sudo curl -X POST http://localhost:5000/api/audiomoth/scan
# Test
curl http://localhost:5000/api/audiomoth/status

```

# API Endpoints

```
curl http://localhost:5000/api/audiomoth/locations
curl http://localhost:5000/api/audiomoth/units
```

sudo journalctl -u aws-cost-dashboard -n 200 --no-pager | grep -i "scan\|flac\|error"




