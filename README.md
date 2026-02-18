# AircraftTracker Cloud Backend 🚀

Cloud-hosted aircraft tracking system with real-time notifications.

## Features

✅ **Cloud-Hosted** - Runs 24/7, tracks for all users  
✅ **Real-Time Tracking** - 10-second polling of ADS-B data  
✅ **Sequential Landing Detection** - 10nm → 5nm → 2nm approach alerts  
✅ **Multi-User** - Each user has their own aircraft, settings, integrations  
✅ **License System** - Secure license key activation  
✅ **Integrations** - Discord, Slack, Microsoft Teams webhooks  
✅ **Custom Messages** - User-configurable notification templates  

---

## Architecture

```
┌─────────────────────────────┐
│   FastAPI Backend (Python)  │
│   - REST API                │
│   - JWT Authentication      │
│   - Real-time tracker       │
└─────────────────────────────┘
            ↕
┌─────────────────────────────┐
│   PostgreSQL Database       │
│   - Users & licenses        │
│   - Aircraft configs        │
│   - Alert settings          │
└─────────────────────────────┘
            ↕
┌─────────────────────────────┐
│   ADS-B Data (adsb.lol)    │
│   - Free API                │
│   - 100nm radius queries    │
└─────────────────────────────┘
```

---

## Quick Start (Local Development)

### 1. Prerequisites

- Python 3.11+
- PostgreSQL 14+
- pip

### 2. Setup

```bash
# Clone repository
cd aircraft-tracker-cloud/backend

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Create .env file
cp .env.example .env
# Edit .env with your database credentials

# Create database
createdb aircraft_tracker

# Run migrations (tables will be created automatically)
```

### 3. Create a License Key

```bash
python -c "
import hashlib, secrets
seed = f'admin@example.com{secrets.token_hex(16)}'
key_hash = hashlib.sha256(seed.encode()).hexdigest()
key = f'KDTO-{key_hash[0:4].upper()}-{key_hash[4:8].upper()}-{key_hash[8:12].upper()}-{key_hash[12:16].upper()}'
print(f'License Key: {key}')
"
```

Then insert into database:
```sql
INSERT INTO licenses (id, license_key, tier, activations_used, activations_max, status, created_at)
VALUES (
    gen_random_uuid(),
    'KDTO-XXXX-XXXX-XXXX-XXXX',  -- Your generated key
    'enterprise',
    0,
    -1,  -- Unlimited activations
    'active',
    NOW()
);
```

### 4. Run Server

```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

API will be available at: http://localhost:8000  
API docs at: http://localhost:8000/docs

---

## Deployment (Railway.app) 🚂

### Step 1: Create Account
1. Go to [railway.app](https://railway.app)
2. Sign up with GitHub
3. Create new project

### Step 2: Add PostgreSQL
1. Click "New" → "Database" → "PostgreSQL"
2. Railway automatically provisions database
3. Note the connection string

### Step 3: Deploy Backend
1. Click "New" → "GitHub Repo"
2. Select your repository
3. Add environment variables:
   - `DATABASE_URL` - (auto-populated by Railway)
   - `JWT_SECRET_KEY` - Generate a secure random string
   - `ENVIRONMENT` - `production`

4. Add build command: `pip install -r requirements.txt`
5. Add start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`

### Step 4: Get API URL
Railway will provide a URL like: `https://yourapp.up.railway.app`

**Done!** Your backend is live! 🎉

---

## Deployment (DigitalOcean) 🌊

### Using Docker

