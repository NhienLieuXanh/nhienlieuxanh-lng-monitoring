"""Logic trang Kế hoạch, chạy bằng CHÍNH các hàm JS trong index.html.

Trang Kế hoạch tính toàn bộ ở trình duyệt, nên pytest không tự nhìn thấy nó. Test này
trích nguyên văn ``planStepDay`` cùng các hàm phụ ra khỏi file rồi chạy bằng node —
không chép tay lại công thức, vì một bản chép tay chỉ kiểm được chính bản chép.

Mỗi ca dưới đây là một lỗi đo được trên trang thật, 28–29/09/2026:

* Ngày nạp mất tiêu thụ: mức hôm sau = đúng mức sau nạp, xoá sạch lượng dùng của
  ngày đó. Ở 7,7 m³/ngày ra 10 lần nạp/62 ngày thay vì số cần thật.
* Luật kích hoạt so mức ĐẦU NGÀY với dự trữ, nên để bồn tụt dưới dự trữ trước lúc xe
  tới: xe tới lúc bồn còn 7,80 m³ trong khi dự trữ là 10.
* Ô "Ngày nghỉ" không chặn được giao hàng, nên ngày lễ 24/11 vẫn có xe; tích thêm
  "Nạp chỉ định" thì màn hình không đổi gì.
* Điểm neo lúc 15:12 bị coi là thể tích đầu ngày rồi trừ nguyên một ngày dùng.

Bỏ qua nếu máy không có node — nhưng máy phát triển của dự án có, và ``node --check``
đã là một bước bắt buộc trước mỗi commit chạm tới index.html.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

INDEX = Path(__file__).resolve().parents[1] / "app" / "static" / "index.html"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="cần node")


def _trich() -> str:
    src = INDEX.read_text(encoding="utf-8").split("\n")

    def one(pat: str) -> str:
        return next(line.strip() for line in src if re.search(pat, line))

    def block(pat: str) -> str:
        i = next(k for k, line in enumerate(src) if re.search(pat, line))
        j = i + 1
        while src[j].rstrip() != "    }":
            j += 1
        return "\n".join(src[i : j + 1])

    return "\n".join(
        [
            "const planRest=new Set(), planForce=new Set(), planNoDeliver=new Set();",
            # Trạng thái chế độ số thật, và lần nạp thật giả lập (bản thật đọc dự báo).
            "let planLive=null, planReadings=new Map(), planOpen=new Map(), REFILLS={};",
            "function planActualRefill(k){ return REFILLS[k] || null; }",
            one(r"const pad2 = "),
            one(r"const round2 = "),
            one(r"const planDayKey = "),
            block(r"function planDeliverable"),
            block(r"function planRefillHour"),
            block(r"function planStepDay"),
            one(r"const planDayDiff = "),
            block(r"function planSeed"),
            block(r"function planShadow"),
            block(r"function planRows"),
        ]
    )


KICH_BAN = r"""
const P = { cap: 60, pct: 90, use: 7.7, res: 10, start: 41.18, days: 62, time: "08:00:00" };
const POST = 54, H = 8 / 24;

function lich(start = "2026-09-28", frac = 0, days = P.days, lvl0 = P.start) {
  const base = new Date(start + "T00:00:00"); base.setHours(8, 0, 0, 0);
  let lvl = lvl0, f = frac; const out = [];
  for (let i = 0; i < days; i++) {
    const r = planStepDay(lvl, f, i, base, P, POST, planRefillHour(P));
    out.push({ key: r.key, dow: r.d.getDay(), lvl, fill: r.fill, conflict: r.conflict,
               atRefill: r.atRefill, next: r.next, trig: r.trig });
    lvl = r.next; f = 0;
  }
  return out;
}
const kq = {};

