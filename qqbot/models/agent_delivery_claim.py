"""agent_delivery_claims —— worker 投递去重 / 租约表(claim-with-lease)。

为什么存在(②"至少一次重发"修复 / ⑤异步worker调用 §6 关联):
  ToolWorker 靠 `NOT EXISTS(终态事件)` 找 pending,然后**先执行外部副作用(跑工具 /
  经工具发 napcat)、后写 *_result|*_failed**。这中间没有 claim/lease:
  - 多实例并发 → 同一条被两个 worker 同时取走、各发一次(代码注释自己承认"重复发送")
  - 单实例:发送成功后、写终态前进程崩 / 写终态那步抛瞬时 DB 异常 → 下一轮
    drain 把它当 pending 再发一次

  本表给"正在投递中"的事件加一把**带租约的锁**:worker 动手前先 `try_claim`
  抢占 event_id;抢到才执行,抢不到(他人持有未过期租约)就跳过。租约到期(默认
  120s)后允许重新抢占重试 → 仍保 at-least-once、但把"每次 drain 都重发"收敛成
  "每租约周期至多一次",并杜绝多实例并发重复。

与 append-only 的关系:
  这是**运维协调表**,不是事件流,允许 UPDATE。它不记录业务事实(那仍在
  agent_events),只记录"谁在什么时候领了哪条、租约到何时"。丢了也只是退回到
  "无去重"的旧行为,不影响正确性。

残留(诚实说明):彻底 exactly-once 需要 napcat 侧幂等发送(同一 dedup key 重复
  发只生效一次),不在本表能力内。崩溃后租约过期重试仍可能产生 1 次重复 ——
  见 delivery_claims.try_claim 注释。
"""

from sqlalchemy import Column, DateTime, Text

from qqbot.models.base import Base


class AgentDeliveryClaim(Base):
    __tablename__ = "agent_delivery_claims"

    # 被投递事件的 event_id(tool_called)。一条事件一把锁。
    event_id = Column(Text, primary_key=True)
    # "tool" —— 仅供排查观测
    kind = Column(Text, nullable=False)
    claimed_at = Column(DateTime(timezone=True), nullable=False)
    # 租约到期时刻;超过即视作 stale,可被重新抢占
    lease_until = Column(DateTime(timezone=True), nullable=False)

    def __repr__(self) -> str:
        return f"<AgentDeliveryClaim({self.event_id} {self.kind} until={self.lease_until})>"