```dockerfile
# Dockerfile
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

```bash
# Build and run
docker build -t aircraft-tracker .
docker run -p 8000:8000 --env-file .env aircraft-tracker
```

### Using DigitalOcean App Platform

1. Create account at [digitalocean.com](https://digitalocean.com)
2. Create App → Select GitHub repo
3. Add PostgreSQL database
4. Configure environment variables
5. Deploy

**Cost: ~$12/month** (Basic droplet + database)

---

## API Endpoints

### Authentication
- `POST /api/activate` - Activate license key
- `GET /api/user/me` - Get current user info

### Aircraft
- `GET /api/aircraft` - List tracked aircraft
- `POST /api/aircraft` - Add aircraft
- `DELETE /api/aircraft/{id}` - Remove aircraft
- `GET /api/aircraft/live` - Get real-time data

### Settings
- `GET /api/settings/alerts` - Get alert settings
- `POST /api/settings/alerts` - Update alert settings

### Integrations
- `GET /api/integrations` - List integrations
- `POST /api/integrations` - Add integration
- `POST /api/integrations/{id}/test` - Test integration

### Health
- `GET /health` - Health check
- `GET /` - API info

---

## Database Schema

See `models.py` for complete schema. Main tables:

- `users` - User accounts
- `licenses` - License keys
- `aircraft` - Tracked aircraft
- `airport_configs` - Airport settings per user
- `alert_settings` - Custom alert messages
- `integrations` - Discord/Slack/Teams webhooks
- `notification_logs` - Notification history

---

## Testing

### Activate License
```bash
curl -X POST http://localhost:8000/api/activate \
  -H "Content-Type: application/json" \
  -d '{
    "license_key": "KDTO-XXXX-XXXX-XXXX-XXXX",
    "email": "test@example.com"
  }'
```

Response:
```json
{
  "access_token": "eyJ...",
  "token_type": "bearer",
  "user_id": "...",
  "email": "test@example.com",
  "license_tier": "enterprise"
}
```

### Add Aircraft
```bash
curl -X POST http://localhost:8000/api/aircraft \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "tail_number": "N80896",
    "icao24": "ab0347"
  }'
```

### Get Live Aircraft
```bash
curl http://localhost:8000/api/aircraft/live \
  -H "Authorization: Bearer YOUR_TOKEN"
```

---

## Environment Variables

| Variable | Description | Required | Default |
|----------|-------------|----------|---------|
| `DATABASE_URL` | PostgreSQL connection string | Yes | - |
| `JWT_SECRET_KEY` | Secret for JWT tokens | Yes | - |
| `API_HOST` | API host | No | `0.0.0.0` |
| `API_PORT` | API port | No | `8000` |
| `ALLOWED_ORIGINS` | CORS origins | No | `*` |
| `ENVIRONMENT` | `development` or `production` | No | `development` |

---

## Costs

### Railway.app (Recommended for Start)
- **Free tier:** $5/month credit (limited)
- **Starter:** $5/month (enough for testing)
- **Pro:** $20/month (production-ready)

### DigitalOcean
- **Droplet:** $6/month (1GB RAM)
- **Database:** $15/month (managed PostgreSQL)
- **Total:** ~$21/month

### AWS
- **EC2 t3.micro:** $10/month
- **RDS PostgreSQL:** $15/month
- **Total:** ~$25/month

---

## Monitoring

### Logs
```bash
# Railway
railway logs

# Docker
docker logs -f container_id

# DigitalOcean
doctl apps logs YOUR_APP_ID
```

### Health Check
```bash
curl http://your-api-url/health
```

---

## Troubleshooting

### Database Connection Failed
- Check `DATABASE_URL` format
- Ensure database exists
- Check firewall rules

### License Activation Failed
- Verify license key format: `KDTO-XXXX-XXXX-XXXX-XXXX`
- Check license exists in database
- Verify not expired

### No Aircraft Tracking
- Check aircraft has `icao24` set
- Verify airport config exists
- Check adsb.lol API is accessible

---

## Next Steps

After backend is running:

1. **Test API** - Use Postman or curl to test endpoints
2. **Create License Keys** - Generate keys for users
3. **Build Desktop App** - Electron + React frontend
4. **Build Web App** - Same React code, hosted separately
5. **Add Features** - SMS notifications, mobile apps, etc.

---

## Support

For issues or questions:
- Check API docs: `/docs`
- Review logs
- Test integrations via `/api/integrations/{id}/test`

---

## License

Proprietary - All rights reserved