// 1. ca của người dùng
let rows = lich();
const naps = rows.filter(r => r.fill);
kq.so_lan_nap = naps.length;
kq.thap_nhat_luc_xe_toi = Math.min(...naps.map(r => r.atRefill));
kq.co_ngay_am = rows.some(r => r.lvl < 0);
kq.nap_chu_nhat = naps.some(r => r.dow === 0);

// 2. ngày nạp vẫn tiêu thụ phần còn lại của ngày
const n0 = naps[0];
kq.sau_nap = n0.next;

// 3. ngưỡng: <= dự trữ + (1+h) ngày dùng, Thứ Bảy thêm một ngày Chủ Nhật
let lech = 0, dem = 0;
for (let dow = 1; dow <= 6; dow++) {
  const base = new Date(`2026-10-${String(4 + dow).padStart(2, "0")}T00:00:00`);
  base.setHours(8, 0, 0, 0);
  for (let L = 0; L <= 40; L += 0.37) {
    const r = planStepDay(L, 0, 0, base, P, POST, H);
    const ky = base.getDay() === 6 ? L < P.res + (2 + H) * P.use : L < P.res + (1 + H) * P.use;
    dem++; if (r.trig !== ky) lech++;
  }
}
kq.nguong_so = dem; kq.nguong_lech = lech;

// 4. ngày lễ TRÙNG một lần nạp ngày thường -> dời sớm, vẫn giữ dự trữ
const truoc = lich().filter(r => r.fill);
const bi = truoc.find(r => r.dow >= 1 && r.dow <= 5 && r.key.slice(5, 7) === "11");
planNoDeliver.add(bi.key);
rows = lich();
const sau = rows.filter(r => r.fill);
kq.le_ngay = bi.key;
kq.le_van_nap = rows.find(r => r.key === bi.key).fill;
kq.le_thap_nhat = Math.min(...sau.map(r => r.atRefill));
const tBi = Date.parse(bi.key);
kq.le_doi_som = sau.some(r => Date.parse(r.key) < tBi && Date.parse(r.key) >= tBi - 3 * 86400000);

// 5. tích "Nạp chỉ định" vào đúng ngày lễ đó -> xung đột, không nạp
planForce.add(bi.key);
const x = lich().find(r => r.key === bi.key);
kq.xung_dot = x.conflict; kq.xung_dot_nap = x.fill;
planForce.clear(); planNoDeliver.clear();

// 6. ngày nghỉ: nhà máy tắt, không trừ tiêu thụ
planRest.add("2026-09-29");
rows = lich();
kq.nghi_truoc = rows.find(r => r.key === "2026-09-29").lvl;
kq.nghi_sau = rows.find(r => r.key === "2026-09-30").lvl;
planRest.clear();

// 7. neo lúc 15:12, sau giờ nạp
const frac = (15 * 60 + 12) / 1440;
rows = lich("2026-09-28", frac, 3);
kq.neo_nap_ao = rows[0].fill;
kq.neo_hom_sau = rows[1].lvl;
kq.neo_ky_vong = 41.18 - (1 - frac) * 7.7;

// 8. CHẾ ĐỘ SỐ THẬT — đúng ca 29/09/2026: lần đo mới nhất 34,39 lúc 15:59 ngày 29,
// xe thật tới 28/09 lúc 14:29 (+35,31). Người dùng lùi ngày bắt đầu về 27/09.
const P2 = { cap: 60, pct: 90, use: 5.99, res: 10, start: 0, days: 12, time: "08:00:00" };
function bang(start, days = P2.days, p = P2) {
  const base = new Date(start + "T00:00:00"); base.setHours(8, 0, 0, 0);
  return planRows({ ...p, days, date: start }, base, 54);
}
planOpen = new Map([
  ["2026-09-27", { m3: 15.40, at: new Date("2026-09-27T00:05:00") }],
  ["2026-09-28", { m3: 9.88, at: new Date("2026-09-28T00:10:00") }],
  ["2026-09-29", { m3: 40.2, at: new Date("2026-09-29T00:01:00") }],
]);
REFILLS = { "2026-09-28": { m3: 35.31, hhmm: "14:29" } };
planLive = { src: "tele", key: "2026-09-29", m3: 34.39,
             at: new Date("2026-09-29T15:59:00"), frac: (15 * 60 + 59) / 1440 };
