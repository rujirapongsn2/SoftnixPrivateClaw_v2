# ตัวแปรสภาพแวดล้อม (`.env`)

เอกสารนี้อธิบายตัวแปรทุกตัวใน [`.env.example`](../.env.example) ว่าตั้งไว้ทำอะไร ค่าเริ่มต้นคืออะไร และควรระวังอะไร

## ก่อนเริ่ม

- ตัวแปรทั้งหมดขึ้นต้นด้วย `CLAW_` ตัวแปรซ้อนใช้ `__` คั่น เช่น `CLAW_LLM__MODEL` คือ `llm.model`
  ข้อยกเว้นคือกลุ่ม **เสียงพูดเป็นข้อความ** ที่ใช้ชื่อ `QROQ_*` (ไม่มี `CLAW_`)
- **ติดตั้งด้วย `./install.sh` ไม่ต้องแก้ไฟล์นี้เอง** ตัวติดตั้งสร้าง `.env` ให้ (secret key สุ่ม, URL ของ Postgres, โหมดล็อกอินด้วยรหัสผ่าน)
  และ **จะไม่เขียนทับ `.env` ที่มีอยู่แล้ว** ตอนรัน `claw update`
- ตัวแปรที่เป็นบรรทัดขึ้นต้นด้วย `#` คือตัวเลือก: ถ้าไม่เปิดใช้ ระบบใช้ค่าเริ่มต้นในโค้ด
  ปล่อยไว้แบบนั้นจะทำให้ติดตั้งตามค่าเริ่มต้นใหม่ได้เมื่ออัปเดต ไม่ถูกตรึงไว้กับค่าเก่า
- ค่าที่ผู้ดูแลตั้งใน Control Plane (LLM providers, guardrails, OAuth apps ฯลฯ) มีผลเหนือค่าเหล่านี้ตอนแชต
- แก้ `.env` แล้วต้อง **restart** (`./scripts/claw restart`) ถึงจะมีผล

### ตัวแปรที่ต้องตั้งเพื่อให้ใช้งานได้

| ตัวแปร | เหตุผล |
|---|---|
| `CLAW_SECRET_KEY` | ใช้ลงนามโทเคนล็อกอินและเข้ารหัสความลับในฐานข้อมูล |
| `CLAW_LLM__MODEL` และ `CLAW_LLM__API_KEY` | โมเดลเริ่มต้น (หรือตั้งผู้ให้บริการเองใน Control Plane แทน) |
| `CLAW_DATABASE_URL` | ต้องชี้ไปที่ Postgres ที่รันอยู่จริง |

ที่เหลือเป็นตัวเลือก

### ค่าที่ `.env.example` ต่างจากค่าเริ่มต้นในโค้ด

| ตัวแปร | ใน `.env.example` | ค่าเริ่มต้นในโค้ด |
|---|---|---|
| `CLAW_DATABASE_URL` | พอร์ต `5442` (docker compose map 5442→5432 เพื่อไม่ชน Postgres อื่นในเครื่อง) | พอร์ต `5432` |
| `CLAW_SANDBOX__TIMEOUT_SECONDS` | `120` | `90` |

---

## 1. ฐานข้อมูลและความปลอดภัยหลัก

| ตัวแปร | ค่าใน `.env.example` | ความหมาย |
|---|---|---|
| `CLAW_DATABASE_URL` | `postgresql+asyncpg://claw:claw@localhost:5442/claw` | ที่อยู่ฐานข้อมูล ต้องตรงกับที่ Postgres รันอยู่จริง หากใช้ Postgres บนดิสก์ภายนอกต้องเปิดดิสก์ก่อน |
| `CLAW_AUTO_MIGRATE` | `true` | รัน Alembic migration อัตโนมัติตอนเริ่มระบบ แนะนำให้เปิดใน production |
| `CLAW_SECRET_KEY` | `change-me-32-bytes-random` | กุญแจหลักของระบบ **ต้องเปลี่ยนเป็นค่าสุ่มยาวๆ** ถ้าเปลี่ยนค่านี้ภายหลัง ผู้ใช้ทุกคนจะถูกบังคับล็อกอินใหม่ และความลับที่เข้ารหัสไว้ในฐานข้อมูล (API key ของ provider, connector) จะถอดรหัสไม่ได้ ควรสำรองค่านี้ไว้ |
| `CLAW_DEV_TOKEN` | `dev-token` | โทเคนคงที่สำหรับสคริปต์/ทดสอบ ใช้ได้เฉพาะเมื่อ `CLAW_AUTH_MODE=dev` |
| `CLAW_AUTH_MODE` | `dev` | `dev` ยอมรับ `CLAW_DEV_TOKEN` + อีเมล เป็นการยืนยันตัวตนด้วย (สะดวกตอนพัฒนา) ส่วน `password` ต้องล็อกอินเอาโทเคน JWT เท่านั้น **ตัวติดตั้งตั้งเป็น `password`** |
| `CLAW_OPEN_REGISTRATION` | `true` | เปิดให้ใครก็ได้สมัครเอง ตั้ง `false` เมื่อต้องการให้เฉพาะผู้ดูแลสร้างบัญชี |

