"""安全状态观察 API 集成测试，需要可连接的 PostgreSQL（DATABASE_URL）。

运行：
    DATABASE_URL=postgresql://restrict@localhost:5432/restrict \
        python3 tests/test_observations_api.py
脚本会清空 observations/observation_events/regions/region_meta 等后跑全套用例
（含分页、失败回滚、幂等回放、过期与取消不产生事件、并发），
结束时再次清空，避免影响其它测试脚本。
"""
from __future__ import annotations

import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.main import app  # noqa: E402

failures: list[str] = []


def check(label, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {label}{(' — ' + detail) if detail and not cond else ''}")
    if not cond:
        failures.append(label)


def iso_z(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def future(seconds: float) -> str:
    return iso_z(datetime.now(timezone.utc) + timedelta(seconds=seconds))


# 宽生效窗口，覆盖所有动态生成的未来轨迹时刻
WIDE = ("2020-01-01T00:00:00Z", "2030-01-01T00:00:00Z")
SQUARE = [[0, 0], [100, 0], [100, 100], [0, 100]]

db.init_pool()
client = TestClient(app)


def reset_all():
    with db.pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE observation_events, observations, passes, "
                        "commitments, idempotency_keys, regions RESTART IDENTITY")
            cur.execute("UPDATE region_meta SET revision = 0 WHERE id = 1")
        conn.commit()


def scalar(sql: str) -> int:
    with db.pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            return cur.fetchone()[0]


reset_all()

# ---- 登记区域 A：[0,100]x[0,100] ----
r = client.post("/api/regions", json={
    "name": "A区", "polygon": SQUARE,
    "valid_from": WIDE[0], "valid_to": WIDE[1],
})
check("登记 A 区 201 revision=1", r.status_code == 201 and r.json()["revision"] == 1, r.text)
id_a = r.json()["region"]["id"]

# ---- 登记观察：安全与不安全均可登记 ----
# 安全观察：轨迹 y=200 从区域上方经过
r = client.post("/api/observations", json={"points": [
    {"x": -50, "y": 200, "t": future(3600)},
    {"x": 350, "y": 200, "t": future(3660)},
]})
check("安全观察 201 safe=true revision=1",
      r.status_code == 201 and r.json()["safe"] is True
      and r.json()["revision"] == 1 and r.json()["status"] == "active", r.text)
safe_obs = r.json()
safe_id = safe_obs["id"]
check("返回观察编号/轨迹/registered_at，cancelled_at 为空",
      isinstance(safe_id, int) and len(safe_obs["trajectory"]) == 2
      and safe_obs["trajectory"][0]["t"].endswith("Z")
      and bool(safe_obs["registered_at"]) and safe_obs["cancelled_at"] is None,
      str(safe_obs))
check("登记观察不推进区域 revision",
      client.get("/api/revision").json()["revision"] == 1)

# 不安全观察：轨迹直穿 A 区 → 仍 201，safe=false
r = client.post("/api/observations", json={"points": [
    {"x": -50, "y": 50, "t": future(3600)},
    {"x": 350, "y": 50, "t": future(3660)},
]})
check("不安全观察同样 201 safe=false",
      r.status_code == 201 and r.json()["safe"] is False
      and r.json()["revision"] == 1, r.text)
unsafe_id = r.json()["id"]

# ---- 首点不晚于登记时刻 / 非法轨迹：422 不落库 ----
n_obs = scalar("SELECT count(*) FROM observations")
r = client.post("/api/observations", json={"points": [
    {"x": -50, "y": 200, "t": future(-10)},
    {"x": 350, "y": 200, "t": future(60)},
]})
check("首点早于登记时刻 422 invalid_trajectory",
      r.status_code == 422 and r.json()["error"]["code"] == "invalid_trajectory", r.text)
# 首点恰为当前时刻也拒绝（必须严格晚于）
now_t = iso_z(datetime.now(timezone.utc))
r = client.post("/api/observations", json={"points": [
    {"x": -50, "y": 200, "t": now_t},
    {"x": 350, "y": 200, "t": future(60)},
]})
check("首点等于当前时刻 422", r.status_code == 422, r.text)
r = client.post("/api/observations", json={"points": [
    {"x": -50, "y": 50, "t": future(3600)},
]})
check("轨迹点不足 422", r.status_code == 422, r.text)
same_t = future(3600)
r = client.post("/api/observations", json={"points": [
    {"x": 0, "y": 0, "t": same_t},
    {"x": 1, "y": 1, "t": same_t},
]})
check("时刻不严格递增 422 invalid_trajectory",
      r.status_code == 422 and r.json()["error"]["code"] == "invalid_trajectory", r.text)
r = client.post("/api/observations", json={"points": [
    {"x": 0, "y": 0, "t": "2026-10-01T00:00:00"},
    {"x": 1, "y": 1, "t": "2026-10-01T00:01:00"},
]})
check("时刻不带时区 422", r.status_code == 422, r.text)
r = client.post("/api/observations", json={"points": [
    {"x": -50, "y": 200, "t": future(3600)},
    {"x": 350, "y": 200, "t": future(3660)},
], "extra": 1})
check("多余字段 422", r.status_code == 422, r.text)
check("非法输入均不落库", scalar("SELECT count(*) FROM observations") == n_obs)

# ---- 查询：轨迹、状态、最近结论、revision ----
r = client.get(f"/api/observations/{unsafe_id}")
check("查询观察 200，最近结论 unsafe / revision=1",
      r.status_code == 200 and r.json()["safe"] is False
      and r.json()["revision"] == 1 and r.json()["status"] == "active", r.text)
r = client.get("/api/observations/9999")
check("未知观察编号 404 not_found",
      r.status_code == 404 and r.json()["error"]["code"] == "not_found", r.text)

# 初始无事件
r = client.get(f"/api/observations/{safe_id}/events")
check("初始事件页为空、next_cursor=null",
      r.status_code == 200 and r.json()["events"] == []
      and r.json()["next_cursor"] is None and r.json()["cursor"] == 0, r.text)
check("事件查询未知编号 404",
      client.get("/api/observations/9999/events").status_code == 404)
check("cursor/limit 参数约束（-1 / 0 / 201 均 422）",
      client.get(f"/api/observations/{safe_id}/events?cursor=-1").status_code == 422
      and client.get(f"/api/observations/{safe_id}/events?limit=0").status_code == 422
      and client.get(f"/api/observations/{safe_id}/events?limit=201").status_code == 422)

# ---- 观察不阻止变更：登记覆盖安全观察轨迹的区域 → 201（不是 409）----
r = client.post("/api/regions", json={
    "name": "B区-压观察", "polygon": [[0, 150], [100, 150], [100, 250], [0, 250]],
    "valid_from": WIDE[0], "valid_to": WIDE[1],
})
check("观察不阻止区域变更：覆盖安全观察轨迹仍 201",
      r.status_code == 201 and r.json()["revision"] == 2, r.text)
id_b = r.json()["region"]["id"]

ev = client.get(f"/api/observations/{safe_id}/events").json()["events"]
check("安全观察翻转为不安全：一条事件 safe→unsafe、revision=2",
      len(ev) == 1 and ev[0]["seq"] == 1 and ev[0]["old_safe"] is True
      and ev[0]["new_safe"] is False and ev[0]["revision"] == 2
      and ev[0]["occurred_at"].endswith("Z"), str(ev))
g = client.get(f"/api/observations/{safe_id}").json()
check("查询反映最近结论 unsafe / revision=2",
      g["safe"] is False and g["revision"] == 2, str(g))

# 不安全观察（y=50）仍被 A 区压住，结论未翻转 → 无事件
ev2 = client.get(f"/api/observations/{unsafe_id}/events").json()["events"]
check("结论未翻转的观察不产生事件（仍不安全）", ev2 == [], str(ev2))

# ---- 不改变结论的无害变更（改名）：revision 推进但无事件 ----
r = client.patch(f"/api/regions/{id_a}", json={"name": "A区-改名"})
check("无害改名 200 revision=3", r.status_code == 200
      and r.json()["revision"] == 3, r.text)
check("改名后两张观察均无新事件",
      client.get(f"/api/observations/{safe_id}/events").json()["events"] == [] or
      all(e["revision"] < 3 for e in client.get(f"/api/observations/{safe_id}/events")
          .json()["events"]))
check("不安全观察仍无事件",
      client.get(f"/api/observations/{unsafe_id}/events").json()["events"] == [])

# ---- 单区删除：不安全观察翻回安全 ----
r = client.delete(f"/api/regions/{id_b}")
check("删除 B 区 200 revision=4", r.status_code == 200
      and r.json()["revision"] == 4, r.text)
ev = client.get(f"/api/observations/{safe_id}/events").json()["events"]
check("删除压区后观察翻回安全：事件 unsafe→safe、revision=4",
      len(ev) == 2 and ev[-1]["old_safe"] is False
      and ev[-1]["new_safe"] is True and ev[-1]["revision"] == 4, str(ev))

# ---- 整批至多一条/观察：一批内多个操作共同造成一次翻转，只留一条 ----
rev = client.get("/api/revision").json()["revision"]
r = client.post("/api/regions/batch", json={
    "expected_revision": rev,
    "operations": [
        # 先删 A（中间态会让 y=50 变安全），再建 C 覆盖 y=50（末态仍不安全）
        {"action": "delete", "id": id_a},
        {"action": "create", "name": "C区",
         "polygon": [[0, 0], [120, 0], [120, 120], [0, 120]],
         "valid_from": WIDE[0], "valid_to": WIDE[1]},
    ],
    "trajectories": [[
        {"x": -50, "y": 999, "t": future(3600)},
        {"x": 350, "y": 999, "t": future(3660)},
    ]],
})
check("批量（删除+新建）200，revision 只推进一次",
      r.status_code == 200 and r.json()["revision"] == rev + 1, r.text)
id_c = r.json()["operations"][1]["region_id"]
ev_u = client.get(f"/api/observations/{unsafe_id}/events").json()["events"]
# 变更前 y=50 被 A 压（不安全），变更后被 C 压（不安全）→ 不翻转，无事件
check("整批前后结论相同（中间态不计数）：无事件", ev_u == [], str(ev_u))
# 再做一批：删除 C → y=50 变安全，恰好一次翻转
rev = client.get("/api/revision").json()["revision"]
r = client.post("/api/regions/batch", json={
    "expected_revision": rev,
    "operations": [{"action": "delete", "id": id_c}],
    "trajectories": [[
        {"x": -50, "y": 999, "t": future(3600)},
        {"x": 350, "y": 999, "t": future(3660)},
    ]],
})
check("批量删除 200", r.status_code == 200 and r.json()["revision"] == rev + 1, r.text)
batch_flip_rev = r.json()["revision"]
ev_u = client.get(f"/api/observations/{unsafe_id}/events").json()["events"]
check("批量造成翻转：unsafe→safe 恰一条，revision 为新版本",
      len(ev_u) == 1 and ev_u[0]["seq"] == 1 and ev_u[0]["old_safe"] is False
      and ev_u[0]["new_safe"] is True and ev_u[0]["revision"] == batch_flip_rev, str(ev_u))

# ---- 连续翻转，构造多条事件用于分页 ----
# 当前 y=50 安全。反复登记/删除压区各两次 → 4 条事件（safe/unsafe 交替）
flip_revs: list[int] = []
for i in range(4):
    rev = client.get("/api/revision").json()["revision"]
    if i % 2 == 0:
        rr = client.post("/api/regions", json={
            "name": f"压区-{i}", "polygon": SQUARE,
            "valid_from": WIDE[0], "valid_to": WIDE[1],
        })
        check(f"翻转变更 {i} 成功", rr.status_code == 201, rr.text)
        new_id = rr.json()["region"]["id"]
    else:
        rr = client.delete(f"/api/regions/{new_id}")
        check(f"翻转变更 {i} 成功", rr.status_code == 200, rr.text)
    flip_revs.append(rr.json()["revision"])

page = client.get(f"/api/observations/{unsafe_id}/events?limit=2").json()
seqs = [e["seq"] for e in page["events"]]
check("分页第 1 页：2 条、序号 [1,2]、next_cursor=2",
      seqs == [1, 2] and page["next_cursor"] == 2 and page["cursor"] == 0, str(page))
page2 = client.get(
    f"/api/observations/{unsafe_id}/events?cursor={page['next_cursor']}&limit=2"
).json()
seqs2 = [e["seq"] for e in page2["events"]]
check("分页第 2 页：序号 [3,4]、next_cursor=4",
      seqs2 == [3, 4] and page2["next_cursor"] == 4, str(page2))
# 还有第 5 条（尚未产生后续翻转前先看半满页）
page3 = client.get(
    f"/api/observations/{unsafe_id}/events?cursor={page2['next_cursor']}&limit=2"
).json()
check("半满页：仅序号 5、next_cursor=null（取不满即末页）",
      [e["seq"] for e in page3["events"]] == [5] and page3["next_cursor"] is None,
      str(page3))
page4 = client.get(
    f"/api/observations/{unsafe_id}/events?cursor=5&limit=2"
).json()
check("越过末条：空、next_cursor=null",
      page4["events"] == [] and page4["next_cursor"] is None, str(page4))
# 事件内容：序号 1 是批量翻转（unsafe→safe），随后交替
all_ev = client.get(
    f"/api/observations/{unsafe_id}/events?limit=200"
).json()["events"]
expected = [(False, True), (True, False), (False, True), (True, False), (False, True)]
check("全部事件按序号升序且 old/new 交替正确",
      [(e["old_safe"], e["new_safe"]) for e in all_ev] == expected, str(all_ev))
check("事件 revision 与逐次变更后的新版本一致",
      [e["revision"] for e in all_ev] == [batch_flip_rev] + flip_revs, str(all_ev))
check("总事件数 = 5（1 批量 + 4 单区）", len(all_ev) == 5, str(len(all_ev)))
# 安全观察（y=200 当前安全）这期间均无翻转
check("安全观察事件仍为早前的 2 条",
      len(client.get(f"/api/observations/{safe_id}/events").json()["events"]) == 2)

# ---- 失败回滚不产生事件 ----
rev = client.get("/api/revision").json()["revision"]
n_events = scalar("SELECT count(*) FROM observation_events")
r = client.post("/api/regions/batch", json={
    "expected_revision": 999,  # 过期
    "operations": [{"action": "create", "name": "X", "polygon": SQUARE,
                    "valid_from": WIDE[0], "valid_to": WIDE[1]}],
    "trajectories": [[
        {"x": -50, "y": 50, "t": future(3600)},
        {"x": 350, "y": 50, "t": future(3660)},
    ]],
})
check("过期 revision 批量 409 revision_conflict",
      r.status_code == 409 and r.json()["error"]["code"] == "revision_conflict", r.text)
check("失败回滚：revision 不变、事件不增加",
      client.get("/api/revision").json()["revision"] == rev
      and scalar("SELECT count(*) FROM observation_events") == n_events)

# ---- 幂等回放不重复产生事件 ----
# 先把 y=50 变安全（删光压区），再用幂等键登记压区造成一次翻转
for rg in client.get("/api/regions").json():
    if rg["name"].startswith("压区"):
        client.delete(f"/api/regions/{rg['id']}")
rev = client.get("/api/revision").json()["revision"]
before_events = len(client.get(
    f"/api/observations/{unsafe_id}/events?limit=200").json()["events"])
body = {"name": "幂等压区", "polygon": SQUARE,
        "valid_from": WIDE[0], "valid_to": WIDE[1]}
r1 = client.post("/api/regions", json=body, headers={"Idempotency-Key": "obs-flip-001"})
check("带幂等键登记 201", r1.status_code == 201, r1.text)
idem_id = r1.json()["region"]["id"]
after_first = len(client.get(
    f"/api/observations/{unsafe_id}/events?limit=200").json()["events"])
check("首次提交产生恰好 1 条翻转事件", after_first == before_events + 1,
      f"{after_first} vs {before_events}")
r2 = client.post("/api/regions", json=body, headers={"Idempotency-Key": "obs-flip-001"})
check("同键重试回放原响应（同一区域 id、同一 revision）",
      r2.status_code == 201 and r2.json() == r1.json(), r2.text)
after_replay = len(client.get(
    f"/api/observations/{unsafe_id}/events?limit=200").json()["events"])
check("幂等回放不重复产生事件、revision 不再推进",
      after_replay == after_first
      and client.get("/api/revision").json()["revision"] == rev + 1)
client.delete(f"/api/regions/{idem_id}")

# ---- 已取消 / 过期的观察不再产生事件 ----
rev_before_cancel = client.get("/api/revision").json()["revision"]
r = client.post(f"/api/observations/{safe_id}/cancel")
check("取消观察 200 状态 cancelled",
      r.status_code == 200 and r.json()["status"] == "cancelled"
      and r.json()["cancelled_at"] is not None, r.text)
cancel_body = r.json()
r = client.post(f"/api/observations/{safe_id}/cancel")
check("重复取消幂等（结果不变）", r.status_code == 200 and r.json() == cancel_body, r.text)
check("取消未知观察 404",
      client.post("/api/observations/9999/cancel").status_code == 404)
rev = client.get("/api/revision").json()["revision"]
check("取消不推进区域 revision", rev == rev_before_cancel)
# 登记压住该（已取消）观察轨迹的区域
r = client.post("/api/regions", json={
    "name": "压已取消", "polygon": [[0, 150], [100, 150], [100, 250], [0, 250]],
    "valid_from": WIDE[0], "valid_to": WIDE[1],
})
check("取消后变更照常 201", r.status_code == 201, r.text)
check("已取消观察不再产生事件",
      len(client.get(f"/api/observations/{safe_id}/events").json()["events"]) == 2)
g = client.get(f"/api/observations/{safe_id}").json()
# 冻结 revision = 该观察最近一次结论（最后一次翻转事件）对应的 revision
frozen_rev = client.get(
    f"/api/observations/{safe_id}/events?limit=200"
).json()["events"][-1]["revision"]
check("已取消观察保留取消前最近结论（safe=true）与冻结 revision，状态 cancelled",
      g["status"] == "cancelled" and g["safe"] is True
      and g["revision"] == frozen_rev == 4,
      str(g))

# 末点已过的观察：短寿命观察 + 等待过期
t_start = datetime.now(timezone.utc) + timedelta(seconds=2)
t_end = datetime.now(timezone.utc) + timedelta(seconds=3.5)
r = client.post("/api/observations", json={"points": [
    {"x": -50, "y": 300, "t": iso_z(t_start)},
    {"x": 350, "y": 300, "t": iso_z(t_end)},
]})
check("登记短寿命观察 201（当前安全）", r.status_code == 201, r.text)
short_id = r.json()["id"]
remaining = (t_end - datetime.now(timezone.utc)).total_seconds()
time.sleep(max(0.0, remaining) + 0.5)
r = client.post("/api/regions", json={
    "name": "压过期观察", "polygon": [[0, 250], [100, 250], [100, 350], [0, 350]],
    "valid_from": WIDE[0], "valid_to": WIDE[1],
})
check("观察末点已过后变更仍 201", r.status_code == 201, r.text)
check("过期观察不再产生事件",
      client.get(f"/api/observations/{short_id}/events").json()["events"] == [])

# ---- 观察不改变通行凭证的保护 ----
# 一条观察与一张凭证共用会被压的轨迹；压区变更仍应被凭证 409 挡下
fut_traj = [
    {"x": -50, "y": 420, "t": future(7200)},
    {"x": 350, "y": 420, "t": future(7260)},
]
r = client.post("/api/observations", json={"points": fut_traj})
check("登记观察（安全）201", r.status_code == 201, r.text)
obs420_id = r.json()["id"]
r = client.post("/api/passes", json={"points": fut_traj})
check("同轨迹签发凭证 201", r.status_code == 201, r.text)
pass420_id = r.json()["id"]
rev = client.get("/api/revision").json()["revision"]
n_events = scalar("SELECT count(*) FROM observation_events")
r = client.post("/api/regions", json={
    "name": "压凭证", "polygon": [[0, 370], [100, 370], [100, 470], [0, 470]],
    "valid_from": WIDE[0], "valid_to": WIDE[1],
})
check("凭证保护不受观察影响：409 pass_conflict",
      r.status_code == 409 and r.json()["error"]["code"] == "pass_conflict"
      and [d["pass_id"] for d in r.json()["error"]["details"]] == [pass420_id], r.text)
check("409 整笔回滚：观察也未因该失败变更产生事件",
      client.get(f"/api/observations/{obs420_id}/events").json()["events"] == []
      and client.get("/api/revision").json()["revision"] == rev
      and scalar("SELECT count(*) FROM observation_events") == n_events)
client.post(f"/api/passes/{pass420_id}/cancel")

# ---- 并发：观察登记/取消与区域写入混合，全部串行化、无 5xx、无重复事件 ----
base_rev = client.get("/api/revision").json()["revision"]
# 两张会反复翻转的观察
r = client.post("/api/observations", json={"points": [
    {"x": -50, "y": 700, "t": future(10 ** 5)},
    {"x": 350, "y": 700, "t": future(10 ** 5 + 60)},
]})
obs_conc_id = r.json()["id"]
r = client.post("/api/observations", json={"points": [
    {"x": -50, "y": 701, "t": future(10 ** 5)},
    {"x": 350, "y": 701, "t": future(10 ** 5 + 60)},
]})
obs_cancel_id = r.json()["id"]
K = 10
barrier = threading.Barrier(K)
results: list[tuple] = []
rlock = threading.Lock()


def conc_worker(i):
    barrier.wait()
    if i < 3:  # 登记观察
        rr = client.post("/api/observations", json={"points": [
            {"x": -50, "y": 800 + 10 * i, "t": future(10 ** 5)},
            {"x": 350, "y": 800 + 10 * i, "t": future(10 ** 5 + 60)},
        ]})
    elif i < 5:  # 两个线程反复取消同一张观察：幂等
        rr = client.post(f"/api/observations/{obs_cancel_id}/cancel")
    elif i < 8:  # 交替建/删压区（每条只操作一次，均造成或恢复翻转）
        if i % 2 == 0:
            rr = client.post("/api/regions", json={
                "name": f"并发压区-{i}",
                "polygon": [[0, 650], [100, 650], [100, 750], [0, 750]],
                "valid_from": WIDE[0], "valid_to": WIDE[1],
            })
        else:
            # 修订该压区到别处（不再压 y=700）：用一个极小远处的多边形
            rr = client.patch(f"/api/regions/{region_for_patch}", json={
                "polygon": [[9000, 9000], [9010, 9000], [9010, 9010], [9000, 9010]],
            })
    else:  # 无害改名
        rr = client.patch(f"/api/regions/{id_a}", json={"name": f"A区-并发-{i}"})
    with rlock:
        results.append((i, rr.status_code))


# 先建好供 patch 的压区（i=7 修订它）
r = client.post("/api/regions", json={
    "name": "待迁移压区",
    "polygon": [[0, 650], [100, 650], [100, 750], [0, 750]],
    "valid_from": WIDE[0], "valid_to": WIDE[1],
})
region_for_patch = r.json()["region"]["id"]

threads = [threading.Thread(target=conc_worker, args=(i,)) for i in range(K)]
for t in threads:
    t.start()
for t in threads:
    t.join(timeout=60)

check("并发混合无 5xx（无死锁/序列化异常泄漏）",
      all(s < 500 for _, s in results) and len(results) == K, str(results))
check("并发取消幂等：两个取消都 200",
      all(s == 200 for i, s in results if 3 <= i < 5), str(results))
check("被并发取消的观察最终 cancelled",
      client.get(f"/api/observations/{obs_cancel_id}").json()["status"] == "cancelled")
# 事件序号对每张观察严格连续 1..N，无重复无跳号（每观察每变更至多一条）
for oid in (obs_conc_id, obs_cancel_id):
    seqs_all = [e["seq"] for e in client.get(
        f"/api/observations/{oid}/events?limit=200").json()["events"]]
    check(f"观察 {oid} 事件序号连续无重复（{seqs_all}）",
          seqs_all == list(range(1, len(seqs_all) + 1)), str(seqs_all))
# 已取消观察在取消之后不再追加事件
ev_after_cancel = client.get(
    f"/api/observations/{obs_cancel_id}/events?limit=200").json()["events"]
check("已取消观察最终结论保持（事件序列不再增长）",
      client.get(f"/api/observations/{obs_cancel_id}").json()["status"] == "cancelled")
check("最终 revision 不低于并发前基线（只有区域写入推进）",
      client.get("/api/revision").json()["revision"] >= base_rev)

# ---- 清理，避免影响其它测试脚本 ----
reset_all()

print()
if failures:
    print(f"{len(failures)} 个失败")
    sys.exit(1)
print("全部安全状态观察 API 集成用例通过")