let b27 = bang("2026-09-27").rows;
kq.live_27 = b27[0].level; kq.live_27_past = b27[0].past;
kq.live_28 = b27[1].level;
kq.live_nap_qua_khu = b27.slice(0, 3).some(r => r.fill);
kq.live_29_dau_ngay = b27[2].level; kq.live_29_neo = b27[2].anchor && b27[2].anchor.m3;
kq.live_30 = b27[3].level;
kq.live_30_ky_vong = 34.39 - (1 - planLive.frac) * 5.99;

// Đổi ngày bắt đầu = đổi KHUNG NHÌN: mọi ngày chung phải trùng số và trùng lịch nạp.
const sig = (rows) => Object.fromEntries(rows.map(r => [r.key, JSON.stringify([round2(r.level ?? -1), r.fill, r.order])]));
const s27 = sig(bang("2026-09-27", 40).rows);
let chung = 0, lech_khung = 0;
for (const s of [sig(bang("2026-09-29", 38).rows), sig(bang("2026-10-03", 34).rows)]) {
  for (const [k, v] of Object.entries(s)) if (s27[k]) { chung++; if (s27[k] !== v) lech_khung++; }
}
kq.khung_chung = chung; kq.khung_lech = lech_khung;

// Ngày đã qua không có số đo: ước tính từ ngày trước (trừ dùng, cộng nạp thật).
planOpen.delete("2026-09-28");
b27 = bang("2026-09-27").rows;
kq.im_28 = b27[1].level; kq.im_28_nodata = b27[1].noData;
kq.im_29_dau_ngay = b27[2].level;
planOpen.delete("2026-09-27");
kq.im_dau = bang("2026-09-27").rows[0].level;

// Chế độ giả định giữ nguyên hành vi cũ: dòng 0 = ô "Thể tích ban đầu", đầu ngày.
planLive = null;
const gd = bang("2026-09-27", 5, { ...P2, start: 20 }).rows;
kq.gia_dinh_0 = gd[0].level; kq.gia_dinh_past = gd.some(r => r.past);