> **ความเสี่ยง:** ในโหมด `dev` ใครรู้ `CLAW_DEV_TOKEN` จะระบุอีเมลของผู้ใช้คนใดก็ได้ (รวมถึงผู้ดูแล) แล้วเข้าระบบในฐานะคนนั้น
> เครื่องที่เปิดให้ผู้อื่นเข้าถึงต้องใช้ `CLAW_AUTH_MODE=password` และเปลี่ยน `CLAW_SECRET_KEY` เสมอ ในโหมด `password` บัญชีแรกที่สมัครจะเป็นผู้ดูแล

---

## 2. การล็อกอินด้วยบัญชีภายนอก (OIDC) และ URL สาธารณะ

| ตัวแปร | ค่าใน `.env.example` | ความหมาย |
|---|---|---|
| `CLAW_PUBLIC_BASE_URL` | `http://localhost:8700` | URL สาธารณะของ API นี้ ใช้สร้าง redirect URI ของ OIDC เมื่ออยู่หลัง proxy/โดเมนจริง ให้ใส่ URL `https://…` จริง |
| `CLAW_WEB_BASE_URL` | `http://localhost:5173` | URL ของเว็บแอปที่จะพากลับมาหลังล็อกอิน และที่ใช้ในลิงก์ในอีเมล (เปิดใช้งานบัญชี, ตั้งรหัสผ่านใหม่) ถ้าตั้งแค่ `PUBLIC_BASE_URL` ระบบจะใช้ค่านั้นแทนให้ เพื่อไม่ให้อีเมลมีลิงก์ `localhost` |
| `CLAW_OIDC_GOOGLE_CLIENT_ID` / `_CLIENT_SECRET` | ว่าง | สร้าง OAuth client ที่ Google แล้วเพิ่ม redirect URI `<PUBLIC_BASE_URL>/api/auth/oidc/google/callback` เปิดใช้เมื่อตั้งครบทั้งคู่ |
| `CLAW_OIDC_MICROSOFT_CLIENT_ID` / `_CLIENT_SECRET` | ว่าง | Microsoft Entra ID redirect URI `<PUBLIC_BASE_URL>/api/auth/oidc/microsoft/callback` เปิดใช้เมื่อตั้งครบทั้งคู่ |
| `CLAW_OIDC_MICROSOFT_TENANT` | `common` | tenant ของ Microsoft `common` รับทุกองค์กร หรือใส่ tenant ID เพื่อจำกัด |
| `CLAW_TELEGRAM_BOT_TOKEN` | ว่าง | โทเคนบอท Telegram ช่อง Telegram จะเริ่มทำงานก็ต่อเมื่อตั้งค่านี้ |

---

## 3. โมเดล AI เริ่มต้น (LLM)

