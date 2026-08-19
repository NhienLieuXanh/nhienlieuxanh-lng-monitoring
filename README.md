# LNG Tank Monitoring — Platform nội bộ

Kéo telemetry bồn LNG từ cổng vendor, chuẩn hoá, lưu PostgreSQL của công ty, phục vụ
qua API + dashboard nội bộ. Logic vendor cô lập hoàn toàn sau một Adapter.

## Chạy nhanh

Hàng ngày: **double-click `start.cmd`**. Script tự kiểm tra Postgres, migrate, mở
http://127.0.0.1:8000/ui/ . Muốn tự chạy khi đăng nhập Windows:

```bat
start.cmd install-startup
```

Token vendor hết hạn: điền `XINGKE_USERNAME` + `XINGKE_PASSWORD` vào `.env` thì app
tự đăng nhập lại, không cần copy `localStorage.token` mỗi sáng.

Lần đầu (chỉ một lần):

```powershell
# 1. venv + deps (KHÔNG dùng Activate.ps1 — bị execution policy chặn)
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt

# 2. PostgreSQL 17 (cần UAC — chạy trong PowerShell có quyền admin)
winget install --id PostgreSQL.PostgreSQL.17 --interactive

# 3. DB + role
$psql = "C:\Program Files\PostgreSQL\17\bin\psql.exe"
& $psql -U postgres -c "CREATE ROLE xingke_app LOGIN PASSWORD '<DB_PASSWORD>';"
& $psql -U postgres -c "CREATE DATABASE xingke      OWNER xingke_app;"
& $psql -U postgres -c "CREATE DATABASE xingke_test OWNER xingke_app;"

# 4. .env + migration
Copy-Item env.example .env      # rồi điền DB_PASSWORD
.\.venv\Scripts\python.exe -m app.cli check-db
.\.venv\Scripts\python.exe -m alembic upgrade head

# 5. Dữ liệu demo (không cần credential vendor) + chạy
.\.venv\Scripts\python.exe -m app.cli seed-demo --days 3
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000
```

Dashboard: <http://127.0.0.1:8000/ui/> · API docs: <http://127.0.0.1:8000/docs>

**Luôn gọi `.\.venv\Scripts\python.exe` tường minh**, không `Activate.ps1`: activate bị
execution policy mặc định chặn, và global site-packages của máy này CÓ sqlalchemy/alembic
nhưng SAI version — `app.config.assert_venv()` sẽ từ chối chạy ngoài venv vì lý do đó.

## Kiến trúc

```
domain/contracts.py  ← TelemetryPort + Normalized* models (cột sống)
        ▲                         ▲
adapters/{xingke,fake}     services · api · repositories · db
```

`app/factory.py` là **file duy nhất** chọn adapter cụ thể.
`tests/test_isolation.py` thi hành luật đó bằng máy, không bằng kỷ luật.

Đổi `XINGKE_ADAPTER=fake|live` trong `.env` để chuyển giữa fixture và vendor thật.

## Những điều BẤT NGỜ cần biết trước khi sửa code

### `volume_percent` là thang 0–100, không phải 0–1

Vendor tính `currentVolume / cylinderVolume * 100`. Nên `0.59` nghĩa là **0.59% đầy** —
61 L trong bồn 10.425 L, tức **gần cạn**.

Prototype dashboard ban đầu có `Math.round(volume_percent * 100)` → hiển thị **"59%"** với
thanh level đầy hơn nửa. Trên màn hình theo dõi tài sản LNG đây là lỗi nghiêm trọng nhất.

Không CHECK constraint nào bắt được lỗi này vì `0.59` hợp lệ ở cả hai thang. Vì vậy API
phát kèm `fill_percent` do server tự tính, và `PERCENT_MISMATCH` alert nổ khi hai số lệch
> 5 điểm.

### `duplicates` rất lớn là hoạt động ĐÚNG

Endpoint vendor trả theo **NGÀY**, không có range. Nên mỗi vòng poll 10 phút refetch lại
cả ngày: ~48 dòng × ~144 lần/ngày, chỉ ~48 dòng được insert. Thấy
`duplicates=6800 inserted=48` là bình thường — **đừng "tối ưu" nó đi**, đó chính là cơ chế
idempotency.

### Timestamp vendor là naive và render ở UTC+8

Vendor gửi `"2026-07-23 16:03:29"` không có offset, giờ **Asia/Shanghai**. Nghĩa là
15:03 giờ Việt Nam, không phải 16:03. Mock ban đầu ghi `+07:00` và lệch đúng 1 tiếng.

Sai timezone ở đây **không phải lỗi hiển thị — nó làm hỏng khoá dedup**
`(psn, sampled_at)`. Sửa parsing về sau thì mọi dòng có khoá khác, `ON CONFLICT` không
match, và **toàn bộ lịch sử bị nhân đôi âm thầm**. Vì vậy `XINGKE_VENDOR_TZ` là setting
(sửa `.env`, không sửa code), và cột `vendor_ts_raw` giữ string gốc để re-derive được
bằng SQL thuần.