console.log(JSON.stringify(kq));
"""


@pytest.fixture(scope="module")
def kq(tmp_path_factory: pytest.TempPathFactory) -> dict:
    d = tmp_path_factory.mktemp("plan_js")
    f = d / "chay.js"
    f.write_text(_trich() + "\n" + KICH_BAN, encoding="utf-8")
    out = subprocess.run(
        ["node", str(f)], capture_output=True, text=True, encoding="utf-8", timeout=60
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_lam_theo_ke_hoach_xe_KHONG_BAO_GIO_toi_luc_bon_duoi_du_tru(kq: dict) -> None:
    """Lỗi lớn nhất: bản cũ để xe tới lúc bồn còn 7,80 m³ với dự trữ 10."""
    assert kq["thap_nhat_luc_xe_toi"] >= 10.0 - 1e-9, kq["thap_nhat_luc_xe_toi"]


def test_so_lan_nap_la_so_can_that_khong_phai_10(kq: dict) -> None:
    """Ở 7,7 m³/ngày, giữ được dự trữ lúc xe tới cần hơn hẳn 10 chuyến / 62 ngày."""
    assert kq["so_lan_nap"] > 10


def test_ngay_nap_van_tieu_thu_phan_con_lai_cua_ngay(kq: dict) -> None:
    """Bản cũ: hôm sau = đúng 54 m³, tức xoá sạch lượng dùng của ngày nạp."""
    assert kq["sau_nap"] == pytest.approx(54.0 - (1 - 8 / 24) * 7.7)
    assert kq["sau_nap"] < 54.0


def test_khong_am_khong_nap_chu_nhat(kq: dict) -> None:
    assert kq["co_ngay_am"] is False
    assert kq["nap_chu_nhat"] is False


def test_nguong_kich_hoat_tinh_toi_GIO_XE_TOI(kq: dict) -> None:
    assert kq["nguong_lech"] == 0, f"{kq['nguong_lech']}/{kq['nguong_so']} lệch"


def test_ngay_le_trung_lan_nap_thi_doi_som_va_van_giu_du_tru(kq: dict) -> None:
    assert kq["le_van_nap"] is False, f"vẫn xếp nạp vào ngày lễ {kq['le_ngay']}"
    assert kq["le_doi_som"] is True, "lần nạp không dời sớm lên trước ngày lễ"
    assert kq["le_thap_nhat"] >= 10.0 - 1e-9


def test_nap_chi_dinh_vao_ngay_le_thi_bao_xung_dot_khong_im_lang(kq: dict) -> None:
    """Đây là "24/11 không hiện gì": bản cũ lặng lẽ bỏ qua."""
    assert kq["xung_dot"] is True
    assert kq["xung_dot_nap"] is False


def test_ngay_nghi_giu_nguyen_the_tich(kq: dict) -> None:
    assert kq["nghi_sau"] == pytest.approx(kq["nghi_truoc"])


def test_neo_giua_ngay_chi_tru_phan_con_lai_va_khong_bia_lan_nap(kq: dict) -> None:
    """Số lúc 15:12 không phải thể tích đầu ngày; sau giờ nạp thì xe hôm nay đã qua."""
    assert kq["neo_nap_ao"] is False
    assert kq["neo_hom_sau"] == pytest.approx(kq["neo_ky_vong"])


# --- chế độ số thật: đúng ca 29/09/2026 -----------------------------------------


def test_lui_ngay_bat_dau_thi_ngay_da_qua_hien_SO_DO_THAT(kq: dict) -> None:
    """Bản cũ: dòng 27/09 = 34,39 (số lúc 15:59 ngày 29/09); bồn thật hôm đó 15,40."""
    assert kq["live_27"] == pytest.approx(15.40)
    assert kq["live_27_past"] is True
    assert kq["live_28"] == pytest.approx(9.88)


def test_khong_bia_lan_nap_trong_qua_khu(kq: dict) -> None:
    """Bản cũ xếp nạp 29/09 trong khi xe thật tới 28/09."""
    assert kq["live_nap_qua_khu"] is False


def test_dong_neo_hien_dau_ngay_that_va_tinh_tu_lan_do_moi_nhat(kq: dict) -> None:
    assert kq["live_29_dau_ngay"] == pytest.approx(40.2)
    assert kq["live_29_neo"] == pytest.approx(34.39)
    assert kq["live_30"] == pytest.approx(kq["live_30_ky_vong"])


def test_doi_ngay_bat_dau_KHONG_doi_so_cua_ngay_nao(kq: dict) -> None:
    assert kq["khung_chung"] > 60
    assert kq["khung_lech"] == 0, f"{kq['khung_lech']}/{kq['khung_chung']} ngày lệch"


def test_ngay_bon_im_thi_uoc_tinh_va_noi_ra(kq: dict) -> None:
    assert kq["im_28_nodata"] is True
    assert kq["im_28"] == pytest.approx(15.40 - 5.99)
    # Ngày kế có số đo thật thì lại dùng số thật, không kéo ước tính theo.
    assert kq["im_29_dau_ngay"] == pytest.approx(40.2)
    # Không có gì để ước tính từ đó thì để trống, không bịa.
    assert kq["im_dau"] is None


def test_che_do_gia_dinh_giu_hanh_vi_cu(kq: dict) -> None:
    assert kq["gia_dinh_0"] == pytest.approx(20)
    assert kq["gia_dinh_past"] is False
