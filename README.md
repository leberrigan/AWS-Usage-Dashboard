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

## HTTPS

The site is served by nginx (`deploy/nginx.conf`), so HTTPS is a one-time Certbot run on the EC2 instance — no code changes needed.

1. **DNS**: point your domain (`motusaws.duckdns.org`) at the instance's public IP. DuckDNS updates instantly; confirm with `nslookup motusaws.duckdns.org`.
2. **Security Group**: open inbound port 443 (in addition to 80 — Certbot needs 80 for the ACME HTTP-01 challenge, and nginx will keep it open to redirect to HTTPS).
3. **Install Certbot** (already done if you ran `setup.sh` after this change):
   ```bash
   sudo apt-get install -y certbot python3-certbot-nginx
   ```
4. **Issue the cert and auto-configure nginx**:
   ```bash
   sudo certbot --nginx -d motusaws.duckdns.org
   ```
   Certbot edits `/etc/nginx/sites-available/aws-cost-dashboard` in place to add a `listen 443 ssl` server block with the cert paths, and (if you accept the prompt) adds an HTTP→HTTPS redirect on port 80. Reload isn't needed — Certbot does it for you.
5. **Verify**: open `https://motusaws.duckdns.org` — should show a valid padlock. `http://` should redirect to `https://`.
6. **Auto-renewal**: Certbot installs a systemd timer/cron job automatically. Confirm with:
   ```bash
   sudo certbot renew --dry-run
   ```

`deploy/nginx.conf` in this repo has `server_name` set to `motusaws.duckdns.org` for reference, but the live SSL block only exists in the server's copy after Certbot runs (it isn't reflected back into git automatically — if you want it tracked, copy `/etc/nginx/sites-available/aws-cost-dashboard` back into `deploy/nginx.conf` after issuing the cert).

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


## Access control

Sign-in and authorization are handled centrally, not by this app. oauth2-proxy
in front of nginx on the shared Motus AWS host does GitHub OAuth and sets
`X-Auth-Request-Email`; this app then asks the shared User Management service
(`127.0.0.1:5004`) whether that email has the `admin` role for `aws-costs`.
Grants are managed in Baserow (Users table, "Tool Access" field,
`aws-costs:admin`), not in this repo — there's no local password to set.

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




