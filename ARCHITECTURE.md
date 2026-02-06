# Universal Quest/Event Management Platform — Architecture

## 1. Обзор компонентов

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           EXTERNAL CLIENTS                                   │
├──────────────────────┬──────────────────────┬──────────────────────────────┤
│  Telegram Mini App   │  Station Host (PWA)  │  Admin Dashboard (Desktop)   │
│  (Player UI)         │  (Mobile-first)      │  (Live Ops Board)            │
└──────────┬───────────┴──────────┬───────────┴──────────────┬───────────────┘
           │                      │                          │
           │   initData / JWT     │   initData / JWT         │   Session/JWT
           ▼                      ▼                          ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         FastAPI BACKEND                                      │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐ │
│  │ Auth        │  │ API Routes  │  │ WebSocket   │  │ Jinja2 Templates    │ │
│  │ (initData,  │  │ (REST-like) │  │ Hub         │  │ (SSR + HTMX)        │ │
│  │  JWT)       │  │             │  │             │  │                     │ │
│  └─────────────┘  └─────────────┘  └─────────────┘  └─────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────────┘
           │                      │                          │
           ▼                      ▼                          ▼
┌──────────────────────┬──────────────────────┬──────────────────────────────┐
│  PostgreSQL/SQLite   │  Redis (optional)    │  Telegram Bot (aiogram)      │
│  (SQLAlchemy ORM)    │  pub/sub, scheduling │  onboarding, notifications   │
└──────────────────────┴──────────────────────┴──────────────────────────────┘
```

### Ответственность компонентов

| Компонент | Ответственность |
|-----------|-----------------|
| **FastAPI Backend** | REST API, WebSocket, SSR, auth, бизнес-логика |
| **Jinja2 + HTMX** | Рендеринг всех UI, динамические обновления без SPA |
| **Telegram Bot** | Регистрация, deep links, уведомления |
| **WebSocket Hub** | Real-time push для teams, hosts, admin |
| **PostgreSQL** | Хранение событий, команд, контента, логов |

---

## 2. Модель данных (минимальная, multi-event)

### Основные сущности

```
events
  id, name, slug, starts_at, ends_at, config JSON, created_at

teams
  id, event_id, name, status, score_total, current_state, current_station_id,
  team_progress JSON, qr_token (signed), created_at
  current_state: free_roam | assigned | in_visit | finished

players
  id, event_id, tg_id, team_id, role, player_progress JSON, flags JSON,
  created_at
  role: ROLE_A | ROLE_B

stations
  id, event_id, name, capacity, config JSON, created_at

station_hosts
  id, event_id, tg_id, station_id, created_at

station_visits
  id, event_id, team_id, station_id, state, started_at, ended_at,
  points_awarded, host_notes, host_rating, created_at
  state: arrived | started | finished

content_blocks
  id, event_id, key, type, payload JSON, audience, station_id nullable,
  version, created_at
  audience: TEAM | ROLE_A | ROLE_B | PLAYER

dialogue_threads
  id, event_id, key, type, title, config JSON, created_at
  type: LEAKED | INTERACTIVE

dialogue_messages
  id, event_id, thread_id, audience, payload JSON, order_index,
  gate_rules JSON (keywords, flags), created_at

deliveries
  id, event_id, content_block_id, team_id, player_id nullable,
  delivered_at, seen_at
  UNIQUE(content_block_id, team_id, player_id) — idempotency

ratings
  id, event_id, station_visit_id, player_id, station_rating, host_rating,
  comment, created_at

event_log
  id, event_id, team_id, player_id nullable, event_type, data JSON, created_at

users (для связи tg_id → роль в контексте event)
  tg_id, event_id, role, station_id nullable
  role: PLAYER | STATION_HOST | ADMIN | SUPERADMIN