| ตัวแปร | ค่าใน `.env.example` | ความหมาย |
|---|---|---|
| `CLAW_LLM__MODEL` | `anthropic/claude-sonnet-4-5` | โมเดลเริ่มต้น รองรับทุกโมเดลที่ LiteLLM รองรับ รูปแบบ `ผู้ให้บริการ/ชื่อโมเดล` |
| `CLAW_LLM__API_KEY` | ว่าง | API key ของผู้ให้บริการ ถ้าข้ามไป ให้ตั้ง provider ใน Control Plane → LLM Providers แทน |
| `CLAW_LLM__API_BASE` | ว่าง | endpoint ที่กำหนดเอง เช่น gateway ภายในหรือ OpenRouter ปล่อยว่างเพื่อใช้ค่าของผู้ให้บริการ |
| `CLAW_LLM__MAX_TOKENS` | `32768` (ปิดคอมเมนต์อยู่) | ขีดจำกัดความยาวคำตอบต่อการเรียกโมเดลหนึ่งครั้ง โมเดลแบบ reasoning ใช้โควตานี้กับการคิดที่ซ่อนอยู่ก่อนเขียนคำตอบ การตั้งต่ำเกินไปทำให้ตอบว่างเปล่า ขีดจำกัดของตัวโมเดลเองยังมีผลก่อนถ้าต่ำกว่า |
| `CLAW_LLM__MAX_RECOVERY_OUTPUT_TOKENS` | `65536` (ปิดคอมเมนต์อยู่) | เพดานที่ระบบจะเพิ่มโควตาข้างต้นเป็นสองเท่าเมื่อคำตอบถูกตัดกลางคัน (ลองใหม่ได้สูงสุด 2 ครั้ง) ใช้เฉพาะหลังจากถูกตัดจริง |
| `CLAW_LLM__MAX_TURN_SECONDS` | `600` (ปิดคอมเมนต์อยู่) | เวลาสูงสุดต่อหนึ่งตาของแชต ตรวจระหว่างขั้นตอน `0` = ไม่จำกัด กันโมเดลที่วนซ้ำขั้นตอนเดิมไม่ให้ทำงานนานเป็นสิบนาที |
| `CLAW_LLM__AUTO_CONTINUE_TURNS` | `true` (ปิดคอมเมนต์อยู่) | เมื่อแชตบนเว็บหมดเวลาหรือครบจำนวนขั้นตอนแต่งานยังคืบหน้า ระบบจะทำต่อเป็นงานเบื้องหลังภายใต้งบของ Control Plane → Background job policy `false` = หยุดแล้วให้ผู้ใช้สั่งต่อเอง |

---

## 4. Browser automation

| ตัวแปร | ค่าใน `.env.example` | ความหมาย |
|---|---|---|
| `CLAW_BROWSER__ENABLED` | `false` | เปิดเบราว์เซอร์ฝั่ง server ให้เอเจนต์ใช้ ต้องติดตั้งก่อน: `uv pip install playwright && playwright install chromium` |
| `CLAW_BROWSER__HEADLESS` | `true` | รันแบบไม่แสดงหน้าต่าง |

เบราว์เซอร์ของผู้ใช้เอง (extension บน Chrome) ตั้งด้วย `CLAW_BROWSER__CLIENT_EXTENSION_ENABLED=true`
(ไม่อยู่ใน `.env.example`)

---

## 5. Sandbox สำหรับรันคำสั่ง

คำสั่ง shell ของเอเจนต์รันใน Docker container ชั่วคราว (`docker run --rm`) ที่เมานต์ workspace ของผู้ใช้ไว้ที่ `/workspace`
สร้าง image ก่อนใช้งานครั้งแรก:

```bash
docker build -f docker/sandbox.Dockerfile -t claw-sandbox:latest .
```

| ตัวแปร | ค่าใน `.env.example` | ความหมาย |
|---|---|---|
| `CLAW_SANDBOX__ENABLED` | `true` | `false` = รันคำสั่งเป็น process ธรรมดาบนเครื่อง **ไม่มีการแยกตัว** ใช้เฉพาะตอนพัฒนา |
| `CLAW_SANDBOX__IMAGE` | `claw-sandbox:latest` | image ที่มีเครื่องมือเอกสาร (reportlab, python-docx, python-pptx, openpyxl, pandas, zip/unzip) |
| `CLAW_SANDBOX__CPU_LIMIT` | `1.0` | จำนวน CPU ต่อคำสั่ง |
| `CLAW_SANDBOX__MEMORY_LIMIT` | `1g` | หน่วยความจำต่อคำสั่ง |
| `CLAW_SANDBOX__NETWORK` | `bridge` | `bridge` = มีอินเทอร์เน็ต (ติดตั้ง pip เพิ่มได้ และทุกคำสั่งมี audit log) `none` = แยกขาด ใช้ได้เฉพาะสิ่งที่อยู่ใน image |
| `CLAW_SANDBOX__TIMEOUT_SECONDS` | `120` | เวลาสูงสุดต่อหนึ่งคำสั่ง เกินแล้วถูกฆ่า (ค่าเริ่มต้นในโค้ดคือ 90) |

