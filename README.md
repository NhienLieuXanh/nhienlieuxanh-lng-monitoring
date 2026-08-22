# Giám sát bồn LNG — GAS Nhiên Liệu Xanh

Kéo telemetry bồn LNG từ cổng vendor, chuẩn hoá, lưu PostgreSQL của công ty, rồi phục
vụ qua API nội bộ + dashboard. Trên nền đó có thêm lớp **dự báo tiêu thụ** (ngày tới
cạn, bay hơi tự nhiên, thời gian giữ áp), **kế hoạch nạp**, **cảnh báo qua email** và
**xuất báo cáo**. Toàn bộ logic vendor cô lập sau một Adapter.

Đang chạy: <https://nhienlieuxanh-lng-monitoring.vercel.app/ui/>

## Hệ thống chạy ở đâu

| Thành phần | Nơi chạy |
|---|---|
| API + dashboard | Vercel (serverless function `app/main.py`) |
| Database | Neon PostgreSQL |
| Thu thập telemetry | Vercel Cron gọi `GET /api/cron/ingest` (Bearer `CRON_SECRET`) |
| Cảnh báo email | Chạy ngay trong vòng cron, sau khi thu thập xong |

APScheduler **không** chạy trên serverless (không có process nền), nên trên Vercel đặt
`SCHEDULER_ENABLED=false` và để Cron làm nhịp. Scheduler chỉ dùng khi chạy máy chủ
thường.

## Đăng nhập

Dashboard yêu cầu đăng nhập bằng **chính tài khoản cổng telemetry**; app không lưu
password, chỉ giữ session cookie (`nlx_session`, HttpOnly, `Secure` ngoài dev). Mọi
endpoint dữ liệu đều sau `UserDep`. `/api/health` để mở cho monitor.

`/api/admin/*` guard riêng bằng header `X-Admin-Token`. **Token này không được nhúng
vào JS dashboard** — dashboard dùng session cookie, không dùng admin token.

## Chức năng

**Giám sát.** Danh sách bồn với mức chứa, thời gian tới mức dự trữ, trạng thái, lần đo
cuối. Panel chi tiết: mức chứa / thể tích / áp suất / nhiệt độ, cảnh báo của bồn, biểu
đồ xu hướng, thông số thiết bị (sửa được tên bồn).

**Dự báo tiêu thụ & an toàn** (`app/domain/forecast.py`, hàm thuần, có test riêng):

- **Mức tiêu thụ/ngày suy từ lịch sử**, không gõ tay. Cộng toàn bộ phần mức đi xuống,
  chia cho **thời gian thực sự có dữ liệu**; loại bước nạp, loại nhiễu dưới deadband,
  loại khoảng mất kết nối khỏi cả tử số lẫn mẫu số.
- **Bay hơi tự nhiên** và **tốc độ tăng áp**: hồi quy trên các chu kỳ nghỉ ≥ 6 giờ, lấy
  trung vị. Bay hơi (~5 L/ngày) nằm *dưới* nhiễu cảm biến nên hiệu số từng cặp là vô
  nghĩa — bắt buộc phải hồi quy. Không đo được thì trả giá trị tham chiếu 0.05 %/ngày
  **kèm nhãn `reference`** để hằng số không bị đọc thành phép đo.
- **Thời gian giữ áp** = (áp van an toàn − áp hiện tại) / tốc độ tăng áp. Áp không tăng
  thì trả `null` ("chưa đủ dữ liệu"), **không phải vô cực**.
- **Ngày tới cạn** theo *tổng* thất thoát = tiêu thụ + bay hơi.
- **Khuyến nghị đặt hàng** = điểm đặt hàng lại có dự trữ an toàn thống kê
  (z × độ lệch chuẩn × √thời gian giao hàng), kèm danh sách `reasons` để mỗi con số
  truy được về đầu vào.

**Kế hoạch nạp.** Lập lịch theo tháng cho một bồn thật (tự lấy dung tích + thể tích
hiện tại), có nút áp dụng mức tiêu thụ đo được, cột ngày nghỉ / nạp chỉ định, và giờ
nạp tới từng giây.

**Báo cáo.** Ba file CSV (tổng hợp bồn + dự báo, nhật ký nạp, dữ liệu đo chi tiết),
UTF-8 có BOM và mốc thời gian giờ Việt Nam để Excel mở không lỗi phông. Nhật ký nạp
được **suy từ telemetry**, không ai nhập tay. Lịch giao gom bồn thành chuyến theo sức
chứa xe.

