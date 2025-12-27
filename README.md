# 🛡️ Server System Guard Bot
## بات نگهبان و مانیتورینگ سرور

🎛 **Telegram Bot for Server Monitoring & Management**  
🎛 **ربات تلگرام برای مانیتورینگ، مدیریت و بررسی اتصال سرور**

---

## ✨ Features | ویژگی‌ها

- 🖥 مدیریت سرور از طریق تلگرام  
- 🔄 ریبوت از راه دور  
- 📊 داشبورد زنده وضعیت  
- 🔔 اعلان قطع و وصل  
- 🌐 مانیتورینگ اتصال ایران (check-host.net)  
- 🐳 آماده اجرا با Docker  

---

## 📦 Requirements | پیش‌نیازها

- Docker & Docker Compose
- Telegram Bot Token
- SSH Access (root user & password)

---

## 🐳 Installation (Docker Compose)

### 1️⃣ ساخت پوشه پروژه
```bash
mkdir server-guard-bot
cd server-guard-bot
```

---

### 2️⃣ ساخت فایل `.env`
```bash
nano .env
```

```env
BOT_TOKEN=YOUR_BOT_TOKEN
OWNER_ID=123456789
SECRET_KEY=GENERATE_RANDOM_SECRET
DB_PATH=/data/database.sqlite
PING_INTERVAL=30
```

🔎 **PING_INTERVAL=30** → مدت زمان چک کردن آپ‌تایم سرور (بر حسب ثانیه)

> 🔐 ساخت SECRET_KEY:
```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

---

### 3️⃣ فایل `docker-compose.yml`
```bash
nano docker-compose.yml
```

```yaml
services:
  bot-guard:
    container_name: server-guard
    image: ghcr.io/fsnova/server-guard-bot:latest
    env_file: .env
    environment:
      - TZ=Asia/Tehran
    volumes:
      - ./data:/data
    restart: unless-stopped
```

---

### 4️⃣ اجرای سرویس
```bash
docker compose up -d
```

---

## 🌐 Iran Monitoring

- هر نود ایران باید پاسخ **4/4** بدهد
- آستانه از داخل ربات قابل تنظیم است
- هشدار فقط هنگام تغییر وضعیت ارسال می‌شود

---

## 🛠 Developer
```text
FS
Telegram: @faradasqarii
```

---

## 💰 Donation
```text
TRX (TRON)
TXfpMhzmKemCYDg9PtAcmF7iWZJJe6couz
```

---

⭐ اگر پروژه مفید بود Star کن!
