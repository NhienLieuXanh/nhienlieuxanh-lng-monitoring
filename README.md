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

**Nguồn đo phút thứ hai** (tắt mặc định, `YOKOHAMA_ENABLED`). Cổng đó bỏ qua bộ lọc
ngày trên lịch sử phút và chỉ stream từ bản ghi mới nhất, nên **không backfill xa
được**: ngân sách mặc định ~8 MB ≈ 4 ngày. Lùi xa hơn phải nâng
`YOKOHAMA_MAX_STREAM_BYTES` có ý thức. Lỗi schema của nguồn này không được pause
ingest của nguồn kia. Gọp giờ + xoá phút cũ là **quyết định hoãn**: ~1,9 MB/ngày
≈ 130 MB/năm trên Neon 512 MB — làm khi `/api/health` báo dung lượng `degraded`.

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

**Bản đồ.** Vị trí bồn trên nền bản đồ thế giới **nhúng sẵn trong app**
(`app/static/world-vi.geojson`, dựng bằng `scripts/build_world_map.py` từ Natural Earth
1:110m — public domain). Không gọi tile của nhà cung cấp nào: nền bản đồ này **không có
đường lưỡi bò**, và `tests/test_world_map.py` canh giữ điều đó bằng 10 điểm thăm dò cộng
một lưới 268 ô quét vùng biển hở — với tile raster thì không viết được test nào như vậy.
Tên nước bằng tiếng Việt (`NAME_VI` có sẵn trong dữ liệu); Hoàng Sa và Trường Sa được
đánh dấu thuộc Việt Nam, khai trong `VN_ISLANDS` ở `index.html` vì ở tỉ lệ 1:110 triệu
Natural Earth bỏ hẳn hai quần đảo. Toạ độ bồn **tự lấy từ GPS của module** mỗi vòng
ingest, và ghim tay được để sửa hoặc để khai khi module không định vị — xem "GPS của
vendor là dữ liệu THỈNH THOẢNG CÓ" bên dưới.

**Kế hoạch nạp.** Lập lịch theo tháng cho một bồn thật, có nút áp dụng mức tiêu thụ đo
được, cột ngày nghỉ / nạp chỉ định, và giờ nạp tới từng giây. **Thông số lưu theo từng
bồn** (`plan_settings`) nên không phải gõ lại mỗi lần mở trang; **dung tích sửa được
ngay tại đây** và ghi thẳng vào `terminals.capacity_l` — một chỗ duy nhất giữ dung tích.
Mốc khởi đầu lấy từ **số đo tay mới nhất**, không lấy từ telemetry: với thiết bị đã im
hàng tháng, số người vừa đo mới là thứ đúng. Cột **Thực tế đo được** cho phép nhập thể tích thật của một ngày:
từ ngày đó kế hoạch tính lại từ số thực tế, và ô đó hiện độ lệch so với ước tính. Số
này lưu ở bảng `plan_readings` theo `(bồn, ngày)` — xem *Ước tính và thực tế* bên dưới.

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

### GPS của vendor là dữ liệu THỈNH THOẢNG CÓ, và 0,0 nghĩa là mất định vị

`psn/search` gửi `gpsLatitude` / `gpsLongitude`, và nó **dùng được** — nhưng không phải
lúc nào cũng có. Xác minh bằng cách gọi thẳng vendor: PSN `2604200016` ngày `2026-07-23`
trả `10.971047 / 106.750161`, còn ngày `2026-06-02` trả `0.000000 / 0.000000` cho **cả
17 dòng**. `gpsAddress` thì luôn là placeholder `"--"`.

Hai cái bẫy, cả hai đều làm mất dữ liệu nếu làm sai:

- **Nhận 0,0** là đặt bồn LNG ở **Null Island giữa vịnh Guinea**, ngoài khơi châu Phi.
  0,0 là tín hiệu mất định vị, không phải một vị trí. Loại ở `mapping.extract_gps` —
  nhưng chỉ loại **cặp** 0,0, vì `lon = 0` riêng lẻ (kinh tuyến Greenwich) là thật.
- **Kết luận "vendor không có GPS" từ một ngày.** Đúng lỗi đã mắc: fixture được chụp vào
  ngày module mất định vị nên toàn 0,0, và kết luận sai đó đã suýt biến một tính năng tự
  động thành việc nhập tay. Một ngày không phải bằng chứng.

Vì vậy có **hai nguồn**, ưu tiên rõ ràng: GPS module tự điền mỗi vòng ingest, còn người
vận hành ghim tay để sửa cho chính xác hơn hoặc để khai khi module không định vị được.
Ingest chỉ `COALESCE` vào chỗ `NULL` nên **ghim tay luôn thắng** — không có luật đó thì
một ngày mất định vị sẽ xoá mất toạ độ đang đúng.

Bốn lưới an toàn ở tầng DB (`ck_terminals_*`): cấm đúng cặp `0,0`, cấm nửa toạ độ, và
chặn `latitude`/`longitude` ngoài khoảng — luật cuối bắt lỗi **đảo thứ tự lat/lon**, vì
với Việt Nam thì kinh độ 106,75 đặt vào ô vĩ độ sẽ vượt ±90 ngay thay vì âm thầm đưa bồn
sang Siberia.

