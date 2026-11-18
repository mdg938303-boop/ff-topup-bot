# Free Fire Diamond Top-up Bot (Bangladesh Server)

Telegram বট — Wallet, Manual + Auto Deposit (bKash/Nagad/Rocket), API Provider দিয়ে
স্বয়ংক্রিয় top-up (অথবা Admin-fulfilled manual), Referral, Admin Panel।
Stack: aiogram 3 · async SQLAlchemy · PostgreSQL · aiohttp · APScheduler।

---

## 🚀 Render-এ ডিপ্লয় (৫০ ডলার ক্রেডিট অনুযায়ী)

| Service | Plan | খরচ/মাস |
|---|---|---|
| Web Service (বট) | **Starter** (কখনো ঘুমায় না) | $7 |
| PostgreSQL | **Basic-256mb** | $6 |
| Key Value (Redis, FSM-এর জন্য) | Free | $0 |
| **মোট** | | **≈ $13** (storage/bandwidth সামান্য আলাদা) |

→ $50 ক্রেডিটে **প্রায় ৩.৫ মাস**। ⚠️ Free Web প্ল্যান ১৫ মিনিট নিষ্ক্রিয় থাকলে ঘুমিয়ে যায়
(বট অচল), আর Free Postgres ৩০ দিনে মুছে যায় — টাকা-পয়সার ডেটার জন্য দুটোই চলবে না।