> คำสั่งที่ค้นทั้งระบบไฟล์ เช่น `find /` หรือ `grep -r … /` ถูกปฏิเสธโดยอัตโนมัติ และให้ค้นใน `/workspace` แทน

### Project containers (โหมด Sbot)

ปิดอยู่เป็นค่าเริ่มต้น เปิดเมื่อต้องการให้ผู้ใช้รันโปรเจกต์ที่พัฒนาต่อเนื่องในคอนเทนเนอร์ของตัวเอง
สร้าง image ด้วย `docker build -f docker/developer.Dockerfile -t sbot-developer:latest .`

| ตัวแปร | ค่าใน `.env.example` | ความหมาย |
|---|---|---|
| `CLAW_SANDBOX__PROJECTS_ENABLED` | `false` | เปิดฟีเจอร์ project containers |
| `CLAW_SANDBOX__PROJECT_DOCKER_ENABLED` | `false` | ให้แต่ละโปรเจกต์มี Docker daemon ของตัวเอง (ใช้ compose ได้) โดยรันคอนเทนเนอร์แบบ `--privileged` ซึ่งเป็นสิทธิ์สูง เปิดเฉพาะเมื่อจำเป็นและเชื่อถือผู้ใช้ |
| `CLAW_SANDBOX__PROJECT_INGRESS_DOMAIN` | ว่าง (ปิดคอมเมนต์) | โดเมนสำหรับเปิดแอปของโปรเจกต์สู่สาธารณะ แบบ wildcard subdomain ต่อผู้ใช้/โปรเจกต์ เช่น `apps.example.com` ต้องตั้ง wildcard DNS + TLS ก่อน แล้วเปิดสวิตช์ใน Control Plane → Project containers |
| `CLAW_SANDBOX__PROJECT_INGRESS_SCHEME` | `https` | `http` หรือ `https` |
| `CLAW_SANDBOX__PROJECT_INGRESS_PORT` | `8000` | พอร์ตคงที่ในคอนเทนเนอร์ที่ proxy ส่งต่อไป ต้องอยู่ในรายการพอร์ตของโปรเจกต์ |
| `CLAW_SANDBOX__PROJECT_NETWORK` | `sbot-project-ingress` | คำนำหน้าชื่อ Docker bridge แยกต่อโปรเจกต์ (ไม่ใช้เครือข่ายร่วม จึงแยกผู้ใช้ออกจากกัน) |
| `CLAW_SANDBOX__PROJECT_NETWORK_POOL` | `10.240.0.0/12` | ช่วง IP ส่วนตัวที่ใช้แบ่งเครือข่ายโปรเจกต์ **ต้องไม่ซ้อนกับเครือข่ายของเครื่องหรือ VPN** |
| `CLAW_SANDBOX__PROJECT_NETWORK_PREFIX` | `28` | ขนาด subnet ต่อโปรเจกต์ (ค่า 24–30) เล็กเพื่อไม่กินช่วง IP ที่ Docker ใช้ |
| `CLAW_SANDBOX__PROJECT_NETWORK_LIMIT` | `4096` | จำนวนเครือข่ายโปรเจกต์สูงสุด |
| `CLAW_SANDBOX__PROJECT_PROXY_CONTAINER` | ว่าง | ตั้งเฉพาะเมื่อคอนเทนเนอร์ของแอปมีชื่อ/hostname เอง ว่าง = ตรวจจับอัตโนมัติ |
| `CLAW_SANDBOX__PROJECT_INGRESS_WS_MAX_BYTES` | `1048576` | ขนาดข้อความ WebSocket สูงสุดที่ proxy ยอมรับ (ไบต์) |
| `CLAW_SANDBOX__PROJECT_INGRESS_MAX_CONNECTIONS` | `256` | จำนวนการเชื่อมต่อพร้อมกันทั้งระบบ |
| `CLAW_SANDBOX__PROJECT_INGRESS_MAX_CONNECTIONS_PER_PROJECT` | `32` | จำนวนการเชื่อมต่อพร้อมกันต่อโปรเจกต์ |