```

### Ключевые ограничения

- `teams.event_id` + `teams.name` UNIQUE
- `players.event_id` + `players.tg_id` UNIQUE (игрок в событии один раз)
- `station_hosts.event_id` + `station_hosts.tg_id` UNIQUE
- `deliveries`: UNIQUE(content_block_id, COALESCE(team_id,0), COALESCE(player_id,0))

---

## 3. Ключевые потоки

### 3.1 Onboarding
1. Пользователь открывает бота или deep link.
2. Бот присылает кнопку «Открыть игру» (Telegram WebApp URL).
3. WebApp получает `initData`, отправляет на `/auth/verify`, бекенд проверяет HMAC.
4. Бекенд создаёт JWT/session, возвращает cookie.
5. По tg_id определяется роль (player/host/admin) для текущего event.

### 3.2 Team assignment (organizer)
1. Admin создаёт команды, добавляет игроков в слоты A/B (по tg_id или username).
2. При добавлении: создаётся player, привязывается к team с role.
3. Real-time: WebSocket broadcast в admin board и player clients.

### 3.3 Roaming / Assigned
1. Admin переводит team в `assigned` и задаёт `current_station_id`, или в `free_roam` (current_station_id = null).
2. Player WebApp через WebSocket получает обновление статуса, HTMX swap обновляет «where to go».

### 3.4 Station scanning
1. Host открывает Station Host UI, нажимает «Scan QR».
2. JS запрашивает камеру, считывает QR (team token).
3. POST `/station/scan` с token → backend валидирует, возвращает team info.
4. Host может start/finish visit, начислять очки, ставить оценку, notes.
5. WebSocket уведомляет team и admin о завершении визита.

### 3.5 Content delivery
1. Admin в Content Control выбирает content_block, audience (TEAM/ROLE_A/ROLE_B), team/player.
2. Deliver now или schedule (если Redis: Celery/APScheduler).
3. Backend создаёт delivery record, шлёт WebSocket event.
4. Player client получает event, HTMX swap или redirect на новый контент.

### 3.6 Dialogues (leaked / interactive)
1. **Leaked**: Player видит thread как историю чата, сообщения фильтруются по audience (TEAM/ROLE_A/ROLE_B).
2. **Interactive**: Player отправляет сообщение или выбор → backend по gate_rules определяет ответ, создаёт delivery, шлёт ответ.

### 3.7 Ratings
1. После `station_visit.state = finished` player видит форму рейтинга (station, host, comment).
2. POST `/rating` → сохраняется, WebSocket уведомляет admin analytics.

---

## 4. Endpoint list и WebSocket events

### Auth
| Method | Path | Описание |
|--------|------|----------|
| POST | `/auth/verify` | initData → JWT/session |
| GET | `/auth/me` | Текущий пользователь и роль |
| POST | `/auth/logout` | Logout |

### Player
| Method | Path | Описание |
|--------|------|----------|
| GET | `/player` | Player dashboard (team state, content feed) |
| GET | `/player/dialogues` | Список dialogue threads |
| GET | `/player/dialogues/{key}` | Диалог (leaked или interactive) |
| POST | `/player/dialogues/{key}/reply` | Отправка ответа (interactive) |
| GET | `/player/content/{id}` | Конкретный content block |
| POST | `/player/rating` | Оценка после визита |

### Station Host
| Method | Path | Описание |
|--------|------|----------|
| GET | `/station` | Station Host UI (scan, team info, actions) |
| POST | `/station/scan` | Валидация QR token, возврат team |
| POST | `/station/visit/start` | Начать визит |
| POST | `/station/visit/finish` | Завершить визит, points, notes, rating |

### Admin
| Method | Path | Описание |
|--------|------|----------|
| GET | `/admin` | Live Ops Board |
| GET | `/admin/teams` | Список команд (partial for HTMX) |
| POST | `/admin/teams` | Создать команду |
| PATCH | `/admin/teams/{id}` | Обновить команду (assign station, roaming) |
| POST | `/admin/teams/{id}/players` | Добавить игрока в слот A/B |
| GET | `/admin/stations` | Станции и capacity |
| GET | `/admin/content` | Content blocks list |
| GET | `/admin/content/{id}` | Редактор content block |
| POST | `/admin/content` | Создать content block |
| PATCH | `/admin/content/{id}` | Обновить и publish |
| POST | `/admin/deliver` | Доставить контент (now/scheduled) |
| GET | `/admin/dialogues` | Dialogue threads |
| GET | `/admin/dialogues/{id}` | Редактор диалога |
| POST | `/admin/dialogues/{id}/messages` | Добавить сообщение |
| GET | `/admin/analytics` | Рейтинги, leaderboard, audit |
| GET | `/admin/log` | Event log stream (filter) |

### WebSocket events (типы)

| Event | Направление | Payload |
|-------|-------------|---------|
| `team:state` | Server→Client | team_id, state, station_id |
| `content:delivered` | Server→Client | team_id, player_id?, content_block_id |
| `visit:started` | Server→Client | visit_id, team_id, station_id |
| `visit:finished` | Server→Client | visit_id, points, ratings |
| `admin:team_update` | Server→Admin | team_id, full team data |
| `admin:visit_update` | Server→Admin | visit_id |
| `admin:log_entry` | Server→Admin | log entry |

### WebSocket subscribe channels
- `event:{event_id}` — все обновления по событию
- `team:{team_id}` — обновления команды (для игроков)
- `station:{station_id}` — обновления станции (для хоста)
- `admin:{event_id}` — обновления для админки

---

## 5. UI Page map и HTMX interactions

### Player WebApp (Telegram Mini App)
| Страница | HTMX swap targets | WebSocket → action |
|----------|-------------------|---------------------|
| `/player` | `#team-state`, `#content-feed`, `#dialogue-list` | team:state → swap team-state; content:delivered → swap content-feed |
| `/player/dialogues/{key}` | `#dialogue-messages`, `#reply-form` | content:delivered → append message |
| `/player/rating` | `#rating-form` | visit:finished → show rating form |

