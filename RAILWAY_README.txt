RAILWAY DEPLOY

1. Upload these files to GitHub:
   bot.py
   requirements.txt
   Dockerfile
   railway.toml
   .gitignore

2. In Railway:
   New Project -> Deploy from GitHub Repo

3. Add these Variables:
   BOT_TOKEN=YOUR_NEW_BOT_TOKEN
   ADMIN_ID=7692023421
   FORCE_CHANNEL=@safa2vz
   ADMIN_USERNAME=@huxmh
   PROXY_URL=
   DB_NAME=/data/bot.db

4. Add a Railway Volume and mount it at:
   /data

5. Deploy.

IMPORTANT:
- Do NOT commit .env or your bot token to GitHub.
- This bot uses Telegram polling, so no public HTTP port is required.
- SQLite is stored at /data/bot.db so it survives normal redeploys when the Volume remains attached.
- Railway pricing/free-credit availability can change; check your Railway account's current plan/usage page.