---

## 6. เสียงพูด

### เสียงเป็นข้อความ (ไมค์ในช่องพิมพ์)

ตัวแปรกลุ่มนี้ **ไม่มี `CLAW_`** ปุ่มไมค์จะปรากฏเมื่อตั้ง `QROQ_KEY`
(ชื่อ `QROQ` สะกดแบบนี้ตามที่โค้ดอ่านจริง)

| ตัวแปร | ค่าใน `.env.example` | ความหมาย |
|---|---|---|
| `QROQ_KEY` | ว่าง | API key (Groq Whisper หรือบริการที่ใช้รูปแบบ OpenAI `/audio/transcriptions`) |
| `QROQ_URL` | `https://api.groq.com/openai/v1` | endpoint ของบริการ |
| `QROQ_MODEL` | `whisper-large-v3` | โมเดลถอดเสียง |

### ข้อความเป็นเสียง (ปุ่มอ่านออกเสียง)

ตั้งแยกจาก LLM providers ใน Control Plane โดยสิ้นเชิง ใช้ได้กับ endpoint `/audio/speech` ที่เข้ากันกับ OpenAI
ถ้าไม่ตั้ง `CLAW_TTS__API_KEY` ปุ่มจะถูกซ่อน และ `/api/tts` ตอบ 503

| ตัวแปร | ค่าใน `.env.example` | ความหมาย |
|---|---|---|
| `CLAW_TTS__API_BASE` | `https://api.openai.com/v1` | endpoint เช่น OpenRouter ใช้ `https://openrouter.ai/api/v1` |
| `CLAW_TTS__API_KEY` | ว่าง | สวิตช์เปิด/ปิดฟีเจอร์เพียงตัวเดียว |
| `CLAW_TTS__MODEL` | `tts-1` | โมเดลเสียง เช่น `openai/gpt-4o-mini-tts-2025-12-15` เมื่อใช้ OpenRouter |
| `CLAW_TTS__VOICE` | `alloy` | ชื่อเสียงพูด |

---

## 7. Log

แอปเขียน log แบบหมุนไฟล์ที่ `logs/claw.log` ด้วยสิทธิ์ `0600` (เฉพาะเจ้าของ)
ส่วน log ของตัวคุมบริการ (`claw.log` หรือ `journalctl` บน systemd) เก็บสิ่งที่พังก่อนระบบ log ถูกตั้งค่า

| ตัวแปร | ค่าเริ่มต้น | ความหมาย |
|---|---|---|
| `CLAW_LOG__LEVEL` | `INFO` | ระดับ log |
| `CLAW_LOG__FILE` | `logs/claw.log` | ไฟล์ log (ว่าง = ไม่เขียนไฟล์) |
| `CLAW_LOG__ROTATION` | `20 MB` | หมุนไฟล์เมื่อถึงขนาดนี้ หรือระบุช่วงเวลา เช่น `1 day` |
| `CLAW_LOG__RETENTION` | `14 days` | เก็บไฟล์เก่าไว้นานเท่านี้ ไฟล์ที่หมุนแล้วถูกบีบอัด |
| `CLAW_LOG__DIAGNOSE` | `false` | **ใช้เฉพาะดีบัก** เพิ่มค่าของทุกตัวแปรในทุกเฟรมของ traceback ซึ่งหมายถึง prompt, ข้อความ และ `CLAW_DATABASE_URL` (รวมรหัสผ่าน) จะลงไฟล์ log อย่าเปิดบนเครื่องที่ใช้ร่วมกันหรือ production |

---

## 8. ที่เก็บข้อมูลถาวร

ค่าเริ่มต้นเป็นพาธสัมพัทธ์ (`./workspaces`, `./knowledge`) เหมาะกับการพัฒนาในเครื่อง
ตัวติดตั้งและ Docker production ตั้งเป็นพาธเต็มให้