### Ước tính và thực tế là HAI con số khác nhau, và số tay không chạm telemetry

Kế hoạch nạp là một chuỗi số học: thể tích đầu ngày kế = thể tích hôm nay − mức tiêu
thụ/ngày. Mức tiêu thụ là con số **bình quân** nên chuỗi đó trượt khỏi thực tế ngay
ngày thứ hai. Ví dụ thật: nạp tới 54 m³ ngày 1, mức dùng 7,4 m³/ngày, công thức cho
ngày 2 là 46,60 m³ — nhưng hôm đó xưởng chạy ít nên đo được 48 m³.

Cột **Thực tế đo được** nhận con số đó. Từ ngày đã nhập trở đi, chuỗi tính lại từ số
thực tế (ngày 3 = 48 − 7,4 = 40,60), và ô hiện `ước tính 46.6 · +1.4`. Ô tổng
*Thực tế so với ước tính* cộng dồn độ lệch của các ngày đã nhập.

**Phải thấy được ảnh hưởng lan tới đâu.** Bản đầu chỉ đánh dấu đúng cái hàng vừa nhập,
nên các ngày sau — dù ĐÃ tính lại — trông y như cũ, và người dùng đọc ra là "số tôi nhập
không ảnh hưởng gì". Mỗi ngày sau một lần nhập tay giờ có **vạch xanh** ở cột Thể tích
đầu ngày kèm dòng *ước tính gốc*: con số kế hoạch sẽ có nếu không nhập gì. Nó tính từ một
chuỗi song song bỏ qua toàn bộ số nhập tay, nên nó còn cho thấy lịch nạp **dịch ngày** —
ví dụ không nhập tay thì 12/08 phải nạp, nhập rồi thì lùi tới 15/08.

Hai quyết định cố ý, đừng "sửa" mà không đọc:

1. **Mức tiêu thụ/ngày KHÔNG bị tính lại** từ số thực tế. Nếu tính lại thì một ngày
   nghỉ lễ hay một lần đo lệch sẽ kéo lệch toàn bộ phần còn lại của kỳ. Số thực tế chỉ
   **dịch mốc**, không đổi độ dốc.
2. **`plan_readings` là bảng riêng, không ghi vào `telemetry`.** `telemetry` có đúng
   một đường ghi là ingestion từ vendor, và mọi con số "đo được" của hệ thống (mức tiêu
   thụ, nhận diện lần nạp, cảnh báo, báo cáo) đọc từ đó. Cho một form web ghi vào cùng
   bảng thì không còn ai phân biệt được số máy với số người. Số tay **chỉ** dùng cho
   trang Kế hoạch.

Ô nhập là `type="text"` + `inputmode="decimal"`, **không** `type="number"`: người Việt
gõ `47,5` và `<input type="number">` trả chuỗi rỗng cho dấu phẩy ở phần lớn locale —
tức số vừa gõ biến mất không một lời báo. JS parse cả `,` và `.`, giống ô toạ độ ở
trang Bản đồ.

API nói bằng **lít** (`volume_l`) như mọi field thể tích khác; UI quy đổi m³ ở đúng một
chỗ. Server chỉ chặn **trần tuyệt đối** 1.000 m³ (sai đơn vị hàng nghìn lần).

Bản đầu chặn theo `terminals.capacity_l` và **điều đó đã sai**: `capacity_l` ingest từ
vendor (`cylinderVolume`), và với bồn Fuji Seal nó là 10425 L trong khi bồn thật là
54 m³ — người vận hành gõ 42 m³ liền nhận 422 kèm thông điệp nói rằng chính số họ tự đo
là sai. **Một con số vendor có thể sai không được phép phủ quyết số người vận hành tự
đo.** Cảnh báo vượt dung tích chuyển sang client, so với ô *Dung tích* mà người dùng
đang lập kế hoạch với, và nó **cảnh báo chứ không chặn**.

Cùng gốc rễ đó còn gây một cái bẫy khác: khi chọn bồn, *Mức tiêu thụ/ngày* và *Mức dự
trữ* được gợi ý theo `capacity_l` **đã lưu**, nên bồn Fuji Seal nhận mức dự trữ 1,56 m³
(15% của 10,425) và ngưỡng kích hoạt 1,56–8,96 m³ — kế hoạch để bồn 54 m³ tụt xuống gần
cạn mới nạp. Trang Kế hoạch giờ **nói ra chỗ lệch này** khi dung tích đã lưu khác dung
tích đang nhập quá 10%, chứ không tự sửa: không biết số nào đúng, chỉ biết hai số không
thể cùng đúng.

### Ngưỡng kích hoạt nạp là "≤ ngưỡng", KHÔNG phải một dải có sàn

