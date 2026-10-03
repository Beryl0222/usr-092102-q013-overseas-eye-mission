"""资源占用账本：从事件流重建设备、人员班次、耗材状态。

- 设备（如单台超声乳化仪）：时间窗区间互斥；
- 人员班次：排期区间必须落在 BATCH_REGISTERED 声明的班次窗口内，且同一人时间窗互斥；
- 耗材：期初库存 - 预占 - 实耗 = 可用；治疗完成后预占转实耗并核对差异。

账本本身是纯读模型；占用的写入（SLOT_RESERVED/RELEASED、SUPPLIES_CONSUMED）由命令服务
通过 EventStore.commit 原子追加，precheck 调用 assert_can_reserve 判定冲突。
"""

from collections import defaultdict

from src.errors import ResourceConflict
from src.events import parse_ts


def _overlaps(a_start, a_end, b_start, b_end) -> bool:
    return a_start < b_end and b_start < a_end


class ResourceLedger:
    def __init__(self, events: list[dict]) -> None:
        # 设备/班次：resource_id -> {"kind","label","windows":[班次窗口],"busy":[(s,e,corr,slot)]}
        self.resources: dict[str, dict] = {}
        # 耗材 code -> {"initial","holds":{corr:qty},"consumed":{corr:qty}}
        self.supplies: dict[str, dict] = {}
        self._replay(events)

    # ---------- 重放 ----------

    def _apply_batch(self, p: dict) -> None:
        for eq in p.get("equipment", []):
            rid = eq["resource_id"]
            self.resources[rid] = {"kind": "equipment", "label": eq.get("label", rid),
                                   "windows": [], "busy": []}
        for st in p.get("staffing", []):
            rid = st["resource_id"]
            res = self.resources.setdefault(rid, {"kind": "staff_shift",
                                                  "label": st.get("staff_id", rid),
                                                  "windows": [], "busy": []})
            res["kind"] = "staff_shift"
            res["windows"].append(
                (parse_ts(st["window_start"]), parse_ts(st["window_end"]))
            )
        for code, qty in p.get("supplies", {}).items():
            self.supplies[code] = {"initial": qty, "holds": {}, "consumed": {}}

    def _replay(self, events: list[dict]) -> None:
        ordered = sorted(events, key=lambda x: (x["occurred_at"], x["event_id"]))
        # 第一遍：批次登记先建账本（现场时间戳可能晚于补录的早期事件，不能让后到的登记冲掉占用）
        for e in ordered:
            if e["event_type"] == "BATCH_REGISTERED":
                self._apply_batch(e.get("payload", {}))
        # 第二遍：其余事件按发生时间重放
        for e in ordered:
            t = e["event_type"]
            if t == "BATCH_REGISTERED":
                continue
            p = e.get("payload", {})
            if t == "SLOT_RESERVED":
                kind = p["kind"]
                corr = p["correlation_id"]
                if kind in ("equipment", "staff_shift"):
                    res = self.resources.get(p["resource_id"])
                    if res is not None:
                        res["busy"].append((parse_ts(p["interval_start"]),
                                            parse_ts(p["interval_end"]), corr,
                                            p.get("purpose_slot_id")))
                elif kind == "supply":
                    code = p["supply_code"]
                    entry = self.supplies.setdefault(code, {"initial": 0, "holds": {}, "consumed": {}})
                    entry["holds"][corr] = entry["holds"].get(corr, 0) + p["qty"]
            elif t == "SLOT_RELEASED":
                corr = p["correlation_id"]
                kind = p["kind"]
                if kind in ("equipment", "staff_shift"):
                    res = self.resources.get(p["resource_id"])
                    if res is not None:
                        res["busy"] = [b for b in res["busy"] if b[2] != corr]
                elif kind == "supply":
                    self.supplies.get(p["supply_code"], {}).get("holds", {}).pop(corr, None)
            elif t == "SUPPLIES_CONSUMED":
                code = p["supply_code"]
                entry = self.supplies.setdefault(code, {"initial": 0, "holds": {}, "consumed": {}})
                # 该排期的预占转为实耗：释放 corr 预占（含余量退回），再记实耗
                entry["holds"].pop(p["correlation_id"], None)
                entry["consumed"][p["correlation_id"]] = (
                    entry["consumed"].get(p["correlation_id"], 0) + p["qty"]
                )

    # ---------- 查询 ----------

    def supply_available(self, code: str, *, exclude_corr: str | None = None) -> int:
        s = self.supplies.get(code)
        if s is None:
            return 0
        held = sum(q for c, q in s["holds"].items() if c != exclude_corr)
        return s["initial"] - held - sum(s["consumed"].values())

    def supply_state(self, code: str) -> dict:
        s = self.supplies.get(code, {"initial": 0, "holds": {}, "consumed": {}})
        held, consumed = sum(s["holds"].values()), sum(s["consumed"].values())
        return {"initial": s["initial"], "held": held, "consumed": consumed,
                "available": s["initial"] - held - consumed}

    def is_busy(self, resource_id: str, start, end, *, exclude_corr: str | None = None) -> bool:
        res = self.resources.get(resource_id)
        if res is None:
            return False
        return any(corr != exclude_corr and _overlaps(start, end, s, e)
                   for s, e, corr, _slot in res["busy"])

    def within_staff_window(self, resource_id: str, start, end) -> bool:
        res = self.resources.get(resource_id)
        if res is None or res["kind"] != "staff_shift":
            return True  # 设备不受班次窗口约束
        return any(ws <= start and end <= we for ws, we in res["windows"])

    def snapshot(self) -> dict:
        """例外批准时刻固化的资源快照。"""
        return {
            "equipment": {
                rid: {"label": r["label"],
                      "busy": [{"start": s.isoformat(), "end": e.isoformat(),
                                "correlation_id": c, "slot": slot}
                               for s, e, c, slot in r["busy"]]}
                for rid, r in self.resources.items() if r["kind"] == "equipment"
            },
            "staff": {
                rid: {"label": r["label"],
                      "windows": [{"start": ws.isoformat(), "end": we.isoformat()}
                                  for ws, we in r["windows"]],
                      "busy": [{"start": s.isoformat(), "end": e.isoformat(),
                                "correlation_id": c, "slot": slot}
                               for s, e, c, slot in r["busy"]]}
                for rid, r in self.resources.items() if r["kind"] == "staff_shift"
            },
            "supplies": {code: self.supply_state(code) for code in sorted(self.supplies)},
        }

    # ---------- 占用判定（commit precheck 用） ----------

    def assert_can_reserve(self, demands: list[dict], correlation_id: str) -> None:
        """对一批同 correlation 的占用需求做判定；冲突抛 ResourceConflict。

        demand: {"kind": equipment|staff_shift|supply, "resource_id"|"supply_code",
                 "interval_start","interval_end"(设备/班次), "qty"(耗材)}
        本批内的暂存占用也计入（ledger 由 commit 的投影事件构建），同 corr 不互斥。
        """
        local_supply: dict[str, int] = defaultdict(int)
        for d in demands:
            kind = d["kind"]
            if kind in ("equipment", "staff_shift"):
                rid = d["resource_id"]
                start, end = parse_ts(d["interval_start"]), parse_ts(d["interval_end"])
                if end <= start:
                    raise ValueError(f"{rid} 占用区间结束必须晚于开始")
                if rid not in self.resources:
                    raise ResourceConflict(f"资源不存在：{rid}")
                if not self.within_staff_window(rid, start, end):
                    raise ResourceConflict(f"{rid} 在 {d['interval_start']}~{d['interval_end']} 不在班次窗口内")
                if self.is_busy(rid, start, end, exclude_corr=correlation_id):
                    raise ResourceConflict(f"{rid} 在该时间窗已被其他排期占用")
                # 同一批里同一资源两次占用也要互斥（除非区间相同，即重复声明）
                for s, e, c, _ in self.resources[rid]["busy"]:
                    if c == correlation_id and _overlaps(start, end, s, e) \
                            and (s, e) != (start, end):
                        raise ResourceConflict(f"{rid} 在本批排期中时间窗自相重叠")
            elif kind == "supply":
                code = d["supply_code"]
                local_supply[code] += d["qty"]
            else:
                raise ValueError(f"未知资源类型：{kind}")

        for code, qty in local_supply.items():
            if code not in self.supplies:
                raise ResourceConflict(f"耗材未登记：{code}")
            if self.supply_available(code, exclude_corr=correlation_id) < qty:
                raise ResourceConflict(
                    f"耗材 {code} 不足：需要 {qty}，"
                    f"可用（不含本批预占）{self.supply_available(code, exclude_corr=correlation_id)}"
                )

    def assert_consumption(self, uses: dict[str, int], correlation_id: str) -> None:
        """治疗完成时：释放本 corr 预占后按实消耗，库存不得为负。

        投影事件中已包含本批的 SLOT_RELEASED 与 SUPPLIES_CONSUMED，
        故排除本 corr 的预占与实耗，再用实际用量对照其余库存。
        """
        for code, qty in uses.items():
            if code not in self.supplies:
                raise ResourceConflict(f"耗材未登记：{code}")
            s = self.supplies[code]
            other_holds = sum(q for c, q in s["holds"].items() if c != correlation_id)
            other_consumed = sum(q for c, q in s["consumed"].items() if c != correlation_id)
            if s["initial"] - other_holds - other_consumed < qty:
                raise ResourceConflict(
                    f"耗材 {code} 实际消耗 {qty} 超过可用 "
                    f"{s['initial'] - other_holds - other_consumed}"
                )