| ตัวแปร | ค่าเริ่มต้น | ความหมาย |
|---|---|---|
| `CLAW_WORKSPACES_ROOT` | `workspaces` | โฟลเดอร์ workspace ต่อผู้ใช้ (ไฟล์แนบและไฟล์ที่เอเจนต์สร้าง ซึ่งสะสมต่อไปเรื่อยๆ ควรวางบนดิสก์ที่มีที่ว่างพอ) |
| `CLAW_KNOWLEDGE_ROOT` | `knowledge` | ชุดไฟล์ฐานความรู้ (OKF bundles) หนึ่งโฟลเดอร์ต่อฐานความรู้ |
| `CLAW_BRANDING_ROOT` | `branding` | โลโก้ที่ผู้ดูแลอัปโหลด (สร้างอัตโนมัติ) |
| `CLAW_WORKSPACE__ENFORCE` | `false` | `false` = **สังเกตการณ์เท่านั้น**: โควตาไม่บล็อกการเขียน (แค่บันทึก log ว่าจะบล็อกใคร) และงานลบไฟล์แค่บันทึก log ว่าจะลบอะไร จึงไม่มีการลบหรือบล็อกผู้ใช้เองตอนอัปเดตระบบที่มีข้อมูลอยู่แล้ว ตรวจ log แล้วค่อยตั้ง `true` หรือกด "Enforce limits" ที่ Control Plane `install.sh` เขียน `true` ให้การติดตั้งใหม่ |
| `CLAW_WORKSPACE__QUOTA_MB` | `2048` | ขนาดรวมสูงสุดของ workspace ต่อผู้ใช้ (MB, `0` = ไม่จำกัด) ตรวจก่อนเขียนไฟล์ (อัปโหลด, `write_file`, สร้างภาพ, ดาวน์โหลดจาก connector, คำสั่ง shell) คำสั่งเดียวอาจเกินโควตาไปได้ หลังจากนั้นจะเขียนเพิ่มไม่ได้จนกว่าจะลบไฟล์ (คำสั่งลบ/ดูไฟล์ยังรันได้) ค่านี้เป็นค่าเริ่มต้น แก้สดได้ที่ Control Plane → Preferences → Workspace storage |
| `CLAW_WORKSPACE__QUOTA_FILES` | `50000` | จำนวนไฟล์สูงสุดต่อผู้ใช้ (`0` = ไม่จำกัด) แก้สดได้เช่นกัน |
| `CLAW_WORKSPACE__TMP_RETENTION_DAYS` | `7` | ไฟล์ใน `.tmp/` ที่เก่ากว่านี้ (วัน) ถูกลบอัตโนมัติ `0` = ไม่ลบ แก้สดได้เช่นกัน |
| `CLAW_WORKSPACE__UPLOADS_RETENTION_DAYS` | `7` | ไฟล์แนบใน `uploads/` ที่เก่ากว่านี้ (วัน) ถูกลบอัตโนมัติ ยกเว้นภาพที่สร้างจากระบบ (`generated-*`) `0` = ไม่ลบ แก้สดได้เช่นกัน |
| `CLAW_WORKSPACE__CLEANUP_ENABLED` | `true` | เปิด/ปิดงานเบื้องหลังที่ลบไฟล์หมดอายุ แก้สดได้ที่ Control Plane |
| `CLAW_WORKSPACE__CLEANUP_INTERVAL_MINUTES` | `60` | ระยะห่างระหว่างรอบลบ (ตั้งได้เฉพาะใน `.env`) |
| `CLAW_WORKSPACE__SNAPSHOT_MAX_FILES` | `20000` | จำนวนไฟล์สูงสุดที่เอเจนต์ตรวจหาไฟล์ใหม่หลังรันคำสั่ง เกินแล้วข้ามการตรวจรอบนั้น ไฟล์ยังอยู่ แต่จะไม่ขึ้นเป็นปุ่มดาวน์โหลดอัตโนมัติ (ตั้งได้เฉพาะใน `.env`) |
| `CLAW_CONNECTORS__MAX_DOWNLOAD_BYTES` | `104857600` (100 MB) | ขนาดไฟล์สูงสุดที่เครื่องมือ `save_connector_file` จะคัดลอกจาก MCP connector (เช่น วิดีโอที่เรนเดอร์เสร็จ) มาไว้ใน workspace ของผู้ใช้ |

