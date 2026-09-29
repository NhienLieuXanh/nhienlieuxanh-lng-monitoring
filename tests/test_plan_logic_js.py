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
            "let planAnchorFrac=0;",
            one(r"const pad2 = "),
            one(r"const round2 = "),
            one(r"const planDayKey = "),
            block(r"function planDeliverable"),
            block(r"function planRefillHour"),
            block(r"function planStepDay"),
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
