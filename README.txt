# 🛡️ Server System Guard Bot | بات نگهبان سیستم سرور

🎛 Telegram bot for server monitoring & management with Iran connectivity checks.  
🎛 ربات تلگرام برای مانیتورینگ و مدیریت سرور با بررسی اتصال ایران.

---

## ✨ Features | ویژگی‌ها
- 🖥 Server management via Telegram | مدیریت سرور از طریق تلگرام  
- 🔄 Remote reboot | ریبوت از راه دور  
- 📊 Live status dashboard | داشبورد وضعیت زنده  
- 🔔 Up / Down notifications | اعلان‌های بالا / پایین بودن سرور  
- 🌐 Iran monitoring using [check-host.net](https://check-host.net) | مانیتورینگ ایران با check-host.net  
- 👥 Admin management from UI | مدیریت ادمین‌ها از رابط کاربری  
- 🧹 Log retention & export | نگهداری و خروجی گرفتن از لاگ‌ها  
- 🐳 Docker-ready | آماده برای Docker  

---

## 📦 Requirements | پیش‌نیازها
- Docker & Docker Compose | داکر و داکر کامپوز  
- Telegram Bot Token | توکن ربات تلگرام  
- SSH access to servers | دسترسی SSH به سرورها  

---

## 🚀 Quick Start (Docker) | شروع سریع (داکر)

### 1. Clone | کلون کردن
```bash
git clone https://github.com/fsnova/server-guard-bot.git
cd server-guard-bot

---
2. Create .env | ساخت فایل .env

BOT_TOKEN=YOUR_BOT_TOKEN 
OWNER_ID=123456789
SECRET_KEY=python3 -c "import secrets; print(secrets.token_hex(32))"
DB_PATH=/data/database.sqlite
PING_INTERVAL=30

-------------------
3. Run with Prebuilt Image | اجرا با ایمیج آماده

services:
  bot-guard:
    container_name: Server-guard
    image: ghcr.io/fsnova/server-guard-bot:latest
    environment:
      - TZ=Asia/Tehran
    build: .
    env_file: .env
    volumes:
      - ./data:/data
    restart: unless-stopped
------------------

docker compose up -d

------------------------

🌐 Iran Monitoring | مانیتورینگ ایران
- Uses check-host.net nodes for Iran connectivity.
- از نودهای check-host.net برای بررسی اتصال ایران استفاده می‌کند.
- Each Iran node must return 4/4.
- هر نود ایران باید ۴/۴ پاسخ دهد.
- Threshold configurable directly from bot UI.
- آستانه از طریق رابط کاربری ربات قابل تنظیم است.
- Alerts sent only on state change.
- اعلان‌ها فقط هنگام تغییر وضعیت ارسال می‌شوند.

🛠 Development | توسعه
t.me:  @faradasqarii


🤝 Contributing | مشارکت

Pull Requests and Issues are welcome!

پول ریکوئست‌ها و ایشوها خوشحال‌کننده‌اند!

📜 License | لایسنس
MIT License  لایسنس MIT