### พื้นที่ของผู้ใช้: อะไรถูกลบเมื่อไหร่

- **ระบบที่อัปเดตมาจะอยู่ในโหมดสังเกตการณ์ก่อน:** ถ้า `.env` ไม่มี `CLAW_WORKSPACE__ENFORCE=true` โควตาจะไม่บล็อกใครและงานลบจะไม่ลบอะไร แค่บันทึกใน log ว่า "จะ" ทำอะไร (ดูบรรทัด `Workspace cleanup would delete …` และ `limits are not enforced yet`) ตรวจแล้วค่อยเปิด `Enforce limits` ที่ Control Plane → Preferences → Workspace storage การติดตั้งใหม่ด้วย `install.sh` บังคับใช้ตั้งแต่แรก
- **ไฟล์ชั่วคราว:** เอเจนต์ถูกสั่งให้เก็บไฟล์ชั่วคราวไว้ใน `.tmp/` ของ workspace ไฟล์ที่เก่ากว่า 7 วันถูกลบ (โฟลเดอร์ว่างถูกลบรอบถัดไป)
- **ไฟล์แนบ:** ไฟล์ใน `uploads/` ที่เก่ากว่า 7 วันถูกลบ ยกเว้นภาพที่สร้างจากระบบ (`generated-*`) ซึ่งเป็นเนื้อหาของแชตและมีเพดานของตัวเอง (200 ไฟล์ต่อผู้ใช้) แชตเก่าที่เคยแนบไฟล์จะยังแสดงชื่อไฟล์ แต่เอเจนต์เปิดไฟล์นั้นไม่ได้แล้ว
- **เมื่อลบแชต:** ไฟล์ที่แชตนั้นสร้างและระบบบันทึกไว้ (ไฟล์ผลงานที่แสดงเป็นปุ่มดาวน์โหลด ไฟล์ที่ `write_file`/`generate_workbook` เขียน และไฟล์แนบของแชตนั้น) ถูกลบด้วย ยกเว้นไฟล์ที่แชตอื่นยังแสดงเป็นปุ่มดาวน์โหลดอยู่ หรือถูกแก้ไขหลังข้อความสุดท้ายของแชต ไฟล์ที่เกิดจากคำสั่ง shell ไม่ถูกบันทึกจึงไม่ถูกลบ (แต่ยังอยู่ใต้โควตาและเกณฑ์อายุข้างต้น)
- **การติดตั้งแพ็กเกจ/`git clone`:** เอเจนต์ถูกห้ามลงใน `/workspace` ต้องทำใน `/tmp` ของ container ซึ่งหายไปเมื่อคำสั่งจบ (ใน Sbot mode ไม่ใช้ข้อห้ามนี้ เพราะ workspace เป็นโฟลเดอร์โปรเจกต์)
- ระบบบันทึก `workspace_cleanup` ใน audit log ทุกรอบที่มีการลบ

---

## 9. Docker production เท่านั้น (`docker-compose.prod.yml`)

| ตัวแปร | ความหมาย |
|---|---|
| `CLAW_DATA_DIR` | พาธเต็มบนโฮสต์ที่เก็บข้อมูลถาวร ถูกเมานต์ที่ **พาธเดียวกัน** ในคอนเทนเนอร์ เพื่อให้ Docker daemon ของโฮสต์เมานต์ workspace เข้า sandbox ได้ถูกที่ (Docker-outside-of-Docker) สร้างก่อนใช้: `mkdir -p /srv/claw/data/workspaces /srv/claw/data/knowledge` |
| `POSTGRES_PASSWORD` | รหัสผ่าน Postgres ต้องตั้ง ไม่เช่นนั้น compose ไม่ยอมเริ่ม |

---

## 10. โหมด Sbot

