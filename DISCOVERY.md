# Xingke (星科云平台) — Kết quả discovery

**Ngày capture:** 2026-08-18 · **Base URL:** `https://www.xk-iot.cn/ls/`
**Account org:** `orgName='Gas LNG'`, `orgId='843744627050676224'`
**Fixture thật (đã redact PII):** `tests/fixtures/xingke/psn_search_real*.json`

Mọi mục dưới đây **đã xác minh trên response thật**. Chỗ nào còn là suy đoán đều ghi rõ.

---

## 1. Auth

| Hạng mục | Giá trị |
|---|---|
| Login | `POST /ls/login`, body `{"userName":…, "password":…}`, plaintext qua TLS |
| Token | `Authorization: Bearer <localStorage.token>` |
| Dạng token | **UUID 36 ký tự, KHÔNG phải JWT** → opaque, session server-side, không decode được expiry |
| Cookie | Không có auth cookie. Cookie duy nhất là `language`. Không `Set-Cookie` trên 401 |
| Header bắt buộc | `X-Requsted-With: XMLHttpRequst` ← **typo của vendor, gửi y nguyên** |
| | `Content-Type: application/json;charset=UTF-8` |

Mọi `X-Token` / `Admin-Token` / `baseURL:"/prod-api"` trong bundle là **dead boilerplate của
vue-element-admin** — bỏ qua.

> **Lỗi phía vendor, nên báo account manager:** login response trả về field `password` và
> frontend lưu vào `localStorage.userInfo.password`. Bất kỳ XSS nào trên domain đó đọc được.
> Không phải thứ ta config sai được.

---

## 2. Envelope — có BA convention khác nhau

```jsonc
// (a) Thành công — CHỈ code/data/msg. KHÔNG có success, KHÔNG có dataNotEmpty.
{"code":200, "msg":"操作成功", "data":{...}}

// (b) Lỗi auth — CÓ thêm success + dataNotEmpty
{"msg":"AccessDenied","code":401,"data":null,"dataNotEmpty":false,"success":false}

// (c) Lỗi validation — HTTP 200 nhưng code = -1
{"code":-1,"msg":"Required Long parameter 'id' is not present","data":null}

// (d) Lỗi routing Spring Cloud Gateway — shape khác hẳn, KHÔNG có code
{"timestamp":"2026-08-18 15:03:18","path":"/login","status":405,
 "error":"Method Not Allowed","message":"…","requestId":"1ac81d31"}
```

**Hệ quả cho `unwrap()`:**

- Thành công là `code == 200`. **KHÔNG phải `code == 0`** — plan gốc giả thiết sai; theo nó thì
  *mọi* request thành công đều bị raise.
- **Không được dựa vào `success` có mặt** — nó vắng trên response thành công.
- **Phải xét `code`, không được chỉ xét HTTP status** — xem (c): HTTP 200 kèm `code:-1`.
- Nhận dạng (d) bằng: có `status` mà không có `code`.

## 3. Pagination — custom wrapper, không phải Spring Data chuẩn

`data` chứa: `content` (list) · `totalElements` · `totalPage` · `currentPage` ·
`currentPageElements` · `pageSize` · `firstPage` · `lastPage` · `nextPage` · `prevPage`

**Không phải** `records`/`total` (MyBatis-Plus), **không phải** `totalPages`/`number`/`size`
(Spring Data chuẩn). `currentPage` **1-based**. `pageSize` max UI = **100**.

---

## 4. `GET infrastructure/server/backstage/device/psn/search`

Params: `currentPage` · `pageSize` · `psn` · `queryTime` (**một ngày `YYYY-MM-DD`**) ·
`searchParam`. Trả list phân trang các lần đọc của **đúng một ngày** đó.

40 key mỗi dòng. Mapping xác minh bằng giá trị thật (PSN 2604200016, `queryTime=2026-07-23`):