### Endpoint vendor rò dữ liệu khách hàng khác

`device/list` không filter trả về **3543 thiết bị của mọi khách hàng** — account có org
scope nhưng endpoint bỏ qua. Và `?psn=` bị **bỏ qua im lặng** (phải dùng `searchParam`).

Vì vậy `XINGKE_ALLOWED_PSNS` là **bắt buộc** và được thi hành ở ranh giới adapter. Không
bao giờ "ingest hết rồi filter sau".

### Cả hai thiết bị thật đang chết

Offline hàng tháng, `battery_v` 3.6, `signal_percent` 15–20%, bồn gần cạn. Hệ quả đã
được thiết kế quanh, không phải phát hiện muộn:

- **0 dòng trả về KHÔNG phải lỗi** — nó vào `psns_no_data`, không vào `errors`.
- Health đọc từ `ingest_runs`, không suy từ `MAX(telemetry.created_at)` — nếu suy từ
  telemetry thì health đỏ vĩnh viễn và không ai còn tin nó.
- Nhánh "online" chỉ chạy được với `seed-demo --fresh`.

Ngoài phạm vi phần mềm: **phát hiện đầu tiên của platform này là hai thiết bị pilot cần
thay pin và ăng-ten** trước khi thêm bao nhiêu code cũng vô ích.

## Lệnh CLI

```powershell
.\.venv\Scripts\python.exe -m app.cli check-db      # kết nối + trạng thái migration
.\.venv\Scripts\python.exe -m app.cli seed-demo --days 3 [--fresh]
.\.venv\Scripts\python.exe -m app.cli status        # bảng trạng thái các bồn
.\.venv\Scripts\python.exe -m app.cli probe         # kiểm auth + mapping vendor thật
.\.venv\Scripts\python.exe -m app.cli discover      # làm mới metadata thiết bị
.\.venv\Scripts\python.exe -m app.cli run-once      # một vòng ingest
.\.venv\Scripts\python.exe -m app.cli set-terminal 2604200016 --name "Bồn A - Kho Long An"
.\.venv\Scripts\python.exe -m app.cli backfill --psn 2604200016 --from 2026-07-01 --to 2026-07-31
.\.venv\Scripts\python.exe scripts\verify_tz.py     # BẮT BUỘC pass trước khi backfill
```

`seed-demo` chạy qua **đúng đường ingestion thật**, nên nó vừa seed vừa smoke-test upsert.
Chạy hai lần: lần hai phải báo `inserted=0 duplicates=288` — bằng chứng idempotency bằng
một câu lệnh.

Backfill bị ngắt thì chỉ cần **chạy lại đúng command**: upsert idempotent nên resume miễn
phí, không cần checkpoint table.

`verify_tz.py` đối chiếu timestamp naive của vendor với UTC tại chỗ. **Không backfill
khi script này fail** — sai timezone nhân đôi toàn bộ lịch sử vì khoá `(psn, sampled_at)`
đổi.

Đổi tên / dung tích bồn: CLI `set-terminal` hoặc `PATCH /api/terminals/{psn}`. Ingest
không ghi đè hai field này.

## Test

```powershell
.\.venv\Scripts\python.exe -m pytest                       # test thuần, không cần DB
$env:TEST_DATABASE_URL = "postgresql+psycopg://xingke_app:<DB_PASSWORD>@localhost:5432/xingke_test"
.\.venv\Scripts\python.exe -m pytest                       # kèm test cần DB
```

Test DB dùng **PostgreSQL thật, không SQLite**: tầng data phụ thuộc JSONB,
`ON CONFLICT`, `DISTINCT ON`, `gen_random_uuid()`, composite FK, `xmax`, và semantics
`timestamptz` — mỗi thứ đó hoặc fail hoặc hành xử khác trên SQLite, nên một suite SQLite
sẽ test một chuyện hư cấu.

## Bảo mật — giới hạn của giai đoạn 1

- **Không có authentication cho GET.** "Nội bộ" không phải biện pháp bảo mật: bind
  `0.0.0.0` là cả LAN đọc được, qua HTTP trần. Giai đoạn 1 bind `127.0.0.1`.
- `/api/admin/*` bảo vệ bằng `ADMIN_TOKEN`. **Nhúng token này vào JS dashboard KHÔNG
  phải authentication.**
- Auth thật (SSO / session cookie) + TLS là **tiền đề của giai đoạn 2** trước khi thứ này
  ra khỏi LAN.
- `raw_payload` không bao giờ ra khỏi API (`tests/test_isolation.py` thi hành).
- Không commit `.env`. `var/` cũng gitignored — nó chứa output probe chưa redact.

## Tài liệu

- `DISCOVERY.md` — kết quả reverse-engineer API vendor, kèm mức độ đã xác minh của
  từng kết luận và danh sách những gì CÒN CHƯA biết.
- `app/adapters/xingke/mapping.py` — mapping khai báo. **File duy nhất phải sửa** khi
  vendor đổi tên field hoặc đơn vị.