**Cảnh báo.** Sáu mã được gửi ra ngoài: `RUNOUT`, `HOLD_TIME`, `LOW_VOLUME`, `OFFLINE`,
`BOIL_OFF_HIGH`, `LOW_BATTERY`. Một email cho cả vòng (không phải một email mỗi cảnh
báo), cửa chặn nhắc lại theo `(bồn, mã)` lưu ở DB, và **lỗi SMTP không bao giờ làm vòng
thu thập thất bại**.

**Cài đặt trong app.** Người nhận cảnh báo, máy chủ thư, thông số bồn, chính sách đặt
hàng, ngưỡng cảnh báo — sửa được trên giao diện, có hiệu lực ngay, không cần deploy
lại. Giá trị đặt trong app **thắng** `.env`; mỗi ô ghi rõ nó đang là *Tuỳ chỉnh* hay
*Mặc định*. Danh sách field được phép sửa là whitelist `appconfig.OVERRIDABLE` —
`SESSION_SECRET`, `ADMIN_TOKEN`, credential vendor **không** sửa được qua web.

## Kiến trúc

```
domain/contracts.py   ← TelemetryPort + Normalized* models (cột sống)
        ▲                              ▲
adapters/xingke/            services · api · repositories · db
```

`app/factory.py` là **file duy nhất** biết adapter cụ thể nào đang dùng.
`tests/test_isolation.py` thi hành luật đó bằng máy, không bằng kỷ luật: nó allowlist
mọi JSON key trên mọi endpoint và assert không có `raw_payload`, không có tên vendor,
không có codepoint CJK trong response.

`app/services/appconfig.py` gộp `.env` với cấu hình người vận hành đặt trong app.
Tầng gọi (notifier, router dự báo, export) **không cần biết** một giá trị đến từ đâu.

## Cấu hình

Copy `env.example` thành `.env` rồi điền. Hai nhóm cần phân biệt:

- **Bí mật và hạ tầng** (`DATABASE_URL`, `SESSION_SECRET`, `ADMIN_TOKEN`, `CRON_SECRET`,
  credential vendor) — chỉ đặt bằng biến môi trường.
- **Thông số vận hành** (`FORECAST_*`, `LNG_*`, `SMTP_*`, `ALERT_*`, `TRUCK_CAPACITY_L`,
  ngưỡng cảnh báo) — đặt được trong app; `.env` chỉ là giá trị khởi tạo.

## Chạy migration lên Neon

Máy trong mạng công ty mở được TCP 5432 tới Neon nhưng **handshake wire protocol bị
firewall reset**, nên `alembic upgrade head` không chạy được từ local. Đường chính
thức là gọi từ chính Vercel:

```bash
curl -X POST -H "X-Admin-Token: <ADMIN_TOKEN production>" \
  https://nhienlieuxanh-lng-monitoring.vercel.app/api/admin/db/sync
```

Endpoint này **chỉ tiến, không có downgrade**. Nếu `alembic.ini` + `migrations/` có
trong bundle (hiện có) nó chạy `alembic upgrade head` thật; nếu không, nó rơi về
`create_all(checkfirst=True)` — đủ cho migration thêm bảng, **không đủ** cho
`ALTER COLUMN`. Khi cần ALTER, phải chạy alembic từ một mạng nói được Postgres tới Neon.

`/api/health` so `alembic_version` với head mà code mong đợi, nên quên migrate sẽ hiện
ra là `degraded` chứ không phải `UndefinedColumn` lúc 3 giờ sáng.

## Lệnh CLI

Chạy được khi máy có đường tới database. **Luôn gọi `.\.venv\Scripts\python.exe` tường
minh**, không `Activate.ps1`: activate bị execution policy mặc định chặn, và global
site-packages của máy này CÓ sqlalchemy/alembic nhưng SAI version —
`app.config.assert_venv()` sẽ từ chối chạy ngoài venv vì lý do đó.

```powershell
.\.venv\Scripts\python.exe -m app.cli check-db      # kết nối + trạng thái migration
.\.venv\Scripts\python.exe -m app.cli status        # bảng trạng thái các bồn
.\.venv\Scripts\python.exe -m app.cli probe         # kiểm auth + mapping vendor thật
.\.venv\Scripts\python.exe -m app.cli discover      # làm mới metadata thiết bị
.\.venv\Scripts\python.exe -m app.cli run-once      # một vòng thu thập
.\.venv\Scripts\python.exe -m app.cli backfill --psn 2604200016 --from 2026-07-01 --to 2026-07-31
.\.venv\Scripts\python.exe -m app.cli set-terminal 2604200016 --name "Bồn A - Kho Long An"
.\.venv\Scripts\python.exe -m app.cli serve         # máy chủ thường + mở dashboard
.\.venv\Scripts\python.exe scripts\verify_tz.py     # BẮT BUỘC pass trước khi backfill
```