| Upstream | Mẫu | → Target | Ghi chú |
|---|---|---|---|
| `time` | `'2026-07-23 16:03:29'` | `sampled_at` | **NAIVE string** — xem §5 |
| `currentVolume` | `61` | `volume_l` | L |
| `cylinderVolume` | `10425` | `terminals.capacity_l` | L, cấu hình tài sản |
| `volumePercentage` | `0.59` | `volume_percent` | **thang 0–100** — xem §6 |
| `pressureMpa` | `0.071` | `pressure_mpa` | MPa |
| `pressure` | `71` | — | **cùng phép đo, đơn vị kPa** (71 kPa = 0.071 MPa) |
| `height` | `42` | `level_mmwc` | **mực lỏng mmWC, KHÔNG phải chiều cao bồn** |
| `diffPressure` | `0.41` | `diff_pressure_kpa` | kPa · `0.41 × 101.972 = 41.8 ≈ height 42` ✓ |
| `currentVoltage` | `3.6` | `battery_v` | V |
| `signalStrengthPercentage` | `20` | `signal_percent` | 0–100 |
| `temperatureOne` | `None` | `temperature_c` | null trên cả 2 thiết bị |
| `temperature` | `0` | — | int; có thể là default, không phải phép đo |
| `vacuumTransducerDegreeOne` | `0` | `vacuum_pa` | Pa |
| `mediumName` / `medium` | `'LNG'` / `2` | `medium_name` | `medium` là code |
| `tankTypeName` / `tankType` | `'立式'` (đứng) / `0` | `tank_type_name` | |
| `hardwareVersion` `softwareVersion` | `None` | terminals | **chú ý §7** |
| `moduleNumber` `cardNumber` | (PII) | `modem_number` `sim_iccid` | |
| `diameter` `tubeLength` `sendFrequency` | `2100` `2310` `60` | — | hình học bồn / chu kỳ báo |
| `gpsLatitude` `gpsLongitude` | `10.971047` `106.750161` | — | **giai đoạn 1 không dùng**; có trong `raw_payload` |
| `index` | `1` | — | **số dòng phía client, không phải dữ liệu** |
| `pressureOne/Two/Three(+Mpa)` · `pressureTwpMpa` · `temperatureTwo/Three` · `color` · `electricityPercentage` · `currentChargingCurrent` | `None` / `0` | — | sensor phụ, không dùng |

> **`pressureTwpMpa`** — typo thật **trong payload** ("Twp" thay vì "Two").

**Kiểm chứng nội bộ chéo đều pass** — đây là cách xác nhận đơn vị bằng dữ liệu thay vì tranh
luận: `pressure`=71 kPa ↔ `pressureMpa`=0.071 MPa, và `diffPressure`=0.41 kPa ↔ `height`=42 mmWC.
Hai cặp độc lập ⇒ đơn vị khớp hợp đồng **1:1, không convert gì cả**.

**Cadence lấy mẫu = 30 phút**, đo từ 12 dòng của PSN 2605090007 ngày 2026-06-02
(22:17:03 / 21:47:03 / 21:17:03 / 20:47:02) ⇒ ngưỡng stale 90 phút = 3 sample bị mất.

---

## 5. ⚠️ Timezone — `time` là naive, render ở UTC+8

Test thực nghiệm 2026-08-18: gọi `GET /ls/login` (405) lấy field `timestamp` của Gateway, so
với cửa sổ UTC đo tại chỗ:

```
server 'timestamp'            : 2026-08-18 15:03:18   (naive)
cửa sổ UTC của tôi            : 07:03:15 .. 07:03:17
  giả thiết UTC              → 15:03:18 UTC   lệch +8.0h  ✗
  giả thiết Asia/Shanghai    → 07:03:18 UTC   KHỚP (lệch 3s = latency)  ✓
  giả thiết Asia/Ho_Chi_Minh → 08:03:18 UTC   lệch +1.0h  ✗
```

**Kết luận: naive timestamp phải parse là `Asia/Shanghai` (UTC+8).**

> Mock dashboard ban đầu ghi `"2026-07-23T16:03:29+07:00"` — coi `16:03:29` là giờ VN. Thực tế
> là giờ Thượng Hải ⇒ **15:03:29 giờ VN**. Lệch 1 tiếng: đủ nhỏ để trông hợp lý và không bao
> giờ bị phát hiện.

*Giới hạn bằng chứng*: test đo formatter của **Gateway** JVM, còn `time` do microservice
`infrastructure` render — gần như chắc chắn cùng deployment nhưng khác JVM. Vì vậy vẫn giữ cả
ba lớp phòng vệ: `XINGKE_VENDOR_TZ` là **setting** (đoán sai thì sửa `.env`, không sửa code),
cột `vendor_ts_raw TEXT` lưu string gốc, và `scripts/verify_tz.py` đối chiếu với UI vendor.

> **KHÔNG backfill lịch sử trước khi `verify_tz.py` pass.** Sai TZ không làm sai hiển thị — nó
> **làm hỏng khoá dedup `(psn, sampled_at)`**: sửa parsing về sau thì mọi dòng có khoá khác,
> `ON CONFLICT` không match, và bạn **nhân đôi âm thầm toàn bộ dữ liệu lịch sử**.

---

## 6. `volume_percent` là thang 0–100, và bồn thật sự gần cạn

Vendor **có** gửi `volumePercentage` trên endpoint này.

| PSN | `currentVolume` | `cylinderVolume` | vendor `volumePercentage` | `vol/cap×100` |
|---|---|---|---|---|
| 2604200016 | 61 | 10425 | **0.59** | **0.5851** ✓ |
| 2605090007 | 30 | 10425 | **0.29** | **0.2878** ✓ |