### Station Host UI
| Страница | HTMX swap targets | Действия |
|----------|-------------------|----------|
| `/station` | `#scanned-team`, `#visit-actions`, `#visit-form` | Scan QR → POST scan → swap scanned-team; Start → swap visit-actions; Finish → swap form, reset |

### Admin Dashboard
| Страница | HTMX swap targets | WebSocket → action |
|----------|-------------------|---------------------|
| `/admin` | `#board-free`, `#board-assigned`, `#board-visit`, `#board-finished` | admin:team_update → swap соответствующей колонки |
| `/admin/content` | `#content-list` | — |
| `/admin/content/{id}` | `#content-editor`, `#preview` | Publish → swap preview |
| `/admin/dialogues/{id}` | `#message-list`, `#message-editor` | — |
| `/admin/analytics` | `#ratings`, `#leaderboard` | Polling каждые 30s или WebSocket |
| `/admin/log` | `#log-stream` | admin:log_entry → append |

### HTMX patterns
- `hx-get` + `hx-trigger="every 5s"` — fallback polling когда WebSocket недоступен
- `hx-swap="outerHTML"` для замены блоков
- `hx-post` + `HX-Request: true` — backend возвращает partial (fragment) для swap
- `HX-Trigger` в response headers — для каскадных обновлений на клиенте

---

## 6. Security: Telegram initData и роли

### Проверка initData
1. Получить `initData` из `window.Telegram.WebApp.initData`.
2. Парсинг query string, извлечь `hash`.
3. Создать data-check-string: отсортировать пары key=value (кроме hash), объединить через `\n`.
4. HMAC-SHA256(data-check-string, base64_decode(BOT_TOKEN)) → сравнить с hash.
5. Проверить `auth_date` не старше 24h.

### Роли
- **PLAYER**: доступ к `/player/*`, WebSocket channel team
- **STATION_HOST**: доступ к `/station/*`, channel station
- **ADMIN**: доступ к `/admin/*`, channel admin
- **SUPERADMIN**: + управление events, пользователями

### Middleware
- Каждый защищённый endpoint проверяет JWT/session и роль.
- `get_current_user()` → UserContext(event_id, tg_id, role, team_id?, station_id?).