Backfill bị ngắt thì chỉ cần **chạy lại đúng command**: upsert idempotent nên resume
miễn phí, không cần checkpoint table.

`scripts/verify_tz.py` đối chiếu timestamp naive của vendor với UTC tại chỗ. **Không backfill
khi script này fail** — sai timezone nhân đôi toàn bộ lịch sử vì khoá
`(psn, sampled_at)` đổi.

## Test

```powershell
.\.venv\Scripts\python.exe -m pytest                # phần thuần, không cần DB
$env:TEST_DATABASE_URL = "postgresql+psycopg://xingke_app:<DB_PASSWORD>@localhost:5432/xingke_test"
.\.venv\Scripts\python.exe -m pytest                # kèm test cần DB
```

Test DB dùng **PostgreSQL thật, không SQLite**: tầng data phụ thuộc JSONB,
`ON CONFLICT`, `DISTINCT ON`, `gen_random_uuid()`, composite FK, `xmax`, và semantics
`timestamptz` — mỗi thứ đó hoặc fail hoặc hành xử khác trên SQLite, nên một suite
SQLite sẽ test một chuyện hư cấu.

Lớp dự báo và lớp cấu hình test được **không cần DB**: chúng là hàm thuần nhận `now`
làm tham số, nên không phải mock clock. `tests/test_appconfig.py` còn chạy router Cài
đặt với một session giả — cách đó từng bắt được lỗi "đổi tên hàm mà quên chỗ gọi" mà
ruff không thấy và test API thì bị skip khi máy không có Postgres.

## Những điều BẤT NGỜ cần biết trước khi sửa code

### `volume_percent` là thang 0–100, không phải 0–1

Vendor tính `currentVolume / cylinderVolume * 100`. Nên `0.59` nghĩa là **0.59 % đầy** —
61 L trong bồn 10 425 L, tức **gần cạn**.

Không CHECK constraint nào bắt được lỗi hiểu sai thang này vì `0.59` hợp lệ ở cả hai.
Vì vậy API phát kèm `fill_percent` do server tự tính, và cảnh báo `PERCENT_MISMATCH`
nổ khi hai số lệch > 5 điểm.

### `duplicates` rất lớn là hoạt động ĐÚNG

Endpoint vendor trả theo **NGÀY**, không có range. Mỗi vòng thu thập refetch lại cả
ngày: ~48 dòng × nhiều lần/ngày, chỉ ~48 dòng được insert. Thấy
`duplicates=6800 inserted=48` là bình thường — **đừng "tối ưu" nó đi**, đó chính là cơ
chế idempotency.

### Timestamp vendor là naive và render ở UTC+8

Vendor gửi `"2026-07-23 16:03:29"` không có offset, giờ **Asia/Shanghai** — tức 15:03
giờ Việt Nam. Sai timezone ở đây **không phải lỗi hiển thị, nó làm hỏng khoá dedup**
`(psn, sampled_at)`: sửa parsing về sau thì mọi dòng có khoá khác, `ON CONFLICT` không
match, và toàn bộ lịch sử bị nhân đôi âm thầm. Vì vậy `XINGKE_VENDOR_TZ` là setting
(sửa `.env`, không sửa code), và cột `vendor_ts_raw` giữ string gốc để re-derive được
bằng SQL thuần.

### Endpoint vendor rò dữ liệu khách hàng khác

`device/list` không filter trả về **3543 thiết bị của mọi khách hàng** — account có org
scope nhưng endpoint bỏ qua. Và `?psn=` bị **bỏ qua im lặng** (phải dùng `searchParam`).

Vì vậy `XINGKE_ALLOWED_PSNS` là **bắt buộc** và được thi hành ở ranh giới adapter.
Không bao giờ "thu thập hết rồi filter sau".

### Không dự báo từ dữ liệu đã lỗi thời