> 💡 **আরও সস্তা (~$7/মাস → ৭ মাস):** Render Postgres বাদ দিয়ে [Neon](https://neon.tech) ফ্রি Postgres
> ব্যবহার করুন। `render.yaml` থেকে `databases:` অংশ ও `DATABASE_URL`-এর `fromDatabase` মুছে
> Dashboard-এ Neon-এর connection string বসান — কোড `?sslmode=…&channel_binding=…` ও `-pooler`
> নিজেই সামলায়।

### পদ্ধতি ১ — Blueprint (সবচেয়ে সহজ)
1. এই ফোল্ডার GitHub-এ **private repo** হিসেবে push করুন (`.env` push করবেন না — `.gitignore`-এ আছে)।
2. Render → **New → Blueprint** → repo বেছে নিন। `render.yaml` পড়ে Web + Postgres + Key Value বানাবে।
3. যে ভ্যারিয়েবলগুলো চাইবে সেগুলো দিন:
   - `BOT_TOKEN` — @BotFather থেকে
   - `ADMIN_IDS` — আপনার Telegram numeric ID (কমা দিয়ে একাধিক) — @userinfobot থেকে জানা যায়
   - `SUPPORT_USERNAME` — যেমন `@my_support`
4. Deploy শেষ হলে লগে `Webhook mode — listening…` দেখবেন। বটে `/start`, তারপর `/admin`।

`DATABASE_URL`, `REDIS_URL`, `SECRET_ENCRYPTION_KEY`, `RENDER_EXTERNAL_URL`, `PORT` — সব নিজে নিজেই সেট হয়।

### পদ্ধতি ২ — হাতে হাতে
Postgres (Basic-256mb) আর Key Value (Free) বানান → Web Service (Python, Starter,
Build: `pip install -r requirements.txt`, Start: `python main.py`, Health check: `/healthz`) →
Environment-এ `DATABASE_URL` = Postgres-এর **Internal** URL, `REDIS_URL` = Key Value-এর Internal URL।

### ⚠️ ডিপ্লয়ের পর অবশ্যই
- **`SECRET_ENCRYPTION_KEY` নিরাপদ জায়গায় কপি করে রাখুন এবং কখনো বদলাবেন না।**
  Provider API key ও Mr Ai Pay key এটা দিয়ে এনক্রিপ্ট হয়; হারালে সেগুলো আবার বসাতে হবে।
- নিয়মিত ডাটাবেস ব্যাকআপ রাখুন (Render Dashboard-এর backup/export অপশন দেখুন, বা `pg_dump`)।
- প্রথমে **ছোট অঙ্কে** একটা টেস্ট অর্ডার ও টেস্ট ডিপোজিট করে দেখুন।

---

## 🧩 সিস্টেম

- **Wallet + ledger** — সব লেনদেন `Transaction` টেবিলে।
- **Top-up:** Package → Player ID → Confirm। Player ID মনে থাকে।
- **দুই ডেলিভারি মোড:** Auto (Epinby/SMM provider API, ৬০ সেকেন্ডে status sync, ব্যর্থ হলে auto-refund) ও Manual।
- **UID Check** (Epinby হলে in-game নাম)।
- **Deposit:** Manual (admin approve) ও Auto (Mr Ai Pay)।
- **Referral bonus,** Ban, Broadcast, Activity Log, Admin Panel (সব Reply Keyboard থেকে)।

---

## 🛠️ এই সংস্করণে যা ঠিক করা হয়েছে

**টাকা-পয়সা**
- ✅ **Double-tap এ ডাবল অর্ডার/চার্জ** — confirm একবারই "consume" হয়, আর ১০ সেকেন্ডের duplicate গার্ড আছে।
- ✅ **Auto deposit বাগ** — প্রথম callback `PENDING` থাকলে পরে `COMPLETED` হলেও আর কখনো credit হতো না; এখন হয়।
- ✅ **ব্রাউজার বন্ধ করলে** — "Payment Verify" বাটনে Transaction ID দিয়ে নিরাপদে ব্যালেন্স উদ্ধার (নিচের Mr Ai Pay নোট দেখুন)।
- ✅ **API timeout-এ ডুপ্লিকেট/ফ্রি ডায়মন্ড** — এখন "আগে চার্জ, পরে dispatch"। Provider নিশ্চিত reject করলে auto-refund; উত্তর অস্পষ্ট হলে refund/retry **হয় না**, Admin alert পায়।
- ✅ **Admin cancel + provider delivery = দুবার পাওনা** — provider-এ চলমান অর্ডার cancel করলে আগে provider-কে জিজ্ঞেস করা হয়।
- ✅ **Double refund / double credit** — সব status পরিবর্তন atomic claim (`UPDATE … WHERE status=…`)। দুই admin একসাথে Approve চাপলেও একবারই credit।
- ✅ **Balance race** — `balance = balance ± x` atomic SQL, সব টাকা `Decimal` (float নয়), DB-তে `balance >= 0` constraint।
- ✅ **Manual deposit** — একই TrxID দ্বিতীয়বার নেওয়া হয় না, ফরম্যাট ভ্যালিডেশন, min/max লিমিট, pending সীমা।
- ✅ **`nan`/`inf` দিয়ে টাকা ঢোকানো** বন্ধ।
- ✅ **Referral abuse** — শুধু নতুন অ্যাকাউন্টে, minimum deposit-এ, একবার।

**স্থিতিশীলতা ও নিরাপত্তা**
- ✅ HTML escape সর্বত্র (নামে `<` থাকলে `/start` crash করত; admin-এ নকল লিংক ঢোকানো যেত)।
- ✅ স্টিকার/ছবি পাঠালে crash নেই; global error handler + Admin alert।
- ✅ Ban এখন সব জায়গায় (deposit/wallet সহ) কার্যকর।
- ✅ Provider-এর raw error ইউজারকে দেখানো হয় না (শুধু Admin-কে)।
- ✅ Package/Method/Provider delete: ইতিহাস থাকলে মোছার বদলে লুকানো (FK crash নেই)।
- ✅ Order sync: partial delivery auto-refund হয় না, আটকে থাকা অর্ডারে alert, সমান্তরাল sync।
- ✅ Broadcast: আগে confirm, rate-limit, flood-wait সামলানো, background-এ।
- ✅ Telegram webhook secret, Redis FSM (restart-এ ফর্ম হারায় না), auto schema upgrade, secret এনক্রিপশন।
- ✅ Graceful shutdown (Render SIGTERM), payment পেজে rate-limit।

---

## 💳 Mr Ai Pay (Auto Deposit) নোট
Admin Panel → **Auto Payment** থেকে তিনটা key বসিয়ে Enable করুন। Redirect URL স্বয়ংক্রিয়ভাবে
`https://<আপনার-সার্ভিস>.onrender.com/payment/success`।

অফিসিয়াল ডকস (mraipay.top/developers/docs) পড়ে যা মিলিয়েছি:
- Endpoint, হেডার (`API-KEY`/`SECRET-KEY`/`BRAND-KEY`), verify-এর status (`COMPLETED/PENDING/ERROR`) ✅ কোডের সাথে মেলে।
- **amount** ডকস বলে পূর্ণসংখ্যায় ট্রেইলিং জিরো বাদ দিতে (`10`, `10.5`) — কোড এখন তা-ই পাঠায়।
- **metadata:** ডকসের টেবিলে `meta_data`, কিন্তু স্যাম্পল কোডে `metadata` — দুটোই পাঠানো হয়। তাছাড়া
  ইউজারকে চেনার দ্বিতীয় উপায় হিসেবে `cus_email` (`user<id>@telegram.local`) ব্যবহার হয়।
- **Transaction ID শুধু success redirect-এ আসে** (`?transactionId=…`) — create-এর response-এ নয়, আর
  ডকসে কোনো webhook/IPN নেই। তাই "পেমেন্ট করে ব্রাউজার বন্ধ করে দিলে নিজে থেকে credit" করা **সম্ভব না**
  (আগের সংস্করণের background reconcile job এখন নিষ্ক্রিয়, gateway ID দিলে নিজে সক্রিয় হবে)।
  এর বদলে যোগ করা হয়েছে **🔄 "টাকা কেটেছে কিন্তু ব্যালেন্স আসেনি?"** বাটন: ইউজার Transaction ID দিলে
  বট Mr Ai Pay-তে verify করে (নিজের অ্যাকাউন্টের পেমেন্ট হলে, একবারই) ব্যালেন্স যোগ করে।
  Admin Mr Ai Pay ড্যাশবোর্ড/অ্যাপ থেকে ID দেখে ইউজারকে দিতে পারেন।
- লাইভে যাওয়ার আগে Mr Ai Pay-এর **Sandbox** বা ছোট অঙ্কে একটা টেস্ট পেমেন্ট করে নিন।

## 💻 লোকালি চালানো
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env     # BOT_TOKEN, ADMIN_IDS বসান
python main.py           # PORT না থাকলে polling মোড, SQLite
python -m unittest discover -s tests -v
```

## ⚙️ লিমিট (env দিয়ে বদলানো যায়)
`MIN_DEPOSIT=10` · `MAX_DEPOSIT=50000` · `MAX_PENDING_DEPOSITS_PER_USER=3` ·
`REFERRAL_MIN_DEPOSIT=50` · `ORDER_STUCK_MINUTES=30`

## ⚠️ জানা সীমাবদ্ধতা
- বট **একটাই instance** হিসেবে চালাতে হবে (Render-এ scale = 1)। Deploy-এর সময় কয়েক সেকেন্ড দুটো overlap করলেও টাকার অপারেশন DB-লেভেলে নিরাপদ।
- SQLite শুধু লোকাল ডেভেলপমেন্টের জন্য (দশমিক হিসাব float হয়ে যায়); production-এ Postgres।
- স্কিমা পরিবর্তন শুধু "নতুন কলাম যোগ" পর্যন্ত স্বয়ংক্রিয়; বড় পরিবর্তনে Alembic লাগবে।