Vendor tự tính `volume_l / capacity_l × 100`. Vậy `0.59` nghĩa là **0.59% đầy** — 61 L trong
bồn 10.425 L, tức **gần cạn**.

Prototype dashboard có `Math.round(tank.volume_percent * 100)` → hiển thị **"59%"** với thanh
level đầy hơn nửa. Lỗi nghiêm trọng nhất của prototype. **Không CHECK constraint nào bắt được**
vì `0.59` hợp lệ ở cả hai thang ⇒ API phát kèm `fill_percent` tính độc lập server-side làm đối
chứng, và log WARNING khi hai số lệch > 5 điểm.

Vì `volumePercentage` do vendor gửi ⇒ `volume_percent_source = 'vendor'`. Vẫn giữ cột
provenance cho trường hợp endpoint khác không gửi field này.

---

## 7. `GET infrastructure/server/backstage/device/list` — HAI CÁI BẪY

### Bẫy A: param `psn` bị bỏ qua **im lặng**

```
device/list?psn=2604200016          → totalElements 3543   ← param BỊ BỎ QUA
device/list?searchParam=2604200016  → totalElements 1      ← ĐÚNG
```

Truyền `psn` không báo lỗi, chỉ âm thầm trả về thiết bị của tất cả mọi người.
**Luôn dùng `searchParam`.**

### Bẫy B: rò dữ liệu khách hàng khác — 3543 thiết bị

`device/list` không filter trả về **3543 thiết bị**, và **trang 1 chứa 0 thiết bị của công ty,
100 thiết bị của bên thứ ba**. Account có org scope (`orgName='Gas LNG'`) nhưng endpoint **bỏ
qua scope đó**. Lỗi phân quyền phía vendor.

**Hệ quả bắt buộc:**

- `fetch_devices()` **không bao giờ** gọi `device/list` không filter. Query per-PSN qua `searchParam`.
- `XINGKE_ALLOWED_PSNS` allowlist thi hành ở **ranh giới adapter**; PSN ngoài danh sách bị drop
  và đếm vào log. Không "ingest hết rồi filter sau".
- Bản capture đầu tiên đã vô tình lưu 100 bản ghi bên thứ ba; **đã xoá khỏi
  `var/probe/capture_raw.json`**. `var/` nằm trong `.gitignore`.
- Nên báo vendor. Nếu đối thủ của bạn cũng dùng Xingke thì dữ liệu bồn của bạn cũng đang hiển
  thị cho họ theo đúng cách này.

### Field của `device/list` (khác `psn/search`)

`id` `psn` `deviceMode` `deviceType` `deviceTypeName` **`hardwarVersion`** `softwareVersion`
`phone` `bindStatus` `bindStatusName` `sensorStatus` `sensorStatusName`
`isSupportFillingStatus` `isSupportFillingStatusName` `createTime` `createId` `createName`

> **`hardwarVersion`** — thiếu chữ `e`, trong khi `psn/search` viết **`hardwareVersion`** đúng
> chính tả. **Hai endpoint viết khác nhau cùng một field.** Đây là lý do alias index chuẩn hoá
> key (`re.sub(r'[^a-z0-9]', '', k.lower())`) không phải đề phòng lý thuyết.
>
> `moduleNumber` / `cardNumber` **không có** trên `device/list` — chúng đến từ `psn/search`.
> Metadata `terminals` phải hợp từ **cả hai** endpoint.

---

## 8. Còn chưa xác minh

| Ẩn số | Cách giải | Rủi ro nếu bỏ qua |
|---|---|---|
| `startDate`/`endDate` có hoạt động thật không | `probe params` — 6 request read-only | Backfill phải walk từng ngày: 2 PSN × 365 ngày ≈ 730+ request |
| Token có bind IP không (login flow gọi `getRealIp`) | mint token ở host A, dùng từ host B | `StaticTokenAuth` chỉ chạy được local → đổi cả câu chuyện deploy |
| Login đồng thời có invalidate session cũ không | login browser → `probe login` → refresh browser | Job **đăng xuất người đang dùng web console**, sinh 401 ngẫu nhiên, cực khó chẩn đoán về sau |
| TTL của token | quan sát | Không biết khi nào cần re-login |
| `device/export/psn/search` có export cả range trong 1 request không | probe | Bỏ mất phương án backfill lịch sự hơn hẳn |

**Nên xin vendor account service read-only riêng** trước khi chạy scheduler thật. Vendor có bảng
audit `userLoginLog/list` và `operateLog/v2/list` — login và operation của bạn được ghi lại.
Bundle load `socket.io 2.2.0` với `URLWS` trỏ về origin ⇒ **hạ tầng push đã tồn tại**; hỏi họ
có API tài liệu hoá hoặc MQTT không có thể tiết kiệm cả cái adapter này.