Lần đo cũ hơn `FORECAST_MAX_READING_AGE_HOURS` (24 giờ) thì dự báo bị đánh dấu `stale`
và **cảnh báo `RUNOUT` / `HOLD_TIME` bị chặn**, bồn đó cũng bị loại khỏi lịch giao. Lý
do: cả hai suy từ mức và áp *hiện tại*, mà "hiện tại" ở đây có thể là số của tháng
trước — bồn có thể đã được nạp tay từ lâu. Sự thật duy nhất ta biết là **mất kết nối**,
và đã có cảnh báo `OFFLINE` lo việc đó.

`BOIL_OFF_HIGH` **không** bị chặn: nó là kết luận về tình trạng cách nhiệt của bồn, suy
từ các chu kỳ nghỉ trong lịch sử, không phải về mức hiện tại.

### Cả hai thiết bị thật đang mất kết nối

Ngoại tuyến hàng tháng, `battery_v` 3.6 V, `signal_percent` 15–20 %, bồn gần cạn. Hệ
quả đã được thiết kế quanh, không phải phát hiện muộn:

- **0 dòng trả về KHÔNG phải lỗi** — nó vào `psns_no_data`, không vào `errors`.
- Health đọc từ `ingest_runs`, không suy từ `MAX(telemetry.created_at)` — suy từ
  telemetry thì health đỏ vĩnh viễn và không ai còn tin nó.
- Dự báo trả `stale` cho cả hai bồn, và giao diện nói thẳng điều đó thay vì hiện một
  con số trông như đang sống.

Ngoài phạm vi phần mềm: **phát hiện đầu tiên của platform này là hai thiết bị pilot cần
thay pin và ăng-ten** trước khi thêm bao nhiêu code cũng vô ích.

## Giao diện — quy ước khi sửa

Dashboard là **một file** `app/static/index.html` (vanilla JS + Chart.js, không build
step). Hai quy ước đã được thi hành và đừng phá:

- **Thang chữ**: 10 token `--fs-*` ở `:root`. Mọi khai báo `font-size` dùng token,
  không có cỡ thô nào trong CSS. KHÔNG dùng shorthand `font:` để đặt cỡ — nó ghi đè
  `font-size` khai báo phía trên và từng làm cỡ nền cả app lệch khỏi thang.
- **Thuật ngữ**: mỗi khái niệm đúng một từ, dùng chung cho giao diện *và* chuỗi
  backend (cảnh báo, email) — mức chứa · thể tích · dung tích · mức tiêu thụ · mức dự
  trữ · lần đo cuối · trực tuyến / ngoại tuyến · dữ liệu lỗi thời · bay hơi tự nhiên ·
  thời gian giữ áp · áp suất van an toàn · thời gian giao hàng · điểm đặt hàng lại ·
  dự trữ an toàn · mức phục vụ · sức chứa xe · ngưỡng.

Container cuộn theo cột dọc (`.inspect`, `.plan-scroll`, `.rep-scroll`, `.set-scroll`)
phải có `> * { flex: 0 0 auto }`: không có dòng đó thì con bị **bóp** thay vì để
container cuộn, và một bảng 30 dòng có thể thành một dải 14px mà DOM vẫn đủ nội dung.

## Bảo mật — giới hạn hiện tại

- Xác thực bằng session cookie qua tài khoản cổng telemetry; **một tài khoản dùng
  chung**, chưa có phân quyền theo người.
- Ai đăng nhập được dashboard thì **đổi được nơi cảnh báo gửi tới** và lưu được mật
  khẩu ứng dụng của hộp thư (trang Cài đặt dùng `UserDep`, không phải `AdminDep`). Với
  một tool nội bộ dùng chung một tài khoản, mức tin cậy đó bằng mức "xem được toàn bộ
  số liệu bồn". Khi có nhiều người dùng khác vai thì siết thành admin-only.
- `smtp_password` ghi được nhưng **không bao giờ đọc ra** khỏi API — chỉ có cờ
  đã-thiết-lập.
- `raw_payload` không bao giờ ra khỏi API (`tests/test_isolation.py` thi hành).
- Không commit `.env`. `var/` cũng gitignored — nó chứa output probe chưa redact.

## Tài liệu

- `DISCOVERY.md` — kết quả reverse-engineer API vendor, kèm mức độ đã xác minh của
  từng kết luận và danh sách những gì CÒN CHƯA biết.
- `app/adapters/xingke/mapping.py` — mapping khai báo. **File duy nhất phải sửa** khi
  vendor đổi tên field hoặc đơn vị.
- `app/domain/forecast.py` — toàn bộ phép tính dự báo, mỗi ngưỡng đều có lý do ghi tại
  chỗ (vì sao 3 giờ, vì sao 6 giờ, vì sao 24 giờ).