Bản đầu theo đúng công thức bảng tính: nạp khi thể tích đầu ngày rơi **vào dải**
`[dự trữ, dự trữ + 1 ngày dùng)`, và Thứ Bảy là `[dự trữ + 1, dự trữ + 2)` (nạp sớm một
nhịp vì Chủ Nhật không giao hàng). Cái **sàn** của dải đó là lỗi.

Dải chỉ rộng đúng một ngày tiêu thụ, mà chuỗi cũng bước đúng một ngày tiêu thụ — nên tuỳ
pha, nó **nhảy qua** dải. Với mức dự trữ 1,56 và mức dùng 1,04/ngày: `2,70 → 1,66` thì
khớp, nhưng `2,60 → 1,56 → 0,52` thì trượt. Trượt một lần là trượt mãi, vì từ đó thể
tích luôn nhỏ hơn sàn. Đo được trên máy người dùng: `−4,1`, `−5,14`, `−6,18`, `−7,22` —
kế hoạch chỉ trừ dần trong 5 ngày liền, không đề xuất gì, và hiển thị **thể tích bồn
âm**, một con số không tồn tại.

Bỏ sàn: nạp khi `thể tích < dự trữ + 1 ngày dùng` (Thứ Bảy: `< dự trữ + 2 ngày`). Hành vi
**trong** dải cũ không đổi một chút nào — dải cũ là tập con — chỉ thêm đúng cái ca đang
hỏng: đã ở dưới mức dự trữ thì phải nạp ngay, không chờ.

Kèm một cảnh báo mới: nếu `mức sau khi nạp ≤ ngưỡng kích hoạt` thì nạp xong vẫn dưới
ngưỡng, nên lịch sẽ đòi nạp **mỗi ngày** với lượng đặt 0. Cấu hình đó vô nghĩa và giờ nó
tự nói ra.

### Cảnh báo suy từ số đo CŨ: hạ cấp cái nói về bồn, bỏ cái nói về thiết bị

Lỗi quan sát được trên production: bồn 2605090007 im **85,6 ngày**, và `/api/alerts`
phát ra hai dòng cạnh nhau —

```
LOW_VOLUME  critical  Mức chứa thấp: 0.29%
OFFLINE     warning   không có dữ liệu trong 85 ngày
```

Hệ thống vừa nói không biết gì về bồn suốt 85 ngày, vừa phát cảnh báo **nghiêm trọng**
về mức chứa dựa trên đúng con số 85 ngày tuổi đó. `forecast.py` đã có chốt chặn này
(`stale` → không phát runout/hold); `alerts.py` thì **không**, nên hai tầng nói khác
nhau về cùng một dữ liệu. Nếu hộp thư đã cấu hình, nó gửi "mức chứa thấp nghiêm trọng"
mỗi ngày về một bồn có thể đang đầy — loại cảnh báo làm người ta ngừng đọc cảnh báo.

Xử lý phân biệt theo **đối tượng** của từng mã, không cắt hết:

| Mã | Nói về | Khi số đo cũ |
|---|---|---|
| `LOW_BATTERY`, `WEAK_SIGNAL`, `PERCENT_MISMATCH` | thiết bị | **bỏ** — `OFFLINE` đã mang đúng một hành động ("ra xem thiết bị"), thêm ba dòng nữa chỉ là nhiễu |
| `LOW_VOLUME` | bồn | **giữ, hạ xuống `warning`**, và ghi tuổi vào message — "lần cuối nhìn thấy thì đã cạn" vẫn là thông tin thật |

Ngưỡng dùng chung `forecast_max_reading_age_hours` (mặc định 24 giờ), **không** dùng
`online_stale_minutes` (90 phút). Hai ngưỡng khác nhau có chủ ý: mất tín hiệu 2 giờ thì
thiết bị đã "ngoại tuyến" nhưng thể tích đo 2 giờ trước vẫn dùng được để cảnh báo; 25
giờ thì không. Dùng chung một con số với dự báo để cả sản phẩm có MỘT định nghĩa "số đo
quá cũ để tin".

Cùng họ: chip tổng thể tích ở thanh trên là tổng các **lần đọc cuối**, nên khi mọi bồn
đều ngoại tuyến nó hiện `0.091 m³ · cũ 34 ngày` thay vì trơ như số hiện tại.

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

Trang Bản đồ có một quy ước riêng: **nền là SVG, ghim là HTML đè lên**. Mọi thứ nằm
trong SVG đều bị `viewBox` co giãn theo, nên một ghim `r=3` ở mức thế giới sẽ phình bằng
cả tỉnh khi zoom vào — và vùng bấm phình y hệt. Tách hai tầng thì ghim luôn đúng 44px
thật. Cũng vì vậy `.mp-land` phải có `vector-effect: non-scaling-stroke`, nếu không
đường bờ biển thành một dải bệt ở mức Việt Nam. Khung ngắm khai bằng **hộp** kinh/vĩ độ
chứ không bằng một span kinh độ: khung rộng gấp 1,83 lần chiều cao nên span cố định 24°
kinh chỉ cho 13,1° vĩ, cắt mất cả mũi Nam Bộ và Trường Sa.

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
