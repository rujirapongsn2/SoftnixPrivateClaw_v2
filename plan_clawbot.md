# Plan: Clawbot — Persistent AI Teammates for Softnix PrivateClaw

> Inspiration: [Grok Bot (docs.x.ai/grok-bot)](https://docs.x.ai/grok-bot/overview) —
> "Bots are AI teammates you can give real work to."
> วันที่จัดทำ: 2026-08-30 · สถานะ: DRAFT รอการอนุมัติ

---

## 1. Grok Bot ทำงานอย่างไร (สรุปจาก docs ทั้งหมด 17 หน้า)

### 1.1 Concept หลัก
- **Bot = durable AI teammate** ที่มีชื่อ มีหน้าที่ (job) มีบทสนทนาของตัวเอง และมี working context ที่สะสมตามเวลา
- คุยกับ Bot เหมือนแชทเพื่อนร่วมงาน: สั่งงาน + ให้บริบท + ให้สิทธิ์เครื่องมือ → Bot ทำจบ end-to-end
- กลับมาหาเราเฉพาะเมื่อ **ต้องขอ approval** หรือถามคำถามที่ต้องตัดสินใจ

### 1.2 เสาหลัก 5 ด้านที่ทำให้ Grok Bot แตกต่าง

| เสาหลัก | รายละเอียด |
|---|---|
| **1. Persistent Cloud Computer** | ทุก Bot แชร์ computer 1 เครื่องต่อ account (VM ที่มี browser + filesystem + terminal) browser sessions/logins คงอยู่ข้าม task ข้าม Bot · แต่ละ Bot มี "screen" ของตัวเองทำงานขนานได้ · `/workspace` ใช้ร่วมกันเป็น shared files |
| **2. Easy start** | สร้าง Bot → แชท → grant สิทธิ์เมื่อจำเป็น ไม่ต้อง workflow builder · เข้าถึงได้ทั้ง desktop และ iOS |
| **3. Bot coordination** | Bot คุยกันเองได้ (async handoff) · group chat 2-6 Bots · `@mention` ชี้เป้า, `@everyone`, ปล่อยให้ Bot เลือกเองก็ได้ · user ไม่ต้องเป็น router กลาง |
| **4. Teach by demonstration** | บันทึกการทำงานบน browser 1 ครั้ง (≤10 นาที) → กลายเป็น **skill** ที่รันซ้ำได้ (schedule หรือ on-demand) |
| **5. Durable state** | memory + files + browser sessions + preferences อยู่ท้าย turn · context สะสม ไม่ reset |

### 1.3 ระบบประกอบ

**Bot management**
- สร้าง (New → Create new agent), Edit Profile (name/title/description/avatar)
- กติกาคั่นชั้น: **Description = กฎถาวร** ("ห้ามส่งข้อความภายนอกโดยไม่ approval") / **Message = คำสั่งงานครั้งนี้**
- Cap 50 Bots+groups ต่อ account · Pin/Hide/Duplicate/Share (share = public link ส่ง config คัดลอกไป ไม่รวมประวัติ/computer) / Delete

**Skills & Routines**
- **Skill** = วิธีทำงานที่ reusable (when-to-use, inputs, steps, validation, output, approval boundaries) · แชร์ข้าม Bot · `พิมพ์ /` เรียกใช้ · ติดตั้งผ่าน Settings → Plugins
- **Routine** = ตัวจับเวลา/เหตุการณ์ที่บอกว่า Bot ต้องรัน workflow เมื่อไหร่ · มี Test run, run history, pause/resume · trigger จาก event ได้ (Slack/GitHub)
- ปรัชญา: task หนึ่งครั้ง → ทำให้เสถียร → save เป็น skill → ค่อย automate เป็น routine

**Approvals & security**
- Approval per-action: แสดง target/scope/values ก่อนกด Allow once / Always allow (บันทึกเป็น rule) / Deny
- **Auto Review**: rule-based + model-based ประเมิน tool call ก่อนรัน (Require Approval / Always Allow, Require ชนะเมื่อชนกัน)
- Secure handoff: ขอรหัส/2FA ผ่านช่อง masked ไม่ค้างใน conversation
- Take over: ผู้ใช้แชร์ screen เข้าไปทำเฉพาะ step ที่ Bot ทำไม่ได้ (captcha/payment)

**Files & results**
- แนบไฟล์ได้หลายประเภท (25MB/file, วิดีโอ 200MB, ≤6 attachments), สั่งผลลัพธ์เป็น artifact ที่ตรวจสอบได้
- ผลลัพธ์สำคัญต้องเก็บลง `/workspace` — temporary state ถือว่า replaceable

### 1.4 ข้อจำกัดที่ Grok Bot ยอมรับตรง ๆ
- Screens แชร์กัน = **ไม่ใช่ security boundary** ระหว่าง Bot
- ไฟล์/login บน computer ใช้ร่วมทุก Bot — อย่าวาง secret ที่ Bot อื่นไม่ควรเห็น
- Bot-to-bot handoff message เป็น text-only

---

## 2. สถาปัตยกรรม PrivateClaw ปัจจุบัน — Gap Analysis

### 2.1 สิ่งที่มีอยู่แล้ว (ค้นจากโค้ดจริง)

| ความสามารถ | โค้ดปัจจุบัน | เทียบ Grok Bot |
|---|---|---|
| Agent loop ต่อ session | `AgentLoop` + `AgentRuntime` (multi-tenant, LRU ต่อ user) | ครูดอยู่แล้ว ~60% |
| Subagent (parallel worker) | `SubagentManager` + `SpawnTool` — isolated loop, budget-capped, ได้ text กลับ | ใกล้เคียง "Bot ทำงานขนาน" แต่ **ไม่มี identity/persistence** |
| Schedules | `SchedulerService` + `ScheduleStore` + schedule tool (มี UI Settings → Schedule) | = Routine เวอร์ชันแรก (interval-based, ผูกกับ session `channel="schedule"`) |
| Skills | `SkillStore` + `read_skill`/`manage_skill` + builtin skills + ต่อ connector_id | = Skills มีอยู่จริง ขาดข้าม-Bot scoping + `/` composer + Teach |
| Sandbox | `EphemeralSandbox` (docker, bridge network, 120s timeout) | = cloud computer แบบ ephemeral — **ยังไม่ persistent** |
| Browser | `BrowserManager` (Playwright) — disabled ตาม config ปัจจุบัน | มีโครง แต่ไม่มี persistent profile/logins |
| Memory | `MemoryService` consolidation + per-user core memory | = durable memory มีอยู่ (ระดับ user ไม่ใช่ระดับ Bot) |
| Connectors/MCP | MCP (stdio+http, SSRF-guarded) + REST api-kind + presets | = Plugins/Connectors ครบระดับเดียวกัน |
| Approvals | `UNSAFE_TOOLS` (exec/workflow/spawn) + confirm round-trip 600s + mode Ask/Auto | = รากฐาน approval มีแล้ว ขาด "Always allow rule" + Auto Review |
| Channels | web / telegram / schedule / heartbeat (Session.channel) | = หลาย channel แล้ว ไม่มี iOS แต่ mobile web ใช้ได้ |
| Groups (users) | user_groups — เป็น plan/policy ไม่ใช่ group chat ของ Bots | ไม่มี Bot group chat |

### 2.2 Gap สำคัญ (สิ่งที่ต้องสร้างใหม่)

1. **ไม่มี Bot entity** — ตอนนี้ "agent" ของ PrivateClaw คือ session เดียวที่มี system prompt + model + tools; ไม่มี named teammate ที่มี job/description/avatar/persistence แยกจาก session
2. **Subagent ไม่มี state** — spawn แล้วหาย (text-only return) ไม่มี memory/ชื่อ/บทสนทนาต่อเนื่อง
3. **Sandbox ไม่ต่อเนื่อง** — exec ใหม่ทุกครั้ง; ไม่มี "computer" ที่ logins/browser state อยู่ข้าม task
4. **ไม่มี Bot-to-Bot messaging** — spawn เป็น parent→child one-shot เท่านั้น ไม่มี @mention/group/handoff
5. **ไม่มี Teach-by-demonstration** — browser tool ยังไม่ผูกกับ recording → skill
6. **Approval rule ไม่มี "Always allow"** — มีแค่ถามทุกครั้ง (Ask) หรือไม่ถามเลย (Auto)
7. **Routine ไม่มี event trigger / test run UI** — มีแต่ interval

---

## 3. โฉมงาน Clawbot — Feature Definition

### 3.1 MVP (Phase 1): Named Bots + durable context

**Goal**: ผู้ใช้สร้าง Bot ตั้งชื่อ/บทบาทได้ แชทต่อเนื่อง และ Bot จำบริบท+ไฟล์ของตัวเองข้าม session

**Backend**
1. `Bot` entity — `bots` table:
   - `id, user_id (owner), name, title, description (กฎถาวร), avatar, enabled, pinned`
   - `model, tools_enabled (json), connector_ids (json), skill_ids (json)`
   - `memory` (text — core memory ของ Bot, เรียกใช้กลไก consolidation เดิมต่อยอด)
   - constraint: ≤50 Bots/user
2. `BotStore` (stores.py) + routes `/api/my/bots` (CRUD + duplicate + share-link token)
3. `Session.bot_id` FK — session ที่ผูก Bot จะ:
   - inject Bot profile + Bot memory เข้า system prompt (ต่อท้าย skills summary เดิม)
   - บังคับ tool set/connector ตามที่ Bot เปิด
4. Bot memory consolidation — ต่อยอด `MemoryService`: consolidate เฉพาะ messages ของ sessions ที่ผูก Bot นั้น → เขียนลง `bots.memory`

**Frontend**
5. Sidebar section "Bots" (แยกจาก chats) + modal Create/Edit Profile (name/title/description/avatar/emoji — สไตล์เดียวกับ pixel icon ที่ระบบมี)
6. New chat flow: เลือก "คุยกับ Bot" ได้
7. Pin/Hide/Duplicate (copy profile ไม่รวมประวัติ) / Share (link + import)

**Estimate**: backend 4-6 วัน, frontend 3-4 วัน (รวม test)

### 3.2 Phase 2: Clawbot Computer — persistent workspace ต่อ owner

**Goal**: ไฟล์/browser state/logins อยู่ข้าม turn ข้าม Bot ภายใน user เดียวกัน (ตามแบบ Grok "one computer per account")

1. **Persistent workspace pool** — เปลี่ยนจาก ephemeral exec เป็น long-lived container ต่อ user:
   - `claw/persistent_computer.py`: docker container ต่อ user (restart policy, idle TTL 30 นาที แล้ว hibernate แต่ volume คงอยู่)
   - volume `claw_pc_<user_id>` mount ที่ `/workspace` (ตอนนี้ใช้ host dir อยู่แล้ว — เพิ่ม browser profile + home dir state)
   - browser: เปิด Playwright persistent context (user-data-dir ใน volume) → logins อยู่จริง
2. **Computer view ใน UI**: streaming screenshot (ซ้ำ execution panel เดิม) + ปุ่ม "Take over" (noVNC หรือ input bridge) สำหรับ captcha/2FA
3. **Security note** (เหมือนที่ Grok ยอมรับ): คอมพิวเตอร์แชร์ระหว่าง Bots ของ user เดียว — เอกสารบอกผู้ใช้ชัดว่าไม่ใช่ security boundary ระหว่าง Bots

**Estimate**: 5-8 วัน (ความเสี่ยง: resource ต่อ user บน mini/M4)

### 3.3 Phase 3: Collaboration — @mention + group chat + handoff

1. **Bot-to-Bot async message**: `bot_messages` table + `send_to_bot` tool — Bot A ส่ง context ให้ Bot B → B ตื่นมาทำงาน (spawn ด้วย profile ของ B ไม่ใช่ blank subagent) แล้วตอบกลับใน thread
2. **Group chat** (2-6 Bots): session แบบ multi-participant — router แรกใช้ model เลือกผู้ตอบ หรือ `@mention` force
3. **UI**: group create, @autocomplete (`@` Bots, `/` skills), thread/reply, handoff แสดงใน transcript เป็น event ใหม่ `bot_handoff`
4. กติกากันงานซ้ำ: ผู้รับ = single owner per stage (เหมือนคำแนะนำ Grok)

**Estimate**: 5-7 วัน

### 3.4 Phase 4: Skills ข้าม-Bot + Routines แบบ Grok + Teach

1. Skill scoping: `skill.visibility = private|user|bot_list` — ตอนนี้ skills ผูก user อยู่แล้ว เพิ่ม enable-per-bot + `/` composer autocomplete
2. Routine v2:
   - เพิ่ม `trigger: cron|event` (event เริ่มจาก Telegram message / webhook)
   - Test run button + run history UI (มี sessions อยู่แล้ว — เพิ่มสถานะ success/fail + error detail)
   - ผูก routine กับ Bot (ตอนนี้ผูกกับ schedule session)
3. **Teach by demonstration** (ท้ายสุด — งานหนักสุด):
   - ใช้ Playwright ที่มีอยู่: record user actions ใน computer view → generate draft skill (action list + selectors) → ผู้ใช้แก้กฎ/เพิ่ม approval → บันทึกเป็น skill
   - อาจพิจารณา browser-use / stagehand library ช่วย generalize

**Estimate**: skills/routines 4-5 วัน, teach 8-12 วัน (ทำเป็น phase แยก)

### 3.5 Phase 5: Approvals v2 — Always-allow rules + Auto Review

1. `approval_rules` table: `user_id, bot_id?, tool, path/host/scope pattern, effect (allow|require), created_at`
2. Confirm dialog เพิ่มปุ่ม **Always allow (save rule)** — เก็บ rule แคบ ๆ (tool+scope)
3. Auto Review (จุดเดียวกับ PolicyEngine ปัจจุบัน): เพิ่ม rule matching ก่อนเรียก model-review (model-based review เป็น opt-in เพราะ cost)
4. Audit: ทุก rule ที่ match ถูก log (มี audit log store อยู่แล้ว)

**Estimate**: 3-4 วัน

---

## 4. สิ่งที่**ไม่ทำ** (และเหตุผล)

| ไม่ทำ | เหตุผล |
|---|---|
| iOS native app | mobile web ของ PrivateClaw responsive อยู่แล้ว; คุ้มค่าไม่คุ้มกับขนาดทีม |
| Public bot-share marketplace | ความเสี่ยง third-party config + moderation — ทำ share-link แบบ private ก่อน |
| แยก VM ต่อ Bot | ต้นทุนสูงเกิน; Grok เองใช้ computer แชร์ + screen ต่อ Bot — เราทำตามแนวนี้ |
| Event trigger ทุก integration พร้อมกัน | เริ่มจาก Telegram (channel ที่มีอยู่) — ลด surface area |

## 5. ลำดับงานแนะนำ (risk-ordered, ตามสไตล์ที่คุณชอบ)

| ขั้น | งาน | เหตุผลลำดับ |
|---|---|---|
| 1 | Phase 1 MVP (Bot entity + session binding + profile UI) | ความเสี่ยงต่ำสุด — ต่อยอด stores/sessions ที่มี; ให้ value ทันที |
| 2 | Phase 2 Computer persistent | แก้ "one computer" ที่เป็นหัวใจ Grok แต่ต้องวัด resource บน M4 ก่อน |
| 3 | Phase 5 Approvals v2 | ก่อนปล่อย Bot ทำงานอัตโนมัติจริง — ความปลอดภัยต้องมาก่อน collaboration |
| 4 | Phase 3 Collaboration | หลัง Bot มีตัวตน+สิทธิ์ชัด จึงปลอดภัยที่จะให้คุยข้ามกัน |
| 5 | Phase 4 Skills/Routines/Teach | Teach หนักสุด ทำท้าย เมื่อ computer+skills เสถียร |

## 6. ตัวชี้วัดความสำเร็จ (MVP)

- สร้าง Bot "นักวิเคราะห์น้ำ" ผูก water-monitor connector + water-report skill → ถาม "สถานการณ์น้ำน่านวันนี้" ในแชทของ Bot → ได้รายงานเดิมทุกประการ โดยไม่ต้องพิมพ์บริบทซ้ำ
- Bot จำ "ผู้ใช้ชอบรายงานสรุปสั้น + แผนที่จริง" ข้ามวัน (จาก bot memory)
- Duplicate Bot เป็น "นักวิเคราะห์น้ำ-ภาคเหนือ" แก้ description นิดเดียวใช้งานได้ทันที

## 7. ความเสี่ยงหลัก

1. **Resource บน M4/mini** — persistent container ต่อ user กิน RAM; ต้อง hibernate + วัดจริง (Phase 2)
2. **Model cost** — Bot memory consolidation เพิ่ม LLM calls; ควรทำ config `memory.model` แยก (ปัญหา 402 เดิม) ก่อน Phase 1
3. **Security ของ share-link** — ต้อง redact connector env/secrets ออกจาก export ให้เด็ดขาด
4. **Scope creep** — Teach-by-demo อาจกินเวลา 2 เท่าของประมาณการ; ตัดออกได้ไม่กระทบ MVP
