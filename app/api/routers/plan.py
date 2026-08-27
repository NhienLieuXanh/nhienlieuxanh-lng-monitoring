"""Số đo tay của trang Kế hoạch: ``/api/plan/readings/...``

Vì sao có endpoint này. Kế hoạch nạp là một chuỗi số học từ *một* mức khởi đầu:
mỗi ngày trừ đi mức tiêu thụ bình quân. Mức bình quân thì đúng trên cả tháng
nhưng sai mỗi ngày — xưởng chạy ít thì cuối ngày còn 48 m³ chứ không phải 46,60
m³ như công thức. Không có đường nhập số thực tế, người vận hành buộc phải sửa
"thể tích ban đầu" rồi dịch "ngày bắt đầu": mất lịch sử những ngày trước, và mất
luôn thứ họ cần nhất là so ước tính với thực tế.

Phạm vi đã chốt: số này CHỈ dùng cho trang Kế hoạch. Dashboard, dự báo, mức tiêu
thụ đo được, nhận diện lần nạp và cảnh báo vẫn chỉ đọc ``telemetry``, tức chỉ đọc
số của thiết bị. Nhờ vậy không có chỗ nào trong hệ thống pha số người vào số máy.

Đơn vị là **lít** như mọi field thể tích khác của API. UI quy đổi m³ ở biên.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response, status

from app.api.deps import SessionDep, UserDep
from app.api.schemas import PlanReadingIn, PlanReadingOut
from app.repositories import plan_readings as pr_repo
from app.repositories import terminals as term_repo

router = APIRouter(prefix="/plan", tags=["plan"])

#: Trần tuyệt đối, KHÔNG theo dung tích lưu trong DB. 1.000 m³ là ngưỡng "chắc chắn
#: gõ sai đơn vị" (thêm một số 0, hoặc nhập lít vào ô m³) mà vẫn không cản trở ca
#: dùng thật nào — bồn LNG ở đây cỡ vài chục m³.
#:
#: Vì sao KHÔNG chặn theo ``terminals.capacity_l``: bản đầu có chặn, và nó đã chặn
#: đúng một lần nhập hợp lệ trên production. `capacity_l` được ingest từ vendor
#: (``cylinderVolume``) và với bồn Fuji Seal nó là 10425 L trong khi bồn thật là
#: 54 m³ — người vận hành gõ 42 m³ thì bị từ chối 422 kèm một thông điệp nói rằng
#: chính số đo của họ là sai. Một con số vendor có thể sai không được phép phủ quyết
#: số người vận hành TỰ ĐO. Cảnh báo vượt dung tích nằm ở client, nơi có con số
#: "Dung tích" mà chính người dùng đang lập kế hoạch với, và nó CẢNH BÁO chứ không chặn.
FALLBACK_MAX_L = 1_000_000


def _require_terminal(session: SessionDep, psn: str) -> None:
    if term_repo.get_by_psn(session, psn) is None:
        # 404 chứ không phải 200 rỗng: PSN gõ sai phải phân biệt được với "bồn này
        # chưa nhập số nào". Cùng luật với /api/terminals/{psn}.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Terminal not found")


@router.get("/readings/{psn}", response_model=list[PlanReadingOut])
def list_readings(
    psn: str,
    session: SessionDep,
    _: UserDep,
    from_: Annotated[date | None, Query(alias="from")] = None,
    to: Annotated[date | None, Query()] = None,
) -> list[PlanReadingOut]:
    """Các số đo tay đã lưu của một bồn, cũ trước mới sau."""
    _require_terminal(session, psn)
    if from_ is not None and to is not None and from_ > to:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "from phải <= to")
    rows = pr_repo.list_for(session, psn, start=from_, end=to)
    return [PlanReadingOut.model_validate(r) for r in rows]


@router.put("/readings/{psn}/{day}", response_model=PlanReadingOut)
def put_reading(
    psn: str,
    day: date,
    body: PlanReadingIn,
    session: SessionDep,
    user: UserDep,
) -> PlanReadingOut:
    """Ghi số đo của một ngày. Gửi lại cùng ngày là ghi đè.

    PUT chứ không POST: địa chỉ ``(bồn, ngày)`` xác định đúng một số đo, nên lệnh
    này idempotent — bấm Lưu hai lần không được sinh ra hai dòng.
    """
    _require_terminal(session, psn)

    # Chỉ trần TUYỆT ĐỐI. Xem FALLBACK_MAX_L: chặn theo capacity_l của DB đã từ chối
    # một lần nhập hợp lệ trên production vì chính capacity_l sai.
    if float(body.volume_l) > FALLBACK_MAX_L:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Thể tích {body.volume_l} L vượt ngưỡng hợp lý ({FALLBACK_MAX_L:g} L) — "
            "kiểm lại đơn vị, giá trị này tính bằng lít",
        )

    row = pr_repo.upsert(session, psn, day, body.volume_l, by=user)
    session.commit()
    return PlanReadingOut.model_validate(row)


@router.delete("/readings/{psn}/{day}", status_code=status.HTTP_204_NO_CONTENT)
def delete_reading(psn: str, day: date, session: SessionDep, _: UserDep) -> Response:
    """Xoá số đo của một ngày — quay về dùng số ước tính cho ngày đó."""
    _require_terminal(session, psn)
    if not pr_repo.delete(session, psn, day):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ngày này chưa có số đo tay")
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