| ตัวแปร | ค่าใน `.env.example` | ความหมาย |
|---|---|---|
| `CLAW_SBOT_ENABLED` | `true` | เปิดโหมด Sbot ในแอปเดียวกัน ใช้ล็อกอิน, providers, นโยบาย และ connectors ร่วมกัน |
| `CLAW_SBOT_WORKSPACES_ROOT` | ว่าง (ปิดคอมเมนต์) | workspace ของ Sbot ค่าเริ่มต้นคือโฟลเดอร์พี่น้องของ `CLAW_WORKSPACES_ROOT` ห้ามเป็นโฟลเดอร์ลูก |
| `CLAW_BLUEPRINTS_ROOT` | `blueprints` | ที่เก็บ blueprint |

---

## 11. Semantic guardrails

ตรวจข้อความขาเข้า/ขาออกด้วยบริการภายนอกเพิ่มจากกฎ regex ปิดอยู่เป็นค่าเริ่มต้น เลือกใช้ได้ครั้งละหนึ่งผู้ให้บริการ
กฎและการตอบสนอง (monitor / warn / confirm / block) ตั้งใน Control Plane ส่วน key เก็บฝั่ง server เท่านั้น
รายละเอียด: [semantic-guardrails.md](semantic-guardrails.md)

| ตัวแปร | ค่าใน `.env.example` | ความหมาย |
|---|---|---|
| `CLAW_SEMANTIC_GUARDRAILS__PROVIDER` | `off` | `off`, `jev` หรือ `laya` |
| `CLAW_SEMANTIC_GUARDRAILS__JEV__API_KEY` | ว่าง | key ของ JEV |
| `CLAW_SEMANTIC_GUARDRAILS__JEV__ENDPOINT` | `https://api.typesafe.ai/v1/systemone` | ต้องเป็น HTTPS ไม่มี username/password, query หรือ fragment |
| `CLAW_SEMANTIC_GUARDRAILS__JEV__MODEL` | `jev-1.13.0` | ชื่อโมเดล |
| `CLAW_SEMANTIC_GUARDRAILS__LAYA__API_KEY` | ว่าง | key ของ Laya |
| `CLAW_SEMANTIC_GUARDRAILS__LAYA__ENDPOINT` | `https://genai.softnix.ai/laya/v1/decide` | เงื่อนไขเดียวกับ JEV |
| `CLAW_SEMANTIC_GUARDRAILS__LAYA__MODEL` | `openthai-systemone` | ชื่อโมเดล |
| `CLAW_SEMANTIC_GUARDRAILS__TIMEOUT_SECONDS` | `5` | เวลารอต่อคำขอ (0–30 วินาที) เกินแล้วปล่อยข้อความผ่าน (fail-open) เพื่อไม่ให้ผู้ใช้ติดค้าง |
| `CLAW_SEMANTIC_GUARDRAILS__AUTO_FALLBACK` | `true` | ถ้าผู้ให้บริการที่เลือกล้มเหลว (5xx, 429, auth, timeout) และอีกรายมี key อยู่ จะสลับไปใช้อีกรายอัตโนมัติ ตัวที่ล้มเหลวถูกข้ามไป 60 วินาที ตั้ง `false` เพื่อปิด |

ตัวแปรเพิ่มเติมที่ไม่อยู่ใน `.env.example`: `CLAW_SEMANTIC_GUARDRAILS__MAX_CHARS` (ความยาวข้อความสูงสุดที่ส่งตรวจ ค่าเริ่มต้น `8000`)
และ `CLAW_SEMANTIC_GUARDRAILS__MAX_CONCURRENT` (จำนวนคำขอตรวจพร้อมกัน ค่าเริ่มต้น `4`)

---

## ตัวแปรอื่นที่ตัวติดตั้งตั้งให้ แต่ไม่อยู่ใน `.env.example`

| ตัวแปร | ความหมาย |
|---|---|
| `CLAW_HOST` | ที่อยู่ที่แอปรับการเชื่อมต่อ (`0.0.0.0` = ทุกเครือข่าย) |
| `CLAW_PORT` | พอร์ตของแอป (ค่าเริ่มต้น `8700`) |

รายการตัวแปรทั้งหมดและค่าเริ่มต้นที่แท้จริงอยู่ใน [`claw/config.py`](../claw/config.py) (และ `sbot/config.py` สำหรับ sandbox/โปรเจกต์)
ทุกฟิลด์มีคอมเมนต์อธิบายอยู่ข้างๆ
